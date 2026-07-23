"""Resume Builder - a small Flask web app.

Paste a job description, click Generate. The app keeps your stable details
(contact info, employers, periods, education) fixed and uses Vertex AI (Gemini)
to write job-tailored bullet points, summary and skills, then returns a PDF.
"""

from __future__ import annotations

import io
import re
from datetime import datetime

from flask import Flask, render_template, request, send_file

from resume.config import load_profile, load_vertex_settings
from resume.generator import GenerationError, generate, generate_cover_letter
from resume.pdf import (
    render_cover_letter_html,
    render_cover_letter_pdf,
    render_html,
    render_pdf,
)

app = Flask(__name__)


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "resume"


@app.get("/")
def index():
    profile = load_profile()
    vertex = load_vertex_settings()
    return render_template(
        "index.html",
        profile=profile,
        vertex_ready=bool(vertex.project),
        vertex=vertex,
    )


@app.post("/generate")
def generate_route():
    profile = load_profile()
    vertex = load_vertex_settings()
    job_description = request.form.get("job_description", "")
    want = request.form.get("format", "pdf")

    try:
        resume = generate(profile, job_description, vertex)
    except GenerationError as exc:
        return render_template(
            "index.html",
            profile=profile,
            vertex_ready=bool(vertex.project),
            vertex=vertex,
            error=str(exc),
            job_description=job_description,
        ), 400

    if want == "preview":
        # Return the resume HTML so it can be shown inline in the browser.
        return render_html(profile, resume)

    pdf_bytes = render_pdf(profile, resume)
    filename = f"{_slug(profile.contact.full_name)}-resume-{datetime.now():%Y%m%d}.pdf"
    return send_file(
        io.BytesIO(pdf_bytes),
        mimetype="application/pdf",
        as_attachment=True,
        download_name=filename,
    )


@app.post("/cover-letter")
def cover_letter_route():
    profile = load_profile()
    vertex = load_vertex_settings()
    job_description = request.form.get("job_description", "")
    want = request.form.get("format", "pdf")
    letter_date = f"{datetime.now():%d %B %Y}"

    try:
        body = generate_cover_letter(profile, job_description, vertex)
    except GenerationError as exc:
        # Plain-text 400 so the browser's fetch() can show the message inline.
        return str(exc), 400, {"Content-Type": "text/plain; charset=utf-8"}

    if want == "preview":
        return render_cover_letter_html(profile, body, letter_date)

    pdf_bytes = render_cover_letter_pdf(profile, body, letter_date)
    filename = f"{_slug(profile.contact.full_name)}-cover-letter-{datetime.now():%Y%m%d}.pdf"
    return send_file(
        io.BytesIO(pdf_bytes),
        mimetype="application/pdf",
        as_attachment=True,
        download_name=filename,
    )


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=True)
