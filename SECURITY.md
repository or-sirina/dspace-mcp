# Security policy

dspace-mcp is an agent-facing control surface for production DSpace
repositories, so credential hygiene and write-safety are first-class design
constraints rather than afterthoughts.

## How credentials are handled

- The config file `~/.config/dspace-mcp/config.toml` (and
  `config.example.toml`) never stores secrets in plaintext. Any string value of
  the form `"env:VAR_NAME"` is resolved from the process environment at load
  time (`src/dspace_mcp/config.py`).
- Passwords are never passed as SSH command-line arguments. SSH password auth
  (when used) is piped to `sudo -S` via stdin; `sudo -n` is always tried first.
  See the caveats in `README.md` and `src/dspace_mcp/ssh.py`.
- Prefer `ssh.identity_file` (a private key) over `ssh.password`. If a password
  was ever committed in plaintext in a previous script, treat it as compromised
  and rotate it.

## Guarded writes

- Reads, local builds, and external API calls always run.
- State-changing DSpace operations default to `confirm=False`: they report the
  exact command(s) and predicted effect without executing anything.
- Direct database/assetstore mutations additionally require
  `allow_db_write=True` and trigger an automatic `db_backup()` first
  (`src/dspace_mcp/guards.py`).

These gates are the primary defense against an agent (or a prompt-injection
attempt) mutating the repository. Do not weaken them.

## Reporting a vulnerability

Please do **not** open a public issue for a suspected vulnerability. Instead,
report it privately to the repository maintainer (see the contact in the
`pyproject.toml` authors field). Include:

- The affected version and the component (tool name, module).
- A minimal reproduction, ideally against a non-production DSpace instance.
- Whether the issue is in credential handling, the guarded-write gates, SQL
  construction, or remote-command handling.

The maintainer will acknowledge the report and aim to respond within a
reasonable timeframe. Once a fix is prepared and released, the issue may be
disclosed publicly with credit.
