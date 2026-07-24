"""SQLite storage for users (auth/RBAC) and candidate profiles.

Two kinds of record:

  * ``users``  - login accounts. Role is ``admin`` or ``user``. Admins manage
                 other accounts and every person; users manage only their own.
  * ``people`` - saved candidate profiles, each *owned* by the user who created
                 it (``owner_id``). The AI only writes bullets/summary/skills at
                 generation time - the facts stored here are never invented.

The store is a single ``resume.db`` file. On first run it creates a default admin
(from ``.env``) and seeds the legacy ``.env`` contact + ``profile.json`` as that
admin's first person so nothing is lost.

Passwords are stored as-is (plaintext) by request. All password handling goes
through ``_store_password`` / ``_password_matches`` so switching to hashing later
is a two-line change (e.g. werkzeug ``generate_password_hash`` /
``check_password_hash``).
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
    load_auth_settings,
    load_profile,
)

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "resume.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    email    TEXT NOT NULL UNIQUE,
    password TEXT NOT NULL DEFAULT '',
    role     TEXT NOT NULL DEFAULT 'user'
);

CREATE TABLE IF NOT EXISTS people (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id    INTEGER REFERENCES users(id) ON DELETE CASCADE,
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


# --- Password handling (swap these two to add hashing later) ----------------


def _store_password(raw: str) -> str:
    return raw


def _password_matches(stored: str, given: str) -> bool:
    return stored == given


# --- Init & migration -------------------------------------------------------


def init_db() -> None:
    """Create tables, migrate old databases, seed the default admin + legacy person."""
    with _connect() as conn:
        conn.executescript(_SCHEMA)
    _migrate_owner_column()
    admin_id = _ensure_admin()
    _seed_from_legacy(admin_id)
    _assign_orphans(admin_id)


def _migrate_owner_column() -> None:
    """Add people.owner_id to databases created before RBAC existed."""
    with _connect() as conn:
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(people)").fetchall()]
        if "owner_id" not in cols:
            conn.execute("ALTER TABLE people ADD COLUMN owner_id INTEGER")
            conn.commit()


def _ensure_admin() -> int:
    """Return an admin id, creating the default admin if no users exist yet."""
    with _connect() as conn:
        row = conn.execute("SELECT id FROM users WHERE role='admin' ORDER BY id LIMIT 1").fetchone()
        if row:
            return row["id"]
    auth = load_auth_settings()
    return create_user(auth.admin_email, auth.admin_password, role="admin")


def _seed_from_legacy(owner_id: int) -> None:
    """Import the original .env contact + profile.json once, owned by the admin."""
    with _connect() as conn:
        has_people = conn.execute("SELECT 1 FROM people LIMIT 1").fetchone()
    if has_people:
        return
    legacy = load_profile()
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
        owner_id=owner_id,
    )


def _assign_orphans(owner_id: int) -> None:
    """Give any pre-RBAC people (no owner) to the admin."""
    with _connect() as conn:
        conn.execute("UPDATE people SET owner_id=? WHERE owner_id IS NULL", (owner_id,))
        conn.commit()


# --- Users ------------------------------------------------------------------


def verify_credentials(email: str, password: str) -> dict | None:
    user = get_user_by_email(email)
    if user and _password_matches(user["password"], password):
        return user
    return None


def get_user(user_id: int) -> dict | None:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    return dict(row) if row else None


def get_user_by_email(email: str) -> dict | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE email=? COLLATE NOCASE", (email.strip(),)
        ).fetchone()
    return dict(row) if row else None


def list_users() -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id, email, role FROM users ORDER BY role, email COLLATE NOCASE"
        ).fetchall()
    return [dict(r) for r in rows]


def count_admins() -> int:
    with _connect() as conn:
        return conn.execute("SELECT COUNT(*) FROM users WHERE role='admin'").fetchone()[0]


def create_user(email: str, password: str, role: str = "user") -> int:
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO users (email, password, role) VALUES (?, ?, ?)",
            (email.strip(), _store_password(password), role),
        )
        conn.commit()
        return cur.lastrowid


def update_user(user_id: int, email: str, role: str, password: str | None = None) -> None:
    """Update account; only change the password when a non-empty one is given."""
    with _connect() as conn:
        if password:
            conn.execute(
                "UPDATE users SET email=?, role=?, password=? WHERE id=?",
                (email.strip(), role, _store_password(password), user_id),
            )
        else:
            conn.execute(
                "UPDATE users SET email=?, role=? WHERE id=?",
                (email.strip(), role, user_id),
            )
        conn.commit()


def delete_user(user_id: int) -> None:
    with _connect() as conn:
        # Remove the user's candidate profiles explicitly (their experience/education
        # cascade via the people FK). Done in-app so it works even on databases
        # where owner_id was added by migration without a foreign key.
        conn.execute("DELETE FROM people WHERE owner_id=?", (user_id,))
        conn.execute("DELETE FROM users WHERE id=?", (user_id,))
        conn.commit()


# --- People (scoped by owner) -----------------------------------------------


def list_people(owner_id: int | None = None) -> list[dict]:
    """People for the selector. ``owner_id=None`` returns everyone's (admin view)."""
    with _connect() as conn:
        if owner_id is None:
            rows = conn.execute(
                "SELECT id, full_name, headline, owner_id FROM people "
                "ORDER BY full_name COLLATE NOCASE"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, full_name, headline, owner_id FROM people WHERE owner_id=? "
                "ORDER BY full_name COLLATE NOCASE",
                (owner_id,),
            ).fetchall()
    return [dict(r) for r in rows]


def get_person_owner(person_id: int) -> int | None:
    with _connect() as conn:
        row = conn.execute("SELECT owner_id FROM people WHERE id=?", (person_id,)).fetchone()
    return row["owner_id"] if row else None


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


def first_person_id(owner_id: int | None = None) -> int | None:
    people = list_people(owner_id)
    return people[0]["id"] if people else None


def create_person(
    contact: Contact,
    base_skills: list[str],
    experience: list[dict],
    education: list[dict],
    owner_id: int,
) -> int:
    with _connect() as conn:
        cur = conn.execute(
            """INSERT INTO people (owner_id, full_name, headline, phone, email, linkedin, location, base_skills)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                owner_id,
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
