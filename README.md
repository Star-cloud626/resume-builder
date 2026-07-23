# Resume Builder

A small Flask web app that keeps **your fixed details** (name, contact info,
employers, periods, education) constant and uses **Vertex AI (Gemini)** to write
job-tailored bullet points, a summary and a skills list for a specific job
description. Output is a print-ready **PDF**.

You give it one input — a **job description** — and it produces a resume tailored
to that role, using the same real employers and dates every time.

## How your data is split

| Data | Where it lives | Who writes it |
| --- | --- | --- |
| Name, headline, phone, email, LinkedIn, location | `.env` | You |
| Employers, job titles, periods, education, base skills | `profile.json` | You |
| Summary, per-job bullet points, tailored skill ordering | generated | Gemini |

> Flat contact fields live in `.env` as you asked. The work-history *skeleton*
> (multiple companies, each with a title and period) and education are structured
> lists, so they live in `profile.json` — still fully under your control and never
> altered by the AI. Only the bullet text is generated.

## Setup

```bash
cd "resume builder"

# 1. Create a virtual environment and install dependencies
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 2. Configure your details
cp .env.example .env      # then edit .env
#   - fill in your contact info
#   - set GOOGLE_CLOUD_PROJECT to your GCP project id
# Edit profile.json with your real work history and education.

# 3. Authenticate to Google Cloud (one of):
gcloud auth application-default login
#   ...or set GOOGLE_APPLICATION_CREDENTIALS to a service-account key path in .env
```

Enable the Vertex AI API on your GCP project once:
`gcloud services enable aiplatform.googleapis.com`.

## Run

```bash
.venv/bin/python app.py
# open http://127.0.0.1:5000
```

Paste a job description, then:

- **Preview** — renders the tailored resume inline in the browser.
- **Download PDF** — generates and downloads the PDF.

## Project layout

```
app.py                 Flask routes (/, /generate)
profile.json           Your stable work history + education + base skills
.env                   Your contact info + Vertex AI settings (not committed)
resume/config.py       Loads .env + profile.json
resume/generator.py    Calls Vertex AI (Gemini), returns structured resume
resume/pdf.py          Jinja2 + WeasyPrint -> PDF
templates/index.html   Web UI
templates/resume.html  Resume layout / print styles
```

## Notes

- Model and region are configurable in `.env` (`GEMINI_MODEL`,
  `GOOGLE_CLOUD_LOCATION`). Default: `gemini-2.5-flash` in `us-central1`.
- The model is instructed **not** to invent or rename employers — it only writes
  bullets for the jobs you list, in order.
- PDF rendering uses WeasyPrint, which needs pango/cairo system libraries
  (already present on most Linux desktops).
