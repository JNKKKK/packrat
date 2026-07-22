"""Tests for the TUI data/liveness layer's pure logic (reltime, ETA).

These are clock-free (``now`` is passed explicitly) so they're deterministic —
the same discipline the golden-frame tests rely on. They also pin the fixture
timestamps to the exact relative-time strings the mockups show.
"""

from __future__ import annotations

from packrat.tui import fixtures
from packrat.tui.data import EtaEstimator, fmt_eta, reltime

NOW = fixtures.REFERENCE_NOW  # 2026-07-15T13:30:00


# --- reltime ---------------------------------------------------------------
def test_reltime_none_is_never():
    assert reltime(None, NOW) == "never"


def test_reltime_now():
    assert reltime("2026-07-15T13:29:30", NOW) == "now"


def test_reltime_minutes():
    assert reltime("2026-07-15T13:00:00", NOW) == "30m ago"


def test_reltime_hours():
    # iPhone scanned 09:04, now 13:30 → ~4h
    assert reltime("2026-07-15T09:04:00", NOW) == "4h ago"


def test_reltime_same_year_date():
    assert reltime("2026-07-12T15:00:00", NOW) == "Jul 12"


def test_reltime_prior_year():
    assert reltime("2025-12-31T10:00:00", NOW) == "2025 Dec 31"


def test_reltime_today_clock():
    assert reltime("2026-07-15T11:31:00", NOW, clock=True) == "today 11:31"


def test_reltime_matches_mockup_camera_dedup():
    # §2.1: Camera "deduped Jul 12"; iPhone "deduped today"
    cam = next(r for r in fixtures.ROOTS if r["name"] == "Camera")
    assert reltime(cam["last_dedup_at"], NOW) == "Jul 12"


# --- ETA -------------------------------------------------------------------
def test_eta_blank_until_two_samples():
    e = EtaEstimator()
    assert e.eta_s(100) is None
    e.observe(0.0, 0)
    assert e.eta_s(100) is None


def test_eta_linear_rate():
    e = EtaEstimator()
    e.observe(0.0, 0)
    e.observe(10.0, 100)      # 10 units/s
    # 8912/13204 mock: remaining at rate → seconds
    assert e.eta_s(200) == 10.0   # 100 remaining / 10 per s


def test_eta_zero_when_done():
    e = EtaEstimator()
    e.observe(0.0, 0)
    e.observe(10.0, 100)
    assert e.eta_s(100) == 0.0


def test_eta_window_trims_old_samples():
    e = EtaEstimator(window_s=5.0)
    e.observe(0.0, 0)
    e.observe(1.0, 10)
    e.observe(100.0, 1000)    # far outside window → old ones dropped (keep last 2+)
    eta = e.eta_s(2000)
    assert eta is not None and eta > 0


def test_fmt_eta_shapes():
    assert fmt_eta(None) == ""
    assert fmt_eta(45) == "ETA 45s"
    assert fmt_eta(240) == "ETA 4m"
    assert fmt_eta(3720) == "ETA 1h02m"
