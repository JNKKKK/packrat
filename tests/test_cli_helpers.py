"""CLI helper units — exit-code propagation + error-detail parsing (§11).

The CLI is otherwise a thin daemon client (exercised via the API tests); these pin
the two pure helpers that guard scripting/CI behavior.
"""

from __future__ import annotations

import pytest
import typer

from packrat.cli.main import _detail, _exit_if_failed
from packrat.daemon.client import DaemonError


@pytest.mark.parametrize("final", ["error", "cancelled", "interrupted"])
def test_exit_if_failed_raises_nonzero(final):
    # A failed/cancelled/interrupted streamed job must exit non-zero (CI/scripts).
    with pytest.raises(typer.Exit) as ei:
        _exit_if_failed(final)
    assert ei.value.exit_code == 1


@pytest.mark.parametrize("final", ["done", "detached"])
def test_exit_if_failed_ok_stays_zero(final):
    # 'done' succeeds; 'detached' is a clean Ctrl-C view detach (job still running) —
    # neither is a failure, so no Exit is raised.
    _exit_if_failed(final)   # must not raise


def test_detail_handles_dict_body():
    assert _detail(DaemonError('400: {"detail": "no such root"}')) == "no such root"


def test_detail_tolerates_non_dict_json_body():
    # A list/str JSON error body must not AttributeError on .get — fall back to raw msg.
    for body in ('["a","b"]', '"just a string"', "42"):
        msg = f"400: {body}"
        assert _detail(DaemonError(msg)) == msg


def test_detail_tolerates_non_json_body():
    assert _detail(DaemonError("500: Internal Server Error")) == "500: Internal Server Error"
