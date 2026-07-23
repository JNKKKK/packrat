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

from .jobs.dedup_rank import ordered_lead_levels

#: PDQ threshold defaults (mirror config.MatchConfig; §5.3) — used for the histogram bin
#: boundaries when a caller doesn't pass live thresholds (the read-only TUI poll doesn't
#: re-read config; the CLI job path passes ctx.config's values).
_T_RECOMPRESS, _T_EDIT, _T_MATCH_VIDEO = 10, 32, 90
_OPEN = 10 ** 9   # open-ended top-bin upper bound


def _thirds(lo: int, hi: int) -> list[tuple[str, int, int]]:
    """Split the inclusive range [lo, hi] into 3 near-even bins → (label, lo, hi) each."""
    span = hi - lo + 1
    a = lo + span // 3 - 1                       # end of bin 1
    b = lo + (2 * span) // 3 - 1                 # end of bin 2
    cuts = [(lo, a), (a + 1, b), (b + 1, hi)]
    return [(f"{x}–{y}" if x != y else f"{x}", x, y) for x, y in cuts]


def _pdq_bins(stage: int, t_rec: int, t_edit: int, t_video: int) -> list[tuple[str, int, int]]:
    """Histogram bins for a stage, derived from the PDQ thresholds (§5.3, §8 B).

    - **Stage 2** (recompression): photos band at ``0..t_rec`` → even thirds; video
      near-dups run on their own frame-vote scale (mean Hamming, can exceed ``t_match``),
      so they get coarse bins ``t_rec+1..t_video`` + an open overflow. Photo and video
      bins share one axis but their labels keep them legible.
    - **Stage 3** (minor edits): photos only, band ``t_rec+1..t_edit`` → even thirds.
    """
    if stage == 3:
        return _thirds(t_rec + 1, t_edit)
    # stage 2: photo thirds of the recompress band, then video bins beyond it.
    lo_v = t_rec + 1
    mid_v = lo_v + (t_video - lo_v) // 2
    return _thirds(0, t_rec) + [
        (f"{lo_v}–{mid_v}", lo_v, mid_v),
        (f"{mid_v + 1}–{t_video}", mid_v + 1, t_video),
        (f"{t_video + 1}+", t_video + 1, _OPEN),
    ]

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


def _group_makeup(grouped: Iterable[list[dict]]) -> tuple[int, int]:
    """Count ``(internal_only, mixed)`` over groups of ``review_actions`` (§8 B).

    A group is *mixed* if it spans both an internal and an external member, else
    *internal-only*. (An all-external group is unreachable — every group has a target-root
    member — so it's folded into neither; see :func:`stage2_stats`.)"""
    internal_only = mixed = 0
    for members in grouped:
        has_ext = any(_truthy(m.get("is_external")) for m in members)
        has_int = any(not _truthy(m.get("is_external")) for m in members)
        if has_ext and has_int:
            mixed += 1
        elif has_int:
            internal_only += 1
    return internal_only, mixed


def stage1_split(rows: Iterable[dict]) -> dict:
    """Stage-1 exact-delete breakdown: files to delete (internal/external) + group make-up.

    ``rows`` are the stage-1 ``review_actions`` (all default-DELETE). ``is_external`` marks
    a file *outside* the target root — nonzero only under ``--prefer-internal`` (§8 B). A
    stage-1 "group" is one asset (its redundant copies): exact dups carry no ``group_no``,
    so group by ``asset_id``. A group is internal-only when the copies being deleted are
    all internal (``exact-internal``), mixed when the delete set reaches an external copy
    (``exact-external`` keeps an external survivor; ``exact-internal-preferred`` deletes an
    external copy) — i.e. the same has-internal/has-external test as stages 2/3, applied to
    the asset's copies (deleted + the recorded survivor).
    """
    rows = [r for r in rows if r.get("kind") == "exact"]
    external = sum(1 for r in rows if _truthy(r.get("is_external")))
    # Group make-up: a stage-1 group spans roots iff its asset's copies aren't all internal.
    # `exact-internal` = internal-only; any other reason means an external copy is involved
    # (survivor external, or an external delete under --prefer-internal) → mixed.
    by_asset: dict = {}
    for r in rows:
        by_asset.setdefault(r.get("asset_id"), []).append(r)
    internal_only = mixed = 0
    for members in by_asset.values():
        if all(m.get("reason") == "exact-internal" for m in members):
            internal_only += 1
        else:
            mixed += 1
    return {"to_delete": len(rows), "internal": len(rows) - external, "external": external,
            "groups_internal_only": internal_only, "groups_mixed": mixed}


def stage2_stats(rows: Iterable[dict], *, stage: int = 2,
                 t_rec: int = _T_RECOMPRESS, t_edit: int = _T_EDIT,
                 t_video: int = _T_MATCH_VIDEO,
                 is_network: Callable[[str], bool] = lambda _p: False) -> dict:
    """Compute the perceptual (stage-2 or stage-3) review bundle from ``review_actions`` (§8 B).

    Returns groups/members, a PDQ-distance histogram (bins derived from the thresholds for
    ``stage``), the internal/external group make-up, and — for stage 2 only — the keep-lead
    pick tally by medium, the mixed-group suggestion split, and the ``--keep-suggested``
    delete set size (non-lead members) with its network exposure. Stage 3 is unranked
    (no keep-lead), so those keep-lead/suggestion fields come back empty/zero.
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
    bins = _pdq_bins(stage, t_rec, t_edit, t_video)
    pdq = {label: 0 for label, _, _ in bins}
    for r in rows:
        d = r.get("distance")
        if d is None:
            continue
        for label, lo, hi in bins:
            if lo <= d <= hi:
                pdq[label] += 1
                break

    # (c) group make-up + (stage 2) suggestion split + the keep-suggested delete set.
    all_internal, mixed = _group_makeup(groups.values())
    suggest_ext = suggest_int = ks_delete = ks_network = 0
    for members in groups.values():
        has_ext = any(_truthy(m.get("is_external")) for m in members)
        has_int = any(not _truthy(m.get("is_external")) for m in members)
        if has_ext and has_int:
            # Every stage-2 group has ≥1 internal member (cluster built from edges with a
            # target-root endpoint), so a mixed group always has a suggested lead and these
            # two counts sum to `mixed` — for fresh data. A leadless mixed group (only stale
            # rows predating is_lead) is dropped from the split; make-up above still has it.
            lead = next((m for m in members if _truthy(m.get("is_lead"))), None)
            if lead is not None:
                if _truthy(lead.get("is_external")):
                    suggest_ext += 1
                else:
                    suggest_int += 1
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
    """The stage-1 lines: delete count (internal/external) + group make-up (§8 B)."""
    lines = [f"  to delete (exact): {split['to_delete']} file(s)  ·  "
             f"{split['internal']} internal, {split['external']} external"]
    lines.append(
        f"  group make-up:  {split.get('groups_internal_only', 0)} internal-only · "
        f"{split.get('groups_mixed', 0)} mixed (internal+external)"
    )
    return lines


def _makeup_line(bundle: dict) -> str:
    """The shared internal/mixed group make-up line for stages 2/3 (§8 B)."""
    return (f"group make-up:  {bundle['groups_all_internal']} all-internal · "
            f"{bundle['groups_mixed']} mixed (internal+external)")


def stage3_lines(bundle: dict, width: int) -> list[str]:
    """The stage-3 (minor edits) Review body: group/member count + PDQ histogram + make-up.

    Stage 3 is deliberately UNRANKED (the edited copy may be the keeper, §8 B), so there is
    no keep-lead column and no keep-suggested action — just the near-dup shape."""
    body = [f"{bundle['groups']} near-dup groups / {bundle['members']} members (default-keep)"]
    body += _histogram_lines(bundle["pdq"], width)
    body.append(_makeup_line(bundle))
    if bundle["groups_mixed"]:
        body.append(
            f"  of the {bundle['groups_mixed']} mixed groups, an external copy is present"
        )
    return body


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


def _lead_column(title: str, tally: dict[str, int], short: dict[str, str]) -> list[str]:
    """One medium's keep-lead column: a ``title (N)`` header + a ``count · label`` per level.

    ``tally`` is per-medium (only its own medium's groups feed it), so ``label in tally``
    already restricts to this medium's levels + the shared tiebreaks — no extra filter
    needed. ``ordered_lead_levels()`` gives the canonical best-first row order shared with
    the CLI log, so the two can't drift."""
    lines = [f"{title} ({sum(tally.values())})"]
    for label in ordered_lead_levels():
        if label in tally:
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
        cols.append(_lead_column("photos", lead["photo"], _PHOTO_SHORT))
    if sum(lead["video"].values()):
        cols.append(_lead_column("videos", lead["video"], _VIDEO_SHORT))

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

    body.append(_makeup_line(bundle))
    if bundle["groups_mixed"]:
        body.append(
            f"  of the {bundle['groups_mixed']} mixed groups →  "
            f"{bundle['mixed_suggest_external']} suggest external · "
            f"{bundle['mixed_suggest_internal']} suggest internal"
        )
    if keep_suggested and bundle["keep_suggested_delete"]:
        net = bundle["keep_suggested_network"]
        net_note = f" ({net} on network)" if net else ""
        body.append(
            f"tip: confirm · keep suggested — deletes "
            f"{bundle['keep_suggested_delete']} non-leads{net_note}"
        )
    return body
