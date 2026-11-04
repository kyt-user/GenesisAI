"""Display untrusted text without sending terminal instructions."""

_CONTROL_BYTES = {i: f'\\u{i:04x}' for i in (*range(32), *range(127, 160)) if i not in (9, 10)}


def terminal_text(value):
    return str(value).translate(_CONTROL_BYTES)
