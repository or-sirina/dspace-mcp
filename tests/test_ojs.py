"""Tests for the OJS/PKP XML -> DSpace CSV converter (pure helpers)."""

from dspace_mcp.dspace import ojs


def test_transliterate():
    assert ojs.transliterate("Вестник") == "vestnik"
    assert ojs.transliterate("Журнал") == "zhurnal"
    assert ojs.transliterate("МГИМО") == "mgimo"


def test_generate_pdf_filename():
    fn = ojs.generate_pdf_filename("", "10", "2", "12-34", "Vestnik MGIMO")
    assert fn == "vestnik_mgimo_10_2_12_34.pdf"


def test_locale_to_lang():
    assert ojs.locale_to_lang("en_US") == "en"
    assert ojs.locale_to_lang("ru_RU") == "ru"
    assert ojs.locale_to_lang("fr_FR") == "fr"
    assert ojs.locale_to_lang("en") == "en"


def test_clean_html():
    assert ojs.clean_html("<p>Hello&nbsp;world</p>") == "Hello world"
    assert ojs.clean_html("") == ""


def test_normalize_subject():
    assert ojs.normalize_subject(": a; b ; c") == ["a", "b", "c"]
    assert ojs.normalize_subject("") == []


def test_parse_pages():
    assert ojs._parse_pages("12–34") == ("12", "34")
    assert ojs._parse_pages("12—34") == ("12", "34")
    assert ojs._parse_pages("12") == ("12", "")
    assert ojs._parse_pages("") == ("", "")


def test_build_citation():
    c = ojs.build_citation("Smith", "J.", "Title", "Journal", "2024", "10", "2", "1-10", "10.1000/xyz")
    assert c.startswith("Smith J.")
    assert "Journal" in c
    assert "2024;(10(2)):1-10" in c
    assert c.endswith("https://doi.org/10.1000/xyz")


def test_parse_issue_roundtrip(tmp_path):
    xml_path = tmp_path / "issue.xml"
    xml_path.write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<issue xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <volume>10</volume>
  <number>2</number>
  <year>2024</year>
  <date_published>2024-06-01</date_published>
  <open_access/>
  <section>
    <title locale="en_US">Test Section</title>
    <title locale="ru_RU">Тестовый раздел</title>
    <abbrev locale="en_US">TS</abbrev>
    <article language="en">
      <id type="doi">10.1000/xyz</id>
      <title locale="en_US">An Example Article</title>
      <abstract locale="en_US"><p>Abstract text here.</p></abstract>
      <indexing><subject locale="en_US">: key1; key2</subject></indexing>
      <author primary_contact="true">
        <lastname locale="en_US">Smith</lastname>
        <firstname locale="en_US">John</firstname>
        <affiliation locale="en_US">University of Example</affiliation>
      </author>
      <pages>12-34</pages>
    </article>
  </section>
</issue>
""",
        encoding="utf-8",
    )

    journal = ojs.JournalInfo(name_en="Test Journal", name_ru="Тестовый журнал", issn_online="1234-5678")
    result = ojs.parse_issue(str(xml_path), str(tmp_path / "out"), "metadata.csv", journal)

    assert result["article_count"] == 1
    assert result["csv_path"].endswith("metadata.csv")
    rows = open(result["csv_path"], encoding="utf-8-sig").read()
    assert "An Example Article" in rows
    assert "Smith, John" in rows
    assert "key1||key2" in rows
    assert "1234-5678" in rows
