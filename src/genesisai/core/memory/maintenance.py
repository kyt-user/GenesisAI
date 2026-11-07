"""Persistent derived-work queue, drained independently of model turns."""
import hashlib
import json
import re
import threading
import time

from genesisai.core.jobs.storage import Slot, SlotBusy, save
from genesisai.core.persistence.database import encode
from genesisai.shared.ids import uid
from genesisai.shared.run_control import RunControl


class Maintenance:
    def __init__(self, session, *, limits):
        self.session, self.limits = session, limits
        self.handlers = {}
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._closing = threading.Event()
        self._restart_requested = False
        self._thread = None
        self._active = None
        self.scope = hashlib.sha256(encode([str(session.workspace), sorted(session.record.grants),
                                            session.record.output]).encode()).hexdigest()

    def register(self, name, handler):
        """Handlers rebuild derived data and observe cancellation_requested()."""
        if name in self.handlers or not callable(handler):
            raise ValueError('Maintenance handler requires a unique name and callable')
        self.handlers[name] = handler

    def enqueue(self, kind, payload=None):
        if kind not in self.handlers:
            raise ValueError('Unregistered maintenance job: ' + kind)
        payload = {} if payload is None else payload
        if not isinstance(payload, dict):
            raise ValueError('Maintenance input must be an object')
        with self.session.database.transaction() as db:
            existing = db.execute("SELECT id,payload FROM jobs WHERE workspace_id=? AND kind=? AND state='queued' ORDER BY updated",
                                  (self.session.record.workspace_id, kind)).fetchall()
            identity = next((row['id'] for row in existing
                if json.loads(row['payload']).get('scope') == self.scope
                and json.loads(row['payload'])['input'] == payload), None)
            if identity is None:
                identity = uid('maintenance')
                db.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?)', (identity, self.session.record.workspace_id,
                    kind, 'queued', encode({'input': payload, 'scope': self.scope, 'session_id': self.session.id}), time.time()))
        self.start()
        return identity

    def start(self):
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                self._start_locked()
            elif self._closing.is_set():
                # A new submission must not clear an older execution's shutdown
                # signal. Let that generation finish and requeue its derived work.
                self._restart_requested = True
            self._wake.set()

    def _start_locked(self):
        self._closing = threading.Event()
        self._restart_requested = False
        self._thread = threading.Thread(target=self._drain, args=(self._closing,),
                                        name='memory-maintenance', daemon=True)
        self._thread.start()

    def status(self, identity=None, *, offset=0, limit=50):
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError('Invalid maintenance page')
        if identity:
            rows = self.session.database.rows('SELECT * FROM jobs WHERE id=? AND workspace_id=?',
                (identity, self.session.record.workspace_id))
            if not rows or json.loads(rows[0]['payload']).get('scope') != self.scope:
                raise ValueError('维护作业不存在或不属于当前授权范围')
            return self._present(rows[0])
        rows = self.session.database.rows("SELECT * FROM jobs WHERE workspace_id=? AND json_extract(payload,'$.scope')=? ORDER BY updated DESC,id LIMIT ? OFFSET ?",
            (self.session.record.workspace_id, self.scope, limit + 1, offset))
        return {'jobs': [self._present(row) for row in rows[:limit]],
                'next_offset': offset + limit if len(rows) > limit else None}

    def _marker(self, identity):
        if not isinstance(identity, str) or not re.fullmatch(r'maintenance_[a-f0-9]{16}', identity):
            raise ValueError('维护作业编号无效')
        return self.session.root / 'maintenance' / 'cancel' / (identity + '.json')

    def _present(self, row):
        value = {**{key: value for key, value in row.items() if key != 'payload'}, **json.loads(row['payload'])}
        if self._marker(row['id']).exists():
            value['cancel_requested'] = True
            if value['state'] == 'queued':
                value['state'] = 'cancelled'
        return value

    def cancel(self, identity):
        prior = self.status(identity)
        if prior['state'] not in {'queued', 'running'}:
            return prior
        # Do not wait for a rebuilding transaction to release SQLite's writer
        # lock. A filesystem signal works across instances and processes.
        marker = self._marker(identity)
        marker.parent.mkdir(parents=True, exist_ok=True)
        save(marker, {'requested': time.time(), 'scope': self.scope})
        with self._lock:
            if self._active and self._active[0] == identity:
                self._active[1].cancel()
        self._wake.set()
        return self.status(identity)

    def wait(self, identity, seconds=0):
        if type(seconds) not in (int, float) or not 0 <= seconds <= 30:
            raise ValueError('维护等待须为0到30秒')
        deadline = time.monotonic() + seconds
        while True:
            state = self.status(identity)
            if state['state'] not in {'queued', 'running'} or time.monotonic() >= deadline:
                return state
            time.sleep(min(.05, max(0, deadline - time.monotonic())))

    def _slot(self):
        pools = self.session.root / 'jobs' / 'slots', self.session.root / 'maintenance' / 'slots'
        for pool in pools:
            pool.mkdir(parents=True, exist_ok=True)
        settings = self.session.root / 'maintenance' / 'limits.json'
        values = json.loads(settings.read_text(encoding='utf-8')) if settings.exists() else {'max_concurrent': 1}
        if not isinstance(values, dict) or set(values) != {'max_concurrent'} or type(values['max_concurrent']) is not int or values['max_concurrent'] < 1:
            raise ValueError('maintenance/limits.json 必须包含正整数 max_concurrent')
        maintenance = None
        for index in range(values['max_concurrent']):
            try:
                maintenance = Slot(pools[1] / f'{index}.lock')
                break
            except SlotBusy:
                continue
        if maintenance is None:
            return None
        try:
            for index in range(self.limits()['max_concurrent']):
                try:
                    return maintenance, Slot(pools[0] / f'{index}.lock')
                except SlotBusy:
                    continue
        except BaseException:
            maintenance.close()
            raise
        maintenance.close()
        return None

    def _queued(self):
        return self.session.database.rows("SELECT * FROM jobs WHERE workspace_id=? AND state='queued' AND json_extract(payload,'$.scope')=? ORDER BY updated,id",
            (self.session.record.workspace_id, self.scope))

    def _claim(self):
        with self.session.database.transaction() as db:
            rows = db.execute("SELECT * FROM jobs WHERE workspace_id=? AND state='queued' AND json_extract(payload,'$.scope')=? ORDER BY updated,id",
                              (self.session.record.workspace_id, self.scope)).fetchall()
            for row in rows:
                if row['kind'] not in self.handlers:
                    continue
                if self._marker(row['id']).exists():
                    db.execute("UPDATE jobs SET state='cancelled',updated=? WHERE id=?", (time.time(), row['id']))
                    continue
                db.execute("UPDATE jobs SET state='running',updated=? WHERE id=?", (time.time(), row['id']))
                return dict(row), json.loads(row['payload'])
        return None

    def _progress(self, identity, control, stopping, value):
        with self.session.database.transaction() as db:
            row = db.execute('SELECT payload FROM jobs WHERE id=?', (identity,)).fetchone()
            payload = json.loads(row['payload'])
            if self._marker(identity).exists() or payload.get('cancel_requested') or stopping.is_set():
                control.cancel()
            payload['progress'] = value
            db.execute('UPDATE jobs SET payload=?,updated=? WHERE id=?', (encode(payload), time.time(), identity))

    def _execute(self, job, payload, stopping):
        identity = job['id']
        control = RunControl(lambda kind, detail: self._progress(identity, control, stopping, detail))
        with self._lock:
            self._active = identity, control
        state = 'completed'
        finished = threading.Event()
        def monitor():
            while not finished.wait(.05):
                if self._marker(identity).exists() or stopping.is_set():
                    control.cancel()
                    return
        observer = threading.Thread(target=monitor, name='maintenance-cancellation', daemon=True)
        observer.start()
        try:
            self._progress(identity, control, stopping, {'phase': 'starting'})
            if not control.cancelled.is_set():
                payload['result'] = control.execute(lambda: self.handlers[job['kind']](payload['input']))
                encode(payload['result'])
            if control.cancelled.is_set():
                state = 'cancelled'
        except BaseException as exc:
            payload.pop('result', None)
            state = 'cancelled' if control.cancelled.is_set() or getattr(exc, 'error_type', None) == 'cancelled' else 'failed'
            payload['error'] = self.session.get_logger().journal.clean(f'{type(exc).__name__}: {exc}')
            if state == 'failed':
                self.session.get_logger().error('memory_maintenance_failed', exc, job_id=identity)
        finally:
            finished.set()
            observer.join()
            with self._lock:
                self._active = None
        with self.session.database.transaction() as db:
            current = json.loads(db.execute('SELECT payload FROM jobs WHERE id=?', (identity,)).fetchone()['payload'])
            payload.update({key: current[key] for key in ('progress', 'cancel_requested') if key in current})
            if self._marker(identity).exists() or current.get('cancel_requested'):
                payload['cancel_requested'] = True
                state = 'cancelled'
            elif stopping.is_set() and state == 'cancelled':
                state = 'queued'
            db.execute('UPDATE jobs SET state=?,payload=?,updated=? WHERE id=?', (state, encode(payload), time.time(), identity))

    def _drain(self, stopping):
        idle_until = time.monotonic() + 2
        failed = False
        try:
            while not stopping.is_set():
                cancelled = [row['id'] for row in self._queued() if self._marker(row['id']).exists()]
                if cancelled:
                    with self.session.database.transaction() as db:
                        db.executemany("UPDATE jobs SET state='cancelled',updated=? WHERE id=? AND state='queued'",
                                       [(time.time(), identity) for identity in cancelled])
                slots, job = self._slot(), None
                if slots:
                    try:
                        job = self._claim()
                        if job:
                            self._execute(*job, stopping)
                    finally:
                        for slot in slots:
                            slot.close()
                if job or any(row['kind'] in self.handlers for row in self._queued()):
                    idle_until = time.monotonic() + 2
                if time.monotonic() >= idle_until:
                    with self._lock:
                        if not self._wake.is_set():
                            return
                if not job:
                    self._wake.wait(.1)
                    self._wake.clear()
        except BaseException as exc:
            failed = True
            self.session.get_logger().error('memory_maintenance_scheduler_failed', exc)
        finally:
            pending = not failed and not stopping.is_set() and any(
                row['kind'] in self.handlers for row in self._queued())
            with self._lock:
                if self._thread is threading.current_thread():
                    self._thread = None
                    if not failed and (self._restart_requested or
                            (not stopping.is_set() and (pending or self._wake.is_set()))):
                        self._start_locked()

    def close(self):
        with self._lock:
            self._restart_requested = False
            self._closing.set()
            self._wake.set()
            if self._active:
                self._active[1].cancel()
            thread = self._thread
        if thread and thread is not threading.current_thread():
            thread.join(2)

    @staticmethod
    def recover_after_host_lock(database):
        """The exclusive application host requeues interrupted derived work."""
        with database.transaction() as db:
            for row in db.execute("SELECT id,payload FROM jobs WHERE state='running'").fetchall():
                if not re.fullmatch(r'maintenance_[a-f0-9]{16}', row['id']):
                    continue
                payload = json.loads(row['payload'])
                marker = database.root / 'maintenance' / 'cancel' / (row['id'] + '.json')
                cancelled = marker.exists() or payload.get('cancel_requested')
                if cancelled:
                    payload['cancel_requested'] = True
                db.execute('UPDATE jobs SET state=?,payload=?,updated=? WHERE id=?',
                           ('cancelled' if cancelled else 'queued', encode(payload), time.time(), row['id']))
