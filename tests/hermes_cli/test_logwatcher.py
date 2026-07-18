"""Tests for hermes_cli.logwatcher — recurring-anomaly gate over the logs.

The pure parsing/signature/gate functions run on plain strings and dicts; the
tail cursor runs against throwaway temp log files; the occurrence + findings
stores use an isolated temp sqlite db. Nothing here touches a live log, board,
or the real zeus ledger.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from hermes_cli import logwatcher as lw


# ---------------------------------------------------------------------------
# Line parsing + signature normalisation (pure)
# ---------------------------------------------------------------------------


def test_parse_line_standard_format():
    line = "2026-07-18 05:47:57,486 WARNING gateway.run: Dropping watch notification"
    assert lw.parse_line(line) == ("WARNING", "gateway.run", "Dropping watch notification")


def test_parse_line_rejects_traceback_continuation():
    assert lw.parse_line('    File "x.py", line 5, in <module>') is None
    assert lw.parse_line("") is None


def test_signature_collapses_volatile_session_keys():
    a = ("2026-07-18 05:47:57,486 WARNING gateway.run: Synthetic event source "
         "unresolvable: session_key='20260717_010052_f08f7c' platform='' evt_type=x")
    b = ("2026-07-18 06:01:02,999 WARNING gateway.run: Synthetic event source "
         "unresolvable: session_key='20260717_020703_9e07d1' platform='' evt_type=x")
    la, lb = lw.parse_line(a), lw.parse_line(b)
    assert lw.signature(*la) == lw.signature(*lb)  # same class, different instances


def test_normalize_message_scrubs_numbers_hex_and_quotes():
    msg = "marking openrouter unhealthy for 60s; commit 687578b7bd; id='abc-123'"
    norm = lw.normalize_message(msg)
    assert "60" not in norm and "687578b7bd" not in norm and "abc-123" not in norm
    assert "<n>" in norm and "<hex>" in norm and "<v>" in norm


def test_different_loggers_do_not_collide():
    m = "connection refused"
    assert lw.signature("ERROR", "a.b", m) != lw.signature("ERROR", "c.d", m)


# ---------------------------------------------------------------------------
# Anomaly extraction
# ---------------------------------------------------------------------------


def test_is_anomaly_respects_floor():
    assert lw.is_anomaly("WARNING") and lw.is_anomaly("ERROR") and lw.is_anomaly("CRITICAL")
    assert not lw.is_anomaly("INFO") and not lw.is_anomaly("DEBUG")
    assert lw.is_anomaly("INFO", min_level="INFO")


def test_extract_anomalies_filters_info_lines():
    lines = [
        "2026-07-18 05:00:00,000 INFO gateway.run: kanban dispatcher spawned=1",
        "2026-07-18 05:00:01,000 WARNING gateway.run: Dropping watch notification",
        "not a log line at all",
        "2026-07-18 05:00:02,000 ERROR agent.x: boom pid=42",
    ]
    got = lw.extract_anomalies(lines, "gateway.log")
    assert [s.level for s in got] == ["WARNING", "ERROR"]
    assert all(s.source_log == "gateway.log" for s in got)


def test_tally_counts_repeats_of_a_signature():
    lines = [
        "2026-07-18 05:00:00,000 WARNING g.run: boom pid=1",
        "2026-07-18 05:00:01,000 WARNING g.run: boom pid=2",
        "2026-07-18 05:00:02,000 WARNING g.run: other thing",
    ]
    tally = lw.tally_sightings(lw.extract_anomalies(lines, "g.log"))
    counts = sorted(v["count"] for v in tally.values())
    assert counts == [1, 2]  # two "boom" (pid scrubbed) + one "other"


# ---------------------------------------------------------------------------
# The once-vs-recurring gate (pure)
# ---------------------------------------------------------------------------


def _tally(sig, count, sample="s", level="WARNING", log="g.log"):
    return {sig: {"count": count, "sample": sample, "level": level,
                  "logger": "g.run", "source_log": log}}


def test_gate_first_sighting_records_but_does_not_promote():
    records, findings = lw.gate({}, _tally("WARNING|g.run|boom", 1),
                                threshold=2, now=100.0)
    assert findings == []                       # one-time noise -> no draft card
    assert len(records) == 1 and records[0].count == 1
    assert records[0].promoted_at is None
    assert records[0].first_seen_at == 100.0


def test_gate_promotes_on_reaching_threshold():
    prior = {"WARNING|g.run|boom": lw.Occurrence(
        signature="WARNING|g.run|boom", count=1, first_seen_at=100.0,
        last_seen_at=100.0, sample="s", level="WARNING", source_log="g.log")}
    records, findings = lw.gate(prior, _tally("WARNING|g.run|boom", 1),
                                threshold=2, now=200.0)
    assert len(findings) == 1
    assert findings[0]["finding_key"] == "logwatch:WARNING|g.run|boom"
    assert findings[0]["evidence"]["count"] == 2
    assert records[0].promoted_at == 200.0
    assert records[0].first_seen_at == 100.0   # preserved across passes


def test_gate_promotes_immediately_when_burst_crosses_threshold():
    records, findings = lw.gate({}, _tally("ERROR|g.run|boom", 3, level="ERROR"),
                                threshold=2, now=100.0)
    assert len(findings) == 1 and findings[0]["severity"] == "error"


def test_gate_preserves_promoted_at_on_reemit():
    prior = {"WARNING|g.run|boom": lw.Occurrence(
        signature="WARNING|g.run|boom", count=5, first_seen_at=10.0,
        last_seen_at=50.0, sample="s", level="WARNING", source_log="g.log",
        promoted_at=20.0)}
    records, findings = lw.gate(prior, _tally("WARNING|g.run|boom", 1),
                                threshold=2, now=99.0)
    assert len(findings) == 1
    assert records[0].promoted_at == 20.0      # first crossing kept, not bumped
    assert records[0].count == 6


# ---------------------------------------------------------------------------
# Tail cursor (IO against temp files)
# ---------------------------------------------------------------------------


def test_read_new_lines_reads_only_the_appended_tail(tmp_path):
    log = tmp_path / "a.log"
    log.write_text("line1\nline2\n")
    lines, cur = lw.read_new_lines(log, lw.Cursor())
    assert lines == ["line1", "line2"]
    # Nothing new yet.
    lines2, cur2 = lw.read_new_lines(log, cur)
    assert lines2 == []
    # Append and re-read: only the new line.
    with open(log, "a") as fh:
        fh.write("line3\n")
    lines3, _ = lw.read_new_lines(log, cur2)
    assert lines3 == ["line3"]


def test_read_new_lines_leaves_partial_trailing_line(tmp_path):
    log = tmp_path / "a.log"
    log.write_text("complete\npartial-no-newline")
    lines, cur = lw.read_new_lines(log, lw.Cursor())
    assert lines == ["complete"]
    with open(log, "a") as fh:
        fh.write("-now-done\n")
    lines2, _ = lw.read_new_lines(log, cur)
    assert lines2 == ["partial-no-newline-now-done"]


def test_read_new_lines_handles_truncation_rewind(tmp_path):
    log = tmp_path / "a.log"
    log.write_text("aaaa\nbbbb\ncccc\n")
    _, cur = lw.read_new_lines(log, lw.Cursor())
    log.write_text("fresh\n")  # truncate + rewrite (size < offset)
    lines, _ = lw.read_new_lines(log, cur)
    assert lines == ["fresh"]


def test_read_new_lines_missing_file_is_noop():
    cur = lw.Cursor(offset=5, inode=1, size=5)
    assert lw.read_new_lines("/no/such/file.log", cur) == ([], cur)


# ---------------------------------------------------------------------------
# Persistence + end-to-end scan (isolated temp store)
# ---------------------------------------------------------------------------


def _store(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    lw.ensure_schema(conn)
    return conn


def test_occurrence_roundtrip(tmp_path):
    conn = _store(tmp_path / "zeus.db")
    rec = lw.Occurrence(signature="WARNING|g|boom", count=3, first_seen_at=1.0,
                        last_seen_at=2.0, sample="s", level="WARNING",
                        source_log="g.log", promoted_at=2.0)
    lw.save_occurrences(conn, [rec])
    back = lw.load_occurrences(conn)
    assert back["WARNING|g|boom"].count == 3
    assert back["WARNING|g|boom"].promoted_at == 2.0


def test_save_occurrences_preserves_first_promoted_at(tmp_path):
    conn = _store(tmp_path / "zeus.db")
    lw.save_occurrences(conn, [lw.Occurrence(
        signature="s", count=2, first_seen_at=1.0, last_seen_at=2.0, sample="x",
        level="WARNING", source_log="g", promoted_at=2.0)])
    # Re-save with a later promoted_at -> COALESCE keeps the original.
    lw.save_occurrences(conn, [lw.Occurrence(
        signature="s", count=3, first_seen_at=1.0, last_seen_at=9.0, sample="x",
        level="WARNING", source_log="g", promoted_at=9.0)])
    assert lw.load_occurrences(conn)["s"].promoted_at == 2.0


def test_first_scan_tails_and_does_not_flood(tmp_path):
    """First attach seeds the cursor to EOF: pre-existing history is ignored."""
    log = tmp_path / "errors.log"
    log.write_text(
        "2026-07-18 05:00:00,000 ERROR g.run: boom pid=1\n"
        "2026-07-18 05:00:01,000 ERROR g.run: boom pid=2\n"
    )
    conn = _store(tmp_path / "zeus.db")
    findings = lw.run_logwatch_scan(log_paths=[log], conn=conn, threshold=2, now=100.0)
    assert findings == []                        # history not counted on attach
    assert lw.load_occurrences(conn) == {}


def test_scan_records_first_then_promotes_on_repeat(tmp_path):
    log = tmp_path / "errors.log"
    log.write_text("")  # start empty so first scan just seeds the cursor
    conn = _store(tmp_path / "zeus.db")
    lw.run_logwatch_scan(log_paths=[log], conn=conn, threshold=2, now=100.0)

    # First real occurrence -> recorded, no finding.
    with open(log, "a") as fh:
        fh.write("2026-07-18 05:00:00,000 ERROR g.run: kaboom pid=1\n")
    f1 = lw.run_logwatch_scan(log_paths=[log], conn=conn, threshold=2, now=200.0)
    assert f1 == []
    assert lw.load_occurrences(conn)["ERROR|g.run|kaboom pid=<n>"].count == 1

    # Second occurrence of the same signature -> promoted to a finding.
    with open(log, "a") as fh:
        fh.write("2026-07-18 05:05:00,000 ERROR g.run: kaboom pid=2\n")
    f2 = lw.run_logwatch_scan(log_paths=[log], conn=conn, threshold=2, now=300.0)
    assert len(f2) == 1
    row = conn.execute(
        "SELECT * FROM findings WHERE source='logwatcher'").fetchone()
    assert row["status"] == "open"
    assert row["category"] == "log-anomaly"
    assert row["board"] == ""            # global system-level problem


def test_scan_from_start_counts_history(tmp_path):
    log = tmp_path / "errors.log"
    log.write_text(
        "2026-07-18 05:00:00,000 ERROR g.run: boom pid=1\n"
        "2026-07-18 05:00:01,000 ERROR g.run: boom pid=2\n"
    )
    conn = _store(tmp_path / "zeus.db")
    findings = lw.run_logwatch_scan(
        log_paths=[log], conn=conn, threshold=2, from_start=True, now=100.0)
    assert len(findings) == 1            # two of the same signature in history

