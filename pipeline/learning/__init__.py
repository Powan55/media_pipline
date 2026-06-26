"""ShadowVerse self-improving feedback loop.

A fail-soft, stdlib-only package that closes the loop between published-video
analytics and the pipeline's tunable knobs:

    capture/repair -> build ledger -> maturity gate -> evaluate experiments
    (revert losers) -> analyze (reach-first) -> classify -> apply <=1 SAFE knob
    + queue PROPOSE -> write dashboard + proposals.

Design rules (non-negotiable — see Documents/Project plan):
  * NEVER raises into the pipeline. Every entrypoint is fail-soft: on error it
    logs and returns a no-op result, so a learning bug can't block a video.
  * Core is deterministic Python statistics over data the pipeline already
    writes — no paid API, no agent framework.
  * Auto-applies ONLY reversible, non-sacred config.yaml knobs within bounded
    deltas, and auto-reverts on regression. Sacred gates and scoring-component
    weights are PROPOSE-only (operator approval via the weekly review).
"""

__all__ = ["__version__"]

__version__ = "0.1.0"
