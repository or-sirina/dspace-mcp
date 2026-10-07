"""Secret redaction, sshpass handling, and backup-gating of DB writes."""

import subprocess

import pytest

from dspace_mcp import guards, ssh
from dspace_mcp.config import DBConfig, Profile, SSHConfig
from dspace_mcp.dspace import admin

DBPW = "s3cr3t-db-pw"
SSHPW = "ssh-pw-123"
SUDOPW = "sudo-pw-456"


@pytest.fixture
def profile():
    return Profile(
        name="t",
        dspace_version="6.3",
        admin_eperson="admin@example.org",
        ssh=SSHConfig(host="h", user="u", identity_file="/k", sudo_password=SUDOPW),
        db=DBConfig(password=DBPW),
    )


def _fake_subprocess(monkeypatch, returncode=1, stderr=""):
    seen = []

    def fake_run(args, **kw):
        seen.append((args, kw))
        err = stderr or f"boom while running {' '.join(args)}"
        return subprocess.CompletedProcess(args, returncode, "", err)

    monkeypatch.setattr(subprocess, "run", fake_run)
    return seen


def test_redact_helper(profile):
    text = f"PGPASSWORD={DBPW} x '{SUDOPW}'"
    out = ssh.redact(profile, text)
    assert DBPW not in out and SUDOPW not in out and "***" in out


def test_psql_password_not_in_argv_or_error(profile, monkeypatch):
    seen = _fake_subprocess(monkeypatch)
    with pytest.raises(ssh.RemoteError) as ei:
        ssh.psql_query(profile, "select 1")
    argv, kw = seen[0]
    assert DBPW not in " ".join(argv)
    assert kw["input"] == DBPW + "\n"
    assert DBPW not in str(ei.value)
    assert DBPW not in ei.value.result.command


def test_stderr_echoing_password_is_redacted(profile, monkeypatch):
    _fake_subprocess(monkeypatch, stderr=f"auth failed for {DBPW}")
    with pytest.raises(ssh.RemoteError) as ei:
        ssh.run(profile, "true", check=True)
    assert DBPW not in str(ei.value)


def test_sudo_password_sent_via_stdin_not_argv(profile, monkeypatch):
    seen = _fake_subprocess(monkeypatch)
    with pytest.raises(ssh.RemoteError) as ei:
        ssh.sudo_run(profile, "ls", check=True)
    assert SUDOPW not in str(ei.value)
    argv, kw = seen[-1]
    assert SUDOPW not in " ".join(argv)
    assert kw["input"] == SUDOPW + "\n"


def test_timeout_message_redacted(profile, monkeypatch):
    def fake_run(args, **kw):
        raise subprocess.TimeoutExpired(args, 1)

    monkeypatch.setattr(subprocess, "run", fake_run)
    profile.ssh.sudo_password = None
    with pytest.raises(subprocess.TimeoutExpired) as ei:
        ssh.run(profile, f"echo {DBPW}")
    assert DBPW not in str(ei.value)


def test_ssh_password_without_sshpass_raises(profile, monkeypatch):
    profile.ssh.password = SSHPW
    monkeypatch.setattr(ssh.shutil, "which", lambda name: None)
    _fake_subprocess(monkeypatch, returncode=0)
    with pytest.raises(ssh.SSHAuthError) as ei:
        ssh.run(profile, "true")
    assert "identity_file" in str(ei.value)
    assert SSHPW not in str(ei.value)


def test_ssh_password_with_sshpass_uses_env(profile, monkeypatch):
    profile.ssh.password = SSHPW
    monkeypatch.setattr(ssh.shutil, "which", lambda name: "/usr/bin/sshpass")
    seen = _fake_subprocess(monkeypatch, returncode=0)
    ssh.run(profile, "true")
    argv, kw = seen[0]
    assert argv[:2] == ["sshpass", "-e"]
    assert SSHPW not in " ".join(argv)
    assert kw["env"]["SSHPASS"] == SSHPW


def test_failed_backup_blocks_write():
    gate = guards.check_db_write(True, lambda: {"status": "error", "stderr": "disk full"}, "op")
    assert gate["status"] == "error" and gate["stage"] == "backup"
    assert gate["executed"] is False
    assert guards.is_stop(gate)


def test_raising_backup_blocks_write():
    def boom():
        raise RuntimeError("nope")

    gate = guards.check_db_write(True, boom, "op")
    assert guards.is_stop(gate)


def test_dry_run_does_not_backup():
    called = []
    gate = guards.check_db_write(True, lambda: called.append(1) or {"status": "ok"}, "op", confirm=False)
    assert called == []
    assert not guards.is_stop(gate)


@pytest.mark.parametrize("which", ["logo", "intranet"])
def test_admin_tools_stop_on_failed_backup_and_skip_on_dry_run(profile, monkeypatch, tmp_path, which):
    if which == "logo":
        png = tmp_path / "x.png"
        png.write_bytes(b"\x89PNG")
        fn, args = admin.set_collection_logo, ("123/4", str(png))
    else:
        fn, args = admin.set_intranet_access, ("10.0.0.0/8",)
    calls = []

    def fake_backup(p, keep_days=14):
        calls.append(1)
        return {"status": "error", "stderr": "x"}

    monkeypatch.setattr(admin, "db_backup", fake_backup)
    monkeypatch.setattr(ssh, "run", lambda *a, **k: ssh.CommandResult(0, "", "", ""))

    dry = fn(profile, *args, confirm=False, allow_db_write=True)
    assert calls == []
    assert dry.get("status") != "error"

    real = fn(profile, *args, confirm=True, allow_db_write=True)
    assert calls == [1]
    assert real["status"] == "error" and real["stage"] == "backup"
