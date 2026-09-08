"""Tests for the DSpace version-capability layer."""

from dspace_mcp.versions import base


def test_resolve_known_versions():
    assert base.resolve("6.3").major == 6
    assert base.resolve("7").major == 7
    assert base.resolve("8.2").major == 8
    assert base.resolve("5.10").major == 5


def test_resolve_capability_flags():
    v6 = base.resolve("6.3")
    assert v6.import_needs_root is True
    assert v6.needs_chown_after_root_write is True
    assert v6.rest_generally_available is False

    v7 = base.resolve("7.6")
    assert v7.import_needs_root is False
    assert v7.rest_generally_available is True
    assert v7.rest_base_path == "/server/api"


def test_resolve_future_version_falls_back():
    assert base.resolve("9.0").major == 8


def test_resolve_unparseable_falls_back():
    assert base.resolve("garbage").major == 6
