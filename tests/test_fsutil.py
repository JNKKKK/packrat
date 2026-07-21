"""fsutil.is_network_path — UNC + mapped-drive (DRIVE_REMOTE) detection (§10).

The permanent-delete safety warning for cleanup/dedup keys off this: a NAS is
usually a *mapped drive* (like ``Z:``), which a pure UNC check misses. These tests
pin both cases, mocking the Win32 GetDriveTypeW call so they run cross-platform.
"""

from __future__ import annotations

import packrat.fsutil as fsutil


def test_unc_paths_are_network():
    assert fsutil.is_network_path(r"\\nas\photos\a.jpg")
    assert fsutil.is_network_path(r"\\?\UNC\nas\photos\a.jpg")


def test_local_extended_prefix_is_not_network():
    # The \\?\ LOCAL extended form (\\?\C:\…) is a fixed disk, not a share.
    assert not fsutil.is_network_path(r"\\?\C:\Users\me\a.jpg")


def test_mapped_network_drive_is_network(monkeypatch):
    # Z:\ mapped to a NAS → GetDriveTypeW returns DRIVE_REMOTE (4).
    monkeypatch.setattr(fsutil, "_IS_WINDOWS", True)
    monkeypatch.setattr(fsutil, "_is_remote_drive", lambda p: p.upper().startswith("Z:"))
    assert fsutil.is_network_path(r"Z:\photos\a.jpg")
    assert not fsutil.is_network_path(r"C:\photos\a.jpg")


def test_remote_drive_helper_reads_win32(monkeypatch):
    # _is_remote_drive strips \\?\, extracts the X:\ root, and compares GetDriveTypeW.
    calls = {}

    class _K32:
        def GetDriveTypeW(self, root):
            val = getattr(root, "value", root)   # c_wchar_p → its string
            calls["root"] = val
            return fsutil._DRIVE_REMOTE if str(val).upper().startswith("Z:") else 3

    class _WinDLL:
        kernel32 = _K32()

    import ctypes

    monkeypatch.setattr(ctypes, "windll", _WinDLL(), raising=False)
    assert fsutil._is_remote_drive(r"Z:\photos\a.jpg") is True
    assert fsutil._is_remote_drive(r"\\?\Z:\photos\a.jpg") is True   # extended prefix stripped
    assert fsutil._is_remote_drive(r"C:\photos\a.jpg") is False
    assert fsutil._is_remote_drive("relative\\a.jpg") is False       # no drive letter


def test_remote_drive_helper_swallows_win32_failure(monkeypatch):
    import ctypes

    class _Boom:
        @property
        def kernel32(self):
            raise OSError("no win32 here")

    monkeypatch.setattr(ctypes, "windll", _Boom(), raising=False)
    # Best-effort: a ctypes failure must never raise into the delete path (§10).
    assert fsutil._is_remote_drive(r"Z:\photos\a.jpg") is False
