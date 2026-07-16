"""ScanProfiler: per-medium bucket accumulation, sectioned report, no-op default."""

from __future__ import annotations

from packrat.profiling import NULL_PROFILER, ScanProfiler, _fmt_bytes


def test_timer_and_add_accumulate_per_medium():
    p = ScanProfiler()
    with p.timer("photo", "pdq"):
        pass
    p.add("photo", "io", 2.0)
    p.add("video", "decode", 1.0)
    p.add("shared", "db", 0.5)
    snap = p.snapshot()
    assert snap["secs"][("photo", "io")] == 2.0
    assert snap["secs"][("video", "decode")] == 1.0
    assert snap["secs"][("shared", "db")] == 0.5


def test_bytes_and_files_per_medium():
    p = ScanProfiler()
    p.add_bytes("photo", 2048)
    p.file_done("photo")
    p.file_done("photo")
    p.file_done("video")
    snap = p.snapshot()
    assert snap["bytes"]["photo"] == 2048
    assert snap["files"]["photo"] == 2 and snap["files"]["video"] == 1


def test_report_has_photo_video_shared_sections():
    p = ScanProfiler()
    p.add("photo", "io", 3.0)
    p.add("photo", "pdq", 1.0)
    p.add_bytes("photo", 6 * 1024 * 1024)  # 6 MB / 3s → 2 MB/s
    p.file_done("photo")
    p.add("video", "decode", 2.0)
    p.file_done("video")
    p.add("shared", "db", 0.5)
    blob = "\n".join(p.report_lines())
    assert "scan profile" in blob
    assert "PHOTOS" in blob and "VIDEOS" in blob and "SHARED" in blob
    assert "rollup:" in blob
    assert "MB/s" in blob
    assert "1 photo · 1 video" in blob


def test_bottleneck_verdict_io_bound():
    p = ScanProfiler()
    p.add("photo", "io", 1.0)
    p.file_done("photo")
    p.consumer_idle(5.0)   # consumers waited a lot → I/O-bound
    p.producer_blocked(0.1)
    blob = "\n".join(p.report_lines())
    assert "I/O-bound" in blob


def test_bottleneck_verdict_cpu_bound():
    p = ScanProfiler()
    p.add("photo", "pdq", 1.0)
    p.file_done("photo")
    p.consumer_idle(0.1)
    p.producer_blocked(5.0)  # producers blocked on a full queue → CPU-bound
    blob = "\n".join(p.report_lines())
    assert "CPU-bound" in blob


def test_empty_medium_shows_none():
    p = ScanProfiler()
    p.add("photo", "io", 1.0)
    p.file_done("photo")
    blob = "\n".join(p.report_lines())
    assert "VIDEOS  (none)" in blob


def test_null_profiler_is_noop():
    assert NULL_PROFILER.enabled is False
    with NULL_PROFILER.timer("photo", "pdq"):
        pass
    NULL_PROFILER.add("photo", "io", 1.0)
    NULL_PROFILER.add_bytes("photo", 10)
    NULL_PROFILER.file_done("photo")
    NULL_PROFILER.consumer_idle(1.0)
    NULL_PROFILER.producer_blocked(1.0)
    assert NULL_PROFILER.report_lines() == []


def test_fmt_bytes():
    assert _fmt_bytes(0) == "0.0 B"
    assert _fmt_bytes(1536).endswith("KB")
    assert _fmt_bytes(5 * 1024 * 1024).endswith("MB")
