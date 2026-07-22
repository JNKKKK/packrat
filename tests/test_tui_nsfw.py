"""NSFW redaction (``packrat --nsfw``) — the display-only, value-based masking layer.

Value-based: keywords are matched ONLY against the live roots' name/path (the source of
truth, §8 A1); those literal values (and path components) are then redacted wherever they
appear in the rendered window. So app chrome ("assets", "analyze") can never be corrupted
— only real root-derived text is touched. Two levels: pure unit tests on
:mod:`packrat.tui.nsfw`, then live-app tests that the flag redacts the frame / toasts /
modals while leaving the plain ``current_frame`` and the real names the daemon acts on
untouched.
"""

from __future__ import annotations

import asyncio

from packrat.tui import demo, tokens
from packrat.tui.app import PackratApp
from packrat.tui.layout import cell_width
from packrat.tui.nsfw import (MASK_CHAR, KEYWORDS, build_redactions, mask_text, redact,
                              sensitive_tokens)


# --- pure masker -----------------------------------------------------------
def test_mask_char_is_bar_empty():
    # ░ == BAR_EMPTY so colorize tints a redacted run with the `dim` role (grey block).
    assert MASK_CHAR == tokens.BAR_EMPTY


def test_clean_text_is_returned_unchanged():
    for t in (r"D:\Backup\iPhone", "Camera", "手机相册 2026年家庭照片", ""):
        assert mask_text(t) == t


def test_english_keyword_masked():
    assert mask_text("PornStash") == f"{MASK_CHAR * 4}Stash"


def test_keyword_masked_case_insensitively():
    assert mask_text("MyPORNfolder") == f"My{MASK_CHAR * 4}folder"


def test_chinese_keyword_masked_two_cells_each():
    # 色情 is two CJK chars (2 cells each) → four ░ (one per cell), width preserved.
    out = mask_text("色情片")
    assert out == f"{MASK_CHAR * 4}片"
    assert cell_width(out) == cell_width("色情片")


def test_mask_text_width_is_always_preserved():
    for t in ["porn", "色情", "sextape", "色情片porn", "巨乳_milf_无码"]:
        assert cell_width(mask_text(t)) == cell_width(t)


def test_keywords_are_all_lowercase():
    assert all(k == k.lower() for k in KEYWORDS)


# --- value-based token derivation (the crux of the redesign) ---------------
def _roots(*specs):
    return [{"name": n, "path": p} for n, p in specs]


def test_sensitive_tokens_come_only_from_roots():
    roots = _roots(("PornStash", r"D:\media\色情片\2024"), ("Camera", r"E:\Photos"))
    toks = sensitive_tokens(roots)
    # the sensitive name, the full path, and the keyword-bearing component are tokens
    assert "PornStash" in toks
    assert r"D:\media\色情片\2024" in toks
    assert "色情片" in toks
    # the benign root contributes nothing
    assert "Camera" not in toks and r"E:\Photos" not in toks
    # a benign COMPONENT of the sensitive path is not a token (only keyword-bearing ones)
    assert "media" not in toks and "2024" not in toks


def test_app_chrome_is_never_a_token_even_if_keyword_substring():
    # Even if a keyword were a substring of a chrome word, chrome is not a root value,
    # so it never becomes a redaction token → it can never be corrupted on screen.
    roots = _roots(("Camera", r"E:\Photos"))
    assert sensitive_tokens(roots) == set()
    # 'assets'/'analyze' contain no listed keyword anyway, but the guarantee is structural:
    assert redact("assets  analyze  avi", build_redactions(roots)) == "assets  analyze  avi"


def test_build_redactions_is_longest_first():
    roots = _roots(("PornStash", r"D:\x\色情片\PornStash\sub"))
    reds = build_redactions(roots)
    lengths = [len(tok) for tok, _ in reds]
    assert lengths == sorted(lengths, reverse=True)     # longest token first
    # the full path (longest) precedes its 'PornStash' component
    toks = [t for t, _ in reds]
    assert toks.index(r"D:\x\色情片\PornStash\sub") < toks.index("PornStash")


def test_redact_replaces_values_preserving_width():
    roots = _roots(("PornStash", r"D:\media\色情片\2024"))
    reds = build_redactions(roots)
    # a job label embeds the name → redacted in place, width preserved
    label = "scan PornStash (full)"
    out = redact(label, reds)
    assert "Porn" not in out and MASK_CHAR in out
    assert cell_width(out) == cell_width(label)
    # a review path embeds the path → redacted, keeping cell width (CJK-aware)
    path = r"review: D:\media\色情片\2024\_packrat_review" + "\\"
    outp = redact(path, reds)
    assert "色情" not in outp
    assert cell_width(outp) == cell_width(path)


def test_redact_is_identity_when_no_redactions():
    assert redact("scan PornStash", []) == "scan PornStash"


def test_redact_masks_component_inside_elided_path():
    # When a long path is middle-elided to a fragment, the surviving keyword COMPONENT
    # is still redacted (components are their own tokens).
    roots = _roots(("A", r"\\nas\share\色情片\deep\folder\tree\leaf"))
    reds = build_redactions(roots)
    fragment = r"\\nas\share\色情片\de…ee\leaf"   # elided middle; component survives head
    out = redact(fragment, reds)
    assert "色情" not in out


# --- live app integration --------------------------------------------------
def _install_nsfw_root(app) -> None:
    """Rewrite the top-of-list root to carry an adult keyword in its name + path.

    Set INSIDE the pilot scenario (after mount): offline ``on_mount`` calls
    ``refresh_data()``, which would clobber a pre-set snapshot. We rewrite the
    most-recently-registered root (the dashboard's first row) so it's visible."""
    snap = demo.status_snapshot(running=True)
    top = max(snap["roots"], key=lambda r: r["id"])   # dashboard shows id-DESC first
    top["name"] = "PornStash"
    top["path"] = r"D:\media\色情片\2024"
    app.snapshot = snap
    app._redaction_sig = None                          # force a rebuild off the new roots
    app.screen.refresh_frame()


def test_flag_defaults_off():
    assert PackratApp(offline=True).nsfw is False
    assert PackratApp(offline=True, nsfw=True).nsfw is True


def test_redactions_empty_when_flag_off():
    app = PackratApp(offline=True, nsfw=False)
    app.snapshot = {"roots": [{"name": "PornStash", "path": r"D:\porn"}]}
    assert app.redactions() == []


def test_dashboard_frame_masks_root_but_plain_frame_keeps_truth():
    app = PackratApp(offline=True, nsfw=True)

    async def scenario(app, pilot):
        _install_nsfw_root(app)
        plain = app.screen.current_frame
        # The PLAIN frame (golden-test / snapshot source of truth) keeps the real name.
        assert "PornStash" in plain and "色情" in plain
        # The LIVE widget text (post-mask, post-colorize) is redacted.
        rendered = app.screen._colorize(app.screen._mask(plain)).plain
        assert "Porn" not in rendered and "色情" not in rendered
        assert MASK_CHAR in rendered
        # And the mask keeps every line exactly 100 cells (frame invariant).
        assert all(cell_width(r) == 100 for r in rendered.split("\n"))

    async def runner():
        async with app.run_test(size=(100, 24)) as pilot:
            await scenario(app, pilot)
    asyncio.run(runner())


def test_chrome_word_survives_when_a_root_is_masked():
    # The Collection box's "Assets" label must stay intact while a root is redacted —
    # proof that value-based masking never touches chrome.
    app = PackratApp(offline=True, nsfw=True)

    async def scenario(app, pilot):
        _install_nsfw_root(app)
        rendered = app.screen._colorize(app.screen._mask(app.screen.current_frame)).plain
        assert "Assets" in rendered            # chrome intact
        assert "PornStash" not in rendered     # root redacted

    async def runner():
        async with app.run_test(size=(100, 24)) as pilot:
            await scenario(app, pilot)
    asyncio.run(runner())


def test_masking_off_leaves_frame_untouched():
    # Flag off → _mask is the identity, so the keyword name renders as-is.
    app = PackratApp(offline=True, nsfw=False)

    async def scenario(app, pilot):
        _install_nsfw_root(app)
        plain = app.screen.current_frame
        assert app.screen._mask(plain) == plain    # no-op when the flag is off
        assert "PornStash" in plain

    async def runner():
        async with app.run_test(size=(100, 24)) as pilot:
            await scenario(app, pilot)
    asyncio.run(runner())


def test_toast_is_masked_when_nsfw_on():
    app = PackratApp(offline=True, nsfw=True)

    async def scenario(app, pilot):
        _install_nsfw_root(app)                     # roots → redaction pairs
        app.notify(r"packrat scan PornStash", title=r"open D:\media\色情片\2024")
        await pilot.pause()
        note = list(app._notifications)[-1]
        assert "Porn" not in note.message
        assert "色情" not in note.title
        assert MASK_CHAR in note.message

    async def runner():
        async with app.run_test(size=(100, 24)) as pilot:
            await scenario(app, pilot)
    asyncio.run(runner())


def test_masking_does_not_alter_real_root_name_used_for_actions():
    # Regression guard: masking is DISPLAY-only. The snapshot / sorted_roots the app
    # submits from must keep the true name (else --nsfw would break scan/dedup/open).
    app = PackratApp(offline=True, nsfw=True)

    async def scenario(app, pilot):
        _install_nsfw_root(app)
        assert app.sorted_roots()[0]["name"] == "PornStash"
        assert app.root_path("PornStash") == r"D:\media\色情片\2024"

    async def runner():
        async with app.run_test(size=(100, 24)) as pilot:
            await scenario(app, pilot)
    asyncio.run(runner())
