from io import StringIO
import time
from types import SimpleNamespace

from rich.console import Console

from genesisai.agent.loop import Runner
from genesisai.app.terminal_view import CliView
from genesisai.model.providers.deepseek import DeepSeekClient
from genesisai.core.tools.tool_runtime import ToolRuntime
from genesisai.shared.messages import Response
from genesisai.shared.security import Access
from genesisai.core.state.store import Store


def test_reasoning_is_hidden_and_html_stream_is_markup_safe():
    stream = StringIO()
    view = CliView(Console(file=stream, width=120, force_terminal=False))

    view.render_progress("reasoning_token", {"text": "<secret>internal reasoning</secret>"})
    view.render_progress("stream_token", {"text": "<!doc"})
    view.render_progress("stream_token", {"text": "type html><script>alert(1)</script>"})

    output = stream.getvalue()
    assert "internal reasoning" not in output
    assert "正在规划" in output
    assert "<!doctype html><script>alert(1)</script>" in output


def test_stream_no_tools_disables_deepseek_thinking_and_keeps_override():
    calls = []

    def create(**kwargs):
        calls.append(kwargs)
        return iter([
            SimpleNamespace(
                usage=None,
                choices=[SimpleNamespace(
                    finish_reason="stop",
                    delta=SimpleNamespace(content="done", reasoning_content=None, tool_calls=None),
                )],
            )
        ])

    model = object.__new__(DeepSeekClient)
    model.model, model.generation = "fixture", {}
    model.reasoning_enabled, model.reasoning_effort = True, "low"
    model.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))

    list(model.stream_chat([], tools=[], max_tokens=321))

    assert calls[0]["tool_choice"] == "none"
    assert calls[0]["extra_body"] == {"thinking": {"type": "disabled"}}
    assert calls[0]["max_tokens"] == 321


def test_runner_passes_final_stream_token_limit(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = Store(workspace / ".genesis", workspace=workspace)
    store.data.update(grants=[str(workspace)], output=str(workspace), deadline=time.time() + 300)
    store.save()
    runtime = ToolRuntime(
        store,
        Access([workspace], workspace, store.root, workspace_root=workspace),
        permission_mode="auto",
        providers=[],
    )

    class StreamingModel:
        def __init__(self):
            self.options = None

        def stream_chat(self, messages, tools=None, **kwargs):
            self.options = kwargs
            yield Response(content="done", finish_reason="stop")

        def chat(self, messages, tools=None, **kwargs):
            raise AssertionError("expected streaming")

    model = StreamingModel()
    runner = Runner(model, runtime)

    response = runner._call_model(runner.context(), [])

    assert response.content == "done"
    assert model.options["max_tokens"] == 600


def test_ordinary_cli_answer_is_visible_before_final_chunk(tmp_path):
    from test_development_continuation import development_env

    _, _, _, store, runtime = development_env(tmp_path)
    sink = StringIO()
    view = CliView(Console(file=sink, force_terminal=False))

    class Model:
        def stream_chat(self, messages, tools=None, **options):
            assert any(tool['function']['name'] == 'read_files' for tool in tools)
            yield '王维'
            assert '王维' in sink.getvalue() and '鹿柴' not in sink.getvalue()
            yield '的《鹿柴》。'
            yield Response(content='王维的《鹿柴》。', finish_reason='stop')

    with view.working():
        result = Runner(Model(), runtime, on_event=view.render_progress).start('推荐一首古诗')
    view.render_result(result, store.data)
    assert result['status'] == 'completed'
    assert sink.getvalue().count('王维的《鹿柴》。') == 1
    assert store.data['messages'][-1]['content'] == '王维的《鹿柴》。'


def test_stream_assembles_fragmented_call_alongside_reasoning_and_usage():
    def chunk(delta, finish=None):
        return SimpleNamespace(usage=None, choices=[SimpleNamespace(delta=delta, finish_reason=finish)])

    def delta(reasoning, text, name, arguments, call_id=None):
        call = SimpleNamespace(index=0, id=call_id, function=SimpleNamespace(name=name, arguments=arguments))
        return SimpleNamespace(reasoning_content=reasoning, content=text, tool_calls=[call])

    chunks = [
        chunk(delta('private', '正在读取。', 'read_', '{"paths":', 'c1')),
        chunk(delta(None, None, 'files', '["main.py"]}'), 'tool_calls'),
        SimpleNamespace(choices=[], usage=SimpleNamespace(model_dump=lambda: {'total_tokens': 12})),
    ]
    model = object.__new__(DeepSeekClient)
    model.model, model.generation = 'fixture', {}
    model.reasoning_enabled, model.reasoning_effort = True, 'low'
    model.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **kw: iter(chunks))))
    reasoning, text, response = model.stream_chat([], tools=[{'type': 'function'}])
    assert reasoning == ('reasoning', 'private') and text == '正在读取。'
    call, = response.tool_calls
    assert (call.id, call.name, call.arguments) == ('c1', 'read_files', '{"paths":["main.py"]}')
    assert response.finish_reason == 'tool_calls' and response.usage['total_tokens'] == 12
