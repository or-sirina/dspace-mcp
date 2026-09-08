"""Wrappers around the `bin/dspace` CLI and the classic DSpace Postgres
schema, run over SSH against a configured Profile.

Every state-changing function takes `confirm: bool` and returns a
guards.preview() dict (nothing executed) when confirm=False. Functions that
touch the assetstore as root additionally chown it back to the tomcat user
afterward, per versions.VersionProfile.needs_chown_after_root_write.

SQL here targets the classic DSpace 5/6 schema (uuid-keyed community/
collection/item/handle/metadatavalue tables), which is what has been
hands-on-verified against open.mgimo.ru (6.3). DSpace 7/8 keep the same
core tables for the object model, but if a query here doesn't match a given
instance, fall back to the raw_sql() escape hatch rather than trusting it
blindly -- table/column names were not independently re-verified against a
live 7/8 instance for this server.
"""

from __future__ import annotations

import os
import shlex
import tempfile

from .. import guards, ssh
from ..config import Profile
from ..versions import resolve as resolve_version
from . import saf as saf_mod

# org.dspace.core.Constants resource type ids (stable across 5.x-8.x)
RESOURCE_TYPE_COMMUNITY = 4
RESOURCE_TYPE_COLLECTION = 3
RESOURCE_TYPE_ITEM = 2


def dspace_version_string(profile: Profile) -> str:
    result = ssh.dspace_cli(profile, "version", timeout=30)
    return result.stdout.strip() or result.stderr.strip()


def ping(profile: Profile) -> dict:
    """Reachability/health check: SSH, dspace CLI, Postgres, Tomcat/Solr."""
    out: dict = {"profile": profile.name, "configured_version": profile.dspace_version}

    ssh_result = ssh.run(profile, "echo ok", timeout=15)
    out["ssh_ok"] = ssh_result.ok
    if not ssh_result.ok:
        out["ssh_error"] = ssh_result.stderr
        return out

    ver = ssh.dspace_cli(profile, "version", timeout=30)
    out["dspace_cli_ok"] = ver.ok
    out["dspace_cli_output"] = (ver.stdout or ver.stderr).strip()

    try:
        rows = ssh.psql_query(profile, "SELECT count(*) FROM item WHERE in_archive = true;", timeout=20)
        out["db_ok"] = True
        out["archived_item_count"] = int(rows[0][0]) if rows and rows[0] else None
    except Exception as exc:  # noqa: BLE001 - surface any DB error to the caller
        out["db_ok"] = False
        out["db_error"] = str(exc)

    tomcat = ssh.run(profile, f"systemctl is-active {shlex.quote(profile.tomcat_service)}", timeout=15)
    out["tomcat_status"] = tomcat.stdout.strip() or tomcat.stderr.strip()

    solr = ssh.run(profile, "curl -s --max-time 8 -o /dev/null -w '%{http_code}' http://localhost:8080/solr/search/admin/ping", timeout=15)
    out["solr_ping_http_code"] = solr.stdout.strip()

    return out


def raw_sql(profile: Profile, sql: str, allow_db_write: bool = False) -> dict:
    """Escape hatch for schema queries/edits this module doesn't cover.

    Read-only (SELECT) statements run regardless of allow_db_write. Anything
    else requires allow_db_write=True and is NOT auto-backed-up here -- call
    db_backup() yourself first, or use a dedicated tool that does.
    """
    is_select = sql.strip().lower().startswith(("select", "with"))
    if not is_select and not allow_db_write:
        return {
            "status": "blocked",
            "reason": "Non-SELECT statement requires allow_db_write=True. "
                      "Consider running db_backup() first.",
        }
    if is_select:
        rows = ssh.psql_query(profile, sql)
        return {"status": "ok", "rows": rows}
    result = ssh.psql_execute(profile, sql)
    return {"status": "ok" if result.ok else "error", "stdout": result.stdout, "stderr": result.stderr}


def resolve_field_id(profile: Profile, schema: str, element: str, qualifier: str | None = None) -> dict:
    """Look up a metadata_field_registry id from schema.element[.qualifier].

    Replaces hardcoding numbers like dc.title=70 -- those are per-installation
    and must never be assumed to hold on a different DSpace instance.
    """
    qual_clause = "mfr.qualifier IS NULL" if qualifier is None else f"mfr.qualifier = '{qualifier}'"
    sql = (
        "SELECT mfr.metadata_field_id FROM metadatafieldregistry mfr "
        "JOIN metadataschemaregistry msr ON mfr.metadata_schema_id = msr.metadata_schema_id "
        f"WHERE msr.short_id = '{schema}' AND mfr.element = '{element}' AND {qual_clause};"
    )
    rows = ssh.psql_query(profile, sql)
    if not rows or not rows[0]:
        return {"status": "not_found", "schema": schema, "element": element, "qualifier": qualifier}
    return {"status": "ok", "field_id": int(rows[0][0])}


def list_communities(profile: Profile, top_level_only: bool = True) -> dict:
    title_field = resolve_field_id(profile, "dc", "title")
    if title_field.get("status") != "ok":
        return {"status": "error", "reason": "could not resolve dc.title field id", "detail": title_field}
    field_id = title_field["field_id"]

    child_filter = (
        f"AND h.resource_id NOT IN (SELECT child_comm_id FROM community2community)"
        if top_level_only else ""
    )
    sql = (
        "SELECT h.handle, mv.text_value FROM handle h "
        f"JOIN metadatavalue mv ON mv.dspace_object_id = h.resource_id "
        f"WHERE h.resource_type_id = {RESOURCE_TYPE_COMMUNITY} "
        f"AND mv.metadata_field_id = {field_id} {child_filter} "
        "ORDER BY mv.text_value;"
    )
    try:
        rows = ssh.psql_query(profile, sql)
    except Exception as exc:  # noqa: BLE001
        return {
            "status": "error",
            "reason": (
                "Query failed -- the metadatavalue.dspace_object_id column name "
                "or community2community table may differ on this instance/version. "
                "Use raw_sql() to introspect the live schema."
            ),
            "detail": str(exc),
        }
    return {"status": "ok", "communities": [{"handle": r[0], "name": r[1]} for r in rows]}


def list_collections(profile: Profile, community_handle: str | None = None) -> dict:
    title_field = resolve_field_id(profile, "dc", "title")
    if title_field.get("status") != "ok":
        return {"status": "error", "reason": "could not resolve dc.title field id", "detail": title_field}
    field_id = title_field["field_id"]

    community_filter = ""
    if community_handle:
        community_filter = (
            "AND h.resource_id IN ("
            "  SELECT c2c.collection_id FROM community2collection c2c "
            "  JOIN handle ch ON ch.resource_id = c2c.community_id "
            f"    AND ch.resource_type_id = {RESOURCE_TYPE_COMMUNITY}"
            f"  WHERE ch.handle = '{community_handle}'"
            ")"
        )
    sql = (
        "SELECT h.handle, mv.text_value FROM handle h "
        "JOIN metadatavalue mv ON mv.dspace_object_id = h.resource_id "
        f"WHERE h.resource_type_id = {RESOURCE_TYPE_COLLECTION} "
        f"AND mv.metadata_field_id = {field_id} {community_filter} "
        "ORDER BY mv.text_value;"
    )
    try:
        rows = ssh.psql_query(profile, sql)
    except Exception as exc:  # noqa: BLE001
        return {
            "status": "error",
            "reason": "Query failed -- verify community2collection/handle schema with raw_sql().",
            "detail": str(exc),
        }
    return {"status": "ok", "collections": [{"handle": r[0], "name": r[1]} for r in rows]}


# ---- metadata export / import -----------------------------------------

def metadata_export(profile: Profile, identifier: str, local_dir: str) -> dict:
    """Read-only: dspace metadata-export -i <handle> -f <remote.csv>, scp back."""
    remote_csv = f"/tmp/dspace_mcp_export_{identifier.replace('/', '_')}.csv"
    result = ssh.dspace_cli(profile, f"metadata-export -i {shlex.quote(identifier)} -f {shlex.quote(remote_csv)}", timeout=300)
    if not result.ok:
        return {"status": "error", "stdout": result.stdout, "stderr": result.stderr}

    os.makedirs(local_dir, exist_ok=True)
    local_csv = os.path.join(local_dir, os.path.basename(remote_csv))
    scp_result = ssh.scp_from(profile, remote_csv, local_csv)
    if not scp_result.ok:
        return {"status": "error", "reason": "export succeeded but scp back failed", "stderr": scp_result.stderr}

    return {"status": "ok", "local_csv": local_csv, "remote_csv": remote_csv}


def metadata_import(
    profile: Profile,
    local_csv_path: str,
    confirm: bool = False,
    silent: bool = True,
    eperson: str | None = None,
    workflow: bool = False,
    notify: bool = False,
    apply_template: bool = False,
) -> dict:
    eperson = eperson or profile.admin_eperson
    remote_csv = f"/tmp/dspace_mcp_import_{os.path.basename(local_csv_path)}"

    flags = ["-f", remote_csv]
    if silent:
        flags.append("-s")
    if eperson:
        flags += ["-e", eperson]
    if workflow:
        flags.append("-w")
    if notify:
        flags.append("-n")
    if apply_template:
        flags.append("-t")
    cmd = f"metadata-import {' '.join(shlex.quote(f) for f in flags)}"

    preview = guards.check_confirm(
        confirm,
        commands=[f"scp {local_csv_path} -> {remote_csv}", f"dspace {cmd}"],
        effect=f"Applies metadata edits from {local_csv_path} to the live repository "
               f"(rows with a leading '+' in id create new items).",
        local_csv=local_csv_path,
    )
    if preview is not None:
        return preview

    scp_result = ssh.scp_to(profile, local_csv_path, remote_csv)
    if not scp_result.ok:
        return {"status": "error", "stage": "scp", "stderr": scp_result.stderr}

    result = ssh.dspace_cli(profile, cmd, timeout=600)
    return {
        "status": "ok" if result.ok else "error",
        "executed": True,
        "command": cmd,
        "stdout": result.stdout,
        "stderr": result.stderr,
    }


# ---- SAF import ---------------------------------------------------------

def saf_import(
    profile: Profile,
    local_saf_dir: str,
    collection_handle: str,
    confirm: bool = False,
    eperson: str | None = None,
) -> dict:
    """Zip a local SAF directory, upload it, and `dspace import -a`.

    DSpace <7 classic bitstore installs need this to run as root (see
    versions.VersionProfile.import_needs_root), which leaves new assetstore
    files root-owned -- chown_assetstore() runs automatically afterward
    when the version profile says so.
    """
    version = resolve_version(profile.dspace_version)
    eperson = eperson or profile.admin_eperson

    validation_issues = saf_mod.validate_saf_dir(local_saf_dir)
    item_dirs = [d for d in os.listdir(local_saf_dir) if os.path.isdir(os.path.join(local_saf_dir, d))]

    remote_zip = f"/tmp/dspace_mcp_saf_{os.path.basename(local_saf_dir.rstrip('/'))}.zip"
    remote_extract_dir = remote_zip[:-4]
    remote_mapfile = f"/tmp/dspace_mcp_mapfile_{os.path.basename(local_saf_dir.rstrip('/'))}.txt"

    import_cmd = (
        f"import -a -e {shlex.quote(eperson)} -c {shlex.quote(collection_handle)} "
        f"-s {shlex.quote(remote_extract_dir)} -m {shlex.quote(remote_mapfile)}"
    )
    commands = [
        f"scp {local_saf_dir}.zip -> {remote_zip}",
        f"unzip {remote_zip} -d /tmp",
        f"{'sudo ' if version.import_needs_root else ''}dspace {import_cmd}",
    ]
    if version.needs_chown_after_root_write:
        commands.append(f"chown -R {profile.tomcat_user}:{profile.tomcat_user} {profile.assetstore.root}")

    preview = guards.check_confirm(
        confirm,
        commands=commands,
        effect=f"Imports {len(item_dirs)} item(s) from {local_saf_dir} into collection {collection_handle}.",
        item_count=len(item_dirs),
        validation_issues=[{"item": i.item_dir, "problem": i.problem} for i in validation_issues],
    )
    if preview is not None:
        return preview

    if validation_issues:
        return {
            "status": "error",
            "reason": "SAF validation failed -- run saf_repair() or fix manually before importing.",
            "validation_issues": [{"item": i.item_dir, "problem": i.problem} for i in validation_issues],
        }

    import shutil as _shutil
    zip_path = local_saf_dir.rstrip("/") + ".zip"
    if not os.path.isfile(zip_path):
        base_name = local_saf_dir.rstrip("/")
        _shutil.make_archive(base_name, "zip", root_dir=os.path.dirname(base_name), base_dir=os.path.basename(base_name))

    scp_result = ssh.scp_to(profile, zip_path, remote_zip, timeout=600)
    if not scp_result.ok:
        return {"status": "error", "stage": "scp", "stderr": scp_result.stderr}

    unzip_result = ssh.run(profile, f"cd /tmp && unzip -oq {shlex.quote(remote_zip)}", timeout=120)
    if not unzip_result.ok:
        return {"status": "error", "stage": "unzip", "stderr": unzip_result.stderr}

    if version.import_needs_root:
        import_result = ssh.sudo_run(profile, f"{shlex.quote(profile.cli_path)} {import_cmd}", timeout=1800)
    else:
        import_result = ssh.dspace_cli(profile, import_cmd, timeout=1800)

    out = {
        "status": "ok" if import_result.ok else "error",
        "executed": True,
        "command": import_cmd,
        "stdout": import_result.stdout,
        "stderr": import_result.stderr,
    }

    if version.needs_chown_after_root_write and import_result.ok:
        chown_result = ssh.chown_assetstore(profile)
        out["chown_ok"] = chown_result.ok

    mapfile_result = ssh.run(profile, f"cat {shlex.quote(remote_mapfile)}", timeout=30)
    out["mapfile"] = mapfile_result.stdout

    return out


# ---- itemupdate (bitstream add/delete, no metadata change) --------------

def item_update_delete_bitstreams(
    profile: Profile,
    identifier: str,
    filename_regex: str,
    confirm: bool = False,
    dry_run_flag: bool = True,
) -> dict:
    """dspace itemupdate -D BitstreamFilterByFilename -F <props>.

    .properties files are ISO-8859-1; non-Latin-1 characters in
    filename_regex are escaped as \\uXXXX automatically (matches how this
    has always been done by hand for Cyrillic filenames at MGIMO).
    """
    props_content = "filename=" + "".join(
        c if ord(c) < 128 else f"\\u{ord(c):04x}" for c in filename_regex
    ) + "\n"

    remote_props = f"/tmp/dspace_mcp_filter_{abs(hash(filename_regex))}.properties"
    test_flag = " -t" if dry_run_flag else ""
    cmd = (
        f"itemupdate -i {shlex.quote(identifier)} "
        f"-D org.dspace.app.itemupdate.BitstreamFilterByFilename -F {shlex.quote(remote_props)}{test_flag}"
    )

    preview = guards.check_confirm(
        confirm,
        commands=[f"write {remote_props} (ISO-8859-1)", f"dspace {cmd}"],
        effect=f"Deletes bitstreams on {identifier} matching regex {filename_regex!r}"
               f"{' (dry_run_flag=True: itemupdate -t, reports only)' if dry_run_flag else ' PERMANENTLY'}.",
    )
    if preview is not None:
        return preview

    with tempfile.NamedTemporaryFile(mode="w", encoding="latin-1", suffix=".properties", delete=False) as f:
        f.write(props_content)
        local_props = f.name
    try:
        scp_result = ssh.scp_to(profile, local_props, remote_props)
        if not scp_result.ok:
            return {"status": "error", "stage": "scp", "stderr": scp_result.stderr}
    finally:
        os.unlink(local_props)

    as_root = resolve_version(profile.dspace_version).import_needs_root
    result = ssh.dspace_cli(profile, cmd, as_root=as_root, timeout=300)
    return {"status": "ok" if result.ok else "error", "executed": True, "command": cmd, "stdout": result.stdout, "stderr": result.stderr}


def item_update_add_bitstreams(
    profile: Profile,
    identifier: str,
    local_source_dir: str,
    confirm: bool = False,
) -> dict:
    """dspace itemupdate -A -s <source_dir> -i <identifier>.

    local_source_dir must contain the bitstream file(s) plus a `contents`
    file listing them (same format as a SAF item directory).
    """
    remote_dir = f"/tmp/dspace_mcp_addbs_{os.path.basename(local_source_dir.rstrip('/'))}"
    cmd = f"itemupdate -a -i {shlex.quote(identifier)} -s {shlex.quote(remote_dir)}"

    preview = guards.check_confirm(
        confirm,
        commands=[f"scp -r {local_source_dir} -> {remote_dir}", f"dspace {cmd}"],
        effect=f"Adds bitstream(s) from {local_source_dir} to item {identifier}.",
    )
    if preview is not None:
        return preview

    ssh.run(profile, f"mkdir -p {shlex.quote(remote_dir)}", timeout=30)
    for fn in os.listdir(local_source_dir):
        scp_result = ssh.scp_to(profile, os.path.join(local_source_dir, fn), f"{remote_dir}/{fn}")
        if not scp_result.ok:
            return {"status": "error", "stage": f"scp {fn}", "stderr": scp_result.stderr}

    as_root = resolve_version(profile.dspace_version).import_needs_root
    result = ssh.dspace_cli(profile, cmd, as_root=as_root, timeout=300)
    out = {"status": "ok" if result.ok else "error", "executed": True, "command": cmd, "stdout": result.stdout, "stderr": result.stderr}
    if as_root and result.ok:
        chown_result = ssh.chown_assetstore(profile)
        out["chown_ok"] = chown_result.ok
    return out


# ---- filter-media ---------------------------------------------------------

def filter_media(
    profile: Profile,
    identifier: str,
    plugin: str,
    confirm: bool = False,
    max_items: int | None = None,
    force: bool = False,
) -> dict:
    """dspace filter-media -i <identifier> -p "<plugin>" [-m N] [-f].

    NOTE: -m is a MAXIMUM ITEM COUNT for this invocation, not a batch/commit
    size -- to process an entire large collection, either omit max_items or
    call this repeatedly (e.g. from a loop tool) until it reports 0 processed.
    """
    flags = ["-i", identifier, "-p", plugin]
    if max_items is not None:
        flags += ["-m", str(max_items)]
    if force:
        flags.append("-f")
    cmd = "filter-media " + " ".join(shlex.quote(f) for f in flags)

    preview = guards.check_confirm(
        confirm,
        commands=[f"JAVA_OPTS includes {profile.java_opts_extra!r}", f"dspace {cmd}"],
        effect=f"Runs the '{plugin}' media filter over {identifier}"
               f"{f' (max {max_items} items)' if max_items else ' (no limit -- processes everything pending)'}.",
    )
    if preview is not None:
        return preview

    result = ssh.dspace_cli(profile, cmd, timeout=1800)
    return {"status": "ok" if result.ok else "error", "executed": True, "command": cmd, "stdout": result.stdout, "stderr": result.stderr}


def index_discovery(profile: Profile, identifier: str | None = None) -> dict:
    """Reindex Solr discovery for a handle (or the whole repo if omitted)."""
    cmd = "index-discovery" + (f" -i {shlex.quote(identifier)}" if identifier else "")
    result = ssh.dspace_cli(profile, cmd, timeout=1800)
    return {"status": "ok" if result.ok else "error", "command": cmd, "stdout": result.stdout, "stderr": result.stderr}


def cleanup(profile: Profile, confirm: bool = False) -> dict:
    """dspace cleanup -- purges bitstreams flagged deleted=true. Run after
    any itemupdate -D or metadata edits that removed bitstreams."""
    preview = guards.check_confirm(
        confirm,
        commands=["dspace cleanup"],
        effect="Permanently removes bitstream files already flagged deleted in the DB.",
    )
    if preview is not None:
        return preview
    result = ssh.dspace_cli(profile, "cleanup", timeout=600)
    return {"status": "ok" if result.ok else "error", "executed": True, "stdout": result.stdout, "stderr": result.stderr}
