from __future__ import annotations

import pytest

from cost_aware_model_router import __version__
from cost_aware_model_router.cli import build_parser


def test_cli_version_flag(capsys: pytest.CaptureFixture[str]) -> None:
    parser = build_parser()
    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args(["--version"])
    assert exc_info.value.code == 0
    captured = capsys.readouterr()
    assert captured.out.strip() == f"cost-router {__version__}"
