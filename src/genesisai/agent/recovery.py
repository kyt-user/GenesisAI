"""Agent 的模型轮次与工具协议恢复策略（从 Runner 拆出）。"""

from __future__ import annotations

import json
import re
import time


SOURCE_REF = re.compile(r"\b(src_[0-9a-f]{16})\b")
PRICE_CLAIM = re.compile(
    r"(?:(CNY|RMB|USD|EUR|GBP|JPY|HKD|AUD|CAD|人民币|美元|欧元|英镑|日元|港元|[¥￥$€£])\s*([0-9][0-9,.]*))"
    r"|(?:([0-9][0-9,.]*)\s*(CNY|RMB|USD|EUR|GBP|JPY|HKD|AUD|CAD|人民币|美元|欧元|英镑|日元|港元|元))",
    re.I,
)
CURRENCY_ALIASES = {
    'cny': 'CNY', 'rmb': 'CNY', '人民币': 'CNY', '元': 'CNY', '¥': 'CNY', '￥': 'CNY',
    'usd': 'USD', '美元': 'USD', '$': 'USD', 'eur': 'EUR', '欧元': 'EUR', '€': 'EUR',
    'gbp': 'GBP', '英镑': 'GBP', '£': 'GBP', 'jpy': 'JPY', '日元': 'JPY',
    'hkd': 'HKD', '港元': 'HKD', 'aud': 'AUD', 'cad': 'CAD',
}


class RecoveryMixin:
    """模型调用、输出长度/空输出恢复、工具协议校验与回答证据校验。"""

    def _can_reuse_answer(self, request, answer):
        value = request.casefold()
        if any(word in value for word in ('价格', '售价', '多少钱', '美元', '欧元', '人民币', 'price', 'cost')):
            return bool(self._price_amounts(answer))
        if any(word in value for word in ('日期', '时间', '哪天', '年份', '月份', 'date', 'time')):
            return bool(re.search(r'\b(?:19|20)\d{2}(?:[-年/.]\d{1,2})?', answer))
        if any(word in value for word in ('标题', '名称', '规格', '编号', 'title', 'name')):
            return False
        return True

    def _call_model(self, context, tools):
        """调用模型并记录用量，支持可重试错误的自动重试。"""
        d = self.store.data; d['rounds'] += 1; self.store.save(); self.on_event('model', dict(round=d['rounds']))
        state = d['run_runtime']; call_id = self.trace.model_start(state); started = time.monotonic(); attempts = 0
        while True:
            try:
                options = {}
                if tools == []:
                    profile = state.get('profile')
                    if profile == 'web_quick':
                        options['max_tokens'] = 900
                    elif profile == 'direct_answer':
                        options['max_tokens'] = 600
                    # 流式输出纯文本轮次，让用户实时看到 token。
                    collected: list[str] = []
                    reasoning_collected: list[str] = []
                    final_response = None
                    try:
                        stream = self.model.stream_chat(context, tools=[], **options)
                    except TypeError as exc:
                        # 兼容仍暴露旧版流式签名的第三方 ModelClient 实现。
                        if options and 'unexpected keyword argument' in str(exc):
                            stream = self.model.stream_chat(context, tools=[])
                        else:
                            raise
                    for chunk in stream:
                        if isinstance(chunk, tuple) and chunk[0] == 'reasoning':
                            reasoning_collected.append(chunk[1])
                            self.on_event('reasoning_token', {})
                        elif isinstance(chunk, str):
                            collected.append(chunk)
                            self.on_event('stream_token', {'text': chunk})
                        else:
                            final_response = chunk
                    if final_response is None:
                        from genesisai.shared.messages import Response
                        final_response = Response(content=''.join(collected))
                    # 如果最终响应缺少推理内容，则补充已收集的推理。
                    if reasoning_collected and not final_response.reasoning:
                        final_response.reasoning = ''.join(reasoning_collected)
                    response = final_response; break
                response = self.model.chat(context, tools=tools, **options); break
            except Exception as exc:
                retryable = type(exc).__name__ in {'APIConnectionError', 'APITimeoutError', 'RateLimitError', 'InternalServerError'}
                if retryable and attempts < 1:
                    attempts += 1; state['recovery_counts']['provider'] += 1
                    self.store.event('model_retry', model_call_id=call_id, code=type(exc).__name__, retry_count=attempts); continue
                return self.fail(exc, reason='model_error' if type(exc).__name__ in {'APIConnectionError', 'APITimeoutError', 'RateLimitError', 'InternalServerError', 'AuthenticationError', 'PermissionDeniedError', 'BadRequestError'} else 'runtime_error')
        usage = response.usage or {}
        for key in ('prompt_tokens', 'completion_tokens', 'total_tokens'):
            if isinstance(usage.get(key), int): d['usage'][key] = d['usage'].get(key, 0) + usage[key]
        self.research.note_model(usage.get('total_tokens', 0) if isinstance(usage.get('total_tokens'), int) else 0)
        self.trace.model_end(call_id, elapsed_ms=round((time.monotonic() - started) * 1000), finish_reason=response.finish_reason, usage={k: usage[k] for k in ('prompt_tokens', 'completion_tokens', 'total_tokens') if isinstance(usage.get(k), int)}, state=state)
        if time.time() >= d['deadline'] and not state['evidence_refs']: return self.stop('limit_reached', '模型返回时已超过运行截止时间')
        return response

    def _handle_length(self, content):
        """处理模型输出达到长度上限的情况。"""
        state = self.store.data['run_runtime']
        if content: state['partial_response'] = (state.get('partial_response', '') + '\n' + content).strip()
        state['recovery_counts']['length'] += 1; self.store.save()
        if state['recovery_counts']['length'] > 1:
            state['stop_reason'] = 'second_length'; return self.status('partial', state['partial_response'] or '模型输出两次达到长度上限。')
        state['phase'] = 'recover'; state['stop_reason'] = 'finish_reason_length'; self.machine.transition('recover', 'finish_reason_length', save=False); self.store.save(); return None

    def _recover_serialized_tool_output(self):
        """模型输出的工具协议文本不可执行，也不可作为回答交付。"""
        state = self.store.data['run_runtime']
        counts = state['recovery_counts']
        counts['serialized_tool_output'] = counts.get('serialized_tool_output', 0) + 1
        self.store.event('tool_protocol_error', code='serialized_tool_output', retry_count=counts['serialized_tool_output'])
        state['response_issue'] = '上一条输出是错误的工具协议文本，已丢弃且未执行。请用一至三句自然语言回答；没有取得所问信息时说明缺口，不输出任何工具标记。'
        if counts['serialized_tool_output'] > 1 or self.store.data['rounds'] >= self.store.data['limits']['rounds']:
            return self.stop(
                'partial' if state['evidence_refs'] else 'failed',
                self._evidence_fallback('模型连续返回了不可用的工具协议文本。'),
                'invalid_model_output',
            )
        state['phase'] = 'recover'
        self.machine.transition('recover', 'serialized_tool_output')
        return None

    def _handle_empty(self):
        """处理模型返回空回答的情况。"""
        state = self.store.data['run_runtime']; state['recovery_counts']['empty'] += 1
        if state['recovery_counts']['empty'] > 1: return self.stop('failed', '模型连续返回空回答；已有运行进度已经保存。')
        state['phase'] = 'recover'; state['stop_reason'] = 'empty_response'; self.machine.transition('recover', 'empty_response', save=False); self.store.save(); return None

    def _invalid_calls(self, calls, definitions):
        """检查工具调用 ID 是否重复或调用了未提供的工具。"""
        ids = [call['id'] for call in calls]
        if len(ids) != len(set(ids)) or any(not value or value in self.store.data['calls'] for value in ids): return '模型返回重复或无效工具调用 ID'
        offered = {item['function']['name'] for item in definitions}
        if any(call['name'] not in offered for call in calls): return '模型调用了当前请求未提供的工具'
        return None

    def _recover_tool_protocol(self, message):
        """处理无效工具调用的恢复流程。"""
        state = self.store.data['run_runtime']; state['recovery_counts']['tool_protocol'] += 1
        if state['recovery_counts']['tool_protocol'] > 1:
            if state['evidence_refs']: self.research.force_answer('tool_protocol_repeated'); self.store.save(); return None
            return self.stop('failed', message)
        state['stop_reason'] = None; self.store.event('tool_protocol_error', code='invalid_tool_call', retry_count=1); self.store.save(); return None

    def _answer_refs_valid(self, content):
        """检查回答中的来源引用是否都已登记且可引用。"""
        refs = SOURCE_REF.findall(content)
        evidence = set(self.store.data['run_runtime']['evidence_refs'])
        sources = self.store.data['sources']
        return not refs or all(
            ref in sources and (ref in evidence or sources[ref].get('kind') == 'file')
            for ref in refs
        )

    @staticmethod
    def _price_claims(content):
        """提取文本中所有币种+金额声明。"""
        values = set()
        for match in PRICE_CLAIM.finditer(content or ''):
            currency = match.group(1) or match.group(4)
            raw = (match.group(2) or match.group(3)).replace(',', '')
            try:
                amount = str(float(raw)).rstrip('0').rstrip('.')
            except ValueError:
                continue
            normalized_currency = CURRENCY_ALIASES.get(currency.casefold())
            if normalized_currency:
                values.add((normalized_currency, amount))
        return values

    @classmethod
    def _price_amounts(cls, content):
        return {amount for _, amount in cls._price_claims(content)}

    def _unsupported_price_claims(self, content):
        """找出回答中未被已读正文支持的价格声明。"""
        claimed = self._price_claims(content)
        if not claimed:
            return []
        refs = SOURCE_REF.findall(content)
        if not refs:
            return sorted(claimed)
        supported = set()
        for ref in refs:
            path = self.store.root / 'sources' / (ref + '.json')
            try:
                source = json.loads(path.read_text(encoding='utf-8'))
            except (OSError, ValueError, TypeError):
                continue
            supported.update(self._price_claims(str(source.get('text', ''))))
        return [f'{currency} {amount}' for currency, amount in sorted(claimed - supported)]

    def _recover_bad_refs(self):
        """处理回答中引用了不存在来源的情况。"""
        state = self.store.data['run_runtime']
        counts = state['recovery_counts']
        counts['source_reference'] = counts.get('source_reference', 0) + 1
        self.store.event('answer_validation_error', code='invalid_source_reference', retry_count=counts['source_reference'])
        if counts['source_reference'] > 1:
            return self.stop(
                'partial' if state['evidence_refs'] else 'failed',
                self._evidence_fallback('模型连续生成了无法验证的来源引用。'),
                'invalid_source_reference',
            )
        allowed = ', '.join(state['evidence_refs']) or '无'
        state['response_issue'] = (
            '上一条回答引用了不存在或尚未读取的来源，已丢弃且不会进入会话历史。'
            f'只可引用以下 source_ref：{allowed}。没有有效来源时不要生成 source_ref。'
        )
        state['phase'] = 'recover'; state['stop_reason'] = 'invalid_source_reference'; self.machine.transition('recover', 'invalid_source_reference', save=False); self.store.save(); return None

    def _evidence_fallback(self, reason):
        """为终止消息补充已读取来源的信息。"""
        state = self.store.data['run_runtime']
        refs = [ref for ref in state.get('evidence_refs', []) if ref in self.store.data.get('sources', {})]
        if not refs:
            return reason + ' 本轮没有取得可引用正文。'
        sources = '、'.join(f'[{ref}]' for ref in refs[-3:])
        return reason + f' 已读取的来源为 {sources}，但当前问题尚未形成可验证答案。'

    def _recover_unsupported_prices(self, amounts):
        """处理回答中包含未支持价格的情况。"""
        state = self.store.data['run_runtime']
        counts = state['recovery_counts']
        counts['evidence_claim'] = counts.get('evidence_claim', 0) + 1
        self.store.event('answer_validation_error', code='unsupported_price_claim', retry_count=counts['evidence_claim'])
        if counts['evidence_claim'] > 1:
            return self.stop(
                'partial' if state['evidence_refs'] else 'failed',
                '未能生成由已读取正文支持的价格答复；已有资料已经保存。',
                'unsupported_evidence_claim',
            )
        state['phase'] = 'recover'
        state['stop_reason'] = 'unsupported_evidence_claim'
        state['response_issue'] = (
            '上一条回答包含已读取正文不支持的价格数字，已丢弃且不会进入会话历史。'
            '请只使用正文中与币种或价格单位相邻的金额；没有受支持金额时，以 [[PARTIAL]] 开头并明确尚未取得价格。'
        )
        self.machine.transition('recover', 'unsupported_price_claim', save=False)
        self.store.save()
        return None

    @staticmethod
    def model_error(exc):
        """将模型异常类型映射为用户可读的中文错误消息。"""
        messages = {'APIConnectionError': '无法连接模型服务。请检查服务地址和网络连接。', 'APITimeoutError': '模型服务响应超时，请稍后重试或调整超时配置。', 'AuthenticationError': '模型认证失败，请检查工作区根目录 .env 中的 API Key。', 'PermissionDeniedError': '模型服务拒绝访问，请检查账号与模型权限。', 'RateLimitError': '模型服务达到限额或请求频率限制，请稍后重试。', 'BadRequestError': '模型服务拒绝了请求，请检查模型名称和工具调用兼容性。', 'InternalServerError': '模型服务内部错误，请稍后重试；会话状态已经保存。', 'StopIteration': '模型调用了当前请求未提供的工具，模型请求已终止。'}
        return messages.get(type(exc).__name__, '运行失败：' + type(exc).__name__ + '。内部执行异常，进度已保存。')