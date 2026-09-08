"""Remote execution helpers: everything that has to happen on the DSpace host
(the `dspace` CLI, psql, assetstore file writes, chown) goes through here.

Local-only work (SAF building from a CSV already on this machine, calling
external APIs like OpenAlex/Unpaywall, LLM enrichment) does NOT use this
module -- it runs in-process. See dspace/cli.py vs scholar/*.py.
"""

from __future__ import annotations

import shlex
import subprocess
from dataclasses import dataclass

from .config import Profile


class RemoteError(RuntimeError):
    def __init__(self, result: "CommandResult", context: str = ""):
        self.result = result
        msg = f"{context}: exit {result.returncode}\nstderr: {result.stderr}"
        super().__init__(msg)


@dataclass
class CommandResult:
    returncode: int
    stdout: str
    stderr: str
    command: str  # the remote command string that was run, for audit/logging

    @property
    def ok(self) -> bool:
        return self.returncode == 0


def _ssh_base_args(profile: Profile) -> list[str]:
    args = [
        "ssh",
        "-o", "ConnectTimeout=15",
        "-o", "BatchMode=yes" if not profile.ssh.password else "BatchMode=no",
        "-p", str(profile.ssh.port),
    ]
    if profile.ssh.identity_file:
        args += ["-i", profile.ssh.identity_file]
    args.append(f"{profile.ssh.user}@{profile.ssh.host}")
    return args


def run(
    profile: Profile,
    command: str,
    timeout: int = 60,
    check: bool = False,
) -> CommandResult:
    """Run `command` as the login user on the profile's SSH host."""
    args = _ssh_base_args(profile) + [command]
    proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    result = CommandResult(proc.returncode, proc.stdout, proc.stderr, command)
    if check and not result.ok:
        raise RemoteError(result, f"ssh command failed: {command!r}")
    return result


def sudo_run(
    profile: Profile,
    command: str,
    timeout: int = 120,
    check: bool = False,
) -> CommandResult:
    """Run `command` as root on the profile's SSH host.

    Prefers passwordless sudo (requires a NOPASSWD sudoers entry for this
    user/command -- the recommended setup). Falls back to piping
    ssh.sudo_password only if it is configured, for parity with hosts that
    don't have NOPASSWD set up yet. The password (if used) is passed via
    stdin to `sudo -S`, never as a command-line argument, but note it is
    still briefly visible to `ps` on the remote host via the `printf`
    invocation -- this is a known limitation shared with how this
    repository's maintenance has always been done; configure NOPASSWD sudo
    to eliminate it entirely.
    """
    noninteractive = f"sudo -n {command}"
    result = run(profile, noninteractive, timeout=timeout)
    if result.ok:
        return result

    if not profile.ssh.sudo_password:
        if check:
            raise RemoteError(
                result,
                "sudo -n failed and no sudo_password is configured; either "
                "grant NOPASSWD sudo for this command or set ssh.sudo_password",
            )
        return result

    quoted_pw = shlex.quote(profile.ssh.sudo_password)
    piped = f"printf '%s\\n' {quoted_pw} | sudo -S -p '' {command}"
    result = run(profile, piped, timeout=timeout)
    if check and not result.ok:
        raise RemoteError(result, f"sudo command failed: {command!r}")
    return result


def scp_to(profile: Profile, local_path: str, remote_path: str, timeout: int = 120) -> CommandResult:
    args = ["scp", "-o", "ConnectTimeout=15", "-P", str(profile.ssh.port)]
    if profile.ssh.identity_file:
        args += ["-i", profile.ssh.identity_file]
    args += [local_path, f"{profile.ssh.user}@{profile.ssh.host}:{remote_path}"]
    proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    return CommandResult(proc.returncode, proc.stdout, proc.stderr, f"scp {local_path} -> {remote_path}")


def scp_from(profile: Profile, remote_path: str, local_path: str, timeout: int = 120) -> CommandResult:
    args = ["scp", "-o", "ConnectTimeout=15", "-P", str(profile.ssh.port)]
    if profile.ssh.identity_file:
        args += ["-i", profile.ssh.identity_file]
    args += [f"{profile.ssh.user}@{profile.ssh.host}:{remote_path}", local_path]
    proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    return CommandResult(proc.returncode, proc.stdout, proc.stderr, f"scp {remote_path} -> {local_path}")


def chown_assetstore(profile: Profile) -> CommandResult:
    """Fix ownership after any root-run write to the assetstore.

    DSpace 6's `dspace import` must run as root (see cli.py), which leaves
    new assetstore files owned by root; Tomcat (running as
    profile.tomcat_user) then can't serve or filter them until this runs.
    """
    cmd = f"chown -R {profile.tomcat_user}:{profile.tomcat_user} {shlex.quote(profile.assetstore.root)}"
    return sudo_run(profile, cmd, check=True)


# ---- Postgres access -------------------------------------------------

_ROW_SEP = "\x1f"  # unit separator: safe delimiter unlikely to appear in DB text


def psql_query(
    profile: Profile,
    sql: str,
    timeout: int = 60,
) -> list[list[str]]:
    """Run a read/write SQL statement and return rows as lists of strings.

    Uses TCP + password auth (profile.db) by default. If profile.db.ssh_peer_user
    is set, runs `sudo -u <user> psql` locally on the host instead (matches the
    MGIMO backup-cron pattern where only the `postgres` OS user has passwordless
    local access).
    """
    escaped_sql = sql.replace("'", "'\\''")
    if profile.db.ssh_peer_user:
        inner = f"psql -Atc '{escaped_sql}' -F'{_ROW_SEP}' {shlex.quote(profile.db.name)}"
        cmd = f"sudo -u {shlex.quote(profile.db.ssh_peer_user)} {inner}"
        result = run(profile, cmd, timeout=timeout, check=True)
    else:
        pw = profile.db.password or ""
        cmd = (
            f"PGPASSWORD={shlex.quote(pw)} psql -h {shlex.quote(profile.db.host)} "
            f"-p {profile.db.port} -U {shlex.quote(profile.db.user)} "
            f"-d {shlex.quote(profile.db.name)} -Atc '{escaped_sql}' -F'{_ROW_SEP}'"
        )
        result = run(profile, cmd, timeout=timeout, check=True)

    rows = []
    for line in result.stdout.splitlines():
        if line == "":
            continue
        rows.append(line.split(_ROW_SEP))
    return rows


def psql_execute(profile: Profile, sql: str, timeout: int = 60) -> CommandResult:
    """Run a SQL statement for its side effects (INSERT/UPDATE/DDL), no row parsing.

    Callers performing writes should go through guards.require_db_write first.
    """
    escaped_sql = sql.replace("'", "'\\''")
    if profile.db.ssh_peer_user:
        inner = f"psql -Atc '{escaped_sql}' {shlex.quote(profile.db.name)}"
        cmd = f"sudo -u {shlex.quote(profile.db.ssh_peer_user)} {inner}"
    else:
        pw = profile.db.password or ""
        cmd = (
            f"PGPASSWORD={shlex.quote(pw)} psql -h {shlex.quote(profile.db.host)} "
            f"-p {profile.db.port} -U {shlex.quote(profile.db.user)} "
            f"-d {shlex.quote(profile.db.name)} -Atc '{escaped_sql}'"
        )
    return run(profile, cmd, timeout=timeout, check=True)


def dspace_cli(
    profile: Profile,
    args: str,
    as_root: bool = False,
    timeout: int = 300,
) -> CommandResult:
    """Invoke the `dspace` CLI on the remote host with extra JAVA_OPTS applied."""
    java_opts_prefix = f"JAVA_OPTS={shlex.quote(profile.java_opts_extra)} " if profile.java_opts_extra else ""
    cmd = f"{java_opts_prefix}{shlex.quote(profile.cli_path)} {args}"
    if as_root:
        return sudo_run(profile, cmd, timeout=timeout)
    return run(profile, cmd, timeout=timeout)
