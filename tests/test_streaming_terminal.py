from io import StringIO
import time
from types import SimpleNamespace

from rich.console import Console

from genesisai.agent.runner import Runner
from genesisai.app.terminal_view import CliView
from genesisai.model.providers.deepseek import DeepSeekClient
from genesisai.runtime.tool_runtime import ToolRuntime
from genesisai.shared.messages import Response
from genesisai.shared.security import Access
from genesisai.state.store import Store


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
        confirm_writes=False,
        confirm_search=False,
        confirm_shell=False,
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

    response = runner._call_model([], [])

    assert response.content == "done"
    assert model.options["max_tokens"] == 600
