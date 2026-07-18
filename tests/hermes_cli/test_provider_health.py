"""Tests for the provider-health store (hermes_cli.provider_health), task t_08676525."""

from __future__ import annotations

import sqlite3

import pytest

from hermes_cli import provider_health as ph


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    try:
        yield c
    finally:
        c.close()


def test_unknown_provider_is_available(conn):
    # No table created yet -> read path must degrade to "available" (fail-open).
    assert ph.is_available(conn, "acp-claude-code") is True
    avail = ph.availability(conn, "acp-claude-code")
    assert avail["status"] == ph.STATUS_OK and avail["available"] is True


def test_empty_provider_name_is_available(conn):
    assert ph.is_available(conn, "") is True


def test_record_unavailable_blocks_then_self_heals(conn):
    now = 1_000_000
    ph.record_unavailable(conn, "acp-claude-code", reason="403", ttl_seconds=600, now=now)
    assert ph.is_available(conn, "acp-claude-code", now=now) is False
    avail = ph.availability(conn, "acp-claude-code", now=now)
    assert avail["status"] == ph.STATUS_UNHEALTHY
    assert avail["reason"] == "403"
    assert avail["until"] == now + 600

    # Just before the TTL: still blocked.
    assert ph.is_available(conn, "acp-claude-code", now=now + 599) is False
    # At/after the TTL: eligible for a fresh probe again (lazy self-heal).
    assert ph.is_available(conn, "acp-claude-code", now=now + 600) is True
    assert ph.is_available(conn, "acp-claude-code", now=now + 999) is True


def test_record_available_clears_unhealthy(conn):
    now = 2_000_000
    ph.record_unavailable(conn, "acp-codex", reason="hang", ttl_seconds=9999, now=now)
    assert ph.is_available(conn, "acp-codex", now=now) is False
    ph.record_available(conn, "acp-codex", now=now + 1)
    assert ph.is_available(conn, "acp-codex", now=now + 1) is True


def test_pause_is_sticky_and_survives_ttl(conn):
    now = 3_000_000
    ph.pause(conn, "acp-claude-code", by="operator", reason="Copilot 403", now=now)
    avail = ph.availability(conn, "acp-claude-code", now=now)
    assert avail["status"] == ph.STATUS_PAUSED and avail["available"] is False
    # A pause never auto-expires: still paused far in the future.
    assert ph.is_available(conn, "acp-claude-code", now=now + 10 ** 9) is False
    # record_available (the machine verb) must NOT lift an operator pause.
    ph.record_available(conn, "acp-claude-code", now=now + 1)
    assert ph.is_available(conn, "acp-claude-code", now=now + 1) is False
    # Only resume clears it.
    ph.resume(conn, "acp-claude-code", now=now + 2)
    assert ph.is_available(conn, "acp-claude-code", now=now + 2) is True


def test_record_unavailable_does_not_override_pause(conn):
    now = 4_000_000
    ph.pause(conn, "acp-claude-code", by="op", reason="sticky", now=now)
    ph.record_unavailable(conn, "acp-claude-code", reason="auto 403", ttl_seconds=60, now=now + 5)
    # Still reported as paused (operator wins), and it does not expire on the
    # unhealthy TTL.
    avail = ph.availability(conn, "acp-claude-code", now=now + 100)
    assert avail["status"] == ph.STATUS_PAUSED and avail["available"] is False


def test_consecutive_counter_increments(conn):
    now = 5_000_000
    ph.record_unavailable(conn, "acp-codex", reason="a", ttl_seconds=60, now=now)
    ph.record_unavailable(conn, "acp-codex", reason="b", ttl_seconds=60, now=now + 1)
    rows = {r["provider"]: r for r in ph.list_health(conn, now=now + 1)}
    assert rows["acp-codex"]["consecutive"] == 2
    assert rows["acp-codex"]["reason"] == "b"


def test_list_health_folds_expired_to_ok(conn):
    now = 6_000_000
    ph.record_unavailable(conn, "acp-codex", reason="x", ttl_seconds=10, now=now)
    listed = {r["provider"]: r for r in ph.list_health(conn, now=now + 100)}
    # The row is still present but reported ok after the TTL elapsed.
    assert listed["acp-codex"]["status"] == ph.STATUS_OK
    assert listed["acp-codex"]["available"] is True
