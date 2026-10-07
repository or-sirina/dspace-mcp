from dspace_mcp.scholar.download import (
    _download, download_oa_pdfs, is_http_url, safe_filename, try_oa_url,
)


def test_safe_filename():
    assert safe_filename("a.pdf") == "a.pdf"
    assert safe_filename("../../etc/passwd") == "passwd"
    assert safe_filename("/abs/x.pdf") == "x.pdf"
    assert safe_filename("..\\..\\x.pdf") == "x.pdf"
    for bad in ("", "  ", ".", "..", "a/..", "dir/", None):
        assert safe_filename(bad) is None


def test_is_http_url():
    assert is_http_url("http://x.org/a.pdf")
    assert is_http_url("HTTPS://x.org")
    for bad in ("file:///etc/passwd", "ftp://x/a", "javascript:1", "", None, "x.org/a"):
        assert not is_http_url(bad)


class _Boom:
    def get(self, *a, **k):
        raise AssertionError("network used")


def test_bad_scheme_not_fetched():
    assert _download("file:///etc/passwd", _Boom()) is None
    assert try_oa_url("ftp://x/a.pdf", _Boom()) is None


def test_traversal_stays_in_output_dir(tmp_path, monkeypatch):
    import dspace_mcp.scholar.download as d
    monkeypatch.setattr(d, "try_oa_url", lambda *a: b"%PDF" + b"0" * 20000)
    out = tmp_path / "out"
    r = download_oa_pdfs([{"filename": "../evil.pdf", "_oa_url": "http://x/a"}],
                         str(out), "", delay=0)
    assert r["downloaded_count"] == 1
    assert (out / "evil.pdf").exists()
    assert not (tmp_path / "evil.pdf").exists()
