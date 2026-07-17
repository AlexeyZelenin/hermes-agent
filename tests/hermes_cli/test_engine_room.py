"""Tests for hermes_cli.engine_room — the "под капотом" (engine room) home model.

Pure model: role prompts are read live from source, surfaces are a static index,
and substrate presence is probed from injected table sets. Nothing here touches a
live board or the real zeus/kanban stores; the one integration test builds an
isolated temp sqlite mirror of the zeus schema.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from hermes_cli import engine_room


# --- Pillar 1: roles --------------------------------------------------------


def test_resolve_roles_reads_real_prompts_live():
    """Every declared role resolves to its actual, non-empty system prompt."""
    roles = engine_room.resolve_roles()
    assert roles, "expected at least one meta-role"
    for row in roles:
        assert row["available"] is True, f"{row['key']} should resolve from source"
        assert isinstance(row["system_prompt"], str) and row["system_prompt"].strip()
        assert row["prompt_chars"] == len(row["system_prompt"])
    # Keys are unique and stable.
    keys = [r["key"] for r in roles]
    assert len(keys) == len(set(keys))
    assert "decomposer" in keys and "goal_judge" in keys


def test_resolve_role_degrades_on_missing_constant():
    """A renamed/removed constant degrades one row, never raises."""
    bad = engine_room.resolve_role({
        "key": "ghost",
        "title": "Ghost",
        "module": "hermes_cli.kanban_decompose",
        "attr": "_NOPE_DOES_NOT_EXIST",
        "purpose": "-",
        "when": "-",
        "model_role": "cheap",
    })
    assert bad["available"] is False
    assert bad["system_prompt"] is None
    assert bad["prompt_chars"] == 0


def test_resolve_role_degrades_on_missing_module():
    bad = engine_room.resolve_role({
        "key": "ghost",
        "title": "Ghost",
        "module": "hermes_cli.no_such_module_xyz",
        "attr": "_SYSTEM_PROMPT",
        "purpose": "-",
        "when": "-",
        "model_role": "cheap",
    })
    assert bad["available"] is False


def test_resolve_role_rejects_non_string_attr(monkeypatch):
    """A constant that exists but isn't a real prompt string degrades cleanly."""
    import hermes_cli.kanban_specify as ks
    monkeypatch.setattr(ks, "_SYSTEM_PROMPT", 123, raising=False)
    row = engine_room.resolve_role({
        "key": "specifier",
        "title": "Спецификатор",
        "module": "hermes_cli.kanban_specify",
        "attr": "_SYSTEM_PROMPT",
        "purpose": "-",
        "when": "-",
        "model_role": "mid",
    })
    assert row["available"] is False


# --- Pillar 2: surfaces -----------------------------------------------------


def test_surfaces_are_unique_and_shaped():
    surfaces = engine_room.surfaces()
    assert surfaces
    keys = [s["key"] for s in surfaces]
    assert len(keys) == len(set(keys)), "surface keys must be unique"
    for s in surfaces:
        assert s["route"].startswith("/api/")
        assert s["owner_task"].startswith("t_")
        assert s["title"] and s["purpose"]


def test_surfaces_returns_defensive_copies():
    """Caller mutation must not corrupt the module-level catalogue."""
    first = engine_room.surfaces()
    first[0]["title"] = "MUTATED"
    second = engine_room.surfaces()
    assert second[0]["title"] != "MUTATED"


# --- Pillar 3: substrate ----------------------------------------------------


def test_substrate_presence_reflects_injected_tables():
    rows = engine_room.substrate(
        zeus_tables={"token_usage", "pacing_state", "decisions"},
        kanban_tables={"task_events"},
        otel_configured=False,
    )
    by_key = {r["key"]: r for r in rows}
    assert by_key["token_usage"]["present"] is True
    assert by_key["pacing_state"]["present"] is True
    assert by_key["decisions"]["present"] is True
    # findings table absent from the injected set -> not present.
    assert by_key["findings"]["present"] is False
    # task_events lives in kanban.db, not zeus.db.
    assert by_key["task_events"]["db"] == engine_room.DB_KANBAN
    assert by_key["task_events"]["present"] is True
    # OTel has no local table and follows the configured flag.
    assert by_key["otel"]["table"] is None
    assert by_key["otel"]["present"] is False


def test_substrate_all_absent_by_default():
    rows = engine_room.substrate()
    assert all(r["present"] is False for r in rows)


def test_substrate_otel_flag():
    rows = engine_room.substrate(otel_configured=True)
    otel = next(r for r in rows if r["key"] == "otel")
    assert otel["present"] is True


# --- Assembly + live probes -------------------------------------------------


def test_table_names_handles_none_and_error():
    assert engine_room._table_names(None) == set()

    class Boom:
        def execute(self, *a):
            raise sqlite3.OperationalError("boom")

    assert engine_room._table_names(Boom()) == set()


def test_engine_room_model_with_temp_zeus(tmp_path: Path):
    """End-to-end assembly against an isolated temp zeus.db mirror."""
    db = tmp_path / "zeus.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(
        "CREATE TABLE token_usage (id INTEGER PRIMARY KEY);"
        "CREATE TABLE pacing_state (id INTEGER PRIMARY KEY);"
    )
    conn.commit()

    model = engine_room.engine_room_model(zeus_conn=conn, otel_configured=False)
    conn.close()

    assert set(model) == {"roles", "surfaces", "substrate"}
    assert model["roles"] and model["surfaces"] and model["substrate"]
    by_key = {r["key"]: r for r in model["substrate"]}
    assert by_key["token_usage"]["present"] is True
    assert by_key["pacing_state"]["present"] is True
    assert by_key["decisions"]["present"] is False  # table not created
    assert by_key["task_events"]["present"] is False  # no kanban conn passed


def test_engine_room_model_no_stores_still_serves_roles_and_surfaces():
    """Missing zeus/kanban stores degrade substrate but keep the other pillars."""
    model = engine_room.engine_room_model(otel_configured=False)
    assert model["roles"]
    assert model["surfaces"]
    assert all(r["present"] is False for r in model["substrate"])


def test_engine_room_otel_env_probe(monkeypatch):
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", raising=False)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_METRICS_ENDPOINT", raising=False)
    assert engine_room._otel_configured() is False
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4317")
    assert engine_room._otel_configured() is True
