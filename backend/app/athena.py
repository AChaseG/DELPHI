"""Athena — what a Pantheon has actually covered.

A group writing weekly intelligence reports accumulates a question the reports
cannot answer: *what have we been covering, and how often?* Twenty documents
hold that answer between them and none of them state it. Athena is where a
Pantheon files what it has written, tagged against a vocabulary of its own, so
the pattern over months becomes something to look at rather than something to
remember.

This module is the rules; `main.py` has the endpoints and `models.py` the four
tables. Three things live here because they are the parts worth getting right
in one place:

  · **slugs**, so a theme named twice is one theme;
  · **the shape of what a browser may file**, since documents are parsed in the
    member's own browser and arrive here as JSON somebody could have written by
    hand — every field is bounded and every theme is checked against the
    Pantheon's own list;
  · **the delete paths**, which SQLite will not enforce for us.

**Documents are never stored.** A .docx is opened, read and discarded in the
browser; only the structure it yielded arrives. These are a group's own
intelligence products, and Delphi has no business holding the originals.
"""
from __future__ import annotations

import re
import unicodedata

from sqlalchemy import delete as sa_delete
from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import AthenaDocument, AthenaDomain, AthenaEntry, AthenaTheme

# Bounds on what one upload may file. Generous against real documents — a
# weekly report runs to a dozen topics with a handful of links each — and small
# enough that a malformed or hostile payload cannot fill the volume. A group
# that genuinely needs more files a second document, which is also how the
# coverage matrix expects to see a long report anyway.
MAX_ENTRIES = 200
MAX_LINKS = 60
MAX_TITLE = 300
MAX_BODY = 4000
MAX_THEMES_PER_ENTRY = 12
# Per Pantheon, so one group cannot crowd out the volume for the others.
MAX_DOCUMENTS = 2000
MAX_THEMES = 200

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
KINDS = ("report", "notes")


def slugify(text: str) -> str:
    """A stable id for a theme or domain, from what somebody typed.

    Accents are folded rather than dropped, so "Réputation" and "Reputation"
    are the same theme instead of two rows that look identical in the list and
    split the group's own coverage figures between them.
    """
    folded = unicodedata.normalize("NFKD", str(text or ""))
    ascii_only = "".join(c for c in folded if not unicodedata.combining(c))
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_only.lower()).strip("-")
    return slug[:60] or "untitled"


def unique_slug(db: Session, model, pantheon_id: int, name: str,
                skip_id: int | None = None) -> str:
    """`slugify`, then a numeric suffix if that name is already taken here.

    Renaming a theme to a name another one already has is a mistake worth
    surviving rather than a request worth refusing — the group is mid-thought,
    and an error dialog at that moment loses whatever they were doing.
    """
    base = slugify(name)
    taken = {s for s, i in db.execute(
        select(model.slug, model.id).where(model.pantheon_id == pantheon_id)).all()
        if i != skip_id}
    if base not in taken:
        return base
    for n in range(2, 500):
        candidate = f"{base[:56]}-{n}"
        if candidate not in taken:
            return candidate
    return f"{base[:52]}-x"


def clean_links(raw) -> list[dict]:
    """The sources behind an entry, as {t: title, u: url}.

    Only http(s) survives. A document can carry `file://` and `javascript:`
    hrefs quite innocently — Word writes the first when somebody links a
    network share — and neither belongs in a list of links this board will
    render for every member of the group.
    """
    out = []
    for item in (raw if isinstance(raw, list) else [])[:MAX_LINKS]:
        if not isinstance(item, dict):
            continue
        url = str(item.get("u") or "").strip()
        if not url.lower().startswith(("http://", "https://")):
            continue
        out.append({"t": str(item.get("t") or "").strip()[:300] or url[:300],
                    "u": url[:1000]})
    return out


def clean_entries(raw, known_themes: set[str]) -> list[dict]:
    """The items in one document, bounded and with their themes checked.

    A theme this Pantheon does not have is dropped rather than created. The
    alternative — filing whatever the browser sent — means one careless upload
    can invent a vocabulary the group never agreed on, and the coverage figures
    stop meaning anything the moment that happens.
    """
    out = []
    for position, item in enumerate((raw if isinstance(raw, list) else [])[:MAX_ENTRIES]):
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()[:MAX_TITLE]
        body = str(item.get("body") or "").strip()[:MAX_BODY]
        if not title and not body:
            continue                       # an entry with nothing in it is noise
        themes, seen = [], set()
        for slug in (item.get("themes") if isinstance(item.get("themes"), list) else []):
            slug = str(slug or "").strip()[:60]
            if slug in known_themes and slug not in seen:
                seen.add(slug)
                themes.append(slug)
            if len(themes) >= MAX_THEMES_PER_ENTRY:
                break
        out.append({"title": title, "body": body, "themes": themes,
                    "links": clean_links(item.get("links")), "position": position})
    return out


def theme_slugs(db: Session, pantheon_id: int) -> set[str]:
    return set(db.scalars(select(AthenaTheme.slug).where(
        AthenaTheme.pantheon_id == pantheon_id)))


def delete_documents(db: Session, documents: list[AthenaDocument]) -> int:
    """A document and its entries, together.

    The entries are the whole of a document's substance, and SQLite is not
    enforcing the foreign key that would take them with it (see database.py).
    Deleting the parent alone leaves them in the table for as long as the
    instance runs, counted by nothing and reachable by nothing.
    """
    if not documents:
        return 0
    ids = [d.id for d in documents]
    db.execute(sa_delete(AthenaEntry).where(AthenaEntry.document_id.in_(ids)))
    db.execute(sa_delete(AthenaDocument).where(AthenaDocument.id.in_(ids)))
    return len(ids)


def delete_theme(db: Session, theme: AthenaTheme) -> int:
    """A theme, and every reference to it.

    `AthenaEntry.themes` is a JSON list of slugs, which buys a great deal and
    owes exactly this: a slug left behind after its theme is gone is a tag that
    renders as nothing and counts towards no column. Stripped in the same
    transaction, so the two cannot disagree.
    """
    touched = 0
    documents = db.scalars(select(AthenaDocument.id).where(
        AthenaDocument.pantheon_id == theme.pantheon_id)).all()
    if documents:
        for entry in db.scalars(select(AthenaEntry).where(
                AthenaEntry.document_id.in_(documents))):
            if theme.slug in (entry.themes or []):
                entry.themes = [s for s in entry.themes if s != theme.slug]
                touched += 1
    db.delete(theme)
    return touched


def purge_pantheon(db: Session, pantheon_id: int) -> None:
    """Everything Athena holds for a Pantheon that is ending.

    Called from both ways a Pantheon can end. Written as one function precisely
    because the same class of bug has been fixed on one deletion path and left
    on another twice in this codebase — a second copy of these four deletes is
    a second place to forget one.
    """
    documents = db.scalars(select(AthenaDocument).where(
        AthenaDocument.pantheon_id == pantheon_id)).all()
    delete_documents(db, documents)
    db.execute(sa_delete(AthenaTheme).where(AthenaTheme.pantheon_id == pantheon_id))
    db.execute(sa_delete(AthenaDomain).where(AthenaDomain.pantheon_id == pantheon_id))


def board_json(db: Session, pantheon_id: int) -> dict:
    """The whole board in one response.

    One request rather than three, because every view of it needs all of it:
    the matrix counts entries per theme per week, and it cannot draw a row
    without the themes or a column without the documents. A Pantheon's own
    output is a bounded thing — hundreds of documents at most, by construction
    — so this stays small enough to be simplest as a single answer.
    """
    domains = db.scalars(select(AthenaDomain).where(
        AthenaDomain.pantheon_id == pantheon_id)
        .order_by(AthenaDomain.position, AthenaDomain.id)).all()
    themes = db.scalars(select(AthenaTheme).where(
        AthenaTheme.pantheon_id == pantheon_id)
        .order_by(AthenaTheme.position, AthenaTheme.id)).all()
    documents = db.scalars(select(AthenaDocument).where(
        AthenaDocument.pantheon_id == pantheon_id)
        .order_by(AthenaDocument.date, AthenaDocument.id)).all()

    # Every entry for these documents in one query, then grouped in memory.
    # A query per document is a query per week of the group's history, on a
    # board whose whole point is looking at a year of it at once.
    by_document: dict[int, list[dict]] = {d.id: [] for d in documents}
    if documents:
        for entry in db.scalars(select(AthenaEntry).where(
                AthenaEntry.document_id.in_(list(by_document)))
                .order_by(AthenaEntry.position, AthenaEntry.id)):
            by_document.setdefault(entry.document_id, []).append({
                "id": entry.id, "title": entry.title, "body": entry.body,
                "themes": entry.themes or [], "links": entry.links or [],
            })

    return {
        "domains": [{"slug": d.slug, "name": d.name, "color": d.color}
                    for d in domains],
        "themes": [{"slug": t.slug, "name": t.name, "domain": t.domain,
                    "blurb": t.blurb, "keywords": t.keywords or []}
                   for t in themes],
        "documents": [{"id": d.id, "kind": d.kind, "date": d.date, "week": d.week,
                       "label": d.label, "filename": d.filename,
                       "uploaded_by": d.uploaded_by,
                       "entries": by_document.get(d.id, [])}
                      for d in documents],
    }
