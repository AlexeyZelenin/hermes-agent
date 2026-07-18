"""Tests for the local mlx-whisper STT provider (chat-composer voice input).

The chat composer's microphone button forces ``provider="mlx"`` so dictation
always runs through on-device mlx-whisper, never a cloud API. These tests
cover the language normaliser, provider selection, the transcription wrapper
(installed + not-installed), and the forced-provider dispatch path.

All external dependencies (the ``mlx_whisper`` package, config) are mocked;
no model download or audio hardware is touched.
"""

import struct
import sys
import types
import wave
from importlib.machinery import ModuleSpec
from unittest.mock import MagicMock, patch

import pytest

import tools.transcription_tools as tt


@pytest.fixture
def sample_wav(tmp_path):
    """A minimal valid WAV file (0.1s of silence at 16 kHz)."""
    wav_path = tmp_path / "voice.wav"
    n_frames = 1600
    silence = struct.pack(f"<{n_frames}h", *([0] * n_frames))
    with wave.open(str(wav_path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(16000)
        wf.writeframes(silence)
    return str(wav_path)


@pytest.fixture
def fake_mlx_whisper():
    """Install a stub ``mlx_whisper`` module and yield its transcribe mock."""
    stub = types.ModuleType("mlx_whisper")
    stub.__spec__ = ModuleSpec("mlx_whisper", loader=None)
    transcribe = MagicMock(
        name="transcribe",
        return_value={"text": "  привет мир  ", "language": "ru"},
    )
    stub.transcribe = transcribe
    sys.modules["mlx_whisper"] = stub
    try:
        with patch.object(tt, "_HAS_MLX_WHISPER", True):
            yield transcribe
    finally:
        sys.modules.pop("mlx_whisper", None)


# ---------------------------------------------------------------------------
# _normalize_stt_language
# ---------------------------------------------------------------------------

class TestNormalizeLanguage:
    @pytest.mark.parametrize("value", [None, "", "auto", "AUTO", " any ", "detect"])
    def test_auto_variants_map_to_none(self, value):
        assert tt._normalize_stt_language(value) is None

    @pytest.mark.parametrize(
        "value,expected", [("ru", "ru"), ("RU", "ru"), (" en ", "en")]
    )
    def test_explicit_language_normalised(self, value, expected):
        assert tt._normalize_stt_language(value) == expected


# ---------------------------------------------------------------------------
# _get_provider — mlx is always routed to its handler
# ---------------------------------------------------------------------------

class TestMlxProviderSelection:
    def test_explicit_mlx_selected_when_installed(self):
        with patch.object(tt, "_HAS_MLX_WHISPER", True):
            assert tt._get_provider({"provider": "mlx"}) == "mlx"

    def test_explicit_mlx_still_routed_when_missing(self):
        """Even without the package, route to mlx so the specific install
        error surfaces instead of the generic 'no provider' message."""
        with patch.object(tt, "_HAS_MLX_WHISPER", False):
            assert tt._get_provider({"provider": "mlx"}) == "mlx"


# ---------------------------------------------------------------------------
# _transcribe_mlx
# ---------------------------------------------------------------------------

class TestTranscribeMlx:
    def test_not_installed_returns_clear_error(self, sample_wav):
        with patch.object(tt, "_HAS_MLX_WHISPER", False):
            result = tt._transcribe_mlx(sample_wav, "mlx-community/whisper-large-v3-turbo")
        assert result["success"] is False
        assert "mlx-whisper is not installed" in result["error"]
        assert result["transcript"] == ""

    def test_success_strips_and_reports_provider(self, sample_wav, fake_mlx_whisper):
        result = tt._transcribe_mlx(sample_wav, "mlx-community/whisper-large-v3-turbo")
        assert result == {
            "success": True,
            "transcript": "привет мир",
            "provider": "mlx",
        }

    def test_auto_language_omits_language_kwarg(self, sample_wav, fake_mlx_whisper):
        tt._transcribe_mlx(sample_wav, "some/model", language="auto")
        _, kwargs = fake_mlx_whisper.call_args
        assert "language" not in kwargs
        assert kwargs["path_or_hf_repo"] == "some/model"

    def test_explicit_language_passed_through(self, sample_wav, fake_mlx_whisper):
        tt._transcribe_mlx(sample_wav, "some/model", language="ru")
        _, kwargs = fake_mlx_whisper.call_args
        assert kwargs["language"] == "ru"

    def test_transcribe_exception_wrapped(self, sample_wav, fake_mlx_whisper):
        fake_mlx_whisper.side_effect = RuntimeError("boom")
        result = tt._transcribe_mlx(sample_wav, "some/model")
        assert result["success"] is False
        assert "boom" in result["error"]


# ---------------------------------------------------------------------------
# transcribe_audio — forced provider + language override
# ---------------------------------------------------------------------------

class TestTranscribeAudioForcedMlx:
    def test_forced_provider_overrides_config(self, sample_wav, fake_mlx_whisper):
        # Config says "local", but the caller forces mlx.
        with patch.object(
            tt, "_load_stt_config", return_value={"enabled": True, "provider": "local"}
        ):
            result = tt.transcribe_audio(sample_wav, provider="mlx", language="ru")
        assert result["success"] is True
        assert result["provider"] == "mlx"
        _, kwargs = fake_mlx_whisper.call_args
        assert kwargs["path_or_hf_repo"] == "mlx-community/whisper-large-v3-turbo"
        assert kwargs["language"] == "ru"

    def test_forced_provider_uses_configured_model(self, sample_wav, fake_mlx_whisper):
        with patch.object(
            tt,
            "_load_stt_config",
            return_value={
                "enabled": True,
                "provider": "local",
                "mlx": {"model": "mlx-community/whisper-tiny"},
            },
        ):
            tt.transcribe_audio(sample_wav, provider="mlx", language="auto")
        _, kwargs = fake_mlx_whisper.call_args
        assert kwargs["path_or_hf_repo"] == "mlx-community/whisper-tiny"
        assert "language" not in kwargs

    def test_forced_mlx_not_installed_surfaces_install_error(self, sample_wav):
        with patch.object(tt, "_HAS_MLX_WHISPER", False), patch.object(
            tt, "_load_stt_config", return_value={"enabled": True, "provider": "local"}
        ):
            result = tt.transcribe_audio(sample_wav, provider="mlx")
        assert result["success"] is False
        assert "mlx-whisper is not installed" in result["error"]
