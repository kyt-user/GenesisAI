"""运行日志入口：所有完整输入输出统一追加到会话 JSONL。"""

from genesisai.core.state.io_journal import IOJournal


class RunLogger:
    def __init__(self, store):
        self.store = store
        self.journal = IOJournal(store)

    def record(self, event, **payload):
        return self.journal.write(event, **payload)

    def add_secret(self, value):
        self.journal.add_secret(value)

    def error(self, event, exc, **payload):
        return self.record(event, error=self.journal.exception(exc), **payload)

    def log_model_input(self, round_num, messages, tools, **details):
        self.record('model_input', round_num=round_num, messages=messages, tools=tools,
                    message_count=len(messages), tool_count=len(tools or []), **details)

    def log_model_output(self, round_num, response, elapsed_ms, **details):
        self.record('model_output', round_num=round_num, response=response,
                    elapsed_ms=elapsed_ms, finish_reason=response.finish_reason,
                    usage=response.usage or {}, tool_call_count=len(response.tool_calls or []), **details)

    def log_tool_execution_start(self, call_id, tool_name, arguments, **details):
        self.record('tool_execution', phase='start', call_id=call_id,
                    tool_name=tool_name, arguments=arguments, **details)

    def log_tool_execution_end(self, call_id, tool_name, result, elapsed_ms, **details):
        outcome = '待确认' if result.get('pending') else ('成功' if result.get('ok') else '失败')
        self.record('tool_execution', phase='end', call_id=call_id, tool_name=tool_name,
                    result=result, elapsed_ms=elapsed_ms, success=result.get('ok', False),
                    outcome=outcome, **details)

    def log_round_summary(self, round_num, status, answer=None):
        self.record('run_summary', round_num=round_num, status=status, answer=answer,
                    answer_length=len(answer or ''), usage=self.store.data.get('usage', {}))
