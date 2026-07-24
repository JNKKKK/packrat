"""Pure stage-1/stage-2 review-stats compute + line-builders (§8 B, review_stats).

No DB or FS — drives the shared module on synthetic ``review_actions`` row-dicts (the
same shape the job builds and the TUI queries), so the numbers the CLI log and the TUI
Review box show are pinned here once.
"""

from __future__ import annotations

from packrat import review_stats as rs


def _perc(group_no, *, is_external=0, is_lead=0, lead_reason=None, distance=0,
          media_type="photo", path="C:/x"):
    return {"kind": "perceptual", "group_no": group_no, "is_external": is_external,
            "is_lead": is_lead, "lead_reason": lead_reason, "distance": distance,
            "media_type": media_type, "path": path}


def test_stage1_split_counts_internal_and_external():
    # asset 1: two internal copies deleted (internal-only group); asset 2: an external
    # survivor kept (mixed); asset 3: --prefer-internal deletes the external copy (mixed).
    rows = [
        {"kind": "exact", "asset_id": 1, "is_external": 0, "reason": "exact-internal"},
        {"kind": "exact", "asset_id": 1, "is_external": 0, "reason": "exact-internal"},
        {"kind": "exact", "asset_id": 2, "is_external": 0, "reason": "exact-external"},
        {"kind": "exact", "asset_id": 3, "is_external": 1, "reason": "exact-internal-preferred"},
    ]
    assert rs.stage1_split(rows) == {
        "to_delete": 4, "internal": 3, "external": 1,
        "groups_internal_only": 1, "groups_mixed": 2,
    }


def test_stage1_lines_renders_split_and_makeup():
    lines = rs.stage1_lines({"to_delete": 3, "internal": 2, "external": 1,
                             "groups_internal_only": 4, "groups_mixed": 1})
    assert lines[0] == "  to delete (exact): 3 file(s)  ·  2 internal, 1 external"
    assert lines[1] == "  group make-up:  4 internal-only · 1 mixed (internal+external)"


def test_stage2_groups_and_members():
    rows = [_perc(1, is_lead=1, lead_reason="resolution"), _perc(1),
            _perc(2, is_lead=1, lead_reason="resolution"), _perc(2)]
    b = rs.perceptual_stats(rows)
    assert b["groups"] == 2 and b["members"] == 4


def test_stage2_lead_tally_split_by_medium():
    rows = [
        _perc(1, is_lead=1, lead_reason="resolution", media_type="photo"), _perc(1),
        _perc(2, is_lead=1, lead_reason="resolution + format", media_type="photo"), _perc(2),
        _perc(3, is_lead=1, lead_reason="resolution", media_type="video"), _perc(3, media_type="video"),
    ]
    b = rs.perceptual_stats(rows)
    # "resolution" appears for BOTH media but must NOT be merged — keyed by (medium, label).
    assert b["lead_by_medium"]["photo"] == {"resolution": 1, "resolution + format": 1}
    assert b["lead_by_medium"]["video"] == {"resolution": 1}


def test_stage2_pdq_histograms_split_by_medium():
    # Photo and video get SEPARATE histograms on their own scales (§8 B). Photo bins are
    # thirds of 0..t_rec (0–2/3–6/7–10); video bins are thirds of 0..t_video + open
    # overflow (0–29/30–59/60–90/91+). A low-distance video lands in the VIDEO 0–29 bin,
    # not a photo bin — the partition is by media_type, not by distance range.
    photos = [_perc(i, distance=d) for i, d in enumerate([0, 2, 5, 8, 10])]
    videos = [_perc(100 + i, distance=d, media_type="video") for i, d in enumerate([5, 40, 90, 120])]
    b = rs.perceptual_stats(photos + videos, stage=2, t_rec=10, t_edit=32, t_video=90)
    assert b["pdq_photo"] == {"0–2": 2, "3–6": 1, "7–10": 2}
    assert b["pdq_video"] == {"0–29": 1, "30–59": 1, "60–90": 1, "91+": 1}
    assert sum(b["pdq_photo"].values()) == len(photos)
    assert sum(b["pdq_video"].values()) == len(videos)


def test_stage3_pdq_histogram_bins_threshold_derived():
    # Stage 3 bins split the recompress+1 .. t_edit band (11..32) into even thirds; every
    # stage-3 photo lands in a real bar (regression: all fell in a single "11+" bucket).
    # Stage 3 is photo-only → pdq_video is empty.
    dists = [11, 15, 18, 24, 25, 32]
    rows = [_perc(i, distance=d) for i, d in enumerate(dists)]
    b = rs.perceptual_stats(rows, stage=3, t_rec=10, t_edit=32)
    assert b["pdq_photo"] == {"11–17": 2, "18–24": 2, "25–32": 2}
    assert b["pdq_video"] == {}
    assert sum(b["pdq_photo"].values()) == len(rows)


def test_stage2_group_makeup_and_suggestion_split():
    rows = [
        # all-internal group
        _perc(1, is_lead=1, lead_reason="resolution"), _perc(1),
        # mixed group suggesting the external copy
        _perc(2, is_external=1, is_lead=1, lead_reason="internal/external preference"), _perc(2),
        # mixed group suggesting the internal copy
        _perc(3, is_external=1), _perc(3, is_lead=1, lead_reason="internal/external preference"),
    ]
    b = rs.perceptual_stats(rows)
    assert b["groups_all_internal"] == 1
    assert b["groups_mixed"] == 2
    assert b["mixed_suggest_external"] == 1
    assert b["mixed_suggest_internal"] == 1


def test_stage2_all_external_group_not_counted_as_all_internal():
    """A group with ONLY external members (unreachable in real stage-2 clusters, but
    guarded) must NOT be lumped into groups_all_internal — it is left uncounted, not
    mislabeled. Regression: the else-branch counted all-external as all-internal."""
    rows = [_perc(1, is_external=1, is_lead=1, lead_reason="resolution"),
            _perc(1, is_external=1)]
    b = rs.perceptual_stats(rows)
    assert b["groups"] == 1
    assert b["groups_all_internal"] == 0     # NOT 1 — no internal member
    assert b["groups_mixed"] == 0            # has_int is False → not mixed either


def test_stage2_keep_suggested_delete_and_network():
    rows = [
        _perc(1, is_lead=1, path="C:/keep.jpg"),
        _perc(1, path="C:/local_drop.jpg"),      # non-lead, local
        _perc(1, path="Z:/nas_drop.jpg"),        # non-lead, network
    ]
    b = rs.perceptual_stats(rows, is_network=lambda p: p.startswith("Z:"))
    assert b["keep_suggested_delete"] == 2       # both non-leads
    assert b["keep_suggested_network"] == 1      # only the Z: one


def test_stage2_lines_omit_empty_medium_column():
    # all-photo → no "videos (" header; all-video → no "photos (".
    photo = rs.perceptual_stats([_perc(1, is_lead=1, lead_reason="resolution"), _perc(1)])
    text = "\n".join(rs.stage2_lines(photo, 90))
    assert "photos (" in text and "videos (" not in text

    video = rs.perceptual_stats([_perc(1, is_lead=1, lead_reason="resolution", media_type="video"),
                             _perc(1, media_type="video")])
    text = "\n".join(rs.stage2_lines(video, 90))
    assert "videos (" in text and "photos (" not in text


def test_stage2_lines_keep_suggested_tip_network_note():
    rows = [_perc(1, is_lead=1, path="C:/k.jpg"), _perc(1, path="Z:/d.jpg")]
    b = rs.perceptual_stats(rows, is_network=lambda p: p.startswith("Z:"))
    text = "\n".join(rs.stage2_lines(b, 90))
    assert "keep suggested" in text and "1 non-leads" in text and "on network" in text


def test_stage2_lines_keep_suggested_false_suppresses_tip():
    """The CLI passes keep_suggested=False (it prints its own tip); the box's keep-suggested
    tip must then be absent. Regression: the CLI log emitted a duplicate tip."""
    rows = [_perc(1, is_lead=1, path="C:/k.jpg"), _perc(1, path="C:/d.jpg")]
    b = rs.perceptual_stats(rows)
    assert not any("keep suggested" in ln for ln in rs.stage2_lines(b, 90, keep_suggested=False))
    assert any("keep suggested" in ln for ln in rs.stage2_lines(b, 90))   # default still shows it


def test_stage2_stats_ignores_exact_rows():
    # A mixed row set (leftover stage-1 exact + stage-2 perceptual) counts only perceptual.
    rows = [{"kind": "exact", "is_external": 1, "path": "C:/x"},
            _perc(1, is_lead=1, lead_reason="resolution"), _perc(1)]
    b = rs.perceptual_stats(rows)
    assert b["members"] == 2 and b["groups"] == 1


# --- dispatch: stats_for_stage / lines_for_stage (the ONE stage→compute/build map) ------
def test_stats_for_stage_dispatches_to_the_right_compute():
    """stats_for_stage(stage) == the per-stage compute it fronts — the dispatch is the ONLY
    stage→compute ladder now, so the three faces can't pick a different builder per stage."""
    exact = [{"kind": "exact", "asset_id": 1, "is_external": 0, "reason": "exact-internal"}]
    assert rs.stats_for_stage(exact, 1) == rs.stage1_split(exact)

    perc = [_perc(1, is_lead=1, lead_reason="resolution", distance=5), _perc(1, distance=5)]
    th = dict(t_rec=10, t_edit=32, t_video=90)
    assert rs.stats_for_stage(perc, 2, thresholds=th) == rs.perceptual_stats(perc, stage=2, **th)
    assert rs.stats_for_stage(perc, 3, thresholds=th) == rs.perceptual_stats(perc, stage=3, **th)


def test_lines_for_stage_matches_per_stage_builders_and_indents():
    """lines_for_stage(bundle, stage) == the per-stage line-builder, with the shared 2-space
    indent applied uniformly (stage 1 bakes it in; stages 2/3 get it here)."""
    s1 = rs.stage1_split([{"kind": "exact", "asset_id": 1, "is_external": 0,
                           "reason": "exact-internal"}])
    assert rs.lines_for_stage(s1, 1, 90) == rs.stage1_lines(s1)

    b2 = rs.perceptual_stats([_perc(1, is_lead=1, lead_reason="resolution"), _perc(1)])
    assert rs.lines_for_stage(b2, 2, 90, keep_suggested=False) == \
        [f"  {ln}" for ln in rs.stage2_lines(b2, 90, keep_suggested=False)]

    b3 = rs.perceptual_stats([_perc(1, distance=12), _perc(1, distance=12)], stage=3)
    assert rs.lines_for_stage(b3, 3, 90) == [f"  {ln}" for ln in rs.stage3_lines(b3, 90)]


def test_thresholds_from_row_reads_snapshot_and_falls_back_on_null():
    """thresholds_from_row reads a run's analyze-time snapshot; a NULL / absent column (a
    run predating the columns) falls back to the _T_* default — the single seam both faces
    feed the snapshot through, so their bins can't diverge for the same run."""
    snap = rs.thresholds_from_row({"t_photo_recompress": 8, "t_photo_edit": 40,
                                   "t_match_video": 100})
    assert snap == {"t_rec": 8, "t_edit": 40, "t_video": 100}
    # A NULL column → its default; a wholly-missing row (None) → all defaults.
    partial = rs.thresholds_from_row({"t_photo_recompress": None, "t_photo_edit": 40,
                                      "t_match_video": None})
    assert partial == {"t_rec": rs._T_RECOMPRESS, "t_edit": 40, "t_video": rs._T_MATCH_VIDEO}
    assert rs.thresholds_from_row(None) == {"t_rec": rs._T_RECOMPRESS, "t_edit": rs._T_EDIT,
                                            "t_video": rs._T_MATCH_VIDEO}


def test_thresholds_from_row_degrades_on_non_numeric_value():
    """A non-int snapshot value (hand-edited dev DB / future writer bug) degrades to the
    default rather than crashing the read-only poll or the staging log — the fallback exists
    precisely so a bad snapshot can never take a face down."""
    out = rs.thresholds_from_row({"t_photo_recompress": "oops", "t_photo_edit": "12",
                                  "t_match_video": 90})
    # "oops" → default; "12" is int-coercible → 12; 90 stays 90.
    assert out == {"t_rec": rs._T_RECOMPRESS, "t_edit": 12, "t_video": 90}


def test_snapshot_thresholds_band_the_histogram_bins():
    """The snapshot's t_rec/t_edit actually move the stage-3 bins (they're load-bearing, not
    cosmetic): the same distances band differently under a non-default snapshot vs the
    default, so a config edit after analyze can't silently rewrite an old run's histogram."""
    dists = [11, 15, 18, 24, 25, 32]
    rows = [_perc(i, distance=d) for i, d in enumerate(dists)]
    default = rs.stats_for_stage(rows, 3, thresholds=rs.thresholds_from_row(None))
    widened = rs.stats_for_stage(rows, 3, thresholds=rs.thresholds_from_row(
        {"t_photo_recompress": 10, "t_photo_edit": 50, "t_match_video": 90}))
    assert default["pdq_photo"] != widened["pdq_photo"]        # different bands → different bins
    assert sum(default["pdq_photo"].values()) == len(rows)     # every distance still binned


def test_ordered_lead_levels_returns_a_fresh_list_each_call():
    """The canonical lead-order is the SINGLE source both faces read; mutating one caller's
    result must not corrupt it (regression: it returned the shared module list). Each call
    yields a fresh, equal list, and mutating one doesn't touch the next."""
    from packrat.jobs.dedup_rank import ordered_lead_levels

    a = ordered_lead_levels()
    b = ordered_lead_levels()
    assert a == b and a is not b          # equal value, distinct objects
    a.append("junk"); a.clear()           # abuse the returned list
    assert ordered_lead_levels() == b     # canonical order is untouched
    assert "resolution" in b
