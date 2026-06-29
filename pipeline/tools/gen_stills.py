"""SDXL-Turbo batch still generator — runs in the ISOLATED image-gen venv.

The production pipeline (which has no diffusers) shells out to this worker via
`C:\\ContentOps\\_imagegen\\venv\\Scripts\\python.exe gen_stills.py --spec <json>`,
exactly as it already shells out to ffmpeg. Loads the model ONCE and generates a
whole video's stills in a single batch (amortizing the cold load over ~120 images).

Spec JSON: [{"prompt": "...", "out": "C:\\...\\beat_007.png"}, ...]

Proven on a 6 GB RTX 4050: ~5 GB peak VRAM with model-CPU-offload, ~7-9 s/image at
1024x576. The fp16-fix VAE (madebyollin/sdxl-vae-fp16-fix) is loaded so the fp16
SDXL VAE does NOT produce the known black/NaN frames.
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path

# keep weights off OneDrive, beside the other model weights
os.environ.setdefault("HF_HOME", r"C:\ContentOps\_models\hf")

import numpy as np  # noqa: E402
import torch  # noqa: E402
from diffusers import AutoencoderKL, AutoPipelineForText2Image  # noqa: E402

MODEL = "stabilityai/sdxl-turbo"
VAE_FIX = "madebyollin/sdxl-vae-fp16-fix"  # keeps the fp16 VAE from going black


def load_pipe(model: str):
    t0 = time.perf_counter()
    vae = AutoencoderKL.from_pretrained(VAE_FIX, torch_dtype=torch.float16)
    try:
        pipe = AutoPipelineForText2Image.from_pretrained(
            model, vae=vae, torch_dtype=torch.float16, variant="fp16")
    except Exception:
        pipe = AutoPipelineForText2Image.from_pretrained(
            model, vae=vae, torch_dtype=torch.float16)
    pipe.enable_model_cpu_offload()  # fits 6 GB; ~5 GB peak
    pipe.set_progress_bar_config(disable=True)
    print(f"load {time.perf_counter() - t0:.1f}s", flush=True)
    return pipe


def main(argv=None):
    ap = argparse.ArgumentParser(description="Batch SDXL-Turbo landscape still generator.")
    ap.add_argument("--spec", required=True, help="JSON list of {prompt, out} objects.")
    ap.add_argument("--width", type=int, default=1024)
    ap.add_argument("--height", type=int, default=576)  # 16:9
    ap.add_argument("--steps", type=int, default=3)      # SDXL-Turbo: 1-4
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--model", default=MODEL)
    args = ap.parse_args(argv)

    spec = json.loads(Path(args.spec).read_text(encoding="utf-8"))
    if not isinstance(spec, list) or not spec:
        print("ERROR: spec must be a non-empty JSON list", file=sys.stderr)
        return 2

    pipe = load_pipe(args.model)
    gen = torch.Generator(device="cuda").manual_seed(args.seed)
    n_black = 0
    t_all = time.perf_counter()
    for i, item in enumerate(spec):
        out = Path(item["out"])
        out.parent.mkdir(parents=True, exist_ok=True)
        ts = time.perf_counter()
        img = pipe(prompt=item["prompt"], num_inference_steps=args.steps,
                   guidance_scale=0.0, height=args.height, width=args.width,
                   generator=gen).images[0]
        std = float(np.asarray(img).std())
        if std < 5:
            n_black += 1
            print(f"  WARN near-black std={std:.1f} on '{item['prompt'][:40]}'", flush=True)
        img.save(out)
        print(f"  [{i+1}/{len(spec)}] {time.perf_counter()-ts:.1f}s std={std:.0f} -> {out.name}",
              flush=True)
    peak = torch.cuda.max_memory_allocated() / 1024**3 if torch.cuda.is_available() else 0
    print(f"DONE {len(spec)} stills in {time.perf_counter()-t_all:.1f}s | "
          f"peak VRAM {peak:.2f} GB | black={n_black}", flush=True)
    return 1 if n_black else 0


if __name__ == "__main__":
    sys.exit(main())
