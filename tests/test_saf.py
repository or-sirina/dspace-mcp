"""Tests for the local-only SAF package builder/validator/repairer."""

import os

from dspace_mcp.dspace import saf


def test_parse_dc_column():
    assert saf.parse_dc_column("dc.title") == ("dc", "title", None, None)
    assert saf.parse_dc_column("dc.title[en]") == ("dc", "title", None, "en")
    assert saf.parse_dc_column("dc.contributor.author") == ("dc", "contributor", "author", None)
    assert saf.parse_dc_column("local.type.coar") == ("local", "type", "coar", None)
    assert saf.parse_dc_column("filename") is None
    assert saf.parse_dc_column("collection") is None


def test_escape_xml():
    assert saf.escape_xml('a & b <c> "d"') == "a &amp; b &lt;c&gt; &quot;d&quot;"
    assert saf.escape_xml("") == ""


def test_sanitize_xml_text():
    # bare ampersand escaped, but an existing entity is left alone
    assert saf.sanitize_xml_text("a & b") == "a &amp; b"
    assert saf.sanitize_xml_text("a &amp; b") == "a &amp; b"
    # XML 1.0 illegal control characters are stripped
    assert saf.sanitize_xml_text("ok\x00bad\x1f!") == "okbad!"


def test_build_from_csv(tmp_path):
    (tmp_path / "paper.pdf").write_bytes(b"%PDF-1.4 fake content")
    csv_path = tmp_path / "meta.csv"
    csv_path.write_text(
        "filename,dc.title[en],dc.contributor.author[en],dc.date.issued,collection\n"
        "paper.pdf,Test Title,Smith J.,2024,123456789/1\n",
        encoding="utf-8",
    )

    result = saf.build_from_csv(str(csv_path), output_name="SimpleArchiveFormat")

    assert result.item_count == 1
    item_dir = os.path.join(str(tmp_path), "SimpleArchiveFormat", "item_001")
    assert os.path.isdir(item_dir)
    assert os.path.isfile(os.path.join(item_dir, "dublin_core.xml"))
    assert os.path.isfile(os.path.join(item_dir, "contents"))
    assert os.path.isfile(os.path.join(item_dir, "paper.pdf"))

    dc = open(os.path.join(item_dir, "dublin_core.xml"), encoding="utf-8").read()
    assert "Test Title" in dc
    assert "Smith J." in dc
    assert "2024" in dc

    contents = open(os.path.join(item_dir, "contents"), encoding="utf-8").read()
    assert "paper.pdf" in contents

    collections = open(os.path.join(item_dir, "collections"), encoding="utf-8").read()
    assert "123456789/1" in collections


def test_build_from_csv_missing_file_warns(tmp_path):
    csv_path = tmp_path / "meta.csv"
    csv_path.write_text("filename,dc.title\nmissing.pdf,Title\n", encoding="utf-8")

    result = saf.build_from_csv(str(csv_path))

    assert result.item_count == 1
    assert any("not found" in w for w in result.warnings)
    # the missing file is referenced but doesn't exist on disk, so it is neither
    # copied nor reported as an unused file
    assert result.unused_files == []


def test_generate_manifest(tmp_path):
    (tmp_path / "a.pdf").write_bytes(b"%PDF")
    (tmp_path / "b.pdf").write_bytes(b"%PDF")
    csv_path = tmp_path / "manifest.csv"

    result = saf.generate_manifest(str(csv_path))

    assert result["status"] == "ok"
    assert result["file_count"] == 2
    header = csv_path.read_text(encoding="utf-8-sig").splitlines()[0]
    assert header.startswith("filename")


def test_validate_and_repair(tmp_path):
    saf_dir = tmp_path / "saf"
    item_dir = saf_dir / "item_001"
    item_dir.mkdir(parents=True)
    (item_dir / "contents").write_text("missing.pdf\n", encoding="utf-8")

    issues = saf.validate_saf_dir(str(saf_dir))
    problems = {i.problem for i in issues}
    assert any("no dublin_core.xml" in p for p in problems)
    assert any("missing file" in p for p in problems)

    result = saf.repair_saf_dir(str(saf_dir))
    assert result["status"] == "ok"
    # the dangling contents line is dropped
    assert (item_dir / "contents").read_text(encoding="utf-8").strip() == ""


def test_validate_missing_dir(tmp_path):
    issues = saf.validate_saf_dir(str(tmp_path / "does_not_exist"))
    assert any("not a directory" in i.problem for i in issues)
