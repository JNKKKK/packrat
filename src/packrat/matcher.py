r"""The §5.3 perceptual matching engine — pure fingerprint math, no file I/O.

A single **scope-agnostic** near-dup matcher, run only by ``dedup`` (§8 B) and
``cleanup --perceptual`` (§6.2). It reads the PDQ signatures ``scan`` already
stored (``phash`` for photos, ``vphash`` for video frames) and reports which
assets are perceptual near-dups of which — it never touches the filesystem and
never uses CLIP (semantic ≠ duplicate, §5).

Two media, two decision rules (§5.3):

- **Photo** — the single signal is PDQ Hamming distance. A pair matches iff
  ``distance ≤ match.t_photo_edit`` (the wider cutoff — the match decision). The
  matcher reports the raw distance; the *caller* (dedup) bands it into review
  stages with ``t_photo_recompress`` (§8 B). Photo quality is *stored* but never
  gates a photo out of matching (a photo has exactly one PDQ; gating it would make
  the asset silently invisible to dedup — §5.3 annotate-never-gate).
- **Video** — durations within tolerance (the ``duration_tol_s`` / ``duration_tol_pct``
  pre-filter) **and** at least ``video.frame_match_fraction`` of *comparable*
  (quality-gated) frame-pairs match within ``t_match_video``, with at least
  ``video.min_comparable_frames`` comparable pairs. Frames are compared
  **frame-index-aligned** (frame k of A vs frame k of B), valid because both were
  sampled at the same relative timeline positions and the duration pre-filter keeps
  their lengths close (§5.3).

**Edges are canonical-ordered** (``asset_a < asset_b``) so an undirected pair has
exactly one row (§4). ``find_matches`` compares a *target* set against a *pool*
and skips self-pairs; when a pair is discovered from both directions (both assets
in the target set) it is emitted once, keeping the smaller distance.

The core functions operate on plain signature lists so they are unit-testable
without a database; :func:`load_signatures` is the thin DB reader dedup/cleanup use.
Heavy ``numpy`` is imported lazily (mirroring :mod:`packrat.media`) so the lean
runtime imports this module fine.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from .config import VideoConfig

log = logging.getLogger("packrat.matcher")

# PDQ is 256 bits = 32 packed bytes (np.packbits form scan stores — §4/M2).
_PDQ_BYTES = 32

# Byte popcount lookup table, built once on first use (keeps numpy import lazy).
_POPCOUNT = None


def _popcount_table():
    global _POPCOUNT
    if _POPCOUNT is None:
        import numpy as np

        _POPCOUNT = np.array([bin(i).count("1") for i in range(256)], dtype=np.uint8)
    return _POPCOUNT


# ---------------------------------------------------------------------------
# signatures (what the matcher compares)
# ---------------------------------------------------------------------------
@dataclass
class PhotoSig:
    """One photo asset's PDQ signature (§5.3)."""

    asset_id: int
    bits: bytes  # 32-byte packed PDQ
    quality: int | None = None


@dataclass
class VideoSig:
    """One video asset's per-frame PDQ signatures + duration (§5.3)."""

    asset_id: int
    duration_s: float | None
    #: (frame_index, 32-byte packed PDQ, quality) per sampled frame.
    frames: list[tuple[int, bytes, int]] = field(default_factory=list)


@dataclass
class Signatures:
    """A loaded set of signatures, split by medium."""

    photos: list[PhotoSig] = field(default_factory=list)
    videos: list[VideoSig] = field(default_factory=list)


@dataclass(frozen=True)
class Edge:
    """A discovered near-dup pair, canonical-ordered (``asset_a < asset_b``)."""

    asset_a: int
    asset_b: int
    media_type: str  # 'photo' | 'video'
    distance: int
    algo: str  # 'pdq' | 'video'


# ---------------------------------------------------------------------------
# Hamming helpers
# ---------------------------------------------------------------------------
def hamming(a: bytes, b: bytes) -> int:
    """PDQ Hamming distance (0–256) between two 32-byte packed hashes."""
    import numpy as np

    tbl = _popcount_table()
    xor = np.frombuffer(a, np.uint8) ^ np.frombuffer(b, np.uint8)
    return int(tbl[xor].sum())


# ---------------------------------------------------------------------------
# photo matching (§5.3) — single PDQ signal, brute-force numpy Hamming
# ---------------------------------------------------------------------------
def match_photos(targets: list[PhotoSig], pool: list[PhotoSig], t_match: int) -> list[Edge]:
    """Return near-dup edges between ``targets`` and ``pool`` (photo, §5.3).

    Each target is Hamming-compared against the whole pool at once (packed-byte
    XOR + popcount table). Self-pairs are skipped; a pair found from both sides is
    emitted once with the smaller distance. Quality never gates (§5.3).
    """
    if not targets or not pool:
        return []
    import numpy as np

    tbl = _popcount_table()
    # Pool matrix: (M, 32) packed bytes. Rows with a wrong-length blob are dropped
    # defensively (a corrupt/legacy row must not crash a whole dedup run).
    pool_valid = [p for p in pool if p.bits is not None and len(p.bits) == _PDQ_BYTES]
    if not pool_valid:
        return []
    pool_ids = np.array([p.asset_id for p in pool_valid], dtype=np.int64)
    pool_bits = np.frombuffer(b"".join(p.bits for p in pool_valid), np.uint8).reshape(
        len(pool_valid), _PDQ_BYTES
    )

    best: dict[tuple[int, int], int] = {}
    for tgt in targets:
        if tgt.bits is None or len(tgt.bits) != _PDQ_BYTES:
            continue
        q = np.frombuffer(tgt.bits, np.uint8)
        dists = tbl[pool_bits ^ q].sum(axis=1)  # (M,), int64
        hits = np.nonzero(dists <= t_match)[0]
        for i in hits:
            other = int(pool_ids[i])
            if other == tgt.asset_id:
                continue  # skip self
            a, b = (tgt.asset_id, other) if tgt.asset_id < other else (other, tgt.asset_id)
            d = int(dists[i])
            if (a, b) not in best or d < best[(a, b)]:
                best[(a, b)] = d
    return [Edge(a, b, "photo", d, "pdq") for (a, b), d in best.items()]


# ---------------------------------------------------------------------------
# video matching (§5.3) — duration pre-filter + frame-aligned majority vote
# ---------------------------------------------------------------------------
def _durations_close(d1: float | None, d2: float | None, cfg: VideoConfig) -> bool:
    """The §5.3 duration pre-filter: ``|d1−d2| ≤ max(tol_s, tol_pct%·min(d1,d2))``."""
    if d1 is None or d2 is None:
        return False
    tol = max(cfg.duration_tol_s, cfg.duration_tol_pct / 100.0 * min(d1, d2))
    return abs(d1 - d2) <= tol


def _video_pair_score(a: VideoSig, b: VideoSig, cfg: VideoConfig, t_match: int) -> int | None:
    """Score a candidate video pair; return the match distance or ``None`` (no match).

    Frames are aligned by ``frame_index``. A frame-pair is *comparable* iff both
    frames clear ``min_frame_quality``; it *matches* iff its Hamming ≤ ``t_match``.
    Match iff comparable ≥ ``min_comparable_frames`` and matching/comparable ≥
    ``frame_match_fraction``. The returned "distance" (for the edge / review
    manifest) is the **mean Hamming over the comparable frame-pairs**, rounded —
    a genuine lower-is-closer number consistent with the photo distance.
    """
    a_frames = {fi: (bits, q) for fi, bits, q in a.frames}
    b_frames = {fi: (bits, q) for fi, bits, q in b.frames}
    comparable = 0
    matching = 0
    dist_sum = 0
    for fi, (a_bits, a_q) in a_frames.items():
        bf = b_frames.get(fi)
        if bf is None:
            continue
        b_bits, b_q = bf
        if (a_q or 0) < cfg.min_frame_quality or (b_q or 0) < cfg.min_frame_quality:
            continue  # not comparable — at least one frame below the quality gate
        if len(a_bits) != _PDQ_BYTES or len(b_bits) != _PDQ_BYTES:
            continue
        d = hamming(a_bits, b_bits)
        comparable += 1
        dist_sum += d
        if d <= t_match:
            matching += 1
    if comparable < cfg.min_comparable_frames:
        return None  # insufficient evidence beats a coin-flip (§5.3)
    if matching / comparable < cfg.frame_match_fraction:
        return None
    return round(dist_sum / comparable)


def match_videos(
    targets: list[VideoSig], pool: list[VideoSig], cfg: VideoConfig, t_match: int
) -> list[Edge]:
    """Return near-dup edges between ``targets`` and ``pool`` (video, §5.3).

    Pre-filters by duration (avoids the all-pairs blowup), then votes over
    frame-aligned comparable frame-pairs. Self-pairs skipped; a pair found from
    both sides is emitted once (smaller distance).
    """
    if not targets or not pool:
        return []
    best: dict[tuple[int, int], int] = {}
    for tgt in targets:
        for other in pool:
            if other.asset_id == tgt.asset_id:
                continue
            if not _durations_close(tgt.duration_s, other.duration_s, cfg):
                continue
            score = _video_pair_score(tgt, other, cfg, t_match)
            if score is None:
                continue
            a, b = (
                (tgt.asset_id, other.asset_id)
                if tgt.asset_id < other.asset_id
                else (other.asset_id, tgt.asset_id)
            )
            if (a, b) not in best or score < best[(a, b)]:
                best[(a, b)] = score
    return [Edge(a, b, "video", d, "video") for (a, b), d in best.items()]


# ---------------------------------------------------------------------------
# top-level: match a target set against a pool (both media)
# ---------------------------------------------------------------------------
def find_matches(targets: Signatures, pool: Signatures, config) -> list[Edge]:
    """Run the §5.3 matcher for ``targets`` against ``pool``; return all edges.

    ``config`` is the frozen job :class:`~packrat.config.Config` — supplies
    ``match.t_photo_edit`` (the photo match cutoff) / ``match.t_match_video`` and
    the ``video.*`` knobs. Photo and video are matched independently (a photo never
    matches a video). The caller bands the returned photo distances into review
    stages via ``t_photo_recompress`` (§8 B); the matcher itself does no banding.
    """
    edges = match_photos(targets.photos, pool.photos, config.match.t_photo_edit)
    edges += match_videos(targets.videos, pool.videos, config.video, config.match.t_match_video)
    return edges


# ---------------------------------------------------------------------------
# DB reader (dedup/cleanup use this to load signatures)
# ---------------------------------------------------------------------------
def load_signatures(db, *, asset_ids=None, statuses=("active",)) -> Signatures:
    """Load photo + video signatures for a set of assets (§5.3 matcher input).

    ``asset_ids`` restricts to those assets (a set/iterable of ids); ``None`` loads
    every asset in ``statuses``. ``statuses`` filters ``assets.status`` — dedup
    passes ``('active',)`` (trashed excluded, §5); ``cleanup --perceptual`` passes
    ``('trashed',)``. Undecodable assets have no perceptual rows so they simply
    contribute nothing. Reads ``phash``/``vphash`` only — no file I/O.
    """
    id_set = None if asset_ids is None else {int(a) for a in asset_ids}
    if id_set is not None and not id_set:
        return Signatures()

    status_ph = ",".join("?" for _ in statuses)
    sig = Signatures()

    # Photos: one PDQ row per asset.
    rows = db.query(
        f"SELECT p.asset_id, p.bits, p.quality FROM phash p "
        f"JOIN assets a ON a.id = p.asset_id "
        f"WHERE a.media_type='photo' AND a.undecodable=0 AND a.status IN ({status_ph})",
        tuple(statuses),
    )
    for r in rows:
        if id_set is not None and r["asset_id"] not in id_set:
            continue
        sig.photos.append(PhotoSig(int(r["asset_id"]), r["bits"], r["quality"]))

    # Videos: gather per-asset frame lists (+ duration from assets).
    vrows = db.query(
        f"SELECT v.asset_id, v.frame_index, v.pdq_bits, v.quality, a.duration_s "
        f"FROM vphash v JOIN assets a ON a.id = v.asset_id "
        f"WHERE a.media_type='video' AND a.undecodable=0 AND a.status IN ({status_ph}) "
        f"ORDER BY v.asset_id, v.frame_index",
        tuple(statuses),
    )
    by_asset: dict[int, VideoSig] = {}
    for r in vrows:
        aid = int(r["asset_id"])
        if id_set is not None and aid not in id_set:
            continue
        vs = by_asset.get(aid)
        if vs is None:
            vs = VideoSig(aid, r["duration_s"], [])
            by_asset[aid] = vs
        vs.frames.append((int(r["frame_index"]), r["pdq_bits"], r["quality"] or 0))
    sig.videos.extend(by_asset.values())
    return sig


def asset_qualities(db, asset_ids, *, min_frame_quality: int = 0) -> dict[int, int]:
    """Per-asset PDQ quality scalar for the review low-confidence hint (§5.3).

    Photo → the asset's single ``phash.quality``. Video → the MIN over frames that
    clear ``min_frame_quality`` (the frames the matcher would actually compare — a
    dark/transition frame already excluded from matching shouldn't drag the hint
    down), falling back to the overall MIN when no frame clears the gate. Passing
    ``min_frame_quality=0`` (the default) reduces the video branch to a plain MIN over
    all frames — what cleanup wants (its single wider cutoff has no frame gate).
    """
    if not asset_ids:
        return {}
    ph = ",".join("?" for _ in asset_ids)
    q: dict[int, int] = {}
    for r in db.query(f"SELECT asset_id, quality FROM phash WHERE asset_id IN ({ph})", tuple(asset_ids)):
        if r["quality"] is not None:
            q[int(r["asset_id"])] = int(r["quality"])
    for r in db.query(
        f"SELECT asset_id, "
        f"  COALESCE(MIN(CASE WHEN quality >= ? THEN quality END), MIN(quality)) mq "
        f"FROM vphash WHERE asset_id IN ({ph}) GROUP BY asset_id",
        (min_frame_quality, *asset_ids),
    ):
        if r["mq"] is not None:
            q[int(r["asset_id"])] = int(r["mq"])
    return q
