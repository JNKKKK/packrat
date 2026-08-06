# CLAUDE.md

Guidance for working in this repo. Keep it current: if something here goes stale, fix it.

## What packrat is

A local, GPU-accelerated **daemon + CLI/TUI** for managing (not viewing) a large personal
photo/video collection on Windows: fingerprint-based dedup and an Explorer-driven
"merge new stuff / trash junk" workflow. Tracks assets by **content fingerprint**, never by path,
so files can be freely moved/renamed without the system losing them. Explorer is the review surface;
packrat stages files and the human keeps/removes them.

Two thin clients (CLI, TUI) drive a background daemon that owns the SQLite DB and a single-worker
job queue. See [notes/architecture.md](notes/architecture.md) for why.

## Commands

Managed with [uv](https://docs.astral.sh/uv/).

```sh
uv sync                                   # core runtime (daemon + CLI)
uv sync --extra media --extra dev         # + decode/fingerprint stack + test deps

uv run pytest                             # full test suite
uv run pytest tests/test_jobs.py -v       # one file, verbose

uv run packrat                            # launch the TUI (the default face)
uv run packrat daemon status              # is the daemon up? pid, port, in-flight job
uv run packrat smoke-test --generate      # verify the decode/fingerprint wheels on this machine
```

The daemon **auto-spawns** on first client use — there is no manual "start the server" step.

## Layout

- `src/packrat/` — top-level pure modules: `media`, `matcher`, `queries`, `review`, `review_stats`,
  `trash`, `roots`, `ignore`, `config`, `smoke`, `profiling`.
- `src/packrat/jobs/` — the job implementations (`scan`, `probe`, `dedup`, `merge`, `cleanup`,
  `trash_refresh`, `untrash`) plus the queue, scheduler, and reconciler.
- `src/packrat/daemon/` — the HTTP API (`server`), client, auto-spawn, and single-instance state.
- `src/packrat/db/` — SQLite schema + connection.
- `src/packrat/cli/` — the Typer CLI (a thin client onto the daemon).
- `src/packrat/tui/` — the Textual TUI: a pure render core (`tokens`, `layout`, `geometry`, `render`,
  `screens/*`) under thin widgets (`frames/*`, `app`). Imports without Textual are tested as strings.
- `tests/` — pytest suite mirroring the above.

## Design spec — `notes/`

The full design lives in [`notes/`](notes/README.md), one file per concern. Consult it before
changing behavior; it is the authoritative description of intended behavior. Start at
[notes/README.md](notes/README.md).

## Writing code comments

Comments and docstrings **document the current state of the code only.** They are read by whoever
touches the code next, not as a project journal. When you add or edit a comment:

- **No pointers to the design spec by section number.** Don't write `§8 B` or `see §3`. If a comment
  needs to reference the design, link the relevant `notes/` file by name (e.g. "see
  notes/workflow-dedup.md") or, better, just state the rule inline.
- **No milestone or tracking tags.** Don't tag comments with `M1`–`M8`, `M6 TUI`, code-review finding
  labels (`F1`, `F5`, …), or ticket/issue numbers. (`# noqa: …` lint codes are not tracking tags —
  keep those.)
- **No historical narrative.** Don't write "previously we…", "used to…", "this was changed from…", or
  "no longer does X". Describe what the code does now. If a non-obvious choice needs defending,
  explain the *current* reason ("we stat lazily because an eager pass wastes I/O on cold drives"),
  not the history of how we got here.
- **Explain the non-obvious, skip the obvious.** Prefer comments that capture an invariant, a
  subtle ordering requirement, or a "why not the simpler thing" — the parts a reader can't recover
  from the code itself.

These rules keep comments from decaying into a changelog. The one-time cleanup that removed the old
`§`/milestone/history annotations should not have to be repeated.
