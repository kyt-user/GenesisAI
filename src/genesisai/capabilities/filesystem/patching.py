"""最小 unified diff 应用器，只修改调用参数指定的单个文本文件。"""

from __future__ import annotations

import re

from genesisai.shared.security import ToolError


HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def apply_unified_patch(text: str, patch: str, newline: str) -> str:
    source = text.splitlines()
    patch_lines = patch.splitlines()
    result: list[str] = []
    cursor = 0
    index = 0
    hunks = 0
    while index < len(patch_lines):
        line = patch_lines[index]
        match = HUNK.match(line)
        if not match:
            if line.startswith(('--- ', '+++ ', 'diff ', 'index ')) or not line.strip():
                index += 1
                continue
            raise ToolError('invalid_patch', '补丁包含无法识别的 unified diff 内容')
        hunks += 1
        old_start = int(match.group(1))
        if old_start - 1 < cursor or old_start - 1 > len(source):
            raise ToolError('patch_conflict', '补丁行号与当前文件不匹配')
        result.extend(source[cursor:old_start - 1])
        cursor = old_start - 1
        index += 1
        while index < len(patch_lines) and not HUNK.match(patch_lines[index]):
            entry = patch_lines[index]
            if entry.startswith(('--- ', '+++ ', 'diff ', 'index ')):
                break
            if entry == r'\ No newline at end of file':
                index += 1
                continue
            if not entry or entry[0] not in {' ', '+', '-'}:
                raise ToolError('invalid_patch', '补丁 hunk 内容无效')
            marker, content = entry[0], entry[1:]
            if marker in {' ', '-'}:
                if cursor >= len(source) or source[cursor] != content:
                    raise ToolError('patch_conflict', '补丁上下文与当前文件不一致')
                if marker == ' ':
                    result.append(source[cursor])
                cursor += 1
            else:
                result.append(content)
            index += 1
    if not hunks:
        raise ToolError('invalid_patch', '补丁没有 hunk')
    result.extend(source[cursor:])
    output = newline.join(result)
    if text.endswith(('\n', '\r')):
        output += newline
    return output

