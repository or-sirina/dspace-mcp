"""Tests for the shared scientometric row-schema helpers."""

from dspace_mcp.scholar import common


def test_normalize_doi():
    assert common.normalize_doi("https://doi.org/10.1000/xyz") == "10.1000/xyz"
    assert common.normalize_doi("10.1000/XYZ") == "10.1000/xyz"
    assert common.normalize_doi("") == ""


def test_normalize_title():
    assert common.normalize_title("  Hello,   World!  ") == "hello world"
    assert common.normalize_title("") == ""


def test_names_to_lastname_initials():
    assert common.names_to_lastname_initials("John Smith") == "Smith, J."
    assert common.names_to_lastname_initials("John Q. Public") == "Public, J. Q."
    assert common.names_to_lastname_initials("Smith, John") == "Smith, John"
    assert common.names_to_lastname_initials("Single") == "Single"


def test_doi_to_filename():
    assert common.doi_to_filename("10.1000/abc", "mgimo") == "mgimo_10.1000_abc.pdf"
    assert common.doi_to_filename("", "mgimo") == ""


def test_dedup_rows():
    rows = [
        {"dc.identifier.doi": "10.1/a", "dc.title[en]": "Title A"},
        {"dc.identifier.doi": "10.1/a", "dc.title[en]": "Title A duplicate"},
        {"dc.identifier.doi": "", "dc.title[en]": "Title B"},
    ]
    kept, stats = common.dedup_rows(rows, set(), set())

    assert stats["input"] == 3
    assert stats["kept"] == 2
    # within-batch and pre-existing duplicates share the DOI counter
    assert stats["dup_existing_doi"] == 1
    assert [r["dc.title[en]"] for r in kept] == ["Title A", "Title B"]


def test_dedup_against_existing():
    rows = [{"dc.identifier.doi": "10.1/a", "dc.title[en]": "Title A"}]
    kept, stats = common.dedup_rows(rows, {"10.1/a"}, {"title a"})
    assert stats["kept"] == 0
    assert kept == []


def test_write_csv_roundtrip(tmp_path):
    rows = [
        {
            "filename": "a.pdf",
            "dc.title[en]": "Title",
            "_oa_url": "https://example.org/a.pdf",
        }
    ]
    out = str(tmp_path / "out.csv")
    result = common.write_csv(rows, out)
    assert result["status"] == "ok"
    # private fields are stripped from the written CSV
    text = open(out, encoding="utf-8-sig").read()
    assert "Title" in text
    assert "_oa_url" not in text
