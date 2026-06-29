"""Long-form landscape asset mixer — produces the visual beats for a deep-dive video.

Runs in the PRODUCTION pipeline venv (PIL + ffmpeg, NO diffusers). For AI stills it
shells out to the ISOLATED image-gen venv worker (`C:\\ContentOps\\_imagegen\\gen_stills.py`)
in a single batch — exactly as the pipeline already shells out to ffmpeg. Each still gets
FFmpeg `zoompan` Ken-Burns motion so a held shot lives 4-8s without feeling static.

Sources (MVP): (1) local SDXL-Turbo stills + Ken-Burns [primary], (2) generated diagram
cards via Pillow [for `diagram —` cues]. Landscape stock + screen-capture are Phase-2
additions to `_route_source`. The point of going SDXL-primary is to dodge the 300-clip
stock rate-limit wall that makes a fast-cut long-form video infeasible on free stock tiers.

Output: a list of Beat dicts {index, kind, clip, duration_s, prompt} + a manifest.json the
long-form `render_master` path concatenates. This module NEVER touches the Shorts path.
"""
from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

log = logging.getLogger(__name__)

# Defaults (overridable via config["longform"]); kept here so the module is usable standalone.
DEFAULT_IMAGEGEN_PY = r"C:\ContentOps\_imagegen\venv\Scripts\python.exe"  # machine-specific venv (config-overridable)
DEFAULT_GEN_STILLS = str(Path(__file__).resolve().parent / "gen_stills.py")  # repo-local worker
STILL_W, STILL_H = 1024, 576           # SDXL native-ish 16:9; upscaled to render res by ffmpeg
OUT_W, OUT_H, FPS = 1920, 1080, 30
STYLE_SUFFIX = (", cinematic wide shot, photorealistic, soft cinematic lighting, "
                "shallow depth of field, high detail, 16:9")


@dataclass
class Beat:
    index: int
    kind: str          # "sdxl" | "diagram"
    clip: str          # path to the Ken-Burns landscape clip
    duration_s: float
    prompt: str


def _imagegen_paths(config: dict | None) -> tuple[str, str]:
    lf = (config or {}).get("longform", {}) if config else {}
    return (lf.get("imagegen_python", DEFAULT_IMAGEGEN_PY),
            lf.get("gen_stills_script", DEFAULT_GEN_STILLS))


def _route_source(cue: str) -> str:
    c = cue.strip().lower()
    if c.startswith("diagram") or "diagram —" in c or "diagram -" in c:
        return "diagram"
    return "sdxl"


def _enhance_prompt(cue: str) -> str:
    return cue.strip().rstrip(".") + STYLE_SUFFIX


def _distribute_durations(n: int, total_s: float) -> list[float]:
    """Even split so the beats sum EXACTLY to total_s (the VO length).

    Held-shot length (~4-8s) is governed by the script's B-ROLL cue density
    (~100-150 cues over 10-12 min ≈ 4-5s/beat), NOT clamped here: clamping breaks the
    sum when a script is over-cued (beats overflow past the VO and the `-t vo_dur`
    render trims the tail) or under-cued. Full VO coverage wins over the soft target.
    """
    if n <= 0:
        return []
    each = round(total_s / n, 3)
    durs = [each] * n
    durs[-1] = round(durs[-1] + (total_s - sum(durs)), 3)  # absorb rounding drift
    return durs


def _font(size: int):
    for p in (r"C:\Windows\Fonts\arialbd.ttf", r"C:\Windows\Fonts\arial.ttf"):
        if Path(p).exists():
            return ImageFont.truetype(p, size)
    return ImageFont.load_default()


def _make_diagram(text: str, out: Path, w: int = STILL_W, h: int = STILL_H) -> Path:
    """A simple dark title/diagram card (Pillow) — rate-limit-free, for `diagram —` cues."""
    img = Image.new("RGB", (w, h), (10, 12, 24))
    d = ImageDraw.Draw(img)
    for y in range(h):  # subtle vertical gradient
        d.line([(0, y), (w, y)], fill=(10, 12 + y * 14 // h, 24 + y * 30 // h))
    label = text.split("—", 1)[-1].strip() if "—" in text else text
    label = label[:80]
    f = _font(56)
    words, lines, cur = label.split(), [], ""
    for word in words:
        if len(cur + " " + word) > 26:
            lines.append(cur.strip()); cur = word
        else:
            cur += " " + word
    lines.append(cur.strip())
    y = h // 2 - len(lines) * 38
    for ln in lines:
        bb = d.textbbox((0, 0), ln, font=f)
        d.text(((w - (bb[2] - bb[0])) / 2, y), ln, font=f, fill=(225, 230, 245))
        y += 76
    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out)
    return out


def _gen_stills_batch(specs: list[dict], config: dict | None) -> None:
    """Batch-generate SDXL stills by shelling out to the isolated-venv worker."""
    if not specs:
        return
    py, script = _imagegen_paths(config)
    if not Path(py).exists():
        raise FileNotFoundError(f"image-gen venv python not found: {py}")
    work = Path(specs[0]["out"]).parent
    spec_path = work / "_sdxl_spec.json"
    spec_path.write_text(json.dumps(specs), encoding="utf-8")
    cmd = [py, script, "--spec", str(spec_path),
           "--width", str(STILL_W), "--height", str(STILL_H), "--steps", "3"]
    log.info("longform: generating %d SDXL stills via %s", len(specs), py)
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode not in (0, 1):  # 1 = some near-black, tolerated
        raise RuntimeError(
            f"gen_stills.py failed (exit {proc.returncode}):\n{proc.stderr[-1500:]}")
    log.info("longform: still batch done\n%s", proc.stdout[-800:])


def _kenburns(still: Path, out: Path, dur: float, idx: int) -> Path:
    """FFmpeg zoompan Ken-Burns: alternate slow zoom-in / horizontal pan per beat."""
    frames = max(1, int(round(dur * FPS)))
    if idx % 2 == 0:
        z, x, y = "min(zoom+0.0010,1.12)", "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"
    else:
        z, x, y = "1.10", f"(iw-iw/zoom)*on/{frames}", "ih/2-(ih/zoom/2)"
    vf = (f"scale={OUT_W}:{OUT_H}:force_original_aspect_ratio=increase,"
          f"crop={OUT_W}:{OUT_H},zoompan=z='{z}':d={frames}:x='{x}':y='{y}':"
          f"s={OUT_W}x{OUT_H}:fps={FPS}")
    out.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["ffmpeg", "-y", "-loop", "1", "-i", str(still), "-t", f"{dur:.3f}",
           "-vf", vf, "-r", str(FPS), "-c:v", "libx264", "-pix_fmt", "yuv420p",
           "-preset", "medium", str(out)]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg Ken-Burns failed for {still.name}:\n{proc.stderr[-1200:]}")
    return out


def produce_beats(cues: list[str], total_duration_s: float, work_dir: str | Path,
                  config: dict | None = None) -> list[dict]:
    """Turn script B-roll cues into landscape Ken-Burns beat clips + a manifest.

    Returns a list of Beat dicts; also writes <work_dir>/manifest.json. The long-form
    render path concatenates `beat["clip"]` in order, then overlays captions + muxes VO.
    """
    work = Path(work_dir)
    stills_dir = work / "stills"
    clips_dir = work / "clips"
    cues = [c.strip() for c in cues if c and c.strip()]
    if not cues:
        raise ValueError("produce_beats: no B-roll cues provided")
    durations = _distribute_durations(len(cues), total_duration_s)

    # 1. route + collect SDXL specs / make diagrams
    routing = [_route_source(c) for c in cues]
    sdxl_specs = []
    still_for = {}
    for i, (cue, kind) in enumerate(zip(cues, routing)):
        still = stills_dir / f"beat_{i:03d}.png"
        still_for[i] = still
        if kind == "sdxl":
            sdxl_specs.append({"prompt": _enhance_prompt(cue), "out": str(still)})
        else:
            _make_diagram(cue, still)

    # 2. one batched SDXL call (load model once)
    _gen_stills_batch(sdxl_specs, config)

    # 3. Ken-Burns each still -> beat clip
    beats: list[dict] = []
    for i, (cue, kind, dur) in enumerate(zip(cues, routing, durations)):
        still = still_for[i]
        if not still.exists():
            log.warning("longform: still missing for beat %d (%s); skipping", i, cue[:40])
            continue
        clip = _kenburns(still, clips_dir / f"beat_{i:03d}.mp4", dur, i)
        beats.append(asdict(Beat(index=i, kind=kind, clip=str(clip),
                                 duration_s=dur, prompt=cue)))

    (work / "manifest.json").write_text(json.dumps(beats, indent=2), encoding="utf-8")
    log.info("longform: produced %d beats (%.1fs total)", len(beats),
             sum(b["duration_s"] for b in beats))
    return beats


if __name__ == "__main__":  # standalone smoke: python longform_assets.py cues.txt 60 outdir
    import sys
    logging.basicConfig(level=logging.INFO)
    cues_file, total, outdir = sys.argv[1], float(sys.argv[2]), sys.argv[3]
    lines = [ln for ln in Path(cues_file).read_text(encoding="utf-8").splitlines() if ln.strip()]
    produce_beats(lines, total, outdir)
