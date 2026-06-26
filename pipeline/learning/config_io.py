"""Comment-preserving, single-key editor for config.yaml.

The loop auto-applies only reversible, non-sacred ``config.yaml`` scalar knobs.
``config.yaml`` is heavily commented (every key documents its rationale), so a
naive ``yaml.safe_load`` + ``yaml.dump`` round-trip — which strips comments and
reorders keys — is unacceptable. This module does a LINE-BASED edit that changes
only the target value token and leaves every comment, blank line, and key order
byte-identical.

Safety:
  * Refuses to edit any sacred / forbidden key (defense-in-depth — the policy
    layer also classifies these as PROPOSE-only).
  * Refuses to operate on a ``scoring_weights*.json`` file (component weights are
    PROPOSE-only and under a governance HOLD).
  * Idempotent: applying the value already on disk is a no-op.
  * Snapshots the pre-change file to ``<config>.learning.bak`` before writing.

Only the two-level keys the loop tunes are supported (``section.key``), which is
all the SAFE-AUTO knobs are. Stdlib only.
"""

from __future__ import annotations

import logging
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("learning.config_io")

# Sacred / never-auto-edit keys (defense-in-depth; policy.py is the primary gate).
FORBIDDEN_KEYS: frozenset[str] = frozenset({
    "fact_check.require_human_resolution",
    "fact_check.auto_resolve_gate_2",
    "publishing.human_qa_required",
    "publishing.kill_switch",
})

_KEY_LINE = re.compile(r"^(?P<indent>\s*)(?P<key>[A-Za-z0-9_]+):(?P<rest>.*)$")


class ConfigEditError(RuntimeError):
    """Raised when a key cannot be located or an edit is refused."""


@dataclass(frozen=True)
class ApplyResult:
    target: str
    old_value: str | None
    new_value: str
    changed: bool
    snapshot: str | None


def _render(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _split_value_comment(rest: str) -> tuple[str, str]:
    """Split the part after ``key:`` into (value_region, comment_region).

    Quote-aware so a ``#`` inside a quoted value isn't treated as a comment.
    ``comment_region`` keeps its leading whitespace so it can be re-appended
    verbatim. ``value_region`` keeps its leading space after the colon.
    """
    in_quote: str | None = None
    for i, ch in enumerate(rest):
        if ch in ('"', "'"):
            if in_quote is None:
                in_quote = ch
            elif in_quote == ch:
                in_quote = None
        elif ch == "#" and in_quote is None:
            j = i
            while j > 0 and rest[j - 1] in " \t":
                j -= 1
            return rest[:j], rest[j:]
    return rest, ""


def _find_key_line(lines: list[str], section: str, key: str) -> int:
    """Index of the ``key`` line inside ``section`` (top-level). -1 if absent."""
    in_section = False
    for idx, line in enumerate(lines):
        stripped = line.rstrip("\n")
        if re.match(rf"^{re.escape(section)}:\s*(#.*)?$", stripped):
            in_section = True
            continue
        if in_section:
            # A new top-level key (no indent, non-comment) ends the section.
            if stripped and not stripped[0].isspace() and not stripped.lstrip().startswith("#"):
                break
            m = _KEY_LINE.match(stripped)
            if m and m.group("key") == key and m.group("indent"):
                return idx
    return -1


def read_yaml_key(path: Path | str, dotted_key: str) -> str | None:
    """Return the current raw value (quotes stripped) or None if not found."""
    section, _, key = dotted_key.partition(".")
    if not key:
        raise ConfigEditError(f"only two-level keys supported, got {dotted_key!r}")
    p = Path(path)
    if not p.exists():
        return None
    lines = p.read_text(encoding="utf-8").splitlines()
    idx = _find_key_line(lines, section, key)
    if idx < 0:
        return None
    m = _KEY_LINE.match(lines[idx])
    value_region, _ = _split_value_comment(m.group("rest"))
    return value_region.strip().strip('"').strip("'")


def set_yaml_text(text: str, dotted_key: str, new_value) -> tuple[str | None, str, bool]:
    """Return (old_value, new_text, changed) for a single-key edit on ``text``."""
    section, _, key = dotted_key.partition(".")
    if not key:
        raise ConfigEditError(f"only two-level keys supported, got {dotted_key!r}")
    lines = text.splitlines(keepends=True)
    stripped_lines = [ln.rstrip("\n") for ln in lines]
    idx = _find_key_line(stripped_lines, section, key)
    if idx < 0:
        raise ConfigEditError(f"key {dotted_key!r} not found")

    m = _KEY_LINE.match(stripped_lines[idx])
    indent, key_name, rest = m.group("indent"), m.group("key"), m.group("rest")
    value_region, comment = _split_value_comment(rest)
    old_raw = value_region.strip()
    old_value = old_raw.strip('"').strip("'")

    # Preserve the original quote style for string values.
    quote = ""
    if old_raw[:1] in ('"', "'"):
        quote = old_raw[0]
    rendered = _render(new_value)
    new_token = f"{quote}{rendered}{quote}" if quote else rendered

    if old_value == rendered:
        return old_value, text, False  # idempotent no-op

    newline = "\n" if lines[idx].endswith("\n") else ""
    lines[idx] = f"{indent}{key_name}: {new_token}{comment}{newline}"
    return old_value, "".join(lines), True


def apply_knob(
    config_path: Path | str,
    dotted_key: str,
    new_value,
    *,
    allow_forbidden: bool = False,
) -> ApplyResult:
    """Set ``dotted_key`` to ``new_value`` in config.yaml (comment-preserving).

    Idempotent (no-op if already set). Snapshots the pre-change file. Refuses
    sacred keys and scoring_weights*.json files.
    """
    p = Path(config_path)
    if p.name.startswith("scoring_weights"):
        raise ConfigEditError("refusing to edit scoring_weights — component weights are PROPOSE-only")
    if dotted_key in FORBIDDEN_KEYS and not allow_forbidden:
        raise ConfigEditError(f"refusing to auto-edit sacred key {dotted_key!r}")
    if not p.exists():
        raise ConfigEditError(f"config not found: {p}")

    text = p.read_text(encoding="utf-8")
    old_value, new_text, changed = set_yaml_text(text, dotted_key, new_value)
    if not changed:
        log.info("apply_knob %s already %s — no-op", dotted_key, _render(new_value))
        return ApplyResult(dotted_key, old_value, _render(new_value), False, None)

    snap = p.with_suffix(p.suffix + ".learning.bak")
    shutil.copy2(p, snap)
    p.write_text(new_text, encoding="utf-8")
    log.info("apply_knob %s: %s -> %s (snapshot %s)", dotted_key, old_value, _render(new_value), snap.name)
    return ApplyResult(dotted_key, old_value, _render(new_value), True, str(snap))


def revert_knob(config_path: Path | str, dotted_key: str, old_value) -> ApplyResult:
    """Restore ``dotted_key`` to ``old_value`` (used by auto-rollback)."""
    return apply_knob(config_path, dotted_key, old_value)


__all__ = [
    "FORBIDDEN_KEYS",
    "ConfigEditError",
    "ApplyResult",
    "read_yaml_key",
    "set_yaml_text",
    "apply_knob",
    "revert_knob",
]
