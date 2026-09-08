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
) -> dict | None:
    """Gate a direct DB/assetstore mutation.

    Returns a "blocked" dict (backup NOT taken) if allow_db_write is False.
    Otherwise runs backup_fn() (expected to perform a fresh pg_dump) and
    returns {"status": "backed_up", "backup": <result>} so the caller can
    merge it into its own response, then proceeds with the mutation.
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
    return {"status": "backed_up", "backup": backup_fn()}
