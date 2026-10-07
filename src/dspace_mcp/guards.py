"""Guarded-write helpers shared by every state-changing tool.

Design (per the "guarded writes" policy):
  - Reads, local builds, and external-API fetches always run.
  - State-changing DSpace operations (import, metadata-import, itemupdate,
    filter-media) default to a dry run: with confirm=False they report the
    exact command(s) they would execute and their predicted effect, and do
    nothing else. Only confirm=True executes.
  - Direct database/assetstore mutations (collection/logo creation, intranet
    ACL changes, raw SQL) require an additional allow_db_write=True and
    trigger an automatic backup first.

Tool functions call check_confirm()/check_db_write() at the top and return
immediately if a preview/blocked dict comes back.
"""

from __future__ import annotations

from typing import Any, Callable


def preview(commands: list[str], effect: str, **extra: Any) -> dict:
    """Build the "not executed yet" response shown when confirm=False."""
    return {
        "status": "dry_run",
        "executed": False,
        "would_run": commands,
        "predicted_effect": effect,
        **extra,
    }


def check_confirm(confirm: bool, commands: list[str], effect: str, **extra: Any) -> dict | None:
    """Return a dry-run preview dict if confirm is False, else None (proceed)."""
    if not confirm:
        return preview(commands, effect, **extra)
    return None


def check_db_write(
    allow_db_write: bool,
    backup_fn: Callable[[], dict],
    action: str,
    confirm: bool = True,
) -> dict | None:
    """Gate a direct DB/assetstore mutation.

    Returns a "blocked" dict (backup NOT taken) if allow_db_write is False.
    If confirm is False (dry run) no backup is taken either: returns
    {"status": "backup_pending", "executed": False} so previews can mention it.
    Otherwise runs backup_fn() (expected to perform a fresh pg_dump). If the
    backup fails (raises, or returns a status other than "ok"), returns an
    "error" dict with stage "backup" and the caller MUST stop -- see
    is_stop(). On success returns {"status": "backed_up", "backup": <result>}
    so the caller can merge it into its own response, then proceed.
    """
    if not allow_db_write:
        return {
            "status": "blocked",
            "executed": False,
            "reason": (
                f"{action} writes directly to the database/assetstore. "
                f"Retry with allow_db_write=True; a fresh backup will be "
                f"taken automatically first."
            ),
        }
    if not confirm:
        return {
            "status": "backup_pending",
            "executed": False,
            "note": "A fresh backup will be taken when confirm=True.",
        }
    try:
        backup = backup_fn()
    except Exception as exc:  # noqa: BLE001 - any failure must block the write
        backup = {"status": "error", "stage": "backup", "reason": f"{type(exc).__name__}: {exc}"}
    if not isinstance(backup, dict) or backup.get("status") != "ok":
        return {
            "status": "error",
            "stage": "backup",
            "executed": False,
            "reason": f"{action} aborted: pre-write backup failed; nothing was changed.",
            "backup": backup,
        }
    return {"status": "backed_up", "backup": backup}


def is_stop(gate: dict | None) -> bool:
    """True if a check_db_write() result means the caller must return it as-is."""
    return gate is not None and gate.get("status") in ("blocked", "error")
