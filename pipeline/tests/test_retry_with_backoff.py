"""Tests for `pipeline._retry_with_backoff` (R1, WORKFLOW_AUDIT_2026-05-31).

The retry primitive is plain Python (for / try / except + time.sleep backoff),
NO tenacity/backoff. Coverage:
- a transient failure that recovers within the attempt budget returns the value
  and sleeps between (but not after the last) attempts;
- exhausting the budget re-raises the LAST exception (so a caller's own handler,
  e.g. the search functions' `except: return None`, still runs);
- an exception NOT in `retry_on` propagates immediately with no retry/sleep;
- the sleep schedule is exponential (base, 2*base, 4*base …);
- the two stock-search callers keep their None-on-final-failure contract once
  the wrapper has exhausted its retries (the provider-fallback guarantee M1 relies on).

`time.sleep` is patched out so the tests stay fast and deterministic. No network.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import requests  # noqa: E402

import pipeline  # noqa: E402


class TestRetryWithBackoff(unittest.TestCase):
    def test_retries_then_succeeds(self) -> None:
        """Fail twice with a retryable error, then succeed → returns the value,
        calls fn 3 times, sleeps twice (no sleep after the final attempt)."""
        calls = {"n": 0}

        def flaky() -> str:
            calls["n"] += 1
            if calls["n"] < 3:
                raise requests.ConnectionError("transient")
            return "ok"

        with mock.patch.object(pipeline.time, "sleep") as m_sleep:
            result = pipeline._retry_with_backoff(flaky, attempts=3, base_delay=0.5)

        self.assertEqual(result, "ok")
        self.assertEqual(calls["n"], 3)
        # Two sleeps for three attempts; the last attempt does not sleep.
        self.assertEqual(m_sleep.call_count, 2)

    def test_succeeds_first_try_never_sleeps(self) -> None:
        with mock.patch.object(pipeline.time, "sleep") as m_sleep:
            result = pipeline._retry_with_backoff(lambda: 42)
        self.assertEqual(result, 42)
        m_sleep.assert_not_called()

    def test_reraises_last_exception_after_exhaustion(self) -> None:
        """All attempts fail → the most recent exception is re-raised (fail loud),
        and the retryable path sleeps attempts-1 times."""
        boom = requests.Timeout("still down")

        def always_fail():
            raise boom

        with mock.patch.object(pipeline.time, "sleep") as m_sleep:
            with self.assertRaises(requests.Timeout) as ctx:
                pipeline._retry_with_backoff(always_fail, attempts=3, base_delay=0.1)

        self.assertIs(ctx.exception, boom)
        self.assertEqual(m_sleep.call_count, 2)

    def test_non_retryable_propagates_immediately(self) -> None:
        """An exception NOT in retry_on must propagate on the first call with no
        retry and no sleep — proving the wrapper fails loud on unexpected errors."""
        def raises_value_error():
            raise ValueError("not a network error")

        with mock.patch.object(pipeline.time, "sleep") as m_sleep:
            with self.assertRaises(ValueError):
                pipeline._retry_with_backoff(
                    raises_value_error,
                    attempts=3,
                    retry_on=(requests.RequestException,),
                )
        m_sleep.assert_not_called()

    def test_exponential_backoff_schedule(self) -> None:
        """Delays follow base_delay * 2**i: 0.5, 1.0, 2.0 across a 4-attempt run."""
        def always_fail():
            raise requests.ConnectionError("x")

        with mock.patch.object(pipeline.time, "sleep") as m_sleep:
            with self.assertRaises(requests.ConnectionError):
                pipeline._retry_with_backoff(always_fail, attempts=4, base_delay=0.5)

        delays = [c.args[0] for c in m_sleep.call_args_list]
        self.assertEqual(delays, [0.5, 1.0, 2.0])

    def test_attempts_below_one_rejected(self) -> None:
        with self.assertRaises(ValueError):
            pipeline._retry_with_backoff(lambda: 1, attempts=0)

    def test_custom_retry_on_tuple(self) -> None:
        """retry_on can be widened to other exception types."""
        calls = {"n": 0}

        def flaky():
            calls["n"] += 1
            if calls["n"] < 2:
                raise KeyError("transient-ish")
            return "recovered"

        with mock.patch.object(pipeline.time, "sleep"):
            result = pipeline._retry_with_backoff(
                flaky, attempts=3, retry_on=(KeyError,)
            )
        self.assertEqual(result, "recovered")
        self.assertEqual(calls["n"], 2)


class TestSearchCallersPreserveNoneContract(unittest.TestCase):
    """Demonstrates R1 wrapping the Pexels/Pixabay search calls: a transient error
    that survives all retries must still yield None (not raise), so fetch_assets'
    provider-fallback chain keeps working (M1's contract)."""

    def test_pexels_search_returns_none_after_retries_exhausted(self) -> None:
        with mock.patch.object(pipeline.time, "sleep") as m_sleep, \
             mock.patch.object(
                 pipeline.requests, "get",
                 side_effect=requests.ConnectionError("down"),
             ):
            result = pipeline._search_pexels_video("ai robot", "fake-key")
        self.assertIsNone(result)
        # Retries actually happened before giving up (default attempts=3 → 2 sleeps).
        self.assertEqual(m_sleep.call_count, 2)

    def test_pixabay_search_returns_none_after_retries_exhausted(self) -> None:
        with mock.patch.object(pipeline.time, "sleep") as m_sleep, \
             mock.patch.object(
                 pipeline.requests, "get",
                 side_effect=requests.Timeout("slow"),
             ):
            result = pipeline._search_pixabay_video("ai robot", "fake-key")
        self.assertIsNone(result)
        self.assertEqual(m_sleep.call_count, 2)

    def test_pexels_unexpected_error_still_propagates(self) -> None:
        """A non-requests error inside the search must NOT be retried or swallowed —
        it propagates loud (M1's fail-loud guarantee), since it is not in retry_on."""
        with mock.patch.object(pipeline.time, "sleep"), \
             mock.patch.object(
                 pipeline.requests, "get",
                 side_effect=ValueError("bug in params"),
             ):
            with self.assertRaises(ValueError):
                pipeline._search_pexels_video("ai robot", "fake-key")


if __name__ == "__main__":
    unittest.main()
