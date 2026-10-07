"""Сквозной прогон книги «Страны Северной Европы и Балтии» (2026) через MCP-сервер по stdio.

Запуск из корня репозитория:
    uv run python examples/book_severnaya_evropa_2026/run_example.py
Источник PDF и эталонного SAF: env SEV2026_SRC (по умолчанию как в make_manifests.py).
Профиль demo (демо-конфиг, недостижимый хост): ничего не меняется на сервере,
ни один вызов не передаёт confirm=True.
"""
import asyncio
import json
import os
import shutil
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
WORK = HERE / "work"
SRC = Path(os.environ.get("SEV2026_SRC", "/home/orsirina/Разработка/Репозиторий/xml_to_saf/deposits/severnaya_evropa_2026"))
CONFIG = REPO / "demo" / "opencode" / "config.demo.toml"
PROFILE = "demo"
VOLUME_COLL = "123456789/8577"
ARTICLES_COLL = "123456789/99999"  # фиктивный handle
PARENT_COMMUNITY = "123456789/136"
COLL_NAME = "II Межвузовская студенческая научно-практическая конференция «Страны Северной Европы и Балтии»"


def scrub(text: str) -> str:
    for p, repl in ((str(WORK), "<work>"), (str(REPO), "<repo>"), (str(SRC), "<SRC>"), (str(Path.home()), "~")):
        text = text.replace(p, repl)
    return text


def show(title, obj, limit=1800):
    s = scrub(json.dumps(obj, ensure_ascii=False, separators=(",", ":")))
    print(f"\n=== {title} ===")
    print(s if len(s) <= limit else s[:limit] + f"... [+{len(s) - limit} симв.]")


async def call(s, tool, args):
    assert args.get("confirm") is not True, "confirm=True запрещён в этом примере"
    res = await s.call_tool(tool, args)
    text = res.content[0].text if res.content else ""
    try:
        return json.loads(text)
    except ValueError:
        return {"raw": text}


def stage():
    if WORK.exists():
        shutil.rmtree(WORK)
    WORK.mkdir()
    for name in ("volume.csv", "articles.csv"):
        shutil.copy2(HERE / name, WORK / name)
    n = 0
    for sub in ("saf_volume", "saf_articles"):
        for pdf in sorted((SRC / sub).glob("item_*/*.pdf")):
            os.symlink(pdf, WORK / pdf.name)
            n += 1
    return n


def norm_xml(path: Path):
    root = ET.parse(path).getroot()
    out = set()
    for v in root.iter("dcvalue"):
        q = v.get("qualifier")
        q = None if q in (None, "", "none") else q
        out.add((root.get("schema", "dc"), v.get("element"), q, v.get("language"), (v.text or "").strip()))
    return out


def norm_contents(path: Path):
    return {tuple(l.split("\t")) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()}


def compare(orig_dir: Path, new_dir: Path):
    """Возвращает (число совпавших, число элементов, список различий)."""
    items = sorted(p.name for p in orig_dir.iterdir() if p.is_dir() and p.name.startswith("item_"))
    same, diffs = 0, []
    for it in items:
        o, n = orig_dir / it, new_dir / it
        bad = []
        for fn in sorted({f.name for f in o.glob("*.xml")} | {f.name for f in n.glob("*.xml")}):
            if not (o / fn).exists() or not (n / fn).exists():
                bad.append(f"{fn}: файл есть только с одной стороны")
                continue
            a, b = norm_xml(o / fn), norm_xml(n / fn)
            if a != b:
                bad.append(f"{fn}: -{sorted(a - b)[:2]} +{sorted(b - a)[:2]}")
        if norm_contents(o / "contents") != norm_contents(n / "contents"):
            bad.append("contents отличается")
        if bad:
            diffs.append({"item": it, "diff": bad})
        else:
            same += 1
    return same, len(items), diffs


async def main() -> int:
    npdf = stage()
    print(f"Рабочий каталог подготовлен: <work>, PDF (симлинки): {npdf}")
    params = StdioServerParameters(
        command="uv",
        args=["--directory", str(REPO), "run", "dspace-mcp"],
        env={**os.environ, "DSPACE_MCP_CONFIG": str(CONFIG), "DSPACE_MCP_PROFILE": PROFILE},
    )
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()

            v = await call(s, "saf_build_from_csv", {"csv_path": str(WORK / "volume.csv"), "output_name": "saf_volume"})
            v["unused_files"] = f"{len(v['unused_files'])} файлов (PDF статей лежат в той же папке, том их не использует)"
            show("Шаг 1. saf_build_from_csv: volume.csv -> saf_volume", v)
            a = await call(s, "saf_build_from_csv", {"csv_path": str(WORK / "articles.csv"), "output_name": "saf_articles"})
            show("Шаг 2. saf_build_from_csv: articles.csv -> saf_articles", a)

            for name in ("saf_volume", "saf_articles"):
                show(f"Шаг 3. saf_validate: {name}", await call(s, "saf_validate", {"saf_dir": str(WORK / name)}))

            print("\n=== Шаг 4. Сравнение с эталоном (нормализованный XML: множество (schema,element,qualifier,language,value)) ===")
            for name in ("saf_volume", "saf_articles"):
                same, total, diffs = compare(SRC / name, WORK / name)
                show(f"{name}: идентичных {same}/{total}", {"diffs": diffs[:10], "diff_count": len(diffs)})

            show("Шаг 5. collection_create (confirm=False, allow_db_write=False)",
                 await call(s, "collection_create", {"profile": PROFILE, "parent_community_handle": PARENT_COMMUNITY,
                                                      "name": COLL_NAME, "confirm": False, "allow_db_write": False}))

            show("Шаг 6a. saf_import статей (confirm=False)",
                 await call(s, "saf_import", {"profile": PROFILE, "local_saf_dir": str(WORK / "saf_articles"),
                                              "collection_handle": ARTICLES_COLL, "confirm": False}))
            show("Шаг 6b. saf_import тома (confirm=False)",
                 await call(s, "saf_import", {"profile": PROFILE, "local_saf_dir": str(WORK / "saf_volume"),
                                              "collection_handle": VOLUME_COLL, "confirm": False}))

            show("Шаг 7a. filter_media (confirm=False)",
                 await call(s, "filter_media", {"profile": PROFILE, "identifier": ARTICLES_COLL,
                                                "plugin": "PDF Text Extractor", "confirm": False}))
            # index_discovery не имеет confirm и выполняется сразу -> в демо только описываем команду.
            print("\n=== Шаг 7b. index_discovery: пропущен (у инструмента нет confirm, выполняется сразу) ===")
            print(f"Для реального запуска после импорта: dspace index-discovery -i {ARTICLES_COLL}")
    return 0


sys.exit(asyncio.run(main()))
