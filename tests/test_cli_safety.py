"""Safety tests for dspace.cli: SAF import paths, read-only raw_sql, SQL-injection validation."""

import pytest

from dspace_mcp import ssh
from dspace_mcp.config import Profile
from dspace_mcp.dspace import cli


@pytest.fixture
def profile():
    return Profile(name="t", dspace_version="6.3", admin_eperson="admin@example.org")


@pytest.fixture
def recorded(monkeypatch):
    calls = {"run": [], "sudo": [], "scp": [], "sql": []}

    def fake_result(cmd, ok=True):
        return ssh.CommandResult(returncode=0 if ok else 1, stdout="", stderr="", command=cmd)

    monkeypatch.setattr(ssh, "run", lambda p, c, timeout=60, check=False: calls["run"].append(c) or fake_result(c))
    monkeypatch.setattr(ssh, "sudo_run", lambda p, c, timeout=120, check=False: calls["sudo"].append(c) or fake_result(c))
    monkeypatch.setattr(ssh, "scp_to", lambda p, l, r, timeout=120: calls["scp"].append((l, r)) or fake_result("scp"))
    monkeypatch.setattr(ssh, "chown_assetstore", lambda p: fake_result("chown"))
    monkeypatch.setattr(ssh, "psql_query", lambda p, sql, timeout=60: calls["sql"].append(sql) or [])
    return calls


def _make_saf(tmp_path):
    saf = tmp_path / "SimpleArchiveFormat"
    item = saf / "item_001"
    item.mkdir(parents=True)
    (item / "dublin_core.xml").write_text(
        '<?xml version="1.0"?><dublin_core><dcvalue element="title" qualifier="">T</dcvalue></dublin_core>'
    )
    (item / "contents").write_text("a.txt\n")
    (item / "a.txt").write_text("x")
    (item / "collections").write_text("123456789/1\n")
    return saf


def test_saf_import_unzips_into_import_source(tmp_path, profile, recorded):
    saf = _make_saf(tmp_path)
    stale = tmp_path / "SimpleArchiveFormat.zip"
    stale.write_bytes(b"stale")

    out = cli.saf_import(profile, str(saf), "123456789/1", confirm=True)
    assert out["executed"] is True

    unzip = next(c for c in recorded["run"] if "unzip" in c)
    extract_dir = "/tmp/dspace_mcp_saf_SimpleArchiveFormat"
    assert f"-d {extract_dir}" in unzip
    assert "rm -rf" in unzip and "mkdir -p" in unzip

    import_cmd = " ".join(recorded["sudo"])
    assert f"-s {extract_dir}/SimpleArchiveFormat" in import_cmd
    # stale zip was rebuilt, not reused
    assert stale.read_bytes() != b"stale"


def test_saf_import_preview_matches_executed(tmp_path, profile, recorded):
    saf = _make_saf(tmp_path)
    preview = cli.saf_import(profile, str(saf), "123456789/1", confirm=False)
    assert preview["executed"] is False
    cmds = preview["would_run"]
    cli.saf_import(profile, str(saf), "123456789/1", confirm=True)
    unzip = next(c for c in recorded["run"] if "unzip" in c)
    assert unzip in cmds
    assert recorded["scp"][0][1] in " ".join(cmds)


def test_raw_sql_readonly_wraps_in_read_only_txn(profile, recorded):
    out = cli.raw_sql(profile, "SELECT 1")
    assert out["status"] == "ok"
    assert recorded["sql"][0].startswith("BEGIN READ ONLY;")


def test_raw_sql_readonly_rejects_non_select(profile, recorded):
    assert cli.raw_sql(profile, "DROP TABLE item")["status"] == "blocked"
    assert recorded["sql"] == []


@pytest.mark.parametrize("sql", [
    "SELECT 1; COMMIT; DROP TABLE item",
    "SELECT 1; end; DROP TABLE item",
    "SELECT 1; SET default_transaction_read_only=off",
    "SELECT 1; /* x */ COMMIT",
    "SELECT 1 -- hi\n; COMMIT",
])
def test_raw_sql_readonly_blocks_txn_escape(profile, recorded, sql):
    assert cli.raw_sql(profile, sql)["status"] == "blocked"
    assert recorded["sql"] == []


@pytest.mark.parametrize("schema,element,qualifier", [
    ("dc'; DROP TABLE item;--", "title", None),
    ("dc", "title' OR '1'='1", None),
    ("dc", "title", "x' OR 1=1 --"),
    ("", "title", None),
])
def test_resolve_field_id_rejects_injection(profile, recorded, schema, element, qualifier):
    out = cli.resolve_field_id(profile, schema, element, qualifier)
    assert out["status"] == "error"
    assert recorded["sql"] == []


def test_resolve_field_id_valid(profile, monkeypatch):
    seen = []
    monkeypatch.setattr(ssh, "psql_query", lambda p, sql, timeout=60: seen.append(sql) or [["70"]])
    assert cli.resolve_field_id(profile, "dc", "contributor", "author") == {"status": "ok", "field_id": 70}
    assert cli.resolve_field_id(profile, "dc", "title", "")["status"] == "ok"
    assert "qualifier IS NULL" in seen[1]


@pytest.mark.parametrize("handle", ["1/2' OR '1'='1", "abc/1", "123/", "1/2; DROP TABLE item", "123456789"])
def test_list_collections_rejects_bad_handle(profile, recorded, handle):
    assert cli.list_collections(profile, handle)["status"] == "error"
    assert recorded["sql"] == []
