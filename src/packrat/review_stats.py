r"""Dedup review stats — one compute + one line-builder, shared by CLI and TUI (§8 B).

The stage-1 exact-delete split and the stage-2 keep-lead / PDQ / internal-external
breakdown surface in TWO faces: the ``packrat dedup`` staging log (jobs layer) and the
root-detail Review box (TUI layer). Neither layer may import the other, so the pure
logic lives here — a neutral, dependency-free module both import.

- :func:`stage1_split` / :func:`stage2_stats` compute a plain dict "bundle" from a list
  of ``review_actions`` row-dicts (works on the in-memory action dicts the job builds
  *and* on DB rows the TUI queries — the field names match). Network classification is
  injected as an ``is_network`` callable so this module does no I/O and stays unit-testable.
- :func:`stage1_lines` / :func:`stage2_lines` turn a bundle into display ``list[str]`` of
  a given width. The TUI wraps them in its Review box; the CLI logs them indented. Same
  text by construction, so the log and the box can't drift.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable

from .jobs.dedup_rank import _PHOTO_LEAD_LEVELS, _VIDEO_LEAD_LEVELS, ordered_lead_levels

#: PDQ-distance histogram bins (§8 B): (label, lo, hi) inclusive; last bin is open-ended.
_PDQ_BINS: tuple[tuple[str, int, int], ...] = (
    ("0–2", 0, 2), ("3–5", 3, 5), ("6–10", 6, 10), ("11+", 11, 10 ** 9),
)

#: Short video-level labels for the narrow TUI column (the shared ``resolution`` prefix
#: is implied). Keyed by the full label so the ordering helper stays the source of truth.
_VIDEO_SHORT = {
    "resolution": "resolution",
    "resolution + bitrate": "+ bitrate",
    "resolution + bitrate + codec": "+ bitrate + codec",
    "resolution + bitrate + codec + fine bitrate": "+ fine bitrate",
}
_PHOTO_SHORT = {
    "resolution": "resolution",
    "resolution + format": "+ format",
    "resolution + format + size": "+ format + size",
}


def _truthy(v) -> bool:
    return bool(v)


def stage1_split(rows: Iterable[dict]) -> dict:
    """Stage-1 exact-delete breakdown: total files to delete, split internal/external.

    ``rows`` are the stage-1 ``review_actions`` (all default-DELETE). ``is_external`` marks
    a file *outside* the target root — nonzero only under ``--prefer-internal`` (§8 B).
    """
    rows = [r for r in rows if r.get("kind") == "exact"]
    external = sum(1 for r in rows if _truthy(r.get("is_external")))
    return {"to_delete": len(rows), "internal": len(rows) - external, "external": external}


def stage2_stats(rows: Iterable[dict], *,
                 is_network: Callable[[str], bool] = lambda _p: False) -> dict:
    """Compute the stage-2 review bundle from perceptual ``review_actions`` rows (§8 B).

    Returns groups/members, the keep-lead pick tally split by medium, a PDQ-distance
    histogram, the internal/external group make-up + suggestion split, and the
    ``--keep-suggested`` delete set size (non-lead members) with its network exposure.
    ``is_network`` classifies a path as a non-recyclable share (injected; no I/O here).
    """
    rows = [r for r in rows if r.get("kind") == "perceptual"]
    groups: dict = {}
    for r in rows:
        groups.setdefault(r.get("group_no"), []).append(r)

    # (a) keep-lead pick tally, keyed by (medium, label). Homogeneous groups → one medium.
    lead_by_medium: dict[str, dict[str, int]] = {"photo": {}, "video": {}}
    for r in rows:
        if not _truthy(r.get("is_lead")):
            continue
        medium = "video" if r.get("media_type") == "video" else "photo"
        label = r.get("lead_reason") or ""
        lead_by_medium[medium][label] = lead_by_medium[medium].get(label, 0) + 1

    # (b) PDQ-distance histogram over every member with a known distance.
    pdq = {label: 0 for label, _, _ in _PDQ_BINS}
    for r in rows:
        d = r.get("distance")
        if d is None:
            continue
        for label, lo, hi in _PDQ_BINS:
            if lo <= d <= hi:
                pdq[label] += 1
                break

    # (c) group make-up + suggestion split, and the keep-suggested delete set.
    all_internal = mixed = suggest_ext = suggest_int = 0
    ks_delete = ks_network = 0
    for members in groups.values():
        has_ext = any(_truthy(m.get("is_external")) for m in members)
        has_int = any(not _truthy(m.get("is_external")) for m in members)
        if has_ext and has_int:
            mixed += 1
            lead = next((m for m in members if _truthy(m.get("is_lead"))), None)
            if lead is not None:
                if _truthy(lead.get("is_external")):
                    suggest_ext += 1
                else:
                    suggest_int += 1
        else:
            all_internal += 1
        # --keep-suggested deletes every non-lead member (deterministic from is_lead).
        for m in members:
            if not _truthy(m.get("is_lead")):
                ks_delete += 1
                if is_network(m.get("path") or ""):
                    ks_network += 1

    return {
        "groups": len(groups),
        "members": len(rows),
        "lead_by_medium": lead_by_medium,
        "pdq": pdq,
        "groups_all_internal": all_internal,
        "groups_mixed": mixed,
        "mixed_suggest_external": suggest_ext,
        "mixed_suggest_internal": suggest_int,
        "keep_suggested_delete": ks_delete,
        "keep_suggested_network": ks_network,
    }


# ---------------------------------------------------------------------------
# line-builders (bundle → list[str]); pure text, width-parameterized
# ---------------------------------------------------------------------------
def stage1_lines(split: dict) -> list[str]:
    """The stage-1 count line(s): total to delete + internal/external split (§8 B)."""
    return [f"  to delete (exact): {split['to_delete']} file(s)  ·  "
            f"{split['internal']} internal, {split['external']} external"]


def _two_column(left: list[str], right: list[str], width: int) -> list[str]:
    """Join two vertical lists side by side within ``width`` (left ~half, right the rest)."""
    if not right:
        return left
    if not left:
        return right
    lcol = max((len(s) for s in left), default=0) + 2
    lcol = min(lcol, max(1, width - 8))
    n = max(len(left), len(right))
    out = []
    for i in range(n):
        l = left[i] if i < len(left) else ""
        r = right[i] if i < len(right) else ""
        out.append((l.ljust(lcol) + r).rstrip() if r else l.rstrip())
    return out


def _lead_column(title: str, tally: dict[str, int], levels, short: dict[str, str]) -> list[str]:
    """One medium's keep-lead column: a ``title (N)`` header + a ``count · label`` per level."""
    total = sum(tally.values())
    lines = [f"{title} ({total})"]
    # ordered_lead_levels() drives the row order for BOTH media (shared source of truth);
    # we only emit the levels that belong to this medium AND have a count.
    for label in ordered_lead_levels():
        if label in tally and (label in levels or label not in (*_PHOTO_LEAD_LEVELS, *_VIDEO_LEAD_LEVELS)):
            lines.append(f"  {tally[label]:>3} · {short.get(label, label)}")
    return lines


def _histogram_lines(pdq: dict, width: int) -> list[str]:
    """A small horizontal PDQ histogram (``label bar count``) within ``width`` cells."""
    total = sum(pdq.values())
    lines = ["PDQ distance (%d):" % total]
    if total == 0:
        return lines + ["  (no distances)"]
    peak = max(pdq.values())
    label_w = max(len(k) for k in pdq)
    bar_max = max(4, min(16, width - label_w - 8))
    for label, n in pdq.items():
        bars = "█" * round(bar_max * n / peak) if peak else ""
        lines.append(f"  {label.ljust(label_w)} {bars} {n}".rstrip())
    return lines


def stage2_lines(bundle: dict, width: int, *, keep_suggested: bool = True) -> list[str]:
    """The stage-2 Review body (§8 B): keep-lead columns + PDQ histogram, group make-up,
    suggestion split, and the ``--keep-suggested`` delete/network tip.

    ``width`` is the usable text width. The keep-lead photo/video columns render
    side-by-side with the histogram to their right; if too narrow the histogram drops to
    its own block below. An empty medium column is omitted.
    """
    lead = bundle["lead_by_medium"]
    cols: list[list[str]] = []
    if sum(lead["photo"].values()):
        cols.append(_lead_column("photos", lead["photo"], _PHOTO_LEAD_LEVELS, _PHOTO_SHORT))
    if sum(lead["video"].values()):
        cols.append(_lead_column("videos", lead["video"], _VIDEO_LEAD_LEVELS, _VIDEO_SHORT))

    lead_block = ["keep-lead decided by:"]
    if len(cols) == 2:
        lead_block += _two_column(cols[0], cols[1], width)
    elif cols:
        lead_block += cols[0]
    else:
        lead_block += ["  (no leads)"]

    lead_w = max((len(s) for s in lead_block), default=0)
    # Size the histogram against the width LEFT of the lead columns so the side-by-side
    # rows never overflow ``width``; a min viable histogram needs ~20 cells. Otherwise
    # render it full-width as its own block below (the narrow fallback, §8 B).
    remaining = width - lead_w - 2
    if remaining >= 20:
        hist = _histogram_lines(bundle["pdq"], remaining)
        body = _two_column(lead_block, [""] + hist, width)  # align hist title to the columns row
    else:
        body = lead_block + _histogram_lines(bundle["pdq"], width)

    body.append(
        f"group make-up:  {bundle['groups_all_internal']} all-internal · "
        f"{bundle['groups_mixed']} mixed (internal+external)"
    )
    if bundle["groups_mixed"]:
        body.append(
            f"  of the {bundle['groups_mixed']} mixed groups →  "
            f"{bundle['mixed_suggest_external']} suggest external · "
            f"{bundle['mixed_suggest_internal']} suggest internal"
        )
    if keep_suggested and bundle["keep_suggested_delete"]:
        net = bundle["keep_suggested_network"]
        net_note = f" ({net} on network ⚠)" if net else ""
        body.append(
            f"tip: [b] confirm --keep-suggested — deletes "
            f"{bundle['keep_suggested_delete']} non-leads{net_note}"
        )
    return body
