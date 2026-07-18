"""Unit tests for the Claude Code subscription pool.

The original focus was login verification: a *stale* credentials payload
(access token expired, nothing left to renew it) must not count as a
logged-in subscription. That covers the ``_credentials_live`` liveness gate.

The rest of this file covers the load-bearing money/rotation logic that was
previously untested: reset-time parsing (``parse_limit_reset`` /
``_parse_clock_reset`` / ``next_window_boundary``), cooldown writes
(``mark_limited``), the rotation-decision regexes (``is_usage_limit_error`` /
``is_auth_error``), and the leasing math (``_selectable_rows`` /
``_try_acquire`` / ``acquire`` — concurrency cap + spread order + the
``NoSubscriptionAvailable`` exhaustion paths).

All DB-touching tests run against the per-test ``HERMES_HOME`` tempdir that
``tests/conftest.py`` pins, so they never touch the real zeus sidecar DB. The
clock-parsing tests pin ``TZ=UTC`` + ``time.tzset()`` for determinism, since
``_parse_clock_reset`` builds candidates via naive ``datetime.fromtimestamp``.
"""

import os
import subprocess
import time
from datetime import datetime, timezone

import pytest

from agent import claude_subscriptions as subs


def _oauth(*, access="tok", refresh=None, expires_at=None):
    payload = {"accessToken": access}
    if refresh is not None:
        payload["refreshToken"] = refresh
    if expires_at is not None:
        payload["expiresAt"] = expires_at
    return payload


_FUTURE_MS = lambda: int(time.time() * 1000) + 3_600_000  # noqa: E731
_PAST_MS = lambda: int(time.time() * 1000) - 3_600_000  # noqa: E731


class TestCredentialsLive:
    def test_no_credentials_is_logged_out(self):
        assert subs._credentials_live(None) is False

    def test_empty_payload_is_logged_out(self):
        assert subs._credentials_live({}) is False

    def test_no_access_token_is_logged_out(self):
        assert subs._credentials_live({"refreshToken": "r"}) is False

    def test_unexpired_token_is_live(self):
        assert subs._credentials_live(_oauth(expires_at=_FUTURE_MS())) is True

    def test_token_without_expiry_is_live(self):
        # Managed keys / unknown expiry: an access token with no expiresAt is
        # treated as live, matching is_claude_code_token_valid.
        assert subs._credentials_live(_oauth()) is True

    def test_expired_without_refresh_is_stale(self):
        # The bug this fixes: an expired access token and no way to renew it.
        assert subs._credentials_live(_oauth(expires_at=_PAST_MS())) is False

    def test_zero_expiry_is_managed_key_and_live(self):
        # expiresAt == 0 means "managed key / unknown expiry" in this codebase
        # (matches is_claude_code_token_valid) — a present token counts as live.
        assert subs._credentials_live(_oauth(expires_at=0)) is True

    def test_expired_with_refresh_stays_live(self):
        # Claude Code renews on session start; auth-error rotation is the
        # backstop if the refresh token itself is revoked.
        assert (
            subs._credentials_live(
                _oauth(refresh="r", expires_at=_PAST_MS())
            )
            is True
        )


class TestIsLoggedIn:
    @pytest.fixture(autouse=True)
    def _clear_cache(self):
        subs._login_cache.clear()
        yield
        subs._login_cache.clear()

    def test_reads_and_verifies_credentials(self, monkeypatch):
        monkeypatch.setattr(
            subs, "read_subscription_credentials",
            lambda config_dir: _oauth(expires_at=_FUTURE_MS()),
        )
        assert subs.is_logged_in("/fake/live") is True

    def test_stale_credentials_are_not_logged_in(self, monkeypatch):
        monkeypatch.setattr(
            subs, "read_subscription_credentials",
            lambda config_dir: _oauth(expires_at=_PAST_MS()),
        )
        assert subs.is_logged_in("/fake/stale") is False

    def test_result_is_cached(self, monkeypatch):
        calls = {"n": 0}

        def _read(config_dir):
            calls["n"] += 1
            return _oauth(expires_at=_FUTURE_MS())

        monkeypatch.setattr(subs, "read_subscription_credentials", _read)
        assert subs.is_logged_in("/fake/cached") is True
        assert subs.is_logged_in("/fake/cached") is True
        assert calls["n"] == 1


class TestSubscriptionAccessToken:
    def test_returns_access_token(self, monkeypatch):
        monkeypatch.setattr(
            subs, "read_subscription_credentials",
            lambda config_dir: _oauth(access="abc123"),
        )
        assert subs.subscription_access_token("/fake") == "abc123"

    def test_none_when_no_credentials(self, monkeypatch):
        monkeypatch.setattr(
            subs, "read_subscription_credentials", lambda config_dir: None
        )
        assert subs.subscription_access_token("/fake") is None


# A fixed reference "now" well in the past so future-vs-past reset comparisons
# are unambiguous regardless of the wall clock. 1_700_000_000 = 2023-11-14 UTC.
_NOW = 1_700_000_000.0


class TestParseLimitReset:
    """parse_limit_reset: epoch-after-pipe, resets_at=<epoch>, ISO, am/pm.

    A missed branch here silently strands a pocket in the wrong cooldown or
    fails to back off at all, so every recognised shape is pinned.
    """

    def test_epoch_after_pipe(self):
        msg = f"Claude AI usage limit reached|{int(_NOW + 3600)}"
        assert subs.parse_limit_reset(msg, _NOW) == _NOW + 3600

    def test_epoch_milliseconds_after_pipe_is_divided(self):
        ms = int((_NOW + 3600) * 1000)
        assert subs.parse_limit_reset(f"limit|{ms}", _NOW) == pytest.approx(_NOW + 3600)

    def test_resets_at_snake_case_epoch(self):
        msg = f"resets_at={int(_NOW + 7200)}"
        assert subs.parse_limit_reset(msg, _NOW) == _NOW + 7200

    def test_resets_at_camel_case_json_epoch(self):
        msg = f'{{"resetsAt": {int(_NOW + 60)}}}'
        assert subs.parse_limit_reset(msg, _NOW) == _NOW + 60

    def test_past_epoch_returns_none(self):
        # A reset time already in the past is not a usable cooldown horizon.
        msg = f"limit|{int(_NOW - 3600)}"
        assert subs.parse_limit_reset(msg, _NOW) is None

    def test_iso_utc_timestamp(self):
        expected = datetime.fromisoformat("2035-01-01T00:00:00+00:00").timestamp()
        assert subs.parse_limit_reset("resets 2035-01-01T00:00:00Z", _NOW) == expected

    def test_iso_with_offset(self):
        expected = datetime.fromisoformat("2035-06-01T12:30:00+02:00").timestamp()
        got = subs.parse_limit_reset("window until 2035-06-01T12:30:00+02:00", _NOW)
        assert got == expected

    def test_iso_naive_no_seconds(self):
        # No Z/offset and no seconds: still parsed (naive, local tz) and, being
        # in 2035, is in the future relative to _NOW.
        got = subs.parse_limit_reset("resets 2035-01-01T12:00", _NOW)
        assert got is not None and got > _NOW

    def test_past_iso_returns_none(self):
        assert subs.parse_limit_reset("was 2000-01-01T00:00:00Z", _NOW) is None

    def test_no_reset_information_returns_none(self):
        assert subs.parse_limit_reset("you hit your usage limit", _NOW) is None

    def test_epoch_pipe_takes_priority_over_iso(self):
        # Both present: the pipe-epoch branch is tried first.
        msg = f"limit 2035-01-01T00:00:00Z |{int(_NOW + 100)}"
        assert subs.parse_limit_reset(msg, _NOW) == _NOW + 100


@pytest.fixture()
def _utc_clock():
    """Pin libc timezone to UTC so datetime.fromtimestamp is deterministic.

    ``_parse_clock_reset`` builds candidates with naive
    ``datetime.fromtimestamp`` / ``.timestamp()``, which read the process
    timezone. conftest sets ``TZ=UTC`` in the environment but does not call
    ``time.tzset()``, so force it here and restore afterwards.
    """
    old = os.environ.get("TZ")
    os.environ["TZ"] = "UTC"
    time.tzset()
    yield
    if old is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = old
    time.tzset()


class TestParseClockReset:
    """am/pm reset phrasing. now = 2030-06-15 10:00 UTC (local hour 10)."""

    _NOON_ANCHOR = datetime(2030, 6, 15, 10, 0, 0, tzinfo=timezone.utc).timestamp()

    def test_afternoon_same_day(self, _utc_clock):
        got = subs.parse_limit_reset("your limit resets at 3pm", self._NOON_ANCHOR)
        assert got is not None
        dt = datetime.fromtimestamp(got)
        assert (dt.hour, dt.minute) == (15, 0)
        assert 0 < got - self._NOON_ANCHOR <= 6 * 3600  # later today

    def test_morning_rolls_to_next_day(self, _utc_clock):
        # 9am is already past at 10:00, so the candidate rolls forward a day.
        got = subs.parse_limit_reset("resets 9am", self._NOON_ANCHOR)
        assert got is not None
        dt = datetime.fromtimestamp(got)
        assert dt.hour == 9
        # 10:00 today -> 09:00 tomorrow is exactly 23h away.
        assert got - self._NOON_ANCHOR == 23 * 3600

    def test_minutes_and_12am_handling(self, _utc_clock):
        got = subs.parse_limit_reset("resets at 12:30am", self._NOON_ANCHOR)
        assert got is not None
        dt = datetime.fromtimestamp(got)
        assert (dt.hour, dt.minute) == (0, 30)

    def test_12pm_is_noon(self, _utc_clock):
        got = subs._parse_clock_reset("resets 12pm", self._NOON_ANCHOR)
        assert got is not None
        assert datetime.fromtimestamp(got).hour == 12

    def test_no_clock_phrase_returns_none(self, _utc_clock):
        assert subs._parse_clock_reset("nothing here", self._NOON_ANCHOR) is None


class TestNextWindowBoundary:
    def test_aligns_to_5h_grid(self):
        boundary = subs.next_window_boundary(_NOW)
        assert boundary > _NOW
        assert boundary % subs.LIMIT_WINDOW_SECONDS == 0
        assert boundary - _NOW <= subs.LIMIT_WINDOW_SECONDS

    def test_exactly_on_boundary_advances(self):
        on_grid = float(2 * subs.LIMIT_WINDOW_SECONDS)
        assert subs.next_window_boundary(on_grid) == 3 * subs.LIMIT_WINDOW_SECONDS


class TestIsUsageLimitError:
    @pytest.mark.parametrize("text", [
        "You've hit your usage limit for now.",
        "hit your 5-hour limit",
        "hit your weekly limit, try later",
        "Claude usage limit reached",
        "You've reached your session limit",
        "weekly limit reached",
        "Your limit will reset at 3pm",
    ])
    def test_positive(self, text):
        assert subs.is_usage_limit_error(text) is True

    @pytest.mark.parametrize("text", [
        None,
        "",
        "the task completed successfully",
        "here is a summary of rate-limiting concepts in distributed systems",
    ])
    def test_negative(self, text):
        assert subs.is_usage_limit_error(text) is False

    def test_verbose_exception_is_still_a_limit_death(self):
        # These matchers now run only on error text (exception message / adapter
        # stderr), never a handoff, so there is no length cap: a genuine limit
        # can arrive as a multi-line exception and must still be detected (the
        # old 600-char gate parked such tasks as capability failures).
        text = (
            "Copilot ACP session/prompt failed: the upstream provider returned "
            + "an error. " * 80
            + "You've hit your usage limit; try again later."
        )
        assert len(text) > 600
        assert subs.is_usage_limit_error(text) is True


class TestIsAuthError:
    @pytest.mark.parametrize("text", [
        "Authentication required",
        "You are not logged in",
        "Please run /login to continue",
        "OAuth token has expired",
        "oauth token been revoked",
        "invalid api key",
        "Invalid bearer token",
    ])
    def test_positive(self, text):
        assert subs.is_auth_error(text) is True

    @pytest.mark.parametrize("text", [None, "", "everything is fine"])
    def test_negative(self, text):
        assert subs.is_auth_error(text) is False

    def test_verbose_exception_is_still_an_auth_death(self):
        text = (
            "Copilot ACP session/prompt failed: " + "diagnostic detail. " * 80
            + "Authentication required - please run /login."
        )
        assert len(text) > 600
        assert subs.is_auth_error(text) is True


def _insert_sub(conn, name, config_dir, *, enabled=1, max_concurrency=4,
                cooling_until=None, last_limited_at=None):
    now = time.time()
    conn.execute(
        "INSERT INTO claude_subscriptions"
        " (name, config_dir, max_concurrency, enabled, cooling_until,"
        "  last_limited_at, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (name, config_dir, max_concurrency, enabled, cooling_until,
         last_limited_at, now, now),
    )
    conn.commit()


def _add_lease(conn, name, *, pid=None):
    conn.execute(
        "INSERT INTO subscription_leases (subscription, task_id, pid, acquired_at)"
        " VALUES (?, ?, ?, ?)",
        (name, "", pid if pid is not None else os.getpid(), time.time()),
    )
    conn.commit()


class TestMarkLimited:
    """mark_limited: cooldown write + event log. Uses the isolated temp DB."""

    def test_uses_parsed_reset_and_records_event(self):
        conn = subs.connect()
        try:
            _insert_sub(conn, "acct", "/tmp/acct")
            reset = subs.mark_limited("acct", f"limit|{int(_NOW + 3600)}", now=_NOW)
            assert reset == _NOW + 3600
            row = conn.execute(
                "SELECT cooling_until, last_limited_at FROM claude_subscriptions"
                " WHERE name = 'acct'"
            ).fetchone()
            assert row["cooling_until"] == _NOW + 3600
            assert row["last_limited_at"] == _NOW
            event = conn.execute(
                "SELECT kind, reset_at FROM subscription_events WHERE subscription = 'acct'"
            ).fetchone()
            assert event["kind"] == "limit"
            assert event["reset_at"] == _NOW + 3600
        finally:
            conn.close()

    def test_falls_back_to_window_boundary_without_reset(self):
        conn = subs.connect()
        try:
            _insert_sub(conn, "acct", "/tmp/acct")
            reset = subs.mark_limited("acct", "you hit your usage limit", now=_NOW)
            assert reset == subs.next_window_boundary(_NOW)
        finally:
            conn.close()

    def test_long_message_is_truncated_in_event_detail(self):
        conn = subs.connect()
        try:
            _insert_sub(conn, "acct", "/tmp/acct")
            subs.mark_limited("acct", "z" * 5000, now=_NOW)
            detail = conn.execute(
                "SELECT detail FROM subscription_events WHERE subscription = 'acct'"
            ).fetchone()["detail"]
            assert len(detail) == 2000
        finally:
            conn.close()


@pytest.fixture()
def _logged_in(monkeypatch):
    """Treat every config dir as logged-in unless explicitly excluded."""
    excluded = set()
    monkeypatch.setattr(subs, "is_logged_in", lambda cd: cd not in excluded)
    return excluded


class TestSelectableRows:
    """_selectable_rows: spread ordering + the exclusion filters."""

    def test_spread_order_fewest_active_first(self, tmp_path, _logged_in):
        conn = subs.connect()
        try:
            for name in ("a", "b", "c"):
                d = tmp_path / name
                d.mkdir()
                _insert_sub(conn, name, str(d))
            _add_lease(conn, "a")
            _add_lease(conn, "a")
            _add_lease(conn, "c")
            names = [r["name"] for r in subs._selectable_rows(conn, time.time())]
            assert names == ["b", "c", "a"]
        finally:
            conn.close()

    def test_tie_break_least_recently_limited_then_name(self, tmp_path, _logged_in):
        conn = subs.connect()
        try:
            for name, limited in (("x", 500.0), ("y", 100.0), ("z", None)):
                d = tmp_path / name
                d.mkdir()
                _insert_sub(conn, name, str(d), last_limited_at=limited)
            # All zero active. Order: z (COALESCE 0), y (100), x (500).
            names = [r["name"] for r in subs._selectable_rows(conn, time.time())]
            assert names == ["z", "y", "x"]
        finally:
            conn.close()

    def test_cooling_and_disabled_and_loggedout_and_missing_excluded(
        self, tmp_path, _logged_in
    ):
        conn = subs.connect()
        try:
            now = time.time()
            live = tmp_path / "live"
            live.mkdir()
            _insert_sub(conn, "live", str(live))

            cooling = tmp_path / "cooling"
            cooling.mkdir()
            _insert_sub(conn, "cooling", str(cooling), cooling_until=now + 1000)

            disabled = tmp_path / "disabled"
            disabled.mkdir()
            _insert_sub(conn, "disabled", str(disabled), enabled=0)

            out = tmp_path / "out"
            out.mkdir()
            _insert_sub(conn, "out", str(out))
            _logged_in.add(str(out))  # logged out

            _insert_sub(conn, "gone", str(tmp_path / "does-not-exist"))

            names = [r["name"] for r in subs._selectable_rows(conn, now)]
            assert names == ["live"]
        finally:
            conn.close()

    def test_expired_cooldown_is_selectable(self, tmp_path, _logged_in):
        conn = subs.connect()
        try:
            now = time.time()
            d = tmp_path / "warm"
            d.mkdir()
            _insert_sub(conn, "warm", str(d), cooling_until=now - 1)
            names = [r["name"] for r in subs._selectable_rows(conn, now)]
            assert names == ["warm"]
        finally:
            conn.close()


class TestTryAcquire:
    def test_leases_first_candidate(self, tmp_path, _logged_in):
        conn = subs.connect()
        try:
            d = tmp_path / "a"
            d.mkdir()
            _insert_sub(conn, "a", str(d))
            lease = subs._try_acquire(conn, "task-1", time.time())
            assert lease is not None
            assert lease.name == "a"
            assert lease.config_dir == str(d)
            count = conn.execute(
                "SELECT COUNT(*) AS n FROM subscription_leases WHERE subscription = 'a'"
            ).fetchone()["n"]
            assert count == 1
        finally:
            conn.close()

    def test_skips_capped_subscription_for_next(self, tmp_path, _logged_in):
        conn = subs.connect()
        try:
            capped = tmp_path / "capped"
            capped.mkdir()
            _insert_sub(conn, "capped", str(capped), max_concurrency=1)
            _add_lease(conn, "capped")  # already at cap
            free = tmp_path / "free"
            free.mkdir()
            _insert_sub(conn, "free", str(free))
            lease = subs._try_acquire(conn, "t", time.time())
            assert lease is not None and lease.name == "free"
        finally:
            conn.close()

    def test_all_capped_returns_none(self, tmp_path, _logged_in):
        conn = subs.connect()
        try:
            d = tmp_path / "only"
            d.mkdir()
            _insert_sub(conn, "only", str(d), max_concurrency=1)
            _add_lease(conn, "only")
            assert subs._try_acquire(conn, "t", time.time()) is None
        finally:
            conn.close()

    def test_stale_lease_from_dead_pid_is_reclaimed(self, tmp_path, _logged_in):
        # A lease whose owning process is gone must not count toward the cap.
        proc = subprocess.Popen(["true"])
        proc.wait()
        dead_pid = proc.pid
        conn = subs.connect()
        try:
            d = tmp_path / "s"
            d.mkdir()
            _insert_sub(conn, "s", str(d), max_concurrency=1)
            _add_lease(conn, "s", pid=dead_pid)
            lease = subs._try_acquire(conn, "t", time.time())
            assert lease is not None and lease.name == "s"
        finally:
            conn.close()


class TestAcquire:
    @pytest.fixture(autouse=True)
    def _no_disk_discovery(self, monkeypatch):
        # Keep sync_registry from importing the developer's real ~/.claude
        # dirs into the temp DB. Tests insert exactly the pool they want.
        monkeypatch.setattr(subs, "_discover_config_dirs", lambda: {})

    def test_returns_lease_when_capacity_exists(self, tmp_path, _logged_in):
        conn = subs.connect()
        try:
            d = tmp_path / "a"
            d.mkdir()
            _insert_sub(conn, "a", str(d))
        finally:
            conn.close()
        lease = subs.acquire("task-x", wait_seconds=0)
        assert lease.name == "a"

    def test_all_cooling_raises_with_earliest_recovery(self, tmp_path, _logged_in):
        now = time.time()
        conn = subs.connect()
        try:
            a = tmp_path / "a"
            a.mkdir()
            b = tmp_path / "b"
            b.mkdir()
            _insert_sub(conn, "a", str(a), cooling_until=now + 900)
            _insert_sub(conn, "b", str(b), cooling_until=now + 300)
        finally:
            conn.close()
        with pytest.raises(subs.NoSubscriptionAvailable) as exc:
            subs.acquire("t", wait_seconds=0)
        assert subs.SUBSCRIPTIONS_EXHAUSTED_MARKER in str(exc.value)
        # Earliest recovery is the soonest cooling_until across the pool.
        assert exc.value.earliest_recovery == pytest.approx(now + 300, abs=2)

    def test_at_capacity_raises_after_wait_without_recovery(self, tmp_path, _logged_in):
        # Not cooling, just fully leased: distinct from exhaustion — no
        # earliest_recovery, and the message names capacity, not cooling.
        conn = subs.connect()
        try:
            d = tmp_path / "a"
            d.mkdir()
            _insert_sub(conn, "a", str(d), max_concurrency=1)
            _add_lease(conn, "a")
        finally:
            conn.close()
        with pytest.raises(subs.NoSubscriptionAvailable) as exc:
            subs.acquire("t", wait_seconds=0)
        assert exc.value.earliest_recovery is None
        assert "capacity" in str(exc.value)

    def test_saturation_is_flagged_and_uses_saturated_marker(self, tmp_path, _logged_in):
        # Fully leased (not cooling): the raise must be flagged ``saturated``
        # and carry the SATURATED marker, NOT the exhausted one — the executor
        # keys off this to requeue instead of block (t_4c4dbe64).
        conn = subs.connect()
        try:
            d = tmp_path / "a"
            d.mkdir()
            _insert_sub(conn, "a", str(d), max_concurrency=1)
            _add_lease(conn, "a")
        finally:
            conn.close()
        with pytest.raises(subs.NoSubscriptionAvailable) as exc:
            subs.acquire("t", wait_seconds=0)
        assert exc.value.saturated is True
        assert subs.SUBSCRIPTIONS_SATURATED_MARKER in str(exc.value)
        assert subs.SUBSCRIPTIONS_EXHAUSTED_MARKER not in str(exc.value)

    def test_cooling_case_is_not_flagged_saturated(self, tmp_path, _logged_in):
        # Genuine exhaustion (every pocket cooling) must NOT be flagged
        # saturated — it stays on the block + auto-unblock path.
        now = time.time()
        conn = subs.connect()
        try:
            a = tmp_path / "a"
            a.mkdir()
            _insert_sub(conn, "a", str(a), cooling_until=now + 300)
        finally:
            conn.close()
        with pytest.raises(subs.NoSubscriptionAvailable) as exc:
            subs.acquire("t", wait_seconds=0)
        assert exc.value.saturated is False


class TestCapacitySnapshot:
    @pytest.fixture(autouse=True)
    def _no_disk_discovery(self, monkeypatch):
        monkeypatch.setattr(subs, "_discover_config_dirs", lambda: {})

    def test_reports_total_and_free_slots(self, tmp_path, _logged_in):
        conn = subs.connect()
        try:
            a = tmp_path / "a"
            a.mkdir()
            b = tmp_path / "b"
            b.mkdir()
            _insert_sub(conn, "a", str(a), max_concurrency=2)
            _insert_sub(conn, "b", str(b), max_concurrency=3)
            _add_lease(conn, "a")  # 1 of a's 2 slots busy
        finally:
            conn.close()
        total, free = subs.capacity_snapshot()
        assert total == 5      # 2 + 3
        assert free == 4       # (2-1) + (3-0)

    def test_saturated_pool_reports_zero_free_nonzero_total(self, tmp_path, _logged_in):
        conn = subs.connect()
        try:
            d = tmp_path / "a"
            d.mkdir()
            _insert_sub(conn, "a", str(d), max_concurrency=1)
            _add_lease(conn, "a")
        finally:
            conn.close()
        total, free = subs.capacity_snapshot()
        assert total == 1
        assert free == 0

    def test_cooling_pocket_contributes_nothing(self, tmp_path, _logged_in):
        now = time.time()
        conn = subs.connect()
        try:
            a = tmp_path / "a"
            a.mkdir()
            _insert_sub(conn, "a", str(a), max_concurrency=2, cooling_until=now + 300)
        finally:
            conn.close()
        # All capacity is cooling -> total 0 (exhaustion), distinct from
        # saturation's total>0/free==0.
        assert subs.capacity_snapshot() == (0, 0)


class TestNoSubscriptionAvailable:
    def test_carries_earliest_recovery(self):
        err = subs.NoSubscriptionAvailable("boom", earliest_recovery=123.0)
        assert err.earliest_recovery == 123.0
        assert str(err) == "boom"

    def test_defaults_recovery_to_none(self):
        assert subs.NoSubscriptionAvailable("boom").earliest_recovery is None
