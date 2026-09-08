"""OJS/PKP "Articles and Issues XML" (elpub) -> DSpace batch CSV + PDFs.

Generalized port of this repo's elpub2csv.py. The original hardcoded a
single journal's name/ISSNs (Vestnik MGIMO); here journal identity is a
parameter (JournalInfo) so the same converter works for any OJS export,
while all the actually-reusable parsing logic (locale handling, author name
building, citation/ispartof formatting, GOST 7.79 transliteration for PDF
filenames, base64 PDF extraction) is unchanged.
"""

from __future__ import annotations

import base64
import csv
import html
import os
import re
from dataclasses import dataclass

from lxml import etree

CSV_COLUMNS = [
    "filename",
    "collection",
    "dc.title[en]",
    "dc.title[ru]",
    "dc.contributor.author[en]",
    "dc.contributor.author[ru]",
    "dc.contributor.affiliation[en]",
    "dc.contributor.affiliation[ru]",
    "dc.date.issued[]",
    "dc.description.abstract[en]",
    "dc.description.abstract[ru]",
    "dc.description[en]",
    "dc.description[ru]",
    "dc.identifier.doi[]",
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
    "dc.relation.ispartof[en]",
    "dc.relation.ispartof[ru]",
    "dc.relation.ispartofseries",
    "dc.format.extent",
    "dc.rights",
    "dc.coverage.spatial",
    "article.fpage[]",
    "article.lpage[]",
    "article.volume[]",
    "article.issue[]",
    "article.journalname[en]",
    "article.journalname[ru]",
    "publication.article.doi[]",
    "publication.journal.link[]",
]

LOCALE_MAP = {"en_US": "en", "ru_RU": "ru"}

_CYR_TO_LAT = {
    'а': 'a', 'б': 'b', 'в': 'v', 'г': 'g', 'д': 'd', 'е': 'e', 'ё': 'yo',
    'ж': 'zh', 'з': 'z', 'и': 'i', 'й': 'y', 'к': 'k', 'л': 'l', 'м': 'm',
    'н': 'n', 'о': 'o', 'п': 'p', 'р': 'r', 'с': 's', 'т': 't', 'у': 'u',
    'ф': 'f', 'х': 'kh', 'ц': 'ts', 'ч': 'ch', 'ш': 'sh', 'щ': 'shch',
    'ъ': '', 'ы': 'y', 'ь': '', 'э': 'e', 'ю': 'yu', 'я': 'ya',
}
_CYR_TO_LAT.update({k.upper(): v.capitalize() for k, v in _CYR_TO_LAT.items()})


@dataclass
class JournalInfo:
    """Journal identity, parameterized instead of hardcoded per-instance."""

    name_en: str
    name_ru: str
    issn_online: str = ""
    issn_print: str = ""


def transliterate(text: str) -> str:
    return "".join(_CYR_TO_LAT.get(ch, ch) for ch in text).lower()


def generate_pdf_filename(section_abbrev: str, volume: str, number: str, pages: str, journal_slug: str | None = None) -> str:
    """{journal}_{volume}_{number}_{fpage}_{lpage}.pdf, GOST-transliterated."""
    raw_name = journal_slug if journal_slug else section_abbrev
    abbrev = transliterate(raw_name)
    abbrev = re.sub(r'[^a-z0-9_]+', '_', abbrev).strip('_')

    fpage, lpage = _parse_pages(pages)
    parts = [abbrev, volume, number]
    if fpage:
        parts.append(fpage)
    if lpage:
        parts.append(lpage)
    return "_".join(p for p in parts if p) + ".pdf"


def locale_to_lang(locale_str: str) -> str:
    if locale_str in LOCALE_MAP:
        return LOCALE_MAP[locale_str]
    if "_" in locale_str:
        return locale_str.split("_")[0].lower()
    return locale_str.lower()


def get_localized_text(parent, tag: str) -> dict[str, str]:
    result = {}
    for elem in parent.findall(tag):
        loc = elem.get("locale")
        if loc:
            result[locale_to_lang(loc)] = elem.text or ""
    return result


def clean_html(text: str) -> str:
    if not text:
        return ""
    text = html.unescape(text)
    text = re.sub(r'<[^>]+>', ' ', text)
    return re.sub(r'\s+', ' ', text).strip()


def normalize_subject(subject_text: str) -> list[str]:
    if not subject_text:
        return []
    text = subject_text.strip()
    if text.startswith(":"):
        text = text[1:].strip()
    return [p.strip() for p in text.split(";") if p.strip()]


def build_author_name(author, lang: str) -> str:
    """'Lastname, Firstname M.' (en) / 'Фамилия, И. О.' (ru)"""
    def _t(tag):
        el = author.find(f'{tag}[@locale="{lang}"]')
        return el.text.strip() if el is not None and el.text else ""

    lastname, firstname, middlename = _t("lastname"), _t("firstname"), _t("middlename")
    if not lastname and not firstname:
        return ""
    name = lastname
    if firstname:
        name = f"{name}, {firstname}" if name else firstname
    if middlename:
        name += f" {middlename}"
    return name


def _build_citation_initials(author, lang_locale: str) -> tuple[str, str]:
    def _t(tag):
        el = author.find(f'{tag}[@locale="{lang_locale}"]')
        return el.text.strip() if el is not None and el.text else ""

    lastname, firstname, middlename = _t("lastname"), _t("firstname"), _t("middlename")
    if not lastname:
        return "", ""
    if lang_locale == "en_US":
        return lastname, (firstname[0] if firstname else "")
    initials = firstname[0] if firstname else ""
    if middlename:
        initials += f".{middlename[0]}"
    return lastname, initials


def build_citation(author_lastname, author_initials, title, journal_name, year, volume, number, pages, doi) -> str:
    author_prefix = f"{author_lastname} {author_initials}" if author_lastname and author_initials else ""

    vol_issue = ""
    if volume and number:
        vol_issue = f"{volume}({number})"
    elif volume or number:
        vol_issue = volume or number

    detail = ""
    if year and vol_issue:
        detail = f"{year};({vol_issue})"
    elif year:
        detail = year
    if detail and pages:
        detail += f":{pages}"
    elif pages:
        detail = pages

    doi_url = f"https://doi.org/{doi}" if doi else ""
    parts = [p for p in [author_prefix.strip(), title, journal_name, detail, doi_url] if p]
    return ". ".join(parts)


def build_ispartof(section_title, section_abbrev, volume, number, year, lang) -> str:
    parts = []
    if section_title:
        parts.append(section_title)
    if section_abbrev:
        parts.append(f"({section_abbrev})")
    vol_parts = []
    if volume:
        vol_parts.append(f"Т. {volume}" if lang == "ru" else f"Vol. {volume}")
    if number:
        vol_parts.append(f"№ {number}" if lang == "ru" else f"No. {number}")
    if year:
        vol_parts.append(str(year))
    if vol_parts:
        parts.append(", ".join(vol_parts))
    return ". ".join(parts)


def build_ispartofseries(section_abbrev, volume, number) -> str:
    parts = []
    if section_abbrev:
        parts.append(section_abbrev)
    vol_parts = []
    if volume:
        vol_parts.append(f"Vol. {volume}")
    if number:
        vol_parts.append(f"No. {number}")
    if vol_parts:
        parts.append(", ".join(vol_parts))
    return "; ".join(parts)


def _parse_pages(pages_str: str) -> tuple[str, str]:
    normalized = pages_str.replace('–', '-').replace('—', '-')
    if '-' in normalized:
        a, b = normalized.split('-', 1)
        return a.strip(), b.strip()
    return (normalized.strip(), '') if normalized.strip() else ('', '')


def process_article(article, section_data: dict, issue_data: dict, output_dir: str, journal: JournalInfo, collection: str = "") -> dict:
    row = {col: "" for col in CSV_COLUMNS}
    row["collection"] = collection
    row["dc.language.iso"] = article.get("language", "")

    id_doi = article.find('id[@type="doi"]')
    doi_value = id_doi.text.strip() if id_doi is not None and id_doi.text else ""
    if doi_value:
        row["dc.identifier.doi[]"] = doi_value

    titles = get_localized_text(article, "title")
    row["dc.title[en]"] = titles.get("en", "")
    row["dc.title[ru]"] = titles.get("ru", "")

    abstracts = get_localized_text(article, "abstract")
    row["dc.description.abstract[en]"] = clean_html(abstracts.get("en", ""))
    row["dc.description.abstract[ru]"] = clean_html(abstracts.get("ru", ""))

    subjects_en: list[str] = []
    subjects_ru: list[str] = []
    indexing = article.find("indexing")
    if indexing is not None:
        for subj_elem in indexing.findall("subject"):
            lang = locale_to_lang(subj_elem.get("locale", ""))
            normalized = normalize_subject(subj_elem.text or "")
            if lang == "en":
                subjects_en = normalized
            elif lang == "ru":
                subjects_ru = normalized
            elif not subjects_en and not subjects_ru:
                subjects_en = normalized
    row["dc.subject[en]"] = "||".join(subjects_en)
    row["dc.subject[ru]"] = "||".join(subjects_ru)

    authors_en: list[str] = []
    authors_ru: list[str] = []
    affiliations_en: list[str] = []
    affiliations_ru: list[str] = []
    countries: list[str] = []
    bio_en = bio_ru = ""
    first_en_lastname = first_en_initial = first_ru_lastname = first_ru_initial = ""

    for idx, author in enumerate(article.findall("author")):
        en_name = build_author_name(author, "en_US")
        ru_name = build_author_name(author, "ru_RU")
        if not en_name and not ru_name:
            continue
        if en_name:
            authors_en.append(en_name)
        if ru_name:
            authors_ru.append(ru_name)

        if idx == 0 or (not first_en_lastname and not first_ru_lastname):
            en_ln, en_in = _build_citation_initials(author, "en_US")
            ru_ln, ru_in = _build_citation_initials(author, "ru_RU")
            if en_ln:
                first_en_lastname, first_en_initial = en_ln, en_in
            if ru_ln:
                first_ru_lastname, first_ru_initial = ru_ln, ru_in

        aff_en = author.find('affiliation[@locale="en_US"]')
        aff_ru = author.find('affiliation[@locale="ru_RU"]')
        if aff_en is not None and aff_en.text and aff_en.text.strip():
            affiliations_en.append(aff_en.text.strip())
        if aff_ru is not None and aff_ru.text and aff_ru.text.strip():
            affiliations_ru.append(aff_ru.text.strip())

        country = author.find("country")
        if country is not None and country.text and country.text.strip():
            countries.append(country.text.strip())

        if author.get("primary_contact") == "true":
            bio_en_elem = author.find('biography[@locale="en_US"]')
            bio_ru_elem = author.find('biography[@locale="ru_RU"]')
            if bio_en_elem is not None and bio_en_elem.text:
                bio_en = bio_en_elem.text.strip()
            if bio_ru_elem is not None and bio_ru_elem.text:
                bio_ru = bio_ru_elem.text.strip()

    row["dc.contributor.author[en]"] = "||".join(authors_en)
    row["dc.contributor.author[ru]"] = "||".join(authors_ru)
    row["dc.contributor.affiliation[en]"] = "||".join(affiliations_en)
    row["dc.contributor.affiliation[ru]"] = "||".join(affiliations_ru)
    row["dc.description[en]"] = bio_en
    row["dc.description[ru]"] = bio_ru
    row["dc.coverage.spatial"] = "||".join(dict.fromkeys(countries))

    pages = article.findtext("pages", default="").strip()
    row["dc.format.extent"] = pages

    article_date = article.findtext("date_published", default="").strip()
    row["dc.date.issued[]"] = article_date or issue_data.get("date_published", "")

    volume = issue_data.get("volume", "")
    number = issue_data.get("number", "")
    year = issue_data.get("year", "")

    row["dc.identifier.issn[online]"] = journal.issn_online
    row["dc.identifier.issn[print]"] = journal.issn_print
    row["dc.identifier.uri"] = ""

    row["dc.identifier.citation[en]"] = build_citation(
        first_en_lastname, first_en_initial, titles.get("en", ""), journal.name_en,
        year, volume, number, pages, doi_value,
    )
    row["dc.identifier.citation[ru]"] = build_citation(
        first_ru_lastname, first_ru_initial, titles.get("ru", ""), journal.name_ru,
        year, volume, number, pages, doi_value,
    )

    row["dc.type"] = "Article"
    row["dc.source[en]"] = section_data.get("title_en", "")
    row["dc.source[ru]"] = section_data.get("title_ru", "")
    row["dc.relation.ispartof[en]"] = build_ispartof(
        section_data.get("title_en", ""), section_data.get("abbrev_en", ""), volume, number, year, "en"
    )
    row["dc.relation.ispartof[ru]"] = build_ispartof(
        section_data.get("title_ru", ""), section_data.get("abbrev_ru", ""), volume, number, year, "ru"
    )
    row["dc.relation.ispartofseries"] = build_ispartofseries(section_data.get("abbrev_en", ""), volume, number)

    if issue_data.get("open_access"):
        row["dc.rights"] = "openAccess"

    fpage, lpage = _parse_pages(pages)
    row["article.fpage[]"] = fpage
    row["article.lpage[]"] = lpage
    row["article.volume[]"] = volume
    row["article.issue[]"] = number
    row["article.journalname[en]"] = journal.name_en
    row["article.journalname[ru]"] = journal.name_ru
    row["publication.article.doi[]"] = f"https://doi.org/{doi_value}" if doi_value else ""

    section_abbrev = section_data.get("abbrev_en", "") or section_data.get("abbrev_ru", "")
    generated_name = generate_pdf_filename(section_abbrev, volume, number, pages, journal.name_en)
    row["filename"] = generated_name

    galley = article.find("galley")
    if galley is not None:
        file_elem = galley.find("file")
        if file_elem is not None:
            embed_elem = file_elem.find("embed")
            if embed_elem is not None:
                encoding = embed_elem.get("encoding", "")
                mime_type = embed_elem.get("mime_type", "")
                b64 = embed_elem.text or ""
                if encoding == "base64" and b64 and "pdf" in mime_type:
                    with open(os.path.join(output_dir, generated_name), "wb") as pdf_file:
                        pdf_file.write(base64.b64decode(b64))

    return row


def parse_issue(
    xml_path: str,
    output_dir: str,
    csv_name: str,
    journal: JournalInfo,
    collection: str = "",
    csv_encoding: str = "utf-8-sig",
) -> dict:
    """Read an OJS issue.xml, write CSV + extracted PDFs to output_dir."""
    os.makedirs(output_dir, exist_ok=True)
    tree = etree.parse(xml_path)
    root = tree.getroot()

    issue_data = {
        "volume": root.findtext("volume", default="").strip(),
        "number": root.findtext("number", default="").strip(),
        "year": root.findtext("year", default="").strip(),
        "date_published": root.findtext("date_published", default="").strip(),
        "open_access": root.find("open_access") is not None,
    }

    all_rows = []
    for section in root.findall("section"):
        section_data = {"title_en": "", "title_ru": "", "abbrev_en": "", "abbrev_ru": ""}
        for title_elem in section.findall("title"):
            lang = locale_to_lang(title_elem.get("locale", ""))
            section_data[f"title_{lang}"] = title_elem.text or ""
        for abbrev_elem in section.findall("abbrev"):
            lang = locale_to_lang(abbrev_elem.get("locale", ""))
            section_data[f"abbrev_{lang}"] = abbrev_elem.text or ""

        for article in section.findall("article"):
            all_rows.append(process_article(article, section_data, issue_data, output_dir, journal, collection))

    csv_path = os.path.join(output_dir, csv_name)
    write_encoding = "utf-8" if "utf-8-sig" in csv_encoding else csv_encoding
    with open(csv_path, "w", newline="", encoding=write_encoding) as f:
        if "utf-8-sig" in csv_encoding:
            f.write("\ufeff")
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS, quoting=csv.QUOTE_MINIMAL)
        writer.writeheader()
        writer.writerows(all_rows)

    pdf_count = sum(1 for fn in os.listdir(output_dir) if fn.lower().endswith(".pdf"))
    return {"article_count": len(all_rows), "csv_path": csv_path, "pdf_count": pdf_count}
