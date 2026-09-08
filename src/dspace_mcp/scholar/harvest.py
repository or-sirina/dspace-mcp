"""Fetch publications by affiliation from OpenAlex, CrossRef, CORE, and
Semantic Scholar, normalized into the common row schema (common.py).

Generalizes this repo's openalex_to_csv.py (which read a pre-downloaded
JSON dump) into a live OpenAlex API query, plus crossref_fetch.py and
core_fetch.py (already API-driven) and fetch_mgimo.py (Semantic Scholar).
All four share the same row shape so callers can dedup/download/enrich/
build-SAF identically regardless of source.
"""

from __future__ import annotations

import re
import time

import requests

from .common import doi_to_filename, names_to_lastname_initials, normalize_doi

OPENALEX_API = "https://api.openalex.org/works"
CROSSREF_API = "https://api.crossref.org/works"
CORE_API = "https://api.core.ac.uk/v3/search/works"
S2_API = "https://api.semanticscholar.org/graph/v1/paper/search"

_TYPE_MAP = {
    "article": "Article", "journal-article": "Article", "proceedings-article": "Article",
    "preprint": "Preprint", "posted-content": "Preprint",
    "review": "Review", "editorial": "Editorial",
    "book-chapter": "Book Chapter",
    "book": "Book",
    "dataset": "Dataset",
    "letter": "Letter",
}


def _reconstruct_abstract(inv_index: dict | None) -> str:
    if not inv_index:
        return ""
    positions = [(pos, word) for word, poss in inv_index.items() for pos in poss]
    positions.sort()
    return " ".join(w for _, w in positions)


def _openalex_row(paper: dict, collection: str, filename_prefix: str) -> dict:
    doi_raw = (paper.get("doi") or "").strip()
    doi_clean = normalize_doi(doi_raw)
    title = (paper.get("title") or paper.get("display_name") or "").strip()

    authors = []
    for auth in paper.get("authorships", []):
        name = (auth.get("author") or {}).get("display_name", "")
        if name:
            authors.append(names_to_lastname_initials(name))

    loc = (paper.get("primary_location") or {}).get("source") or {}
    issns = loc.get("issn") or []
    oa = paper.get("open_access") or {}
    subjects = [
        c.get("display_name", "") for c in (paper.get("concepts") or [])
        if c.get("level", 0) <= 2 and c.get("score", 0) > 0.3
    ][:10]

    filename = doi_to_filename(doi_clean, filename_prefix) or f"{filename_prefix}_{(paper.get('id') or '').split('/')[-1]}.pdf"

    return {
        "filename": filename,
        "collection": collection,
        "dc.title[en]": title,
        "dc.contributor.author[en]": "||".join(authors),
        "dc.date.issued": str(paper.get("publication_year") or ""),
        "dc.description.abstract[en]": _reconstruct_abstract(paper.get("abstract_inverted_index")),
        "dc.identifier.doi": doi_clean,
        "dc.identifier.issn[online]": issns[0] if issns else (loc.get("issn_l") or ""),
        "dc.identifier.issn[print]": issns[1] if len(issns) > 1 else "",
        "dc.identifier.uri": doi_raw or paper.get("id", ""),
        "dc.language.iso": "en",
        "dc.subject[en]": "||".join(subjects),
        "dc.type": _TYPE_MAP.get((paper.get("type") or "article").lower(), "Article"),
        "dc.source[en]": loc.get("display_name") or "",
        "dc.rights": "openAccess" if oa.get("is_oa") else "",
        "article.journalname[en]": loc.get("display_name") or "",
        "publication.article.doi": doi_clean,
        "_oa_url": (oa.get("oa_url") or (paper.get("primary_location") or {}).get("pdf_url") or "").strip(),
        "_source_id": paper.get("id", ""),
        "_source": "openalex",
    }


def fetch_openalex(
    affiliation: str, mailto: str = "", max_results: int = 2000, per_page: int = 200, filename_prefix: str = "ext"
) -> list[dict]:
    """Live OpenAlex Works API query by raw affiliation string, cursor-paginated."""
    rows: list[dict] = []
    cursor = "*"
    headers = {"User-Agent": f"dspace-mcp/1.0 (mailto:{mailto})"} if mailto else {}
    params_base = {
        "filter": f"raw_affiliation_strings.search:{affiliation}",
        "per-page": min(per_page, 200),
    }
    if mailto:
        params_base["mailto"] = mailto

    while len(rows) < max_results:
        params = {**params_base, "cursor": cursor}
        resp = requests.get(OPENALEX_API, params=params, headers=headers, timeout=60)
        resp.raise_for_status()
        data = resp.json()
        results = data.get("results", [])
        if not results:
            break
        rows.extend(_openalex_row(p, "", filename_prefix) for p in results)
        cursor = (data.get("meta") or {}).get("next_cursor")
        if not cursor:
            break
        time.sleep(0.1)

    return rows[:max_results]


def _crossref_row(item: dict, collection: str, filename_prefix: str) -> dict:
    doi = (item.get("DOI") or "").strip()
    titles = item.get("title") or []
    title = titles[0] if titles else ""

    authors = []
    for auth in item.get("author", []):
        family, given = auth.get("family", ""), auth.get("given", "")
        if family:
            initials = " ".join(f"{g[0]}." for g in given.split() if g)
            authors.append(f"{family}, {initials}" if initials else family)

    year = ""
    for date_field in ("published-print", "published-online", "created"):
        dp = (item.get(date_field) or {}).get("date-parts", [[]])
        if dp and dp[0] and dp[0][0]:
            year = str(dp[0][0])
            break

    abstract = re.sub(r"<[^>]+>", "", (item.get("abstract") or "").strip())
    container = item.get("container-title") or []
    issns = item.get("ISSN") or []
    page = item.get("page", "") or ""
    fpage, _, lpage = page.partition("-")

    return {
        "filename": doi_to_filename(doi, filename_prefix),
        "collection": collection,
        "dc.title[en]": title,
        "dc.contributor.author[en]": "||".join(authors),
        "dc.date.issued": year,
        "dc.description.abstract[en]": abstract,
        "dc.identifier.doi": doi,
        "dc.identifier.issn[online]": issns[0] if issns else "",
        "dc.identifier.issn[print]": issns[1] if len(issns) > 1 else "",
        "dc.identifier.uri": f"https://doi.org/{doi}" if doi else "",
        "dc.language.iso": "en",
        "dc.type": _TYPE_MAP.get((item.get("type") or "journal-article").lower(), "Article"),
        "dc.source[en]": container[0] if container else "",
        "article.fpage": fpage.strip(),
        "article.lpage": lpage.strip(),
        "article.volume": item.get("volume", "") or "",
        "article.issue": item.get("issue", "") or "",
        "article.journalname[en]": container[0] if container else "",
        "publication.article.doi": doi,
        "_oa_url": "",
        "_source_id": doi,
        "_source": "crossref",
    }


def fetch_crossref(
    affiliation: str, mailto: str, max_results: int = 5000, rows_per_request: int = 1000, filename_prefix: str = "ext"
) -> list[dict]:
    rows: list[dict] = []
    offset = 0
    while offset < max_results:
        params = {"query.affiliation": affiliation, "rows": rows_per_request, "offset": offset, "mailto": mailto}
        resp = requests.get(CROSSREF_API, params=params, timeout=60)
        resp.raise_for_status()
        items = (resp.json().get("message") or {}).get("items", [])
        if not items:
            break
        rows.extend(_crossref_row(it, "", filename_prefix) for it in items)
        offset += rows_per_request
        if len(items) < rows_per_request:
            break
        time.sleep(1.0)
    return rows[:max_results]


def _core_row(item: dict, collection: str, filename_prefix: str) -> dict:
    doi = (item.get("doi") or "").strip()
    title = (item.get("title") or "").strip()
    authors = [names_to_lastname_initials(a.get("name", "")) for a in item.get("authors", []) if a.get("name")]

    year = ""
    pub_date = item.get("publishedDate") or item.get("year") or ""
    m = re.search(r"(\d{4})", str(pub_date))
    if m:
        year = m.group(1)

    oa_url = (item.get("downloadUrl") or item.get("fullTextUrl") or "").strip()
    lang = ((item.get("language") or {}).get("code") or "en").strip()

    return {
        "filename": doi_to_filename(doi, filename_prefix) or f"{filename_prefix}_core_{item.get('id', 'unknown')}.pdf",
        "collection": collection,
        "dc.title[en]" if lang != "ru" else "dc.title[ru]": title,
        "dc.contributor.author[en]": "||".join(authors),
        "dc.date.issued": year,
        "dc.description.abstract[en]" if lang != "ru" else "dc.description.abstract[ru]": (item.get("abstract") or "").strip(),
        "dc.identifier.doi": doi,
        "dc.identifier.uri": f"https://doi.org/{doi}" if doi else oa_url,
        "dc.language.iso": lang,
        "dc.type": "Article",
        "dc.rights": "openAccess" if oa_url else "",
        "publication.article.doi": doi,
        "_oa_url": oa_url,
        "_source_id": str(item.get("id", "")),
        "_source": "core",
    }


def fetch_core(
    affiliation: str, api_key: str, max_results: int = 1000, page_size: int = 100, filename_prefix: str = "ext"
) -> list[dict]:
    rows: list[dict] = []
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    offset = 0
    query = f'"{affiliation}"'
    while offset < max_results:
        params = {"q": query, "offset": offset, "limit": page_size}
        resp = requests.get(CORE_API, params=params, headers=headers, timeout=60)
        resp.raise_for_status()
        results = resp.json().get("results", [])
        if not results:
            break
        rows.extend(_core_row(it, "", filename_prefix) for it in results)
        offset += page_size
        if len(results) < page_size:
            break
        time.sleep(1.0)
    return rows[:max_results]


def fetch_semanticscholar(
    affiliation_query: str, api_key: str = "", max_results: int = 1000,
    year_from: int = 2000, year_to: int = 2100, filename_prefix: str = "ext",
) -> list[dict]:
    """Semantic Scholar's graph search does not filter by affiliation
    directly -- this searches by query string (e.g. an institution name) and
    should be treated as lower-precision than the other three sources;
    dedup against them before trusting results wholesale."""
    rows: list[dict] = []
    headers = {"x-api-key": api_key} if api_key else {}
    offset = 0
    limit = 100
    fields = "title,abstract,year,externalIds,authors,venue,openAccessPdf"

    while offset < max_results:
        params = {
            "query": affiliation_query, "offset": offset, "limit": limit, "fields": fields,
            "year": f"{year_from}-{year_to}",
        }
        resp = requests.get(S2_API, params=params, headers=headers, timeout=30)
        if resp.status_code in (429, 403):
            time.sleep(2.0)
            continue
        resp.raise_for_status()
        data = resp.json()
        papers = data.get("data", [])
        if not papers:
            break
        for p in papers:
            doi = normalize_doi((p.get("externalIds") or {}).get("DOI", ""))
            authors = [names_to_lastname_initials(a.get("name", "")) for a in p.get("authors", []) if a.get("name")]
            oa = p.get("openAccessPdf") or {}
            rows.append({
                "filename": doi_to_filename(doi, filename_prefix) or f"{filename_prefix}_s2_{p.get('paperId', '')}.pdf",
                "collection": "",
                "dc.title[en]": p.get("title") or "",
                "dc.contributor.author[en]": "||".join(authors),
                "dc.date.issued": str(p.get("year") or ""),
                "dc.description.abstract[en]": p.get("abstract") or "",
                "dc.identifier.doi": doi,
                "dc.identifier.uri": f"https://doi.org/{doi}" if doi else "",
                "dc.language.iso": "en",
                "dc.type": "Article",
                "dc.source[en]": p.get("venue") or "",
                "dc.rights": "openAccess" if oa.get("url") else "",
                "article.journalname[en]": p.get("venue") or "",
                "publication.article.doi": doi,
                "_oa_url": oa.get("url", ""),
                "_source_id": p.get("paperId", ""),
                "_source": "semanticscholar",
            })
        offset += limit
        if len(papers) < limit:
            break
        time.sleep(1.0)

    return rows[:max_results]


def harvest_by_affiliation(
    affiliation: str,
    sources: list[str],
    collection: str = "",
    mailto: str = "",
    core_api_key: str = "",
    s2_api_key: str = "",
    max_results_per_source: int = 1000,
    filename_prefix: str = "ext",
) -> dict:
    """Unified entry point: fetch from any subset of
    {openalex, crossref, core, semanticscholar}, tag each row's `collection`,
    and return them grouped by source plus a combined deduplicated-within-batch
    list (by DOI, keeping the first occurrence -- source order as passed in
    `sources` decides precedence). `filename_prefix` namespaces the generated
    PDF filenames (e.g. an institution short name) so multiple institutions'
    harvests don't collide if run against a shared pdfs/ directory; defaults
    to the generic "ext" (external)."""
    from .common import dedup_rows

    by_source: dict[str, list[dict]] = {}
    errors: dict[str, str] = {}

    for source in sources:
        try:
            if source == "openalex":
                rows = fetch_openalex(affiliation, mailto, max_results_per_source, filename_prefix=filename_prefix)
            elif source == "crossref":
                rows = fetch_crossref(affiliation, mailto, max_results_per_source, filename_prefix=filename_prefix)
            elif source == "core":
                rows = fetch_core(affiliation, core_api_key, max_results_per_source, filename_prefix=filename_prefix)
            elif source == "semanticscholar":
                rows = fetch_semanticscholar(affiliation, s2_api_key, max_results_per_source, filename_prefix=filename_prefix)
            else:
                errors[source] = f"unknown source '{source}'"
                continue
            for r in rows:
                r["collection"] = collection
            by_source[source] = rows
        except Exception as exc:  # noqa: BLE001
            errors[source] = f"{type(exc).__name__}: {exc}"
            by_source[source] = []

    combined: list[dict] = []
    for source in sources:
        combined.extend(by_source.get(source, []))
    deduped, stats = dedup_rows(combined, set(), set())

    return {
        "status": "ok",
        "by_source_counts": {k: len(v) for k, v in by_source.items()},
        "errors": errors,
        "combined_rows": deduped,
        "dedup_stats": stats,
    }
