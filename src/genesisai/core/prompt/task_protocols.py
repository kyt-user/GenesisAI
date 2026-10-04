"""任务协议选择：根据用户输入与活动任务推断协议集合与继续意图。

从 `agent/loop.py` 拆出（规格 §6），属 L4 core；agent 经 `PromptPort` 调用，
自身不 import 任何 core.*。
"""

from __future__ import annotations

import re

_CONTINUATION_EXACT = {
    '开始', '开始吧', '继续', '继续吧', '继续实现', '继续执行', '开始实现', '开始执行',
    '开始写', '动手吧', '就这样', '按这个来',
    '按默认来', '按你的默认来', '就按默认来', '就按你的默认来', '照默认来',
    '可以', '可以开始', '同意', '确认', '没问题', '照这个方案做', '按这个方案做',
}
_DEFAULT_PLAN_EXACT = {
    '就这样', '按这个来', '按默认来', '按你的默认来', '就按默认来',
    '就按你的默认来', '照默认来', '照这个方案做', '按这个方案做', '可以', '同意', '确认',
}


def _normalize(text: str) -> str:
    """去除空白与常见中英文标点，并转小写。"""
    return re.sub(r'[\s，。！？!,.；;：:]+', '', (text or '').casefold())


class TaskProtocolSelector:
    """按关键词判定继续意图与所需协议集合（原 Runner 静态谓词，属 L4 core）。"""

    def is_development_continuation(self, text: str, active_task) -> bool:
        """判断短文本是否为开发任务的继续指令。"""
        if not active_task or active_task.get('status') in {'verified', 'cancelled'}:
            return False
        value = _normalize(text)
        if len(value) > 24:
            return False
        return value in _CONTINUATION_EXACT

    def accepts_default_plan(self, text: str) -> bool:
        """判断文本是否表示接受默认计划。"""
        return _normalize(text) in _DEFAULT_PLAN_EXACT

    def requests_development_execution(self, text: str, protocols) -> bool:
        """判断用户输入是否明确要求执行开发操作。"""
        if 'coding' not in protocols:
            return False
        value = text.casefold()
        if any(phrase in value for phrase in ('先讨论', '先规划', '有什么要问', '暂时不要写', '不要修改')):
            return False
        return any(word in value for word in ('实现', '修复', '编写', '创建', '构建', '开发', '修改'))

    def protocols_for(self, text: str) -> list:
        """根据用户输入关键词推断所需的协议列表。"""
        value = text.casefold()
        result = []
        if any(word in value for word in ('代码', '仓库', '修复', 'bug', '重构', '实现', '报错', '编写', '开发', '函数', '类', 'python', 'pyproject', 'pip', 'ruff', 'mypy')):
            result.append('coding')
        if any(word in value for word in ('测试', 'pytest', 'unittest', 'npm test', 'cargo test', '跑测试', '跑一下测试')):
            result.append('testing')
        if any(word in value for word in ('git', 'diff', '提交记录', '分支')):
            result.append('git_read')
        if any(word in value for word in ('文件', '目录', '移动', '重命名', '删除', '整理')):
            result.append('file_management')
        if any(word in value for word in ('docx', 'xlsx', 'pptx', 'pdf', 'word', 'excel', 'powerpoint', '办公文档')):
            result.append('office')
        if any(word in value for word in ('调试', 'debug', '排查', '为什么失败', '异常', 'traceback', '错误信息')):
            result.append('debugging')
        if any(word in value for word in ('写小说', '续写', '扩写', '章节', '故事', '剧情', '角色', '世界观', '创作', '写作')):
            result.append('creative_writing')
        if any(word in value for word in ('修改', '润色', '改写', '修订', '审稿')):
            result.append('revision')
        return result