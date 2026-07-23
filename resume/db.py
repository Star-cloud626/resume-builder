"""SQLite storage for candidate profiles.

Each *person* is a saved candidate the client can pick from. A person owns their
contact details, a work-history skeleton (companies/titles/periods) and education
entries. The AI still only writes bullets/summary/skills at generation time - the
facts stored here are never invented.

The store is a single ``resume.db`` file next to the app. On first run it is
seeded from the legacy ``.env`` contact + ``profile.json`` so nothing is lost.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .config import (
    Contact,
    Education,
    ExperienceSkeleton,
    Profile,
    load_profile,
)

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "resume.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS people (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    full_name   TEXT NOT NULL,
    headline    TEXT NOT NULL DEFAULT '',
    phone       TEXT NOT NULL DEFAULT '',
    email       TEXT NOT NULL DEFAULT '',
    linkedin    TEXT NOT NULL DEFAULT '',
    location    TEXT NOT NULL DEFAULT '',
    base_skills TEXT NOT NULL DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS experience (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    person_id  INTEGER NOT NULL REFERENCES people(id) ON DELETE CASCADE,
    company    TEXT NOT NULL DEFAULT '',
    title      TEXT NOT NULL DEFAULT '',
    period     TEXT NOT NULL DEFAULT '',
    location   TEXT NOT NULL DEFAULT '',
    sort_order INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS education (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    person_id   INTEGER NOT NULL REFERENCES people(id) ON DELETE CASCADE,
    institution TEXT NOT NULL DEFAULT '',
    degree      TEXT NOT NULL DEFAULT '',
    period      TEXT NOT NULL DEFAULT '',
    location    TEXT NOT NULL DEFAULT '',
    sort_order  INTEGER NOT NULL DEFAULT 0
);
"""


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    """Create tables if missing and seed the first person from legacy files."""
    with _connect() as conn:
        conn.executescript(_SCHEMA)
        count = conn.execute("SELECT COUNT(*) FROM people").fetchone()[0]
    if count == 0:
        _seed_from_legacy()


def _seed_from_legacy() -> None:
    """Import the original .env contact + profile.json into the DB (once)."""
    legacy = load_profile()
    # Only seed if there's a real name to carry over.
    if not legacy.contact.full_name or legacy.contact.full_name == "Your Name":
        return
    create_person(
        contact=legacy.contact,
        base_skills=legacy.base_skills,
        experience=[
            {"company": e.company, "title": e.title, "period": e.period, "location": e.location}
            for e in legacy.experience
        ],
        education=[
            {"institution": e.institution, "degree": e.degree, "period": e.period, "location": e.location}
            for e in legacy.education
        ],
    )


# --- Reads ------------------------------------------------------------------


def list_people() -> list[dict]:
    """Lightweight list for the selector: id, full_name, headline."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id, full_name, headline FROM people ORDER BY full_name COLLATE NOCASE"
        ).fetchall()
    return [dict(r) for r in rows]


def get_profile(person_id: int) -> Profile | None:
    with _connect() as conn:
        p = conn.execute("SELECT * FROM people WHERE id = ?", (person_id,)).fetchone()
        if p is None:
            return None
        exp = conn.execute(
            "SELECT * FROM experience WHERE person_id = ? ORDER BY sort_order, id",
            (person_id,),
        ).fetchall()
        edu = conn.execute(
            "SELECT * FROM education WHERE person_id = ? ORDER BY sort_order, id",
            (person_id,),
        ).fetchall()

    return Profile(
        id=p["id"],
        contact=Contact(
            full_name=p["full_name"],
            headline=p["headline"],
            phone=p["phone"],
            email=p["email"],
            linkedin=p["linkedin"],
            location=p["location"],
        ),
        experience=[
            ExperienceSkeleton(
                company=e["company"], title=e["title"], period=e["period"], location=e["location"]
            )
            for e in exp
        ],
        education=[
            Education(
                institution=e["institution"], degree=e["degree"], period=e["period"], location=e["location"]
            )
            for e in edu
        ],
        base_skills=json.loads(p["base_skills"] or "[]"),
    )


def first_person_id() -> int | None:
    people = list_people()
    return people[0]["id"] if people else None


# --- Writes -----------------------------------------------------------------


def create_person(
    contact: Contact,
    base_skills: list[str],
    experience: list[dict],
    education: list[dict],
) -> int:
    with _connect() as conn:
        cur = conn.execute(
            """INSERT INTO people (full_name, headline, phone, email, linkedin, location, base_skills)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                contact.full_name,
                contact.headline,
                contact.phone,
                contact.email,
                contact.linkedin,
                contact.location,
                json.dumps(base_skills),
            ),
        )
        person_id = cur.lastrowid
        _insert_children(conn, person_id, experience, education)
        conn.commit()
    return person_id


def update_person(
    person_id: int,
    contact: Contact,
    base_skills: list[str],
    experience: list[dict],
    education: list[dict],
) -> None:
    with _connect() as conn:
        conn.execute(
            """UPDATE people SET full_name=?, headline=?, phone=?, email=?, linkedin=?,
               location=?, base_skills=? WHERE id=?""",
            (
                contact.full_name,
                contact.headline,
                contact.phone,
                contact.email,
                contact.linkedin,
                contact.location,
                json.dumps(base_skills),
                person_id,
            ),
        )
        conn.execute("DELETE FROM experience WHERE person_id=?", (person_id,))
        conn.execute("DELETE FROM education WHERE person_id=?", (person_id,))
        _insert_children(conn, person_id, experience, education)
        conn.commit()


def delete_person(person_id: int) -> None:
    with _connect() as conn:
        conn.execute("DELETE FROM people WHERE id=?", (person_id,))
        conn.commit()


def _insert_children(
    conn: sqlite3.Connection, person_id: int, experience: list[dict], education: list[dict]
) -> None:
    for i, e in enumerate(experience):
        conn.execute(
            """INSERT INTO experience (person_id, company, title, period, location, sort_order)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (person_id, e.get("company", ""), e.get("title", ""), e.get("period", ""),
             e.get("location", ""), i),
        )
    for i, e in enumerate(education):
        conn.execute(
            """INSERT INTO education (person_id, institution, degree, period, location, sort_order)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (person_id, e.get("institution", ""), e.get("degree", ""), e.get("period", ""),
             e.get("location", ""), i),
        )
