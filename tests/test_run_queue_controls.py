from concurrent.futures import wait
from threading import Event, get_ident

import pytest

from genesisai.app.run_queue import RunQueue


@pytest.mark.parametrize('finish_normally', [False, True])
def test_revocation_waits_for_runner_and_uses_terminal_owner(finish_normally):
    queue = RunQueue()
    entered, release = Event(), Event()
    calls = []
    def run():
        entered.set()
        assert release.wait(5)
        calls.append(('run_stopped', get_ident()))
        return {'status': 'cancelled'}
    queue.start(run)
    assert entered.wait(5)
    result = queue.defer_until_stopped(lambda: calls.append(('revoked', get_ident())))
    assert queue.control.cancelled.is_set()
    assert not result.done()
    release.set()
    assert wait([queue.future], timeout=5).done
    assert [item[0] for item in calls] == ['run_stopped']
    if finish_normally:
        queue.finish()
    queue.close()
    assert result.result() is None
    assert [item[0] for item in calls] == ['run_stopped', 'revoked']
    assert calls[0][1] != get_ident()
    assert calls[1][1] == get_ident()


def test_one_control_failure_does_not_discard_other_revocations():
    queue = RunQueue()
    queue.start(lambda: {'status': 'completed'})
    def fail():
        raise ValueError('unknown authorization')
    failure = queue.defer_until_stopped(fail)
    success = queue.defer_until_stopped(lambda: 'revoked')
    queue.close()
    with pytest.raises(ValueError, match='unknown authorization'):
        failure.result()
    assert success.result() == 'revoked'
