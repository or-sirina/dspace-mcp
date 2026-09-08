# dspace-mcp

> **English** · **[Русский](README.ru.md)**

MCP server that gives an AI agent a stable, typed interface to operate
DSpace institutional repositories: building SAF import packages, running
`bin/dspace` CLI operations, batch metadata editing, author/COAR/ORCID
normalization, scientometric harvesting by affiliation, open-access PDF
download (Unpaywall included), LLM-assisted metadata enrichment, and
repository admin/diagnostics.

Grew out of ad-hoc tooling used to administer open.mgimo.ru (DSpace 6.3),
but is not tied to that institution: every host/credential/handle-prefix/
affiliation-string/journal-identity/type-map value is config- or
argument-driven (see "Adding another institution" below), and a
version-capability layer covers DSpace 5.x-8.x generally.

## Keywords

MCP · Model Context Protocol · DSpace · institutional repository · AI agent ·
LLM · Simple Archive Format (SAF) · metadata · scientometrics · open access ·
ORCID · COAR · Unpaywall · OpenAlex · Python

_Ключевые слова: MCP · Model Context Protocol · DSpace · институциональный
репозиторий · ИИ-агент · LLM · Simple Archive Format (SAF) · метаданные ·
наукометрия · открытый доступ · ORCID · COAR · Unpaywall · OpenAlex · Python_

## How it connects

Two execution paths:

- **Local, no server needed**: SAF package building, OJS XML conversion,
  scientometric API harvesting, OA PDF download, LLM enrichment, dedup/
  batching. These run entirely on the machine hosting this MCP server.
- **Remote, over SSH**: anything that touches the actual DSpace
  installation (`bin/dspace` CLI, Postgres, the assetstore) shells out to
  the configured host via `ssh`/`scp`. This is mandatory for classic
  DSpace (<7) instances, where the REST API is frequently broken/disabled
  and `import` must run as root. A REST-based path can be added later for
  instances where DSpace 7/8's REST API is known to work, but every tool
  here currently talks to the DSpace CLI/DB, not REST.

## Guarded writes

- Reads, local builds, and external API calls always run.
- State-changing DSpace operations (`saf_import`, `metadata_import`,
  `item_update_*`, `filter_media`, `cleanup`) default to **confirm=False**:
  they report the exact command(s) they would run and their predicted
  effect, and execute nothing. Pass `confirm=True` to actually run them.
- Direct database/assetstore mutations (`collection_create`,
  `set_collection_logo`, `set_intranet_access`, and the normalization tools
  when writing) additionally require `allow_db_write=True`, which triggers
  an automatic `db_backup()` before anything is touched.

This was verified: with `confirm=False`, `saf_import`/`metadata_import`/
`filter_media` return instantly with zero network I/O (tested against an
unroutable host) and report the exact remote commands they would run.

## Install

Requires Python >=3.10 and [`uv`](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/or-sirina/dspace-mcp.git
cd dspace-mcp
uv venv .venv
uv pip install -e . --python .venv/bin/python
```

Register with an MCP client (e.g. in `.claude.json`, matching how other
stdio servers are configured on this machine):

```json
{
  "mcpServers": {
    "dspace-mcp": {
      "type": "stdio",
      "command": "uv",
      "args": ["--directory", "/path/to/dspace-mcp", "run", "dspace-mcp"]
    }
  }
}
```

See [docs/INSTALLATION.md](docs/INSTALLATION.md) for ready-to-paste
registration snippets for Claude Desktop / Claude Code, Cursor, VS Code
(Copilot, Cline, Roo Code), Continue.dev, opencode, DeepSeek Harness, and any
generic stdio client.

## Configure

Copy `config.example.toml` to `~/.config/dspace-mcp/config.toml` (or point
`DSPACE_MCP_CONFIG` elsewhere) and fill in one `[profile.<name>]` block per
DSpace instance. **Never write secrets directly into the file** -- any
string value of the form `"env:VAR_NAME"` is resolved from the process
environment at load time:

```toml
[profile.mgimo]
dspace_version = "6.3"
admin_eperson = "admin@example.org"
handle_prefix = "123456789"

[profile.mgimo.ssh]
host = "203.0.113.10"
user = "deploy"
identity_file = "~/.ssh/id_dspace"   # prefer a key over a password

[profile.mgimo.db]
password = "env:DSPACE_DB_PASSWORD"

[profile.mgimo.api]
unpaywall_email = "you@example.org"
llm_api_key = "env:DEEPSEEK_API_KEY"
```

Most tools take an optional `profile` argument selecting which
`[profile.*]` block to use; if only one is configured it's picked
automatically, otherwise set `DSPACE_MCP_PROFILE` or pass `profile`
explicitly on each call.

**Security notes carried over from the tooling this replaces:**
- Prefer `ssh.identity_file` over `ssh.password`/`sudo_password`. Password
  auth still works for parity with older setups, but the password is
  briefly visible to `ps` on the remote host during `sudo -S`.
- Grant NOPASSWD sudo for the `dspace` CLI/chown commands this server needs
  if you want the password path avoided entirely -- `sudo_run()` always
  tries `sudo -n` first and only falls back to piping a password if one is
  configured.
- If you're migrating off scripts that had an API key or DB password
  hardcoded in source, rotate that credential -- it's already compromised
  once it's been committed in plaintext.

## Adding another institution

The server is multi-institution by design -- MGIMO is the deployment it
was first built against, not a hardcoded target. Nothing in the tool
logic references MGIMO, a specific handle prefix, or a specific host;
every institution-specific value (SSH host/creds, DB creds, handle prefix,
assetstore layout, COAR type map, IP ranges, LLM provider/key, affiliation
string) is a config field or a tool argument.

To onboard a second (or twentieth) institution:

1. Add a new `[profile.<short-name>]` block to `config.toml` -- see
   `[profile.example_university]` in `config.example.toml` for a complete
   template (DSpace 7, `dspace-angular`, an OpenAI-compatible LLM instead
   of DeepSeek, all different from the MGIMO example next to it).
2. Run `dspace_ping(profile="<short-name>")` first to confirm SSH/DB/Tomcat/
   Solr reachability before trying anything else.
3. Run `list_communities`/`list_collections`/`resolve_field_id` to confirm
   the canned SQL in `dspace/cli.py`/`dspace/admin.py` (written against the
   classic uuid-keyed schema) matches this instance; if it doesn't, use
   `raw_sql` to find the right table/column names for that instance instead
   of assuming MGIMO's.
4. For `harvest_by_affiliation`, pass that institution's affiliation string
   (e.g. `"University of Example"`) and a `filename_prefix` (e.g. `"uex"`)
   so its harvested PDF filenames don't collide with another institution's
   if you ever run both against a shared working directory.
5. For `saf_from_ojs_xml`, pass that institution's own `journal_name_en`/
   `journal_name_ru`/ISSNs -- nothing about the OJS converter assumes a
   particular journal or language pair.
6. For `assign_coar_types`, the default `type_map` reflects one
   institution's historical `dc.type` values (a mix of English and Russian
   strings, since that's what MGIMO's existing records used) -- pass your
   own `type_map` reflecting whatever `dc.type` values this instance
   actually has; `list_communities`/`raw_sql` can show you the current
   distribution first.

Nothing else needs to change -- the same 33 tools, the same guarded-write
behavior, and the same version-capability layer apply to every profile.

## Tool reference

**Introspection** -- `dspace_ping`, `list_communities`, `list_collections`,
`resolve_field_id`, `raw_sql`, `server_health`.

**SAF building (local)** -- `saf_build_from_csv`, `saf_generate_manifest`,
`saf_validate`, `saf_repair`, `saf_from_ojs_xml`.

**Metadata / import** -- `metadata_export`, `metadata_import`, `saf_import`,
`item_update_add_bitstreams`, `item_update_delete_bitstreams`,
`filter_media`, `index_discovery`, `cleanup`.

**Normalization** -- `normalize_authors`, `assign_coar_types`,
`harvest_orcid`, `apply_orcid_matches`.

**Admin** -- `db_backup`, `collection_create`, `set_collection_logo`,
`set_intranet_access`.

**Scientometrics / OA** -- `harvest_by_affiliation` (OpenAlex, CrossRef,
CORE, Semantic Scholar), `deduplicate`, `batch_missing`, `download_oa_pdfs`
(Unpaywall-backed), `extract_pdf_text`, `enrich_metadata_llm`.

Every tool has a docstring visible to the calling agent describing its
exact behavior, arguments, and any caveats -- read those first; this list
is just an index.

## Use cases

**Ingest a whole discipline by affiliation (scientometric pipeline).**
`harvest_by_affiliation` -> `deduplicate` (against existing metadata
exports) -> `download_oa_pdfs` (Unpaywall) -> `enrich_metadata_llm` ->
`saf_build_from_csv` -> `saf_import`. Turns an institution name into a batch
of validated, deduplicated, PDF-backed items ready to import.

**Import a journal issue from OJS/PKP.** `saf_from_ojs_xml` converts an
"Articles and Issues XML" export into a batch CSV plus extracted PDFs;
`saf_build_from_csv` packages it as SAF; `saf_import` loads it. Bilingual
metadata and transliterated filenames are handled automatically.

**Batch metadata cleanup.** `metadata_export` a collection to CSV, edit it in
a spreadsheet, then `metadata_import` it back. The import is a dry run by
default (`confirm=False`), so you can preview the exact command and effect
before executing.

**Author / type / ORCID normalization.** `normalize_authors` canonicalizes
ё/е, case, and punctuation variants; `assign_coar_types` remaps `dc.type` to
bilingual values + COAR URIs; `harvest_orcid` + `apply_orcid_matches` link
authors to ORCIDs via Crossref.

**Create and brand a collection.** `collection_create` (structure-builder
first, SQL fallback) -> `set_collection_logo` -> `set_intranet_access` for
IP-restricted collections.

**Diagnose an unhealthy repository.** `dspace_ping` / `server_health`
pinpoint SSH/CLI/Postgres/Tomcat/Solr failures; `db_backup`,
`index_discovery`, `filter_media`, and `cleanup` cover routine maintenance.

**Add or remove files on existing items.** `item_update_add_bitstreams` /
`item_update_delete_bitstreams` change bitstreams without touching metadata
(follow deletion with `cleanup`).

## Version coverage

`src/dspace_mcp/versions/base.py` encodes what differs across DSpace
5.x-8.x: whether `import` needs root, whether REST is generally usable,
how collections/logos are created, and known per-version CLI deltas
(all sourced from the LYRASIS DSpace documentation, cross-checked against
hands-on operation of a 6.3 instance). `collection_create` tries
`structure-builder` (present since 5.x/6.x) before falling back to direct
SQL; if a canned SQL query in `list_communities`/`list_collections`
doesn't match your instance's schema, use `raw_sql` to introspect it and
report/fix the mismatch.

## Known gaps / follow-ups

- No REST API backend yet -- everything goes through SSH + the `dspace`
  CLI + Postgres. A REST adapter behind the same tool signatures is a
  natural Phase 4 for DSpace 7/8 instances where REST is reachable and
  preferred over SSH.
- SQL in `dspace/cli.py` and `dspace/admin.py` targets the classic
  uuid-keyed schema (hands-on verified against DSpace 6.3); table/column
  names were not independently re-verified against a live 7/8 instance.
  `raw_sql` is the escape hatch when a canned query doesn't match.
- Keyed scientometric sources (Scopus, Web of Science, Dimensions) aren't
  wired up -- `scholar/harvest.py` is structured so adding one is a new
  `fetch_<source>` function plus a branch in `harvest_by_affiliation`.
- `collection_create`'s `structure-builder` XML currently passes a bare
  `<collection>` element; verify the resulting placement/handle before
  relying on it in bulk, since the documented format nests collections
  under a `<community>` block for whole-subtree declarations.

## Verification performed

- `saf_build_from_csv` on a sample CSV -> correct `dublin_core.xml`,
  `metadata_local.xml`, `contents`, `collections`, copied bitstreams;
  `saf_validate` confirms no issues; `saf_repair` fixes an intentionally
  corrupted `contents` file.
- `saf_from_ojs_xml` on a synthetic OJS issue XML -> correct bilingual
  title/author/abstract extraction, HTML stripping, citation building,
  transliterated filename, and base64 PDF extraction verified byte-for-byte.
- `saf_import`/`metadata_import`/`filter_media` with `confirm=False` return
  in <2ms against an unroutable host (192.0.2.1, RFC 5737 TEST-NET) with the
  correct command preview -- proves zero network I/O happens before confirm.
- `dedup`/`batch_missing` correctly drop within-batch and existing-corpus
  duplicates and chunk output CSVs.
- `harvest_by_affiliation` (OpenAlex) executed as a **live** call through
  the actual MCP `call_tool` protocol path (not just the underlying Python
  function) and returned real MGIMO-affiliated papers with correctly
  mapped fields, including a genuine open-access URL.
- The installed console entrypoint (`dspace-mcp`) starts cleanly under the
  stdio transport with no traceback.
- Not verified in this pass (no reachable DSpace host from the build
  environment): `dspace_ping`/`saf_import(confirm=True)`/`collection_create`
  against a live instance. Run `dspace_ping` first against your real
  profile before trusting the SSH-dependent tools.
- An automated `pytest` suite (`tests/`) covers the local-only paths — SAF
  building/validation/repair, OJS conversion, dedup/normalization helpers,
  the guarded-write gates, and version resolution (Python 3.10-3.13).

## Development

```bash
uv pip install -e ".[dev]" --python .venv/bin/python
.venv/bin/python -m pytest
```

The test suite is local-only and needs no DSpace host. SSH/CLI/Postgres tools
are exercised manually against a live profile (`dspace_ping` first). See
[CONTRIBUTING.md](CONTRIBUTING.md) and [SECURITY.md](SECURITY.md).

## Acknowledgments

This project was developed with the assistance of AI coding agents:
**Claude Code** (Claude Opus, Claude Sonnet) and **DeepSeek Harness**
(DeepSeek V4 Pro, DeepSeek V4 Flash).

## License

[MIT](LICENSE) © 2026 V. V. Vinogradov.
