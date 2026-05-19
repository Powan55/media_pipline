"""Unit tests for T8 pipeline wiring.

Covers the four discrete acceptance criteria from the T8 spec:

  1. `pipeline` imports cleanly with `elevenlabs` not installed.
  2. `config.yaml.template` parses as YAML and exposes all new keys at the
     expected paths with the operator-safe defaults.
  3. `render_master`'s output kwargs include `"ac": 2` (R7 stereo upmix).
  4. `IntegrityCheckFailed` subclasses `PipelineHalted` so `main()`'s halt
     handler picks it up identically to the existing halts.

Run:
    C:/ContentOps/_pipeline/.venv/Scripts/python.exe -m pytest \\
        tests/test_pipeline_wiring.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def test_pipeline_imports_without_elevenlabs() -> None:
    """`pipeline` must import even when the `elevenlabs` SDK is absent.

    The dispatch branch in `generate_voiceover` lazy-imports
    `tools.tts_elevenlabs.synthesize`; the module itself lazy-imports the
    actual SDK only inside `synthesize()`. Both layers together guarantee
    pipeline import never touches elevenlabs.

    We assert the SDK is genuinely uninstalled in this venv (it should be —
    requirements.txt does not pin it) and that `pipeline` imports succeed.
    Run as a subprocess so we get a fresh interpreter — popping pipeline /
    elevenlabs from sys.modules in-process would create stale
    `_PipelineHaltedBase` references in `tools.prepublish_qa` if it was
    imported by another test in the same session.
    """
    import subprocess
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys, importlib;\n"
                "try:\n"
                "    importlib.import_module('elevenlabs');\n"
                "    elevenlabs_installed = True\n"
                "except ImportError:\n"
                "    elevenlabs_installed = False\n"
                "assert not elevenlabs_installed, 'elevenlabs unexpectedly installed'\n"
                "import pipeline\n"
                "assert hasattr(pipeline, 'run_for_topic')\n"
                "assert hasattr(pipeline, 'generate_voiceover')\n"
                "assert hasattr(pipeline, 'IntegrityCheckFailed')\n"
                "print('IMPORT_OK')\n"
            ),
        ],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, (
        f"subprocess returncode={proc.returncode}\n"
        f"stdout={proc.stdout!r}\nstderr={proc.stderr!r}"
    )
    assert "IMPORT_OK" in proc.stdout


def test_config_template_parses_with_new_keys() -> None:
    """`config.yaml.template` is valid YAML and carries every new T8 key."""
    template_path = REPO_ROOT / "config.yaml.template"
    assert template_path.exists(), f"missing template: {template_path}"
    cfg = yaml.safe_load(template_path.read_text(encoding="utf-8"))
    assert isinstance(cfg, dict)

    # captions.style + font_name
    captions = cfg.get("captions")
    assert isinstance(captions, dict), "captions block missing"
    assert captions.get("style") == "word_pop", (
        f"captions.style should default to 'word_pop' (got {captions.get('style')!r})"
    )
    assert captions.get("font_name") == "Montserrat Black", (
        f"captions.font_name should default to 'Montserrat Black' "
        f"(got {captions.get('font_name')!r})"
    )

    # loudnorm targets
    loudnorm = cfg.get("loudnorm")
    assert isinstance(loudnorm, dict), "loudnorm block missing"
    assert loudnorm.get("target_lufs") == -14.0
    assert loudnorm.get("target_tp") == -1.0
    assert loudnorm.get("target_lra") == 11.0

    # prepublish_qa
    qa = cfg.get("prepublish_qa")
    assert isinstance(qa, dict), "prepublish_qa block missing"
    assert qa.get("enabled") is True
    assert qa.get("check_cited_observation") is False

    # tts.provider stays edge-tts by default
    tts = cfg.get("tts")
    assert isinstance(tts, dict)
    assert tts.get("provider") == "edge-tts"


def test_render_master_kwargs_include_stereo_ac2() -> None:
    """R7: `render_master` and `_fade_variant` both encode with `-ac 2`.

    We grep the source rather than running ffmpeg — the assertion is purely
    about the kwargs dict literals, which is what ships to ffmpeg-python.
    """
    src = (REPO_ROOT / "pipeline.py").read_text(encoding="utf-8")

    # Locate render_master and confirm its out_kwargs literal carries `"ac": 2`.
    render_master_idx = src.find("def render_master(")
    assert render_master_idx >= 0, "render_master not found"
    fade_idx = src.find("def _fade_variant", render_master_idx)
    assert fade_idx > render_master_idx, "_fade_variant not found"
    end_idx = src.find("def generate_metadata(", fade_idx)
    assert end_idx > fade_idx

    render_master_body = src[render_master_idx:fade_idx]
    fade_variant_body = src[fade_idx:end_idx]

    assert '"ac": 2' in render_master_body, (
        "render_master out_kwargs missing '\"ac\": 2' — required by R7 stereo upmix"
    )
    # _fade_variant uses kwarg-style ac=2 inside the ffmpeg.output(...) call.
    assert "ac=2" in fade_variant_body, (
        "_fade_variant ffmpeg.output(...) missing ac=2 — required by R7 stereo across variants"
    )


def test_integrity_check_failed_subclasses_pipelinehalted() -> None:
    """`IntegrityCheckFailed` must inherit from `PipelineHalted` so the
    existing `except PipelineHalted` in `main()` catches it as a halt.
    """
    import pipeline

    assert issubclass(pipeline.IntegrityCheckFailed, pipeline.PipelineHalted)

    # Spot-check the message format — the exception should embed the path,
    # reason, and stage so the operator can act without a debugger.
    exc = pipeline.IntegrityCheckFailed(
        Path("C:/tmp/foo_master.mp4"),
        "moov atom not found",
        stage="post-master",
    )
    msg = str(exc)
    assert "INTEGRITY-GATE" in msg
    assert "foo_master.mp4" in msg
    assert "moov atom not found" in msg
    assert "post-master" in msg


def test_pipeline_qa_failed_reused_from_tools() -> None:
    """Sanity: T7's `PipelineQAFailed` is a `PipelineHalted` too — main()
    halt handler will pick it up exactly the same as our two new exceptions.

    NOTE: tools.prepublish_qa lazy-resolves the PipelineHalted base on first
    import (see `_resolve_pipeline_halted_base`). To avoid reading a stale
    base class cached against a popped pipeline module, we import pipeline
    FIRST (without popping), then import the tool — that way the base
    resolution sees the live pipeline module.
    """
    import pipeline as live_pipeline  # noqa: F401  (cached in sys.modules)
    from tools.prepublish_qa import PipelineQAFailed

    assert issubclass(PipelineQAFailed, live_pipeline.PipelineHalted)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
