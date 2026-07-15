r"""Opt-in scan profiler — where does a scan's wall time actually go? (§10.1)

A scan splits its time between **I/O** (byte transfer — disk or network reads),
**CPU** (BLAKE3 hashing, PDQ perceptual hashing, image decode), and shared
**DB/enumeration** overhead. ``scan --profile`` measures that split so you can
tell "82% I/O" from "CPU-bound on PDQ".

**Per-medium sections.** Photos and videos have very different cost shapes, so
buckets are keyed by ``(medium, name)`` and the report prints a **PHOTOS** and a
**VIDEOS** section, each with its own rollup + read throughput, plus a small
**SHARED** footer (enumeration, DB). For photos the producer/consumer pipeline
(§scan) reads each file once in a producer thread and decodes it from RAM in a
consumer — so a photo's ``io`` bucket is *pure* byte transfer and ``decode``/``pdq``
are *pure* CPU (no file descriptor → no hidden lazy reads). Videos keep the
path-based stream+seek pipeline, so their ``decode`` is honestly labelled mixed.

**Bottleneck readout.** The photo pipeline records how often consumers sat idle
waiting for the queue (I/O-bound) vs producers blocked on a full queue
(CPU-bound); the report turns that into a one-line verdict.

Design: zero overhead when off (``NULL_PROFILER`` no-op singleton); thread-safe
accumulation under a lock (bucket seconds are thread-summed — they can exceed
wall time, which is the parallelism signal).
"""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager

#: Per-medium buckets: (name, label, kind). kind ∈ io | cpu | mixed.
#: 'io' is disk-or-network file byte transfer (local NTFS or SMB/NAS alike).
_MEDIUM_BUCKETS: tuple[tuple[str, str, str], ...] = (
    ("io", "I/O: file byte transfer", "io"),
    ("hash", "hash: BLAKE3", "cpu"),
    ("decode", "decode: Pillow/PyAV", "cpu"),
    ("pdq", "pdq: perceptual hash", "cpu"),
)
#: Shared (medium-independent) buckets.
_SHARED_BUCKETS: tuple[tuple[str, str], ...] = (
    ("enumerate", "enumerate (dir round-trips)"),
    ("db", "DB writes"),
)
_MEDIA = ("photo", "video")


class ScanProfiler:
    """Accumulates per-(medium,bucket) time + bytes across scan worker threads."""

    enabled = True

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # secs[(medium, bucket)] and secs[("shared", bucket)].
        self._secs: dict[tuple[str, str], float] = {}
        self._bytes: dict[str, int] = {m: 0 for m in _MEDIA}
        self._files: dict[str, int] = {m: 0 for m in _MEDIA}
        # pipeline bottleneck signals (photo producer/consumer).
        self._consumer_idle_s = 0.0   # consumers waiting on an empty queue → I/O-bound
        self._producer_block_s = 0.0  # producers waiting on a full queue → CPU-bound
        self._t0 = time.perf_counter()

    # -- timing ----------------------------------------------------------
    @contextmanager
    def timer(self, medium: str, bucket: str):
        """Time a block into ``(medium, bucket)``; medium 'shared' for global work."""
        start = time.perf_counter()
        try:
            yield
        finally:
            self._add(medium, bucket, time.perf_counter() - start)

    def add(self, medium: str, bucket: str, seconds: float) -> None:
        self._add(medium, bucket, seconds)

    def _add(self, medium: str, bucket: str, seconds: float) -> None:
        with self._lock:
            key = (medium, bucket)
            self._secs[key] = self._secs.get(key, 0.0) + seconds

    def add_bytes(self, medium: str, n: int) -> None:
        with self._lock:
            self._bytes[medium] = self._bytes.get(medium, 0) + n

    def file_done(self, medium: str) -> None:
        with self._lock:
            self._files[medium] = self._files.get(medium, 0) + 1

    def consumer_idle(self, seconds: float) -> None:
        with self._lock:
            self._consumer_idle_s += seconds

    def producer_blocked(self, seconds: float) -> None:
        with self._lock:
            self._producer_block_s += seconds

    # -- reporting -------------------------------------------------------
    def snapshot(self) -> dict:
        with self._lock:
            return {
                "wall_s": time.perf_counter() - self._t0,
                "secs": dict(self._secs),
                "bytes": dict(self._bytes),
                "files": dict(self._files),
                "consumer_idle_s": self._consumer_idle_s,
                "producer_block_s": self._producer_block_s,
            }

    def report_lines(self) -> list[str]:
        snap = self.snapshot()
        wall = snap["wall_s"] or 1e-9
        secs = snap["secs"]
        total = sum(secs.values()) or 1e-9
        files = snap["files"]
        # Single aggregate throughput = ALL bytes read / wall time — the real link
        # rate. Per-medium MB/s would mislead: photos and videos run in disjoint
        # phases (§scan), so dividing each medium's bytes by the *full* wall
        # understates its in-phase speed and the two don't sum to the aggregate.
        total_bytes = sum(snap["bytes"].values())
        tput = f" · {(total_bytes / (1024 * 1024)) / wall:.1f} MB/s read" if total_bytes else ""
        lines = [
            f"── scan profile ── wall {wall:.1f}s · "
            f"{files.get('photo', 0) + files.get('video', 0)} file(s) "
            f"({files.get('photo', 0)} photo · {files.get('video', 0)} video)"
            f"{tput}"
        ]
        for medium in _MEDIA:
            lines += self._medium_section(medium, secs, snap, total)
        # SHARED footer.
        shared = [(lbl, secs.get(("shared", b), 0.0)) for b, lbl in _SHARED_BUCKETS]
        if any(v > 0 for _l, v in shared):
            lines.append(" SHARED")
            for lbl, v in shared:
                if v > 0:
                    lines.append(f"   {lbl:32s} {v:8.1f}s")
        lines.append(f" measured work {total:.1f}s over {wall:.1f}s wall → ~{total / wall:.1f}x parallelism")
        return lines

    def _medium_section(self, medium: str, secs: dict, snap: dict, total: float) -> list[str]:
        rows = [(lbl, kind, secs.get((medium, b), 0.0)) for b, lbl, kind in _MEDIUM_BUCKETS]
        nfiles = snap["files"].get(medium, 0)
        if nfiles == 0 and all(v == 0 for _l, _k, v in rows):
            return [f" {medium.upper()}S  (none)"]
        nbytes = snap["bytes"].get(medium, 0)
        out = [f" {medium.upper()}S  {nfiles} file(s) · {_fmt_bytes(nbytes)}"]
        for lbl, kind, v in rows:
            if v <= 0:
                continue
            out.append(f"   {lbl:32s} {v:8.1f}s  {100.0 * v / total:5.1f}%  [{kind}]")
        # Per-medium rollup: io vs cpu (+mixed).
        roll: dict[str, float] = {}
        med_total = 0.0
        for _lbl, kind, v in rows:
            roll[kind] = roll.get(kind, 0.0) + v
            med_total += v
        if med_total > 0:
            # io-vs-cpu split for this medium (a meaningful per-medium ratio);
            # the single aggregate read-throughput lives on the header line.
            roll_str = " · ".join(
                f"{k} {100.0 * v / med_total:.0f}%" for k, v in roll.items() if v > 0
            )
            out.append(f"   rollup: {roll_str}")
        # Photo pipeline bottleneck verdict — only when there's meaningful signal
        # (skip on trivially fast runs where both are ~0 and the ratio is noise).
        if medium == "photo":
            idle, block = snap["consumer_idle_s"], snap["producer_block_s"]
            if max(idle, block) >= 1.0:
                if idle > block * 1.5:
                    out.append(
                        f"   bottleneck: consumers idle {idle:.1f}s → I/O-bound "
                        "(reads dominate; more io_workers help only until the link/disk saturates)"
                    )
                elif block > idle * 1.5:
                    out.append(f"   bottleneck: producers blocked {block:.1f}s → CPU-bound (add cpu_workers)")
                else:
                    out.append(f"   bottleneck: balanced (idle {idle:.1f}s / blocked {block:.1f}s)")
        return out


class _NullProfiler:
    """No-op profiler used when ``--profile`` is off — near-zero overhead."""

    enabled = False

    @contextmanager
    def timer(self, medium: str, bucket: str):
        yield

    def add(self, medium: str, bucket: str, seconds: float) -> None:
        pass

    def add_bytes(self, medium: str, n: int) -> None:
        pass

    def file_done(self, medium: str) -> None:
        pass

    def consumer_idle(self, seconds: float) -> None:
        pass

    def producer_blocked(self, seconds: float) -> None:
        pass

    def report_lines(self) -> list[str]:
        return []


#: Shared no-op instance so callers never branch on "is profiling on?".
NULL_PROFILER = _NullProfiler()


def _fmt_bytes(n: int) -> str:
    f = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if f < 1024 or unit == "TB":
            return f"{f:.1f} {unit}"
        f /= 1024
    return f"{f:.1f} TB"
