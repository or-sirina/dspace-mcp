"""dspace-mcp: MCP server exposing DSpace repository operations as tools.

Run with `dspace-mcp` (stdio transport) after configuring
~/.config/dspace-mcp/config.toml (see config.example.toml).
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from .config import ConfigError, Profile, get_profile
from .ssh import RemoteError
from .dspace import admin as admin_mod
from .dspace import cli as cli_mod
from .dspace import normalize as normalize_mod
from .dspace import ojs as ojs_mod
from .dspace import saf as saf_mod
from .scholar import dedup as dedup_mod
from .scholar import download as download_mod
from .scholar import enrich as enrich_mod
from .scholar import harvest as harvest_mod

mcp = FastMCP(
    name="dspace-mcp",
    instructions=(
        "Tools for operating DSpace institutional repositories: building SAF "
        "import packages, running the dspace CLI over SSH, batch metadata "
        "editing, author/COAR/ORCID normalization, and repository admin/"
        "diagnostics. State-changing tools default to a dry run (confirm=False) "
        "and report the exact command(s) they would execute; pass confirm=True "
        "to actually run them. Direct database/assetstore mutations additionally "
        "require allow_db_write=True and take a fresh backup automatically first."
    ),
)


def _profile(profile: str | None) -> Profile | dict:
    try:
        return get_profile(profile)
    except ConfigError as exc:
        return {"status": "error", "reason": str(exc)}


def _guard(fn, *args, **kwargs) -> dict:
    """Run fn(*args, **kwargs), translating SSH/config errors into a dict
    instead of letting a stack trace reach the agent."""
    try:
        return fn(*args, **kwargs)
    except RemoteError as exc:
        return {"status": "error", "stage": "remote_exec", "reason": str(exc)}
    except Exception as exc:  # noqa: BLE001 - last-resort safety net for tool calls
        return {"status": "error", "reason": f"{type(exc).__name__}: {exc}"}


# =========================================================================
# Introspection / connectivity
# =========================================================================

@mcp.tool()
def dspace_ping(profile: str | None = None) -> dict:
    """Check connectivity and health of a configured DSpace instance: SSH
    reachability, `dspace version`, Postgres, Tomcat, Solr. Always safe to
    call -- read-only."""
    p = _profile(profile)
    if isinstance(p, dict):
        return p
    return _guard(cli_mod.ping, p)


@mcp.tool()
def list_communities(profile: str | None = None, top_level_only: bool = True) -> dict:
    """List communities (handle + name). Read-only."""
    p = _profile(profile)
    if isinstance(p, dict):
        return p
    return _guard(cli_mod.list_communities, p, top_level_only)


@mcp.tool()
def list_collections(profile: str | None = None, community_handle: str | None = None) -> dict:
    """List collections (handle + name), optionally filtered to one community's
    children. Read-only."""
    p = _profile(profile)
    if isinstance(p, dict):
        return p
    return _guard(cli_mod.list_collections, p, community_handle)


@mcp.tool()
def resolve_field_id(profile: str | None, schema: str, element: str, qualifier: str | None = None) -> dict:
    """Look up a metadata_field_registry id for schema.element[.qualifier]
    (e.g. dc, title -> field id). Field ids are per-installation -- never
    assume a number like 70 for dc.title holds on a different instance;
    always resolve it here first. Read-only."""
    p = _profile(profile)
    if isinstance(p, dict):
        return p
    return _guard(cli_mod.resolve_field_id, p, schema, element, qualifier)


@mcp.tool()
def raw_sql(profile: str | None, sql: str, allow_db_write: bool = False) -> dict:
    """Escape hatch: run arbitrary SQL against the DSpace database. SELECT/
    WITH statements always run. Anything else (INSERT/UPDATE/DELETE/DDL)
    requires allow_db_write=True and is NOT auto-backed-up -- call db_backup
    yourself first. Use this when a canned query (list_communities, etc.)
    doesn't match this instance's schema."""
    p = _profile(profile)
    if isinstance(p, dict):
        return p
    return _guard(cli_mod.raw_sql, p, sql, allow_db_write)


@mcp.tool()
def server_health(profile: str | None = None) -> dict:
    """Read-only host diagnostics: root filesystem writability + ext4 error
    count, Postgres cluster status, Tomcat status, Solr ping, archived item
    count. Use this first when a repository seems down."""
    p = _profile(profile)
    if isinstance(p, dict):
        return p
    return _guard(admin_mod.server_health, p)


# =========================================================================
# SAF (Simple Archive Format) package building -- local, no SSH involved
# =========================================================================

@mcp.tool()
def saf_build_from_csv(
    csv_path: str,
    output_name: str = "SimpleArchiveFormat",
    use_symlink: bool = False,
    export_zip: bool = False,
) -> dict:
    """Build a SAF import package from a DSpace-batch-style CSV. Content
    files referenced by `filename`/`filename__bundle:X__permissions:Y`
    columns must sit next to the CSV. Columns are `schema.element[.qualifier]
    [[lang]]` (e.g. dc.title[en], local.type.coar); `||` separates multiple
    values in one cell; `collection` sets the owning (+ mapped) handles.
    Local operation, does not touch any DSpace server."""
    try:
        result = saf_mod.build_from_csv(csv_path, output_name, use_symlink, export_zip)
    except FileNotFoundError as exc:
        return {"status": "error", "reason": f"CSV not found: {exc}"}
    return {
        "status": "ok",
        "saf_dir": result.saf_dir,
        "item_count": result.item_count,
        "zip_path": result.zip_path,
        "warnings": result.warnings,
        "unused_files": result.unused_files,
    }


@mcp.tool()
def saf_generate_manifest(csv_path: str) -> dict:
    """Write a template CSV (filename, dc.title, dc.date.issued, collection
    columns) listing every content file in the CSV's directory -- a starting
    point for hand-filling metadata before saf_build_from_csv."""
    return saf_mod.generate_manifest(csv_path)


@mcp.tool()
def saf_validate(saf_dir: str) -> dict:
    """Check a SAF directory for missing dublin_core.xml, broken symlinks,
    or `contents` lines pointing at files that don't exist. Read-only."""
    issues = saf_mod.validate_saf_dir(saf_dir)
    return {"status": "ok", "issue_count": len(issues), "issues": [{"item": i.item_dir, "problem": i.problem} for i in issues]}


@mcp.tool()
def saf_repair(saf_dir: str, drop_missing_from_contents: bool = True, resolve_symlinks: bool = True) -> dict:
    """Fix common SAF problems in place: dereference symlinks into real file
    copies (symlinks often don't survive zip/scp transport), drop `contents`
    lines for files that don't exist, and sanitize XML (strip characters
    illegal in XML 1.0, escape bare '&') -- useful for metadata scraped from
    external sources like OpenAlex abstracts or HTML."""
    return saf_mod.repair_saf_dir(saf_dir, drop_missing_from_contents, resolve_symlinks)


@mcp.tool()
def saf_from_ojs_xml(
    xml_path: str,
    output_dir: str,
    journal_name_en: str,
    journal_name_ru: str,
    issn_online: str = "",
    issn_print: str = "",
    collection: str = "",
    csv_name: str = "metadata.csv",
) -> dict:
    """Convert an OJS/PKP "Articles and Issues XML" (elpub) export into a
    DSpace batch CSV plus per-article PDFs extracted from embedded base64
    galleys. Feed the resulting CSV into saf_build_from_csv. Local operation."""
    journal = ojs_mod.JournalInfo(name_en=journal_name_en, name_ru=journal_name_ru, issn_online=issn_online, issn_print=issn_print)
    try:
        result = ojs_mod.parse_issue(xml_path, output_dir, csv_name, journal, collection)
    except Exception as exc:  # noqa: BLE001
        return {"status": "error", "reason": f"{type(exc).__name__}: {exc}"}
    return {"status": "ok", **result}


# =========================================================================
# Metadata round-trip
# =========================================================================

@mcp.tool()
def metadata_export(profile: str | None, identifier: str, local_dir: str) -> dict:
    """Export an item/collection/community's metadata to CSV (dspace
    metadata-export), copied back to local_dir. Read-only on the server."""
    p = _profile(profile)
    if isinstance(p, dict):
        return p
    return _guard(cli_mod.metadata_export, p, identifier, local_dir)


@mcp.tool()
def metadata_import(
    profile: str | None,
    local_csv_path: str,
    confirm: bool = False,
    silent: bool = True,
    eperson: str | None = None,
    workflow: bool = False,
    notify: bool = False,
    apply_template: bool = False,
) -> dict:
    """Apply a metadata CSV (dspace metadata-import). Rows with '+' in the
    `id` column create new items; `action` column supports expunge/withdraw/
    reinstate; `||` separates multi-values. confirm=False (default) previews
    the exact command without executing it."""
    p = _profile(profile)
    if isinstance(p, dict):
        return p
    return _guard(cli_mod.metadata_import, p, local_csv_path, confirm, silent, eperson, workflow, notify, apply_template)


@mcp.tool()
def saf_import(profile: str | None, local_saf_dir: str, collection_handle: str, confirm: bool = False, eperson: str | None = None) -> dict:
    """Zip + upload a local SAF directory and run `dspace import -a`.
    Validates the SAF first (see saf_validate). On instances where import
    must run as root (classic DSpace <7 bitstore), automatically chowns the
    assetstore back to the tomcat user afterward. confirm=False previews the
    exact commands and item count without executing anything."""
    p = _profile(profile)
    if isinstance(p, dict):
        return p
    return _guard(cli_mod.saf_import, p, local_saf_dir, collection_handle, confirm, eperson)


@mcp.tool()
def item_update_add_bitstreams(profile: str | None, identifier: str, local_source_dir: str, confirm: bool = False) -> dict:
    """Add bitstream(s) to an existing item without touching its metadata
    (dspace itemupdate -a). local_source_dir must contain the file(s) plus a
    `contents` listing (same format as a SAF item directory)."""
    p = _profile(profile)
    if isinstance(p, dict):
        return p
    return _guard(cli_mod.item_update_add_bitstreams, p, identifier, local_source_dir, confirm)


@mcp.tool()
def item_update_delete_bitstreams(profile: str | None, identifier: str, filename_regex: str, confirm: bool = False, dry_run_flag: bool = True) -> dict:
    """Delete bitstream(s) from an item by filename regex (dspace itemupdate
    -D BitstreamFilterByFilename). dry_run_flag=True (default) passes -t to
    itemupdate itself, so it only reports matches without deleting -- set
    both dry_run_flag=False and confirm=True to actually delete. Follow up
    with cleanup() to purge the files from disk."""
    p = _profile(profile)
    if isinstance(p, dict):
        return p
    return _guard(cli_mod.item_update_delete_bitstreams, p, identifier, filename_regex, confirm, dry_run_flag)


@mcp.tool()
def filter_media(profile: str | None, identifier: str, plugin: str, confirm: bool = False, max_items: int | None = None, force: bool = False) -> dict:
    """Run a media filter plugin (e.g. "PDF Text Extractor", "PDFBox JPEG
    Thumbnail" -- verify exact names for this instance) over an item/
    collection/community. max_items is a MAXIMUM for this call, not a batch
    size -- omit it or call repeatedly to process an entire large collection.
    force=True re-generates derivatives that already exist."""
    p = _profile(profile)
    if isinstance(p, dict):
        return p
    return _guard(cli_mod.filter_media, p, identifier, plugin, confirm, max_items, force)


@mcp.tool()
def index_discovery(profile: str | None = None, identifier: str | None = None) -> dict:
    """Reindex Solr discovery for a handle, or the whole repository if
    identifier is omitted. Run after imports/edits/SQL-created collections."""
    p = _profile(profile)
    if isinstance(p, dict):
        return p
    return _guard(cli_mod.index_discovery, p, identifier)


@mcp.tool()
def cleanup(profile: str | None, confirm: bool = False) -> dict:
    """dspace cleanup: permanently removes bitstream files already flagged
    deleted in the database. Run after itemupdate -D or metadata edits that
    removed bitstreams."""
    p = _profile(profile)
    if isinstance(p, dict):
        return p
    return _guard(cli_mod.cleanup, p, confirm)


# =========================================================================
# Normalization: authors, COAR types, ORCID
# =========================================================================

@mcp.tool()
def normalize_authors(profile: str | None, confirm: bool = False, allow_db_write: bool = False) -> dict:
    """Find mechanical author-name spelling variants (ё/е, case, punctuation,
    spacing) grouped by (surname, initials) and propose a canonical spelling
    per group. Preview-only unless allow_db_write=True (auto-backs-up first)
    and confirm=True."""
    p = _profile(profile)
    if isinstance(p, dict):
        return p
    return _guard(normalize_mod.normalize_authors, p, confirm, allow_db_write)


@mcp.tool()
def assign_coar_types(profile: str | None, confirm: bool = False, allow_db_write: bool = False, type_map: dict[str, list[str]] | None = None) -> dict:
    """Remap dc.type values to a bilingual (en/ru) pair plus a
    local.type.coar COAR resource-type URI, registering that field if
    missing. type_map overrides the default {current_value: [en, ru, coar_uri]}
    mapping. Unmapped values are left untouched and reported. Preview-only
    unless allow_db_write=True (auto-backs-up first) and confirm=True."""
    p = _profile(profile)
    if isinstance(p, dict):
        return p
    tm = {k: tuple(v) for k, v in type_map.items()} if type_map else None
    return _guard(normalize_mod.assign_coar_types, p, confirm, allow_db_write, tm)


@mcp.tool()
def harvest_orcid(profile: str | None, mailto: str, limit: int = 0) -> dict:
    """Match ORCIDs to stored author name-values via Crossref, by DOI
    (read-only network calls, no DB write). Returns match triples to hand to
    apply_orcid_matches(). `mailto` is sent to Crossref's polite pool."""
    p = _profile(profile)
    if isinstance(p, dict):
        return p
    return _guard(normalize_mod.harvest_orcid, p, mailto, limit)


@mcp.tool()
def apply_orcid_matches(profile: str | None, matches: list[dict], confirm: bool = False, allow_db_write: bool = False) -> dict:
    """Apply harvest_orcid() matches: sets metadatavalue.authority=<orcid>,
    confidence=600 on unambiguous (item, author-text) pairs, keeping a
    reversible backup table (orcid_apply_backup). Preview-only unless
    allow_db_write=True (auto-backs-up the whole DB first) and confirm=True."""
    p = _profile(profile)
    if isinstance(p, dict):
        return p
    return _guard(normalize_mod.apply_orcid_matches, p, matches, confirm, allow_db_write)


# =========================================================================
# Admin: backups, collection/logo creation, intranet access
# =========================================================================

@mcp.tool()
def db_backup(profile: str | None = None, keep_days: int = 14) -> dict:
    """pg_dump the DSpace database (metadata only, not the assetstore) to
    the configured backup_dir on the remote host, then delete dumps older
    than keep_days. Safe to call any time."""
    p = _profile(profile)
    if isinstance(p, dict):
        return p
    return _guard(admin_mod.db_backup, p, keep_days)


@mcp.tool()
def collection_create(profile: str | None, parent_community_handle: str, name: str, confirm: bool = False, allow_db_write: bool = False) -> dict:
    """Create a collection under a community: tries `dspace structure-builder`
    first, falls back to direct SQL (mints a handle, mirrors a sibling
    collection's access policies) if that fails. Preview-only unless
    allow_db_write=True (auto-backs-up first) and confirm=True. May require a
    Tomcat restart + index_discovery() on classic (pre-7) instances before
    the new collection is visible."""
    p = _profile(profile)
    if isinstance(p, dict):
        return p
    return _guard(admin_mod.collection_create, p, parent_community_handle, name, confirm, allow_db_write)


@mcp.tool()
def set_collection_logo(profile: str | None, collection_handle: str, local_image_path: str, confirm: bool = False, allow_db_write: bool = False) -> dict:
    """Set a collection's logo by injecting a bitstream directly into the
    assetstore + database (no CLI/REST path exists for this on classic
    DSpace <7). Preview-only unless allow_db_write=True (auto-backs-up
    first) and confirm=True. Verify success via DB parity (logo_bitstream_id),
    not by curling the frontend -- some UIs wrap /retrieve/<uuid> in an HTML
    shell that returns 200 either way."""
    p = _profile(profile)
    if isinstance(p, dict):
        return p
    return _guard(admin_mod.set_collection_logo, p, collection_handle, local_image_path, confirm, allow_db_write)


@mcp.tool()
def set_intranet_access(profile: str | None, cidr: str, group_name: str = "Intranet", confirm: bool = False, allow_db_write: bool = False) -> dict:
    """Report the config changes needed to IP-restrict access to a CIDR
    range (IPAuthentication plugin + authentication-ip.cfg) and create the
    corresponding EPersonGroup. Only the group-creation SQL is executed by
    this tool (transactional, preview-only unless allow_db_write=True and
    confirm=True); the config file edits and Tomcat restart remain manual
    steps reported in the response, and per-bitstream
    `permissions:-r '<group_name>'` SAF contents lines are the caller's
    responsibility for whichever items should be restricted."""
    p = _profile(profile)
    if isinstance(p, dict):
        return p
    return _guard(admin_mod.set_intranet_access, p, cidr, group_name, confirm, allow_db_write)


# =========================================================================
# Scientometrics: affiliation harvest, dedup, OA download, LLM enrichment
# =========================================================================

@mcp.tool()
def harvest_by_affiliation(
    affiliation: str,
    sources: list[str],
    collection: str = "",
    mailto: str = "",
    core_api_key: str = "",
    s2_api_key: str = "",
    max_results_per_source: int = 1000,
    filename_prefix: str = "ext",
) -> dict:
    """Fetch publications by affiliation string (any institution -- pass its
    name/variant as it appears in author affiliations, e.g. "University of
    Example" or "MGIMO") from any subset of {openalex, crossref, core,
    semanticscholar}, normalized into the common row schema used by
    deduplicate/download_oa_pdfs/enrich_metadata_llm/saf_build_from_csv.
    OpenAlex and CrossRef are live-API, affiliation-filtered and fairly
    precise; CORE searches by query string; Semantic Scholar has no
    affiliation filter and is lower-precision -- dedup its results against
    the others before trusting them. `mailto` is used for OpenAlex/CrossRef
    polite-pool access (recommended, not required). `filename_prefix`
    namespaces generated PDF filenames -- set it to a short institution code
    when running this for more than one institution against a shared
    working directory, so filenames don't collide. Local/network operation
    -- does not touch any DSpace server."""
    return _guard(
        harvest_mod.harvest_by_affiliation,
        affiliation, sources, collection, mailto, core_api_key, s2_api_key, max_results_per_source, filename_prefix,
    )


@mcp.tool()
def deduplicate(rows: list[dict], existing_csv_paths: list[str]) -> dict:
    """Filter harvested rows (from harvest_by_affiliation) against one or
    more existing DSpace metadata-export/corpus CSVs, by DOI then by
    normalized title. Returns only the rows NOT already in the repository."""
    return _guard(dedup_mod.deduplicate, rows, existing_csv_paths)


@mcp.tool()
def batch_missing(rows: list[dict], output_dir: str, batch_size: int = 1000, prefix: str = "batch") -> dict:
    """Write deduplicated rows to output_dir/<prefix>_N.csv files of at most
    batch_size rows each (DSpace docs recommend not exceeding ~1000 rows per
    metadata-import CSV to avoid OOM/verification problems)."""
    return _guard(dedup_mod.batch_missing, rows, output_dir, batch_size, prefix)


@mcp.tool()
def download_oa_pdfs(
    rows: list[dict],
    output_dir: str,
    unpaywall_email: str,
    resume: bool = True,
    delay: float = 0.5,
    limit: int = 0,
) -> dict:
    """Download open-access PDFs for harvested rows: tries each row's own OA
    URL first, then Unpaywall (api.unpaywall.org, by DOI), then a DOI
    content-negotiated redirect. SSRN links are skipped outright (Cloudflare
    blocks server-side downloads there) and reported as such, not as
    failures. unpaywall_email is required by Unpaywall's API terms."""
    return _guard(download_mod.download_oa_pdfs, rows, output_dir, unpaywall_email, resume, delay, limit)


@mcp.tool()
def extract_pdf_text(pdf_path: str) -> dict:
    """Extract grounding text (first 2 + last 2 pages) from a PDF for
    metadata verification/extraction -- the same context enrich_metadata_llm
    uses internally, exposed standalone for building metadata from files
    without necessarily calling the LLM."""
    text = enrich_mod.extract_pdf_text(pdf_path)
    return {"status": "ok" if text else "empty", "text": text, "char_count": len(text)}


@mcp.tool()
def enrich_metadata_llm(
    profile: str | None,
    rows: list[dict],
    pdf_dir: str,
    concurrency: int = 10,
    rate_per_sec: float = 10.0,
    checkpoint_path: str | None = None,
) -> dict:
    """LLM-based metadata cleanup: fills missing ru/en title+abstract
    translations, extracts 5-10 subject keywords, builds bibliographic
    citations -- using each row's PDF (pdf_dir/<filename>) as grounding
    context when present. Provider/model/key come from the profile's [api]
    config (llm_provider/llm_base_url/llm_model/llm_api_key). Resumable via
    checkpoint_path if the run is interrupted partway through a large batch."""
    p = _profile(profile)
    if isinstance(p, dict):
        return p
    return _guard(
        enrich_mod.enrich_metadata_llm,
        rows, pdf_dir, p.api.llm_base_url, p.api.llm_api_key or "", p.api.llm_model,
        concurrency, rate_per_sec, checkpoint_path,
    )


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
