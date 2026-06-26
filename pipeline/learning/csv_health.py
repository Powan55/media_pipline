"""Quote-aware integrity audit for the weekly analytics CSV, plus a robust
positional loader the learning ledger uses.

Why this exists
---------------
``_weekly_analytics.csv`` is append-only and its on-disk HEADER predates two
additive column groups, so the header names only the first 12 columns while
newer rows carry 14 (``hold_at_3s``, ``traffic_source_shorts_pct`` — added
2026-05-08) or 15 (``analytics_error`` — added 2026-05-31) fields. This is a
benign header/row drift, NOT corruption: ``csv.reader`` (quote-aware) parses
every historical row into one of {12, 14, 15} fields. (A naive comma-split
over-counts properly-quoted comma-bearing titles and falsely flags them — do
not audit this file by splitting on commas.)

This module:
  * reports the drift (``audit_analytics_csv``) so the operator can optionally
    canonicalize the header, and flags any genuinely-anomalous row (a field
    count outside the known-good schema eras — e.g. a future unquoted comma);
  * provides ``load_latest_analytics_rows``, a POSITIONAL loader that maps each
    row onto the canonical column order regardless of the on-disk header, so
    downstream code reads ``hold_at_3s`` / ``traffic_source_shorts_pct``
    correctly WITHOUT requiring a header rewrite of the canonical file.

Stdlib only. Pure read — never mutates the CSV.
"""

from __future__ import annotations

import csv
import logging
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("learning.csv_health")

# Canonical column order — mirror of analytics_pull.merge_into_tracker `fields`
# (and analytics_pull.VideoMetrics). Append-only; never reorder.
CANONICAL_ANALYTICS_HEADER: tuple[str, ...] = (
    "pull_date",
    "platform",
    "video_id",
    "title",
    "published_at",
    "views",
    "avg_view_pct",
    "avg_view_duration_sec",
    "likes",
    "shares",
    "comments",
    "follower_delta",
    "hold_at_3s",
    "traffic_source_shorts_pct",
    "analytics_error",
)

# Legacy 12-column header still on disk (frozen before the additive columns).
LEGACY_ANALYTICS_HEADER: tuple[str, ...] = CANONICAL_ANALYTICS_HEADER[:12]

# Field counts a row may legitimately have, by schema era:
#   12 = pre-2026-05-08 (no hold/traffic), 14 = +hold/traffic,
#   15 = +analytics_error (2026-05-31 onward).
KNOWN_GOOD_FIELD_COUNTS: frozenset[int] = frozenset({12, 14, 15})


@dataclass(frozen=True)
class CsvHealthReport:
    """Result of a quote-aware audit of the analytics CSV."""

    path: str
    exists: bool
    header: tuple[str, ...]
    header_is_canonical: bool
    total_data_rows: int
    field_count_distribution: dict[int, int]
    # (file_line_number, n_fields, video_id_or_blank) for each anomalous row.
    anomalous_rows: tuple[tuple[int, int, str], ...]

    @property
    def healthy(self) -> bool:
        """True when the file exists and every data row has a known-good shape.

        Note: a non-canonical header is NOT unhealthy — the positional loader
        reads correctly regardless. ``healthy`` is strictly about row integrity.
        """
        return self.exists and not self.anomalous_rows


def audit_analytics_csv(path: Path | str) -> CsvHealthReport:
    """Quote-aware audit of the analytics tracker. Pure read, never mutates."""
    p = Path(path)
    if not p.exists() or p.stat().st_size == 0:
        return CsvHealthReport(
            path=str(p),
            exists=False,
            header=(),
            header_is_canonical=False,
            total_data_rows=0,
            field_count_distribution={},
            anomalous_rows=(),
        )

    dist: Counter[int] = Counter()
    anomalous: list[tuple[int, int, str]] = []
    header: tuple[str, ...] = ()
    total = 0

    with p.open("r", encoding="utf-8", newline="") as f:
        for idx, row in enumerate(csv.reader(f)):
            if idx == 0:
                header = tuple(row)
                continue
            total += 1
            n = len(row)
            dist[n] += 1
            if n not in KNOWN_GOOD_FIELD_COUNTS:
                video_id = row[2] if n > 2 else ""
                anomalous.append((idx + 1, n, video_id))  # idx+1 = 1-based file line

    return CsvHealthReport(
        path=str(p),
        exists=True,
        header=header,
        header_is_canonical=(header == CANONICAL_ANALYTICS_HEADER),
        total_data_rows=total,
        field_count_distribution=dict(sorted(dist.items())),
        anomalous_rows=tuple(anomalous),
    )


def load_latest_analytics_rows(path: Path | str) -> dict[str, dict]:
    """Map the analytics CSV to ``video_id -> latest (max pull_date) row dict``.

    Each row is mapped POSITIONALLY onto ``CANONICAL_ANALYTICS_HEADER`` so the
    correct column names are used regardless of the on-disk header. Robust to
    the 12/14/15-field era drift; anomalous rows (field count outside
    ``KNOWN_GOOD_FIELD_COUNTS``) are skipped (``audit_analytics_csv`` surfaces
    them). Quote-aware (``csv.reader``). Missing/empty file -> ``{}``.
    """
    out: dict[str, dict] = {}
    p = Path(path)
    if not p.exists() or p.stat().st_size == 0:
        return out

    with p.open("r", encoding="utf-8", newline="") as f:
        for idx, row in enumerate(csv.reader(f)):
            if idx == 0:
                continue  # header
            n = len(row)
            if n not in KNOWN_GOOD_FIELD_COUNTS:
                continue
            rec = {CANONICAL_ANALYTICS_HEADER[i]: row[i] for i in range(n)}
            video_id = (rec.get("video_id") or "").strip()
            pull_date = (rec.get("pull_date") or "").strip()
            if not video_id or not pull_date:
                continue
            existing = out.get(video_id)
            if existing is None or pull_date > (existing.get("pull_date") or ""):
                out[video_id] = rec
    return out


def format_report(report: CsvHealthReport) -> str:
    """Render a human-readable summary for the CLI / dashboard."""
    if not report.exists:
        return f"analytics CSV not found or empty: {report.path}"
    lines = [
        f"Analytics CSV health: {report.path}",
        f"  header canonical (15-col): {report.header_is_canonical}"
        + ("" if report.header_is_canonical else f"  (on disk: {len(report.header)} cols)"),
        f"  data rows: {report.total_data_rows}",
        "  field-count distribution (quote-aware):",
    ]
    for n, c in report.field_count_distribution.items():
        tag = "" if n in KNOWN_GOOD_FIELD_COUNTS else "  <-- ANOMALOUS"
        lines.append(f"    {n} fields: {c} rows{tag}")
    if report.anomalous_rows:
        lines.append(f"  anomalous rows ({len(report.anomalous_rows)}):")
        for line_no, n, vid in report.anomalous_rows[:20]:
            lines.append(f"    line {line_no}: {n} fields (video_id={vid!r})")
    else:
        lines.append("  integrity: OK (no anomalous rows)")
    if not report.header_is_canonical:
        lines.append(
            "  note: header drift is benign — the positional loader reads "
            "hold_at_3s/traffic_source_shorts_pct correctly. Header rewrite is "
            "optional and operator-gated (learning.repair_csv_header)."
        )
    return "\n".join(lines)


__all__ = [
    "CANONICAL_ANALYTICS_HEADER",
    "LEGACY_ANALYTICS_HEADER",
    "KNOWN_GOOD_FIELD_COUNTS",
    "CsvHealthReport",
    "audit_analytics_csv",
    "load_latest_analytics_rows",
    "format_report",
]
