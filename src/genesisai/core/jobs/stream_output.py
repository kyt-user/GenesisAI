"""Incremental UTF-8 output without delaying prompts or splitting secrets."""
import codecs


class StreamOutput:
    def __init__(self, clean):
        self.clean = clean
        self.owner = getattr(clean, '__self__', None)
        self.decoder = codecs.getincrementaldecoder('utf-8')(errors='replace')
        self.pending = ''

    def feed(self, chunk, *, final=False):
        text = self.pending + self.decoder.decode(chunk, final=final)
        secrets = sorted((value for value in getattr(self.owner, '_secrets', ())
                          if isinstance(value, str) and value), key=len, reverse=True)
        keep = 0
        for secret in secrets:
            for size in range(min(len(text), len(secret) - 1), keep, -1):
                if text.endswith(secret[:size]):
                    keep = size
                    break
        boundary = len(text) - keep
        # Never cut a complete short secret while retaining the prefix of a
        # longer or overlapping one (abc/abcdef and abab are both valid).
        changed = True
        while changed and boundary < len(text):
            changed = False
            for secret in secrets:
                start = text.find(secret)
                while start >= 0:
                    if start < boundary < start + len(secret):
                        boundary = start
                        changed = True
                        break
                    start = text.find(secret, start + 1)
        self.pending = text[boundary:]
        released = self.clean(text[:boundary])
        if final and self.pending:
            released += '[REDACTED]'
            self.pending = ''
        return released
