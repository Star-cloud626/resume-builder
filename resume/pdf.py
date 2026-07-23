"""Renders a :class:`TailoredResume` to a print-ready PDF via Jinja2 + WeasyPrint."""

from __future__ import annotations

from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape
from weasyprint import HTML

from .config import Profile
from .generator import TailoredResume

ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = ROOT / "templates"

_env = Environment(
    loader=FileSystemLoader(str(TEMPLATES)),
    autoescape=select_autoescape(["html"]),
)


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
