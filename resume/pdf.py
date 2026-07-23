"""Renders tailored resumes, cover letters and Q&A sheets to print-ready PDFs
via Jinja2 + WeasyPrint."""

from __future__ import annotations

import re
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import Markup, escape
from weasyprint import HTML

from .config import Profile
from .generator import QAPair, TailoredResume

ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = ROOT / "templates"

_BOLD_RE = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)


def md_bold(text: str) -> Markup:
    """Escape ``text`` for HTML, then turn ``**spans**`` into <b> tags.

    Escaping first means any real HTML in the model output is neutralised; only
    our own <b> wrappers survive. Safe to render unescaped in the template.
    """
    escaped = str(escape(text or ""))
    return Markup(_BOLD_RE.sub(r"<b>\1</b>", escaped))


_env = Environment(
    loader=FileSystemLoader(str(TEMPLATES)),
    autoescape=select_autoescape(["html"]),
)
_env.filters["bold"] = md_bold


def render_html(profile: Profile, resume: TailoredResume) -> str:
    template = _env.get_template("resume.html")
    return template.render(contact=profile.contact, resume=resume)


def _document(profile: Profile, resume: TailoredResume):
    html = render_html(profile, resume)
    return HTML(string=html, base_url=str(ROOT)).render()


def render_pdf(profile: Profile, resume: TailoredResume) -> bytes:
    return _document(profile, resume).write_pdf()


def page_count(profile: Profile, resume: TailoredResume) -> int:
    return len(_document(profile, resume).pages)


def render_cover_letter_html(profile: Profile, body: str, date: str = "") -> str:
    template = _env.get_template("cover_letter.html")
    return template.render(contact=profile.contact, body=body, date=date)


def render_cover_letter_pdf(profile: Profile, body: str, date: str = "") -> bytes:
    html = render_cover_letter_html(profile, body, date)
    return HTML(string=html, base_url=str(ROOT)).write_pdf()


def render_answers_html(profile: Profile, pairs: list[QAPair], date: str = "") -> str:
    template = _env.get_template("answers.html")
    return template.render(contact=profile.contact, pairs=pairs, date=date)


def render_answers_pdf(profile: Profile, pairs: list[QAPair], date: str = "") -> bytes:
    html = render_answers_html(profile, pairs, date)
    return HTML(string=html, base_url=str(ROOT)).write_pdf()
