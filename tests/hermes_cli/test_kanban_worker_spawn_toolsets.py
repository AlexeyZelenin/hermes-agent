from __future__ import annotations

import subprocess


def _make_task(kb, *, assignee: str, title="spawn tools", body=None, executor="hermes-worker"):
    return kb.Task(
        id="t_spawn_tools",
        title=title,
        body=body,
        assignee=assignee,
        status="running",
        priority=0,
        created_by="test",
        created_at=1,
        started_at=None,
        completed_at=None,
        workspace_kind="dir",
        workspace_path=None,
        claim_lock="lock",
        claim_expires=None,
        tenant=None,
        current_run_id=7,
        executor=executor,
    )


def _narrow_profile(tmp_path):
    """Isolated HERMES_HOME whose profile enables narrow toolset selection."""
    root = tmp_path / ".hermes"
    profile = root / "profiles" / "elias"
    profile.mkdir(parents=True)
    profile.joinpath("config.yaml").write_text(
        """
platform_toolsets:
  cli:
    - file
    - terminal
    - web
    - memory
    - delegation
toolsets:
  - hermes-cli
kanban:
  toolset_selection:
    mode: narrow
    base: [file, terminal]
""".lstrip(),
        encoding="utf-8",
    )
    return root, profile


def test_default_spawn_pins_assignee_profile_cli_toolsets(monkeypatch, tmp_path):
    """Manual profile assignment should keep that profile's CLI tools.

    Regression guard for dispatcher-spawned workers that boot with
    HERMES_KANBAN_TASK: the worker must not collapse to only kanban lifecycle
    tools when the assigned profile's top-level ``toolsets`` is the default
    composite. The spawned CLI gets an explicit --toolsets pin resolved from
    platform_toolsets.cli; model_tools appends task-scoped kanban tools later.
    """
    root = tmp_path / ".hermes"
    profile = root / "profiles" / "elias"
    profile.mkdir(parents=True)
    profile.joinpath("config.yaml").write_text(
        """
platform_toolsets:
  cli:
    - clarify
    - code_execution
    - delegation
    - file
    - memory
    - session_search
    - skills
    - terminal
    - web
toolsets:
  - hermes-cli
agent:
  disabled_toolsets: []
""".lstrip(),
        encoding="utf-8",
    )
    root.joinpath("config.yaml").write_text("toolsets:\n  - kanban\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(root))

    from hermes_cli import kanban_db as kb

    monkeypatch.setattr(kb, "_resolve_hermes_argv", lambda: ["hermes"])

    captured = {}

    class FakeProc:
        pid = 4242

    def fake_popen(cmd, *args, **kwargs):
        captured["cmd"] = list(cmd)
        captured["env"] = dict(kwargs.get("env") or {})
        captured["cwd"] = kwargs.get("cwd")
        return FakeProc()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    pid = kb._default_spawn(_make_task(kb, assignee="elias"), str(workspace))

    assert pid == 4242
    assert captured["env"]["HERMES_HOME"] == str(profile)
    assert captured["env"]["HERMES_KANBAN_TASK"] == "t_spawn_tools"
    assert "--toolsets" in captured["cmd"]
    pinned = captured["cmd"][captured["cmd"].index("--toolsets") + 1].split(",")
    for required in ("terminal", "web", "file", "skills", "code_execution", "delegation"):
        assert required in pinned


def test_default_spawn_never_boots_the_tui(monkeypatch, tmp_path):
    """Workers are headless: an inherited HERMES_TUI=1 (or a TUI-default
    config) must not send the quiet chat run into the Ink TUI, whose no-TTY
    bail-out exits 0 without doing the task — every attempt then ends in
    "protocol violation". The spawn pins --cli (highest-precedence interface
    flag) and strips HERMES_TUI from the child env."""
    root = tmp_path / ".hermes"
    (root / "profiles" / "elias").mkdir(parents=True)
    root.joinpath("config.yaml").write_text("display:\n  interface: tui\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setenv("HERMES_TUI", "1")

    from hermes_cli import kanban_db as kb

    monkeypatch.setattr(kb, "_resolve_hermes_argv", lambda: ["hermes"])

    captured = {}

    class FakeProc:
        pid = 4243

    def fake_popen(cmd, *args, **kwargs):
        captured["cmd"] = list(cmd)
        captured["env"] = dict(kwargs.get("env") or {})
        return FakeProc()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    kb._default_spawn(_make_task(kb, assignee="elias"), str(workspace))

    assert "--cli" in captured["cmd"]
    assert "HERMES_TUI" not in captured["env"]


def test_default_spawn_model_override_survives_real_cli_parse(monkeypatch, tmp_path):
    """The dispatcher's pre-``chat`` model flag must reach ``args.model``.

    This is an integration contract between Kanban's worker argv builder and
    the real CLI parser. A parser default once erased the explicit override,
    silently sending the worker to its profile default or fallback instead.
    """
    root = tmp_path / ".hermes"
    (root / "profiles" / "elias").mkdir(parents=True)
    root.joinpath("config.yaml").write_text("{}\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(root))

    from hermes_cli import kanban_db as kb
    from hermes_cli._parser import build_top_level_parser

    monkeypatch.setattr(kb, "_resolve_hermes_argv", lambda: ["hermes"])
    captured = {}

    class FakeProc:
        pid = 4244

    def fake_popen(cmd, *args, **kwargs):
        captured["cmd"] = list(cmd)
        return FakeProc()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    task = _make_task(kb, assignee="elias")
    task.model_override = "gpt-5.6-sol"
    kb._default_spawn(task, str(workspace))

    parser, _subparsers, _chat_parser = build_top_level_parser()
    # Profile selection is attached by the outer CLI bootstrap rather than
    # build_top_level_parser(); remove that already-validated prefix and parse
    # the worker flags/subcommand through the real shared parser.
    assert captured["cmd"][1:3] == ["-p", "elias"]
    args = parser.parse_args(captured["cmd"][3:])

    assert args.command == "chat"
    assert args.model == "gpt-5.6-sol"
    assert args.query == "work kanban task t_spawn_tools"


def test_resolve_worker_cli_toolsets_uses_profile_home_not_parent_config(monkeypatch, tmp_path):
    root = tmp_path / ".hermes"
    profile = root / "profiles" / "elias"
    profile.mkdir(parents=True)
    root.joinpath("config.yaml").write_text("platform_toolsets:\n  cli:\n    - kanban\n", encoding="utf-8")
    profile.joinpath("config.yaml").write_text(
        """
platform_toolsets:
  cli:
    - terminal
    - web
toolsets:
  - hermes-cli
""".lstrip(),
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(root))

    from hermes_cli import kanban_db as kb

    resolved = kb._resolve_worker_cli_toolsets(str(profile))

    assert resolved is not None
    assert "terminal" in resolved
    assert "web" in resolved
    assert "kanban" in resolved  # recovered worker lifecycle surface
    assert resolved != ["kanban"]


def test_resolve_narrow_mode_narrows_by_task_content(monkeypatch, tmp_path):
    """With ``mode: narrow``, the worker gets base + only task-relevant toolsets.

    A task about searching online docs earns ``web`` (keyword rule); the
    unrelated ``memory``/``delegation`` toolsets in the profile ceiling are
    withheld. Base (file/terminal) is always kept.
    """
    _root, profile = _narrow_profile(tmp_path)

    from hermes_cli import kanban_db as kb

    task = _make_task(kb, assignee="elias", title="Search the online documentation")
    resolved = kb._resolve_worker_cli_toolsets(str(profile), task, "ra")

    assert resolved is not None
    assert set(resolved) >= {"file", "terminal"}     # base always present
    assert "web" in resolved                          # earned by content
    assert "memory" not in resolved                   # no signal → withheld
    assert "delegation" not in resolved


def test_resolve_narrow_mode_minimal_task_is_base_only(monkeypatch, tmp_path):
    _root, profile = _narrow_profile(tmp_path)

    from hermes_cli import kanban_db as kb

    task = _make_task(kb, assignee="elias", title="do the thing", body="just do it")
    resolved = kb._resolve_worker_cli_toolsets(str(profile), task, "ra")

    assert sorted(resolved) == ["file", "terminal"]


def test_resolve_narrow_mode_bypasses_acp_executor(monkeypatch, tmp_path):
    """claude-code/codex don't consume --toolsets → full ceiling, no narrowing."""
    _root, profile = _narrow_profile(tmp_path)

    from hermes_cli import kanban_db as kb

    task = _make_task(
        kb, assignee="elias", title="do the thing", executor="claude-code")
    resolved = kb._resolve_worker_cli_toolsets(str(profile), task, "ra")

    # unchanged full ceiling — memory/delegation survive despite no keyword hit
    assert {"memory", "delegation", "web"} <= set(resolved)


def test_resolve_mode_off_matches_legacy_full_ceiling(monkeypatch, tmp_path):
    """Default ``mode: off`` returns the full profile ceiling (today's behaviour)."""
    root = tmp_path / ".hermes"
    profile = root / "profiles" / "elias"
    profile.mkdir(parents=True)
    profile.joinpath("config.yaml").write_text(
        "platform_toolsets:\n  cli:\n    - file\n    - terminal\n    - web\n    - memory\n"
        "toolsets:\n  - hermes-cli\n",
        encoding="utf-8",
    )

    from hermes_cli import kanban_db as kb

    task = _make_task(kb, assignee="elias", title="do the thing")
    with_task = kb._resolve_worker_cli_toolsets(str(profile), task, "ra")
    legacy = kb._resolve_worker_cli_toolsets(str(profile))

    assert with_task == legacy
    assert {"memory", "web"} <= set(with_task)
