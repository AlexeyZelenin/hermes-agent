"""Tests for hermes_cli.engine_log — the structured engine-room log's
normalization/validation core (t_adf37522).

Pure module: it defines the vocabulary (sources/severities) and clamps
untrusted, browser-submitted log records to a safe, bounded shape before they
reach the store. Nothing here touches a DB — the round-trip against a live board
lives in test_kanban_engine_log.py.
"""

from __future__ import annotations

from hermes_cli import engine_log as el

NOW = 1_700_000_000


# --- severity ladder --------------------------------------------------------


def test_normalize_severity_snaps_to_ladder():
    assert el.normalize_severity("warn") == "warn"
    assert el.normalize_severity("ERROR") == "error"
    assert el.normalize_severity("  info ") == "info"
    # Unknown / non-str → default.
    assert el.normalize_severity("catastrophe") == el.DEFAULT_SEVERITY
    assert el.normalize_severity(None) == el.DEFAULT_SEVERITY
    assert el.normalize_severity(7) == el.DEFAULT_SEVERITY


def test_severity_at_least_is_a_minimum_floor():
    assert el.severity_at_least("error", "warn") is True
    assert el.severity_at_least("warn", "warn") is True
    assert el.severity_at_least("info", "warn") is False
    # Unknown stored severity never masquerades as above a real floor.
    assert el.severity_at_least("bogus", "info") is False
    # Unknown floor treated as weakest → everything passes.
    assert el.severity_at_least("debug", "bogus") is True


# --- single-entry normalization ---------------------------------------------


def test_normalize_client_entry_happy_path():
    entry = el.normalize_client_entry(
        {
            "severity": "warn",
            "category": "ws",
            "event": "ws.close",
            "task_id": "t_123",
            "session_id": "web-abc",
            "payload": {"code": 1008},
            "created_at": NOW - 5,
        },
        now=NOW,
    )
    assert entry == {
        "source": "client",
        "severity": "warn",
        "category": "ws",
        "event": "ws.close",
        "task_id": "t_123",
        "session_id": "web-abc",
        "payload": '{"code": 1008}',
        "created_at": NOW - 5,
    }


def test_source_is_forced_to_client_even_if_spoofed():
    """A browser can never forge an operator-sourced row."""
    entry = el.normalize_client_entry(
        {"event": "x", "source": "operator"}, now=NOW,
    )
    assert entry is not None and entry["source"] == "client"


def test_event_is_required():
    assert el.normalize_client_entry({"severity": "info"}, now=NOW) is None
    assert el.normalize_client_entry({"event": "   "}, now=NOW) is None
    assert el.normalize_client_entry("not-a-dict", now=NOW) is None
    assert el.normalize_client_entry(None, now=NOW) is None


def test_string_fields_are_truncated():
    entry = el.normalize_client_entry(
        {
            "event": "e" * 500,
            "category": "c" * 500,
            "task_id": "t" * 500,
            "session_id": "s" * 500,
        },
        now=NOW,
    )
    assert len(entry["event"]) == el.MAX_EVENT_CHARS
    assert len(entry["category"]) == el.MAX_CATEGORY_CHARS
    assert len(entry["task_id"]) == el.MAX_ID_CHARS
    assert len(entry["session_id"]) == el.MAX_ID_CHARS


def test_oversized_payload_is_dropped_not_the_line():
    entry = el.normalize_client_entry(
        {"event": "big", "payload": {"blob": "x" * (el.MAX_PAYLOAD_CHARS + 100)}},
        now=NOW,
    )
    assert entry is not None
    assert entry["event"] == "big"
    assert entry["payload"] is None


def test_non_dict_payload_is_wrapped():
    entry = el.normalize_client_entry({"event": "e", "payload": [1, 2, 3]}, now=NOW)
    assert entry["payload"] == '{"value": [1, 2, 3]}'


def test_exotic_payload_object_serialises_via_str_fallback():
    """default=str keeps a non-JSON object from blocking the line."""
    entry = el.normalize_client_entry(
        {"event": "e", "payload": {"o": object()}}, now=NOW,
    )
    assert entry is not None
    assert entry["payload"] is not None and "object" in entry["payload"]


def test_clock_skew_outside_window_is_replaced_with_now():
    ancient = el.normalize_client_entry(
        {"event": "e", "created_at": 1}, now=NOW,
    )
    assert ancient["created_at"] == NOW
    future = el.normalize_client_entry(
        {"event": "e", "created_at": NOW + el.CLOCK_SKEW_TOLERANCE_SECONDS + 10},
        now=NOW,
    )
    assert future["created_at"] == NOW
    # A bool must not be treated as an int timestamp.
    boolean = el.normalize_client_entry(
        {"event": "e", "created_at": True}, now=NOW,
    )
    assert boolean["created_at"] == NOW


# --- batch sanitisation -----------------------------------------------------


def test_sanitize_batch_drops_invalid_and_keeps_valid():
    raw = [
        {"event": "ok1"},
        {"severity": "info"},        # no event → dropped
        "garbage",                   # not a dict → dropped
        {"event": "ok2", "severity": "error"},
    ]
    out = el.sanitize_client_batch(raw, now=NOW)
    assert [e["event"] for e in out] == ["ok1", "ok2"]
    assert out[1]["severity"] == "error"


def test_sanitize_batch_caps_length():
    raw = [{"event": f"e{i}"} for i in range(el.MAX_BATCH + 50)]
    out = el.sanitize_client_batch(raw, now=NOW)
    assert len(out) == el.MAX_BATCH


def test_batch_session_id_backfills_missing_entries():
    raw = [
        {"event": "a"},
        {"event": "b", "session_id": "own"},
    ]
    out = el.sanitize_client_batch(raw, now=NOW, session_id="batch-sess")
    assert out[0]["session_id"] == "batch-sess"
    assert out[1]["session_id"] == "own"  # per-entry wins


def test_sanitize_batch_rejects_non_iterable_body():
    assert el.sanitize_client_batch(None, now=NOW) == []
    assert el.sanitize_client_batch("string", now=NOW) == []
    assert el.sanitize_client_batch({"event": "x"}, now=NOW) == []
