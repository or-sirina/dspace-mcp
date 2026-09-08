"""Deduplicate a harvested batch against one or more existing DSpace CSV
exports/corpora, and split the missing rows into import-sized batches.
Ports find_missing_and_batch.py's DOI/title diffing + 1000-row chunking
(DSpace docs recommend not exceeding ~1000 rows per metadata-import CSV)."""

from __future__ import annotations

from .common import CSV_COLUMNS, dedup_rows, load_existing_dedup, write_csv


def deduplicate(rows: list[dict], existing_csv_paths: list[str]) -> dict:
    """rows: harvested paper dicts (see common.CSV_COLUMNS + _oa_url/_source*).
    existing_csv_paths: DSpace metadata-export/corpus CSVs already in the repo."""
    existing_dois, existing_titles = load_existing_dedup(existing_csv_paths)
    kept, stats = dedup_rows(rows, existing_dois, existing_titles)
    return {"status": "ok", "missing_rows": kept, "stats": stats}


def batch_missing(rows: list[dict], output_dir: str, batch_size: int = 1000, prefix: str = "batch") -> dict:
    """Write `rows` (already deduplicated) to output_dir/<prefix>_N.csv files
    of at most batch_size rows each."""
    import os
    os.makedirs(output_dir, exist_ok=True)
    paths = []
    for i in range(0, len(rows), batch_size):
        chunk = rows[i : i + batch_size]
        path = os.path.join(output_dir, f"{prefix}_{i // batch_size + 1}.csv")
        result = write_csv(chunk, path)
        if result.get("status") == "ok":
            paths.append(path)
    return {"status": "ok", "batch_count": len(paths), "batch_paths": paths, "columns": CSV_COLUMNS}
