"""Apply unified hunks to a text stream without buffering the whole document."""

import re

from genesisai.shared.security import ToolError


HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def write_patch(source, output, patch, newline):
    entries = patch.splitlines()
    cursor = index = hunks = 0
    while index < len(entries):
        entry = entries[index]
        match = HUNK.match(entry)
        if not match:
            if entry.startswith(('--- ', '+++ ', 'diff ', 'index ')) or not entry.strip():
                index += 1
                continue
            raise ToolError('invalid_patch', '补丁包含无法识别的 unified diff 内容')
        hunks += 1
        old_start, old_count = int(match[1]), int(match[2] or 1)
        new_count = int(match[4] or 1)
        position = old_start if old_count == 0 else old_start - 1
        if position < cursor:
            raise ToolError('patch_conflict', '补丁行号与当前文件不匹配')
        while cursor < position:
            raw = source.readline()
            if not raw:
                raise ToolError('patch_conflict', '补丁行号超出文件')
            output.write(raw)
            cursor += 1
        index += 1
        removed = added = 0
        while index < len(entries) and not HUNK.match(entries[index]):
            entry = entries[index]
            if entry.startswith(('--- ', '+++ ', 'diff ', 'index ')) and removed == old_count and added == new_count:
                break
            if not entry or entry[0] not in {' ', '+', '-'}:
                raise ToolError('invalid_patch', '补丁 hunk 内容无效')
            marker, content = entry[0], entry[1:]
            no_newline = index + 1 < len(entries) and entries[index + 1] == r'\ No newline at end of file'
            if marker in {' ', '-'}:
                raw = source.readline()
                if not raw or raw.rstrip('\r\n') != content or (no_newline and raw.endswith(('\r', '\n'))):
                    raise ToolError('patch_conflict', '补丁上下文与当前文件不一致')
                cursor += 1
                removed += 1
                if marker == ' ':
                    output.write(raw)
                    added += 1
            else:
                output.write(content + ('' if no_newline else newline))
                added += 1
            index += 2 if no_newline else 1
        if (removed, added) != (old_count, new_count):
            raise ToolError('invalid_patch', '补丁 hunk 行数与声明不符')
    if not hunks:
        raise ToolError('invalid_patch', '补丁没有 hunk')
    while chunk := source.read(64 * 1024):
        output.write(chunk)
