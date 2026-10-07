# Changelog

## 0.2.0

### Fixed
- **Server failed to start on fresh installs**: `mcp>=1.2.0` resolved to mcp
  2.x, where `FastMCP` was renamed. Pinned `mcp>=1.2.0,<2`.
- **`saf_import` extracted to the wrong directory**: the archive was unzipped
  into `/tmp` but imported from `/tmp/dspace_mcp_saf_<name>`. It now unzips
  into a fresh dedicated directory and imports from the matching path; the
  local zip is always rebuilt (no stale-zip reuse); the dry-run preview shows
  the exact commands that run.
- **Write guard bypass in `raw_sql`**: read-only queries now run inside
  `BEGIN READ ONLY`, so Postgres rejects writes such as
  `SELECT 1; DROP TABLE item`; comments and transaction-control statements are
  rejected in read-only mode.
- **SQL injection** in `resolve_field_id` / `list_collections`: identifiers and
  handles are validated before use.
- **Secrets leaking to the agent**: SSH/sudo/DB passwords are redacted from
  command text, stdout/stderr and error messages; the DB and sudo passwords
  are passed over stdin instead of the command line.
- **Backups**: a failed `db_backup` now blocks the DB write; dry runs
  (`confirm=False`) no longer take a backup.
- **SSH password auth** now works through `sshpass -e` (or fails with a clear
  message suggesting `identity_file`) instead of hanging.
- `download_oa_pdfs`: filenames are reduced to a basename (no path traversal)
  and only `http`/`https` URLs are fetched.
- `requires-python` raised to `>=3.11` (the code uses `tomllib`).

### Added
- `demo/opencode/`: ready-to-run opencode demo (config, demo profile, sample
  data, Russian scenario `DEMO.md`, `run_demo.sh`, `smoke_test.py`).
- Tests: server start-up / tool registry (33 tools), `saf_import` command
  paths, `raw_sql` read-only enforcement, injection rejection, secret
  redaction, backup gating, download sanitizing — 69 tests in total.

### Known limitations
- Read-only `raw_sql` still allows side-effecting reads that the DB role is
  privileged for (e.g. `COPY ... TO PROGRAM` for a superuser); use a
  non-superuser DB role.
- SSH-dependent tools are covered by mocked tests only; run `dspace_ping`
  against your real profile first.

## 0.1.0

- Initial release.
