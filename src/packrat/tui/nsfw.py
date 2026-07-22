"""NSFW keyword masking (``packrat --nsfw``) — a display-only redaction layer.

When the TUI runs with ``--nsfw``, root names and paths that contain adult-content
keywords (Chinese + English) are redacted before they reach the screen — a privacy
affordance for screen-sharing / screenshots of a media collection. Like
:mod:`packrat.tui.colorize`, this runs **post-layout** on composed text, so it is
**display-only**: the plain ``current_frame`` and every real name the daemon acts on
(navigation, submits, Explorer opens) keep the true text.

**Value-based, not frame-scanning.** The data that needs masking originates in exactly
two columns — ``roots.name`` and ``roots.path`` (§8 A1) — so that is the *only* place
we run keyword matching. From the live roots we derive the set of **sensitive tokens**
(a name, a path, or a single path component that contains a keyword), each paired with
its keyword-masked form, and then redact those literal strings wherever they appear in
the rendered window (a root row, a job label like ``scan PornStash``, a review path, a
toast, a modal). Two consequences fall out of this:

- **App chrome can never be corrupted.** We only ever rewrite frame regions that
  *equal a real root name/path/component*; "assets"/"analyze" are not root values, so a
  keyword that is their substring can never redact them. The worst a mis-chosen keyword
  can do is over-mask a genuine root (e.g. flag a "Sussex" folder via "sex") — a
  cosmetic over-redaction, never a chrome bug and never a leak. This is strictly safer
  than scanning the whole frame for keywords.
- **Redaction is cell-width-preserving.** Each masked keyword character becomes
  :data:`MASK_CHAR` (``░``) repeated to that character's display width (a CJK char → two
  ``░``), so a 100×24 frame stays byte-aligned. ``░`` equals
  :data:`packrat.tui.tokens.BAR_EMPTY`, so :func:`colorize` tints a redacted run with
  the ``dim`` role — a grey censored block, which reads correctly.

Known limit: a value is matched **literally**, so a keyword split across an ellipsis by
middle/end elision of a very long path won't be caught. Names (≤ the fixed name column)
and full paths on a wide terminal render verbatim, and a keyword-bearing path *component*
is redacted on its own — so the elision gap is narrow (a keyword cut mid-component by
``…``), and it fails toward *showing* text, never toward corrupting chrome.
"""

from __future__ import annotations

import re

from .layout import char_width

#: The redaction glyph — one terminal cell (== ``tokens.BAR_EMPTY``; see module note).
MASK_CHAR = "░"

#: Split a path into components on either separator (UNC ``\\host\share`` → the empty
#: leading parts are dropped by the truthy filter in :func:`sensitive_tokens`).
_PATH_SEP = re.compile(r"[\\/]")


# Adult-content keywords, matched ONLY against root name/path values (never chrome), so
# the curation is about avoiding *over-masking benign roots*, not about protecting the
# app's vocabulary. Stored lowercase (ASCII matching is case-insensitive; CJK has no
# case). Still avoids ultra-generic stems that would flag ordinary folder names — no bare
# ``anal`` (would flag an "analytics" root), ``ass`` ("Cassette"), or ``av`` ("Avatars").
_KEYWORDS_EN = (
    "porn", "porno", "pornhub", "xvideos", "xhamster", "redtube", "youporn",
    "spankbang", "brazzers", "onlyfans", "javhd", "xxx", "nsfw", "sex", "sextape",
    "nude", "naked", "boobs", "boobies", "milf", "bdsm", "hentai", "ecchi", "lewd",
    "fetish", "hardcore", "softcore", "erotic", "orgasm", "dildo", "incest",
    "voyeur", "upskirt", "bukkake", "gangbang", "threesome", "creampie", "blowjob",
    "cumshot", "deepthroat", "handjob", "footjob", "rimjob", "masturbat", "camgirl",
    "camwhore", "titties", "titjob", "r18",
)

_KEYWORDS_CN = (
    "色情", "情色", "黄片", "黄图", "成人", "成人电影", "三级片", "裸体", "裸照",
    "裸聊", "性爱", "做爱", "情趣", "情趣用品", "淫乱", "淫荡", "淫秽", "自慰",
    "手淫", "高潮", "巨乳", "美乳", "无码", "有码", "里番", "萝莉", "幼女", "偷拍",
    "春宫", "春药", "一夜情", "约炮", "嫖娼", "露出", "口交", "肛交", "群交",
    "乱伦", "强奸", "迷奸", "援交", "内射", "颜射", "潮吹", "足交", "阴道", "阴茎",
    "精液", "18禁", "av女优", "肉肉"
)

#: The full masking vocabulary (all lowercase).
KEYWORDS: tuple[str, ...] = tuple(k.lower() for k in (_KEYWORDS_EN + _KEYWORDS_CN))


def mask_text(text: str) -> str:
    """Redact any :data:`KEYWORDS` occurrence in ``text`` (case-insensitive substring).

    Each matched character becomes :data:`MASK_CHAR` repeated to its display width, so
    the result has the **same cell width** as ``text`` (a CJK char → two ``░``). Text
    with no keyword is returned unchanged (identity). This masks a *single known value*
    (a root name / path / component); frame-wide redaction is :func:`redact`, which
    replaces such values by their masked form only where they literally appear."""
    if not text:
        return text
    lower = text.lower()
    n = len(text)
    redact = bytearray(n)                       # 1 where a char should be masked
    for kw in KEYWORDS:
        klen = len(kw)
        start = 0
        while True:
            i = lower.find(kw, start)
            if i == -1:
                break
            for j in range(i, i + klen):
                redact[j] = 1
            start = i + klen                    # keywords don't overlap themselves
    if not any(redact):
        return text
    out: list[str] = []
    for idx, ch in enumerate(text):
        out.append(MASK_CHAR * char_width(ch) if redact[idx] else ch)
    return "".join(out)


def sensitive_tokens(roots: list[dict]) -> set[str]:
    """The literal strings to redact, sourced ONLY from ``roots`` name/path (§8 A1).

    For each root, a token is added for its ``name``, its ``path``, and each individual
    path **component** — but only when the value contains a keyword (``mask_text`` would
    change it). Components are included so a keyword-bearing folder is still redacted when
    the full path is middle-elided in a narrow row and only that segment survives."""
    toks: set[str] = set()
    for r in roots:
        name, path = r.get("name") or "", r.get("path") or ""
        for value in (name, path):
            if value and mask_text(value) != value:
                toks.add(value)
        for comp in _PATH_SEP.split(path):
            if comp and mask_text(comp) != comp:
                toks.add(comp)
    return toks


def build_redactions(roots: list[dict]) -> list[tuple[str, str]]:
    """``(token, masked_token)`` pairs for ``roots``, **longest token first**.

    Longest-first so a full path is redacted before its own components (and a long name
    before a shorter substring token), avoiding a shorter replacement pre-empting a
    longer one. Empty when nothing is sensitive → :func:`redact` is then a no-op."""
    reds = [(t, mask_text(t)) for t in sensitive_tokens(roots)]
    reds.sort(key=lambda kv: len(kv[0]), reverse=True)
    return reds


def redact(text: str, redactions: list[tuple[str, str]]) -> str:
    """Replace each sensitive ``token`` in ``text`` with its cell-width-equal masked form.

    ``redactions`` comes from :func:`build_redactions` (longest-first). Each replacement
    preserves display width (masked token == token width), so a composed frame stays
    aligned. Identity when ``redactions`` is empty."""
    if not redactions or not text:
        return text
    for token, masked in redactions:
        if token in text:
            text = text.replace(token, masked)
    return text


def mask_obj(obj, redactions: list[tuple[str, str]]):
    """Deep-copy ``obj`` (a read-model dict / list / scalar) with :func:`redact` applied
    to every string leaf — the **pre-layout** masker fed to the pure builders.

    Masking the DATA before layout is what closes the elision leak: a keyword-bearing
    path/name is redacted *before* :func:`~packrat.tui.layout.middle_elide` can split the
    keyword across a ``…`` (post-layout redaction can't match the broken value). Because
    ``redact`` only rewrites literal root name/path values, applying it to every string is
    safe — status/type/timestamp fields (``"done"``, ``"library"``, ISO dates) contain no
    root value, so they pass through untouched and the builders' branch logic is unaffected.
    Returns ``obj`` unchanged when ``redactions`` is empty (``--nsfw`` off)."""
    if not redactions:
        return obj
    if isinstance(obj, str):
        return redact(obj, redactions)
    if isinstance(obj, dict):
        return {k: mask_obj(v, redactions) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return type(obj)(mask_obj(v, redactions) for v in obj)
    return obj
