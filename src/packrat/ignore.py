r"""The scan ignore set (§8 A1) — what a walk even *looks at*.

Two independent filters, both of which a file must pass to become an asset:

1. **Media extension allowlist** — only these extensions qualify as media
   (photo + video, plus the RAW group iff ``allowlist.raw``). Anything else is
   ignored outright. Comes from :class:`packrat.config.Config`.
2. **Ignore globs** — gitignore-style path globs, matched **relative to the root,
   case-insensitively, with ``/`` as the separator** (§8 A1). Includes the
   built-in junk/system exclusions (``Thumbs.db``, ``desktop.ini``, ``.DS_Store``,
   ``.lnk`` shortcuts, and packrat's own ``_packrat_review\`` staging area) plus
   the per-root ``--ignore`` patterns bound at register time.

Attribute/size-based exclusions (hidden/system attribute, zero-byte) need a
``stat``/``DirEntry`` and so are applied by the scan walker via
:func:`is_junk_dirent`, not here — this module is pure path/glob logic.

Glob semantics (a pragmatic gitignore subset covering the §8 A1 examples):
- ``*`` matches within one segment; ``**`` matches across segments; ``?`` one
  non-separator char; ``[abc]`` a char class.
- A trailing ``/`` matches directories only (``Screenshots/``).
- A pattern with an interior/leading ``/`` is anchored to the root; otherwise it
  matches at any depth (``*.tmp`` skips ``a/b/c.tmp``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .config import Config

#: Fixed junk filenames excluded regardless of config (compared case-insensitively).
JUNK_NAMES = frozenset({"thumbs.db", "desktop.ini", ".ds_store"})

#: packrat's own staging area — never index it or the .lnk shortcuts inside (§8 A1).
REVIEW_DIR = "_packrat_review"

#: Built-in ignore globs folded into every root's set (§8 A1 junk/system list).
BUILTIN_GLOBS = (
    "*.lnk",             # dedup/cleanup shortcuts
    REVIEW_DIR + "/",    # the review staging tree, at any depth
)


def ext_of(name: str) -> str:
    """Lower-case extension without the dot (``IMG.JPG`` -> ``jpg``)."""
    return Path(name).suffix.lower().lstrip(".")


def _translate(pattern: str) -> tuple[re.Pattern[str], bool]:
    """Compile one gitignore-style glob to a ``(regex, dir_only)`` pair.

    The regex matches a full **relative posix path** (lower-cased by the caller).
    """
    dir_only = pattern.endswith("/")
    core = pattern[:-1] if dir_only else pattern
    lead_anchor = core.startswith("/")
    if lead_anchor:
        core = core[1:]
    anchored = lead_anchor or ("/" in core)
    core = core.lower()

    # Translate wildcard tokens to regex. Handle ``**`` (with optional adjacent
    # slash) before the single-char tokens.
    out: list[str] = []
    i, n = 0, len(core)
    while i < n:
        c = core[i]
        if c == "*":
            if i + 1 < n and core[i + 1] == "*":
                # '**' — spans segments. Absorb an adjacent '/' so '**/' becomes
                # "zero or more leading dirs" and '/**' becomes "everything below".
                i += 2
                if i < n and core[i] == "/":
                    out.append("(?:.*/)?")
                    i += 1
                else:
                    out.append(".*")
            else:
                out.append("[^/]*")
                i += 1
        elif c == "?":
            out.append("[^/]")
            i += 1
        elif c == "[":
            j = i + 1
            if j < n and core[j] in ("!", "^"):
                j += 1
            if j < n and core[j] == "]":
                j += 1
            while j < n and core[j] != "]":
                j += 1
            if j >= n:  # unterminated class → literal '['
                out.append(re.escape("["))
                i += 1
            else:
                cls = core[i + 1 : j]
                if cls.startswith(("!", "^")):
                    cls = "^" + cls[1:]
                out.append("[" + cls + "]")
                i = j + 1
        elif c == "/":
            out.append("/")
            i += 1
        else:
            out.append(re.escape(c))
            i += 1

    body = "".join(out)
    if not anchored:
        body = "(?:.*/)?" + body
    return re.compile(body + r"\Z"), dir_only


@dataclass
class IgnoreSet:
    """Compiled allowlist + ignore globs for one root (§8 A1)."""

    media_exts: frozenset[str]
    _rules: list[tuple[re.Pattern[str], bool]]

    @classmethod
    def build(cls, config: Config, extra_globs: list[str] | None = None) -> "IgnoreSet":
        """Assemble from config's allowlist + built-ins + the root's ``--ignore`` set."""
        globs = list(BUILTIN_GLOBS) + list(extra_globs or [])
        return cls(
            media_exts=config.allowlist.media_exts(),
            _rules=[_translate(g) for g in globs if g.strip()],
        )

    # -- allowlist -------------------------------------------------------
    def is_media(self, name: str) -> bool:
        """True if the filename's extension is in the media allowlist."""
        return ext_of(name) in self.media_exts

    # -- glob matching (rel is a posix, root-relative path) --------------
    def _matches(self, rel: str, *, is_dir: bool) -> bool:
        rel = rel.lower()
        for regex, dir_only in self._rules:
            if dir_only and not is_dir:
                continue
            if regex.match(rel):
                return True
        return False

    def is_file_ignored(self, rel: str) -> bool:
        """True if a file at root-relative posix path ``rel`` is excluded by a glob."""
        # Basename-level junk (Thumbs.db, .DS_Store, …) — cheap, always applied.
        if Path(rel).name.lower() in JUNK_NAMES:
            return True
        return self._matches(rel, is_dir=False)

    def is_dir_pruned(self, rel: str) -> bool:
        """True if a directory subtree can be skipped whole during the walk.

        Prunes when the directory itself matches a rule, or when a rule reaches
        *inside* it (e.g. ``**/cache/**`` prunes any ``cache`` dir). The second
        case is tested with a sentinel child so the walk needn't descend to
        discover that everything below is ignored.
        """
        if Path(rel).name == REVIEW_DIR:
            return True
        rel_l = rel.lower()
        for regex, dir_only in self._rules:
            if regex.match(rel_l):
                return True
            if not dir_only and regex.match(rel_l + "/\x00"):
                return True
        return False


def is_junk_dirent(size: int | None, attrs: int) -> str | None:
    """Classify a file by size/Win32 attributes; return a reason or ``None``.

    Applied by the walker where a ``DirEntry``/``stat`` is in hand (§8 A1
    hidden/system/zero-byte exclusions). ``attrs`` is ``stat_result.st_file_attributes``
    (0 when unavailable, e.g. non-Windows).
    """
    # FILE_ATTRIBUTE_HIDDEN = 0x2, FILE_ATTRIBUTE_SYSTEM = 0x4.
    if attrs & 0x2:
        return "hidden"
    if attrs & 0x4:
        return "system"
    if size == 0:
        return "zero-byte"
    return None
