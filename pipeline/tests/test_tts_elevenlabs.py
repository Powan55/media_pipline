"""Unit tests for `tools.tts_elevenlabs`.

Uses stdlib `unittest` (matches `test_media_integrity.py`) so the suite is
runnable via either `python -m unittest tests.test_tts_elevenlabs -v` or
`pytest tests/test_tts_elevenlabs.py`.

ALL tests mock the `elevenlabs` SDK and the ffmpeg subprocess — no real
network or audio I/O. Per the LLM API policy, this module is dormant until
the operator activates it; the SDK is intentionally NOT in
`requirements.txt`, so the test suite must be runnable WITHOUT
`elevenlabs` installed. The lazy import inside `synthesize()` is verified
explicitly (`test_module_imports_without_elevenlabs_installed` and
`test_missing_sdk_raises_elevenlabserror`).
"""

from __future__ import annotations

import builtins
import importlib
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

# Make the repo root importable so `from tools.tts_elevenlabs import ...`
# works regardless of how the test runner discovers this file.
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools import tts_elevenlabs  # noqa: E402
from tools.tts_elevenlabs import (  # noqa: E402
    DEFAULT_MODEL,
    DEFAULT_VOICE_ID,
    ElevenLabsError,
    synthesize,
)


# ---------------------------------------------------------------------------
# Fake SDK shims — installed into sys.modules so the lazy import inside
# `synthesize()` resolves to mocks instead of the real package (which is
# intentionally not in requirements.txt).
# ---------------------------------------------------------------------------


class _FakeTTS:
    """Stand-in for `client.text_to_speech` exposing a `convert` MagicMock."""

    def __init__(self, convert: mock.MagicMock) -> None:
        self.convert = convert


class _FakeClient:
    """Stand-in for `elevenlabs.client.ElevenLabs(api_key=...)`.

    Records the kwargs of the most recent constructor call on the class so
    tests can assert that `api_key=` was forwarded correctly.
    """

    last_init_kwargs: dict[str, object] = {}
    _convert_mock: mock.MagicMock = mock.MagicMock()

    def __init__(self, *args: object, **kwargs: object) -> None:
        type(self).last_init_kwargs = dict(kwargs)
        self.text_to_speech = _FakeTTS(_FakeClient._convert_mock)


class _FakeHTTPStatusError(Exception):
    """Stand-in for `httpx.HTTPStatusError` — has `.response.status_code`.

    The retryability classifier in `tts_elevenlabs._is_retryable` is duck-typed
    (looks at `exc.response.status_code`), so this minimal shim exercises the
    same branches without depending on httpx's exception class hierarchy.
    """

    def __init__(self, status_code: int) -> None:
        super().__init__(f"HTTP {status_code}")
        self.response = types.SimpleNamespace(status_code=status_code)


def _install_fake_elevenlabs() -> mock.MagicMock:
    """Install a fake `elevenlabs` package in sys.modules.

    Returns the `convert` MagicMock so the test can configure return values
    or side-effects. Caller is responsible for cleanup (use the
    `_TTSTestCase` base class, which does this in setUp/tearDown).
    """
    fake_pkg = types.ModuleType("elevenlabs")
    fake_client_mod = types.ModuleType("elevenlabs.client")

    convert_mock = mock.MagicMock()
    _FakeClient._convert_mock = convert_mock
    _FakeClient.last_init_kwargs = {}
    fake_client_mod.ElevenLabs = _FakeClient  # type: ignore[attr-defined]
    fake_pkg.client = fake_client_mod  # type: ignore[attr-defined]

    sys.modules["elevenlabs"] = fake_pkg
    sys.modules["elevenlabs.client"] = fake_client_mod
    return convert_mock


def _purge_elevenlabs_modules() -> dict[str, object]:
    """Remove any cached `elevenlabs*` entries from sys.modules.

    Returns the dict of removed entries so the caller can restore them after
    the test, keeping cross-test state hygiene tight.
    """
    purged: dict[str, object] = {}
    for k in list(sys.modules):
        if k == "elevenlabs" or k.startswith("elevenlabs."):
            purged[k] = sys.modules.pop(k)
    return purged


# ---------------------------------------------------------------------------
# Test base — handles env var + sys.modules cleanup so individual tests stay
# tight and don't leak fake SDK shims into each other.
# ---------------------------------------------------------------------------


class _TTSTestCase(unittest.TestCase):
    """Common setUp/tearDown for tts_elevenlabs tests.

    - Snapshots and restores `ELEVENLABS_API_KEY` env var.
    - Removes any cached `elevenlabs*` entries from sys.modules before and
      after each test so fake-SDK injection is hermetic.
    - Provides a tmp_path per test (mirrors pytest's `tmp_path` fixture).
    """

    tmp_path: Path

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory(prefix="tts_elevenlabs_test_")
        self.tmp_path = Path(self._tmpdir.name)
        self._env_snapshot = {"ELEVENLABS_API_KEY": __import__("os").environ.pop(
            "ELEVENLABS_API_KEY", None
        )}
        self._purged_before = _purge_elevenlabs_modules()

    def tearDown(self) -> None:
        # Restore env.
        import os
        for k, v in self._env_snapshot.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        # Drop fakes added by the test, restore anything we purged in setUp.
        _purge_elevenlabs_modules()
        for k, v in self._purged_before.items():
            sys.modules[k] = v
        self._tmpdir.cleanup()

    def _set_api_key(self, value: str = "fake-key") -> None:
        import os
        os.environ["ELEVENLABS_API_KEY"] = value


# ---------------------------------------------------------------------------
# Module-level constants and import behavior
# ---------------------------------------------------------------------------


class ConstantsTests(unittest.TestCase):
    def test_default_voice_is_brian(self) -> None:
        self.assertEqual(DEFAULT_VOICE_ID, "nPczCjzI2devNBz1zQrb")

    def test_default_model_is_v2_not_v3(self) -> None:
        # v3 is Creator-tier per R2; this sprint targets Starter ($5/mo).
        self.assertEqual(DEFAULT_MODEL, "eleven_multilingual_v2")
        self.assertNotIn("v3", DEFAULT_MODEL)

    def test_module_docstring_lists_activation_steps(self) -> None:
        """Spec AC8: module docstring at top must include the 5-step
        operator-activation list verbatim."""
        doc = tts_elevenlabs.__doc__ or ""
        self.assertIn("Subscribe to ElevenLabs", doc)
        self.assertIn("ELEVENLABS_API_KEY", doc)
        self.assertIn("requirements.txt", doc)
        self.assertIn("config.tts.provider: elevenlabs", doc)


class LazyImportTests(unittest.TestCase):
    def test_module_imports_without_elevenlabs_installed(self) -> None:
        """Spec AC1: `import tools.tts_elevenlabs` must succeed when the
        `elevenlabs` package is not installed.

        Simulates absence by patching `builtins.__import__` to raise
        ImportError for any `elevenlabs*` import, dropping any cached SDK
        modules from `sys.modules`, then re-importing
        `tools.tts_elevenlabs` from scratch.
        """
        real_import = builtins.__import__

        def fake_import(name: str, *args: object, **kwargs: object) -> object:
            if name == "elevenlabs" or name.startswith("elevenlabs."):
                raise ImportError(f"No module named {name!r}")
            return real_import(name, *args, **kwargs)

        purged = _purge_elevenlabs_modules()
        sys.modules.pop("tools.tts_elevenlabs", None)

        try:
            with mock.patch.object(builtins, "__import__", side_effect=fake_import):
                mod = importlib.import_module("tools.tts_elevenlabs")
                self.assertTrue(hasattr(mod, "synthesize"))
                self.assertEqual(mod.DEFAULT_VOICE_ID, "nPczCjzI2devNBz1zQrb")
        finally:
            # Re-import the real module so other tests get a clean copy.
            sys.modules.pop("tools.tts_elevenlabs", None)
            for k, v in purged.items():
                sys.modules[k] = v
            importlib.import_module("tools.tts_elevenlabs")


# ---------------------------------------------------------------------------
# Auth / SDK presence error paths
# ---------------------------------------------------------------------------


class AuthErrorTests(_TTSTestCase):
    def test_missing_api_key_raises_elevenlabserror(self) -> None:
        # ELEVENLABS_API_KEY removed in setUp.
        with self.assertRaises(ElevenLabsError) as cm:
            synthesize("hello world", self.tmp_path / "out.wav")
        self.assertIn("API key", str(cm.exception))

    def test_missing_sdk_raises_elevenlabserror(self) -> None:
        """When the lazy `from elevenlabs.client import ElevenLabs` fails,
        we surface ElevenLabsError (not raw ImportError)."""
        self._set_api_key()
        # Ensure no fake SDK is sitting in sys.modules from another test.
        _purge_elevenlabs_modules()

        real_import = builtins.__import__

        def fake_import(name: str, *args: object, **kwargs: object) -> object:
            if name == "elevenlabs" or name.startswith("elevenlabs."):
                raise ImportError(f"No module named {name!r}")
            return real_import(name, *args, **kwargs)

        with mock.patch.object(builtins, "__import__", side_effect=fake_import):
            with self.assertRaises(ElevenLabsError) as cm:
                synthesize("hello world", self.tmp_path / "out.wav")

        msg = str(cm.exception)
        self.assertIn("pip install elevenlabs", msg)
        self.assertIsInstance(cm.exception.__cause__, ImportError)

    def test_empty_text_raises_elevenlabserror(self) -> None:
        self._set_api_key()
        with self.assertRaises(ElevenLabsError):
            synthesize("   ", self.tmp_path / "out.wav")


# ---------------------------------------------------------------------------
# Happy path — SDK is mocked, ffmpeg decode is mocked
# ---------------------------------------------------------------------------


class HappyPathTests(_TTSTestCase):
    def test_synthesize_calls_sdk_and_writes_wav(self) -> None:
        self._set_api_key()
        convert_mock = _install_fake_elevenlabs()
        convert_mock.return_value = b"\xff\xfb\x90\x00FAKE_MP3_PAYLOAD"

        out_path = self.tmp_path / "vo.wav"

        def fake_decode(mp3_path: Path, wav_path: Path, sample_rate_hz: int) -> None:
            self.assertEqual(sample_rate_hz, 48000)
            wav_path.write_bytes(b"RIFF\x00\x00\x00\x00WAVEfake")

        with mock.patch.object(tts_elevenlabs, "_decode_mp3_to_wav", fake_decode):
            result = synthesize("hello world", out_path)

        self.assertEqual(result, out_path)
        self.assertEqual(result.suffix, ".wav")
        self.assertTrue(result.exists())
        # MP3 cached alongside per the contract.
        mp3 = self.tmp_path / "vo.mp3"
        self.assertTrue(mp3.exists())
        self.assertTrue(mp3.read_bytes().startswith(b"\xff\xfb"))

        # SDK called exactly once with our params.
        convert_mock.assert_called_once()
        kwargs = convert_mock.call_args.kwargs
        self.assertEqual(kwargs["voice_id"], DEFAULT_VOICE_ID)
        self.assertEqual(kwargs["model_id"], DEFAULT_MODEL)
        self.assertEqual(kwargs["text"], "hello world")
        self.assertAlmostEqual(kwargs["voice_settings"]["stability"], 0.45)
        self.assertAlmostEqual(kwargs["voice_settings"]["speed"], 1.05)
        self.assertEqual(_FakeClient.last_init_kwargs.get("api_key"), "fake-key")

    def test_synthesize_handles_iterator_audio(self) -> None:
        """SDK may return an iterator of bytes chunks; ensure we coalesce them."""
        self._set_api_key()
        convert_mock = _install_fake_elevenlabs()
        convert_mock.return_value = iter([b"\xff\xfb", b"chunk1", b"chunk2"])

        with mock.patch.object(
            tts_elevenlabs, "_decode_mp3_to_wav",
            lambda mp3, wav, sr: wav.write_bytes(b"WAV"),
        ):
            result = synthesize("hello", self.tmp_path / "vo.wav")

        self.assertTrue(result.exists())
        self.assertEqual(
            (self.tmp_path / "vo.mp3").read_bytes(),
            b"\xff\xfbchunk1chunk2",
        )

    def test_mp3_only_skips_wav_decode(self) -> None:
        self._set_api_key()
        convert_mock = _install_fake_elevenlabs()
        convert_mock.return_value = b"\xff\xfbMP3ONLY"

        decode_mock = mock.MagicMock()
        with mock.patch.object(tts_elevenlabs, "_decode_mp3_to_wav", decode_mock):
            result = synthesize(
                "hello", self.tmp_path / "vo.wav", output_format="mp3_only",
            )

        self.assertEqual(result.suffix, ".mp3")
        self.assertTrue(result.exists())
        decode_mock.assert_not_called()


# ---------------------------------------------------------------------------
# Retry policy
# ---------------------------------------------------------------------------


class RetryPolicyTests(_TTSTestCase):
    def test_retry_on_429(self) -> None:
        """429 is retryable; sleeps follow `backoff_base_s * 2**attempt`."""
        self._set_api_key()
        convert_mock = _install_fake_elevenlabs()
        convert_mock.side_effect = [
            _FakeHTTPStatusError(429),
            _FakeHTTPStatusError(429),
            b"\xff\xfbOK",
        ]

        sleep_calls: list[float] = []
        with mock.patch.object(
            tts_elevenlabs.time, "sleep", lambda s: sleep_calls.append(s),
        ), mock.patch.object(
            tts_elevenlabs, "_decode_mp3_to_wav",
            lambda mp3, wav, sr: wav.write_bytes(b"WAV"),
        ):
            result = synthesize(
                "hello", self.tmp_path / "vo.wav",
                max_retries=3, backoff_base_s=1.5,
            )

        self.assertTrue(result.exists())
        self.assertEqual(convert_mock.call_count, 3)
        # 2 retries -> 2 sleeps; exponential: 1.5*2**0, 1.5*2**1.
        self.assertEqual(len(sleep_calls), 2)
        self.assertAlmostEqual(sleep_calls[0], 1.5)
        self.assertAlmostEqual(sleep_calls[1], 3.0)

    def test_retry_on_5xx(self) -> None:
        """5xx (server error) is retryable."""
        self._set_api_key()
        convert_mock = _install_fake_elevenlabs()
        convert_mock.side_effect = [
            _FakeHTTPStatusError(503),
            b"\xff\xfbOK",
        ]
        with mock.patch.object(tts_elevenlabs.time, "sleep", lambda s: None), \
             mock.patch.object(
                 tts_elevenlabs, "_decode_mp3_to_wav",
                 lambda mp3, wav, sr: wav.write_bytes(b"WAV"),
             ):
            result = synthesize(
                "hello", self.tmp_path / "vo.wav", max_retries=3,
            )

        self.assertTrue(result.exists())
        self.assertEqual(convert_mock.call_count, 2)

    def test_retry_exhausts_then_raises(self) -> None:
        """When every attempt fails with a retryable error, the final raise
        is `ElevenLabsError` with the underlying exception chained."""
        self._set_api_key()
        convert_mock = _install_fake_elevenlabs()
        convert_mock.side_effect = _FakeHTTPStatusError(500)

        with mock.patch.object(tts_elevenlabs.time, "sleep", lambda s: None):
            with self.assertRaises(ElevenLabsError) as cm:
                synthesize("hello", self.tmp_path / "vo.wav", max_retries=2)

        # max_retries=2 -> attempts 1, 2, 3 (initial + 2 retries) all fail.
        self.assertEqual(convert_mock.call_count, 3)
        self.assertIsInstance(cm.exception.__cause__, _FakeHTTPStatusError)

    def test_non_retryable_4xx_raises_immediately(self) -> None:
        """401 (auth) is not retryable — fail loud on first attempt."""
        self._set_api_key()
        convert_mock = _install_fake_elevenlabs()
        convert_mock.side_effect = _FakeHTTPStatusError(401)

        sleep_mock = mock.MagicMock()
        with mock.patch.object(tts_elevenlabs.time, "sleep", sleep_mock):
            with self.assertRaises(ElevenLabsError):
                synthesize("hello", self.tmp_path / "vo.wav", max_retries=3)

        self.assertEqual(convert_mock.call_count, 1)
        sleep_mock.assert_not_called()

    def test_non_retryable_403_raises_immediately(self) -> None:
        """403 (forbidden / quota) is not retryable either."""
        self._set_api_key()
        convert_mock = _install_fake_elevenlabs()
        convert_mock.side_effect = _FakeHTTPStatusError(403)

        sleep_mock = mock.MagicMock()
        with mock.patch.object(tts_elevenlabs.time, "sleep", sleep_mock):
            with self.assertRaises(ElevenLabsError):
                synthesize("hello", self.tmp_path / "vo.wav", max_retries=3)

        self.assertEqual(convert_mock.call_count, 1)
        sleep_mock.assert_not_called()


# ---------------------------------------------------------------------------
# CLI — argparse --help works without elevenlabs installed (spec AC1 corollary)
# ---------------------------------------------------------------------------


class CLITests(unittest.TestCase):
    def test_help_works_without_elevenlabs(self) -> None:
        """`python tools/tts_elevenlabs.py --help` must succeed even when
        elevenlabs is not installed (the SDK is lazy-imported, so --help
        never reaches the lazy import)."""
        import subprocess
        proc = subprocess.run(
            [sys.executable, str(REPO_ROOT / "tools" / "tts_elevenlabs.py"), "--help"],
            capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(proc.returncode, 0, f"stderr: {proc.stderr!r}")
        self.assertIn("--text", proc.stdout)
        self.assertIn("--out", proc.stdout)
        self.assertIn("--voice-id", proc.stdout)
        self.assertIn("--no-wav", proc.stdout)


if __name__ == "__main__":
    unittest.main()
