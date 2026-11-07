"""End-to-end invariants of the application-owned memory architecture."""

import json
from pathlib import Path
import subprocess
import pytest
from genesisai.core.persistence.session import Session
from genesisai.core.memory import MemoryService
from genesisai.core.memory.project_index import ProjectIndex
from genesisai.core.memory.maintenance import Maintenance
from genesisai.core.tools.tool_runtime import ToolRuntime
from genesisai.core.prompt.providers import ContextBlock
from genesisai.shared.context_config import ContextConfig
from genesisai.shared.security import Access


@pytest.fixture
def library(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    session = Session(tmp_path / "app", workspace=workspace)
    session.record.grants = [str(workspace)]
    session.record.output = str(workspace)
    session.record.messages.append(
        {"role": "user", "content": "约定：使用 SQLite。禁止删除用户数据。"}
    )
    session.commit()
    runtime = ToolRuntime(
        session,
        Access([workspace], workspace, session.root, workspace_root=workspace),
        providers=[],
    )
    try:
        yield session, runtime
    finally:
        runtime.close()


def test_400k_capacity_is_distinct_from_working_history():
    config = ContextConfig.parse()
    assert config.context_window == config.max_input_tokens == 400000
    assert config.input_limit == 394880
    assert config.history_tokens == 32000


def test_project_context_prioritizes_tail_match_and_keeps_exact_reference(library):
    from genesisai.core.prompt.context_sources import project_context
    session, runtime = library
    for i in range(55):
        (session.workspace / f'file_{i:02}.py').write_text(f'value = {i}\n', encoding='utf-8')
    target = session.workspace / 'zz_release_guard.py'
    target.write_text('def release_guard():\n    return "tail requirement"\n', encoding='utf-8')
    runtime.project_index.refresh()
    session.record.messages.append({'role': 'user', 'content': '检查 release_guard 的实现'})
    runtime.context_budgeter.configure({'project_context_tokens': 1500})
    blocks = list(project_context({'session': session, 'runtime': runtime}))
    entries = [json.loads(block.content) for block in blocks if block.source.startswith('project_reference:')]
    assert entries[0]['title'] == 'zz_release_guard.py'
    assert 'tail requirement' in runtime.memory.read(entries[0]['ref'])['text']
    assert sum(len(block.content) for block in blocks) < 6000
    assert 'memory_search' in blocks[0].content
    assert runtime.project_index.overview(limit=10, offset=50)[-1]['path'] == 'zz_release_guard.py'


def test_optional_references_shrink_individually_to_request_budget(library):
    session, runtime = library
    runtime.context_budgeter.configure({'max_input_tokens': 6500, 'strategy': 'basic'})
    runtime.register_context('many_references', lambda request: [
        ContextBlock('ref_' + str(i), f'REFERENCE {i} ' + 'x' * 2100, token_budget=1000) for i in range(20)])
    messages = runtime.context_budgeter.prepare(runtime.context_budgeter.build('direct_answer'), [])
    included = [m for m in messages if (m.content or '').startswith('REFERENCE')]
    assert 0 < len(included) < 20
    manifest = session.record.run_runtime['context_report']['manifest']
    assert any(item.get('omission_reason') == 'request_budget' for item in manifest)
    assert session.record.messages[0]['content'] in '\n'.join(m.content or '' for m in messages)


def test_long_current_input_uses_window_and_keeps_history_bounded(library):
    from genesisai.agent.loop import Runner
    from genesisai.shared.messages import Response
    from test_acceptance import FakeModel

    session, runtime = library
    old = "historical material " * 6000
    session.record.messages.append({"role": "assistant", "content": old})
    session.commit()
    request = "current material " * 7000
    model = FakeModel(Response(content="已收到"))
    model.context_config = {"strategy": "basic"}
    result = Runner(model, runtime).start(request)
    assert result["status"] == "completed"
    messages = model.contexts[0]
    assert [m.role for m in messages if request in (m.content or "")] == ["user"]
    assert all(old not in (m.content or "") for m in messages)
    assert session.record.context_checkpoint


def test_chinese_search_exact_paging_and_project_isolation(library, tmp_path):
    session, runtime = library
    hits = runtime.memory.search("约定", corpus="history")
    assert len(hits) == 1
    ref = hits[0]["ref"]
    page = runtime.memory.read(ref, limit=10)
    following = runtime.memory.read(ref, offset=page["next_offset"])
    assert (
        json.loads(page["text"] + following["text"])["content"]
        == session.record.messages[0]["content"]
    )
    other = tmp_path / "other"
    other.mkdir()
    isolated = MemoryService(Session(session.root, workspace=other))
    assert isolated.search("约定") == []
    with pytest.raises(ValueError, match="工作区"):
        isolated.read(ref)
    assert list(session.workspace.iterdir()) == []


def test_index_versions_invalidate_knowledge_transitively_and_rebuild(library):
    session, runtime = library
    source = session.workspace / "module.py"
    source.write_text("import json\ndef calculate():\n    return 1\n", encoding="utf-8")
    assert runtime.project_index.refresh()["updated"] == 1
    assert runtime.project_index.refresh()["updated"] == 0
    (hit,) = runtime.memory.search("calculate", corpus="code")
    note = runtime.memory.write_notes([dict(
        title="Architecture",
        body="calculate returns 1",
        kind="architecture",
        sources=[hit["ref"]],
        status="active",
    )])['entries'][0]
    dependent = runtime.memory.write_notes([dict(
        title="Consumer",
        body="Calls calculate",
        kind="experience",
        sources=[note["id"]],
        status="active",
    )])['entries'][0]
    source.write_text("def calculate():\n    return 2\n", encoding="utf-8")
    runtime.project_index.refresh()
    assert runtime.memory.read(note["id"])["status"] == "stale"
    stale = runtime.memory.read(dependent["id"])
    assert stale["status"] == "stale"
    assert stale["source_versions"] == {note["id"]: 1}
    assert (
        runtime.memory.read(dependent["id"], revision=1)["source_versions"]
        == stale["source_versions"]
    )
    assert "return 1" in runtime.memory.read(hit["ref"])["text"]
    assert runtime.memory.search("Architecture", corpus="project") == []
    (current,) = runtime.memory.search("calculate", corpus="code")
    assert current["ref"] != hit["ref"]
    assert "return 2" in runtime.memory.read(current["ref"])["text"]
    with session.database.transaction() as db:
        db.execute("DELETE FROM search_fts")
        db.execute("DELETE FROM search_documents")
    runtime.memory.rebuild_search()
    assert runtime.memory.search("约定", corpus="history")
    assert runtime.memory.search("calculate", corpus="code")[0]["ref"] == current["ref"]
    assert not runtime.memory.search("Architecture", corpus="project")
    assert (
        len(
            session.database.rows(
                "SELECT * FROM note_versions WHERE note_id=?", (note["id"],)
            )
        )
        == 2
    )


def test_note_revision_conflict_correction_and_delete(library, tmp_path):
    session, runtime = library
    memory = runtime.memory
    ref = memory.search("约定")[0]["ref"]
    note = memory.write_notes([dict(
        title="约束", body="SQLite", kind="constraint", sources=[ref]
    )])['entries'][0]
    updated = memory.write_notes([dict(
        ref=note["id"],
        expected_revision=1,
        title="约束",
        body="SQLite transactions",
        kind="constraint",
        sources=[ref],
        status="active",
    )])['entries'][0]
    assert updated["revision"] == 2
    assert memory.read(note["id"], revision=1)["text"] == "SQLite"
    response = runtime.execute(
        {
            "id": "old_revision",
            "name": "memory_read",
            "arguments": json.dumps({"ref": note["id"], "revision": 1}),
        }
    )
    assert response["ok"] and response["data"]["text"] == "SQLite"
    assert memory.read(note["id"], revision=2)["text"] == "SQLite transactions"
    assert (
        len(
            session.database.rows(
                "SELECT * FROM note_versions WHERE note_id=?", (note["id"],)
            )
        )
        == 2
    )
    with pytest.raises(ValueError, match="版本"):
        memory.write_notes([dict(
            ref=note["id"],
            expected_revision=1,
            title="约束",
            body="wrong",
            kind="constraint",
            sources=[ref],
        )])
    destination = tmp_path / "export.md"
    memory.export(destination)
    assert "SQLite transactions" in destination.read_text(encoding="utf-8")
    memory.delete_note(note["id"], 2)
    assert not memory.search("transactions")
    with pytest.raises(ValueError):
        memory.read(note["id"])


def test_git_worktrees_share_project_but_isolate_history(library, tmp_path):
    session, runtime = library
    root = tmp_path / "git-project"
    root.mkdir()

    def git(*args):
        return subprocess.run(
            ["git", "-C", str(root), *args], capture_output=True, check=True
        )

    git("init")
    (root / "readme.md").write_text("project", encoding="utf-8")
    git("add", "readme.md")
    git(
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.invalid",
        "commit",
        "-m",
        "initial",
    )
    tree = tmp_path / "worktree"
    git("worktree", "add", "-b", "feature", str(tree))
    first, second = (
        Session(session.root, workspace=root),
        Session(session.root, workspace=tree),
    )
    assert first.record.project_id == second.record.project_id
    assert first.record.workspace_id != second.record.workspace_id
    first.record.messages.append({"role": "user", "content": "branch specific history"})
    first.commit()
    a, b = MemoryService(first), MemoryService(second)
    ref = a.search("branch")[0]["ref"]
    shared = a.write_notes([dict(
        title="Shared decision",
        body="Project policy",
        kind="decision",
        sources=[ref],
        shared=True,
    )])['entries'][0]
    assert b.search("branch", corpus="history") == []
    assert b.search("Shared", corpus="project")[0]["ref"] == shared["id"]
    with pytest.raises(ValueError):
        b.read(ref)


def test_index_respects_gitignore_and_secret_files(library):
    session, runtime = library
    subprocess.run(
        ["git", "init", str(session.workspace)], capture_output=True, check=True
    )
    (session.workspace / ".gitignore").write_text("ignored.py\n", encoding="utf-8")
    (session.workspace / "ignored.py").write_text("PRIVATE_IGNORED", encoding="utf-8")
    (session.workspace / ".env").write_text("PRIVATE_ENV", encoding="utf-8")
    (session.workspace / "key.pem").write_text("PRIVATE_KEY", encoding="utf-8")
    (session.workspace / "visible.py").write_text(
        "def public(): pass", encoding="utf-8"
    )
    runtime.project_index.refresh()
    assert not runtime.memory.search("PRIVATE")
    assert runtime.memory.search("public")


def test_narrower_file_grants_filter_existing_snapshots_and_new_indexing(library):
    session, full = library
    allowed = session.workspace / "允许"
    allowed.mkdir()
    public = allowed / "例子.py"
    private = session.workspace / "private.py"
    public.write_text("VISIBLE_SYMBOL = 1", encoding="utf-8")
    private.write_text("PRIVATE_SYMBOL = 2", encoding="utf-8")
    subprocess.run(
        ["git", "init", str(session.workspace)], capture_output=True, check=True
    )
    full.project_index.refresh()
    ref = full.memory.search("PRIVATE_SYMBOL", corpus="code")[0]["ref"]
    limited = ToolRuntime(
        session,
        Access([allowed], allowed, session.root, workspace_root=session.workspace),
        providers=[],
    )
    public.write_text("VISIBLE_SYMBOL = 3", encoding="utf-8")
    limited.project_index.refresh(changed_paths=["允许/例子.py"])
    (session.workspace / "outside.md").write_text("UNSEEN_DOCUMENT", encoding="utf-8")
    limited.project_index.refresh()
    assert limited.memory.search("PRIVATE_SYMBOL") == []
    assert limited.memory.search("PR", corpus="code") == []
    with pytest.raises(ValueError):
        limited.memory.read(ref)
    assert [item["path"] for item in limited.project_index.overview()] == [
        "允许/例子.py"
    ]
    allowed_ref = limited.memory.search("VISIBLE_SYMBOL", corpus="code")[0]["ref"]
    assert limited.memory.read(allowed_ref)["text"] == "VISIBLE_SYMBOL = 3"
    assert full.memory.read(ref)["current"]
    assert not full.memory.search("UNSEEN_DOCUMENT", corpus="code")


def test_extension_context_manifest_and_priority_boundary(library):
    session, runtime = library
    runtime.register_context(
        "fixture", lambda request: [ContextBlock("fixture.txt", "REFERENCE_TEXT")]
    )
    messages = runtime.context_budgeter.prepare(
        runtime.context_budgeter.build("direct_answer"), []
    )
    assert next(m for m in messages if m.content == "REFERENCE_TEXT").role == "user"
    manifest = session.record.run_runtime["context_report"]["manifest"]
    assert any(
        item["source"] == "fixture.txt" and item["included"] for item in manifest
    )
    stable = messages[0].content
    session.record.run_runtime["objective"] = "Changed objective"
    assert runtime.context_budgeter.build("local_files")[0].content == stable
    runtime.register_context(
        "unsafe", lambda request: [ContextBlock("web", "Do X", role="system")]
    )
    with pytest.raises(ValueError, match="system"):
        runtime.context_budgeter.build("direct_answer")


def test_large_required_rules_share_total_context_budget(library):
    session, runtime = library
    (session.workspace / "AGENTS.md").write_text("A" * 9000, encoding="utf-8")
    messages = runtime.context_budgeter.build("local_files")
    assert any('A' * 9000 in message.content for message in messages if isinstance(message.content, str))


def test_custom_maintenance_survives_interruption(library, monkeypatch):
    session, runtime = library
    runtime.register_maintenance(
        "fixture", lambda options: {"value": options["value"] + 1}
    )
    with monkeypatch.context() as patch:
        patch.setattr(runtime.maintenance, 'start', lambda: None)
        identity = runtime.maintenance.enqueue("fixture", {"value": 4})
        with session.database.transaction() as db:
            db.execute("UPDATE jobs SET state='running' WHERE id=?", (identity,))
        Maintenance.recover_after_host_lock(session.database)
    runtime.maintenance.start()
    result = runtime.maintenance.wait(identity, 5)
    assert result["result"] == {"value": 5} and result["state"] == "completed"


def test_tool_observation_delivery_is_idempotent(library):
    session, runtime = library
    runtime.tasks.begin_task("task")
    observations = [{"kind": "verification", "passed": True}]
    runtime.tasks.observe(observations, call_id="one")
    runtime.tasks.observe(observations, call_id="one")
    assert len(runtime.tasks.active_task()["observations"]) == 1


def test_registered_tools_and_verifier_complete_without_core_dispatch(
    library, tmp_path
):
    from genesisai.agent.loop import Runner
    from genesisai.core.tools.base import ToolDescriptor, ToolSpec, object_schema
    from genesisai.core.tools.observations import Observation
    from genesisai.shared.messages import Response
    from genesisai.shared.ids import sha
    from test_acceptance import FakeModel
    from test_development_completion_gate import tool_response
    import yaml

    session, runtime = library
    runtime.set_permission("auto")
    target = session.workspace / "custom.xyz"

    class Writer:
        def prepare(self, context, args):
            return {"target": str(target)}

        def execute(self, context, args):
            target.write_text("CUSTOM_SYMBOL", encoding="utf-8")
            return {"path": str(target)}

        def observe(self, args, payload):
            return [
                Observation(
                    "resource_changed", path=payload["path"], sha256=sha(target)
                )
            ]

    class Verifier:
        def prepare(self, context, args):
            return None

        def execute(self, context, args):
            return {"passed": target.read_text(encoding="utf-8") == "CUSTOM_SYMBOL"}

        def observe(self, args, payload):
            return [Observation("verification", passed=payload["passed"])]

    for name, implementation, permission in [
        ("custom_write", Writer(), "write"),
        ("custom_verify", Verifier(), "read"),
    ]:
        runtime.register_tool(
            ToolDescriptor(
                name=name,
                enabled=True,
                category="kernel",
                description=name,
                directory=tmp_path,
                spec=ToolSpec(
                    parameters=object_schema([]),
                    permission=permission,
                    side_effect=permission == "write",
                    routes=("coding",) if permission == "write" else (),
                ),
            ),
            implementation,
        )
    runtime.register_verifier(
        "custom_language",
        lambda workspace, state, available: {"name": "custom_verify", "arguments": {}},
    )
    runtime.register_parser(
        ".xyz",
        lambda text: {
            "symbols": [{"name": "CUSTOM_SYMBOL", "line": 1}],
            "imports": [],
            "coverage": "fixture",
        },
    )
    prompt = tmp_path / "custom.prompt.yaml"
    prompt.write_text(
        yaml.safe_dump(
            {
                "name": "custom_protocol",
                "category": "protocol",
                "description": "Custom protocol",
                "content": {
                    "objective": "CUSTOM_PROTOCOL_TEXT",
                    "instructions": [],
                    "constraints": [],
                    "completion": [],
                },
            }
        ),
        encoding="utf-8",
    )
    runtime.register_prompt(
        "custom_protocol",
        prompt,
        protocol="custom_protocol",
        selector=lambda text: "插件" in text,
    )
    model = FakeModel(
        tool_response("custom_write", {}, "custom"),
        Response(content="已经完成"),
        Response(content="验证通过"),
    )
    result = Runner(model, runtime).start("实现插件功能")
    assert result["status"] == "completed"
    assert (
        session.record.run_runtime["project_changed"]
        and session.record.run_runtime["verification_succeeded"]
    )
    assert any(
        item["call"]["name"] == "custom_verify"
        for item in session.record.calls.values()
    )
    assert runtime.project_index.overview()[0]["coverage"] == "fixture"
    assert any(
        "CUSTOM_PROTOCOL_TEXT" in str(message.content) for message in model.contexts[0]
    )


def test_observed_change_invalidates_note_before_next_turn(library):
    from genesisai.shared.ids import sha

    session, runtime = library
    path = session.workspace / "module.py"
    path.write_text("value = 1", encoding="utf-8")
    runtime.project_index.refresh()
    ref = runtime.memory.search("value", corpus="code")[0]["ref"]
    note = runtime.memory.write_notes([dict(
        title="Value", body="one", kind="architecture", sources=[ref], status="active"
    )])['entries'][0]
    path.write_text("value = 2", encoding="utf-8")
    runtime.deliver_observations(
        [{"kind": "resource_changed", "path": str(path), "sha256": sha(path)}],
        call_id="change",
    )
    assert runtime.memory.read(note["id"])["status"] == "stale"
    assert runtime.memory.read(ref)['current'] is False
    for job in runtime.maintenance.status()['jobs']:
        assert runtime.maintenance.wait(job['id'], 10)['state'] == 'completed'
    current = runtime.memory.search("value", corpus="code")[0]["ref"]
    assert current != ref and "value = 2" in runtime.memory.read(current)["text"]


def test_image_library_is_shared_only_within_workspace(library, tmp_path):
    from genesisai.core.media.images import ImageStore, prepare_image

    session, runtime = library
    source = Path(__file__).parents[1] / "src/genesisai/app/assets/pet/spritesheet.webp"
    part = runtime.images.import_image(
        prepare_image(source.read_bytes(), "pet.webp", runtime.images.config)
    )
    same = Session(session.root, workspace=session.workspace)
    images = ImageStore(same, runtime.access)
    assert images.read(image_ref=part["image_ref"])["images"] == [part]
    assert images.data_url(part).startswith("data:image/png;base64,")
    other = tmp_path / "other"
    other.mkdir()
    unrelated = ImageStore(Session(session.root, workspace=other), runtime.access)
    with pytest.raises(ValueError, match="引用"):
        unrelated.read(image_ref=part["image_ref"])


def test_project_index_large_file_tail_and_search_pagination(library):
    session, runtime = library
    source = session.workspace / 'large.custom'
    source.write_text('filler ' * 160000 + '\nUNICORN_TAIL_MARKER\n', encoding='utf-8')
    for i in range(25):
        (session.workspace / f'page_{i:02d}.txt').write_text(f'PAGE_NEEDLE {i}', encoding='utf-8')
    assert runtime.project_index.refresh()['updated'] == 26
    hits = runtime.memory.search(' '.join(['unrelated'] * 12) + ' UNICORN_TAIL_MARKER', corpus='code')
    assert hits and hits[0]['title'] == 'large.custom'
    assert 'UNICORN_TAIL_MARKER' in runtime.memory.read(hits[0]['ref'], offset=1120000, limit=200000)['text']
    first = runtime.memory.search('PAGE_NEEDLE', corpus='code', limit=20)
    second = runtime.memory.search('PAGE_NEEDLE', corpus='code', limit=20, offset=20)
    assert len(first) == 20 and len(second) == 5
    assert len({item['ref'] for item in first + second}) == 25
