"""Gemini Flash-Lite parallel fact-check adapter (code-only ship; dormant by default).

Per `feedback_llm_api_policy.md`, each API-enabled feature requires per-feature
operator approval before adding to `requirements.txt`. The operator approved
Gemini Flash-Lite (free tier only) on 2026-05-08 to replace the Stage 5
manual-halt fact-check with parallel automated verification. This module ships
the code but is dormant until the operator activates it. Activation steps:

    1. Get a Gemini API key from https://aistudio.google.com/apikey (free tier)
    2. Add `GEMINI_API_KEY=...` to `.env`
    3. `pip install google-genai` (already pinned in requirements.txt)
    4. Flip `config.fact_check.gemini_enabled: true` in `config.yaml`
    5. Pipeline integration is a SEPARATE decision — this tool is standalone
       until then. Run it manually via the CLI to fact-check a script.

Until step 4 is done, this module is dormant. The `google.genai` SDK is
imported LAZILY inside the function bodies that use it, so the rest of the
pipeline does not ImportError at startup when the dep is not installed.

SDK migration (2026-05-08): the legacy `google-generativeai` package reaches
end-of-life on 2025-11-30 and is replaced by the unified `google-genai` SDK
(`from google import genai`, `genai.Client(api_key=...)`). All call sites use
the new client API; the legacy `genai.configure()` + `genai.GenerativeModel`
shape is gone.

Output contract: emits a markdown table matching the schema in
`prompts/05_fact_check.md` so `pipeline._parse_factcheck_response` consumes
the output unchanged. Columns:

    | Claim | Status | URL | Quote | Fix |

Statuses: VERIFIED / LIKELY_WRONG / UNVERIFIABLE / UNCLEAR

CLI:
    python tools/factcheck_gemini.py --script <path> [--out <path>]
                                     [--model gemini-flash-lite-latest]
                                     [--max-parallel 5] [--dry-run]

`--dry-run` prints the extracted claims but does NOT call the verification
API (saves quota during testing).

Free-tier model name: as of 2026-05, the free-tier Flash-Lite model is exposed
in the SDK as `gemini-flash-lite-latest`. The constant DEFAULT_MODEL below
tracks the current name; override via `--model` if Google renames it.

URL realness check: `verify_claim` post-processes every URL the verifier
emits with a HEAD request. Hallucinated/dead URLs are stripped (and the
status downgraded to UNVERIFIABLE) so a fabricated source can't poison the
fact-check table. See `_verify_url_real`.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

log = logging.getLogger("factcheck_gemini")

DEFAULT_MODEL = "gemini-flash-lite-latest"
DEFAULT_MAX_PARALLEL = 5
DEFAULT_MAX_RETRIES = 3
DEFAULT_BACKOFF_BASE_S = 2.0  # exponential: 2s, 4s, 8s

# URL realness check: HEAD request timeout + browser-like UA so we don't get
# 403'd by Cloudflare/CDN edge filters that block default `python-requests/x.y`.
_URL_CHECK_TIMEOUT_S = 5.0
_URL_CHECK_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# Status tokens accepted in verifier responses — must be a subset of the
# statuses pipeline._STATUS_NORMALIZE understands. UNCLEAR is the catch-all
# for content-policy refusals and ambiguous results.
_VALID_STATUSES = {"VERIFIED", "LIKELY_WRONG", "UNVERIFIABLE", "UNCLEAR"}

# Prompt for claim extraction — single Gemini call returning a JSON list of
# verbatim claim strings. Tightened 2026-05-08 after a 2/8 extraction rate
# vs Claude's 8/8 on the same script. The earlier prompt over-filtered on
# "rhetorical" and "editorial framing" labels and missed product names,
# version numbers, behavioral claims, and UX assertions.
_EXTRACT_PROMPT = """You are a fact-checking assistant. Extract EVERY checkable factual claim from the script below. Be exhaustive — under-extraction is the failure mode to avoid.

A factual claim is ANY statement a reader could verify against an external source. Extract aggressively, including but NOT limited to:

1. EVERY named product, tool, brand, company, or model:
   ChatGPT, Claude, Gemini, GPT-4, GPT-4o, GPT-5, Sonnet, Opus, Haiku, Anthropic, OpenAI, Google, Microsoft, etc. — if a name is mentioned, it's a claim that the named thing exists and was used in the context described.

2. EVERY version number, statistic, or quantitative claim:
   "GPT-4 has 1.76 trillion parameters", "Sonnet 4.5 launched in October", "70% of users", "$200/mo", "5x faster", "100ms latency", any number tied to a fact.

3. EVERY behavioral assertion ("X does Y", "Y happens when Z"):
   "ChatGPT routes long prompts to a smaller model", "Claude doesn't show a warning when it switches models", "Gemini cuts off at 8k tokens", any claim about HOW a system behaves.

4. EVERY user-facing UX claim:
   "they don't show a warning", "you get routed to a different model", "the interface hides the model name", "users see X", any claim about what a user observes or experiences.

5. EVERY causal / historical claim:
   "X launched in November 2022", "Y was released after Z", "A caused B", "C was introduced in version D".

DO NOT drop a claim because it is "rhetorical", "editorial framing", "obviously true", "context", or "setup". If the script presents it as a fact a viewer is meant to believe, it IS a claim. Extract it.

Skip ONLY:
- Pure opinions with no factual backbone ("AI is amazing")
- Stage directions in brackets like [B-roll: server room]
- Hooks framed as questions with no factual assertion ("ever wonder what AI does?")
- Direct first-person speech without an underlying factual claim ("I think it's cool")

Return a JSON array of strings. Each string is one claim, quoted VERBATIM (or near-verbatim — preserve the factual content; minor pronoun normalization is OK if the original is ambiguous in isolation). Output ONLY the JSON array — no preamble, no markdown fences, no commentary.

Example output: ["ChatGPT was released in November 2022", "GPT-4 has 1.76 trillion parameters", "Claude does not show a warning when routing to a smaller model"]

SCRIPT:
{SCRIPT}
"""

_VERIFY_PROMPT = """You are a fact-checker. Verify the claim below against authoritative sources.

CLAIM: {CLAIM}

Return a JSON object with exactly these keys:
- "status": one of "VERIFIED", "LIKELY_WRONG", "UNVERIFIABLE", "UNCLEAR"
- "url": the most authoritative source URL you found, or empty string if none
- "quote": the exact wording from the source that supports or contradicts the claim, or empty string
- "fix": if status is LIKELY_WRONG, a `Replace "X" with "Y"` suggestion (using verbatim claim text for X); else empty string

Status meanings:
- VERIFIED: claim is supported by authoritative source
- LIKELY_WRONG: claim contradicts authoritative source — provide a fix
- UNVERIFIABLE: cannot be checked (private data, not yet published, etc.)
- UNCLEAR: ambiguous wording, missing context, or you cannot determine

CRITICAL: only emit a URL you are CERTAIN exists and is reachable. If you are not certain a specific article URL exists, emit empty string for "url" and explain in "quote" instead. Do NOT guess plausible-looking URLs. Hallucinated URLs are worse than no URL.

Output ONLY the JSON object — no preamble, no markdown fences, no commentary.
"""


class GeminiFactcheckError(RuntimeError):
    """Raised when Gemini fact-check fails (missing SDK, auth, quota, network, parse).

    Wraps every failure mode `extract_claims`/`verify_claim`/`run_factcheck` can
    hit so callers have a single exception type instead of `ImportError`,
    `KeyError`, `json.JSONDecodeError`, or SDK-specific exception classes
    leaking out.
    """


# ---------------------------------------------------------------------------
# Lazy SDK access
# ---------------------------------------------------------------------------


def _resolve_api_key(api_key: str | None) -> str:
    """Resolve the Gemini API key — explicit arg first, then env. Fail loud if missing."""
    resolved = api_key if api_key is not None else os.environ.get("GEMINI_API_KEY")
    if not resolved:
        raise GeminiFactcheckError(
            "Gemini API key not provided: pass `api_key=` or set the "
            "GEMINI_API_KEY environment variable. Free tier: get a key at "
            "https://aistudio.google.com/apikey"
        )
    return resolved


def _lazy_import_genai() -> Any:
    """Lazy-import `google.genai`. Raises GeminiFactcheckError on missing dep."""
    try:
        from google import genai  # type: ignore[import-not-found]
    except ImportError as exc:
        raise GeminiFactcheckError(
            "google-genai SDK not installed; pip install google-genai "
            "to activate. See module docstring for the activation flow."
        ) from exc
    return genai


def _is_retryable(exc: BaseException) -> bool:
    """Return True if `exc` looks like a transient Gemini/network failure.

    Retryable: HTTP 429 / 5xx, connect/read timeouts, generic OSError network
    errors. The SDK exposes its own exception classes; we duck-type by class
    name so we don't import the SDK just for isinstance checks.
    """
    # google-api-core / google-genai raises errors with a `.code` HTTP status.
    code = getattr(exc, "code", None)
    if isinstance(code, int):
        if code == 429 or 500 <= code < 600:
            return True
    response = getattr(exc, "response", None)
    if response is not None:
        status = getattr(response, "status_code", None)
        if isinstance(status, int):
            return status == 429 or 500 <= status < 600
    name = type(exc).__name__
    if name in {
        "ResourceExhausted",      # google-api-core 429
        "ServiceUnavailable",     # google-api-core 503
        "DeadlineExceeded",       # google-api-core deadline
        "InternalServerError",    # google-api-core 500
        "ServerError",            # google-genai 5xx wrapper
        "APIError",               # google-genai generic — retry; non-retryable will be re-raised by attempt cap
        "ConnectError", "ConnectTimeout", "ReadError", "ReadTimeout",
        "WriteError", "WriteTimeout", "PoolTimeout", "RemoteProtocolError",
        "NetworkError", "TimeoutException",
    }:
        return True
    if isinstance(exc, OSError):
        return True
    return False


def _is_content_policy_refusal(exc: BaseException) -> bool:
    """Return True if `exc` looks like a Gemini content-policy/safety refusal.

    Per the Gemini SDK, blocked content surfaces as either a non-200 finish
    reason ("SAFETY", "RECITATION", "PROHIBITED_CONTENT") embedded in the
    response, or as an explicit `BlockedPromptException` / `StopCandidateException`.
    Duck-typed by class name to avoid importing the SDK here.
    """
    name = type(exc).__name__
    if name in {"BlockedPromptException", "StopCandidateException"}:
        return True
    msg = str(exc).lower()
    if any(t in msg for t in ("safety", "blocked", "prohibited", "recitation")):
        # Only if it's not also a transient — retryable wins over content-policy.
        if not _is_retryable(exc):
            return True
    return False


def _strip_code_fence(text: str) -> str:
    """Strip ```json ... ``` fences if Gemini wraps the response despite our prompt."""
    s = text.strip()
    if s.startswith("```"):
        # Drop opening fence (with optional language tag) and closing fence.
        s = re.sub(r"^```[a-zA-Z]*\s*\n?", "", s)
        if s.endswith("```"):
            s = s[: -3]
    return s.strip()


# ---------------------------------------------------------------------------
# URL realness check
# ---------------------------------------------------------------------------


def _verify_url_real(url: str) -> tuple[bool, str]:
    """HEAD-check a URL to confirm it resolves to a real (2xx/3xx) page.

    Returns (is_real, reason). `is_real` is True if the URL responded with a
    2xx or 3xx status. `reason` is a short human-readable string for the
    fact-check Fix column when stripping the URL.

    A 4xx (especially 404) is the canonical "Gemini hallucinated a URL"
    failure mode; a 5xx or timeout means the source is currently
    unreachable, which is also unsafe to leave in the table — a viewer
    clicking through gets nothing.

    Browser-like User-Agent avoids generic 403s from CDN edge filters.
    `allow_redirects=True` so a 301 → 200 chain still counts as real.
    """
    try:
        import requests  # type: ignore[import-not-found]
    except ImportError as exc:
        # `requests` is a pinned dep — if it's missing the env is broken;
        # treat as "URL unverifiable" rather than crashing the batch.
        log.error("url-check: requests library missing — %s", exc)
        return False, "URL realness check unavailable (requests not installed)"

    log.info("url-check: HEAD %s", url)
    try:
        resp = requests.head(
            url,
            allow_redirects=True,
            timeout=_URL_CHECK_TIMEOUT_S,
            headers={"User-Agent": _URL_CHECK_UA},
        )
    except requests.exceptions.Timeout:
        log.warning("url-check: TIMEOUT — %s", url)
        return False, f"timed out after {_URL_CHECK_TIMEOUT_S:.0f}s"
    except requests.exceptions.ConnectionError as exc:
        log.warning("url-check: CONNECTION_ERROR — %s (%s)", url, exc)
        return False, "connection error"
    except requests.exceptions.RequestException as exc:
        log.warning("url-check: REQUEST_ERROR — %s (%s)", url, exc)
        return False, f"request error ({type(exc).__name__})"

    code = resp.status_code
    if 200 <= code < 400:
        log.info("url-check: OK %d — %s", code, url)
        return True, ""
    log.warning("url-check: NOT_REAL HTTP %d — %s", code, url)
    return False, f"HTTP {code}"


def _apply_url_realness_check(verification: dict[str, str]) -> dict[str, str]:
    """Post-process a verify dict, stripping URLs that fail the HEAD check.

    Mutates a copy of `verification` and returns it. If the URL is empty or
    fails the HEAD check, the URL is set to "" and a note is appended to the
    Fix column. If the original status was VERIFIED, it is downgraded to
    UNVERIFIABLE because a verified claim must have a retrievable source.

    LIKELY_WRONG status is preserved even on URL strip — the verifier flagged
    a mismatch, and dropping the source doesn't undo that judgment; the fix
    column already carries the correction. The note is still appended.

    UNCLEAR / UNVERIFIABLE status is preserved.
    """
    out = dict(verification)
    url = (out.get("url") or "").strip()
    if not url:
        return out

    is_real, reason = _verify_url_real(url)
    if is_real:
        return out

    original_status = out.get("status", "UNCLEAR")
    note = (
        f"Source URL failed verification (HTTP {reason}); claim could not be backed by a retrievable source"
        if reason.startswith("HTTP ")
        else f"Source URL failed verification ({reason}); claim could not be backed by a retrievable source"
    )
    out["url"] = ""

    existing_fix = (out.get("fix") or "").strip()
    if existing_fix:
        out["fix"] = f"{existing_fix} | {note}"
    else:
        out["fix"] = note

    if original_status == "VERIFIED":
        out["status"] = "UNVERIFIABLE"
        log.warning(
            "url-realness: stripped fabricated URL on VERIFIED claim — downgraded to UNVERIFIABLE",
        )
    else:
        log.warning(
            "url-realness: stripped URL on %s claim (status preserved)", original_status,
        )

    return out


# ---------------------------------------------------------------------------
# Core API calls (google-genai SDK)
# ---------------------------------------------------------------------------


def _call_gemini_with_retry(
    prompt: str,
    *,
    model: str,
    api_key: str,
    max_retries: int = DEFAULT_MAX_RETRIES,
    backoff_base_s: float = DEFAULT_BACKOFF_BASE_S,
) -> str:
    """Single Gemini call with exponential backoff on retryable errors.

    Uses the `google-genai` SDK (the unified client that replaced the
    EOL `google-generativeai` package).

    Returns the raw text response. Raises GeminiFactcheckError on exhaustion or
    non-retryable failure. Content-policy refusals are NOT caught here —
    callers handle them at the verify_claim level (so the claim becomes UNCLEAR
    instead of failing the whole batch).
    """
    genai = _lazy_import_genai()
    client = genai.Client(api_key=api_key)

    last_exc: BaseException | None = None
    for attempt in range(max_retries + 1):
        try:
            log.info(
                "gemini: call attempt %d/%d — model=%s prompt_chars=%d",
                attempt + 1, max_retries + 1, model, len(prompt),
            )
            response = client.models.generate_content(
                model=model, contents=prompt,
            )
            text = getattr(response, "text", None)
            if not text:
                # Empty response — could be a safety block surfaced as an
                # empty .text with a finish_reason on the candidate. Surface
                # as content-policy refusal for the verify path.
                raise GeminiFactcheckError(
                    "Gemini returned empty response (likely content-policy block)"
                )
            return text
        except GeminiFactcheckError:
            raise
        except Exception as exc:  # noqa: BLE001 — duck-typed retry classifier below
            last_exc = exc
            if not _is_retryable(exc) or attempt >= max_retries:
                raise GeminiFactcheckError(
                    f"Gemini call failed after {attempt + 1} attempt(s): "
                    f"{type(exc).__name__}: {exc}"
                ) from exc
            sleep_s = backoff_base_s * (2 ** attempt)
            log.warning(
                "gemini: retryable error on attempt %d (%s: %s) — sleeping %.2fs",
                attempt + 1, type(exc).__name__, exc, sleep_s,
            )
            time.sleep(sleep_s)

    # Defensive — loop should have either returned or raised.
    raise GeminiFactcheckError(
        f"Gemini call produced no result (last exc: {last_exc!r})"
    )


def extract_claims(
    script_text: str,
    model: str = DEFAULT_MODEL,
    *,
    api_key: str | None = None,
    max_retries: int = DEFAULT_MAX_RETRIES,
) -> list[str]:
    """Extract factual claims from a script via a single Gemini call.

    Args:
        script_text: Script body to extract claims from.
        model: Gemini model id. Default `gemini-flash-lite-latest`.
        api_key: Optional explicit API key; falls back to GEMINI_API_KEY env.
        max_retries: Retry count on 429/5xx/network errors.

    Returns:
        List of claim strings, each verbatim from the script.

    Raises:
        GeminiFactcheckError: on missing SDK, missing key, exhausted retries,
            empty/malformed JSON response, or non-list JSON top-level.
    """
    if not script_text or not script_text.strip():
        raise GeminiFactcheckError("extract_claims: `script_text` must be non-empty")

    resolved_key = _resolve_api_key(api_key)
    prompt = _EXTRACT_PROMPT.format(SCRIPT=script_text)
    raw = _call_gemini_with_retry(
        prompt, model=model, api_key=resolved_key, max_retries=max_retries,
    )

    cleaned = _strip_code_fence(raw)
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        log.error("gemini: malformed JSON from extract_claims: %s", cleaned[:500])
        raise GeminiFactcheckError(
            f"extract_claims: Gemini returned malformed JSON: {exc}"
        ) from exc

    if not isinstance(parsed, list):
        raise GeminiFactcheckError(
            f"extract_claims: expected JSON array, got {type(parsed).__name__}"
        )
    if not all(isinstance(c, str) for c in parsed):
        raise GeminiFactcheckError(
            "extract_claims: JSON array must contain only strings"
        )

    log.info("gemini: extracted %d claims", len(parsed))
    return [c.strip() for c in parsed if c.strip()]


def verify_claim(
    claim: str,
    model: str = DEFAULT_MODEL,
    *,
    api_key: str | None = None,
    max_retries: int = DEFAULT_MAX_RETRIES,
    skip_url_check: bool = False,
) -> dict[str, str]:
    """Verify a single claim via a single Gemini call.

    Returns a dict with keys: status, url, quote, fix. Status is normalized to
    one of VERIFIED / LIKELY_WRONG / UNVERIFIABLE / UNCLEAR.

    Content-policy refusals → UNCLEAR with a note in the `quote` field. This
    is intentional: the parent pipeline's `_parse_factcheck_response` treats
    UNCLEAR as needing operator review at gate 2, which is the right behavior
    when Gemini won't engage with the claim.

    URL realness: every emitted URL is HEAD-checked. Fabricated/dead URLs are
    stripped and the status is downgraded from VERIFIED → UNVERIFIABLE; a note
    is appended to the Fix column. Disable the HEAD check via
    `skip_url_check=True` (used in tests that mock the verify call directly
    without wanting to exercise the network HEAD step).

    Args:
        claim: The single claim to verify.
        model: Gemini model id.
        api_key: Optional explicit API key; falls back to GEMINI_API_KEY env.
        max_retries: Retry count on 429/5xx/network errors.
        skip_url_check: If True, do not HEAD-check the URL. Default False.

    Returns:
        Dict with keys {status, url, quote, fix}. All values are strings
        (never None) so format_as_markdown_table doesn't have to handle None.

    Raises:
        GeminiFactcheckError: on missing SDK, missing key, exhausted retries,
            or malformed JSON. Content-policy refusals do NOT raise — they
            return an UNCLEAR dict instead.
    """
    if not claim or not claim.strip():
        raise GeminiFactcheckError("verify_claim: `claim` must be non-empty")

    resolved_key = _resolve_api_key(api_key)
    prompt = _VERIFY_PROMPT.format(CLAIM=claim)

    try:
        raw = _call_gemini_with_retry(
            prompt, model=model, api_key=resolved_key, max_retries=max_retries,
        )
    except GeminiFactcheckError as exc:
        # Surface content-policy refusal as UNCLEAR rather than failing the batch.
        cause = exc.__cause__ if exc.__cause__ is not None else exc
        if _is_content_policy_refusal(cause) or "content-policy" in str(exc).lower():
            log.warning(
                "gemini: content-policy refusal on claim %r — marking UNCLEAR",
                claim[:80],
            )
            return {
                "status": "UNCLEAR",
                "url": "",
                "quote": "Content-policy refusal from Gemini; manual review required.",
                "fix": "",
            }
        raise

    cleaned = _strip_code_fence(raw)
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        log.error("gemini: malformed JSON from verify_claim: %s", cleaned[:500])
        raise GeminiFactcheckError(
            f"verify_claim: Gemini returned malformed JSON: {exc}"
        ) from exc

    if not isinstance(parsed, dict):
        raise GeminiFactcheckError(
            f"verify_claim: expected JSON object, got {type(parsed).__name__}"
        )

    status = str(parsed.get("status", "")).strip().upper().replace(" ", "_")
    if status not in _VALID_STATUSES:
        log.warning(
            "gemini: unrecognized status %r for claim %r; coercing to UNCLEAR",
            status, claim[:80],
        )
        status = "UNCLEAR"

    result = {
        "status": status,
        "url": str(parsed.get("url", "") or "").strip(),
        "quote": str(parsed.get("quote", "") or "").strip(),
        "fix": str(parsed.get("fix", "") or "").strip(),
    }

    if not skip_url_check:
        result = _apply_url_realness_check(result)

    return result


# ---------------------------------------------------------------------------
# Parallel verification
# ---------------------------------------------------------------------------


async def _verify_one_async(
    claim: str,
    model: str,
    api_key: str,
    semaphore: asyncio.Semaphore,
    max_retries: int,
) -> dict[str, str]:
    """Run `verify_claim` in a thread under the parallelism semaphore.

    The google-genai SDK exposes `client.aio.models.generate_content` for
    native async, but `verify_claim` is a sync function (it also needs to do
    a sync `requests.head` for the URL realness check). We offload the whole
    thing to the default loop executor under a semaphore that caps
    concurrency at `max_parallel` to stay within free-tier QPM limits.
    """
    async with semaphore:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None,
            lambda: verify_claim(claim, model, api_key=api_key, max_retries=max_retries),
        )


async def _verify_all_async(
    claims: list[str],
    model: str,
    api_key: str,
    max_parallel: int,
    max_retries: int,
) -> list[dict[str, str]]:
    semaphore = asyncio.Semaphore(max_parallel)
    return await asyncio.gather(
        *[
            _verify_one_async(c, model, api_key, semaphore, max_retries)
            for c in claims
        ]
    )


def verify_all_claims(
    claims: list[str],
    model: str = DEFAULT_MODEL,
    max_parallel: int = DEFAULT_MAX_PARALLEL,
    *,
    api_key: str | None = None,
    max_retries: int = DEFAULT_MAX_RETRIES,
) -> list[dict[str, str]]:
    """Verify N claims in parallel via asyncio.gather + a semaphore.

    Args:
        claims: List of claim strings (from `extract_claims`).
        model: Gemini model id.
        max_parallel: Concurrency cap. Default 5 — free-tier Flash-Lite has
            ~30 QPM so 5 parallel verifies (each ~1-3s) keeps headroom.
        api_key: Optional explicit API key; falls back to GEMINI_API_KEY env.
        max_retries: Per-claim retry count.

    Returns:
        List of verification dicts, one per input claim, in the same order.

    Raises:
        GeminiFactcheckError: if any single verification raises it (asyncio.gather
            propagates the first exception).
    """
    if not claims:
        return []
    resolved_key = _resolve_api_key(api_key)
    log.info(
        "gemini: verifying %d claims in parallel (max_parallel=%d, model=%s)",
        len(claims), max_parallel, model,
    )
    return asyncio.run(
        _verify_all_async(claims, model, resolved_key, max_parallel, max_retries),
    )


# ---------------------------------------------------------------------------
# Markdown formatting
# ---------------------------------------------------------------------------


def _escape_md_cell(text: str) -> str:
    """Escape pipes and collapse newlines so a cell stays on one row."""
    if not text:
        return ""
    return text.replace("|", "\\|").replace("\n", " ").replace("\r", " ").strip()


def format_as_markdown_table(
    claims: list[str],
    verifications: list[dict[str, str]],
) -> str:
    """Emit the markdown table consumed by `pipeline._parse_factcheck_response`.

    Schema (5 columns, exact headers — matches prompts/05_fact_check.md):

        | Claim | Status | URL | Quote | Fix |
        |-------|--------|-----|-------|-----|
        | "..." | VERIFIED | https://... | "..." | (n/a) |

    Args:
        claims: List of claim strings.
        verifications: List of verify dicts. Must be same length as claims.

    Returns:
        Markdown table as a single string (no trailing newline beyond the
        last row).

    Raises:
        ValueError: if len(claims) != len(verifications).
    """
    if len(claims) != len(verifications):
        raise ValueError(
            f"format_as_markdown_table: claims ({len(claims)}) and "
            f"verifications ({len(verifications)}) length mismatch"
        )

    lines: list[str] = []
    lines.append("| Claim | Status | URL | Quote | Fix |")
    lines.append("|-------|--------|-----|-------|-----|")
    for claim, v in zip(claims, verifications):
        lines.append(
            "| {claim} | {status} | {url} | {quote} | {fix} |".format(
                claim=_escape_md_cell(claim),
                status=_escape_md_cell(v.get("status", "UNCLEAR")),
                url=_escape_md_cell(v.get("url", "")),
                quote=_escape_md_cell(v.get("quote", "")),
                fix=_escape_md_cell(v.get("fix", "")),
            )
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Top-level entry
# ---------------------------------------------------------------------------


def run_factcheck(
    script_path: Path,
    out_path: Path | None = None,
    *,
    model: str = DEFAULT_MODEL,
    max_parallel: int = DEFAULT_MAX_PARALLEL,
    api_key: str | None = None,
    dry_run: bool = False,
) -> str:
    """Run the full extract → verify → format pipeline on a script file.

    Args:
        script_path: Path to the script text file.
        out_path: Optional path to write the markdown table. If None, the
            table is only returned (and printed by the CLI).
        model: Gemini model id.
        max_parallel: Concurrency cap for verification.
        api_key: Optional explicit API key; falls back to GEMINI_API_KEY env.
        dry_run: If True, extract claims but skip verification. Output table
            shows status=UNCLEAR for every claim with a "(dry run)" note.

    Returns:
        The markdown table string.

    Raises:
        GeminiFactcheckError: any failure mode of the underlying calls.
        FileNotFoundError: if `script_path` doesn't exist.
    """
    script_path = Path(script_path)
    if not script_path.exists():
        raise FileNotFoundError(f"script_path does not exist: {script_path}")

    script_text = script_path.read_text(encoding="utf-8")
    log.info("factcheck_gemini: read %d chars from %s", len(script_text), script_path)

    claims = extract_claims(script_text, model=model, api_key=api_key)
    if not claims:
        log.warning("factcheck_gemini: no claims extracted from %s", script_path)
        table = "| Claim | Status | URL | Quote | Fix |\n|-------|--------|-----|-------|-----|"
    elif dry_run:
        log.info("factcheck_gemini: dry-run — skipping verification of %d claims", len(claims))
        verifications = [
            {"status": "UNCLEAR", "url": "", "quote": "(dry run — not verified)", "fix": ""}
            for _ in claims
        ]
        table = format_as_markdown_table(claims, verifications)
    else:
        verifications = verify_all_claims(
            claims, model=model, max_parallel=max_parallel, api_key=api_key,
        )
        table = format_as_markdown_table(claims, verifications)

    if out_path is not None:
        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(table, encoding="utf-8")
        log.info("factcheck_gemini: wrote table to %s (%d chars)", out_path, len(table))

    return table


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Gemini Flash-Lite parallel fact-check (free tier). "
                    "Dormant by default — see module docstring for activation.",
    )
    parser.add_argument(
        "--script", required=True, type=Path,
        help="Path to script text file to fact-check.",
    )
    parser.add_argument(
        "--out", type=Path, default=None,
        help="Optional output path for the markdown table. "
             "If omitted, the table is printed to stdout only.",
    )
    parser.add_argument(
        "--model", default=DEFAULT_MODEL,
        help=f"Gemini model id (default: {DEFAULT_MODEL}).",
    )
    parser.add_argument(
        "--max-parallel", type=int, default=DEFAULT_MAX_PARALLEL,
        help=f"Max concurrent verifications (default: {DEFAULT_MAX_PARALLEL}).",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Extract claims but do NOT call the verification API "
             "(saves quota during testing).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = _build_arg_parser().parse_args(argv)
    try:
        table = run_factcheck(
            args.script,
            out_path=args.out,
            model=args.model,
            max_parallel=args.max_parallel,
            dry_run=args.dry_run,
        )
    except GeminiFactcheckError as exc:
        log.error("factcheck failed: %s", exc)
        return 1
    except FileNotFoundError as exc:
        log.error("script not found: %s", exc)
        return 1
    print(table)
    return 0


if __name__ == "__main__":
    sys.exit(main())
