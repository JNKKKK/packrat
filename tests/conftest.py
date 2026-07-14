"""Shared test fixtures: isolate PACKRAT_HOME so tests never touch real state."""

from __future__ import annotations

import os

import pytest


@pytest.fixture()
def packrat_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("PACKRAT_HOME", str(home))
    # paths.home_dir() reads the env each call, so nothing else to reset.
    return home
