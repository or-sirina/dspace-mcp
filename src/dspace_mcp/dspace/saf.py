"""SAF (Simple Archive Format) package construction and repair.

The CSV -> SAF builder is a structured port of this repo's safbuilder.py
(itself a from-scratch reimplementation of the DSpace-labs Java SAFBuilder),
generalized to return a structured result instead of printing to stdout.
See https://wiki.lyrasis.org/display/DSDOC6x/Importing+and+Exporting+Items+via+Simple+Archive+Format
for the on-disk format this produces:

    item_NNN/
        dublin_core.xml           # dc.* fields
        metadata_<schema>.xml     # any other schema, e.g. local.*, article.*
        contents                  # bitstream filenames + bundle/permission params
        collections               # extra (non-owning) collection handles

CSV column conventions (unchanged from the existing tooling / DSpace batch
import): `schema.element[.qualifier][[lang]]`, multi-values joined with
`||`, `filename` / `filename__bundle:X__permissions:Y` for bitstreams,
`collection` for the owning (+ mapped) collection handle(s).
"""

from __future__ import annotations

import csv
import os
import re
import shutil
import zipfile
from dataclasses import dataclass, field

import chardet

_DC_COLUMN_RE = re.compile(
    r'^([a-zA-Z0-9_-]+)\.([a-zA-Z0-9_-]+)(?:\.([a-zA-Z0-9_-]+))?(?:\[([a-zA-Z0-9_-]+)\])?$'
)

_INVALID_XML_RE = re.compile(
    "[\\x00-\\x08\\x0b\\x0c\\x0e-\\x1f\\x7f-\\x84\\x86-\\x9f"
    "\\ufdd0-\\ufdef\\ufffe\\uffff"
    "\\ud800-\\udfff]"
)
_BARE_AMP_RE = re.compile(r'&(?!amp;|lt;|gt;|quot;|apos;|#\d+;|#x[0-9a-fA-F]+;)')


def parse_dc_column(col_name: str) -> tuple[str, str, str | None, str | None] | None:
    """Parse a metadata column name -> (schema, element, qualifier, language)."""
    m = _DC_COLUMN_RE.match(col_name)
    if not m:
        return None
    schema, element, qualifier, language = m.groups()
    return (schema, element, qualifier, language)


def escape_xml(text: str) -> str:
    if not text:
        return ""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


def sanitize_xml_text(text: str) -> str:
    """Strip characters that are illegal in XML 1.0 and escape bare '&'.

    Ported from fix_zip_on_server.py -- used both when writing SAF XML
    (defensively, on every dcvalue) and by saf_repair() on existing packages
    that came from external sources (OpenAlex abstracts, scraped HTML) known
    to contain control characters / lone ampersands.
    """
    fixed = _INVALID_XML_RE.sub("", text)
    fixed = _BARE_AMP_RE.sub("&amp;", fixed)
    return fixed


def detect_encoding(filepath: str) -> str:
    with open(filepath, "rb") as f:
        raw = f.read(100_000)
    return chardet.detect(raw).get("encoding") or "utf-8"


@dataclass
class BuildResult:
    saf_dir: str
    item_count: int
    zip_path: str | None = None
    warnings: list[str] = field(default_factory=list)
    unused_files: list[str] = field(default_factory=list)


def _write_dc_xml(path: str, values: list[tuple[str, str | None, str | None, str]]) -> None:
    lines = ['<?xml version="1.0" encoding="UTF-8"?>', "<dublin_core>"]
    for element, qualifier, language, value in values:
        attrs = f' element="{escape_xml(element)}"'
        attrs += f' qualifier="{escape_xml(qualifier)}"' if qualifier else ' qualifier="none"'
        if language:
            attrs += f' language="{escape_xml(language)}"'
        lines.append(f"  <dcvalue{attrs}>{escape_xml(sanitize_xml_text(value))}</dcvalue>")
    lines.append("</dublin_core>\n")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


def _write_schema_xml(path: str, schema: str, values: list[tuple[str, str | None, str | None, str]]) -> None:
    lines = ['<?xml version="1.0" encoding="UTF-8"?>', f'<dublin_core schema="{escape_xml(schema)}">']
    for element, qualifier, language, value in values:
        attrs = f' element="{escape_xml(element)}"'
        attrs += f' qualifier="{escape_xml(qualifier)}"' if qualifier else ' qualifier="none"'
        if language:
            attrs += f' language="{escape_xml(language)}"'
        lines.append(f"  <dcvalue{attrs}>{escape_xml(sanitize_xml_text(value))}</dcvalue>")
    lines.append("</dublin_core>\n")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


def _process_row(row: dict, item_dir: str, content_dir: str, use_symlink: bool, used_files: set) -> list[str]:
    warnings: list[str] = []
    files_for_contents: list[tuple[str, dict]] = []
    dc_values: list[tuple[str, str | None, str | None, str]] = []
    metadata_schemas: dict[str, list] = {}
    collections: list[str] = []

    for col_name, value in row.items():
        col_name = (col_name or "").strip()
        value = (value or "").strip()
        if not value:
            continue

        if col_name == "filename":
            for fname in value.split("||"):
                fname = fname.strip()
                if fname:
                    files_for_contents.append((fname, {}))
                    used_files.add(fname)
            continue

        if col_name.startswith("filename__"):
            _, params = col_name.split("__", 1)
            for fname in value.split("||"):
                fname = fname.strip()
                if not fname:
                    continue
                for existing_fname, existing_params in files_for_contents:
                    if existing_fname == fname:
                        existing_params[params] = True
                        break
                else:
                    files_for_contents.append((fname, {params: True}))
                used_files.add(fname)
            continue

        if col_name in ("collection", "dc.identifier[collection]"):
            collections.extend(h.strip() for h in value.split("||") if h.strip())
            continue

        if col_name == "filegroup":
            continue

        parsed = parse_dc_column(col_name)
        if not parsed:
            continue
        schema, element, qualifier, language = parsed
        for single_value in value.split("||"):
            single_value = single_value.strip()
            if not single_value:
                continue
            if schema == "dc":
                dc_values.append((element, qualifier, language, single_value))
            else:
                metadata_schemas.setdefault(schema, []).append((element, qualifier, language, single_value))

    _write_dc_xml(os.path.join(item_dir, "dublin_core.xml"), dc_values)
    for schema, values in metadata_schemas.items():
        _write_schema_xml(os.path.join(item_dir, f"metadata_{schema}.xml"), schema, values)

    contents_path = os.path.join(item_dir, "contents")
    with open(contents_path, "w", encoding="utf-8") as f:
        for fname, params in files_for_contents:
            src = os.path.join(content_dir, fname)
            dst = os.path.join(item_dir, fname)
            if os.path.isfile(src):
                if use_symlink:
                    if not os.path.exists(dst):
                        os.symlink(os.path.abspath(src), dst)
                else:
                    shutil.copy2(src, dst)
            else:
                warnings.append(f"file '{fname}' not found in {content_dir}")
            parts = [fname]
            if params:
                parts.append("__".join(params))
            f.write("\t".join(parts) + "\n")

    if collections:
        with open(os.path.join(item_dir, "collections"), "w", encoding="utf-8") as f:
            for coll in collections:
                f.write(coll + "\n")

    return warnings


def build_from_csv(
    csv_path: str,
    output_name: str = "SimpleArchiveFormat",
    use_symlink: bool = False,
    export_zip: bool = False,
) -> BuildResult:
    """Read a DSpace-batch-style CSV and build a SAF package next to it.

    Content files (referenced by `filename` columns) are expected in the
    same directory as the CSV -- matches how every existing build_*.py
    script in this repo lays out its working directory.
    """
    if not os.path.isfile(csv_path):
        raise FileNotFoundError(csv_path)

    encoding = detect_encoding(csv_path)
    content_dir = os.path.dirname(os.path.abspath(csv_path))
    saf_dir = os.path.join(content_dir, output_name)

    if os.path.exists(saf_dir):
        shutil.rmtree(saf_dir)
    os.makedirs(saf_dir, exist_ok=True)

    all_files = {
        f for f in os.listdir(content_dir)
        if os.path.isfile(os.path.join(content_dir, f)) and f != os.path.basename(csv_path)
    }
    used_files: set = set()
    all_warnings: list[str] = []
    item_count = 0

    with open(csv_path, "r", encoding=encoding) as f:
        first_char = f.read(1)
        if first_char != "\ufeff":
            f.seek(0)
        reader = csv.DictReader(f)
        for idx, row in enumerate(reader, start=1):
            item_dir = os.path.join(saf_dir, f"item_{idx:03d}")
            os.makedirs(item_dir, exist_ok=True)
            all_warnings.extend(_process_row(row, item_dir, content_dir, use_symlink, used_files))
            item_count += 1

    unused_files = sorted(all_files - used_files - {os.path.basename(csv_path)})

    result = BuildResult(saf_dir=saf_dir, item_count=item_count, warnings=all_warnings, unused_files=unused_files)

    if export_zip:
        zip_path = saf_dir + ".zip"
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for root, _, files in os.walk(saf_dir):
                for file in files:
                    file_path = os.path.join(root, file)
                    arcname = os.path.relpath(file_path, os.path.dirname(saf_dir))
                    zf.write(file_path, arcname)
        result.zip_path = zip_path

    return result


def generate_manifest(csv_path: str) -> dict:
    """Write a template CSV listing every content file in the directory."""
    content_dir = os.path.dirname(os.path.abspath(csv_path))
    files = sorted(
        f for f in os.listdir(content_dir)
        if os.path.isfile(os.path.join(content_dir, f)) and f != os.path.basename(csv_path)
    )
    if not files:
        return {"status": "error", "reason": "no content files found in directory"}

    encoding = detect_encoding(csv_path) if os.path.isfile(csv_path) else "utf-8-sig"
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        if "utf-8-sig" in encoding:
            f.write("﻿")
        writer = csv.writer(f)
        writer.writerow(["filename", "dc.title", "dc.date.issued", "collection"])
        for fname in files:
            writer.writerow([fname, "", "", ""])

    return {"status": "ok", "csv_path": csv_path, "file_count": len(files)}


# ---- SAF validation / repair ------------------------------------------

@dataclass
class ValidationIssue:
    item_dir: str
    problem: str


def validate_saf_dir(saf_dir: str) -> list[ValidationIssue]:
    """Check a built/unpacked SAF directory for common problems:
    missing dublin_core.xml, contents listing a file that isn't present,
    broken symlinks, and item dirs with no metadata at all.
    """
    issues: list[ValidationIssue] = []
    if not os.path.isdir(saf_dir):
        return [ValidationIssue(saf_dir, "not a directory")]

    for entry in sorted(os.listdir(saf_dir)):
        item_dir = os.path.join(saf_dir, entry)
        if not os.path.isdir(item_dir):
            continue

        dc_path = os.path.join(item_dir, "dublin_core.xml")
        has_any_schema = os.path.isfile(dc_path) or any(
            fn.startswith("metadata_") and fn.endswith(".xml") for fn in os.listdir(item_dir)
        )
        if not has_any_schema:
            issues.append(ValidationIssue(entry, "no dublin_core.xml or metadata_<schema>.xml"))

        contents_path = os.path.join(item_dir, "contents")
        if os.path.isfile(contents_path):
            with open(contents_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    fname = line.split("\t")[0]
                    fpath = os.path.join(item_dir, fname)
                    if os.path.islink(fpath) and not os.path.exists(fpath):
                        issues.append(ValidationIssue(entry, f"broken symlink: {fname}"))
                    elif not os.path.exists(fpath):
                        issues.append(ValidationIssue(entry, f"contents references missing file: {fname}"))

    return issues


def repair_saf_dir(saf_dir: str, drop_missing_from_contents: bool = True, resolve_symlinks: bool = True) -> dict:
    """Fix the issues validate_saf_dir() finds, in place:
      - working symlinks are dereferenced into real file copies (symlinks
        commonly don't survive zip/scp transport to the DSpace host intact)
      - broken symlinks / missing files can't be recovered -- reported and
        dropped from `contents` when drop_missing_from_contents is set
      - sanitize XML files (invalid control chars, bare '&') via sanitize_xml_text
    Mirrors fix_saf_on_server.sh / fix_contents_on_server.sh / fix_xml_entities.sh.
    """
    fixed_contents = 0
    fixed_xml = 0
    dereferenced = 0
    unrecoverable: list[str] = []

    for entry in sorted(os.listdir(saf_dir)):
        item_dir = os.path.join(saf_dir, entry)
        if not os.path.isdir(item_dir):
            continue

        contents_path = os.path.join(item_dir, "contents")
        if os.path.isfile(contents_path):
            with open(contents_path, "r", encoding="utf-8") as f:
                lines = f.readlines()
            kept = []
            changed = False
            for line in lines:
                fname = line.strip().split("\t")[0]
                if not fname:
                    continue
                fpath = os.path.join(item_dir, fname)

                if resolve_symlinks and os.path.islink(fpath) and os.path.exists(fpath):
                    target = os.path.realpath(fpath)
                    os.remove(fpath)
                    shutil.copy2(target, fpath)
                    dereferenced += 1

                if os.path.islink(fpath) and not os.path.exists(fpath):
                    unrecoverable.append(f"{entry}/{fname}")
                    if drop_missing_from_contents:
                        changed = True
                        continue
                elif not os.path.exists(fpath):
                    if drop_missing_from_contents:
                        changed = True
                        continue
                kept.append(line)
            if changed:
                with open(contents_path, "w", encoding="utf-8") as f:
                    f.writelines(kept)
                fixed_contents += 1

        for fn in os.listdir(item_dir):
            if not fn.endswith(".xml"):
                continue
            fpath = os.path.join(item_dir, fn)
            with open(fpath, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
            fixed = sanitize_xml_text(content)
            if fixed != content:
                with open(fpath, "w", encoding="utf-8") as f:
                    f.write(fixed)
                fixed_xml += 1

    return {
        "status": "ok",
        "contents_files_fixed": fixed_contents,
        "xml_files_sanitized": fixed_xml,
        "symlinks_dereferenced": dereferenced,
        "unrecoverable_bitstreams": unrecoverable,
    }
