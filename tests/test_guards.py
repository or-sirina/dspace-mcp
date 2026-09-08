"""Tests for the guarded-write gates shared by every mutating tool."""

from dspace_mcp import guards


def test_preview():
    p = guards.preview(["cmd"], "effect", item_count=3)
    assert p["status"] == "dry_run"
    assert p["executed"] is False
    assert p["would_run"] == ["cmd"]
    assert p["predicted_effect"] == "effect"
    assert p["item_count"] == 3


def test_check_confirm_false_returns_preview():
    result = guards.check_confirm(False, ["dspace import"], "imports items")
    assert result is not None
    assert result["status"] == "dry_run"


def test_check_confirm_true_proceeds():
    assert guards.check_confirm(True, ["dspace import"], "imports items") is None


def test_check_db_write_blocked_without_flag():
    result = guards.check_db_write(False, lambda: {"status": "ok"}, "collection_create")
    assert result["status"] == "blocked"
    assert result["executed"] is False


def test_check_db_write_backs_up_and_proceeds():
    result = guards.check_db_write(True, lambda: {"status": "ok", "path": "/backup"}, "op")
    assert result["status"] == "backed_up"
    assert result["backup"]["status"] == "ok"
    assert result["backup"]["path"] == "/backup"
