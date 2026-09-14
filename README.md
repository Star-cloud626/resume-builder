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

## Outreach (admin only)

The **Outreach** page emails a list of people from a Google Sheet — one message
per person, sent one at a time from your own mailbox, with duplicate checking
before anything goes out.

### One-time setup

1. **Mail account.** Add the `SMTP_*` block from `.env.example` to your `.env`.
   Gmail requires an **app password** (Google Account → Security → 2-Step
   Verification → App passwords); a normal account password is rejected.
   Behind a VPN that fakes DNS answers for mail servers (Astrill does), sending
   times out. Set `SMTP_RESOLVER="doh"`: the app then looks the server up over
   Google DNS-over-HTTPS and connects to that address, still verifying the TLS
   certificate against `smtp.gmail.com`.
2. **Sheet access.** The page reads and writes the sheet with the same service
   account as Vertex. Enable the Sheets API once
   (`gcloud services enable sheets.googleapis.com`) and share the spreadsheet
   with the key file's `client_email` as an **Editor**.

### The sheet

Row 1 is the header row. Columns are found by name — a header containing
`email`, one containing `linkedin`, and one containing `status` — or you can type
the column letters on the page instead. If there is no status column, the next
free column is used.

| | A | B | C | D |
| --- | --- | --- | --- | --- |
| **1** | Name | Email | LinkedIn | Status |
| **2** | Ada | ada@example.com | linkedin.com/in/ada | |

### A run

Enter the sheet URL, the row range (e.g. 100 to 200), a subject and a message,
then:

- **Check the range** — reads the sheet and shows what *would* happen. Sends
  nothing, writes nothing.
- **Start sending** — works down the range one row at a time. Progress updates
  live, and **Stop** halts after the current message; rows it never reached keep
  an empty status cell.

Before each send the address is checked, and the row is **skipped** if it is:

- blank or unreadable → marked `invalid`;
- already seen earlier in the same range → marked `duplicated`;
- already emailed successfully in an earlier run → marked `duplicated`.

Every outcome is written back into the sheet's status column and recorded in
`resume.db`, which is what the "earlier run" check reads. Skipped rows are
stamped in a single batched call, so a range full of duplicates doesn't burn
through the Sheets write quota.

Each message is an ordinary email from the configured account: one real `To:`,
plain text, no BCC list. The gap between sends (default 8s) is there to stay
within provider rate limits — Gmail in particular caps daily sends, and a large
range should be split across days.

### Testing it without sending anything

`tests/test_outreach.py` runs the whole flow against a fake spreadsheet, a fake
mail server and a throwaway database — no Google account, no mailbox, no network:

```bash
.venv/bin/python tests/test_outreach.py      # .venv/Scripts/python.exe on Windows
```

For a live rehearsal, make a scratch sheet of addresses you own (Gmail treats
`you+a@gmail.com` and `you+b@gmail.com` as separate addresses that all arrive in
your own inbox), include a deliberate repeat, and run the range with **Check the
range** first.

## Project layout

```
app.py                 Flask routes (home, generation, person CRUD, outreach)
resume/db.py           SQLite storage for people (CRUD + first-run seed)
resume/config.py       Loads Vertex settings from .env; profile dataclasses
resume/generator.py    Calls Vertex AI (Gemini): resume, cover letter, answers
resume/pdf.py          Jinja2 + WeasyPrint -> PDF (incl. **bold** keyword filter)
resume/sheets.py       Google Sheets read/write over the service-account key
resume/outreach.py     Duplicate checking + one-at-a-time SMTP sending
templates/base.html    Shared web-UI shell + styles
templates/index.html   Home: candidate picker + generation
templates/person_form.html  Add / edit a candidate
templates/resume.html       Resume layout (bundled Inter + Source Serif fonts)
templates/cover_letter.html Cover-letter layout
templates/answers.html      Application-answers layout
templates/outreach.html     Outreach: sheet range, message, live progress
tests/test_outreach.py      Offline test of the outreach flow (fakes, no network)
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
- `resume.db` is git-ignored (it holds real candidate data, and now the outreach
  send log).
- Outreach is **admin-only**: every account would send from the single mailbox in
  `.env`, so it isn't exposed to client accounts. The duplicate check spans all
  users for the same reason.
