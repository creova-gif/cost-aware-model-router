from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from cost_aware_model_router.cli import _write_auth_config_atomic, main


def _parse_kv_output(stdout: str) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for line in stdout.strip().splitlines():
        key, value = line.split("=", 1)
        parsed[key] = value
    return parsed


def test_write_auth_config_atomic_sets_mode_and_leaves_no_tmp_files(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "auth-registry.json"

    _write_auth_config_atomic(target, '{"version": 1}\n')
    assert target.read_text(encoding="utf-8") == '{"version": 1}\n'
    if os.name == "posix":
        assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert [p for p in target.parent.iterdir() if p.name.endswith(".tmp")] == []

    _write_auth_config_atomic(target, '{"version": 2}\n')
    assert target.read_text(encoding="utf-8") == '{"version": 2}\n'
    if os.name == "posix":
        assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert [p for p in target.parent.iterdir() if p.name.endswith(".tmp")] == []


def test_write_auth_config_atomic_cleans_up_temp_file_on_replace_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "auth-registry.json"
    target.write_text("original\n", encoding="utf-8")

    def fail_replace(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
        raise OSError("simulated replace failure")

    monkeypatch.setattr(os, "replace", fail_replace)

    with pytest.raises(OSError, match="simulated replace failure"):
        _write_auth_config_atomic(target, "updated\n")

    assert target.read_text(encoding="utf-8") == "original\n"
    assert list(tmp_path.iterdir()) == [target]


def test_write_auth_config_atomic_cleans_up_temp_file_on_fsync_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "auth-registry.json"

    def fail_fsync(fd: int) -> None:
        raise OSError("simulated fsync failure")

    monkeypatch.setattr(os, "fsync", fail_fsync)

    with pytest.raises(OSError, match="simulated fsync failure"):
        _write_auth_config_atomic(target, "updated\n")

    assert not target.exists()
    assert list(tmp_path.iterdir()) == []


def test_cli_issue_rotate_revoke_key_produce_0600_auth_config(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    auth_path = tmp_path / "auth-registry.json"

    rc_issue = main(
        [
            "issue-key",
            "--org-id",
            "org_acme",
            "--role",
            "router_client",
            "--out",
            str(auth_path),
        ]
    )
    assert rc_issue == 0
    issue_out = _parse_kv_output(capsys.readouterr().out)
    initial_key_id = issue_out["key_id"]
    if os.name == "posix":
        assert stat.S_IMODE(auth_path.stat().st_mode) == 0o600
    assert [p for p in tmp_path.iterdir() if p.name.endswith(".tmp")] == []

    rc_rotate = main(
        [
            "rotate-key",
            "--key-id",
            initial_key_id,
            "--auth-config",
            str(auth_path),
        ]
    )
    assert rc_rotate == 0
    rotate_out = _parse_kv_output(capsys.readouterr().out)
    rotated_key_id = rotate_out["key_id"]
    if os.name == "posix":
        assert stat.S_IMODE(auth_path.stat().st_mode) == 0o600
    assert [p for p in tmp_path.iterdir() if p.name.endswith(".tmp")] == []

    rc_revoke = main(
        [
            "revoke-key",
            "--key-id",
            rotated_key_id,
            "--auth-config",
            str(auth_path),
        ]
    )
    assert rc_revoke == 0
    _ = capsys.readouterr()
    if os.name == "posix":
        assert stat.S_IMODE(auth_path.stat().st_mode) == 0o600
    assert [p for p in tmp_path.iterdir() if p.name.endswith(".tmp")] == []
