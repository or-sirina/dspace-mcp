"""Shared row schema + CSV I/O + dedup helpers for the scientometric
harvest pipeline (OpenAlex/CrossRef/CORE/Semantic Scholar -> Unpaywall ->
LLM enrichment -> SAF). Every source-specific fetcher in harvest.py returns
a list of these dicts; downstream tools (dedup, download, enrich,
saf_build_from_csv) all speak this one shape.
"""

from __future__ import annotations

import csv
import os
import re

# Columns written to disk; `_oa_url` / `_source_id` are carried in the
# in-memory dict for download.py but stripped before writing CSV (matches
# the *_oaid.json sidecar pattern from the original scripts, just kept
# in-process instead of round-tripped through a file).
CSV_COLUMNS = [
    "filename",
    "collection",
    "dc.title[en]",
    "dc.title[ru]",
    "dc.contributor.author[en]",
    "dc.contributor.author[ru]",
    "dc.date.issued",
    "dc.description.abstract[en]",
    "dc.description.abstract[ru]",
    "dc.identifier.doi",
    "dc.identifier.issn[online]",
    "dc.identifier.issn[print]",
    "dc.identifier.uri",
    "dc.identifier.citation[en]",
    "dc.identifier.citation[ru]",
    "dc.language.iso",
    "dc.subject[en]",
    "dc.subject[ru]",
    "dc.type",
    "dc.source[en]",
    "dc.source[ru]",
    "dc.format.extent",
    "dc.rights",
    "article.fpage",
    "article.lpage",
    "article.volume",
    "article.issue",
    "article.journalname[en]",
    "article.journalname[ru]",
    "publication.article.doi",
    "publication.journal.link",
]

def normalize_title(title: str) -> str:
    t = (title or "").strip().lower()
    t = re.sub(r"[^\w\s]", "", t)
    return re.sub(r"\s+", " ", t).strip()


def normalize_doi(doi: str) -> str:
    return (doi or "").strip().lower().replace("https://doi.org/", "")


def names_to_lastname_initials(name: str) -> str:
    """'First Middle Last' -> 'Last, F. M.'; already-comma-formed names pass through."""
    name = (name or "").strip()
    if not name or "," in name:
        return name
    parts = name.split()
    if len(parts) < 2:
        return name
    last = parts[-1]
    initials = " ".join(f"{p[0]}." for p in parts[:-1] if p)
    return f"{last}, {initials}"


def doi_to_filename(doi: str, prefix: str = "ext") -> str:
    clean = normalize_doi(doi).replace("/", "_")
    return f"{prefix}_{clean}.pdf" if clean else ""


def load_existing_dedup(csv_paths: list[str]) -> tuple[set[str], set[str]]:
    """DOIs + normalized titles already present in one or more DSpace CSV
    exports/corpora, for filtering out papers the repository already has."""
    dois: set[str] = set()
    titles: set[str] = set()
    for csv_path in csv_paths:
        if not csv_path or not os.path.isfile(csv_path):
            continue
        with open(csv_path, "r", encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                doi = normalize_doi(row.get("dc.identifier.doi") or row.get("dc.identifier.doi[]") or "")
                if doi:
                    dois.add(doi)
                title = normalize_title(row.get("dc.title[en]") or "")
                if title:
                    titles.add(title)
    return dois, titles


def dedup_rows(rows: list[dict], existing_dois: set[str], existing_titles: set[str]) -> tuple[list[dict], dict]:
    """Filter rows against an existing-DOI/title set AND against duplicates
    within the batch itself. Returns (kept_rows, stats)."""
    kept = []
    stats = {"input": len(rows), "dup_existing_doi": 0, "dup_existing_title": 0, "dup_within_batch": 0, "kept": 0}
    seen_dois = set(existing_dois)
    seen_titles = set(existing_titles)

    for row in rows:
        doi = normalize_doi(row.get("dc.identifier.doi", ""))
        title = normalize_title(row.get("dc.title[en]", "") or row.get("dc.title[ru]", ""))

        if doi and doi in seen_dois:
            stats["dup_existing_doi"] += 1
            continue
        if not doi and title and title in seen_titles:
            stats["dup_existing_title"] += 1
            continue

        kept.append(row)
        if doi:
            seen_dois.add(doi)
        if title:
            seen_titles.add(title)

    stats["kept"] = len(kept)
    return kept, stats


def write_csv(rows: list[dict], output_path: str) -> dict:
    if not rows:
        return {"status": "error", "reason": "no rows to write"}
    os.makedirs(os.path.dirname(os.path.abspath(output_path)) or ".", exist_ok=True)
    with open(output_path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    oa_map = {r["filename"]: r.get("_oa_url", "") for r in rows if r.get("filename")}
    return {"status": "ok", "csv_path": output_path, "row_count": len(rows), "oa_url_map": oa_map}
