"""三级上下文压缩器。"""

from __future__ import annotations

import json
import re
from typing import Any


class ContextCompressor:
    """三级上下文压缩器。

    Level 1: > 8K token — 早期消息摘要化
    Level 2: > 15K token — 激进摘要 + 工具结果压缩
    Level 3: > 25K token — 紧急截断，仅保留当前轮
    """

    LEVELS = [
        {"threshold": 8000, "strategy": "early_summary"},
        {"threshold": 15000, "strategy": "aggressive_summary"},
        {"threshold": 25000, "strategy": "emergency_truncate"},
    ]

    def compress(self, messages: list[dict], *, store: Any = None, session_id: str = "") -> list[dict]:
        """压缩消息列表。"""
        token_estimate = self._estimate_tokens(messages)
        level = self._determine_level(token_estimate)

        if level == 0:
            return messages  # 未达阈值，不压缩

        # 分离当前轮和历史消息
        current_turn = self._extract_current_turn(messages)
        history = messages[:len(messages) - len(current_turn)]

        summary = ""
        if level >= 1 and history:
            # 将早期消息压缩为摘要
            summary = self._summarize(history[:-len(current_turn)] if current_turn else history)

        if level >= 2:
            # 激进压缩工具调用结果
            current_turn = self._compress_tool_results(current_turn)

        if level >= 3:
            # 紧急截断：仅保留当前轮用户消息
            current_turn = self._emergency_truncate(current_turn)

        result = []
        if summary:
            result.append({
                "role": "system",
                "content": f"[早期对话摘要]\n{summary}"
            })
        result.extend(current_turn)
        return result

    def _estimate_tokens(self, messages: list[dict]) -> int:
        """粗略估算 token 数（中文约 1.5 字/token，英文约 4 字符/token）。"""
        total_chars = 0
        chinese_chars = 0
        for m in messages:
            content = str(m.get("content", ""))
            total_chars += len(content)
            chinese_chars += len(re.findall(r'[\u4e00-\u9fff]', content))
        
        # 中文部分按 1.5 字/token，英文部分按 4 字符/token
        english_chars = total_chars - chinese_chars
        return int(chinese_chars / 1.5 + english_chars / 4)

    def _determine_level(self, tokens: int) -> int:
        """确定压缩级别。"""
        for i, level in enumerate(reversed(self.LEVELS)):
            if tokens > level["threshold"]:
                return len(self.LEVELS) - i
        return 0

    def _extract_current_turn(self, messages: list[dict]) -> list[dict]:
        """提取当前轮消息（最后一条用户消息及其后续）。"""
        for i in range(len(messages) - 1, -1, -1):
            if messages[i].get("role") == "user":
                return messages[i:]
        return messages

    def _summarize(self, messages: list[dict]) -> str:
        """生成结构化摘要。"""
        user_goals = []
        assistant_results = []
        changes = []
        source_refs = []

        for m in messages:
            role = m.get("role", "")
            content = str(m.get("content", ""))
            if role == "user" and len(content) < 200:
                user_goals.append(content[:100])
            elif role == "assistant":
                if "tool_call" in content or "function" in content:
                    assistant_results.append("工具调用")
                elif len(content) < 200:
                    assistant_results.append(content[:100])
            # 提取文件变更
            if "修改" in content or "创建" in content or "删除" in content:
                changes.append(content[:50])
            # 提取来源引用
            urls = re.findall(r'https?://[^\s]+', content)
            source_refs.extend(urls[:2])

        summary = {
            "user_goals": user_goals[:3],
            "assistant_results": assistant_results[:3],
            "changes": changes[:3],
            "source_refs": source_refs[:3],
        }
        return json.dumps(summary, ensure_ascii=False, indent=2)

    def _compress_tool_results(self, messages: list[dict]) -> list[dict]:
        """压缩工具调用结果，仅保留首尾。"""
        compressed = []
        for m in messages:
            content = str(m.get("content", ""))
            if m.get("role") == "tool" and len(content) > 500:
                # 保留首尾各 200 字符
                compressed_content = content[:200] + "\n...[已压缩]...\n" + content[-200:]
                compressed.append({**m, "content": compressed_content})
            else:
                compressed.append(m)
        return compressed

    def _emergency_truncate(self, messages: list[dict]) -> list[dict]:
        """紧急截断：仅保留最后一条用户消息和最近一条工具结果。"""
        result = []
        user_msg = None
        tool_result = None
        for m in reversed(messages):
            if m.get("role") == "user" and user_msg is None:
                user_msg = m
            elif m.get("role") == "tool" and tool_result is None:
                tool_result = m
            if user_msg and tool_result:
                break
        if tool_result:
            result.append(tool_result)
        if user_msg:
            result.append(user_msg)
        return result if result else messages[-1:]
