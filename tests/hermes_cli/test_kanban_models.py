"""Model maps per board/project/task: config, Take v2 tiers, dispatch."""
from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import projects_db as pdb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def test_board_model_map_merges_and_clears(kanban_home):
    meta = kb.write_board_metadata(
        "default", models={"worker": "m-big", "cheap": "m-small"})
    assert meta["models"] == {"worker": "m-big", "cheap": "m-small"}

    # Role-by-role merge: empty string clears, omitted roles persist.
    meta = kb.write_board_metadata("default", models={"cheap": "", "aux": "m-aux"})
    assert meta["models"] == {"worker": "m-big", "aux": "m-aux"}
    assert kb.read_board_metadata("default")["models"] == meta["models"]


def test_board_model_map_rejects_unknown_roles(kanban_home):
    with pytest.raises(ValueError, match="unknown model roles"):
        kb.write_board_metadata("default", models={"bogus": "x"})


def test_resolve_model_map_precedence(kanban_home, monkeypatch):
    monkeypatch.setattr(kb, "_global_model_map",
                        lambda: {"worker": "g-w", "aux": "g-a"})
    kb.write_board_metadata("default", models={"worker": "b-w"})

    board_level = kb.resolve_model_map(None)
    assert board_level == {"worker": "b-w", "aux": "g-a"}

    with_project = kb.resolve_model_map(None, {"worker": "p-w"})
    assert with_project == {"worker": "p-w", "aux": "g-a"}


def test_create_task_stores_explicit_model_override(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="x", model_override="claude-x")
        assert kb.get_task(conn, tid).model_override == "claude-x"


def test_create_task_freezes_project_worker_model(kanban_home, tmp_path, monkeypatch):
    monkeypatch.setattr(pdb, "projects_db_path", lambda: tmp_path / "projects.db")
    with pdb.connect_closing() as pconn:
        pid = pdb.create_project(pconn, name="Proj", models={"worker": "proj-w"})
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="x", project_id=pid)
        task = kb.get_task(conn, tid)
    assert task.model_override == "proj-w"

    # An explicit override still wins over the project map.
    with kb.connect() as conn:
        tid2 = kb.create_task(conn, title="y", project_id=pid,
                              model_override="explicit-m")
        assert kb.get_task(conn, tid2).model_override == "explicit-m"


def test_decompose_children_inherit_executor_and_model(kanban_home):
    kb.write_board_metadata("default", executor="claude-code")
    with kb.connect() as conn:
        root = kb.create_task(conn, title="root", triage=True,
                              model_override="root-m")
        child_ids = kb.decompose_triage_task(
            conn,
            root,
            root_assignee=None,
            children=[
                {"title": "tiered", "parents": [], "model_override": "tier-cheap"},
                {"title": "plain", "parents": []},
            ],
        )
        tiered, plain = (kb.get_task(conn, cid) for cid in child_ids)
    assert tiered.model_override == "tier-cheap"
    assert plain.model_override == "root-m"
    assert tiered.executor == "claude-code"
    assert plain.executor == "claude-code"


def _fake_llm_response(content: str):
    return type("R", (), {"choices": [
        type("C", (), {"message": type("M", (), {"content": content})()})()
    ]})()


def test_decomposer_maps_tiers_to_board_models(kanban_home):
    from hermes_cli import kanban_decompose

    kb.write_board_metadata(
        "default",
        models={"cheap": "m-cheap", "mid": "m-mid", "strong": "m-strong"})
    with kb.connect() as conn:
        root = kb.create_task(conn, title="big", body="do it", triage=True)

    llm_json = (
        '{"fanout": true, "rationale": "r", "tasks": ['
        '{"title": "translate strings", "body": "b", "parents": [],'
        ' "model_tier": "cheap", "model_rationale": "mechanical"},'
        '{"title": "redesign core", "body": "b", "parents": [0],'
        ' "model_tier": "strong", "model_rationale": "architecture"},'
        '{"title": "write tests", "body": "b", "parents": [0],'
        ' "model_tier": "mid"},'
        '{"title": "normal piece", "body": "b", "parents": [0],'
        ' "model_tier": "standard"}]}'
    )
    with patch("agent.auxiliary_client.call_llm",
               return_value=_fake_llm_response(llm_json)) as llm:
        outcome = kanban_decompose.decompose_task(root)

    assert outcome.ok and outcome.fanout
    # No aux model configured — the decomposer must not force one.
    assert llm.call_args.kwargs["model"] is None
    with kb.connect() as conn:
        models = {kb.get_task(conn, cid).title: kb.get_task(conn, cid).model_override
                  for cid in outcome.child_ids}
    assert models == {
        "translate strings": "m-cheap",
        "redesign core": "m-strong",
        "write tests": "m-mid",
        "normal piece": None,
    }

    # Each assignment is recorded with the planner's rationale, and surfaces as
    # an auditable comment on the child task.
    by_title = {a["title"]: a for a in outcome.model_assignments}
    assert by_title["redesign core"]["tier"] == "strong"
    assert by_title["redesign core"]["model"] == "m-strong"
    assert by_title["redesign core"]["rationale"] == "architecture"
    assert by_title["normal piece"]["model"] is None  # inherits board default

    strong_id = by_title["redesign core"]["child_id"]
    with kb.connect() as conn:
        bodies = [c.body for c in kb.list_comments(conn, strong_id)]
    assert any("Model tier: strong → m-strong — architecture" in b for b in bodies)


def test_decomposer_uses_board_aux_model(kanban_home):
    from hermes_cli import kanban_decompose

    kb.write_board_metadata("default", models={"aux": "m-aux"})
    with kb.connect() as conn:
        root = kb.create_task(conn, title="single", body="b", triage=True)

    llm_json = '{"fanout": false, "rationale": "r", "title": "single", "body": "spec"}'
    with patch("agent.auxiliary_client.call_llm",
               return_value=_fake_llm_response(llm_json)) as llm:
        outcome = kanban_decompose.decompose_task(root)

    assert outcome.ok
    assert llm.call_args.kwargs["model"] == "m-aux"


def _spawnable_task(task_id: str, *, executor: str = "hermes-worker",
                    model_override=None) -> kb.Task:
    return kb.Task(
        id=task_id,
        title="t",
        body=None,
        assignee="default",
        status="running",
        priority=0,
        created_by="test",
        created_at=1,
        started_at=None,
        completed_at=None,
        workspace_kind="dir",
        workspace_path=None,
        claim_lock="test-lock",
        claim_expires=None,
        tenant=None,
        current_run_id=1,
        executor=executor,
        model_override=model_override,
    )


@pytest.fixture
def spawn_env(tmp_path, monkeypatch):
    root = tmp_path / ".hermes"
    (root / "profiles" / "default").mkdir(parents=True)
    root.joinpath("config.yaml").write_text("{}\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    captured = {}

    class FakeProc:
        pid = 4321

    def fake_popen(cmd, *args, **kwargs):
        captured["cmd"] = list(cmd)
        captured["env"] = dict(kwargs["env"])
        return FakeProc()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    return captured, str(workspace)


def test_spawn_resolves_board_worker_model_live(spawn_env):
    captured, workspace = spawn_env
    kb.write_board_metadata("default", models={"worker": "board-w"})
    assert kb._default_spawn(_spawnable_task("t_m1"), workspace) == 4321
    cmd = captured["cmd"]
    assert cmd[cmd.index("-m") + 1] == "board-w"


def test_spawn_task_override_beats_board_model(spawn_env):
    captured, workspace = spawn_env
    kb.write_board_metadata("default", models={"worker": "board-w"})
    task = _spawnable_task("t_m2", model_override="task-m")
    assert kb._default_spawn(task, workspace) == 4321
    cmd = captured["cmd"]
    assert cmd[cmd.index("-m") + 1] == "task-m"


def test_spawn_passes_model_env_to_acp_executor(spawn_env):
    captured, workspace = spawn_env
    kb.write_board_metadata("default", models={"worker": "board-w"})
    task = _spawnable_task("t_m3", executor="claude-code")
    assert kb._default_spawn(task, workspace) == 4321
    assert captured["cmd"] == [kb.sys.executable, "-m", "agent.acp_task_executor"]
    assert captured["env"]["HERMES_KANBAN_MODEL"] == "board-w"


def test_spawn_no_model_configured_keeps_defaults(spawn_env):
    captured, workspace = spawn_env
    assert kb._default_spawn(_spawnable_task("t_m4"), workspace) == 4321
    assert "-m" not in captured["cmd"]
    task = _spawnable_task("t_m5", executor="claude-code")
    assert kb._default_spawn(task, workspace) == 4321
    assert "HERMES_KANBAN_MODEL" not in captured["env"]


def test_gateway_dispatch_limits_parse_live_config():
    from gateway.kanban_watchers import _resolve_dispatch_limits

    assert _resolve_dispatch_limits(
        lambda: {"kanban": {"max_spawn": "3", "max_in_progress": 5,
                            "max_in_progress_per_profile": 2}}
    ) == (3, 5, 2)
    assert _resolve_dispatch_limits(
        lambda: {"kanban": {"max_in_progress": 0, "max_spawn": "junk"}}
    ) == (None, None, None)
    assert _resolve_dispatch_limits(lambda: {}) == (None, None, None)

    def boom():
        raise RuntimeError("config unavailable")

    assert _resolve_dispatch_limits(boom) == (None, None, None)


def test_project_model_map_round_trip(tmp_path, monkeypatch):
    monkeypatch.setattr(pdb, "projects_db_path", lambda: tmp_path / "projects.db")
    with pdb.connect_closing() as conn:
        pid = pdb.create_project(conn, name="P", models={"worker": "w1"})
        assert pdb.get_project(conn, pid).models == {"worker": "w1"}
        pdb.update_project(conn, pid, models={"worker": "w2", "cheap": "c1"})
        assert pdb.get_project(conn, pid).models == {"worker": "w2", "cheap": "c1"}
        pdb.update_project(conn, pid, models={})
        assert pdb.get_project(conn, pid).models == {}
        with pytest.raises(ValueError, match="unknown model roles"):
            pdb.update_project(conn, pid, models={"nope": "x"})
