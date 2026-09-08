# Contributing to dspace-mcp

Thanks for your interest in contributing. dspace-mcp is an MCP server that
gives AI agents a typed, guarded interface to DSpace institutional
repositories. Contributions of code, tests, documentation, and bug reports are
welcome.

## Ground rules

- **Never commit secrets.** The configuration model is built around this:
  `~/.config/dspace-mcp/config.toml` is git-ignored, and every secret in the
  example config is written as `"env:VAR_NAME"` rather than a literal value.
  Do not add a real SSH password, DB password, API key, or LLM key to any
  committed file. See `SECURITY.md`.
- **Preserve the guarded-write model.** State-changing DSpace tools default to
  `confirm=False` (dry run) and direct DB/assetstore mutations additionally
  require `allow_db_write=True` with an automatic backup. New mutating tools
  must follow the same pattern via `src/dspace_mcp/guards.py`.
- **No institution-specific hardcoding.** Nothing in tool logic may reference a
  specific host, handle prefix, affiliation string, or journal. Every such
  value is a config field or a tool argument.

## Development setup

Requires Python >= 3.10 and [`uv`](https://docs.astral.sh/uv/).

```bash
cd dspace-mcp
uv venv .venv
uv pip install -e ".[dev]" --python .venv/bin/python
```

The test suite is local-only: it exercises SAF building, OJS conversion,
deduplication/normalization helpers, the guarded-write gates, and the
version-capability layer without needing a reachable DSpace host.

```bash
.venv/bin/python -m pytest
```

## What the tests deliberately do not cover

SSH/`bin/dspace`/Postgres-backed tools (`dspace_ping`, `saf_import`,
`collection_create`, …) require a live DSpace instance and are not exercised in
CI. When changing those paths:

1. Keep the dry-run preview (`confirm=False`) the first code path a caller hits,
   and make it return the exact command(s) that would run with zero network I/O.
2. Run `dspace_ping` against a real profile before trusting any SSH-dependent
   change, and record what you verified in the pull request.

## Code style

- Follow the existing structure: one module per concern (`dspace/*.py` for
  DSpace operations, `scholar/*.py` for the scientometrics/OA pipeline,
  `versions/base.py` for version deltas).
- Tool docstrings in `server.py` are the agent-facing contract. Describe the
  exact behavior, every argument, and any caveat — the calling LLM reads them.
- Type hints are used throughout; keep them consistent.
- Keep the `# noqa: BLE001` pattern only where a broad `except` is a deliberate
  last-resort safety net that converts exceptions into tool-result dicts.

## Pull request checklist

- [ ] New/changed behavior has local tests where possible.
- [ ] No secrets or real host/credential values in committed files.
- [ ] Mutating tools respect `confirm` / `allow_db_write` + auto-backup.
- [ ] `server.py` tool docstrings are updated for any changed tool surface.
- [ ] `README.md` or `docs/INSTALLATION.md` updated as needed.
