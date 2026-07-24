r"""Dedup review stats — one compute + one line-builder, shared by CLI and TUI (§8 B).

The stage-1 exact-delete split and the stage-2 keep-lead / PDQ / internal-external
breakdown surface in TWO faces: the ``packrat dedup`` staging log (jobs layer) and the
root-detail Review box (TUI layer). Neither layer may import the other, so the pure
logic lives here — a neutral, dependency-free module both import.

- :func:`stage1_split` / :func:`perceptual_stats` compute a plain dict "bundle" from a list
  of ``review_actions`` row-dicts (works on the in-memory action dicts the job builds
  *and* on DB rows the TUI queries — the field names match). Network classification is
  injected as an ``is_network`` callable so this module does no I/O and stays unit-testable.
- :func:`stage1_lines` / :func:`stage2_lines` / :func:`stage3_lines` turn a bundle into
  display ``list[str]`` of a given width. The TUI wraps them in its Review box; the CLI
  logs them indented. Same text by construction, so the log and the box can't drift.
- :func:`stats_for_stage` / :func:`lines_for_stage` are the **dispatch** — the ONE place
  the ``stage → which compute`` and ``stage → which line-builder`` maps live, so a new
  stage (or a changed ``stage=`` arg) is a one-place edit rather than three hand-written
  ladders across ``queries``/``dedup``/``rootdetail`` (§8 B). Each face calls one entry
  point; :func:`thresholds_from_row` is the seam both feed the run's analyze-time snapshot
  through (see [[review-stats-shared-renderer]]).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable

from .jobs.dedup_rank import ordered_lead_levels

#: PDQ threshold defaults (mirror config.MatchConfig; §5.3) — the fallback histogram bin
#: boundaries when a run predates the analyze-time snapshot columns (§8 B) so its
#: ``review_runs`` row reads NULL. Both faces normally feed the run's OWN snapshotted
#: thresholds through :func:`thresholds_from_row`; these only cover the pre-snapshot rows.
_T_RECOMPRESS, _T_EDIT, _T_MATCH_VIDEO = 10, 32, 90
_OPEN = 10 ** 9   # open-ended top-bin upper bound


def thresholds_from_row(row) -> dict:
    """PDQ histogram thresholds for a run, from its analyze-time snapshot (§8 B).

    ``row`` is the run's ``review_runs`` row (any mapping with the three columns, or
    ``None``). Each snapshotted column that is present feeds the matching bin boundary;
    a NULL / missing one falls back to the :data:`_T_*` default (a run analyzed before the
    columns existed). This is the single seam BOTH faces feed the snapshot through, so the
    CLI staging log and the TUI poll derive identical bins for the same run — config edits
    after analyze can't retroactively rewrite an old run's histogram (the dual-source drift
    the columns close). Keyword names match :func:`perceptual_stats`' threshold params, so
    the result splats straight in: ``perceptual_stats(rows, **thresholds_from_row(run))``.
    """
    def _pick(key, default):
        if row is None:
            return default
        try:
            val = row[key]
        except (KeyError, IndexError, TypeError):
            return default
        if val is None:
            return default
        # A non-int value (hand-edited dev DB, future writer bug) degrades to the default
        # rather than crashing the read-only poll / staging log — the whole point of the
        # fallback is that a bad snapshot can never take a face down.
        try:
            return int(val)
        except (ValueError, TypeError):
            return default
    return {"t_rec": _pick("t_photo_recompress", _T_RECOMPRESS),
            "t_edit": _pick("t_photo_edit", _T_EDIT),
            "t_video": _pick("t_match_video", _T_MATCH_VIDEO)}

#: The text width the line-builders receive at the TUI's reference geometry — the SAME
#: value the Review box passes them there (``_review_text_w(REFERENCE) − 2`` for the 2-space
#: indent). Headless callers (the daemon staging log, with no client terminal size) render
#: to this so the log and the box stay byte-identical (§8 B "can't drift"); the live TUI
#: derives its width from the real geometry instead.
REFERENCE_TEXT_WIDTH = 90


def _thirds(lo: int, hi: int) -> list[tuple[str, int, int]]:
    """Split the inclusive range [lo, hi] into 3 near-even bins → (label, lo, hi) each."""
    span = hi - lo + 1
    a = lo + span // 3 - 1                       # end of bin 1
    b = lo + (2 * span) // 3 - 1                 # end of bin 2
    cuts = [(lo, a), (a + 1, b), (b + 1, hi)]
    return [(f"{x}–{y}" if x != y else f"{x}", x, y) for x, y in cuts]


def _photo_bins(stage: int, t_rec: int, t_edit: int) -> list[tuple[str, int, int]]:
    """Photo histogram bins for a stage — even thirds of the stage's PDQ band (§5.3).

    Stage 2 (recompression) photos band at ``0..t_rec``; stage 3 (minor edits) at
    ``t_rec+1..t_edit``. (Stage 1 is exact, no distance.)"""
    return _thirds(0, t_rec) if stage == 2 else _thirds(t_rec + 1, t_edit)


def _video_bins(t_video: int) -> list[tuple[str, int, int]]:
    """Video histogram bins (stage 2 only) — even thirds of ``0..t_match_video`` + an open
    overflow. Video distance is a *mean* Hamming over comparable frames (matcher §5.3): it
    can be low for a tight match OR exceed ``t_match_video`` (non-matching frames pull the
    mean up), so the scale is independent of the photo thresholds and the top bin is open.
    """
    return _thirds(0, t_video) + [(f"{t_video + 1}+", t_video + 1, _OPEN)]


def _bin(bins: list[tuple[str, int, int]], distances: Iterable[int]) -> dict[str, int]:
    """Tally ``distances`` into ``bins`` (label → count); a distance in no bin is dropped."""
    out = {label: 0 for label, _, _ in bins}
    for d in distances:
        for label, lo, hi in bins:
            if lo <= d <= hi:
                out[label] += 1
                break
    return out

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
    # Group make-up: a stage-1 group (one asset's copies) spans roots iff any of its rows
    # isn't `exact-internal` — a non-internal reason means an external copy is involved
    # (survivor external, or an external delete under --prefer-internal) → mixed.
    mixed_assets = {r.get("asset_id") for r in rows if r.get("reason") != "exact-internal"}
    mixed = len(mixed_assets)
    internal_only = len({r.get("asset_id") for r in rows}) - mixed
    return {"to_delete": len(rows), "internal": len(rows) - external, "external": external,
            "groups_internal_only": internal_only, "groups_mixed": mixed}


def perceptual_stats(rows: Iterable[dict], *, stage: int = 2,
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

    # (b) PDQ-distance histograms — SEPARATE photo and video tallies (partitioned by
    # media_type, not by distance range), since video distance is a mean-Hamming on its
    # own scale (§5.3). Stage 3 is photo-only, so its video tally stays empty.
    def _dists(medium: str) -> list[int]:
        return [r["distance"] for r in rows
                if r.get("distance") is not None
                and (r.get("media_type") == "video") == (medium == "video")]
    pdq_photo = _bin(_photo_bins(stage, t_rec, t_edit), _dists("photo"))
    pdq_video = _bin(_video_bins(t_video), _dists("video")) if stage == 2 else {}

    # (c) group make-up + (stage 2) suggestion split + the keep-suggested delete set — one
    # pass over the groups. A group is *mixed* if it spans both sides, *all-internal* if it
    # has only internal members; an all-external group is unreachable (every group has a
    # target-root member) so it falls into neither count.
    all_internal = mixed = suggest_ext = suggest_int = ks_delete = ks_network = 0
    for members in groups.values():
        has_ext = any(_truthy(m.get("is_external")) for m in members)
        has_int = any(not _truthy(m.get("is_external")) for m in members)
        if has_ext and has_int:
            mixed += 1
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
        elif has_int:
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
        "pdq_photo": pdq_photo,
        "pdq_video": pdq_video,
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
def _makeup_line(internal_only: int, mixed: int, *,
                 internal_label: str = "all-internal", indent: str = "") -> str:
    """The shared internal/mixed group make-up line (§8 B) — one source for the
    ``· N mixed (internal+external)`` tail across all three stages, so it can't drift.

    Stage 1 labels its internal count "internal-only" and bakes in its own indent (its
    callers don't add one); stages 2/3 use "all-internal" and let their caller indent."""
    return (f"{indent}group make-up:  {internal_only} {internal_label} · "
            f"{mixed} mixed (internal+external)")


def stage1_lines(split: dict) -> list[str]:
    """The stage-1 lines: delete count (internal/external) + group make-up (§8 B)."""
    return [
        f"  to delete (exact): {split['to_delete']} file(s)  ·  "
        f"{split['internal']} internal, {split['external']} external",
        _makeup_line(split.get("groups_internal_only", 0), split.get("groups_mixed", 0),
                     internal_label="internal-only", indent="  "),
    ]


def stage3_lines(bundle: dict, width: int) -> list[str]:
    """The stage-3 (minor edits) Review body: group/member count + PDQ histogram + make-up.

    Stage 3 is deliberately UNRANKED (the edited copy may be the keeper, §8 B), so there is
    no keep-lead column and no keep-suggested action — just the near-dup shape."""
    body = [f"{bundle['groups']} near-dup groups / {bundle['members']} members (default-keep)"]
    body += _histogram_lines(bundle["pdq_photo"], width, title="PDQ distance")
    body.append(_makeup_line(bundle["groups_all_internal"], bundle["groups_mixed"]))
    if bundle["groups_mixed"]:
        body.append(
            f"  of the {bundle['groups_mixed']} mixed groups, an external copy is present"
        )
    return body


def _hcolumns(cols: list[list[str]], gap: int = 2) -> list[str]:
    """Join vertical text columns side by side, each padded to its own widest line + ``gap``.

    The last column isn't padded (trailing spaces are stripped). Empty columns are dropped."""
    cols = [c for c in cols if c]
    if not cols:
        return []
    if len(cols) == 1:
        return [s.rstrip() for s in cols[0]]
    widths = [max((len(s) for s in c), default=0) + gap for c in cols]
    n = max(len(c) for c in cols)
    out = []
    for i in range(n):
        row = ""
        for j, c in enumerate(cols):
            cell = c[i] if i < len(c) else ""
            row += cell.ljust(widths[j]) if j < len(cols) - 1 else cell
        out.append(row.rstrip())
    return out


def _fits(lines: list[str], width: int) -> bool:
    """Whether a laid-out block (from :func:`_hcolumns`) stays within ``width`` cells."""
    return max((len(ln) for ln in lines), default=0) <= width


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


def _histogram_lines(pdq: dict, width: int, *, title: str = "PDQ distance") -> list[str]:
    """A small horizontal PDQ histogram (``label bar count``) titled ``title (N)``, laid
    out within ``width`` cells. Empty ``pdq`` → just the titled header + a placeholder."""
    total = sum(pdq.values())
    lines = [f"{title} ({total}):"]
    if total == 0:
        return lines + ["  (none)"]
    peak = max(pdq.values())
    label_w = max(len(k) for k in pdq)
    bar_max = max(4, min(16, width - label_w - 8))
    for label, n in pdq.items():
        bars = "█" * round(bar_max * n / peak) if peak else ""
        lines.append(f"  {label.ljust(label_w)} {bars} {n}".rstrip())
    return lines


def stage2_lines(bundle: dict, width: int, *, keep_suggested: bool = True) -> list[str]:
    """The stage-2 Review body (§8 B): keep-lead columns + two PDQ histograms, group
    make-up, suggestion split, and the ``--keep-suggested`` delete/network tip.

    Four sub-columns lay out left→right: keep-lead **photos**, keep-lead **videos**, **PDQ
    photo** histogram, **PDQ video** histogram. When ``width`` fits all four they render
    side by side under the "keep-lead decided by:" header; otherwise the two histograms
    WRAP onto their own side-by-side row below the keep-lead columns (and, if even that is
    too narrow, stack). Empty medium columns/histograms are omitted.
    """
    lead = bundle["lead_by_medium"]
    lead_cols: list[list[str]] = []
    if sum(lead["photo"].values()):
        lead_cols.append(_lead_column("photos", lead["photo"], _PHOTO_SHORT))
    if sum(lead["video"].values()):
        lead_cols.append(_lead_column("videos", lead["video"], _VIDEO_SHORT))
    if not lead_cols:
        lead_cols = [["(no leads)"]]

    hist_cols: list[list[str]] = []
    if bundle.get("pdq_photo"):
        hist_cols.append(_histogram_lines(bundle["pdq_photo"], width, title="PDQ photo"))
    if any(bundle.get("pdq_video", {}).values()):
        hist_cols.append(_histogram_lines(bundle["pdq_video"], width, title="PDQ video"))

    header = ["keep-lead decided by:"]
    one_row = _hcolumns(lead_cols + hist_cols)
    if hist_cols and _fits(one_row, width):
        # Everything fits on one row: photos · videos · PDQ photo · PDQ video.
        body = header + one_row
    else:
        # Wrap: keep-lead columns under the header, then the two histograms side by side on
        # their own row below; if even that overflows, stack them (bar length is capped by
        # ``width`` in either case, so they never spill — only their side-by-side sum can).
        body = header + _hcolumns(lead_cols)
        hist_row = _hcolumns(hist_cols)
        if len(hist_cols) == 2 and _fits(hist_row, width):
            body += hist_row
        else:
            for h in hist_cols:      # too narrow even for two → stack
                body += h

    body.append(_makeup_line(bundle["groups_all_internal"], bundle["groups_mixed"]))
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


# ---------------------------------------------------------------------------
# stage dispatch (the ONE stage→compute / stage→build map; §8 B) — each face calls
# these two entry points instead of hand-writing the ladder (queries poll, dedup
# staging log, rootdetail render). A new stage 4 is then a one-place edit here.
# ---------------------------------------------------------------------------
#: Number of dedup review stages (exact → recompression → minor-edit). The single source
#: for the "stage N of <count>" phrase every face prints, so adding a stage doesn't leave a
#: stale "of 3" hardcoded across the CLI log and the TUI Review box (§8 B). Mirrors the
#: jobs-layer STAGE_* constants, kept here (the neutral module both faces already import)
#: because the TUI render layer must not import the jobs layer.
N_DEDUP_STAGES = 3


def stats_for_stage(rows: Iterable[dict], stage: int, *, thresholds: dict | None = None,
                    is_network: Callable[[str], bool] = lambda _p: False) -> dict:
    """``stage → the right bundle`` — the ONE place the stage→compute map lives (§8 B).

    Stage 1 is the exact-delete split (no thresholds/network band there); stages 2 & 3 are
    the perceptual bundle, PDQ-banded by the run's ``thresholds`` (a dict as produced by
    :func:`thresholds_from_row`; ``None`` → the ``_T_*`` defaults). ``is_network`` marks a
    path as a non-recyclable share (injected; no I/O here). Callers pass the run's
    analyze-time snapshot so the log and the poll derive identical bins for the same run.
    """
    if stage == 1:
        return stage1_split(rows)
    return perceptual_stats(rows, stage=stage, is_network=is_network, **(thresholds or {}))


def lines_for_stage(bundle: dict, stage: int, width: int, *,
                    keep_suggested: bool = True) -> list[str]:
    """``stage → the right line list``, from a bundle :func:`stats_for_stage` produced (§8 B).

    Every returned line carries the shared 2-space indent (stage 1 bakes it in; stages 2/3
    get it here), so both faces consume the list verbatim — the CLI ``ctx.log(ln)`` and the
    TUI Review box ``detail = [...]`` — with no per-stage indent ladder. ``width`` is the
    text cells the perceptual body lays out to (stage 1 is width-agnostic); ``keep_suggested``
    toggles stage 2's bulk keep-suggested tip (the CLI prints its own, so it passes False).
    """
    if stage == 1:
        return stage1_lines(bundle)
    if stage == 2:
        return [f"  {ln}" for ln in stage2_lines(bundle, width, keep_suggested=keep_suggested)]
    return [f"  {ln}" for ln in stage3_lines(bundle, width)]
