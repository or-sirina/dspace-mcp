"""High-risk admin operations: DB backup, collection/community creation,
collection logo injection, intranet-only access control, and read-only
server diagnostics.

Every mutating function here requires allow_db_write=True (checked via
guards.check_db_write, which auto-runs db_backup() first) plus confirm=True
for the actual command execution -- these are the operations most likely to
do real damage to a production repository if wrong.
"""

from __future__ import annotations

import hashlib
import os
import random
import shlex
import tempfile
import time
import uuid as uuid_mod

from .. import guards, ssh
from ..config import Profile
from ..versions import resolve as resolve_version


# ---- DB backup ------------------------------------------------------------

def db_backup(profile: Profile, keep_days: int = 14) -> dict:
    """pg_dump -Fc the DSpace database to profile.backup_dir on the remote
    host, then delete dumps older than keep_days. Metadata only -- does NOT
    touch the assetstore (bitstream files), matching the existing MGIMO
    dspace-db-backup cron job this mirrors.
    """
    stamp = time.strftime("%Y-%m-%d_%H%M")
    out_path = f"{profile.backup_dir}/dspace_{stamp}.dump"

    ssh.run(profile, f"mkdir -p {shlex.quote(profile.backup_dir)}", timeout=30)

    if profile.db.ssh_peer_user:
        dump_cmd = (
            f"sudo -u {shlex.quote(profile.db.ssh_peer_user)} "
            f"pg_dump -Fc {shlex.quote(profile.db.name)} -f {shlex.quote(out_path)}"
        )
    else:
        pw = profile.db.password or ""
        dump_cmd = (
            f"PGPASSWORD={shlex.quote(pw)} pg_dump -h {shlex.quote(profile.db.host)} "
            f"-p {profile.db.port} -U {shlex.quote(profile.db.user)} "
            f"-Fc {shlex.quote(profile.db.name)} -f {shlex.quote(out_path)}"
        )
    result = ssh.run(profile, dump_cmd, timeout=600)
    if not result.ok:
        return {"status": "error", "stage": "pg_dump", "stderr": result.stderr}

    size_result = ssh.run(profile, f"du -h {shlex.quote(out_path)} | cut -f1", timeout=15)
    rotate_result = ssh.run(
        profile,
        f"find {shlex.quote(profile.backup_dir)} -maxdepth 1 -name 'dspace_*.dump' "
        f"-mtime +{keep_days} -print -delete",
        timeout=30,
    )

    return {
        "status": "ok",
        "backup_path": out_path,
        "size": size_result.stdout.strip(),
        "rotated_removed": [l for l in rotate_result.stdout.splitlines() if l.strip()],
    }


# ---- collection / community creation --------------------------------------

def collection_create(
    profile: Profile,
    parent_community_handle: str,
    name: str,
    confirm: bool = False,
    allow_db_write: bool = False,
) -> dict:
    """Create a collection under an existing community.

    Tries `dspace structure-builder` first (present since DSpace 5.x/6.x per
    LYRASIS docs -- https://wiki.lyrasis.org/display/DSDOC6x/Importing+Community+and+Collection+Hierarchy),
    which is a first-class DSpace CLI feature and does not require raw SQL.
    Falls back to direct SQL (mint a handle via nextval, mirror an existing
    sibling collection's resourcepolicies) if structure-builder is
    unavailable/fails -- this SQL fallback is what was historically used at
    MGIMO's DSpace 6.3 instance without trying structure-builder first.
    """
    version = resolve_version(profile.dspace_version)

    gate = guards.check_db_write(allow_db_write, lambda: db_backup(profile), "collection_create")
    if gate is not None and gate.get("status") == "blocked":
        return gate

    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<import_structure><collection><name>{_esc(name)}</name></collection></import_structure>\n'
    )
    remote_in = f"/tmp/dspace_mcp_structbuild_{abs(hash(name))}_in.xml"
    remote_out = f"/tmp/dspace_mcp_structbuild_{abs(hash(name))}_out.xml"
    cli_cmd = (
        f"structure-builder -f {shlex.quote(remote_in)} -o {shlex.quote(remote_out)} "
        f"-e {shlex.quote(profile.admin_eperson)}"
    )

    preview = guards.check_confirm(
        confirm,
        commands=[
            f"write {remote_in}: {xml.strip()}",
            f"dspace {cli_cmd}   # attempt 1: structure-builder",
            "(falls back to direct SQL against handle/collection/resourcepolicy if this fails)",
        ],
        effect=f"Creates collection '{name}' under community {parent_community_handle}.",
        backup=gate,
        note=(
            "structure-builder's XML format in LYRASIS docs creates collections "
            "as children of a <community> block, which normally means an "
            "entire community subtree is (re)declared, not a single collection "
            "appended to an existing one. This tool passes a bare <collection> "
            "element and reports whatever structure-builder actually does; "
            "verify the result and be ready to use the SQL fallback."
        ),
    )
    if preview is not None:
        return preview

    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", suffix=".xml", delete=False) as f:
        f.write(xml)
        local_xml = f.name
    try:
        scp_result = ssh.scp_to(profile, local_xml, remote_in)
    finally:
        os.unlink(local_xml)
    if not scp_result.ok:
        return {"status": "error", "stage": "scp", "stderr": scp_result.stderr}

    as_root = version.import_needs_root
    cli_result = ssh.dspace_cli(profile, cli_cmd, as_root=as_root, timeout=300)

    if cli_result.ok:
        out_xml = ssh.run(profile, f"cat {shlex.quote(remote_out)}", timeout=30).stdout
        return {
            "status": "ok",
            "method": "structure-builder",
            "stdout": cli_result.stdout,
            "output_xml": out_xml,
            "backup": gate,
            "note": "Parse output_xml for the assigned handle; verify it landed under the intended community.",
        }

    # Fallback: direct SQL, mirroring an existing sibling in the target community.
    sql_result = _collection_create_via_sql(profile, parent_community_handle, name)
    return {
        "status": "ok" if sql_result.get("status") == "ok" else "error",
        "method": "sql_fallback",
        "structure_builder_error": cli_result.stderr,
        "backup": gate,
        **sql_result,
    }


def _collection_create_via_sql(profile: Profile, parent_community_handle: str, name: str) -> dict:
    """Mint a handle + collection row, mirror the first sibling's
    resourcepolicies (so the new collection inherits the same submit/read
    permissions as its neighbors), and link it under the parent community.
    """
    sql = f"""\\set ON_ERROR_STOP on
BEGIN;

CREATE TEMP TABLE _parent AS
SELECT h.resource_id AS community_id FROM handle h WHERE h.handle = '{_esc(parent_community_handle)}' AND h.resource_type_id = 4;

CREATE TEMP TABLE _sibling AS
SELECT c2c.collection_id FROM community2collection c2c, _parent p
WHERE c2c.community_id = p.community_id LIMIT 1;

INSERT INTO collection (uuid) VALUES (gen_random_uuid());
CREATE TEMP TABLE _new AS
SELECT uuid AS collection_id FROM collection ORDER BY collection_id DESC LIMIT 1;

INSERT INTO handle (handle_id, handle, resource_type_id, resource_id)
SELECT nextval('handle_id_seq'),
       (SELECT '{profile.handle_prefix}/' || nextval('handle_seq')::text),
       3, n.collection_id
FROM _new n;

INSERT INTO metadatavalue (metadata_field_id, text_value, text_lang, place, confidence, dspace_object_id)
SELECT (SELECT r.metadata_field_id FROM metadatafieldregistry r JOIN metadataschemaregistry s
          ON s.metadata_schema_id = r.metadata_schema_id WHERE s.short_id='dc' AND r.element='title' AND r.qualifier IS NULL),
       '{_esc(name)}', NULL, 0, -1, n.collection_id
FROM _new n;

INSERT INTO community2collection (community_id, collection_id)
SELECT p.community_id, n.collection_id FROM _parent p, _new n;

INSERT INTO resourcepolicy (resource_type_id, resource_id, action_id, epersongroup_id, rptype)
SELECT 3, n.collection_id, rp.action_id, rp.epersongroup_id, rp.rptype
FROM resourcepolicy rp, _sibling s, _new n
WHERE rp.resource_type_id = 3 AND rp.resource_id = s.collection_id;

COMMIT;

\\echo NEW_HANDLE:
SELECT h.handle FROM handle h JOIN _new n ON n.collection_id = h.resource_id WHERE h.resource_type_id = 3;
"""
    result = ssh.psql_execute(profile, sql, timeout=60)
    if not result.ok:
        return {"status": "error", "stdout": result.stdout, "stderr": result.stderr}
    return {
        "status": "ok",
        "stdout": result.stdout,
        "note": (
            "Restart Tomcat and run index_discovery() on the parent community "
            "handle before the new collection is visible in the webapp/Solr "
            "(per the MGIMO SQL-collection-creation runbook)."
        ),
    }


def _esc(s: str) -> str:
    return s.replace("'", "''")


# ---- collection logo injection --------------------------------------------

def set_collection_logo(
    profile: Profile,
    collection_handle: str,
    local_image_path: str,
    confirm: bool = False,
    allow_db_write: bool = False,
) -> dict:
    """Inject a JPEG as a collection's logo bitstream directly via the
    assetstore + DB (no CLI/REST path exists for this on DSpace <7).

    Path scheme: <assetstore_root>/<internal_id[0:2]>/<internal_id[2:4]>/<internal_id[4:6]>/<internal_id>
    (3 levels, 2 digits each -- the classic DSpaceBitStoreService default;
    verify against the target instance's assetstore.cfg / dspace.cfg if
    directoryLevels/digitsPerLevel differ). internal_id is a random 38-digit
    decimal string. bitstream.sequence_id MUST be -1, not NULL, or the JSPUI
    will not resolve /retrieve/<uuid> correctly.
    """
    if not os.path.isfile(local_image_path):
        return {"status": "error", "reason": f"{local_image_path} not found locally"}

    with open(local_image_path, "rb") as f:
        data = f.read()
    checksum = hashlib.md5(data).hexdigest()
    size_bytes = len(data)
    internal_id = "".join(str(random.randint(0, 9)) for _ in range(38))
    lvl = [internal_id[0:2], internal_id[2:4], internal_id[4:6]]
    remote_rel_path = "/".join(lvl + [internal_id])
    remote_abs_path = f"{profile.assetstore.root}/{remote_rel_path}"
    remote_dir = os.path.dirname(remote_abs_path)
    bitstream_uuid = str(uuid_mod.uuid4())

    gate = guards.check_db_write(allow_db_write, lambda: db_backup(profile), "set_collection_logo")
    if gate is not None and gate.get("status") == "blocked":
        return gate

    preview = guards.check_confirm(
        confirm,
        commands=[
            f"mkdir -p {remote_dir} && scp {local_image_path} -> {remote_abs_path}",
            f"chown -R {profile.tomcat_user}:{profile.tomcat_user} {profile.assetstore.root}",
            "INSERT INTO dspaceobject/bitstream (sequence_id=-1, checksum, ...) + resourcepolicy (Anonymous READ) "
            "+ UPDATE collection SET logo_bitstream_id (transactional, backed up)",
        ],
        effect=f"Sets the collection logo for {collection_handle} to {os.path.basename(local_image_path)}.",
        checksum_md5=checksum,
        size_bytes=size_bytes,
        backup=gate,
    )
    if preview is not None:
        return preview

    mkdir_result = ssh.sudo_run(profile, f"mkdir -p {shlex.quote(remote_dir)}", timeout=30)
    if not mkdir_result.ok:
        return {"status": "error", "stage": "mkdir", "stderr": mkdir_result.stderr}
    scp_result = ssh.scp_to(profile, local_image_path, f"/tmp/{os.path.basename(local_image_path)}")
    if not scp_result.ok:
        return {"status": "error", "stage": "scp", "stderr": scp_result.stderr}
    move_result = ssh.sudo_run(
        profile,
        f"mv {shlex.quote('/tmp/' + os.path.basename(local_image_path))} {shlex.quote(remote_abs_path)}",
        timeout=30,
    )
    if not move_result.ok:
        return {"status": "error", "stage": "move_into_assetstore", "stderr": move_result.stderr}
    ssh.chown_assetstore(profile)

    sql = f"""\\set ON_ERROR_STOP on
BEGIN;
INSERT INTO dspaceobject (uuid) VALUES ('{bitstream_uuid}');

INSERT INTO bitstream (uuid, bitstream_format_id, size_bytes, checksum, checksum_algorithm,
                        internal_id, deleted, store_number, sequence_id, name)
VALUES ('{bitstream_uuid}', 16, {size_bytes}, '{checksum}', 'MD5',
        '{internal_id}', false, {profile.assetstore.store_number}, -1, '{_esc(os.path.basename(local_image_path))}');

INSERT INTO resourcepolicy (resource_type_id, resource_id, action_id, epersongroup_id, rptype)
SELECT 0, '{bitstream_uuid}', 0, eg.uuid, 'TYPE_INHERITED'
FROM epersongroup eg WHERE eg.name = 'Anonymous';

UPDATE collection SET logo_bitstream_id = '{bitstream_uuid}'
WHERE uuid = (SELECT resource_id FROM handle WHERE handle = '{_esc(collection_handle)}' AND resource_type_id = 3);
COMMIT;
"""
    result = ssh.psql_execute(profile, sql, timeout=60)
    return {
        "status": "ok" if result.ok else "error",
        "bitstream_uuid": bitstream_uuid,
        "assetstore_path": remote_abs_path,
        "backup": gate,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "note": "Verify via DB parity (SELECT logo_bitstream_id) rather than curl -- "
                "some frontends wrap /retrieve/<uuid> in an HTML shell even when it works.",
    }


# ---- intranet-only (IP-restricted) access ----------------------------------

def set_intranet_access(
    profile: Profile,
    cidr: str,
    group_name: str = "Intranet",
    confirm: bool = False,
    allow_db_write: bool = False,
) -> dict:
    """Report the config changes needed to add an IP-restricted access group.

    This mirrors the config-file-based approach (authentication.cfg +
    authentication-ip.cfg) rather than a pure-DB one, because DSpace's
    IPAuthentication plugin sequence lives in a Spring/properties config
    file, not the database. Returns exact edits + a Tomcat-restart warning;
    only creates the `group_name` EPersonGroup in the DB (which IS safe to
    do transactionally) when confirm+allow_db_write are both set. The
    per-bitstream `permissions:-r '<group_name>'` line still has to be added
    to the relevant SAF `contents` file(s) by the caller (see saf.py) --
    this function does not know which bitstreams should be restricted.
    """
    gate = guards.check_db_write(allow_db_write, lambda: db_backup(profile), "set_intranet_access")
    if gate is not None and gate.get("status") == "blocked":
        return gate

    config_edits = [
        f"authentication.cfg: add org.dspace.authenticate.IPAuthentication to "
        f"plugin.sequence.org.dspace.authenticate.AuthenticationMethod, BEFORE PasswordAuthentication "
        f"(check for an existing *uncommented* line first -- commented examples reuse the same class string)",
        f"authentication-ip.cfg: authentication-ip.{group_name} = {cidr}",
        f"Apache must forward the real client IP (X-Forwarded-For) and Tomcat needs "
        f"RemoteIpValve + dspace.cfg useProxies=true, or every request will appear to "
        f"come from the proxy's own IP.",
        f"Restart {profile.tomcat_service} (~downtime) for the plugin sequence change to take effect.",
    ]

    preview = guards.check_confirm(
        confirm,
        commands=[f"INSERT INTO epersongroup (name='{group_name}') if not exists"] + config_edits,
        effect=f"Creates group '{group_name}' and documents the {cidr}-restricted access config.",
        config_edits=config_edits,
        backup=gate,
    )
    if preview is not None:
        return preview

    sql = f"""\\set ON_ERROR_STOP on
INSERT INTO epersongroup (uuid, name)
SELECT gen_random_uuid(), '{_esc(group_name)}'
WHERE NOT EXISTS (SELECT 1 FROM epersongroup WHERE name = '{_esc(group_name)}');
"""
    result = ssh.psql_execute(profile, sql, timeout=30)
    return {
        "status": "ok" if result.ok else "error",
        "group_created_or_existing": group_name,
        "remaining_manual_steps": config_edits,
        "backup": gate,
        "stdout": result.stdout,
        "stderr": result.stderr,
    }


# ---- diagnostics (read-only) -----------------------------------------------

def server_health(profile: Profile) -> dict:
    """Read-only host diagnostics: root filesystem mount mode + ext4 error
    count, Postgres cluster status, Tomcat status, Solr ping, archived item
    count. See the disk-recovery runbook this generalizes: a
    remounted-read-only root filesystem (VMware/disk I/O error -> aborted
    ext4 journal) is the most disruptive failure mode observed so far.
    """
    out: dict = {}

    mount_result = ssh.run(profile, "mount | grep -E ' / '", timeout=15)
    out["root_mount"] = mount_result.stdout.strip()
    out["root_writable"] = ",rw," in mount_result.stdout or mount_result.stdout.strip().endswith("(rw)")

    tune2fs = ssh.sudo_run(profile, "tune2fs -l $(findmnt -n -o SOURCE /) 2>/dev/null | grep -iE 'Filesystem state|Error count'", timeout=20)
    out["root_fs_state"] = tune2fs.stdout.strip()

    pg = ssh.run(profile, "pg_lsclusters 2>/dev/null", timeout=15)
    out["postgres_clusters"] = pg.stdout.strip()

    tomcat = ssh.run(profile, f"systemctl is-active {shlex.quote(profile.tomcat_service)}", timeout=15)
    out["tomcat_status"] = tomcat.stdout.strip()

    solr = ssh.run(profile, "curl -s --max-time 8 -o /dev/null -w '%{http_code}' http://localhost:8080/solr/search/admin/ping", timeout=15)
    out["solr_ping_http_code"] = solr.stdout.strip()

    try:
        rows = ssh.psql_query(profile, "SELECT count(*) FROM item WHERE in_archive = true;", timeout=20)
        out["archived_item_count"] = int(rows[0][0]) if rows and rows[0] else None
    except Exception as exc:  # noqa: BLE001
        out["db_error"] = str(exc)

    out["overall_ok"] = (
        out.get("root_writable", False)
        and out.get("tomcat_status") == "active"
        and out.get("solr_ping_http_code") == "200"
    )
    return out
