"""Resume Builder - a small Flask web app.

Log in, pick a saved candidate, paste a job description, and Gemini writes
job-tailored bullets, a summary, tailored skills, a cover letter, and answers to
application questions - then returns print-ready PDFs.

Access control (RBAC):
  * ``admin`` - full access, plus user management (CRUD client accounts) and
    every candidate profile.
  * ``user``  - may log in, add/edit/delete their OWN candidate profiles, and
    generate resumes / cover letters / answers for them.

Candidate facts are stored per person (owned by a user) in a local SQLite
database and are never invented by the AI.
"""

from __future__ import annotations

import io
import re
import uuid
from collections import OrderedDict
from datetime import datetime
from functools import wraps
from pathlib import Path

from flask import (
    Flask,
    abort,
    make_response,
    redirect,
    render_template,
    request,
    send_file,
    send_from_directory,
    session,
    url_for,
)

from resume import db
from resume.config import Contact, load_auth_settings, load_vertex_settings
from resume.generator import (
    GenerationError,
    generate,
    generate_answers,
    generate_cover_letter,
    parse_questions,
)
from resume.pdf import (
    html_to_pdf,
    render_answers_html,
    render_cover_letter_html,
    render_html,
)

app = Flask(__name__)
app.secret_key = load_auth_settings().secret_key
db.init_db()

ROOT = Path(__file__).resolve().parent
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


# --- Auth helpers -----------------------------------------------------------


def current_user() -> dict | None:
    uid = session.get("user_id")
    return db.get_user(uid) if uid else None


def is_admin(user: dict | None) -> bool:
    return bool(user and user["role"] == "admin")


@app.context_processor
def inject_user():
    """Make the logged-in user available to every template (for the nav)."""
    return {"current_user": current_user()}


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not current_user():
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)

    return wrapped


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        user = current_user()
        if not user:
            return redirect(url_for("login", next=request.path))
        if not is_admin(user):
            abort(403)
        return view(*args, **kwargs)

    return wrapped


def _can_access_person(user: dict, person_id: int) -> bool:
    """Admins reach any person; users only their own."""
    if is_admin(user):
        return True
    return db.get_person_owner(person_id) == user["id"]


# --- Static assets ----------------------------------------------------------


@app.get("/assets/<path:filename>")
def assets(filename: str):
    """Serve bundled fonts/images so the browser preview can load them too."""
    return send_from_directory(ROOT / "assets", filename)


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "resume"


# --- Preview cache ----------------------------------------------------------
# Each preview stashes its SERVER-rendered HTML under a random token so the modal
# can download it as a PDF later without calling the AI again. In-memory + capped;
# server-generated HTML only (never client-supplied), so no HTML-injection risk.

_PREVIEW_CACHE: "OrderedDict[str, dict]" = OrderedDict()
_PREVIEW_CACHE_MAX = 60


def _stash_preview(html: str, download_name: str) -> str:
    token = uuid.uuid4().hex
    _PREVIEW_CACHE[token] = {"html": html, "name": download_name}
    while len(_PREVIEW_CACHE) > _PREVIEW_CACHE_MAX:
        _PREVIEW_CACHE.popitem(last=False)
    return token


def _preview_response(html: str, name: str, kind: str):
    """Return the preview HTML and hand back a download token in a header."""
    resp = make_response(html)
    filename = f"{_slug(name)}-{kind}-{datetime.now():%Y%m%d}.pdf"
    resp.headers["X-Preview-Token"] = _stash_preview(html, filename)
    return resp


@app.get("/download/<token>")
@login_required
def download_preview(token: str):
    item = _PREVIEW_CACHE.get(token)
    if not item:
        abort(404)  # preview expired (e.g. server restarted) - re-open it
    return send_file(
        io.BytesIO(html_to_pdf(item["html"])),
        mimetype="application/pdf",
        as_attachment=True,
        download_name=item["name"],
    )


# --- Login / logout ---------------------------------------------------------


@app.get("/login")
def login():
    if current_user():
        return redirect(url_for("index"))
    return render_template("login.html")


@app.post("/login")
def login_submit():
    email = request.form.get("email", "").strip()
    password = request.form.get("password", "")
    if not email or not password:
        return render_template("login.html", error="Enter your email and password.", email=email), 400
    user = db.verify_credentials(email, password)
    if not user:
        return render_template("login.html", error="Incorrect email or password.", email=email), 401
    session.clear()
    session["user_id"] = user["id"]
    dest = request.form.get("next") or request.args.get("next") or url_for("index")
    if not dest.startswith("/"):  # only allow local redirects
        dest = url_for("index")
    return redirect(dest)


@app.get("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# --- Home -------------------------------------------------------------------


def _resolve_person_id(raw: str | None, user: dict) -> int | None:
    """Pick a requested person the user may access, else their first person."""
    owner = None if is_admin(user) else user["id"]
    if raw and raw.isdigit():
        pid = int(raw)
        if db.get_profile(pid) and _can_access_person(user, pid):
            return pid
    return db.first_person_id(owner)


@app.get("/")
@login_required
def index():
    user = current_user()
    vertex = load_vertex_settings()
    people = db.list_people(None if is_admin(user) else user["id"])
    active_id = _resolve_person_id(request.args.get("person"), user)
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


def _load_active_profile(user: dict):
    person_id = _resolve_person_id(request.form.get("person_id"), user)
    if not person_id:
        raise GenerationError("No candidate selected. Add a person first.")
    if not _can_access_person(user, person_id):
        raise GenerationError("You don't have access to that candidate.")
    profile = db.get_profile(person_id)
    if not profile:
        raise GenerationError("Selected candidate no longer exists.")
    return profile


def _text_error(exc: GenerationError):
    """Plain-text 400 so the browser fetch shows the message inline."""
    return str(exc), 400, {"Content-Type": "text/plain; charset=utf-8"}


@app.post("/generate")
@login_required
def generate_route():
    user = current_user()
    vertex = load_vertex_settings()
    job_description = request.form.get("job_description", "")
    try:
        profile = _load_active_profile(user)
        resume = generate(profile, job_description, vertex)
    except GenerationError as exc:
        return _text_error(exc)
    return _preview_response(render_html(profile, resume), profile.contact.full_name, "resume")


@app.post("/cover-letter")
@login_required
def cover_letter_route():
    user = current_user()
    vertex = load_vertex_settings()
    job_description = request.form.get("job_description", "")
    letter_date = f"{datetime.now():%d %B %Y}"
    try:
        profile = _load_active_profile(user)
        body = generate_cover_letter(profile, job_description, vertex)
    except GenerationError as exc:
        return _text_error(exc)
    return _preview_response(
        render_cover_letter_html(profile, body, letter_date),
        profile.contact.full_name,
        "cover-letter",
    )


@app.post("/answers")
@login_required
def answers_route():
    user = current_user()
    vertex = load_vertex_settings()
    job_description = request.form.get("job_description", "")
    questions = parse_questions(request.form.get("questions", ""))
    answer_date = f"{datetime.now():%d %B %Y}"
    try:
        profile = _load_active_profile(user)
        pairs = generate_answers(profile, job_description, questions, vertex)
    except GenerationError as exc:
        return _text_error(exc)
    return _preview_response(
        render_answers_html(profile, pairs, answer_date),
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


def _require_person_access(person_id: int) -> dict:
    """404 for unknown persons; 403 when the user doesn't own it."""
    if not db.get_profile(person_id):
        abort(404)
    user = current_user()
    if not _can_access_person(user, person_id):
        abort(403)
    return user


@app.get("/people/new")
@login_required
def person_new():
    return render_template("person_form.html", profile=None, action=url_for("person_create"))


@app.post("/people/new")
@login_required
def person_create():
    contact, skills, experience, education = _form_to_person()
    if not contact.full_name:
        return render_template(
            "person_form.html", profile=None, action=url_for("person_create"),
            error="Full name is required.",
        ), 400
    person_id = db.create_person(contact, skills, experience, education, owner_id=current_user()["id"])
    return redirect(url_for("index", person=person_id, saved=1))


@app.get("/people/<int:person_id>/edit")
@login_required
def person_edit(person_id: int):
    _require_person_access(person_id)
    profile = db.get_profile(person_id)
    return render_template(
        "person_form.html", profile=profile,
        action=url_for("person_update", person_id=person_id),
    )


@app.post("/people/<int:person_id>/edit")
@login_required
def person_update(person_id: int):
    _require_person_access(person_id)
    contact, skills, experience, education = _form_to_person()
    if not contact.full_name:
        return render_template(
            "person_form.html", profile=db.get_profile(person_id),
            action=url_for("person_update", person_id=person_id),
            error="Full name is required.",
        ), 400
    db.update_person(person_id, contact, skills, experience, education)
    return redirect(url_for("index", person=person_id, saved=1))


@app.post("/people/<int:person_id>/delete")
@login_required
def person_delete(person_id: int):
    _require_person_access(person_id)
    db.delete_person(person_id)
    return redirect(url_for("index", deleted=1))


# --- User management (admin only) -------------------------------------------


def _validate_user_form(email: str, password: str, role: str, *, require_password: bool,
                        existing_id: int | None = None) -> str | None:
    if not EMAIL_RE.match(email):
        return "Enter a valid email address."
    if role not in ("admin", "user"):
        return "Role must be admin or user."
    if require_password and not password:
        return "A password is required."
    other = db.get_user_by_email(email)
    if other and other["id"] != existing_id:
        return "That email is already in use."
    return None


@app.get("/users")
@admin_required
def users_list():
    return render_template("users.html", users=db.list_users(), me=current_user())


@app.get("/users/new")
@admin_required
def user_new():
    return render_template("user_form.html", user=None, action=url_for("user_create"))


@app.post("/users/new")
@admin_required
def user_create():
    email = request.form.get("email", "").strip()
    password = request.form.get("password", "")
    role = request.form.get("role", "user")
    err = _validate_user_form(email, password, role, require_password=True)
    if err:
        return render_template("user_form.html", user=None, action=url_for("user_create"),
                               error=err, form={"email": email, "role": role}), 400
    db.create_user(email, password, role)
    return redirect(url_for("users_list", saved=1))


@app.get("/users/<int:user_id>/edit")
@admin_required
def user_edit(user_id: int):
    user = db.get_user(user_id)
    if not user:
        abort(404)
    return render_template("user_form.html", user=user, action=url_for("user_update", user_id=user_id))


@app.post("/users/<int:user_id>/edit")
@admin_required
def user_update(user_id: int):
    user = db.get_user(user_id)
    if not user:
        abort(404)
    email = request.form.get("email", "").strip()
    password = request.form.get("password", "")
    role = request.form.get("role", "user")
    err = _validate_user_form(email, password, role, require_password=False, existing_id=user_id)
    # Guard against demoting the last admin.
    if not err and user["role"] == "admin" and role != "admin" and db.count_admins() <= 1:
        err = "This is the only admin - keep at least one admin account."
    if err:
        return render_template("user_form.html", user=user, action=url_for("user_update", user_id=user_id),
                               error=err, form={"email": email, "role": role}), 400
    db.update_user(user_id, email, role, password or None)
    return redirect(url_for("users_list", saved=1))


@app.post("/users/<int:user_id>/delete")
@admin_required
def user_delete(user_id: int):
    user = db.get_user(user_id)
    if not user:
        abort(404)
    if user_id == current_user()["id"]:
        abort(400)  # can't delete yourself
    if user["role"] == "admin" and db.count_admins() <= 1:
        abort(400)  # can't delete the last admin
    db.delete_user(user_id)
    return redirect(url_for("users_list", deleted=1))


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=True)
