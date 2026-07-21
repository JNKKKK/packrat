"""Decode smoke test (§9.1) — "decode is the gate".

Runs one real sample of every allowlisted extension (plus the RAW group) through
the ``decode → hash → perceptual → embed`` path and reports, per file, which
stages succeeded. This is the only check that truly resolves the ⚠ cells in the
§9.1 matrix — whether *this* Windows wheel of ``pillow-heif`` / ``rawpy`` /
``pdqhash`` handles *your* camera's CR3 or *that* AVIF encoder's output.

Design (matches §9.1 "decode is the gate"):
- **Hash (BLAKE3)** is format-agnostic — it hashes raw bytes, so it runs on every
  file including ones that won't decode.
- **Perceptual (PDQ)** and **embed (CLIP)** both gate on a *decoded RGB array*.
  We decode once per file (photo → still, video → sampled frames) and feed the
  same array to PDQ and (optionally) CLIP.
- **Metadata** (exiftool/ffprobe) is independent and best-effort.

Optional deps are imported lazily; a missing dep is reported as ``skipped
(dep missing)`` rather than crashing, so the harness is useful even before the
full ``media`` extra is installed. The embed stage is skipped unless the
``embed`` extra + a CUDA/CPU torch is present (it's opt-in, §7).

**Sample generation.** :func:`generate_samples` synthesizes one file per photo
and video extension in-memory (a detailed RGB gradient re-saved in each container;
a short shifting-color clip per video codec) so the harness needs no hand-gathered
fixtures. RAW formats can't be synthesized — supply real camera files to test
those. This is exactly what proves the ⚠ cells (does *this* wheel decode *this*
encoder's output), which is why the generator lives beside the test that consumes
its output.

Usage::

    packrat smoke-test                        # dry inventory of what deps are available
    packrat smoke-test path\\to\\samples     # run over one file per extension in that folder
    packrat smoke-test --generate             # synthesize samples in a temp dir, then run
    packrat smoke-test path\\to\\out --generate  # synthesize into that dir, then run
"""

from __future__ import annotations

import json
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .config import PHOTO_EXTS, RAW_EXTS, VIDEO_EXTS

# The stages we report per sample.
STAGES = ("decode", "hash", "perceptual", "metadata", "embed")


@dataclass
class SampleResult:
    path: str
    ext: str
    kind: str  # photo|video|raw|unknown
    stages: dict[str, str] = field(default_factory=dict)  # stage -> ok|fail|skip
    detail: dict[str, str] = field(default_factory=dict)  # stage -> message


# ---------------------------------------------------------------------------
# dependency probing
# ---------------------------------------------------------------------------
def _probe_deps() -> dict[str, bool]:
    deps = {}
    for name, mod in [
        ("blake3", "blake3"),
        ("numpy", "numpy"),
        ("pillow", "PIL"),
        ("pillow-heif", "pillow_heif"),
        ("pyav", "av"),
        ("pdqhash", "pdqhash"),
        ("rawpy", "rawpy"),
        ("exiftool", "exiftool"),
        ("torch", "torch"),
        ("open_clip", "open_clip"),
    ]:
        try:
            __import__(mod)
            deps[name] = True
        except Exception:
            deps[name] = False
    return deps


def _classify(ext: str) -> str:
    e = ext.lower().lstrip(".")
    if e in PHOTO_EXTS:
        return "photo"
    if e in VIDEO_EXTS:
        return "video"
    if e in RAW_EXTS:
        return "raw"
    return "unknown"


# ---------------------------------------------------------------------------
# sample generation (synthesizes fixtures so the harness is self-contained)
# ---------------------------------------------------------------------------
# ext -> Pillow save format. RAW is intentionally absent: RAW containers can't
# be synthesized (they wrap sensor data + a maker-specific preview), so those
# rows need a real camera file.
_PHOTO_SAVE = {
    "jpg": "JPEG", "jpeg": "JPEG", "jfif": "JPEG", "png": "PNG", "gif": "GIF",
    "bmp": "BMP", "tif": "TIFF", "tiff": "TIFF", "webp": "WEBP",
    "heic": "HEIF", "heif": "HEIF", "avif": "AVIF",
}
# ext -> (codec, container format) for PyAV. Each codec is one the container
# realistically carries, chosen to stay within ffmpeg's built-in encoders.
_VIDEO_ENCODE = {
    "mp4": ("libx264", "mp4"), "m4v": ("libx264", "mp4"), "mov": ("libx264", "mov"),
    "avi": ("mpeg4", "avi"), "mkv": ("libx264", "matroska"), "webm": ("libvpx", "webm"),
    "wmv": ("wmv2", "asf"), "flv": ("flv", "flv"), "mpg": ("mpeg2video", "mpeg"),
    "mpeg": ("mpeg2video", "mpeg"), "m2ts": ("libx264", "mpegts"),
    "mts": ("libx264", "mpegts"), "3gp": ("libx264", "3gp"),
}


def _gradient_image(size: int = 256):
    """A detailed RGB gradient — varied pixels so PDQ yields a meaningful hash.

    A flat/near-black image produces a low-quality, spuriously-colliding PDQ
    signature (the §5.3 quality caveat), so the fixture deliberately has detail.
    """
    import numpy as np
    from PIL import Image

    arr = np.zeros((size, size, 3), dtype=np.uint8)
    xs = np.arange(size, dtype=np.uint16)
    arr[:, :, 0] = xs[None, :]                       # red ramps across x
    arr[:, :, 1] = xs[:, None]                        # green ramps down y
    arr[:, :, 2] = ((xs[None, :] * xs[:, None]) // size).astype(np.uint8)
    return Image.fromarray(arr)


def _write_video(path: Path, codec: str, fmt: str, n_frames: int = 15) -> None:
    """Encode a short clip of shifting solid-color frames via PyAV."""
    import av  # type: ignore
    import numpy as np

    container = av.open(str(path), "w", format=fmt)
    try:
        stream = container.add_stream(codec, rate=15)
        stream.width, stream.height, stream.pix_fmt = 128, 128, "yuv420p"
        for i in range(n_frames):
            f = np.full((128, 128, 3), (i * 15) % 256, dtype=np.uint8)
            f[:, :, 1] = (i * 7) % 256
            frame = av.VideoFrame.from_ndarray(f, format="rgb24")
            for pkt in stream.encode(frame):
                container.mux(pkt)
        for pkt in stream.encode():  # flush
            container.mux(pkt)
    finally:
        container.close()


def generate_samples(out_dir: str | Path) -> dict:
    """Synthesize one sample file per (non-RAW) allowlisted extension.

    Returns a summary ``{"photos": [...], "videos": [...], "failed": {ext: err}}``.
    Photo/video deps are imported lazily; if they're missing the relevant group
    is reported as failed rather than raising, so ``--generate`` degrades like the
    rest of the harness.
    """
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    summary: dict = {"photos": [], "videos": [], "failed": {}}

    # --- photos ---
    try:
        import pillow_heif  # type: ignore

        pillow_heif.register_heif_opener()  # enables HEIC + AVIF save/open
    except Exception:
        pass  # non-HEIF formats still work via plain Pillow
    try:
        img = _gradient_image()
    except Exception as exc:
        for ext in _PHOTO_SAVE:
            summary["failed"][ext] = f"pillow/numpy missing: {exc}"
        img = None
    if img is not None:
        for ext, fmt in _PHOTO_SAVE.items():
            try:
                img.save(out / f"sample.{ext}", format=fmt)
                summary["photos"].append(ext)
            except Exception as exc:
                summary["failed"][ext] = str(exc)

    # --- videos ---
    for ext, (codec, fmt) in _VIDEO_ENCODE.items():
        try:
            _write_video(out / f"sample.{ext}", codec, fmt)
            summary["videos"].append(ext)
        except Exception as exc:
            summary["failed"][ext] = str(exc)[:120]

    summary["photos"].sort()
    summary["videos"].sort()
    return summary


# ---------------------------------------------------------------------------
# per-stage runners (all lazy-import their deps)
# ---------------------------------------------------------------------------
def _run_hash(path: Path, res: SampleResult) -> None:
    try:
        from blake3 import blake3  # type: ignore
    except Exception as exc:
        res.stages["hash"] = "skip"
        res.detail["hash"] = f"blake3 missing: {exc}"
        return
    try:
        h = blake3()
        with path.open("rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        res.stages["hash"] = "ok"
        res.detail["hash"] = h.hexdigest()[:16] + "…"
    except Exception as exc:
        res.stages["hash"] = "fail"
        res.detail["hash"] = str(exc)


def _decode_image(path: Path):
    """Return an RGB numpy array for a still, or raise. Handles HEIC/AVIF/RAW."""
    import numpy as np

    kind = _classify(path.suffix)
    if kind == "raw":
        import rawpy  # type: ignore

        with rawpy.imread(str(path)) as raw:
            try:
                # Prefer the embedded preview (fast, matches viewers — §9.1).
                thumb = raw.extract_thumb()
                if thumb.format == rawpy.ThumbFormat.JPEG:
                    from io import BytesIO

                    from PIL import Image

                    img = Image.open(BytesIO(thumb.data)).convert("RGB")
                    return np.asarray(img)
            except Exception:
                pass
            rgb = raw.postprocess()
            return np.asarray(rgb)

    # Register HEIF/AVIF openers if available.
    try:
        import pillow_heif  # type: ignore

        pillow_heif.register_heif_opener()
        try:
            pillow_heif.register_avif_opener()
        except Exception:
            pass
    except Exception:
        pass

    from PIL import Image

    img = Image.open(path)
    img.seek(0) if getattr(img, "is_animated", False) else None  # first frame
    return np.asarray(img.convert("RGB"))


def _decode_video_frames(path: Path, n: int = 4):
    """Return a list of RGB numpy frames sampled across the timeline."""
    import av  # type: ignore
    import numpy as np

    frames = []
    with av.open(str(path)) as container:
        stream = container.streams.video[0]
        stream.thread_type = "AUTO"
        # Sample the first few decoded frames (a real scan samples by timeline;
        # the smoke test just needs to prove the codec decodes to RGB).
        for i, frame in enumerate(container.decode(stream)):
            frames.append(np.asarray(frame.to_image().convert("RGB")))
            if len(frames) >= n:
                break
    return frames


def _run_decode_and_perceptual(path: Path, res: SampleResult) -> None:
    import importlib.util

    if importlib.util.find_spec("numpy") is None:
        res.stages["decode"] = "skip"
        res.stages["perceptual"] = "skip"
        res.detail["decode"] = "numpy missing"
        return

    arrays = []
    try:
        if res.kind == "video":
            arrays = _decode_video_frames(path)
            if not arrays:
                raise RuntimeError("no frames decoded")
        else:
            arrays = [_decode_image(path)]
        res.stages["decode"] = "ok"
        res.detail["decode"] = f"{len(arrays)} frame(s), shape {arrays[0].shape}"
    except Exception as exc:
        res.stages["decode"] = "fail"
        res.detail["decode"] = str(exc)
        res.stages["perceptual"] = "skip"
        res.detail["perceptual"] = "no decoded pixels"
        res._arrays = []  # type: ignore[attr-defined]
        return

    res._arrays = arrays  # type: ignore[attr-defined]

    # PDQ over the decoded array(s).
    try:
        import pdqhash  # type: ignore

        h, quality = pdqhash.compute(arrays[0])
        res.stages["perceptual"] = "ok"
        res.detail["perceptual"] = f"pdq quality={quality}"
    except Exception as exc:
        res.stages["perceptual"] = "fail" if res.stages["decode"] == "ok" else "skip"
        res.detail["perceptual"] = str(exc)


def _run_metadata(path: Path, res: SampleResult) -> None:
    try:
        import exiftool  # type: ignore
    except Exception as exc:
        res.stages["metadata"] = "skip"
        res.detail["metadata"] = f"pyexiftool missing: {exc}"
        return
    try:
        with exiftool.ExifToolHelper() as et:
            meta = et.get_metadata(str(path))[0]
        res.stages["metadata"] = "ok"
        res.detail["metadata"] = f"{len(meta)} tags"
    except Exception as exc:
        # A missing exiftool *binary* is a not-installed dep, not a decode
        # failure — report it as 'skip' so it never trips the hard-fail exit.
        msg = str(exc)
        if "not found" in msg.lower() or "no such file" in msg.lower():
            res.stages["metadata"] = "skip"
            res.detail["metadata"] = "exiftool binary not on PATH"
        else:
            res.stages["metadata"] = "fail"
            res.detail["metadata"] = msg


def _run_embed(res: SampleResult, model_cache: dict) -> None:
    arrays = getattr(res, "_arrays", None)
    if not arrays:
        res.stages["embed"] = "skip"
        res.detail["embed"] = "no decoded pixels"
        return
    try:
        import open_clip  # type: ignore
        import torch  # type: ignore
        from PIL import Image
    except Exception as exc:
        res.stages["embed"] = "skip"
        res.detail["embed"] = f"embed extra missing: {exc}"
        return
    try:
        if "model" not in model_cache:
            model, _, preprocess = open_clip.create_model_and_transforms(
                "ViT-B-32", pretrained="openai"
            )
            model.eval()
            model_cache["model"] = model
            model_cache["preprocess"] = preprocess
        model = model_cache["model"]
        preprocess = model_cache["preprocess"]
        img = Image.fromarray(arrays[0])
        tensor = preprocess(img).unsqueeze(0)
        with torch.no_grad():
            vec = model.encode_image(tensor)
        res.stages["embed"] = "ok"
        res.detail["embed"] = f"dim={vec.shape[-1]}"
    except Exception as exc:
        res.stages["embed"] = "fail"
        res.detail["embed"] = str(exc)


# ---------------------------------------------------------------------------
# driver
# ---------------------------------------------------------------------------
def run_smoke_test(
    samples_dir: str | None, *, json_out: bool = False, generate: bool = False
) -> int:
    """Run the smoke test; return a process exit code (0 = no hard failures).

    With ``generate=True``, synthesize samples first (into ``samples_dir`` if
    given, else a temp dir) and then run over them — a one-command self-test.
    """
    import tempfile

    deps = _probe_deps()

    if generate:
        target = samples_dir or tempfile.mkdtemp(prefix="packrat-smoke-")
        summary = generate_samples(target)
        if not json_out:
            n = len(summary["photos"]) + len(summary["videos"])
            print(f"generated {n} sample(s) in {target}")
            if summary["failed"]:
                for ext, err in summary["failed"].items():
                    print(f"  ⚠ could not generate .{ext}: {err}")
            print()
        samples_dir = target
    elif not samples_dir:
        _report_inventory(deps, json_out)
        return 0

    root = Path(samples_dir)
    if not root.is_dir():
        print(f"not a folder: {samples_dir}", file=sys.stderr)
        return 2

    all_exts = sorted(PHOTO_EXTS | VIDEO_EXTS | RAW_EXTS)
    files = [p for p in sorted(root.rglob("*")) if p.is_file() and p.suffix.lower().lstrip(".") in all_exts]

    results: list[SampleResult] = []
    model_cache: dict = {}
    for path in files:
        res = SampleResult(path=str(path), ext=path.suffix.lower().lstrip("."), kind=_classify(path.suffix))
        _run_hash(path, res)
        _run_decode_and_perceptual(path, res)
        _run_metadata(path, res)
        _run_embed(res, model_cache)
        results.append(res)

    return _report_results(results, deps, all_exts, files, json_out)


def _report_inventory(deps: dict, json_out: bool) -> None:
    if json_out:
        print(json.dumps({"deps": deps}, indent=2))
        return
    print("packrat decode smoke test (§9.1)")
    print("no samples folder given — reporting dependency availability only.\n")
    for name, ok in deps.items():
        print(f"  {'✅' if ok else '❌'} {name}")
    print("\nprovide a folder with one sample per extension to run the full path:")
    print("  packrat smoke-test path\\to\\samples")


def _report_results(results, deps, all_exts, files, json_out) -> int:
    if json_out:
        payload = {
            "deps": deps,
            "results": [
                {**{k: v for k, v in asdict(r).items() if not k.startswith("_")}}
                for r in results
            ],
        }
        print(json.dumps(payload, indent=2))
    else:
        print("packrat decode smoke test (§9.1)\n")
        print(f"deps: " + "  ".join(f"{'✅' if v else '❌'}{k}" for k, v in deps.items()))
        print()
        header = f"{'file':40s} {'kind':6s} " + " ".join(f"{s:10s}" for s in STAGES)
        print(header)
        print("-" * len(header))
        for r in results:
            cells = " ".join(f"{_mark(r.stages.get(s, '?')):10s}" for s in STAGES)
            name = Path(r.path).name
            print(f"{name[:40]:40s} {r.kind:6s} {cells}")
        # Coverage: which allowlisted extensions had no sample?
        seen = {r.ext for r in results}
        missing = [e for e in all_exts if e not in seen]
        if missing:
            print(f"\nno sample for: {', '.join(missing)}")
        # Highlight the ⚠ cells the test exists to resolve.
        print("\n⚠ cells to confirm (§9.1): avif, heic/heif, RAW (esp. cr3), and the pdqhash wheel.")

    # Exit non-zero if hash/decode/PERCEPTUAL hard-FAILED on a present sample. A 'skip'
    # (a missing optional dep) is NOT a failure. Perceptual is included because a broken
    # `pdqhash` wheel that crashes on decoded pixels (fail, not skip) is exactly a ⚠ cell
    # this gate exists to catch (§9.1) — "non-zero on any format failure" (§11). Embed
    # stays advisory (opt-in, §7) and never gates the exit code.
    hard_fail = any(
        r.stages.get(stage) == "fail"
        for r in results for stage in ("hash", "decode", "perceptual")
    )
    return 1 if hard_fail else 0


def _mark(state: str) -> str:
    return {"ok": "✅ok", "fail": "❌fail", "skip": "· skip"}.get(state, "?")
