"""Строит volume.csv и articles.csv (формат saf_build_from_csv) из готовых SAF-элементов.

Источник: каталог с saf_volume/ и saf_articles/ (аргумент 1 или env SEV2026_SRC).
Колонки: schema.element[.qualifier][[lang]]; qualifier="none" в исходном XML
опускается (saf.py сам запишет qualifier="none"), что даёт точный round-trip.
PDF в CSV не попадают - только имена файлов в колонке filename__bundle:ORIGINAL.
"""
import csv
import os
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

DEFAULT_SRC = "/home/orsirina/Разработка/Репозиторий/xml_to_saf/deposits/severnaya_evropa_2026"
HERE = Path(__file__).resolve().parent
VOLUME_COLLECTION = "123456789/8577"
ARTICLES_COLLECTION = "123456789/99999"  # ФИКТИВНЫЙ handle: реальную коллекцию ещё предстоит создать


def read_values(item: Path):
    """Возвращает список (schema, element, qualifier|None, lang|None, value) в порядке файлов."""
    out = []
    for xml in sorted(item.glob("*.xml")):
        root = ET.parse(xml).getroot()
        schema = root.get("schema", "dc")
        for v in root.iter("dcvalue"):
            q = v.get("qualifier")
            q = None if q in (None, "", "none") else q
            out.append((schema, v.get("element"), q, v.get("language"), (v.text or "").strip()))
    return out


def read_contents(item: Path):
    files = []
    for line in (item / "contents").read_text(encoding="utf-8").splitlines():
        if line.strip():
            parts = line.split("\t")
            files.append((parts[0], parts[1] if len(parts) > 1 else ""))
    return files


def col_name(schema, el, q, lang):
    return f"{schema}.{el}" + (f".{q}" if q else "") + (f"[{lang}]" if lang else "")


def build_rows(items, collection):
    rows, columns = [], []
    for item in items:
        row = {"collection": collection}
        for s, e, q, l, val in read_values(item):
            c = col_name(s, e, q, l)
            if val == "":
                continue
            if "||" in val:
                raise ValueError(f"'||' в значении: {item.name} {c}")
            row[c] = row[c] + "||" + val if c in row else val
            if c not in columns:
                columns.append(c)
        contents = read_contents(item)
        bundles = {b for _, b in contents}
        if bundles != {"bundle:ORIGINAL"}:
            raise ValueError(f"неожиданные параметры contents в {item}: {bundles}")
        row["filename__bundle:ORIGINAL"] = "||".join(f for f, _ in contents)
        rows.append(row)
    return rows, ["filename__bundle:ORIGINAL"] + columns + ["collection"]


def write_csv(path, rows, header):
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=header, restval="")
        w.writeheader()
        w.writerows(rows)


def main():
    src = Path(sys.argv[1] if len(sys.argv) > 1 else os.environ.get("SEV2026_SRC", DEFAULT_SRC))
    for name, sub, coll in (("volume.csv", "saf_volume", VOLUME_COLLECTION),
                            ("articles.csv", "saf_articles", ARTICLES_COLLECTION)):
        items = sorted(p for p in (src / sub).iterdir() if p.is_dir() and p.name.startswith("item_"))
        rows, header = build_rows(items, coll)
        names = [f for r in rows for f in r["filename__bundle:ORIGINAL"].split("||")]
        assert len(names) == len(set(names)), "имена PDF не уникальны"
        write_csv(HERE / name, rows, header)
        print(f"{name}: {len(rows)} строк, {len(header)} колонок, collection={coll}")


if __name__ == "__main__":
    main()
