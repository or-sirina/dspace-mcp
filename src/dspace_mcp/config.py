"""Profile & secret loading for dspace-mcp.

Profiles live in ~/.config/dspace-mcp/config.toml (or $DSPACE_MCP_CONFIG).
Secrets are never stored in plaintext in that file: any string value of the
form "env:VAR_NAME" is resolved from the environment at load time. This lets
the TOML file be safe to keep in a dotfiles repo while keeping real
credentials (SSH password, DB password, LLM/API keys) in the shell
environment or a local .env loaded by the caller.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


DEFAULT_CONFIG_PATH = Path.home() / ".config" / "dspace-mcp" / "config.toml"


class ConfigError(RuntimeError):
    pass


def _resolve(value: Any) -> Any:
    """Resolve "env:VAR_NAME" string values from the environment."""
    if isinstance(value, str) and value.startswith("env:"):
        var_name = value[4:]
        resolved = os.environ.get(var_name)
        if resolved is None:
            raise ConfigError(
                f"config references env:{var_name} but that environment "
                f"variable is not set"
            )
        return resolved
    if isinstance(value, dict):
        return {k: _resolve(v) for k, v in value.items()}
    return value


@dataclass
class SSHConfig:
    host: str
    user: str
    port: int = 22
    identity_file: str | None = None  # path to ssh private key; None = use ssh-agent/default
    # Password auth is discouraged (plaintext in memory / command history on the
    # remote host) but supported for parity with how this repo has been run so
    # far. Prefer identity_file.
    password: str | None = None
    sudo_password: str | None = None  # often same as `password` on MGIMO


@dataclass
class DBConfig:
    host: str = "127.0.0.1"
    port: int = 5432
    name: str = "dspace"
    user: str = "dspace"
    password: str | None = None
    # If set, DB reads/writes go through `ssh <host> psql ...` using peer auth
    # as this OS user (e.g. "postgres") instead of TCP+password. Matches the
    # MGIMO backup-cron pattern.
    ssh_peer_user: str | None = None


@dataclass
class AssetstoreConfig:
    root: str = "/dspace/assetstore"
    store_number: int = 0
    directory_levels: int = 3
    digits_per_level: int = 2


@dataclass
class ApiKeysConfig:
    unpaywall_email: str | None = None
    crossref_mailto: str | None = None
    openalex_mailto: str | None = None
    core_api_key: str | None = None
    semanticscholar_api_key: str | None = None
    llm_provider: str = "deepseek"
    llm_base_url: str = "https://api.deepseek.com/v1"
    llm_model: str = "deepseek-chat"
    llm_api_key: str | None = None


@dataclass
class Profile:
    name: str
    dspace_version: str  # e.g. "6.3"
    ui: str = "jspui"  # jspui | xmlui | dspace-angular
    cli_path: str = "/dspace/bin/dspace"
    install_root: str = "/dspace"
    admin_eperson: str = ""
    handle_prefix: str = ""
    java_opts_extra: str = "-Djavax.accessibility.assistive_technologies="
    rest_base: str | None = None  # set only if REST is known to work on this instance
    tomcat_service: str = "tomcat8"
    tomcat_user: str = "tomcat8"
    backup_dir: str = "/dspace-copy/db_backup"

    ssh: SSHConfig = field(default_factory=lambda: SSHConfig(host="", user=""))
    db: DBConfig = field(default_factory=DBConfig)
    assetstore: AssetstoreConfig = field(default_factory=AssetstoreConfig)
    api: ApiKeysConfig = field(default_factory=ApiKeysConfig)


def _config_path() -> Path:
    override = os.environ.get("DSPACE_MCP_CONFIG")
    return Path(override) if override else DEFAULT_CONFIG_PATH


def load_profiles(path: Path | None = None) -> dict[str, Profile]:
    cfg_path = path or _config_path()
    if not cfg_path.is_file():
        raise ConfigError(
            f"No config file at {cfg_path}. Copy config.example.toml there and "
            f"fill in your instance(s), or set DSPACE_MCP_CONFIG to point elsewhere."
        )

    with open(cfg_path, "rb") as f:
        raw = tomllib.load(f)

    profiles: dict[str, Profile] = {}
    for name, block in raw.get("profile", {}).items():
        block = _resolve(block)

        ssh_block = block.get("ssh", {})
        db_block = block.get("db", {})
        assetstore_block = block.get("assetstore", {})
        api_block = block.get("api", {})

        profiles[name] = Profile(
            name=name,
            dspace_version=str(block.get("dspace_version", "6.3")),
            ui=block.get("ui", "jspui"),
            cli_path=block.get("cli_path", "/dspace/bin/dspace"),
            install_root=block.get("install_root", "/dspace"),
            admin_eperson=block.get("admin_eperson", ""),
            handle_prefix=str(block.get("handle_prefix", "")),
            java_opts_extra=block.get(
                "java_opts_extra", "-Djavax.accessibility.assistive_technologies="
            ),
            rest_base=block.get("rest_base"),
            tomcat_service=block.get("tomcat_service", "tomcat8"),
            tomcat_user=block.get("tomcat_user", "tomcat8"),
            backup_dir=block.get("backup_dir", "/dspace-copy/db_backup"),
            ssh=SSHConfig(
                host=ssh_block.get("host", ""),
                user=ssh_block.get("user", ""),
                port=int(ssh_block.get("port", 22)),
                identity_file=ssh_block.get("identity_file"),
                password=ssh_block.get("password"),
                sudo_password=ssh_block.get("sudo_password") or ssh_block.get("password"),
            ),
            db=DBConfig(
                host=db_block.get("host", "127.0.0.1"),
                port=int(db_block.get("port", 5432)),
                name=db_block.get("name", "dspace"),
                user=db_block.get("user", "dspace"),
                password=db_block.get("password"),
                ssh_peer_user=db_block.get("ssh_peer_user"),
            ),
            assetstore=AssetstoreConfig(
                root=assetstore_block.get("root", "/dspace/assetstore"),
                store_number=int(assetstore_block.get("store_number", 0)),
                directory_levels=int(assetstore_block.get("directory_levels", 3)),
                digits_per_level=int(assetstore_block.get("digits_per_level", 2)),
            ),
            api=ApiKeysConfig(
                unpaywall_email=api_block.get("unpaywall_email"),
                crossref_mailto=api_block.get("crossref_mailto"),
                openalex_mailto=api_block.get("openalex_mailto"),
                core_api_key=api_block.get("core_api_key"),
                semanticscholar_api_key=api_block.get("semanticscholar_api_key"),
                llm_provider=api_block.get("llm_provider", "deepseek"),
                llm_base_url=api_block.get("llm_base_url", "https://api.deepseek.com/v1"),
                llm_model=api_block.get("llm_model", "deepseek-chat"),
                llm_api_key=api_block.get("llm_api_key"),
            ),
        )

    if not profiles:
        raise ConfigError(f"Config file {cfg_path} has no [profile.*] sections")

    return profiles


def get_profile(name: str | None = None, path: Path | None = None) -> Profile:
    profiles = load_profiles(path)
    if name is None:
        name = os.environ.get("DSPACE_MCP_PROFILE")
    if name is None:
        if len(profiles) == 1:
            return next(iter(profiles.values()))
        raise ConfigError(
            f"Multiple profiles configured ({', '.join(profiles)}); "
            f"specify one via the 'profile' tool argument or DSPACE_MCP_PROFILE"
        )
    if name not in profiles:
        raise ConfigError(f"Unknown profile '{name}'; available: {', '.join(profiles)}")
    return profiles[name]
