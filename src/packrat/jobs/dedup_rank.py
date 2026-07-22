r"""Keep-lead ranking for dedup stage 2 (§8 B) — pure fingerprint/metadata math.

Given a homogeneous near-dup group (all photo, or all video — a photo never matches
a video) and a ``rank`` dict of per-asset metadata (``_asset_rank_fields`` in
:mod:`packrat.jobs.dedup` loads it), decide which member is the **keep-lead** — the
copy dedup suggests keeping — and *why* it won. No database, no filesystem: given the
metadata it is a deterministic function of the group, which is why it is unit-tested
in isolation (``tests/test_video_lead.py``) and lives apart from dedup's stateful
analyze→confirm lifecycle.

Ranking keys (best = greatest tuple, all components DESC):

- **Photo:** pixels → format rank (:func:`_photo_format_rank`: lossless > efficient-
  lossy HEIC/AVIF > other-lossy JPEG/WebP) → file size. Resolution first (a downscaled
  re-export loses outright); then format — the primary quality signal at equal
  resolution, since a modern codec packs more real detail per byte, so an iPhone HEIC
  original outranks its JPEG export; then, **within one format** (where the encoder's
  output size is a clean monotonic quality proxy), the larger file. Size is compared
  only within a format because it lies across them (an efficient HEIC master is smaller
  than a bloated JPEG export) — exactly what the format rank handles.
- **Video:** pixels → effective-bitrate BAND → codec weight → raw effective bitrate.
  Effective bitrate = ``size/duration_s × codec_weight``: a more-efficient codec's bits
  are worth more, so an HEVC master beats an H.264 re-export at equal resolution+quality.
  Bitrates within ``video_bitrate_tie_pct`` share a log-scale band so the *codec weight*
  breaks a cross-codec near-tie instead of a coin-flip on a noisy diff. The final raw
  bitrate then breaks a *same-codec* near-tie (band + weight both tied): higher bitrate
  at equal resolution+codec is a clean quality dial, the video analogue of file ``size``
  within one photo format. It sits last, so it never reverses the cross-codec decision.

Ties on the full key fall to a stable smallest-normcase-path tiebreak (deterministic
across runs).
"""

from __future__ import annotations

import math
import os

from ..config import RAW_EXTS
from ..ignore import ext_of

#: Photo extensions that are lossless / an original master (§8 B keep-lead).
_LOSSLESS_PHOTO_EXTS = frozenset({"png", "tif", "tiff", "bmp"}) | RAW_EXTS

#: Modern *lossy* codecs that are more efficient than JPEG — a HEIC/AVIF file packs
#: more real detail into a byte than a same-size JPEG. On iPhone these are the
#: originals; the JPEG is the export.
_EFFICIENT_LOSSY_PHOTO_EXTS = frozenset({"heic", "heif", "avif"})

# Ranking-key component labels, best-decision first — what the keep-lead was decided
# by (the leftmost key component where the lead is uniquely ahead). Index i names the
# level "decided once you consider key[:i+1]"; a full tuple tie falls to the path
# tiebreak (`_PATH_TIEBREAK`). Reported as stage-2 lead-pick stats (§8 B).
_PHOTO_LEAD_LEVELS = ("resolution", "resolution + format", "resolution + format + size")
_VIDEO_LEAD_LEVELS = ("resolution", "resolution + bitrate", "resolution + bitrate + codec",
                      "resolution + bitrate + codec + fine bitrate")
_PATH_TIEBREAK = "path tiebreak (identical rank)"


def _photo_format_rank(path: str) -> int:
    """Ordinal photo-format preference for the keep-lead (§8 B), best first.

    ``2`` lossless/original (png/tif/bmp/RAW) · ``1`` efficient-lossy (heic/heif/avif)
    · ``0`` other lossy (jpg/webp/gif/…). This is the **primary quality signal** after
    resolution: at equal resolution a lossless copy is the master, and among lossy
    copies a modern codec (HEIC/AVIF) packs more real detail per byte than JPEG, so an
    iPhone HEIC original outranks its JPEG export. Below it, file size breaks ties
    *within a single format* (see :func:`_pick_lead`).
    """
    ext = ext_of(path)
    if ext in _LOSSLESS_PHOTO_EXTS:
        return 2
    if ext in _EFFICIENT_LOSSY_PHOTO_EXTS:
        return 1
    return 0


def _photo_lead_key(inst, r) -> tuple:
    """Photo keep-lead ranking key (best = greatest): (pixels, format rank, size). §8 B."""
    pixels = (r.get("width") or 0) * (r.get("height") or 0)
    return (pixels, _photo_format_rank(inst["path"]), r.get("size") or 0)


def _video_lead_key(r, config) -> tuple:
    """Video keep-lead ranking key (best = greatest): (pixels, bitrate band, codec weight, eff). §8 B.

    The final component is the *raw* (unbanded) effective bitrate. It can only fire once
    the band **and** the codec weight both tie — i.e. same codec, effective bitrates
    within ``video_bitrate_tie_pct``. There, higher raw bitrate is a clean quality dial
    (analogous to file ``size`` within one photo format), so it breaks the near-tie
    rather than a coin-flip on the path. It can never reverse the cross-codec weight
    decision, which sits ahead of it.
    """
    pixels = (r.get("width") or 0) * (r.get("height") or 0)
    weight = config.match.codec_weights.get((r.get("codec") or "").lower(), 1.0)
    eff = _effective_bitrate(r.get("size"), r.get("duration_s"), weight)
    return (pixels, _log_band(eff, config.match.video_bitrate_tie_pct), weight, eff)


def _group_lead_and_level(members, rank, config) -> tuple:
    """Pick the keep-lead of a stage-2 group AND *why* it won (§8 B).

    ``members`` is a list of ``(asset_id, instance)`` pairs; ``rank`` maps asset_id →
    the metadata dict (:func:`packrat.jobs.dedup._asset_rank_fields`). Returns
    ``(lead_asset_id, level_label)``. ``level_label`` is the leftmost key component
    that made the lead *uniquely* best (:data:`_PHOTO_LEAD_LEVELS` /
    :data:`_VIDEO_LEAD_LEVELS`), or :data:`_PATH_TIEBREAK` if every key component tied
    and the stable smallest-normcase-path tiebreak decided. Empty group → ``(None, None)``.
    """
    if not members:
        return None, None
    is_video = any(rank.get(aid, {}).get("media_type") == "video" for aid, _ in members)
    if is_video:
        keys = [_video_lead_key(rank.get(aid, {}), config) for aid, _ in members]
        levels = _VIDEO_LEAD_LEVELS
    else:
        keys = [_photo_lead_key(inst, rank.get(aid, {})) for aid, inst in members]
        levels = _PHOTO_LEAD_LEVELS

    # Lead = greatest key, tiebroken by smallest normcase path (deterministic across runs).
    best_i = 0
    for i in range(1, len(members)):
        if keys[i] > keys[best_i] or (
            keys[i] == keys[best_i]
            and os.path.normcase(members[i][1]["path"]) < os.path.normcase(members[best_i][1]["path"])
        ):
            best_i = i
    klead = keys[best_i]

    # Decision level: the shortest key prefix at which the lead's key is unique.
    label = _PATH_TIEBREAK
    for depth in range(len(klead)):
        prefix = klead[: depth + 1]
        if sum(1 for k in keys if k[: depth + 1] == prefix) == 1:
            label = levels[depth]
            break
    return members[best_i][0], label


def _pick_lead(members, rank, config):
    """Keep-lead asset id for a stage-2 group (§8 B); see :func:`_group_lead_and_level`."""
    return _group_lead_and_level(members, rank, config)[0]


def _effective_bitrate(size, duration_s, weight: float) -> float:
    """size/duration × codec weight (§8 B video keep-lead); raw size × weight if no duration."""
    if not size:
        return 0.0
    if duration_s and duration_s > 0:
        return (size / duration_s) * weight
    return size * weight  # no duration → raw size (still weighted); consistent within a group


def _log_band(value: float, tie_pct: float) -> int:
    """Quantize a value to a log-scale band so ~equal values tie (§8 B keep-lead).

    Two values within ``tie_pct`` percent land in the same band → the next ranking
    key decides, instead of a coin-flip on a noisy diff. Log scale so "within X%"
    means the same at any magnitude. ``value<=0`` → a sentinel low band. Used by the
    video keep-lead (effective bitrate → codec weight breaks the tie); the photo
    keep-lead needs no band (format rank + file size are both clean signals, §8 B).
    """
    if value <= 0 or tie_pct <= 0:
        return -1
    return round(math.log(value) / math.log(1.0 + tie_pct / 100.0))
