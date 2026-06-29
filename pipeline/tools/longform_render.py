"""Long-form (16:9) render path — invoked by pipeline.render_master when is_longform.

Unlike the Shorts render (which times a handful of fetched stock clips against the
VO), the long-form render builds its visual beats AT RENDER TIME — once the VO
duration is known — via tools.longform_assets.produce_beats (SDXL stills + diagrams
→ Ken-Burns clips summing to the VO length). Then one ffmpeg pass concatenates the
beats, burns the lower-third captions, and muxes the (already loudnormed) VO.

Writes the master to the SAME path pipeline._master_output_path returns, via an
atomic `.part` → os.replace, so the RenderLock / Stage-10.1 integrity / skip-guard
machinery wraps it unchanged. Respects force_encoder for the NVENC→libx264 retry.
"""
from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)


def _select_encoder(config: dict, force_encoder: str | None) -> str:
    if force_encoder == "libx264":
        return "libx264"
    if config["render"].get("hardware_accel") == "nvenc":
        return "h264_nvenc"
    return config["render"].get("video_codec", "libx264")


def render_master_longform(
    script,
    vo_path: Path,
    captions_path: Path,
    config: dict,
    out_path: Path,
    *,
    force_encoder: str | None = None,
) -> Path:
    """Render the long-form master. Returns out_path (atomic-promoted on success)."""
    import ffmpeg  # production venv has ffmpeg-python

    from tools.longform_assets import produce_beats

    if not vo_path.exists():
        raise FileNotFoundError(f"VO audio not found: {vo_path}")
    if not captions_path.exists():
        raise FileNotFoundError(f"Captions ASS not found: {captions_path}")

    # 1. VO duration is the master length (the spine).
    vo_dur = float(ffmpeg.probe(str(vo_path))["format"]["duration"])

    # 2. Build the visual beats now that we know the duration.
    cues = [c for c in (script.broll_cues or []) if c and str(c).strip()]
    if not cues:
        # Defensive: a long-form script should always carry B-ROLL cues, but never
        # deadlock the render — fall back to a few generic conceptual beats.
        cues = ["abstract artificial intelligence concept, glowing network, cinematic"] * 6
        log.warning("render(longform): script had no B-ROLL cues; using %d fallback beats", len(cues))
    work = Path(config["paths"]["channel_root"]) / "04_renders" / "_wip" / script.topic_id / "lf_assets"
    beats = produce_beats(cues, vo_dur, work, config)
    if not beats:
        raise RuntimeError("render_master_longform: produce_beats returned no beats")

    # 3. Concat list (absolute beat paths).
    concat_list = work / "render_concat.txt"
    concat_list.write_text(
        "".join(f"file '{Path(b['clip']).as_posix()}'\n" for b in beats), encoding="utf-8"
    )

    res = config["render"]["resolution"]
    w, h = int(res[0]), int(res[1])
    fps = int(config["render"]["framerate"])
    bitrate_k = int(config["render"]["bitrate_kbps"])
    vcodec = _select_encoder(config, force_encoder)

    part = out_path.with_name(out_path.stem + ".part.mp4")
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # 4. One pass: concat beats (video) + VO (audio), scale/crop to render res, burn
    #    the lower-third ASS, encode. The VO is already loudnormed (Stage 7.5), so no
    #    re-normalize here. `-t vo_dur` forces the master to exactly the VO length.
    #    Windows ASS-path workaround: cwd = captions dir, pass the bare basename to the
    #    `ass` filter so its drive-letter colon doesn't break the filtergraph parser.
    vf = (f"scale={w}:{h}:force_original_aspect_ratio=increase,"
          f"crop={w}:{h},ass={captions_path.name}")

    def _encode(codec: str) -> subprocess.CompletedProcess:
        if part.exists():
            part.unlink()
        cmd = [
            "ffmpeg", "-y",
            "-f", "concat", "-safe", "0", "-i", str(concat_list),
            "-i", str(vo_path),
            "-vf", vf,
            "-map", "0:v", "-map", "1:a", "-t", f"{vo_dur:.3f}",
            "-r", str(fps), "-c:v", codec, "-b:v", f"{bitrate_k}k", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-ac", "2",
            str(part),
        ]
        log.info("render(longform): %d beats, vo=%.1fs, encoder=%s -> %s",
                 len(beats), vo_dur, codec, out_path.name)
        return subprocess.run(cmd, cwd=str(captions_path.parent), capture_output=True, text=True)

    proc = _encode(vcodec)
    if proc.returncode != 0 and vcodec == "h264_nvenc" and force_encoder is None:
        # NVENC can fail to OPEN (driver/API mismatch, not silent corruption), which the
        # integrity-retry wrapper does NOT catch — fall back to libx264 here so the render
        # still completes (mirrors the Shorts NVENC->libx264 resilience).
        log.warning("render(longform): h264_nvenc failed to open; falling back to libx264\n%s",
                    (proc.stderr or "")[-800:])
        vcodec = "libx264"
        proc = _encode(vcodec)
    if proc.returncode != 0:
        raise RuntimeError(
            f"render_master_longform ffmpeg failed (encoder={vcodec}):\n{(proc.stderr or '')[-2000:]}"
        )
    os.replace(part, out_path)
    log.info("render(longform): master written %s (encoder=%s)", out_path, vcodec)
    return out_path
