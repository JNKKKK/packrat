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
    b = rs.stage2_stats(rows)
    assert b["groups"] == 2 and b["members"] == 4


def test_stage2_lead_tally_split_by_medium():
    rows = [
        _perc(1, is_lead=1, lead_reason="resolution", media_type="photo"), _perc(1),
        _perc(2, is_lead=1, lead_reason="resolution + format", media_type="photo"), _perc(2),
        _perc(3, is_lead=1, lead_reason="resolution", media_type="video"), _perc(3, media_type="video"),
    ]
    b = rs.stage2_stats(rows)
    # "resolution" appears for BOTH media but must NOT be merged — keyed by (medium, label).
    assert b["lead_by_medium"]["photo"] == {"resolution": 1, "resolution + format": 1}
    assert b["lead_by_medium"]["video"] == {"resolution": 1}


def test_stage2_pdq_histogram_bins_threshold_derived():
    # Stage 2 bins (t_rec=10, t_video=90): photo thirds 0–2/3–6/7–10, then video 11–50 /
    # 51–90 / 91+ (mean-Hamming can exceed t_match_video, so the top bin is open).
    dists = [0, 2, 5, 8, 10, 40, 90, 120]
    rows = [_perc(i, distance=d) for i, d in enumerate(dists)]
    b = rs.stage2_stats(rows, stage=2, t_rec=10, t_edit=32, t_video=90)
    # 0,2 → 0–2 ; 5 → 3–6 ; 8,10 → 7–10 ; 40 → 11–50 ; 90 → 51–90 ; 120 → 91+
    assert b["pdq"] == {"0–2": 2, "3–6": 1, "7–10": 2, "11–50": 1, "51–90": 1, "91+": 1}
    assert sum(b["pdq"].values()) == len(rows)


def test_stage3_pdq_histogram_bins_threshold_derived():
    # Stage 3 bins split the recompress+1 .. t_edit band (11..32) into even thirds; every
    # stage-3 photo lands in a real bar (regression: all fell in a single "11+" bucket).
    dists = [11, 15, 18, 24, 25, 32]
    rows = [_perc(i, distance=d) for i, d in enumerate(dists)]
    b = rs.stage2_stats(rows, stage=3, t_rec=10, t_edit=32)
    assert b["pdq"] == {"11–17": 2, "18–24": 2, "25–32": 2}
    assert sum(b["pdq"].values()) == len(rows)


def test_stage2_group_makeup_and_suggestion_split():
    rows = [
        # all-internal group
        _perc(1, is_lead=1, lead_reason="resolution"), _perc(1),
        # mixed group suggesting the external copy
        _perc(2, is_external=1, is_lead=1, lead_reason="internal/external preference"), _perc(2),
        # mixed group suggesting the internal copy
        _perc(3, is_external=1), _perc(3, is_lead=1, lead_reason="internal/external preference"),
    ]
    b = rs.stage2_stats(rows)
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
    b = rs.stage2_stats(rows)
    assert b["groups"] == 1
    assert b["groups_all_internal"] == 0     # NOT 1 — no internal member
    assert b["groups_mixed"] == 0            # has_int is False → not mixed either


def test_stage2_keep_suggested_delete_and_network():
    rows = [
        _perc(1, is_lead=1, path="C:/keep.jpg"),
        _perc(1, path="C:/local_drop.jpg"),      # non-lead, local
        _perc(1, path="Z:/nas_drop.jpg"),        # non-lead, network
    ]
    b = rs.stage2_stats(rows, is_network=lambda p: p.startswith("Z:"))
    assert b["keep_suggested_delete"] == 2       # both non-leads
    assert b["keep_suggested_network"] == 1      # only the Z: one


def test_stage2_lines_omit_empty_medium_column():
    # all-photo → no "videos (" header; all-video → no "photos (".
    photo = rs.stage2_stats([_perc(1, is_lead=1, lead_reason="resolution"), _perc(1)])
    text = "\n".join(rs.stage2_lines(photo, 90))
    assert "photos (" in text and "videos (" not in text

    video = rs.stage2_stats([_perc(1, is_lead=1, lead_reason="resolution", media_type="video"),
                             _perc(1, media_type="video")])
    text = "\n".join(rs.stage2_lines(video, 90))
    assert "videos (" in text and "photos (" not in text


def test_stage2_lines_keep_suggested_tip_network_note():
    rows = [_perc(1, is_lead=1, path="C:/k.jpg"), _perc(1, path="Z:/d.jpg")]
    b = rs.stage2_stats(rows, is_network=lambda p: p.startswith("Z:"))
    text = "\n".join(rs.stage2_lines(b, 90))
    assert "keep suggested" in text and "1 non-leads" in text and "on network" in text


def test_stage2_lines_keep_suggested_false_suppresses_tip():
    """The CLI passes keep_suggested=False (it prints its own tip); the box's keep-suggested
    tip must then be absent. Regression: the CLI log emitted a duplicate tip."""
    rows = [_perc(1, is_lead=1, path="C:/k.jpg"), _perc(1, path="C:/d.jpg")]
    b = rs.stage2_stats(rows)
    assert not any("keep suggested" in ln for ln in rs.stage2_lines(b, 90, keep_suggested=False))
    assert any("keep suggested" in ln for ln in rs.stage2_lines(b, 90))   # default still shows it


def test_stage2_stats_ignores_exact_rows():
    # A mixed row set (leftover stage-1 exact + stage-2 perceptual) counts only perceptual.
    rows = [{"kind": "exact", "is_external": 1, "path": "C:/x"},
            _perc(1, is_lead=1, lead_reason="resolution"), _perc(1)]
    b = rs.stage2_stats(rows)
    assert b["members"] == 2 and b["groups"] == 1
