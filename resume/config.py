"""Loads the stable resume data.

Two sources, both edited by *you* (never by the AI):

  * ``.env``        - flat personal / contact fields + Vertex AI settings.
  * ``profile.json`` - structured lists that don't fit in a flat .env file
                        (education entries and the work-history skeleton:
                        each company name, title and period).

The AI only ever writes the *bullet points* for each job, the summary and
the tailored skills - it never touches anything loaded here.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
PROFILE_PATH = ROOT / "profile.json"

load_dotenv(ROOT / ".env")


def _normalize_credentials() -> None:
    """Make GOOGLE_APPLICATION_CREDENTIALS portable across machines/OSes.

    Google's auth libraries read this env var as a file path. To avoid having to
    hand-edit an absolute path per machine:
      * a relative path is resolved against the project root;
      * if the configured path doesn't exist (e.g. a stale path from another OS),
        or the var is unset, fall back to ``vertex-sa.json`` in the project root.
    So dropping the key file next to ``app.py`` just works anywhere.
    """
    cred = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "").strip().strip('"')
    fallback = ROOT / "vertex-sa.json"
    if cred:
        path = Path(cred)
        if not path.is_absolute():
            path = ROOT / cred
        if path.exists():
            os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = str(path)
            return
    if fallback.exists():
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = str(fallback)


_normalize_credentials()


@dataclass
class Contact:
    full_name: str
    headline: str
    phone: str
    email: str
    linkedin: str
    location: str


@dataclass
class ExperienceSkeleton:
    """Stable facts about a job. Bullets are filled in later by the AI."""

    company: str
    title: str
    period: str
    location: str = ""


@dataclass
class Education:
    institution: str
    degree: str
    period: str
    location: str = ""


@dataclass
class VertexSettings:
    project: str
    location: str
    model: str


@dataclass
class AuthSettings:
    """Session secret + the default admin seeded on first run."""

    secret_key: str
    admin_email: str
    admin_password: str


@dataclass
class Profile:
    contact: Contact
    experience: list[ExperienceSkeleton] = field(default_factory=list)
    education: list[Education] = field(default_factory=list)
    base_skills: list[str] = field(default_factory=list)
    id: int | None = None  # DB primary key when loaded from the store


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def load_contact() -> Contact:
    return Contact(
        full_name=_env("FULL_NAME", "Your Name"),
        headline=_env("HEADLINE", ""),
        phone=_env("PHONE", ""),
        email=_env("EMAIL", ""),
        linkedin=_env("LINKEDIN", ""),
        location=_env("LOCATION", ""),
    )


def load_vertex_settings() -> VertexSettings:
    return VertexSettings(
        project=_env("GOOGLE_CLOUD_PROJECT"),
        location=_env("GOOGLE_CLOUD_LOCATION", "us-central1"),
        model=_env("GEMINI_MODEL", "gemini-2.5-flash"),
    )


def load_auth_settings() -> AuthSettings:
    return AuthSettings(
        secret_key=_env("FLASK_SECRET_KEY", "dev-insecure-change-me"),
        admin_email=_env("ADMIN_EMAIL", "admin@example.com"),
        admin_password=_env("ADMIN_PASSWORD", "admin"),
    )


def load_profile() -> Profile:
    contact = load_contact()

    data: dict = {}
    if PROFILE_PATH.exists():
        data = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))

    experience = [
        ExperienceSkeleton(
            company=e.get("company", ""),
            title=e.get("title", ""),
            period=e.get("period", ""),
            location=e.get("location", ""),
        )
        for e in data.get("experience", [])
    ]
    education = [
        Education(
            institution=e.get("institution", ""),
            degree=e.get("degree", ""),
            period=e.get("period", ""),
            location=e.get("location", ""),
        )
        for e in data.get("education", [])
    ]

    return Profile(
        contact=contact,
        experience=experience,
        education=education,
        base_skills=list(data.get("skills", [])),
    )
