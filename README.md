# packrat

**Personal Media Collection Manager**

packrat helps you wrangle a large personal photo and video collection that's
scattered across many folders and drives. It treats everything as one big
collection, keeps track of what you have, and helps you fold in new stuff while
weeding out the junk — without ever losing sight of a file just because you
moved or renamed it.

It's built for the hoarder who still wants a system: keep everything, but keep
it organized.

## What it does

- **Merges new exports in** — point it at a fresh dump (say, your whole iPhone)
  and it copies over only the items that are genuinely new to your collection.
- **Finds duplicates** — spots both exact copies and near-duplicates (resized,
  re-compressed, or re-encoded versions of the same photo or video) so you can
  clean them up.
- **Remembers your trash** — once you throw something out, it stays out, even if
  it shows up again in a future export.
- **Tracks by content, not location** — files are identified by what they are,
  so you're free to reorganize your folders however you like.

packrat doesn't show you your photos — your file explorer already does that.
Instead, it works quietly in the background to keep the whole collection tidy.

## Getting started

packrat is managed with [uv](https://docs.astral.sh/uv/). Install dependencies once:

```sh
uv sync                       # core runtime (daemon + CLI)
uv sync --extra media --extra dev   # + decode/fingerprint stack + test deps
```

## Running the daemon

The daemon **auto-spawns** the first time you run any command, so you rarely need
to start it by hand. The lifecycle commands are there for control and troubleshooting:

```sh
uv run packrat daemon start    # explicitly spawn the detached daemon (no-op if up)
uv run packrat daemon status   # is it running? pid, port, in-flight job
uv run packrat daemon stop     # graceful shutdown (an in-flight job is left resumable)
```

Check collection state anytime (read-only, never blocked by a running job):

```sh
uv run packrat status          # collection rollup + any running/interrupted job
```

### Dev-only commands (M0 scaffolding)

These aren't part of the planned CLI surface — they exist to exercise the job
runtime before the real operations (`scan`/`dedup`/`merge`) land in M1+:

```sh
uv run packrat demo            # submit + stream a throwaway demo job
uv run packrat jobs            # list recent job runs (the TUI will own this later)
```

`demo` streams its progress live; Ctrl-C detaches the view but the job keeps
running in the daemon, exactly like a real job will.

## Development

Run the unit tests:

```sh
uv run pytest                  # full suite
uv run pytest -q               # quiet
uv run pytest tests/test_jobs.py -v   # one file, verbose
```

Confirm the decode/fingerprint wheels work on your machine (the §9.1 smoke test):

```sh
uv run packrat smoke-test                    # report which deps are available
uv run packrat smoke-test --generate         # synthesize samples, then run the full path
uv run packrat smoke-test path/to/samples    # decode → hash → perceptual over your own samples
```

`--generate` synthesizes one sample per photo/video format in a temp dir, so it's
a one-command self-test. RAW formats (cr3, dng, …) can't be synthesized — point
the command at a folder of real camera files to exercise those.
