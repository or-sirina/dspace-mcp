"""Provider-agnostic LLM metadata enrichment: clean/translate titles &
abstracts, extract keywords, build citations -- using PDF first/last-page
text as grounding context. Generalized port of enrich_metadata.py (which
hardcoded DeepSeek); provider/model/key now come from Profile.api.

Checkpointing (resumable) and a token-bucket rate limiter are preserved
from the original since enrichment runs can be large (thousands of rows)
and API calls are the bottleneck.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path

import aiohttp

try:
    import pdfplumber
except ImportError:  # pragma: no cover - optional at import time
    pdfplumber = None

SYSTEM_PROMPT = """You are a metadata librarian for a DSpace repository. Your task is to clean and enrich article metadata.

RULES:
1. Author names: EN format "Lastname, F. M." (with spaces between initials), RU format "Фамилия, И. О." (with spaces). Multiple authors separated by "||". If you see "First Last" convert to "Last, F.". If you see garbled/incorrect names, fix them using context from the PDF.
2. Titles: Sentence case (not ALL CAPS). First word capitalized, rest lowercase except proper nouns and acronyms.
3. If dc.title[en] exists but dc.title[ru] is empty -> translate to Russian.
4. If dc.title[ru] exists but dc.title[en] is empty -> translate to English.
5. Same logic for dc.description.abstract -- translate missing language version.
6. dc.subject[en]: Extract 5-10 keywords from the abstract, separated by "||". No trailing punctuation.
7. dc.subject[ru]: Russian translation of the same keywords, separated by "||".
8. dc.identifier.citation[en]: Format "Lastname F. Title. Journal. Year;Vol(Issue):pp. https://doi.org/DOI"
9. dc.identifier.citation[ru]: Same format but with Russian journal name and author.
10. dc.language.iso: ISO 639-1 code (en, ru, de, fr, etc.)
11. If PDF text is provided, use it to verify/fix metadata and extract missing information.
12. Do NOT invent data. If information is genuinely missing, leave the field empty.
13. Remove HTML entities, control characters, extra whitespace.

INPUT: You will receive existing metadata as key:value pairs, followed by PDF text if available.
OUTPUT: Return ONLY the corrected/added fields as key:value pairs, one per line. Only include fields that changed or were newly generated."""

_FIELDS = [
    "dc.title[en]", "dc.title[ru]",
    "dc.contributor.author[en]", "dc.contributor.author[ru]",
    "dc.description.abstract[en]", "dc.description.abstract[ru]",
    "dc.identifier.doi", "dc.subject[en]", "dc.subject[ru]",
    "dc.language.iso", "dc.identifier.citation[en]", "dc.identifier.citation[ru]",
]
_ABSTRACT_TRUNCATE = 500
_PDF_TRUNCATE_CHARS = 8000


def extract_pdf_text(pdf_path: str) -> str:
    """First 2 + last 2 pages (1+1 if <4 pages) -- enough context for
    title/author/abstract verification without spending the whole budget
    on a full-text dump."""
    if pdfplumber is None or not os.path.isfile(pdf_path):
        return ""
    try:
        with pdfplumber.open(pdf_path) as pdf:
            n = len(pdf.pages)
            if n == 0:
                return ""
            indices = sorted(set(i for i in ([0, n - 1] if n < 4 else [0, 1, n - 2, n - 1]) if 0 <= i < n))
            text = "\n".join(pdf.pages[i].extract_text() or "" for i in indices)
            return text[:_PDF_TRUNCATE_CHARS]
    except Exception:  # noqa: BLE001
        return ""


def _build_user_message(row: dict, pdf_text: str) -> str:
    lines = ["EXISTING METADATA:"]
    for key in _FIELDS:
        val = (row.get(key) or "").strip()
        if "abstract" in key and len(val) > _ABSTRACT_TRUNCATE:
            val = val[:_ABSTRACT_TRUNCATE] + "..."
        lines.append(f"{key}: {val}")
    if pdf_text:
        lines += ["", "PDF TEXT:", pdf_text]
    return "\n".join(lines)


def _parse_response(text: str) -> dict:
    result = {}
    for line in text.strip().splitlines():
        line = line.strip()
        if not line or ":" not in line:
            continue
        key, _, value = line.partition(":")
        key, value = key.strip(), value.strip()
        if key and value:
            result[key] = value
    return result


class _RateLimiter:
    def __init__(self, rate: float):
        self.interval = 1.0 / rate
        self._lock = asyncio.Lock()
        self._last = 0.0

    async def acquire(self):
        async with self._lock:
            elapsed = time.monotonic() - self._last
            if elapsed < self.interval:
                await asyncio.sleep(self.interval - elapsed)
            self._last = time.monotonic()


async def _call_llm(
    session: aiohttp.ClientSession, semaphore: asyncio.Semaphore, limiter: _RateLimiter,
    base_url: str, api_key: str, model: str, user_message: str, max_retries: int = 3,
) -> dict:
    payload = {
        "model": model,
        "messages": [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": user_message}],
        "temperature": 0.1,
        "max_tokens": 4096,
    }
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    url = base_url.rstrip("/") + "/chat/completions"

    for attempt in range(1, max_retries + 1):
        try:
            async with semaphore:
                await limiter.acquire()
                async with session.post(url, json=payload, headers=headers, timeout=aiohttp.ClientTimeout(total=120)) as resp:
                    if resp.status == 429:
                        await asyncio.sleep(2 ** attempt)
                        continue
                    if resp.status != 200:
                        if attempt < max_retries:
                            await asyncio.sleep(2 ** attempt)
                            continue
                        return {}
                    data = await resp.json()
                    return _parse_response(data["choices"][0]["message"]["content"])
        except Exception:  # noqa: BLE001
            if attempt < max_retries:
                await asyncio.sleep(2 ** attempt)
    return {}


async def _enrich_async(
    rows: list[dict], pdf_dir: str, base_url: str, api_key: str, model: str,
    concurrency: int, rate_per_sec: float, checkpoint_path: str | None,
) -> list[dict]:
    checkpoint = {"processed_indices": [], "updated_rows": {}}
    if checkpoint_path and os.path.isfile(checkpoint_path):
        try:
            checkpoint = json.loads(Path(checkpoint_path).read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            pass
    done = set(checkpoint["processed_indices"])

    semaphore = asyncio.Semaphore(concurrency)
    limiter = _RateLimiter(rate_per_sec)

    async with aiohttp.ClientSession() as session:
        async def process(i: int, row: dict):
            if i in done:
                return
            pdf_name = (row.get("filename") or "").strip()
            pdf_text = extract_pdf_text(os.path.join(pdf_dir, pdf_name)) if pdf_dir and pdf_name else ""
            updates = await _call_llm(session, semaphore, limiter, base_url, api_key, model, _build_user_message(row, pdf_text))
            checkpoint["updated_rows"][str(i)] = updates
            checkpoint["processed_indices"].append(i)
            if checkpoint_path and len(checkpoint["processed_indices"]) % 50 == 0:
                Path(checkpoint_path).write_text(json.dumps(checkpoint, ensure_ascii=False), encoding="utf-8")

        await asyncio.gather(*(process(i, r) for i, r in enumerate(rows)))

    if checkpoint_path:
        Path(checkpoint_path).write_text(json.dumps(checkpoint, ensure_ascii=False), encoding="utf-8")

    enriched = []
    for i, row in enumerate(rows):
        merged = dict(row)
        merged.update(checkpoint["updated_rows"].get(str(i), {}))
        enriched.append(merged)
    return enriched


def enrich_metadata_llm(
    rows: list[dict],
    pdf_dir: str,
    base_url: str,
    api_key: str,
    model: str,
    concurrency: int = 10,
    rate_per_sec: float = 10.0,
    checkpoint_path: str | None = None,
) -> dict:
    """Enrich `rows` (see scholar/common.py schema) in place: fills missing
    ru/en title+abstract translations, extracts subject keywords, builds
    citations, using each row's PDF (pdf_dir/<filename>) as grounding
    context when present. Resumable via checkpoint_path."""
    if not api_key:
        return {"status": "error", "reason": "no LLM api_key configured (profile.api.llm_api_key)"}
    enriched = asyncio.run(
        _enrich_async(rows, pdf_dir, base_url, api_key, model, concurrency, rate_per_sec, checkpoint_path)
    )
    return {"status": "ok", "row_count": len(enriched), "rows": enriched}
