"""Resume Builder - a small Flask web app.

Pick a saved candidate, paste a job description, and Gemini writes job-tailored
bullets, a summary, tailored skills, a cover letter, and answers to application
questions - then returns print-ready PDFs. Candidate facts (contact, employers,
periods, education) are stored per person in a local SQLite database and are never
invented by the AI.
"""

from __future__ import annotations

import io
import re
from datetime import datetime
from pathlib import Path

from flask import (
    Flask,
    abort,
    redirect,
    render_template,
    request,
    send_file,
    send_from_directory,
    url_for,
)

from resume import db
from resume.config import Contact, load_vertex_settings
from resume.generator import (
    GenerationError,
    generate,
    generate_answers,
    generate_cover_letter,
    parse_questions,
)
from resume.pdf import (
    render_answers_html,
    render_answers_pdf,
    render_cover_letter_html,
    render_cover_letter_pdf,
    render_html,
    render_pdf,
)

app = Flask(__name__)
db.init_db()

ROOT = Path(__file__).resolve().parent


@app.get("/assets/<path:filename>")
def assets(filename: str):
    """Serve bundled fonts/images so the browser preview can load them too.

    The templates reference fonts as ``assets/fonts/*.ttf`` (a relative URL).
    WeasyPrint resolves that against the filesystem for the PDF; this route
    resolves the same URL over HTTP for the in-browser preview.
    """
    return send_from_directory(ROOT / "assets", filename)


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "resume"


def _resolve_person_id(raw: str | None) -> int | None:
    """Pick the requested person, falling back to the first one that exists."""
    if raw and raw.isdigit() and db.get_profile(int(raw)):
        return int(raw)
    return db.first_person_id()


def _pdf_response(pdf_bytes: bytes, name: str, kind: str):
    filename = f"{_slug(name)}-{kind}-{datetime.now():%Y%m%d}.pdf"
    return send_file(
        io.BytesIO(pdf_bytes),
        mimetype="application/pdf",
        as_attachment=True,
        download_name=filename,
    )


# --- Home -------------------------------------------------------------------


@app.get("/")
def index():
    vertex = load_vertex_settings()
    people = db.list_people()
    active_id = _resolve_person_id(request.args.get("person"))
    profile = db.get_profile(active_id) if active_id else None
    return render_template(
        "index.html",
        people=people,
        active_id=active_id,
        profile=profile,
        vertex_ready=bool(vertex.project),
        can_generate=bool(vertex.project and profile),
        job_description=request.args.get("job_description", ""),
        questions=request.args.get("questions", ""),
    )


# --- Generation -------------------------------------------------------------


def _load_active_profile():
    person_id = _resolve_person_id(request.form.get("person_id"))
    if not person_id:
        raise GenerationError("No candidate selected. Add a person first.")
    profile = db.get_profile(person_id)
    if not profile:
        raise GenerationError("Selected candidate no longer exists.")
    return profile


def _error_page(exc: GenerationError):
    vertex = load_vertex_settings()
    active_id = _resolve_person_id(request.form.get("person_id"))
    return render_template(
        "index.html",
        people=db.list_people(),
        active_id=active_id,
        profile=db.get_profile(active_id) if active_id else None,
        vertex_ready=bool(vertex.project),
        can_generate=bool(vertex.project and active_id),
        error=str(exc),
        job_description=request.form.get("job_description", ""),
        questions=request.form.get("questions", ""),
    ), 400


@app.post("/generate")
def generate_route():
    vertex = load_vertex_settings()
    job_description = request.form.get("job_description", "")
    want = request.form.get("format", "pdf")
    try:
        profile = _load_active_profile()
        resume = generate(profile, job_description, vertex)
    except GenerationError as exc:
        return _error_page(exc)

    if want == "preview":
        return render_html(profile, resume)
    return _pdf_response(render_pdf(profile, resume), profile.contact.full_name, "resume")


@app.post("/cover-letter")
def cover_letter_route():
    vertex = load_vertex_settings()
    job_description = request.form.get("job_description", "")
    want = request.form.get("format", "pdf")
    letter_date = f"{datetime.now():%d %B %Y}"
    try:
        profile = _load_active_profile()
        body = generate_cover_letter(profile, job_description, vertex)
    except GenerationError as exc:
        if want == "preview":
            return str(exc), 400, {"Content-Type": "text/plain; charset=utf-8"}
        return _error_page(exc)

    if want == "preview":
        return render_cover_letter_html(profile, body, letter_date)
    return _pdf_response(
        render_cover_letter_pdf(profile, body, letter_date),
        profile.contact.full_name,
        "cover-letter",
    )


@app.post("/answers")
def answers_route():
    vertex = load_vertex_settings()
    job_description = request.form.get("job_description", "")
    questions = parse_questions(request.form.get("questions", ""))
    want = request.form.get("format", "pdf")
    answer_date = f"{datetime.now():%d %B %Y}"
    try:
        profile = _load_active_profile()
        pairs = generate_answers(profile, job_description, questions, vertex)
    except GenerationError as exc:
        if want == "preview":
            return str(exc), 400, {"Content-Type": "text/plain; charset=utf-8"}
        return _error_page(exc)

    if want == "preview":
        return render_answers_html(profile, pairs, answer_date)
    return _pdf_response(
        render_answers_pdf(profile, pairs, answer_date),
        profile.contact.full_name,
        "answers",
    )


# --- Person CRUD ------------------------------------------------------------


def _zip_rows(*columns, keys) -> list[dict]:
    rows = []
    for values in zip(*columns):
        row = {k: v.strip() for k, v in zip(keys, values)}
        if any(row.values()):  # drop fully-empty rows
            rows.append(row)
    return rows


def _form_to_person() -> tuple[Contact, list[str], list[dict], list[dict]]:
    f = request.form
    contact = Contact(
        full_name=f.get("full_name", "").strip(),
        headline=f.get("headline", "").strip(),
        phone=f.get("phone", "").strip(),
        email=f.get("email", "").strip(),
        linkedin=f.get("linkedin", "").strip(),
        location=f.get("location", "").strip(),
    )
    skills = [s.strip() for s in re.split(r"[,\n]", f.get("skills", "")) if s.strip()]
    experience = _zip_rows(
        f.getlist("exp_company"), f.getlist("exp_title"),
        f.getlist("exp_period"), f.getlist("exp_location"),
        keys=("company", "title", "period", "location"),
    )
    education = _zip_rows(
        f.getlist("edu_institution"), f.getlist("edu_degree"),
        f.getlist("edu_period"), f.getlist("edu_location"),
        keys=("institution", "degree", "period", "location"),
    )
    return contact, skills, experience, education


@app.get("/people/new")
def person_new():
    return render_template("person_form.html", profile=None, action=url_for("person_create"))


@app.post("/people/new")
def person_create():
    contact, skills, experience, education = _form_to_person()
    if not contact.full_name:
        return render_template(
            "person_form.html", profile=None, action=url_for("person_create"),
            error="Full name is required.",
        ), 400
    person_id = db.create_person(contact, skills, experience, education)
    return redirect(url_for("index", person=person_id, saved=1))


@app.get("/people/<int:person_id>/edit")
def person_edit(person_id: int):
    profile = db.get_profile(person_id)
    if not profile:
        abort(404)
    return render_template(
        "person_form.html", profile=profile,
        action=url_for("person_update", person_id=person_id),
    )


@app.post("/people/<int:person_id>/edit")
def person_update(person_id: int):
    profile = db.get_profile(person_id)
    if not profile:
        abort(404)
    contact, skills, experience, education = _form_to_person()
    if not contact.full_name:
        return render_template(
            "person_form.html", profile=profile,
            action=url_for("person_update", person_id=person_id),
            error="Full name is required.",
        ), 400
    db.update_person(person_id, contact, skills, experience, education)
    return redirect(url_for("index", person=person_id, saved=1))


@app.post("/people/<int:person_id>/delete")
def person_delete(person_id: int):
    db.delete_person(person_id)
    return redirect(url_for("index", deleted=1))


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=True)
