from pathlib import Path
from unittest.mock import patch


def test_kanban_handoff_uses_absolute_default_and_model_override():
    from agent.kanban_context_policy import resolve_handoff_threshold

    config = {
        "context_handoff_tokens": 100_000,
        "context_handoff_tokens_by_model": {"gpt-5.6-terra": 120_000},
    }

    assert resolve_handoff_threshold(config, "other-model", 200_000) == 100_000
    assert resolve_handoff_threshold(config, "gpt-5.6-terra", 200_000) == 120_000


def test_kanban_handoff_keeps_safe_headroom_for_smaller_context():
    from agent.kanban_context_policy import resolve_handoff_threshold

    assert resolve_handoff_threshold({"context_handoff_tokens": 100_000}, "small", 96_000) == 76_800


def test_handoff_checkpoint_is_written_to_workspace(tmp_path: Path):
    from agent.kanban_context_policy import write_handoff_checkpoint

    checkpoint = write_handoff_checkpoint(
        workspace=tmp_path,
        task_id="t_123",
        model="test-model",
        prompt_tokens=100_000,
        summary="## Historical In-Progress State\nImplemented the parser.",
    )

    assert checkpoint == tmp_path / ".hermes" / "kanban-handoff.md"
    text = checkpoint.read_text()
    assert "# Kanban worker handoff" in text
    assert "Task: `t_123`" in text
    assert "Prompt tokens at handoff: 100,000" in text
    assert "Implemented the parser." in text


def test_kanban_worker_rotates_at_absolute_checkpoint():
    from run_agent import AIAgent

    with patch.dict(
        "os.environ",
        {"HERMES_KANBAN_TASK": "t_123", "OPENROUTER_API_KEY": "test-key"},
        clear=False,
    ):
        agent = AIAgent(
            api_key="test-key",
            base_url="https://openrouter.ai/api/v1",
            model="test/model",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )

    assert agent.kanban_context_handoff_enabled is True
    assert agent.context_compressor.threshold_tokens == 100_000
    assert agent.compression_in_place is False


def test_kanban_compaction_writes_checkpoint_and_rotates_session(tmp_path: Path):
    from agent.context_compressor import COMPRESSED_SUMMARY_METADATA_KEY
    from agent.conversation_compression import compress_context
    from hermes_state import SessionDB
    from run_agent import AIAgent

    db = SessionDB(db_path=tmp_path / "state.db")
    session_id = "worker-session"
    db.create_session(session_id, "cli", model="test/model")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    with patch.dict(
        "os.environ",
        {
            "HERMES_KANBAN_TASK": "t_123",
            "HERMES_KANBAN_WORKSPACE": str(workspace),
            "OPENROUTER_API_KEY": "test-key",
        },
        clear=False,
    ):
        agent = AIAgent(
            api_key="test-key",
            base_url="https://openrouter.ai/api/v1",
            model="test/model",
            quiet_mode=True,
            session_db=db,
            session_id=session_id,
            skip_context_files=True,
            skip_memory=True,
        )
        agent.context_compressor.compress = lambda *_args, **_kwargs: [
            {
                "role": "user",
                "content": "## Historical In-Progress State\nParser is complete.",
                COMPRESSED_SUMMARY_METADATA_KEY: True,
            },
            {"role": "assistant", "content": "recent reply"},
        ]
        agent.context_compressor._last_compress_aborted = False
        agent.context_compressor._last_summary_error = None
        agent.context_compressor.compression_count = 1
        compress_context(
            agent,
            [{"role": "user", "content": "continue"}],
            system_message="sys",
            approx_tokens=100_000,
        )

    checkpoint = workspace / ".hermes" / "kanban-handoff.md"
    assert checkpoint.exists()
    assert "Parser is complete." in checkpoint.read_text()
    assert agent.session_id != session_id
