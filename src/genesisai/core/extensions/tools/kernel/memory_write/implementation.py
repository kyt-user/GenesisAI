import hashlib


class Implementation:
    def prepare(self, context, args):
        self.prepared = context.runtime.memory.prepare_notes(args['entries'])
        return {
            "project_id": context.store.record.project_id,
            "workspace_id": context.store.record.workspace_id,
            'entries': [{**{k: v for k, v in entry.items() if k != 'body'},
                         'body_chars': len(entry['body']), 'body_preview': entry['body'][:160],
                         'body_sha256': hashlib.sha256(entry['body'].encode('utf-8')).hexdigest()}
                        for entry in self.prepared],
        }

    def execute(self, context, args):
        return context.runtime.memory.commit_notes(self.prepared)
