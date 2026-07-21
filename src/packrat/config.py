"""Configuration — ``%APPDATA%\\packrat\\config.toml`` (§9.2).

Lifecycle:
- **Auto-created with commented defaults** on first daemon start (:func:`ensure_config`).
- **Re-read at each job start** (:func:`load_config`), so an edit applies to the next
  job with no daemon restart. A job already running keeps the snapshot it started with.
- **Missing keys fall back** to the built-in default; **unknown keys are ignored with a
  logged warning**; a **malformed file raises** :class:`ConfigError` naming the failure.

Scope is global-only; the one per-root setting (``--ignore`` globs) lives on the ``roots``
row, not here (§9.2).
"""

from __future__ import annotations

import logging
import tomllib
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path

from . import paths

log = logging.getLogger("packrat.config")


class ConfigError(Exception):
    """Raised when ``config.toml`` cannot be parsed (§9.2 malformed-file path)."""


# ---------------------------------------------------------------------------
# Media extension allowlist (§8 A1). The shipped default is a fixed closed set.
# Stored lower-case, no leading dot. RAW is opt-in (allowlist.raw).
# ---------------------------------------------------------------------------
PHOTO_EXTS = frozenset(
    "jpg jpeg jfif png gif bmp tif tiff webp avif heic heif".split()
)
VIDEO_EXTS = frozenset(
    "mp4 m4v mov avi mkv webm wmv flv mpg mpeg m2ts mts 3gp".split()
)
RAW_EXTS = frozenset("dng cr2 cr3 nef arw raf orf rw2 pef srw".split())


@dataclass(frozen=True)
class AllowlistConfig:
    raw: bool = False
    # Extension lists are editable in TOML but default to the §8 A1 closed sets.
    photo: frozenset[str] = PHOTO_EXTS
    video: frozenset[str] = VIDEO_EXTS

    def media_exts(self) -> frozenset[str]:
        """The effective set of extensions that qualify as media (incl. RAW iff enabled)."""
        exts = self.photo | self.video
        if self.raw:
            exts = exts | RAW_EXTS
        return exts


@dataclass(frozen=True)
class FastpathConfig:
    #: tolerant-mtime skip window (§8 A2 step 4); 0 = strict path+size+mtime.
    mtime_tolerance_s: float = 2.0


#: Codec-efficiency weights for the video keep-lead (§8 B). A more-efficient codec's
#: bits are "worth more" → higher weight, so at equal resolution an HEVC master beats
#: an H.264 re-export on effective bitrate (= size/duration × weight). Coarse + tunable;
#: an unknown/missing codec gets weight 1.0 (neutral). H.265 == HEVC (same codec).
DEFAULT_CODEC_WEIGHTS: dict[str, float] = {
    "mpeg2video": 0.5, "mpeg4": 0.5, "msmpeg4v3": 0.5, "wmv3": 0.6, "vc1": 0.7,
    "h264": 1.0, "avc": 1.0,
    "vp8": 1.2, "vp9": 1.5,
    "hevc": 2.0, "h265": 2.0,
    "av1": 2.5,
    "vvc": 3.0, "h266": 3.0,
}


@dataclass(frozen=True)
class MatchConfig:
    #: photo PDQ Hamming cutoffs (§5.3). `t_photo_edit` is the match decision (a pair
    #: with distance ≤ this is a near-dup); `t_photo_recompress` (tighter) BANDS matched
    #: photos into dedup's review stages — ≤ recompress = stage 2 (recompression),
    #: recompress < d ≤ edit = stage 3 (minor edit). recompress < edit. Both need
    #: calibration (§14 #1). `cleanup --perceptual` uses t_photo_edit alone (no banding).
    t_photo_recompress: int = 10
    t_photo_edit: int = 32
    #: per-frame PDQ cutoff for video (§5.3); looser, the frame vote reclaims precision.
    #: Video near-dups are a single frame-vote match (not banded) → all go to dedup stage 2.
    t_match_video: int = 90
    #: downscale each decoded image/frame to this longest edge before PDQ. PDQ on a
    #: full 12MP photo is ~7x slower than on a 512px copy while the hash barely moves
    #: (drift ~7/256, well inside t_photo_edit). 0 = no downscale (hash full-res).
    pdq_max_edge: int = 512
    #: video keep-lead (§8 B): two effective-bitrates within this percent are a "tie"
    #: (log-scale bucket), so the codec-efficiency weight then the path decide — not a
    #: coin-flip on a noisy bitrate diff. Resolution is ranked above this either way.
    video_bitrate_tie_pct: float = 10.0
    #: codec-efficiency weights for the video keep-lead effective bitrate (see above).
    codec_weights: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_CODEC_WEIGHTS))


@dataclass(frozen=True)
class VideoConfig:
    sample_frames: int = 12
    duration_tol_s: float = 1.0
    duration_tol_pct: float = 5.0
    frame_match_fraction: float = 0.60
    min_frame_quality: int = 50
    min_comparable_frames: int = 5


@dataclass(frozen=True)
class ReviewConfig:
    #: photo PDQ quality below this flags a near-dup pair low_confidence (§5.3).
    low_quality_hint: int = 50


@dataclass(frozen=True)
class SmbConfig:
    #: concurrent hashing/decoding streams over SMB (§10.1); 4–8 typical. Used for
    #: the video path (per-file) and as a fallback; the photo pipeline uses the
    #: separate io/cpu worker knobs below.
    scan_workers: int = 6
    #: PHOTO pipeline — decouple I/O from CPU concurrency (§producer-consumer).
    #: io_workers read whole photo files off disk/NAS into a bounded queue (want
    #: high, to saturate the link); cpu_workers hash+decode+PDQ from RAM (want ≈
    #: cores). 0 = auto (io: 2×cores capped at 16; cpu: max(2, cores−2)).
    io_workers: int = 0
    cpu_workers: int = 0
    #: Memory budget for in-flight photo buffers (bytes). Producers block once the
    #: sum of queued+in-flight buffer sizes reaches this, so RAM is bounded by
    #: *bytes* not file count — a burst of large HEIC/RAW can't balloon the queue.
    photo_buffer_budget_bytes: int = 1024 * 1024 * 1024
    #: Photos larger than this are NOT buffered whole — fall back to the path
    #: decode (streamed) so a single pathological file can't exceed the budget. Bytes.
    photo_buffer_max_bytes: int = 128 * 1024 * 1024

    def resolved_io_workers(self) -> int:
        """io_workers, resolving 0 → auto.

        Aims for the §10.1 SMB sweet spot of ~4–8 concurrent read streams: enough
        outstanding requests to hide latency and saturate the link, but not so many
        that parallel reads thrash a single NAS volume with seek contention. A
        single sequential reader can't fill the bandwidth-delay product (it would
        starve the CPU consumers), so this is never 1; it's also bounded by cores.
        """
        if self.io_workers > 0:
            return self.io_workers
        import os

        return min(8, max(4, os.cpu_count() or 4))

    def resolved_cpu_workers(self) -> int:
        """cpu_workers, resolving 0 → auto (cores−2, at least 2)."""
        if self.cpu_workers > 0:
            return self.cpu_workers
        import os

        return max(2, (os.cpu_count() or 4) - 2)


@dataclass(frozen=True)
class AuditConfig:
    #: 0 = keep review audits forever (§8.1); >0 = prune older (deferred pass, §14 #5).
    retention_days: int = 0


@dataclass(frozen=True)
class Config:
    allowlist: AllowlistConfig = field(default_factory=AllowlistConfig)
    fastpath: FastpathConfig = field(default_factory=FastpathConfig)
    match: MatchConfig = field(default_factory=MatchConfig)
    video: VideoConfig = field(default_factory=VideoConfig)
    review: ReviewConfig = field(default_factory=ReviewConfig)
    smb: SmbConfig = field(default_factory=SmbConfig)
    audit: AuditConfig = field(default_factory=AuditConfig)


# ---------------------------------------------------------------------------
# The shipped default file. Written verbatim on first start so defaults are
# always visible + editable (§9.2). Keep in sync with the dataclasses above.
# ---------------------------------------------------------------------------
DEFAULT_CONFIG_TOML = """\
# packrat configuration (%APPDATA%\\packrat\\config.toml)
# Auto-created with defaults on first daemon start. Re-read at each job start,
# so edits apply to the NEXT scan/dedup/merge/cleanup (no daemon restart).
# Missing keys fall back to the built-in default; unknown keys are ignored (logged).

[allowlist]
# Media extensions that become assets. Photo + video are the fixed default set.
raw = false            # include the RAW group (dng cr2 cr3 nef arw raf orf rw2 pef srw); needs rawpy
# photo/video extension lists are editable here too, but default to the closed sets.

[fastpath]
mtime_tolerance_s = 2  # tolerant-mtime skip window; 0 = strict path+size+mtime

[match]
t_photo_recompress = 10  # photo PDQ cutoff for dedup stage 2 (recompression band); tight
t_photo_edit       = 32  # photo PDQ match cutoff; recompress < d ≤ edit → dedup stage 3 (minor edit)
t_match_video      = 90  # per-frame PDQ cutoff for video; looser, the frame vote reclaims precision
pdq_max_edge       = 512 # downscale each image/frame to this longest edge before PDQ (~7x faster; 0 = full-res)
video_bitrate_tie_pct = 10.0  # video keep-lead: effective-bitrates within this % tie → codec then path decide

# Codec-efficiency weights for the video keep-lead's effective bitrate: size/duration × weight.
# A more-efficient codec's bits are worth more (higher weight), so an HEVC master beats an H.264
# re-export at equal resolution. Unknown/missing codec → 1.0. Override only the ones you care about;
# unlisted codecs keep their built-in default. (H.265 == HEVC.)
[match.codec_weights]
h264 = 1.0
hevc = 2.0
av1  = 2.5
vp9  = 1.5
mpeg4 = 0.5

[video]
sample_frames        = 12    # frames sampled per video, at segment midpoints
duration_tol_s       = 1.0   # duration pre-filter: absolute floor
duration_tol_pct     = 5.0   # duration pre-filter: relative part (percent)
frame_match_fraction = 0.60  # >= this fraction of comparable frame-pairs must match
min_frame_quality    = 50    # PDQ quality gate; frames below are excluded from the vote
min_comparable_frames = 5    # fewer comparable pairs than this -> no match (insufficient evidence)

[review]
low_quality_hint = 50  # photo PDQ quality below this flags a near-dup pair low_confidence (annotate-only)

[smb]
scan_workers = 6       # video-path + fallback concurrency; 4-8 typical
io_workers   = 0        # PHOTO pipeline: file-reader threads (0 = auto 4-8, SMB sweet spot)
cpu_workers  = 0        # PHOTO pipeline: hash+decode+pdq threads (0 = auto cores-2)
photo_buffer_budget_bytes = 1073741824  # RAM budget for in-flight photo buffers (1 GB)
photo_buffer_max_bytes    = 134217728   # photos above this bypass buffering (stream, 128 MB)

[audit]
retention_days = 0     # 0 = keep review audits forever; >0 = prune older (deferred knob)
"""


def ensure_config(path: Path | None = None) -> Path:
    """Create ``config.toml`` with commented defaults if it does not exist (§9.2).

    Returns the path. Idempotent — never overwrites an existing file.
    """
    p = path or paths.config_path()
    if not p.exists():
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(DEFAULT_CONFIG_TOML, encoding="utf-8")
        log.info("wrote default config to %s", p)
    return p


def _coerce_section(cls: type, raw: dict, section_name: str) -> object:
    """Build a config dataclass from a raw TOML table.

    Known keys are pulled (with light type coercion); unknown keys are warned
    about and ignored (§9.2 forward-compat). Missing keys keep their default.
    """
    known = {f.name: f for f in fields(cls)}
    kwargs: dict[str, object] = {}
    for key, value in raw.items():
        f = known.get(key)
        if f is None:
            log.warning("unknown config key [%s].%s — ignored", section_name, key)
            continue
        kwargs[key] = _coerce_value(f, value, f"{section_name}.{key}")
    return cls(**kwargs)


def _coerce_value(f, value, dotted: str):
    """Coerce a TOML scalar/list to the field's declared type, or raise ConfigError.

    A malformed value is rejected HERE (at load, naming the bad key — §9.2) rather
    than stored and detonated deep inside a running job: e.g. a scalar where an
    extension list / codec-weight table is expected would otherwise blow up later in
    ``media_exts()`` (``str | frozenset``) or the video keep-lead.
    """
    # frozenset[str] fields (the extension allowlists) arrive as TOML arrays.
    if f.name in ("photo", "video"):
        if not isinstance(value, list):
            raise ConfigError(
                f"[{dotted}] must be an array of extensions (e.g. [\"jpg\", \"png\"]), "
                f"got {type(value).__name__}"
            )
        return frozenset(str(v).lower().lstrip(".") for v in value)
    # codec_weights: a TOML table → {codec (lower): weight (float)}. Merge onto the
    # defaults so a partial user override doesn't drop the built-in codecs.
    if f.name == "codec_weights":
        if not isinstance(value, dict):
            raise ConfigError(
                f"[{dotted}] must be a table of codec = weight (e.g. hevc = 2.0), "
                f"got {type(value).__name__}"
            )
        merged = dict(DEFAULT_CODEC_WEIGHTS)
        for k, v in value.items():
            try:
                merged[str(k).lower()] = float(v)
            except (TypeError, ValueError):
                log.warning("bad codec weight [%s] %s=%r — ignored", dotted, k, v)
        return merged
    try:
        if f.type in ("bool", bool):
            return bool(value)
        if f.type in ("int", int):
            return int(value)
        if f.type in ("float", float):
            return float(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"[{dotted}] must be {f.type} — got {value!r} ({exc})") from exc
    return value


def load_config(path: Path | None = None) -> Config:
    """Parse ``config.toml`` into a :class:`Config` (§9.2).

    Missing file -> all defaults. Missing keys/sections -> per-key defaults.
    Unknown keys -> warned + ignored. Malformed TOML -> :class:`ConfigError`.
    """
    p = path or paths.config_path()
    if not p.exists():
        return Config()
    try:
        raw = tomllib.loads(p.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"malformed config.toml: {exc}") from exc
    except OSError as exc:
        raise ConfigError(f"cannot read config.toml: {exc}") from exc

    kwargs: dict[str, object] = {}
    section_types = {f.name: f.type for f in fields(Config)}
    section_defaults = {f.name: f for f in fields(Config)}
    for name, section_field in section_defaults.items():
        raw_section = raw.get(name)
        if raw_section is None:
            continue  # keep default_factory
        if not isinstance(raw_section, dict):
            raise ConfigError(f"config section [{name}] must be a table")
        # Resolve the concrete dataclass type for this section.
        cls = _section_class(section_field)
        kwargs[name] = _coerce_section(cls, raw_section, name)

    # Warn about unknown top-level tables.
    for name in raw:
        if name not in section_types:
            log.warning("unknown config section [%s] — ignored", name)

    return Config(**kwargs)


def _section_class(section_field) -> type:
    """Return the dataclass type backing a top-level Config section."""
    factory = section_field.default_factory  # type: ignore[attr-defined]
    inst = factory()
    assert is_dataclass(inst)
    return type(inst)
