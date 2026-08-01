"""Turn a column of news into a file someone can work with elsewhere.

Five formats, chosen for what people actually do with a feed: a spreadsheet to
sort and filter in, a document to circulate, a plain table for scripts, text for
a wiki or a notebook, and the raw records for another program.

Everything here is written with the standard library. .xlsx and .docx are both
ZIP archives of XML, and the subset each needs is small — a few hundred lines
against two more dependencies to install, pin and keep current on a server whose
whole point is to run unattended. The files open in Excel, LibreOffice, Google
Sheets, Word and Pages; the test suite unzips them and checks the parts.
"""
from __future__ import annotations

import csv
import io
import json
import zipfile
from datetime import datetime, timezone
from xml.sax.saxutils import escape

# What a row of an export contains, in the order a reader wants to see it.
# Title first: a spreadsheet is read left to right, and the headline is the
# thing being scanned for.
COLUMNS = [
    ("title", "Title"),
    ("published_at", "Published (UTC)"),
    ("source", "Outlet"),
    ("country", "Country"),
    ("categories", "Categories"),
    ("importance", "Importance"),
    ("scope", "Scope"),
    ("language", "Language"),
    ("event_id", "Story"),
    ("summary", "Summary"),
    ("url", "Link"),
]

FORMATS = {
    "csv": ("text/csv", "csv"),
    "xlsx": ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "xlsx"),
    "docx": ("application/vnd.openxmlformats-officedocument.wordprocessingml.document", "docx"),
    "md": ("text/markdown; charset=utf-8", "md"),
    "json": ("application/json", "json"),
}


def _clean(value) -> str:
    """One cell's text.

    Control characters are stripped rather than escaped: Excel refuses to open
    a workbook containing them, and they arrive routinely in feed summaries."""
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return ", ".join(str(v) for v in value)
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M")
    text = str(value)
    return "".join(ch for ch in text if ch == "\n" or ch >= " ").strip()


def rows_from(articles: list[dict]) -> list[list[str]]:
    """The articles as a table, header first."""
    out = [[label for _, label in COLUMNS]]
    for a in articles:
        out.append([_clean(a.get(key)) for key, _ in COLUMNS])
    return out


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


# ---------- csv ----------

def to_csv(name: str, articles: list[dict]) -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\r\n")
    writer.writerows(rows_from(articles))
    # The BOM is what makes Excel open a UTF-8 CSV as UTF-8 rather than as the
    # system code page, which is the difference between "Zürich" and "ZÃ¼rich".
    return b"\xef\xbb\xbf" + buf.getvalue().encode("utf-8")


# ---------- markdown ----------

def to_markdown(name: str, articles: list[dict]) -> bytes:
    lines = [f"# {name}", "", f"{len(articles)} article(s), exported {_stamp()} "
             "from D.E.L.P.H.I.", ""]
    for a in articles:
        title = _clean(a.get("title"))
        url = _clean(a.get("url"))
        lines.append(f"## {title}" if not url else f"## [{title}]({url})")
        facts = " · ".join(x for x in [
            _clean(a.get("source")), _clean(a.get("published_at")),
            _clean(a.get("country")), _clean(a.get("categories")),
            f"importance {a['importance']}" if a.get("importance") is not None else "",
        ] if x)
        if facts:
            lines.append(f"*{facts}*")
        summary = _clean(a.get("summary"))
        if summary:
            lines += ["", summary]
        lines.append("")
    return "\n".join(lines).encode("utf-8")


# ---------- json ----------

def to_json(name: str, articles: list[dict]) -> bytes:
    payload = {"feed": name, "exported_at": _stamp(), "count": len(articles),
               "articles": articles}
    return json.dumps(payload, indent=2, ensure_ascii=False, default=str).encode("utf-8")


# ---------- xlsx ----------

_XLSX_CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>
</Types>"""

_XLSX_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>"""

_XLSX_WORKBOOK = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
 xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
<sheets><sheet name="{sheet}" sheetId="1" r:id="rId1"/></sheets>
</workbook>"""

_XLSX_WORKBOOK_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
</Relationships>"""

# Two formats: the header (bold, on a fill) and body cells that wrap, so a
# summary doesn't run across the sheet in one endless line.
_XLSX_STYLES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
<fonts count="2"><font><sz val="11"/><name val="Calibri"/></font>
<font><b/><sz val="11"/><color rgb="FFFFFFFF"/><name val="Calibri"/></font></fonts>
<fills count="3"><fill><patternFill patternType="none"/></fill>
<fill><patternFill patternType="gray125"/></fill>
<fill><patternFill patternType="solid"><fgColor rgb="FF1F3352"/><bgColor indexed="64"/></patternFill></fill></fills>
<borders count="1"><border/></borders>
<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
<cellXfs count="3">
<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>
<xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyFont="1" applyFill="1"/>
<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0" applyAlignment="1"><alignment vertical="top" wrapText="1"/></xf>
</cellXfs>
<!-- Excel expects the built-in "Normal" style to exist. Without it the
     workbook still opens, but readers warn about a missing default. -->
<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>
</styleSheet>"""


def _col_letter(index: int) -> str:
    """0 -> A, 25 -> Z, 26 -> AA."""
    letters = ""
    index += 1
    while index:
        index, rem = divmod(index - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


def to_xlsx(name: str, articles: list[dict]) -> bytes:
    table = rows_from(articles)
    # Roughly what each column needs to be readable without being dragged.
    widths = [60, 18, 22, 9, 18, 11, 12, 9, 8, 70, 40]
    cols = "".join(
        f'<col min="{i + 1}" max="{i + 1}" width="{w}" customWidth="1"/>'
        for i, w in enumerate(widths[:len(COLUMNS)]))

    body = []
    for r, row in enumerate(table, start=1):
        style = 1 if r == 1 else 2
        cells = "".join(
            f'<c r="{_col_letter(c)}{r}" t="inlineStr" s="{style}">'
            f"<is><t xml:space=\"preserve\">{escape(value)}</t></is></c>"
            for c, value in enumerate(row))
        body.append(f'<row r="{r}">{cells}</row>')

    last = f"{_col_letter(len(COLUMNS) - 1)}{max(len(table), 1)}"
    sheet = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f'<dimension ref="A1:{last}"/>'
        # Freeze the header and turn on filters: the two things anyone does to
        # a sheet like this before they can use it.
        '<sheetViews><sheetView workbookViewId="0" tabSelected="1">'
        '<pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/>'
        "</sheetView></sheetViews>"
        f"<cols>{cols}</cols>"
        f'<sheetData>{"".join(body)}</sheetData>'
        f'<autoFilter ref="A1:{last}"/>'
        "</worksheet>")

    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", _XLSX_CONTENT_TYPES)
        z.writestr("_rels/.rels", _XLSX_RELS)
        z.writestr("xl/workbook.xml", _XLSX_WORKBOOK.format(sheet=escape(_sheet_name(name))))
        z.writestr("xl/_rels/workbook.xml.rels", _XLSX_WORKBOOK_RELS)
        z.writestr("xl/styles.xml", _XLSX_STYLES)
        z.writestr("xl/worksheets/sheet1.xml", sheet)
    return out.getvalue()


def _sheet_name(name: str) -> str:
    """Excel refuses these characters in a tab name, and caps it at 31."""
    cleaned = "".join(" " if ch in "[]:*?/\\" else ch for ch in name).strip()
    return (cleaned or "Feed")[:31]


# ---------- docx ----------

_DOCX_CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
<Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>
</Types>"""

_DOCX_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>"""

_W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'

# Word tolerates a styles part with nothing but the styles it uses, but every
# reader expects docDefaults and a Normal style to hang the rest off — without
# them a heading is not recognized as a heading, which is the whole reason the
# document has headings.
_DOCX_STYLES = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:styles {_W}>
<w:docDefaults>
<w:rPrDefault><w:rPr><w:rFonts w:ascii="Calibri" w:hAnsi="Calibri"/><w:sz w:val="22"/></w:rPr></w:rPrDefault>
<w:pPrDefault><w:pPr><w:spacing w:after="120" w:line="276" w:lineRule="auto"/></w:pPr></w:pPrDefault>
</w:docDefaults>
<w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/>
<w:qFormat/></w:style>
<w:style w:type="paragraph" w:styleId="Title"><w:name w:val="Title"/>
<w:basedOn w:val="Normal"/><w:qFormat/>
<w:pPr><w:spacing w:after="120"/></w:pPr>
<w:rPr><w:b/><w:sz w:val="40"/></w:rPr></w:style>
<w:style w:type="paragraph" w:styleId="Heading1"><w:name w:val="heading 1"/>
<w:basedOn w:val="Normal"/><w:qFormat/>
<w:pPr><w:outlineLvl w:val="0"/><w:spacing w:before="280" w:after="80"/></w:pPr>
<w:rPr><w:b/><w:sz w:val="28"/></w:rPr></w:style>
<w:style w:type="paragraph" w:styleId="Facts"><w:name w:val="Facts"/>
<w:basedOn w:val="Normal"/><w:qFormat/>
<w:pPr><w:spacing w:after="60"/></w:pPr>
<w:rPr><w:i/><w:color w:val="555555"/><w:sz w:val="18"/></w:rPr></w:style>
</w:styles>"""


def _para(text: str, style: str = "") -> str:
    style_xml = f'<w:pPr><w:pStyle w:val="{style}"/></w:pPr>' if style else ""
    return (f"<w:p>{style_xml}<w:r><w:t xml:space=\"preserve\">"
            f"{escape(text)}</w:t></w:r></w:p>")


def to_docx(name: str, articles: list[dict]) -> bytes:
    """A readable brief rather than a table — this is the format people send on.

    Headings carry outline levels, so Word's navigation pane and any generated
    table of contents list the headlines.
    """
    parts = [_para(name, "Title"),
             _para(f"{len(articles)} article(s), exported {_stamp()} from "
                   f"D.E.L.P.H.I.", "Facts")]
    for a in articles:
        parts.append(_para(_clean(a.get("title")), "Heading1"))
        facts = " · ".join(x for x in [
            _clean(a.get("source")), _clean(a.get("published_at")),
            _clean(a.get("country")), _clean(a.get("categories")),
            f"importance {a['importance']}" if a.get("importance") is not None else "",
        ] if x)
        if facts:
            parts.append(_para(facts, "Facts"))
        summary = _clean(a.get("summary"))
        if summary:
            parts.append(_para(summary))
        url = _clean(a.get("url"))
        if url:
            parts.append(_para(url, "Facts"))

    document = (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                f"<w:document {_W}><w:body>{''.join(parts)}"
                '<w:sectPr><w:pgSz w:w="11906" w:h="16838"/>'
                '<w:pgMar w:top="1134" w:right="1134" w:bottom="1134" w:left="1134"/>'
                "</w:sectPr></w:body></w:document>")

    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", _DOCX_CONTENT_TYPES)
        z.writestr("_rels/.rels", _DOCX_RELS)
        z.writestr("word/styles.xml", _DOCX_STYLES)
        z.writestr("word/document.xml", document)
        z.writestr("word/_rels/document.xml.rels",
                   '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                   '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
                   "</Relationships>")
    return out.getvalue()


BUILDERS = {"csv": to_csv, "xlsx": to_xlsx, "docx": to_docx,
            "md": to_markdown, "json": to_json}


def build(fmt: str, name: str, articles: list[dict]) -> tuple[bytes, str, str]:
    """Return (bytes, media type, filename) for one export."""
    if fmt not in BUILDERS:
        raise ValueError(f"Unknown export format {fmt!r} — choose one of "
                         + ", ".join(sorted(BUILDERS)))
    media, extension = FORMATS[fmt]
    return BUILDERS[fmt](name, articles), media, f"{safe_filename(name)}.{extension}"


def safe_filename(name: str) -> str:
    """A filename every operating system will accept, from a feed's own name."""
    keep = [ch if (ch.isalnum() or ch in " -_") else " " for ch in name]
    cleaned = " ".join("".join(keep).split()).strip()
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return f"{(cleaned or 'delphi-feed')[:60]} {stamp}".replace(" ", "-")
