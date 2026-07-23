# Resume Builder

A small Flask web app that stores **candidate profiles** (name, contact info,
employers, periods, education, base skills) and uses **Vertex AI (Gemini)** to
tailor three documents to a specific job description:

- a **resume** — job-tailored summary, per-job achievement bullets (with key terms
  **bolded**), and a prioritised skills list, flowing to **2–3 pages**;
- a **cover letter** — a concise, grounded body;
- **application answers** — paste screening questions (one per line) and Gemini
  writes a 3–5 sentence answer to each.

Multiple people can be saved. Pick a candidate, paste a job description, and
generate — the candidate's real employers and dates stay fixed every time.

## How your data is split

| Data | Where it lives | Who writes it |
| --- | --- | --- |
| People: contact info, employers, titles, periods, education, base skills | `resume.db` (SQLite) | You, via the web UI |
| Vertex AI project / region / model | `.env` | You |
| Summaries, per-job bullets, tailored skills, cover letter, answers | generated | Gemini |

> Candidate facts live in a local **SQLite** database (`resume.db`) and are managed
> through the app — add, edit and delete people, and select which one to generate
> for. The AI only ever writes the prose; it never invents or renames employers.
> On first run the database is seeded once from any legacy `.env` contact +
> `profile.json` so nothing is lost.

## Setup

```bash
cd "resume-builder"

# 1. Create a virtual environment and install dependencies
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 2. Configure Vertex AI
cp .env.example .env      # then edit .env
#   - set GOOGLE_CLOUD_PROJECT to your GCP project id
#   (the FULL_NAME/contact fields in .env are only used to seed the FIRST
#    person on a fresh database — after that, manage people in the web UI)

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

1. **+ New person** — add a candidate (contact, work history, education, skills).
2. Pick the candidate from the selector; **Edit** / **Delete** as needed.
3. Paste a job description (and optionally screening questions).
4. For each of **Resume**, **Cover letter** and **Answers**:
   - **Preview** — renders inline in the browser.
   - **Download PDF** — generates and downloads the PDF.

## Project layout

```
app.py                 Flask routes (home, generation, person CRUD)
resume/db.py           SQLite storage for people (CRUD + first-run seed)
resume/config.py       Loads Vertex settings from .env; profile dataclasses
resume/generator.py    Calls Vertex AI (Gemini): resume, cover letter, answers
resume/pdf.py          Jinja2 + WeasyPrint -> PDF (incl. **bold** keyword filter)
templates/base.html    Shared web-UI shell + styles
templates/index.html   Home: candidate picker + generation
templates/person_form.html  Add / edit a candidate
templates/resume.html       Resume layout (bundled Inter + Source Serif fonts)
templates/cover_letter.html Cover-letter layout
templates/answers.html      Application-answers layout
assets/fonts/          Bundled Inter + Source Serif 4 (embedded into the PDFs)
```

## Notes

- Model and region are configurable in `.env` (`GEMINI_MODEL`,
  `GOOGLE_CLOUD_LOCATION`). Default: `gemini-2.5-flash` in `us-central1`.
- The model is instructed **not** to invent or rename employers — it only writes
  bullets for the jobs you list, in order, and bolds 2–4 key terms per bullet.
- PDFs embed the bundled **Inter** and **Source Serif 4** typefaces, so they render
  identically everywhere. Both are SIL Open Font License.
- PDF rendering uses WeasyPrint, which needs pango/cairo system libraries
  (already present on most Linux desktops).
- `resume.db` is git-ignored (it holds real candidate data).
