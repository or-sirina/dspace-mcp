"""Metadata normalization helpers: author name canonicalization, COAR
resource-type assignment, ORCID harvesting/application.

All DB-mutating operations here go through guards.check_db_write (blocked
unless allow_db_write=True, auto-backed-up first) and wrap their SQL in a
single transaction with \\set ON_ERROR_STOP on, matching the hand-written
SQL this module replaces (build_saf/process_authors.py,
build_saf/gather_orcid.py + apply_orcid.sql, build_saf/type_coar.sql).
"""

from __future__ import annotations

import re
import tempfile
import time
import collections
from dataclasses import dataclass

from .. import guards, ssh
from ..config import Profile

AUTHOR_FIELD_SQL = (
    "metadata_field_id IN (SELECT r.metadata_field_id FROM metadatafieldregistry r "
    "JOIN metadataschemaregistry s ON s.metadata_schema_id = r.metadata_schema_id "
    "WHERE s.short_id = 'dc' AND r.element = 'contributor' AND r.qualifier = 'author')"
)

# Default dc.type -> (en, ru, COAR URI) mapping, from type_coar.sql. Callers
# can pass their own via the `type_map` argument to assign_coar_types.
DEFAULT_COAR_TYPE_MAP: dict[str, tuple[str, str, str]] = {
    "Article": ("journal article", "статья", "http://purl.org/coar/resource_type/c_6501"),
    "монография": ("book", "монография", "http://purl.org/coar/resource_type/c_2f33"),
    "диссертация": ("thesis", "диссертация", "http://purl.org/coar/resource_type/c_46ec"),
    "Preprint": ("preprint", "препринт", "http://purl.org/coar/resource_type/c_816b"),
    "Dataset": ("dataset", "база данных", "http://purl.org/coar/resource_type/c_ddb1"),
    "База данных": ("dataset", "база данных", "http://purl.org/coar/resource_type/c_ddb1"),
    "доклад": ("report", "доклад", "http://purl.org/coar/resource_type/c_93fc"),
    "учебное пособие": ("book", "учебное пособие", "http://purl.org/coar/resource_type/c_2f33"),
    "Книга": ("book", "книга", "http://purl.org/coar/resource_type/c_2f33"),
    "научное издание": ("book", "научное издание", "http://purl.org/coar/resource_type/c_2f33"),
    "Image": ("image", "изображение", "http://purl.org/coar/resource_type/c_c513"),
}


def _q(s: str) -> str:
    return s.replace("'", "''")


# ---- author canonicalization --------------------------------------------

def _norm_letters(s: str) -> str:
    s = s.lower().replace("ё", "е")
    return "".join(ch for ch in s if ch.isalpha())


def _parse_name_key(val: str) -> tuple[str, str] | None:
    v = val.strip()
    if "," in v:
        surname, rest = v.split(",", 1)
    else:
        m = re.match(r'^(.*?)[\s]+([A-ZА-ЯЁ]\.?(?:\s*[A-ZА-ЯЁ]\.?)*)$', v)
        surname, rest = (m.group(1), m.group(2)) if m else (v, "")
    sk, ik = _norm_letters(surname), _norm_letters(rest)
    if not sk or len(ik) > 4:
        return None
    return (sk, ik)


def _is_allcaps(s: str) -> bool:
    letters = re.sub(r'[^A-Za-zА-Яа-яЁё]', '', s)
    return letters.isupper() and len(letters) > 1


@dataclass
class AuthorRewrite:
    variant: str
    canonical: str
    uses: int


def normalize_authors(profile: Profile, confirm: bool = False, allow_db_write: bool = False) -> dict:
    """Find mechanical author-name variants (ё/е, case, punctuation, spacing)
    grouped by (surname, initials) and propose/apply a canonical spelling.

    Read-only by default: returns the proposed variant->canonical rewrites.
    Pass confirm=True and allow_db_write=True to apply them as a single
    transaction (auto-backed-up first).
    """
    rows = ssh.psql_query(
        profile,
        f"SELECT text_value, count(*) FROM metadatavalue WHERE {AUTHOR_FIELD_SQL} "
        f"AND text_value <> '' GROUP BY text_value;",
    )

    groups: dict[tuple[str, str], list[tuple[str, int]]] = collections.defaultdict(list)
    for text_value, count_str in rows:
        key = _parse_name_key(text_value)
        if key is None:
            continue
        groups[key].append((text_value, int(count_str)))

    rewrites: list[AuthorRewrite] = []
    for _key, variants in groups.items():
        if len(variants) < 2:
            continue

        def score(item: tuple[str, int]) -> tuple:
            val, uses = item
            has_comma = 1 if "," in val else 0
            not_caps = 0 if _is_allcaps(val) else 1
            clean = 0 if re.search(r"[^\w\s.,'\-]", val) else 1
            has_yo = 1 if ("ё" in val or "Ё" in val) else 0
            return (has_comma, not_caps, clean, uses, has_yo, len(val))

        canonical = max(variants, key=score)[0]
        for val, uses in variants:
            if val != canonical:
                rewrites.append(AuthorRewrite(variant=val, canonical=canonical, uses=uses))

    rewrites.sort(key=lambda r: -r.uses)

    def do_backup() -> dict:
        from . import admin as admin_mod  # local import to avoid a cycle
        return admin_mod.db_backup(profile)

    gate = guards.check_db_write(allow_db_write, do_backup, "normalize_authors")
    if gate is not None and gate.get("status") == "blocked":
        return {
            "status": "preview",
            "rewrite_count": len(rewrites),
            "affected_value_count": sum(r.uses for r in rewrites),
            "rewrites": [r.__dict__ for r in rewrites[:50]],
            "note": "Preview only. " + gate["reason"],
        }

    preview = guards.check_confirm(
        confirm,
        commands=[f"UPDATE metadatavalue ... ({len(rewrites)} statements, transactional)"],
        effect=f"Rewrites {sum(r.uses for r in rewrites)} stored author values across {len(rewrites)} variant spellings.",
        rewrites=[r.__dict__ for r in rewrites[:50]],
        backup=gate,
    )
    if preview is not None:
        return preview

    if not rewrites:
        return {"status": "ok", "applied": 0, "backup": gate}

    sql_lines = ["\\set ON_ERROR_STOP on", "BEGIN;"]
    for r in rewrites:
        sql_lines.append(
            f"UPDATE metadatavalue SET text_value='{_q(r.canonical)}' "
            f"WHERE {AUTHOR_FIELD_SQL} AND text_value='{_q(r.variant)}';"
        )
    sql_lines.append("COMMIT;")

    result = ssh.psql_execute(profile, "\n".join(sql_lines))
    return {
        "status": "ok" if result.ok else "error",
        "applied": len(rewrites) if result.ok else 0,
        "backup": gate,
        "stdout": result.stdout,
        "stderr": result.stderr,
    }


# ---- COAR resource type assignment ---------------------------------------

def assign_coar_types(
    profile: Profile,
    confirm: bool = False,
    allow_db_write: bool = False,
    type_map: dict[str, tuple[str, str, str]] | None = None,
) -> dict:
    """Remap dc.type values to a bilingual (en/ru) pair + local.type.coar URI.

    Registers local.type.coar if it doesn't exist yet. Unmapped dc.type
    values are left untouched and reported.
    """
    type_map = type_map or DEFAULT_COAR_TYPE_MAP

    current_rows = ssh.psql_query(
        profile,
        "SELECT mv.text_value, count(*) FROM metadatavalue mv "
        "JOIN metadatafieldregistry r ON r.metadata_field_id = mv.metadata_field_id "
        "JOIN metadataschemaregistry s ON s.metadata_schema_id = r.metadata_schema_id "
        "WHERE s.short_id = 'dc' AND r.element = 'type' AND r.qualifier IS NULL "
        "GROUP BY mv.text_value ORDER BY 2 DESC;",
    )
    mapped = {v: int(c) for v, c in current_rows if v in type_map}
    unmapped = {v: int(c) for v, c in current_rows if v not in type_map}

    def do_backup() -> dict:
        from . import admin as admin_mod
        return admin_mod.db_backup(profile)

    gate = guards.check_db_write(allow_db_write, do_backup, "assign_coar_types")
    if gate is not None and gate.get("status") == "blocked":
        return {
            "status": "preview",
            "would_remap": mapped,
            "unmapped_left_as_is": unmapped,
            "note": "Preview only. " + gate["reason"],
        }

    preview = guards.check_confirm(
        confirm,
        commands=["INSERT/DELETE/UPDATE metadatavalue for dc.type + local.type.coar (transactional)"],
        effect=f"Remaps {sum(mapped.values())} items across {len(mapped)} dc.type values to "
               f"bilingual dc.type + COAR URI.",
        would_remap=mapped,
        unmapped_left_as_is=unmapped,
        backup=gate,
    )
    if preview is not None:
        return preview

    values_sql = ", ".join(
        f"('{_q(cur)}', '{_q(en)}', '{_q(ru)}', '{_q(uri)}')" for cur, (en, ru, uri) in type_map.items()
    )
    sql = f"""\\set ON_ERROR_STOP on
BEGIN;
CREATE TEMP TABLE _ids AS
SELECT (SELECT r.metadata_field_id FROM metadatafieldregistry r JOIN metadataschemaregistry s ON s.metadata_schema_id=r.metadata_schema_id
        WHERE s.short_id='dc' AND r.element='type' AND r.qualifier IS NULL) AS type_fid;

INSERT INTO metadatafieldregistry(metadata_field_id, metadata_schema_id, element, qualifier)
SELECT nextval('metadatafieldregistry_seq'), s.metadata_schema_id, 'type', 'coar'
FROM metadataschemaregistry s
WHERE s.short_id='local'
  AND NOT EXISTS (SELECT 1 FROM metadatafieldregistry f JOIN metadataschemaregistry s2 ON s2.metadata_schema_id=f.metadata_schema_id
                  WHERE s2.short_id='local' AND f.element='type' AND f.qualifier='coar');

CREATE TEMP TABLE tmap(cur text PRIMARY KEY, en text, ru text, uri text);
INSERT INTO tmap VALUES {values_sql};

CREATE TEMP TABLE newt AS
SELECT DISTINCT mv.dspace_object_id AS obj, t.en, t.ru, t.uri
FROM metadatavalue mv JOIN tmap t ON t.cur=mv.text_value
WHERE mv.metadata_field_id=(SELECT type_fid FROM _ids);

INSERT INTO metadatavalue(metadata_field_id, text_value, text_lang, place, confidence, dspace_object_id)
SELECT (SELECT r.metadata_field_id FROM metadatafieldregistry r JOIN metadataschemaregistry s ON s.metadata_schema_id=r.metadata_schema_id
        WHERE s.short_id='local' AND r.element='type' AND r.qualifier='coar'),
       n.uri, NULL, 0, -1, n.obj
FROM newt n
WHERE NOT EXISTS (SELECT 1 FROM metadatavalue x
        WHERE x.dspace_object_id=n.obj
          AND x.metadata_field_id=(SELECT r.metadata_field_id FROM metadatafieldregistry r JOIN metadataschemaregistry s ON s.metadata_schema_id=r.metadata_schema_id WHERE s.short_id='local' AND r.element='type' AND r.qualifier='coar'));

DELETE FROM metadatavalue WHERE metadata_field_id=(SELECT type_fid FROM _ids) AND dspace_object_id IN (SELECT obj FROM newt);

INSERT INTO metadatavalue(metadata_field_id, text_value, text_lang, place, confidence, dspace_object_id)
SELECT (SELECT type_fid FROM _ids), en, 'en', 0, -1, obj FROM newt;
INSERT INTO metadatavalue(metadata_field_id, text_value, text_lang, place, confidence, dspace_object_id)
SELECT (SELECT type_fid FROM _ids), ru, 'ru', 1, -1, obj FROM newt;
COMMIT;
"""
    result = ssh.psql_execute(profile, sql)
    return {
        "status": "ok" if result.ok else "error",
        "remapped_types": mapped,
        "backup": gate,
        "stdout": result.stdout,
        "stderr": result.stderr,
    }


# ---- ORCID harvesting (Crossref, read-only network call) ----------------

def _fam_init(name: str) -> tuple[str, str]:
    name = name.strip()
    if "," in name:
        fam, rest = name.split(",", 1)
    else:
        parts = name.rsplit(" ", 1)
        fam, rest = (parts[0], parts[1]) if len(parts) == 2 else (name, "")
    famk = "".join(ch for ch in fam.lower() if ch.isalpha())
    inits = "".join(ch for ch in rest.lower() if ch.isalpha())
    return famk, inits


def harvest_orcid(profile: Profile, mailto: str, limit: int = 0) -> dict:
    """Match ORCIDs to author name-values by DOI via Crossref (read-only,
    no DB write). Returns {uuid, author_text, orcid} triples ready to be
    handed to apply_orcid_matches().
    """
    import requests

    field = ssh.psql_query(
        profile,
        "SELECT r.metadata_field_id FROM metadatafieldregistry r JOIN metadataschemaregistry s "
        "ON s.metadata_schema_id=r.metadata_schema_id WHERE s.short_id='dc' AND r.element='identifier' AND r.qualifier='doi';",
    )
    if not field or not field[0]:
        return {"status": "error", "reason": "could not resolve dc.identifier.doi field id"}
    doi_field_id = field[0][0]

    pairs = ssh.psql_query(
        profile,
        f"SELECT d.dspace_object_id, d.text_value AS doi, a.text_value AS author "
        f"FROM metadatavalue d JOIN metadatavalue a ON a.dspace_object_id = d.dspace_object_id "
        f"WHERE d.metadata_field_id = {doi_field_id} AND {AUTHOR_FIELD_SQL.replace('metadata_field_id', 'a.metadata_field_id')} "
        f"AND a.authority IS NULL AND d.text_value <> '';",
    )

    by_doi: dict[str, list[tuple[str, str]]] = collections.OrderedDict()
    for uuid, doi, author in pairs:
        by_doi.setdefault(doi.strip(), []).append((uuid, author))

    dois = list(by_doi.keys())
    if limit:
        dois = dois[:limit]

    headers = {"User-Agent": f"dspace-mcp/1.0 (mailto:{mailto})"}
    matches: list[dict] = []
    doi_with_orcid = 0

    for doi in dois:
        try:
            r = requests.get(f"https://api.crossref.org/works/{doi}", params={"mailto": mailto}, headers=headers, timeout=25)
            if r.status_code != 200:
                time.sleep(0.05)
                continue
            msg = r.json().get("message", {})
        except Exception:  # noqa: BLE001
            time.sleep(0.1)
            continue

        candidates = []
        for a in msg.get("author", []) or []:
            orcid = a.get("ORCID")
            if not orcid:
                continue
            fk = "".join(ch for ch in a.get("family", "").lower() if ch.isalpha())
            gi = "".join(ch for ch in a.get("given", "").lower() if ch.isalpha())
            oid = re.sub(r"^https?://orcid\.org/", "", orcid.strip())
            if fk:
                candidates.append((fk, gi, oid))
        if not candidates:
            time.sleep(0.02)
            continue
        doi_with_orcid += 1

        for uuid, author in by_doi[doi]:
            fk, inits = _fam_init(author)
            cands = [c for c in candidates if c[0] == fk]
            pick = None
            if len(cands) == 1:
                pick = cands[0]
            elif len(cands) > 1 and inits:
                narrowed = [c for c in cands if c[1].startswith(inits[0])]
                if len(narrowed) == 1:
                    pick = narrowed[0]
            if pick and inits and pick[1] and not pick[1].startswith(inits[0]):
                pick = None
            if pick:
                matches.append({"uuid": uuid, "author": author, "orcid": pick[2]})
        time.sleep(0.02)

    return {
        "status": "ok",
        "dois_queried": len(dois),
        "dois_with_orcid_data": doi_with_orcid,
        "matches": matches,
        "distinct_orcids": len({m["orcid"] for m in matches}),
    }


def apply_orcid_matches(
    profile: Profile,
    matches: list[dict],
    confirm: bool = False,
    allow_db_write: bool = False,
) -> dict:
    """Apply harvest_orcid() matches: set metadatavalue.authority=<orcid>,
    confidence=600 on unambiguous (item, author-text) pairs. Keeps a
    reversible backup table (orcid_apply_backup) of every row it touches.
    """
    unambiguous: dict[tuple[str, str], str] = {}
    ambiguous_keys: set[tuple[str, str]] = set()
    for m in matches:
        key = (m["uuid"], m["author"])
        if key in unambiguous and unambiguous[key] != m["orcid"]:
            ambiguous_keys.add(key)
        else:
            unambiguous[key] = m["orcid"]
    for key in ambiguous_keys:
        unambiguous.pop(key, None)

    def do_backup() -> dict:
        from . import admin as admin_mod
        return admin_mod.db_backup(profile)

    gate = guards.check_db_write(allow_db_write, do_backup, "apply_orcid_matches")
    if gate is not None and gate.get("status") == "blocked":
        return {"status": "preview", "would_apply": len(unambiguous), "note": "Preview only. " + gate["reason"]}

    preview = guards.check_confirm(
        confirm,
        commands=["\\copy omap FROM <tsv>", "UPDATE metadatavalue SET authority=..., confidence=600 (transactional, backed up)"],
        effect=f"Sets ORCID authority on {len(unambiguous)} author metadata rows.",
        would_apply=len(unambiguous),
        backup=gate,
    )
    if preview is not None:
        return preview

    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", suffix=".tsv", delete=False) as f:
        for (uuid, author), orcid in unambiguous.items():
            f.write(f"{uuid}\t{author}\t{orcid}\n")
        local_tsv = f.name

    remote_tsv = "/tmp/dspace_mcp_orcid_map.tsv"
    scp_result = ssh.scp_to(profile, local_tsv, remote_tsv)
    if not scp_result.ok:
        return {"status": "error", "stage": "scp", "stderr": scp_result.stderr}

    sql = f"""\\set ON_ERROR_STOP on
CREATE TEMP TABLE omap(obj uuid, author text, orcid text);
\\copy omap FROM '{remote_tsv}' WITH (FORMAT text, DELIMITER E'\\t')
CREATE TEMP TABLE omap1 AS
SELECT obj, author, min(orcid) AS orcid FROM omap GROUP BY obj, author HAVING count(DISTINCT orcid)=1;

BEGIN;
DROP TABLE IF EXISTS orcid_apply_backup;
CREATE TABLE orcid_apply_backup AS
SELECT mv.metadata_value_id, mv.dspace_object_id, mv.text_value,
       mv.authority AS old_authority, mv.confidence AS old_confidence
FROM metadatavalue mv
JOIN omap1 o ON o.obj=mv.dspace_object_id AND o.author=mv.text_value
WHERE {AUTHOR_FIELD_SQL} AND mv.authority IS NULL;

UPDATE metadatavalue mv SET authority=o.orcid, confidence=600
FROM omap1 o
WHERE mv.dspace_object_id=o.obj AND mv.text_value=o.author
  AND {AUTHOR_FIELD_SQL} AND mv.authority IS NULL;
COMMIT;
"""
    result = ssh.psql_execute(profile, sql)
    return {
        "status": "ok" if result.ok else "error",
        "applied": len(unambiguous) if result.ok else 0,
        "ambiguous_skipped": len(ambiguous_keys),
        "backup": gate,
        "recovery_table": "orcid_apply_backup",
        "stdout": result.stdout,
        "stderr": result.stderr,
    }
