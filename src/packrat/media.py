r"""Per-file fingerprinting — the pixels/bytes half of ``scan`` (§8 A2, §5, §9.1).

This module turns one file on disk into the values ``scan`` persists:
- **content hash** — BLAKE3 of the raw bytes (§5.1). Format-agnostic; the identity
  key. Always computed; never fails on a decodable-or-not file.
- **metadata** — dimensions / duration / capture time, best-effort (§8 A2 step 7).
- **perceptual signature** (M2, §5.3) — photo: one 256-bit PDQ + quality; video:
  duration + per-frame PDQ (+ quality) at ``video.sample_frames`` timeline
  midpoints. Both gate on a decoded RGB array (§9.1 "decode is the gate").
  **Transport streams (.ts/.m2ts/.mts)** need two extra robustness steps the plain
  mp4/mov path doesn't: they often report no container/stream duration (recovered by
  a demux-only ``_duration_by_demux`` pass) and often break mid-file seeking (so
  sampling falls back from ``_sample_by_seek`` to a single-pass ``_sample_sequential``
  decode). Both only engage when the fast path under-delivers, so mp4/mov are unaffected.

**Graceful failure is mandatory (§9.1).** Bytes hash first, so a file that won't
decode still gets identity; the caller records it ``undecodable=1`` with a
``decode_error`` and no ``phash``/``vphash``. Nothing in here raises past the hash.

Heavy deps (pillow/pillow-heif/av/pdqhash/rawpy) are imported lazily so the lean
runtime (and non-``media`` installs) import this module fine; a scan without the
``media`` extra fails cleanly per file rather than at import.

Decode paths (HEIC/AVIF opener registration, RAW embedded-preview preference,
first-frame for animated stills, PyAV timeline sampling) are lifted from the
confirmed M0 smoke test (:mod:`packrat.smoke`) — see the §9.1 wheel notes.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from . import fsutil
from .config import PHOTO_EXTS, RAW_EXTS, VIDEO_EXTS, VideoConfig
from .ignore import ext_of
from .profiling import NULL_PROFILER

log = logging.getLogger("packrat.media")

_HASH_CHUNK = 1 << 20  # 1 MiB streaming reads (§10.1 bandwidth-bound over SMB)


# ---------------------------------------------------------------------------
# media-type classification (by EXTENSION, never by decoding — §8 A2 step 7)
# ---------------------------------------------------------------------------
def media_type_of(name: str) -> str | None:
    """``'photo'`` / ``'video'`` by extension, or ``None`` if not allowlisted media.

    RAW extensions classify as ``photo`` (they decode to a still — §9.1). The
    caller has already applied the allowlist; this only assigns the type column.
    """
    e = ext_of(name)
    if e in PHOTO_EXTS or e in RAW_EXTS:
        return "photo"
    if e in VIDEO_EXTS:
        return "video"
    return None


# ---------------------------------------------------------------------------
# content hash — always runs (§5.1)
# ---------------------------------------------------------------------------
def hash_file(path: str, *, medium: str = "photo", profiler=NULL_PROFILER) -> str:
    """Stream a file through BLAKE3, returning the hex digest (§5.1).

    Used for the **video** path and the photo-fallback (files too big to buffer).
    Uses the long-path-safe form for the actual open (§8 A1). Raises on an I/O
    error — an unreadable file has no identity and the caller records it as an
    error (distinct from *undecodable*, which still has a hash).

    When profiling, each ``read()`` is timed into ``(medium, io)`` (the byte
    transfer — disk or network, §10.1) and each ``update()`` into ``(medium,
    hash)`` (pure CPU) — a clean transfer-vs-CPU split on the one step that touches
    every byte. The default no-op profiler makes this a plain loop.
    """
    from blake3 import blake3  # type: ignore

    h = blake3()
    with open(fsutil.extended(path), "rb") as f:
        if not profiler.enabled:
            for chunk in iter(lambda: f.read(_HASH_CHUNK), b""):
                h.update(chunk)
        else:
            while True:
                t = time.perf_counter()
                chunk = f.read(_HASH_CHUNK)
                profiler.add(medium, "io", time.perf_counter() - t)
                if not chunk:
                    break
                profiler.add_bytes(medium, len(chunk))
                t = time.perf_counter()
                h.update(chunk)
                profiler.add(medium, "hash", time.perf_counter() - t)
    return h.hexdigest()


def hash_bytes(data: bytes, *, medium: str = "photo", profiler=NULL_PROFILER) -> str:
    """BLAKE3 of an in-memory buffer (§5.1) — the **photo pipeline** consumer path.

    The producer already read the bytes off disk/NAS (timed as ``io``), so this is
    *pure CPU*: timed into ``(medium, hash)`` with no I/O to blur it.
    """
    from blake3 import blake3  # type: ignore

    with profiler.timer(medium, "hash"):
        return blake3(data).hexdigest()


# ---------------------------------------------------------------------------
# results
# ---------------------------------------------------------------------------
@dataclass
class FrameSig:
    """One sampled video frame's PDQ (§5.3)."""

    frame_index: int
    t_offset_s: float
    pdq_bits: bytes  # 32-byte packed PDQ
    quality: int


@dataclass
class Fingerprint:
    """Everything scan persists for one file besides its ``file_instances`` row.

    ``undecodable`` + ``decode_error`` mirror the §4 asset columns. For a photo
    ``phash_bits``/``phash_quality`` are set (unless undecodable); for a video
    ``frames`` holds the per-frame PDQ rows and ``duration_s`` the clip length.
    """

    media_type: str
    content_hash: str
    size: int | None = None
    width: int | None = None
    height: int | None = None
    duration_s: float | None = None
    captured_at: str | None = None
    undecodable: bool = False
    decode_error: str | None = None
    #: Video codec name (h264|hevc|av1|…) for the §8 B video keep-lead weight. Video only.
    codec: str | None = None
    # photo perceptual
    phash_bits: bytes | None = None
    phash_quality: int | None = None
    # video perceptual
    frames: list[FrameSig] = field(default_factory=list)


# ---------------------------------------------------------------------------
# decode (lifted from the confirmed M0 smoke test — §9.1)
# ---------------------------------------------------------------------------
def _register_heif_openers() -> None:
    """Enable HEIC + AVIF decode. pillow-heif ≥1.4 covers both via one call.

    (M0 gotcha: ``register_avif_opener`` was dropped in 1.4 — guard it.)
    """
    try:
        import pillow_heif  # type: ignore

        pillow_heif.register_heif_opener()
        try:
            pillow_heif.register_avif_opener()  # pre-1.4 only
        except AttributeError:
            pass
    except Exception:  # noqa: BLE001 - decode is best-effort
        pass


def _decode_still(
    path: str, *, data: bytes | None = None, profiler=NULL_PROFILER
) -> tuple["object", str | None]:
    """Return ``(RGB numpy array, captured_at|None)`` for a photo/RAW still (§9.1).

    ``data`` (in-memory bytes) is the **photo pipeline** path: the producer already
    read the file, so decode runs from RAM — timed into ``(photo, decode)`` as
    *pure CPU* (no file descriptor, so no hidden lazy reads). When ``data`` is None
    we decode from ``path`` (the fallback for oversized photos), which re-reads the
    bytes and is therefore mixed I/O+CPU — still bucketed as decode.

    ``path``'s extension decides RAW vs regular either way. Capture time comes from
    PIL EXIF (no ``exiftool`` subprocess — §10.1); RAW uses rawpy's embedded
    preview and skips EXIF (best-effort).
    """
    import numpy as np
    from io import BytesIO

    is_raw = ext_of(path) in RAW_EXTS
    with profiler.timer("photo", "decode"):
        if is_raw:
            import rawpy  # type: ignore

            raw_src = BytesIO(data) if data is not None else fsutil.extended(path)
            with rawpy.imread(raw_src) as raw:
                try:
                    thumb = raw.extract_thumb()  # prefer embedded preview (§9.1)
                    if thumb.format == rawpy.ThumbFormat.JPEG:
                        from PIL import Image

                        img = Image.open(BytesIO(thumb.data)).convert("RGB")
                        return np.asarray(img), None
                except Exception:  # noqa: BLE001 - fall back to full demosaic
                    pass
                return np.asarray(raw.postprocess()), None

        _register_heif_openers()
        from PIL import Image

        src = BytesIO(data) if data is not None else fsutil.extended(path)
        # `with` so the underlying file handle is released promptly — the streamed
        # fallback path (data is None) opens a real file, and leaking it exhausts fds /
        # pins SMB handles over a large scan of oversized photos. np.asarray copies the
        # pixels out, so the array outlives the closed image.
        with Image.open(src) as img:
            captured_at = _exif_capture_time(img)
            if getattr(img, "is_animated", False):
                img.seek(0)  # first frame for animated GIF / multi-page TIFF (§9.1)
            return np.asarray(img.convert("RGB")), captured_at


def _exif_capture_time(img) -> str | None:
    """Best-effort capture time from an open PIL image's EXIF (§8 A2 step 7).

    Reads ``DateTimeOriginal`` (0x9003), falling back to ``DateTime`` (0x0132),
    normalizing EXIF's ``YYYY:MM:DD HH:MM:SS`` to ISO. Never raises.
    """
    try:
        exif = img.getexif()
    except Exception:  # noqa: BLE001 - many formats have no EXIF
        return None
    if not exif:
        return None
    raw = exif.get(0x9003) or exif.get(0x0132)
    if not raw or str(raw).startswith("0000"):
        return None
    s = str(raw).strip()
    return s[:10].replace(":", "-") + s[10:]


def _downscale_for_pdq(arr, max_edge: int):
    """Return ``arr`` shrunk so its longest edge ≤ ``max_edge`` (or unchanged).

    PDQ on a full-resolution ~12MP array is ~7x slower than on a 512px copy while
    the hash drifts only ~7/256 bits (well inside ``t_photo_edit``); downscaling
    also normalizes away JPEG recompression noise, so near-dup recall is unchanged.
    A LANCZOS (antialiasing) resize is essential — a naive subsample would alias
    and *hurt* the hash. ``max_edge<=0`` disables (hash full-res). This copies, so
    the caller's array (used for stored ``width``/``height``) is untouched.
    """
    import numpy as np

    if max_edge <= 0:
        return arr
    h, w = arr.shape[0], arr.shape[1]
    if max(h, w) <= max_edge:
        return arr
    from PIL import Image

    img = Image.fromarray(arr)
    img.thumbnail((max_edge, max_edge), Image.LANCZOS)
    return np.ascontiguousarray(np.asarray(img))


def _pdq(arr, *, max_edge: int = 0, medium: str = "photo", profiler=NULL_PROFILER) -> tuple[bytes, int]:
    """Compute PDQ over an RGB array; return ``(32-byte packed bits, quality)``.

    Downscales to ``max_edge`` first (§ profiler finding — PDQ was the largest
    scan bucket, dominated by full-res input). Pure CPU + a small resize, timed
    into ``(medium, pdq)`` when profiling (§10.1 CPU signal).
    """
    import numpy as np
    import pdqhash  # type: ignore

    with profiler.timer(medium, "pdq"):
        small = _downscale_for_pdq(arr, max_edge)
        bits, quality = pdqhash.compute(small)
    packed = np.packbits(np.asarray(bits, dtype=np.uint8)).tobytes()
    return packed, int(quality)


def _probe_video(
    path: str, cfg: VideoConfig, *, max_edge: int = 0, profiler=NULL_PROFILER
) -> tuple[float | None, int | None, int | None, str | None, str | None, list[FrameSig]]:
    """Decode a video: return ``(duration_s, width, height, captured_at, codec, frames)`` (§5.3).

    Samples ``cfg.sample_frames`` frames at segment midpoints ``t_k = dur·(k+0.5)/N``.
    For each target time we seek to the preceding keyframe then decode forward to
    the first frame at/after the target (seek lands on a keyframe, not the exact
    pts). Per-frame PDQ + quality is stored for *every* decoded frame; the
    ``min_frame_quality`` gate is a *matching*-time filter (M3), not a scan-time
    drop (§5.3: frames are "stored, but flagged"). ``codec`` is the video stream's
    codec name (``h264``/``hevc``/``av1``/…) for the §8 B video keep-lead weight.
    """
    import av  # type: ignore

    n = max(1, cfg.sample_frames)
    with profiler.timer("video", "decode"):
        container = av.open(fsutil.extended(path))
    try:
        vs = container.streams.video[0]
        vs.thread_type = "AUTO"
        tb = vs.time_base
        width = vs.codec_context.width or None
        height = vs.codec_context.height or None
        codec = (vs.codec_context.name or None) if vs.codec_context else None
        # Transport streams (.ts/.m2ts/.mts) and other header-less muxes routinely report NO
        # stream/container duration. Without a timeline we'd fall through to a single frame
        # below → too few comparable frames to ever match (§5.3 min_comparable_frames), so the
        # clip is catalogued but invisible to dedup. `_video_duration_s` prefers the stream
        # duration, then the container's, then a demux-only fallback (packet headers, not
        # pixels) — the fallback only runs when neither native source exists.
        with profiler.timer("video", "decode"):
            duration_s = _video_duration_s(
                vs.duration, tb, container.duration, av.time_base,
                demux_dur=lambda: _duration_by_demux(container, vs, tb))
        if duration_s and tb:
            try:
                container.seek(0, stream=vs)         # rewind after any demux pass, before sampling
            except Exception:  # noqa: BLE001 - per-k seeks are absolute, so this is belt-and-braces
                pass

        captured_at = _video_capture_time(container)
        frames: list[FrameSig] = []
        if duration_s and tb:
            # Presentation timestamps don't necessarily start at 0 — transport streams commonly
            # carry a non-zero start_time (PCR offset), so seek targets must be measured FROM it.
            start = vs.start_time or 0
            targets = [start + int(duration_s * (k + 0.5) / n / tb) for k in range(n)]
            # Seek-based sampling is cheap on well-behaved containers (mp4/mov): jump to each
            # target's keyframe, decode forward a little. Try it first.
            frames = _sample_by_seek(container, vs, tb, targets, max_edge=max_edge,
                                     profiler=profiler)
            # Transport streams frequently BREAK mid-file seeking (a seek to a non-zero target
            # returns zero frames — measured), collapsing the sample set. When seeking yields
            # well under the target count, fall back to a single sequential decode pass that
            # picks the frame nearest each target — O(clip) but reliable, and it only runs when
            # seeking under-delivered. min_comparable_frames is the floor that makes a clip
            # dedup-able (§5.3), so recover at least that many before giving up on the timeline.
            if len(frames) < min(n, cfg.min_comparable_frames):
                seq = _sample_sequential(container, vs, tb, targets, max_edge=max_edge,
                                         profiler=profiler)
                if len(seq) > len(frames):
                    frames = seq

        # Guarantee ≥1 frame so the asset is "fully fingerprinted" (§8 A2 — a vphash row must
        # exist, else every scan re-decodes it). Covers a still-unknown duration AND a clip that
        # is decodable but yielded nothing above: decode the first frame from the start. Such a
        # clip still won't have enough comparable frames to dedup, but it's catalogued once and
        # skipped by the fast-path (a truly undecodable file yields nothing → undecodable, §9.1).
        if not frames:
            with profiler.timer("video", "decode"):
                try:
                    container.seek(0, stream=vs)
                except Exception:  # noqa: BLE001
                    pass
                arr = next(
                    (f.to_ndarray(format="rgb24") for f in container.decode(vs)), None
                )
            if arr is not None:
                bits, q = _pdq(arr, max_edge=max_edge, medium="video", profiler=profiler)
                frames.append(FrameSig(0, 0.0, bits, q))
        return duration_s, width, height, captured_at, codec, frames
    finally:
        container.close()


def _sample_by_seek(container, vs, tb, targets, *, max_edge, profiler) -> list[FrameSig]:
    """Sample one frame per target pts by seeking to each (cheap on seekable containers).

    For each target: seek to the preceding keyframe, decode forward to the first frame
    at/after the target. Skips a target whose seek raises or whose decode yields nothing
    (a transport stream may do either) — the caller detects the shortfall and falls back."""
    frames: list[FrameSig] = []
    for k, target in enumerate(targets):
        with profiler.timer("video", "decode"):
            try:
                container.seek(target, stream=vs)
            except Exception:  # noqa: BLE001 - some containers dislike seeking; skip k
                continue
            picked = None
            for frame in container.decode(vs):
                if frame.pts is None:
                    continue
                picked = frame
                if frame.pts >= target:
                    break
            arr = picked.to_ndarray(format="rgb24") if picked is not None else None
        if arr is None:
            continue
        bits, q = _pdq(arr, max_edge=max_edge, medium="video", profiler=profiler)
        frames.append(FrameSig(k, float(picked.pts * tb), bits, q))
    return frames


def _sample_sequential(container, vs, tb, targets, *, max_edge, profiler) -> list[FrameSig]:
    """Sample the frame nearest each target in ONE forward decode pass (no per-target seek).

    The reliable fallback for streams where mid-file seeking is broken (transport streams):
    decode from the start once, and for each ascending target keep the first frame whose pts
    is at/after it. O(clip) rather than O(keyframe) but correct where seeking silently
    returns nothing. Rewinds to the start first; ``targets`` must be ascending (they are —
    segment midpoints)."""
    frames: list[FrameSig] = []
    with profiler.timer("video", "decode"):
        try:
            container.seek(0, stream=vs)
        except Exception:  # noqa: BLE001
            pass
        ti = 0
        for frame in container.decode(vs):
            if ti >= len(targets):
                break
            if frame.pts is None:
                continue
            # Advance past any targets this frame satisfies (handles sparse/low-fps clips
            # where one decoded frame is the nearest to several targets — take it once).
            if frame.pts >= targets[ti]:
                arr = frame.to_ndarray(format="rgb24")
                bits, q = _pdq(arr, max_edge=max_edge, medium="video", profiler=profiler)
                frames.append(FrameSig(ti, float(frame.pts * tb), bits, q))
                ti += 1
                while ti < len(targets) and frame.pts >= targets[ti]:
                    ti += 1                          # this frame is also nearest later targets
    return frames


def _video_duration_s(stream_dur, tb, container_dur, container_tb, *, demux_dur) -> float | None:
    """Pick a video's duration (seconds) from the available sources, best-first (§5.3).

    Order: the video **stream** duration (``stream_dur`` in ``tb`` units) → the **container**
    duration (``container_dur`` in ``container_tb`` units, i.e. ``av.time_base``) → the
    **demux fallback** (``demux_dur()``, called ONLY when neither native source exists, so
    the extra packet-scan hits just the header-less .ts/.m2ts case). Returns ``None`` when no
    source yields a timeline (the caller then keeps the single-frame path). Pure except for
    the injected ``demux_dur`` thunk, so the source-selection ladder is unit-testable without
    a real container.
    """
    if stream_dur and tb:
        return float(stream_dur * tb)
    if container_dur:
        return container_dur / container_tb
    if tb:
        return demux_dur()
    return None


def _duration_by_demux(container, vs, tb) -> float | None:
    """Estimate a video's duration from its last packet's timestamp (§5.3 fallback).

    For containers that report no stream/container duration (transport streams, truncated
    captures), demux the video stream WITHOUT decoding — cheap relative to a full decode,
    since it reads packet headers, not pixels — and take the greatest presentation end
    (``pts + packet.duration``). Falls back to ``dts`` when ``pts`` is absent. Returns
    ``None`` if the stream carries no timestamps at all (the caller then keeps the
    single-frame path). Never raises — a demux error just means "no better estimate".
    """
    if not tb:
        return None
    last = None
    try:
        for packet in container.demux(vs):
            ts = packet.pts if packet.pts is not None else packet.dts
            if ts is None:
                continue
            end = ts + (packet.duration or 0)
            if last is None or end > last:
                last = end
    except Exception:  # noqa: BLE001 - unreadable tail → best-effort None
        return None
    return float(last * tb) if last else None


def _video_capture_time(container) -> str | None:
    """Best-effort capture time from a PyAV container's metadata (§8 A2 step 7).

    Reads the ``creation_time`` tag most containers carry; normalizes to a bare
    ISO datetime. Never raises.
    """
    try:
        raw = container.metadata.get("creation_time")
    except Exception:  # noqa: BLE001
        return None
    if not raw:
        return None
    s = str(raw).strip().rstrip("Z").replace("T", " ")
    return s[:19] if len(s) >= 10 else None


# ---------------------------------------------------------------------------
# the one entry point scan calls per file
# ---------------------------------------------------------------------------
def fingerprint(
    path: str,
    size: int,
    config,
    *,
    want_perceptual: bool = True,
    content_hash: str | None = None,
    profiler=NULL_PROFILER,
) -> Fingerprint:
    """Hash + (optionally) decode + perceptual-hash one file by **path** (§8 A2 5–8).

    The path-based entry point — used for videos and the photo-fallback (oversized
    files). Always hashes (or reuses ``content_hash``). The photo pipeline instead
    reads the file once in a producer and calls :func:`hash_bytes` +
    :func:`fill_perceptual` (with ``data=``) from the buffer — see ``jobs.scan``.
    """
    mtype = media_type_of(path) or "photo"
    ch = content_hash or hash_file(path, medium=mtype, profiler=profiler)
    fp = Fingerprint(media_type=mtype, content_hash=ch, size=size)
    if want_perceptual:
        fill_perceptual(fp, path, config, profiler=profiler)
    return fp


def probe_metadata(path: str, media_type: str, config) -> Fingerprint:
    r"""Best-effort metadata (dims / duration / captured_at / codec) — **no PDQ** (§8 C step 11).

    Used by ``merge`` to fill a ``new`` asset's display columns when it registers a
    just-copied file, *without* the expensive perceptual pass: a later ``scan``/``dedup``
    of the dest backfills ``phash``/``vphash`` (and re-derives these metadata columns
    via the not-yet-fingerprinted backfill exception, §8 A2 step 6). So this is a
    lightweight header read, not a full decode+hash.

    A merge-created asset is **not-yet-fingerprinted** (``undecodable=0``, no phash yet)
    — *not* undecodable (§4). So this never sets ``undecodable``: on any probe failure it
    just leaves the metadata fields ``None`` and returns (the backfill scan sorts out
    real decodability). Never raises. ``content_hash`` is left blank — the caller supplies
    the frozen hash from the merge plan.
    """
    fp = Fingerprint(media_type=media_type, content_hash="")
    try:
        if media_type == "video":
            import av  # type: ignore

            container = av.open(fsutil.extended(path))
            try:
                vs = container.streams.video[0]
                tb = vs.time_base
                fp.width = vs.codec_context.width or None
                fp.height = vs.codec_context.height or None
                fp.codec = (vs.codec_context.name or None) if vs.codec_context else None
                # Same duration-source ladder as _probe_video (incl. the .ts demux fallback),
                # so a merged transport stream gets a real duration in its display column
                # rather than NULL — one seam, consistent across both probe paths (§5.3).
                fp.duration_s = _video_duration_s(
                    vs.duration, tb, container.duration, av.time_base,
                    demux_dur=lambda: _duration_by_demux(container, vs, tb))
                fp.captured_at = _video_capture_time(container)
            finally:
                container.close()
        elif ext_of(path) in RAW_EXTS:
            import rawpy  # type: ignore

            with rawpy.imread(fsutil.extended(path)) as raw:
                s = raw.sizes
                fp.width, fp.height = int(s.width), int(s.height)
        else:
            _register_heif_openers()
            from PIL import Image

            with Image.open(fsutil.extended(path)) as img:
                fp.width, fp.height = int(img.size[0]), int(img.size[1])  # PIL: (w, h)
                fp.captured_at = _exif_capture_time(img)
    except Exception as exc:  # noqa: BLE001 - metadata is best-effort; scan backfills it
        log.debug("merge metadata probe failed for %s: %s", path, exc)
    return fp


def fill_perceptual(
    fp: Fingerprint, path: str, config, *, data: bytes | None = None, profiler=NULL_PROFILER
) -> Fingerprint:
    """Decode and fill ``fp``'s perceptual/metadata fields in place (M2, §5.3).

    ``data`` (photo pipeline) decodes from RAM; otherwise from ``path``. A decode
    failure sets ``undecodable=1`` + ``decode_error`` and leaves the perceptual
    fields empty (§9.1) — never raises past here. Capture time / dimensions come
    from the same decode pass (no ``exiftool`` subprocess, §10.1).
    """
    # Downscale-before-PDQ edge (§ profiler finding). Full-res dimensions are read
    # from the decoded array *before* PDQ downscales its own copy, so stored
    # width/height stay original — only the hash input shrinks.
    max_edge = config.match.pdq_max_edge
    try:
        if fp.media_type == "video":
            duration_s, width, height, captured_at, codec, frames = _probe_video(
                path, config.video, max_edge=max_edge, profiler=profiler
            )
            fp.duration_s = duration_s
            fp.width, fp.height = width, height
            fp.captured_at = captured_at
            fp.codec = codec
            if not frames:
                raise RuntimeError("no decodable frames")
            fp.frames = frames
        else:
            import numpy as np  # noqa: F401 - ensure numpy present before decode

            arr, captured_at = _decode_still(path, data=data, profiler=profiler)
            fp.height, fp.width = int(arr.shape[0]), int(arr.shape[1])
            fp.captured_at = captured_at
            bits, quality = _pdq(arr, max_edge=max_edge, medium="photo", profiler=profiler)
            fp.phash_bits, fp.phash_quality = bits, quality
    except Exception as exc:  # noqa: BLE001 - undecodable: keep the hash, flag it (§9.1)
        fp.undecodable = True
        fp.decode_error = f"{type(exc).__name__}: {exc}"[:500]
        fp.phash_bits = fp.phash_quality = None
        fp.frames = []
        log.debug("undecodable %s: %s", path, fp.decode_error)
    return fp
