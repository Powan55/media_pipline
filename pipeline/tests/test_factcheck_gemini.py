"""Unit tests for `tools.factcheck_gemini`.

Uses stdlib `unittest` (matches test_tts_elevenlabs.py) so the suite is
runnable via either `python -m unittest tests.test_factcheck_gemini -v` or
`pytest tests/test_factcheck_gemini.py`.

ALL tests mock the `google.genai` SDK — no real network or API I/O.
Per the LLM API policy, the SDK is intentionally NOT installed during test
runs (it ships in requirements.txt for activation, but tests must work
without it). The lazy import inside the verification functions is verified
explicitly (`test_module_imports_without_sdk_installed` and
`test_missing_sdk_raises_factcheck_error`).

SDK migration (2026-05-08): the legacy `google-generativeai` package is
EOL on 2025-11-30; tests now mock the unified `google.genai` SDK shape:
`from google import genai; client = genai.Client(api_key=...);
resp = client.models.generate_content(model=..., contents=...)`.

`requests.head` (used by the URL realness check inside `verify_claim`) is
also patched throughout — by default to a 200 response so existing tests
do not exercise the network.

Test count: 19 tests total (16 existing + 3 new URL-realness tests).
"""

from __future__ import annotations

import asyncio
import builtins
import importlib
import json
import os
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest import mock

# Make the repo root importable so `from tools.factcheck_gemini import ...`
# works regardless of how the test runner discovers this file.
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools import factcheck_gemini  # noqa: E402
from tools.factcheck_gemini import (  # noqa: E402
    DEFAULT_MODEL,
    GeminiFactcheckError,
    _apply_url_realness_check,
    _escape_md_cell,
    _strip_code_fence,
    extract_claims,
    format_as_markdown_table,
    run_factcheck,
    verify_all_claims,
    verify_claim,
)


# ---------------------------------------------------------------------------
# Fake SDK shims — installed into sys.modules so the lazy import inside
# the function bodies resolves to mocks instead of the real package (which is
# not installed in the test env).
#
# The new google-genai SDK shape:
#     from google import genai
#     client = genai.Client(api_key=...)
#     resp = client.models.generate_content(model=..., contents=...)
#
# We track init args on a class-level list and route generate_content calls
# through a shared MagicMock so tests can assert on call counts / model ids
# / prompts without coupling to the SDK exception hierarchy.
# ---------------------------------------------------------------------------


class _FakeModelsNamespace:
    """Stand-in for `client.models`.

    Forwards `generate_content(model=..., contents=...)` to a class-level
    MagicMock so tests can configure return values or side effects.
    """

    generate_mock: mock.MagicMock = mock.MagicMock()

    def generate_content(self, *args: object, **kwargs: object) -> object:
        return type(self).generate_mock(*args, **kwargs)


class _FakeClient:
    """Stand-in for `genai.Client(api_key=...)`.

    Records init kwargs on a class-level list. Exposes a `.models` namespace
    that forwards `generate_content` to the shared MagicMock.
    """

    init_calls: list[tuple[tuple, dict]] = []

    def __init__(self, *args: object, **kwargs: object) -> None:
        type(self).init_calls.append((args, kwargs))
        self.models = _FakeModelsNamespace()


class _FakeResourceExhausted(Exception):
    """Stand-in for google-api-core ResourceExhausted (HTTP 429 retryable).

    Class-name match is enough for `_is_retryable` to flag it.
    """

    def __init__(self, msg: str = "rate limit") -> None:
        super().__init__(msg)
        self.code = 429


class _FakeBlockedPromptException(Exception):
    """Stand-in for google.genai content-policy block exception.

    Class-name "BlockedPromptException" is what `_is_content_policy_refusal`
    looks for to flag a content-policy refusal.
    """


def _install_fake_genai() -> mock.MagicMock:
    """Install a fake `google.genai` package in sys.modules.

    Returns the `generate_content` MagicMock so the test can configure
    return values or side effects. Caller is responsible for cleanup.
    """
    fake_google = types.ModuleType("google")
    fake_genai = types.ModuleType("google.genai")

    generate_mock = mock.MagicMock()
    _FakeModelsNamespace.generate_mock = generate_mock
    _FakeClient.init_calls = []

    fake_genai.Client = _FakeClient  # type: ignore[attr-defined]
    fake_google.genai = fake_genai  # type: ignore[attr-defined]

    sys.modules["google"] = fake_google
    sys.modules["google.genai"] = fake_genai
    return generate_mock


def _purge_genai_modules() -> dict[str, object]:
    """Remove any cached `google.genai*` (and legacy `google.generativeai*`) entries.

    Returns the dict of removed entries so the caller can restore them,
    keeping cross-test state hygiene tight. We do NOT purge top-level `google`
    by default since other unrelated google.* packages may live alongside.
    """
    purged: dict[str, object] = {}
    for k in list(sys.modules):
        if (
            k == "google.genai"
            or k.startswith("google.genai.")
            or k == "google.generativeai"
            or k.startswith("google.generativeai.")
        ):
            purged[k] = sys.modules.pop(k)
    # Also drop the top-level "google" if WE installed a fake (no real submodules left).
    if "google" in sys.modules:
        mod = sys.modules["google"]
        if getattr(mod, "__file__", None) is None and not any(
            k.startswith("google.") for k in sys.modules
        ):
            purged["google"] = sys.modules.pop("google")
    return purged


def _make_response(text: str) -> mock.MagicMock:
    """Create a mock Gemini response object with a `.text` attribute."""
    resp = mock.MagicMock()
    resp.text = text
    return resp


# ---------------------------------------------------------------------------
# Test base — handles env var + sys.modules cleanup so individual tests stay
# tight and don't leak fake SDK shims into each other.
# ---------------------------------------------------------------------------


class _GeminiTestCase(unittest.TestCase):
    """Common setUp/tearDown for factcheck_gemini tests.

    Auto-patches `requests.head` to return a 200-OK so the URL realness
    check inside `verify_claim` is a no-op for tests that don't specifically
    care about it. URL-realness tests below override the patch.
    """

    tmp_path: Path

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory(prefix="factcheck_gemini_test_")
        self.tmp_path = Path(self._tmpdir.name)
        self._env_snapshot = {"GEMINI_API_KEY": os.environ.pop("GEMINI_API_KEY", None)}
        self._purged_before = _purge_genai_modules()

        # Default URL-check stub: 200 response so verify_claim's HEAD check
        # is a no-op for all tests that don't override. We patch the bound
        # module object (not the dotted-string path) so the patch survives
        # any module-reload that LazyImportTests performs upstream.
        self._head_patcher = mock.patch.object(
            factcheck_gemini, "_verify_url_real",
            return_value=(True, ""),
        )
        self._head_patcher.start()

    def tearDown(self) -> None:
        self._head_patcher.stop()
        for k, v in self._env_snapshot.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        _purge_genai_modules()
        for k, v in self._purged_before.items():
            sys.modules[k] = v
        self._tmpdir.cleanup()

    def _set_api_key(self, value: str = "fake-key") -> None:
        os.environ["GEMINI_API_KEY"] = value


# ---------------------------------------------------------------------------
# Module-level constants and import behavior
# ---------------------------------------------------------------------------


class ConstantsTests(unittest.TestCase):
    def test_default_model_is_flash_lite(self) -> None:
        # Free-tier Flash-Lite is the operator-approved model.
        self.assertIn("flash-lite", DEFAULT_MODEL)

    def test_module_docstring_lists_activation_steps(self) -> None:
        doc = factcheck_gemini.__doc__ or ""
        self.assertIn("GEMINI_API_KEY", doc)
        # Migrated to google-genai SDK 2026-05-08 (replaces EOL google-generativeai).
        self.assertIn("google-genai", doc)
        self.assertIn("config.fact_check.gemini_enabled", doc)
        self.assertIn("free tier", doc.lower())


class LazyImportTests(unittest.TestCase):
    def test_module_imports_without_sdk_installed(self) -> None:
        """`import tools.factcheck_gemini` must succeed when
        google-genai is not installed."""
        real_import = builtins.__import__

        def fake_import(name: str, *args: object, **kwargs: object) -> object:
            # Block both the new and legacy package names — fromlist-style
            # `from google import genai` resolves through `google` itself,
            # so we also block when fromlist contains 'genai'.
            if name == "google.genai" or name.startswith("google.genai."):
                raise ImportError(f"No module named {name!r}")
            if name == "google":
                fromlist = args[2] if len(args) >= 3 else kwargs.get("fromlist")
                if fromlist and "genai" in fromlist:
                    raise ImportError("No module named 'google.genai'")
            return real_import(name, *args, **kwargs)

        purged = _purge_genai_modules()
        sys.modules.pop("tools.factcheck_gemini", None)

        try:
            with mock.patch.object(builtins, "__import__", side_effect=fake_import):
                mod = importlib.import_module("tools.factcheck_gemini")
                self.assertTrue(hasattr(mod, "extract_claims"))
                self.assertTrue(hasattr(mod, "verify_claim"))
                self.assertTrue(hasattr(mod, "verify_all_claims"))
                self.assertTrue(hasattr(mod, "format_as_markdown_table"))
                self.assertTrue(hasattr(mod, "run_factcheck"))
        finally:
            sys.modules.pop("tools.factcheck_gemini", None)
            for k, v in purged.items():
                sys.modules[k] = v
            importlib.import_module("tools.factcheck_gemini")


# ---------------------------------------------------------------------------
# Auth / SDK presence error paths
# ---------------------------------------------------------------------------


class AuthErrorTests(_GeminiTestCase):
    def test_missing_api_key_raises_factcheck_error(self) -> None:
        with self.assertRaises(GeminiFactcheckError) as cm:
            extract_claims("hello world")
        self.assertIn("API key", str(cm.exception))

    def test_missing_sdk_raises_factcheck_error(self) -> None:
        """When the lazy `from google import genai` fails, we surface
        GeminiFactcheckError (not raw ImportError)."""
        self._set_api_key()
        _purge_genai_modules()

        real_import = builtins.__import__

        def fake_import(name: str, *args: object, **kwargs: object) -> object:
            if name == "google.genai" or name.startswith("google.genai."):
                raise ImportError(f"No module named {name!r}")
            if name == "google":
                fromlist = args[2] if len(args) >= 3 else kwargs.get("fromlist")
                if fromlist and "genai" in fromlist:
                    raise ImportError("No module named 'google.genai'")
            return real_import(name, *args, **kwargs)

        with mock.patch.object(builtins, "__import__", side_effect=fake_import):
            with self.assertRaises(GeminiFactcheckError) as cm:
                extract_claims("hello world")

        msg = str(cm.exception)
        self.assertIn("pip install google-genai", msg)
        self.assertIsInstance(cm.exception.__cause__, ImportError)

    def test_empty_text_raises_factcheck_error(self) -> None:
        self._set_api_key()
        with self.assertRaises(GeminiFactcheckError):
            extract_claims("   ")


# ---------------------------------------------------------------------------
# extract_claims — happy path + parse failure modes
# ---------------------------------------------------------------------------


class ExtractClaimsTests(_GeminiTestCase):
    def test_extract_claims_happy_path(self) -> None:
        self._set_api_key()
        gen_mock = _install_fake_genai()
        gen_mock.return_value = _make_response(
            json.dumps([
                "ChatGPT was released in November 2022",
                "GPT-4 has 1.76 trillion parameters",
            ])
        )

        claims = extract_claims("Some script body about ChatGPT and GPT-4.")

        self.assertEqual(len(claims), 2)
        self.assertIn("ChatGPT", claims[0])
        self.assertIn("GPT-4", claims[1])
        gen_mock.assert_called_once()
        # Model id forwarded to client.models.generate_content as a kwarg.
        _, call_kwargs = gen_mock.call_args
        self.assertEqual(call_kwargs.get("model"), DEFAULT_MODEL)
        # Client was constructed with api_key kwarg.
        self.assertTrue(_FakeClient.init_calls)
        _, init_kwargs = _FakeClient.init_calls[-1]
        self.assertEqual(init_kwargs.get("api_key"), "fake-key")

    def test_extract_claims_strips_code_fence(self) -> None:
        """Gemini sometimes wraps JSON in ```json``` despite our prompt."""
        self._set_api_key()
        gen_mock = _install_fake_genai()
        gen_mock.return_value = _make_response(
            '```json\n["claim one", "claim two"]\n```'
        )
        claims = extract_claims("script body")
        self.assertEqual(claims, ["claim one", "claim two"])

    def test_extract_claims_malformed_json_raises(self) -> None:
        self._set_api_key()
        gen_mock = _install_fake_genai()
        gen_mock.return_value = _make_response("this is not json at all")

        with self.assertRaises(GeminiFactcheckError) as cm:
            extract_claims("script body")
        self.assertIn("malformed JSON", str(cm.exception))

    def test_extract_claims_non_list_json_raises(self) -> None:
        self._set_api_key()
        gen_mock = _install_fake_genai()
        gen_mock.return_value = _make_response('{"not": "a list"}')

        with self.assertRaises(GeminiFactcheckError) as cm:
            extract_claims("script body")
        self.assertIn("expected JSON array", str(cm.exception))

    def test_extract_claims_non_string_elements_raises(self) -> None:
        self._set_api_key()
        gen_mock = _install_fake_genai()
        gen_mock.return_value = _make_response('["valid", 42, "also valid"]')

        with self.assertRaises(GeminiFactcheckError) as cm:
            extract_claims("script body")
        self.assertIn("only strings", str(cm.exception))


# ---------------------------------------------------------------------------
# verify_claim — happy path + content-policy refusal
# ---------------------------------------------------------------------------


class VerifyClaimTests(_GeminiTestCase):
    def test_verify_claim_happy_path(self) -> None:
        self._set_api_key()
        gen_mock = _install_fake_genai()
        gen_mock.return_value = _make_response(json.dumps({
            "status": "VERIFIED",
            "url": "https://example.com/source",
            "quote": "ChatGPT launched on November 30, 2022.",
            "fix": "",
        }))

        result = verify_claim("ChatGPT was released in November 2022")

        self.assertEqual(result["status"], "VERIFIED")
        self.assertEqual(result["url"], "https://example.com/source")
        self.assertIn("November 30", result["quote"])
        self.assertEqual(result["fix"], "")
        # Schema: exactly the four keys, all string values.
        self.assertEqual(set(result.keys()), {"status", "url", "quote", "fix"})
        for v in result.values():
            self.assertIsInstance(v, str)

    def test_verify_claim_likely_wrong_with_fix(self) -> None:
        self._set_api_key()
        gen_mock = _install_fake_genai()
        gen_mock.return_value = _make_response(json.dumps({
            "status": "LIKELY_WRONG",
            "url": "https://example.com/correction",
            "quote": "GPT-4 parameter count is undisclosed.",
            "fix": 'Replace "1.76 trillion parameters" with "an undisclosed parameter count"',
        }))

        result = verify_claim("GPT-4 has 1.76 trillion parameters")

        self.assertEqual(result["status"], "LIKELY_WRONG")
        self.assertIn("Replace", result["fix"])

    def test_verify_claim_normalizes_likely_wrong_with_space(self) -> None:
        """Status `LIKELY WRONG` (with space) normalizes to `LIKELY_WRONG`."""
        self._set_api_key()
        gen_mock = _install_fake_genai()
        gen_mock.return_value = _make_response(json.dumps({
            "status": "likely wrong",
            "url": "",
            "quote": "",
            "fix": "Replace x with y",
        }))
        result = verify_claim("some claim")
        self.assertEqual(result["status"], "LIKELY_WRONG")

    def test_verify_claim_unknown_status_coerces_to_unclear(self) -> None:
        self._set_api_key()
        gen_mock = _install_fake_genai()
        gen_mock.return_value = _make_response(json.dumps({
            "status": "WIBBLE",
            "url": "",
            "quote": "",
            "fix": "",
        }))
        result = verify_claim("some claim")
        self.assertEqual(result["status"], "UNCLEAR")

    def test_verify_claim_content_policy_refusal_returns_unclear(self) -> None:
        """Content-policy refusal → UNCLEAR with note in `quote`, NOT raise."""
        self._set_api_key()
        gen_mock = _install_fake_genai()
        gen_mock.side_effect = _FakeBlockedPromptException(
            "Prompt blocked due to safety concerns"
        )

        result = verify_claim("some controversial claim", max_retries=0)

        self.assertEqual(result["status"], "UNCLEAR")
        self.assertIn("Content-policy", result["quote"])
        self.assertEqual(result["url"], "")
        self.assertEqual(result["fix"], "")

    def test_verify_claim_malformed_json_raises(self) -> None:
        self._set_api_key()
        gen_mock = _install_fake_genai()
        gen_mock.return_value = _make_response("not json at all {{{")

        with self.assertRaises(GeminiFactcheckError) as cm:
            verify_claim("some claim")
        self.assertIn("malformed JSON", str(cm.exception))


# ---------------------------------------------------------------------------
# URL realness check (NEW — 2026-05-08, fixes Gemini hallucinated-URL bug)
# ---------------------------------------------------------------------------


class UrlRealnessTests(_GeminiTestCase):
    """The Gemini smoke (Team A6 + B6, 2026-05-08) caught a fabricated
    URL emitted as a VERIFIED source. `_apply_url_realness_check` HEAD-checks
    every URL and strips fabricated/dead ones, downgrading VERIFIED to
    UNVERIFIABLE so a viewer is never sent to a 404."""

    def test_url_realness_real_url_kept(self) -> None:
        """200 response → URL kept, status preserved, no Fix note appended."""
        self._set_api_key()
        gen_mock = _install_fake_genai()
        gen_mock.return_value = _make_response(json.dumps({
            "status": "VERIFIED",
            "url": "https://openai.com/blog/chatgpt",
            "quote": "ChatGPT launched November 30, 2022.",
            "fix": "",
        }))
        # Override the default 200-OK stub explicitly to make intent clear.
        with mock.patch.object(
            factcheck_gemini, "_verify_url_real",
            return_value=(True, ""),
        ):
            result = verify_claim("ChatGPT was released in November 2022")

        self.assertEqual(result["status"], "VERIFIED")
        self.assertEqual(result["url"], "https://openai.com/blog/chatgpt")
        self.assertEqual(result["fix"], "")

    def test_url_realness_404_strips_url_and_downgrades(self) -> None:
        """404 → URL stripped, VERIFIED → UNVERIFIABLE, Fix note appended."""
        self._set_api_key()
        gen_mock = _install_fake_genai()
        gen_mock.return_value = _make_response(json.dumps({
            "status": "VERIFIED",
            "url": "https://theinformation.com/articles/the-staggering-cost-of-training-chatgpt",
            "quote": "Training cost was $X million.",
            "fix": "",
        }))
        with mock.patch.object(
            factcheck_gemini, "_verify_url_real",
            return_value=(False, "HTTP 404"),
        ):
            result = verify_claim("ChatGPT cost X million to train")

        self.assertEqual(result["status"], "UNVERIFIABLE")
        self.assertEqual(result["url"], "")
        self.assertIn("Source URL failed verification", result["fix"])
        self.assertIn("HTTP 404", result["fix"])
        self.assertIn("retrievable source", result["fix"])

    def test_url_realness_timeout_strips_url_and_downgrades(self) -> None:
        """Timeout → URL stripped, VERIFIED → UNVERIFIABLE, Fix note appended."""
        self._set_api_key()
        gen_mock = _install_fake_genai()
        gen_mock.return_value = _make_response(json.dumps({
            "status": "VERIFIED",
            "url": "https://very-slow-server.example.com/article",
            "quote": "Some quote.",
            "fix": "",
        }))
        with mock.patch.object(
            factcheck_gemini, "_verify_url_real",
            return_value=(False, "timed out after 5s"),
        ):
            result = verify_claim("some claim")

        self.assertEqual(result["status"], "UNVERIFIABLE")
        self.assertEqual(result["url"], "")
        self.assertIn("Source URL failed verification", result["fix"])
        self.assertIn("timed out", result["fix"])

    def test_url_realness_likely_wrong_status_preserved_on_strip(self) -> None:
        """LIKELY_WRONG status preserved even when URL is stripped — the
        verifier flagged a mismatch, which doesn't depend on a live source."""
        self._set_api_key()
        result = _apply_url_realness_check({
            "status": "LIKELY_WRONG",
            "url": "https://fake.example.com/article",
            "quote": "Real source contradicts.",
            "fix": 'Replace "X" with "Y"',
        })
        # We need to actually call the real _verify_url_real for this dict,
        # but the test base patches it to (True, ""). Override:
        with mock.patch.object(
            factcheck_gemini, "_verify_url_real",
            return_value=(False, "HTTP 404"),
        ):
            result = _apply_url_realness_check({
                "status": "LIKELY_WRONG",
                "url": "https://fake.example.com/article",
                "quote": "Real source contradicts.",
                "fix": 'Replace "X" with "Y"',
            })
        self.assertEqual(result["status"], "LIKELY_WRONG")
        self.assertEqual(result["url"], "")
        # Existing fix preserved + new note appended.
        self.assertIn('Replace "X" with "Y"', result["fix"])
        self.assertIn("Source URL failed verification", result["fix"])

    def test_url_realness_empty_url_short_circuits(self) -> None:
        """Empty URL — no HEAD check, no status change."""
        with mock.patch.object(
            factcheck_gemini, "_verify_url_real",
        ) as head_mock:
            result = _apply_url_realness_check({
                "status": "UNVERIFIABLE",
                "url": "",
                "quote": "no source available",
                "fix": "",
            })
        head_mock.assert_not_called()
        self.assertEqual(result["status"], "UNVERIFIABLE")
        self.assertEqual(result["url"], "")
        self.assertEqual(result["fix"], "")


# ---------------------------------------------------------------------------
# Retry policy
# ---------------------------------------------------------------------------


class RetryPolicyTests(_GeminiTestCase):
    def test_rate_limit_retry_succeeds(self) -> None:
        """429 raises once, succeeds on retry. Exponential backoff sleeps applied."""
        self._set_api_key()
        gen_mock = _install_fake_genai()
        gen_mock.side_effect = [
            _FakeResourceExhausted("rate limit"),
            _make_response(json.dumps(["claim one"])),
        ]

        sleep_calls: list[float] = []
        with mock.patch.object(
            factcheck_gemini.time, "sleep", lambda s: sleep_calls.append(s),
        ):
            claims = extract_claims("script body", max_retries=3)

        self.assertEqual(claims, ["claim one"])
        self.assertEqual(gen_mock.call_count, 2)
        # 1 retry → 1 sleep at backoff_base_s * 2**0 = 2.0s.
        self.assertEqual(len(sleep_calls), 1)
        self.assertAlmostEqual(sleep_calls[0], 2.0)

    def test_rate_limit_retry_exhausts_then_raises(self) -> None:
        self._set_api_key()
        gen_mock = _install_fake_genai()
        gen_mock.side_effect = _FakeResourceExhausted("persistent rate limit")

        with mock.patch.object(factcheck_gemini.time, "sleep", lambda s: None):
            with self.assertRaises(GeminiFactcheckError) as cm:
                extract_claims("script body", max_retries=2)

        # max_retries=2 → 3 total attempts.
        self.assertEqual(gen_mock.call_count, 3)
        self.assertIsInstance(cm.exception.__cause__, _FakeResourceExhausted)


# ---------------------------------------------------------------------------
# verify_all_claims — parallel / semaphore behavior
# ---------------------------------------------------------------------------


class VerifyAllClaimsTests(_GeminiTestCase):
    def test_empty_claims_returns_empty(self) -> None:
        # No SDK calls, no API key needed for the empty-list short-circuit.
        # But _resolve_api_key fires before the empty check is reached if we
        # passed claims; however the implementation short-circuits before key
        # resolution when claims=[]. Confirm.
        self.assertEqual(verify_all_claims([]), [])

    def test_verify_all_claims_runs_in_parallel(self) -> None:
        """N claims → N verify dicts in same order. Semaphore caps concurrency.

        We assert ordering by giving each claim a distinct mock response and
        checking the returned verifications align by index.
        """
        self._set_api_key()
        gen_mock = _install_fake_genai()

        responses_by_claim = {
            "claim_a": json.dumps({"status": "VERIFIED", "url": "https://a.example.com", "quote": "q_a", "fix": ""}),
            "claim_b": json.dumps({"status": "LIKELY_WRONG", "url": "https://b.example.com", "quote": "q_b", "fix": "Replace x with y"}),
            "claim_c": json.dumps({"status": "UNCLEAR", "url": "", "quote": "q_c", "fix": ""}),
        }

        def side_effect(*args: object, **kwargs: object) -> object:
            # The new SDK shape passes prompt as the `contents` kwarg.
            prompt = kwargs.get("contents", "")
            if not isinstance(prompt, str):
                prompt = str(prompt)
            for claim_key, payload in responses_by_claim.items():
                if claim_key in prompt:
                    return _make_response(payload)
            return _make_response('{"status": "UNCLEAR", "url": "", "quote": "", "fix": ""}')

        gen_mock.side_effect = side_effect

        claims = ["claim_a", "claim_b", "claim_c"]
        results = verify_all_claims(claims, max_parallel=2)

        self.assertEqual(len(results), 3)
        self.assertEqual(results[0]["url"], "https://a.example.com")
        self.assertEqual(results[1]["url"], "https://b.example.com")
        self.assertEqual(results[1]["status"], "LIKELY_WRONG")
        self.assertEqual(results[2]["status"], "UNCLEAR")
        self.assertEqual(gen_mock.call_count, 3)

    def test_semaphore_caps_concurrency(self) -> None:
        """With max_parallel=2, no more than 2 in-flight at once."""
        self._set_api_key()
        gen_mock = _install_fake_genai()

        in_flight = 0
        max_in_flight = 0

        def side_effect(*args: object, **kwargs: object) -> object:
            nonlocal in_flight, max_in_flight
            in_flight += 1
            max_in_flight = max(max_in_flight, in_flight)
            time.sleep(0.05)  # let other coros pile up
            in_flight -= 1
            return _make_response(json.dumps({
                "status": "VERIFIED", "url": "", "quote": "", "fix": "",
            }))

        gen_mock.side_effect = side_effect

        claims = [f"claim_{i}" for i in range(6)]
        results = verify_all_claims(claims, max_parallel=2)

        self.assertEqual(len(results), 6)
        # Cap is 2; with 6 claims and a 50ms sleep, we should hit exactly 2.
        self.assertLessEqual(max_in_flight, 2)


# ---------------------------------------------------------------------------
# format_as_markdown_table — schema verification
# ---------------------------------------------------------------------------


class FormatTableTests(unittest.TestCase):
    def test_emits_exact_5_column_schema(self) -> None:
        claims = ["claim alpha", "claim beta"]
        verifications = [
            {"status": "VERIFIED", "url": "https://a.com", "quote": "qa", "fix": ""},
            {"status": "LIKELY_WRONG", "url": "https://b.com", "quote": "qb", "fix": "Replace x with y"},
        ]
        out = format_as_markdown_table(claims, verifications)
        lines = out.splitlines()

        # Header row: exactly 5 columns named Claim/Status/URL/Quote/Fix.
        self.assertEqual(lines[0], "| Claim | Status | URL | Quote | Fix |")
        self.assertEqual(lines[1], "|-------|--------|-----|-------|-----|")
        # Two data rows.
        self.assertEqual(len(lines), 4)
        # Each data row has exactly 6 pipes (5 columns + 1 leading + 1 trailing == 6).
        for line in lines[2:]:
            self.assertEqual(line.count("|"), 6, f"row had wrong pipe count: {line!r}")

    def test_escape_md_cell_handles_pipes_and_newlines(self) -> None:
        # Pipes inside cell text would break parsing — escape them.
        self.assertEqual(_escape_md_cell("a | b"), "a \\| b")
        # Newlines collapse to spaces.
        self.assertEqual(_escape_md_cell("line one\nline two"), "line one line two")

    def test_length_mismatch_raises(self) -> None:
        with self.assertRaises(ValueError):
            format_as_markdown_table(["one"], [])

    def test_strip_code_fence_removes_json_wrapper(self) -> None:
        """Helper used in extract_claims/verify_claim parsing path."""
        self.assertEqual(_strip_code_fence("```json\n[1,2,3]\n```"), "[1,2,3]")
        self.assertEqual(_strip_code_fence("```\n[1,2,3]\n```"), "[1,2,3]")
        self.assertEqual(_strip_code_fence("[1,2,3]"), "[1,2,3]")


# ---------------------------------------------------------------------------
# run_factcheck end-to-end + dry-run
# ---------------------------------------------------------------------------


class RunFactcheckTests(_GeminiTestCase):
    def test_run_factcheck_writes_table_to_out_path(self) -> None:
        self._set_api_key()
        gen_mock = _install_fake_genai()
        responses = [
            _make_response(json.dumps(["ChatGPT was released in November 2022"])),
            _make_response(json.dumps({
                "status": "VERIFIED",
                "url": "https://openai.com/blog/chatgpt",
                "quote": "Released November 30, 2022",
                "fix": "",
            })),
        ]
        gen_mock.side_effect = responses

        script_path = self.tmp_path / "script.txt"
        script_path.write_text("ChatGPT was released in November 2022", encoding="utf-8")
        out_path = self.tmp_path / "out.md"

        table = run_factcheck(script_path, out_path=out_path)

        self.assertTrue(out_path.exists())
        self.assertEqual(out_path.read_text(encoding="utf-8"), table)
        self.assertIn("| Claim | Status | URL | Quote | Fix |", table)
        self.assertIn("VERIFIED", table)
        self.assertIn("https://openai.com/blog/chatgpt", table)

    def test_dry_run_extracts_but_does_not_verify(self) -> None:
        """`--dry-run` calls extract_claims ONCE and skips verify_claim entirely."""
        self._set_api_key()
        gen_mock = _install_fake_genai()
        gen_mock.return_value = _make_response(json.dumps([
            "claim one", "claim two", "claim three",
        ]))

        script_path = self.tmp_path / "script.txt"
        script_path.write_text("body with three claims", encoding="utf-8")

        table = run_factcheck(script_path, dry_run=True)

        # Exactly one SDK call (extract). Zero verify calls.
        self.assertEqual(gen_mock.call_count, 1)
        # All claims marked UNCLEAR with the dry-run note.
        self.assertEqual(table.count("UNCLEAR"), 3)
        self.assertIn("(dry run", table)

    def test_run_factcheck_missing_script_raises(self) -> None:
        with self.assertRaises(FileNotFoundError):
            run_factcheck(self.tmp_path / "does_not_exist.txt")


# ---------------------------------------------------------------------------
# CLI — argparse --help works without google-genai installed
# ---------------------------------------------------------------------------


class CLITests(unittest.TestCase):
    def test_help_works_without_sdk(self) -> None:
        """`python tools/factcheck_gemini.py --help` succeeds without the SDK."""
        import subprocess
        proc = subprocess.run(
            [sys.executable, str(REPO_ROOT / "tools" / "factcheck_gemini.py"), "--help"],
            capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(proc.returncode, 0, f"stderr: {proc.stderr!r}")
        self.assertIn("--script", proc.stdout)
        self.assertIn("--out", proc.stdout)
        self.assertIn("--model", proc.stdout)
        self.assertIn("--max-parallel", proc.stdout)
        self.assertIn("--dry-run", proc.stdout)


if __name__ == "__main__":
    unittest.main()
