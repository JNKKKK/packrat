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


@dataclass(frozen=True)
class MatchConfig:
    #: photo PDQ Hamming cutoff (§5.3); the single photo decision — tune tight.
    t_match_photo: int = 32
    #: per-frame PDQ cutoff for video (§5.3); looser, the frame vote reclaims precision.
    t_match_video: int = 90


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
    #: concurrent hashing/decoding streams over SMB (§10.1); 4–8 typical.
    scan_workers: int = 6


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
# Media extensions that become assets (§8 A1). Photo + video are the fixed default set.
raw = false            # include the RAW group (dng cr2 cr3 nef arw raf orf rw2 pef srw); needs rawpy
# photo/video extension lists are editable here too, but default to the §8 A1 closed sets.

[fastpath]
mtime_tolerance_s = 2  # tolerant-mtime skip window (§8 A2 step 4); 0 = strict path+size+mtime

[match]
t_match_photo = 32     # photo PDQ Hamming cutoff (§5.3); the single photo decision — tune tight
t_match_video = 90     # per-frame PDQ cutoff for video (§5.3); looser, the frame vote reclaims precision

[video]
sample_frames        = 12    # frames sampled per video, at segment midpoints (§5.3)
duration_tol_s       = 1.0   # duration pre-filter: absolute floor (§5.3)
duration_tol_pct     = 5.0   # duration pre-filter: relative part (percent)
frame_match_fraction = 0.60  # >= this fraction of comparable frame-pairs must match
min_frame_quality    = 50    # PDQ quality gate; frames below are excluded from the vote
min_comparable_frames = 5    # fewer comparable pairs than this -> no match (insufficient evidence)

[review]
low_quality_hint = 50  # photo PDQ quality below this flags a near-dup pair low_confidence (§5.3, annotate-only)

[smb]
scan_workers = 6       # concurrent hashing/decoding streams over SMB (§10.1); 4-8 typical

[audit]
retention_days = 0     # 0 = keep review audits forever (§8.1); >0 = prune older (deferred knob, §14 #5)
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
    """Coerce a TOML scalar/list to the field's declared type where sensible."""
    # frozenset[str] fields (the extension allowlists) arrive as TOML arrays.
    if f.name in ("photo", "video") and isinstance(value, list):
        return frozenset(str(v).lower().lstrip(".") for v in value)
    if f.type in ("bool", bool):
        return bool(value)
    if f.type in ("int", int):
        return int(value)
    if f.type in ("float", float):
        return float(value)
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
