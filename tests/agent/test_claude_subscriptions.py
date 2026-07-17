"""Unit tests for the Claude Code subscription pool login verification.

Focus: a *stale* credentials payload (access token expired, nothing left to
renew it) must not count as a logged-in subscription. The pool leasing such a
pocket was the root cause of the 'Authentication required' session deaths that
blocked prior attempts on this task.
"""

import time

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
