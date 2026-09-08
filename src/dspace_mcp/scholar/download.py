"""Open-access PDF download: direct OA URL -> Unpaywall -> DOI redirect,
each validated by content-type/magic-bytes/size before being accepted.
Generalized port of download_pdfs_ext.py.

Unpaywall (https://unpaywall.org, api.unpaywall.org/v2/{doi}?email=...) is
the load-bearing source here per the user's explicit ask -- it is tried
whenever a DOI is present and the direct OA URL (if any) didn't pan out.
"""

from __future__ import annotations

import os
import time

import requests

_MIN_PDF_BYTES = 10_000
_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"


def _is_ssrn_url(url: str) -> bool:
    u = (url or "").lower()
    return "ssrn.com" in u or "ssrn." in u


def _looks_like_pdf(resp: requests.Response) -> bool:
    ct = resp.headers.get("content-type", "")
    return "pdf" in ct or resp.url.endswith(".pdf") or resp.content[:4] == b"%PDF"


def _download(url: str, session: requests.Session) -> bytes | None:
    try:
        resp = session.get(url, timeout=30, allow_redirects=True)
        if resp.status_code == 200 and _looks_like_pdf(resp) and len(resp.content) > _MIN_PDF_BYTES:
            return resp.content
    except Exception:  # noqa: BLE001
        pass
    return None


def try_oa_url(oa_url: str, session: requests.Session) -> bytes | None:
    if not oa_url or _is_ssrn_url(oa_url):
        return None
    return _download(oa_url, session)


def try_unpaywall(doi: str, email: str, session: requests.Session) -> bytes | None:
    if not doi or not email:
        return None
    try:
        resp = session.get(f"https://api.unpaywall.org/v2/{doi}", params={"email": email}, timeout=15)
        if resp.status_code != 200:
            return None
        best = (resp.json() or {}).get("best_oa_location") or {}
        pdf_url = best.get("url_for_pdf") or best.get("url")
        if not pdf_url or _is_ssrn_url(pdf_url):
            return None
        return _download(pdf_url, session)
    except Exception:  # noqa: BLE001
        return None


def try_doi_redirect(doi: str, session: requests.Session) -> bytes | None:
    if not doi:
        return None
    try:
        resp = session.head(f"https://doi.org/{doi}", timeout=10, allow_redirects=True)
        if "pdf" in resp.headers.get("content-type", ""):
            return _download(resp.url, session)
    except Exception:  # noqa: BLE001
        pass
    return None


def download_oa_pdfs(
    rows: list[dict],
    output_dir: str,
    unpaywall_email: str,
    resume: bool = True,
    delay: float = 0.5,
    limit: int = 0,
) -> dict:
    """rows: harvested paper dicts with `filename`, `dc.identifier.doi`, and
    optionally `_oa_url` (see scholar/harvest.py / common.py). Writes one PDF
    per row to output_dir/<filename> using, in order: the row's OA URL ->
    Unpaywall -> DOI content-negotiated redirect. SSRN URLs are skipped
    outright (Cloudflare blocks server-side downloads there).
    """
    os.makedirs(output_dir, exist_ok=True)
    session = requests.Session()
    session.headers.update({"User-Agent": _UA})

    queue = []
    ssrn_skipped = 0
    for row in rows:
        filename = (row.get("filename") or "").strip()
        if not filename:
            continue
        pdf_path = os.path.join(output_dir, filename)
        if resume and os.path.exists(pdf_path):
            continue
        doi = (row.get("dc.identifier.doi") or "").strip()
        oa_url = (row.get("_oa_url") or "").strip()
        if _is_ssrn_url(oa_url) or _is_ssrn_url(doi):
            ssrn_skipped += 1
            continue
        queue.append({"filename": filename, "doi": doi, "oa_url": oa_url})

    if limit:
        queue = queue[:limit]

    results = {"downloaded": [], "failed": []}
    for item in queue:
        pdf_path = os.path.join(output_dir, item["filename"])
        data = (
            try_oa_url(item["oa_url"], session)
            or try_unpaywall(item["doi"], unpaywall_email, session)
            or try_doi_redirect(item["doi"], session)
        )
        if data:
            with open(pdf_path, "wb") as f:
                f.write(data)
            results["downloaded"].append(item["filename"])
        else:
            results["failed"].append({"filename": item["filename"], "doi": item["doi"]})
        time.sleep(delay)

    return {
        "status": "ok",
        "attempted": len(queue),
        "downloaded_count": len(results["downloaded"]),
        "failed_count": len(results["failed"]),
        "ssrn_skipped": ssrn_skipped,
        "failed": results["failed"],
        "output_dir": output_dir,
    }
