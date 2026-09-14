"""One-at-a-time outreach sender driven by a Google Sheet.

What it does, in order:

  1. Reads a row range (e.g. 100-200) from a Google Sheet.
  2. For every row, checks the address *before* sending:
       * blank / unparseable        -> skipped, marked ``invalid``
       * seen earlier in this range -> skipped, marked ``duplicated``
       * emailed in an earlier run  -> skipped, marked ``duplicated``
  3. Sends the remaining ones individually - one recipient per message, with a
     pause in between - from the mailbox configured in ``.env``.
  4. Writes the outcome back into the sheet's status column and records it
     locally so the next run can dedup against it.

Each message is an ordinary personal email from the configured account: one
real ``To:``, no BCC lists, no header tricks. The pause between sends is there
to be gentle on the mail server, not to disguise anything.
"""

from __future__ import annotations

import re
import smtplib
import socket
import ssl
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid

import requests

from . import db
from .config import SmtpSettings, load_slack_webhook_url
from .sheets import SheetError, SheetRef, Sheets, column_index, column_letter, parse_sheet_url

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")

MAX_ROWS = 500          # guard against a fat-fingered range
MIN_DELAY = 1.0         # seconds between sends
DEFAULT_DELAY = 8.0
HEADER_ROW = 1
_JOB_LIMIT = 20


class OutreachError(RuntimeError):
    """A problem the user can fix (bad range, missing config, no access)."""


# --- Row plan ---------------------------------------------------------------


@dataclass
class RowPlan:
    """One spreadsheet row, and what we decided to do with it."""

    row_number: int
    email: str
    linkedin: str = ""
    status: str = "pending"   # pending | duplicated | invalid | sent | failed | skipped
    detail: str = ""

    @property
    def will_send(self) -> bool:
        return self.status == "pending"


@dataclass
class SheetPlan:
    ref: SheetRef
    tab: str
    status_column: str
    rows: list[RowPlan]
    email_column: str
    linkedin_column: str

    def counts(self) -> dict[str, int]:
        out = {"total": len(self.rows), "pending": 0, "duplicated": 0, "invalid": 0}
        for row in self.rows:
            out[row.status] = out.get(row.status, 0) + 1
        return out


def extract_email(cell: str) -> str:
    """Pull an address out of a cell that may read 'Jane <jane@x.com>'."""
    match = EMAIL_RE.search(cell or "")
    return match.group(0).strip().lower() if match else ""


# --- Column detection -------------------------------------------------------


def _detect(headers: list[str], needles: tuple[str, ...], taken: set[int]) -> int | None:
    """First header matching any needle, ignoring columns already claimed.

    ``taken`` matters because the needles overlap: a header like "Contact Email"
    answers to both the email and the linkedin sniff, and whichever is resolved
    first should keep it.
    """
    for i, head in enumerate(headers):
        low = head.strip().lower()
        if i not in taken and any(n in low for n in needles):
            return i
    return None


def _resolve_column(explicit: str, headers: list[str], needles: tuple[str, ...],
                    label: str, *, required: bool, taken: set[int] | None = None) -> int | None:
    """Use the letter the user typed, else sniff the header row."""
    if explicit.strip():
        return column_index(explicit)
    found = _detect(headers, needles, taken or set())
    if found is None and required:
        raise OutreachError(
            f"Couldn't find the {label} column. Add a '{label}' header in row "
            f"{HEADER_ROW}, or type the column letter."
        )
    return found


# --- Plan building ----------------------------------------------------------


@dataclass
class JobSpec:
    sheet_url: str
    tab: str = ""
    start_row: int = 2
    end_row: int = 2
    email_column: str = ""
    linkedin_column: str = ""
    status_column: str = ""
    subject: str = ""
    body: str = ""
    delay: float = DEFAULT_DELAY
    user_id: int | None = None

    def validate(self) -> None:
        if self.start_row < 1 or self.end_row < 1:
            raise OutreachError("Row numbers start at 1.")
        if self.end_row < self.start_row:
            raise OutreachError("The end row must not be before the start row.")
        if self.end_row - self.start_row + 1 > MAX_ROWS:
            raise OutreachError(f"That's more than {MAX_ROWS} rows - narrow the range.")
        if not self.subject.strip():
            raise OutreachError("Enter a subject.")
        if not self.body.strip():
            raise OutreachError("Enter the message body.")
        if self.delay < MIN_DELAY:
            raise OutreachError(f"Keep at least {MIN_DELAY:g}s between sends.")


def _cell(cells: list[str], index: int | None) -> str:
    if index is None or index >= len(cells):
        return ""
    return cells[index].strip()


def build_plan(spec: JobSpec, client: Sheets | None = None) -> SheetPlan:
    """Read the range and decide, row by row, what happens - without sending."""
    spec.validate()
    client = client or Sheets()
    ref = parse_sheet_url(spec.sheet_url)
    tab = spec.tab.strip() or client.tab_titles(ref)[0]

    header_rows = client.read(ref, tab, f"A{HEADER_ROW}:ZZ{HEADER_ROW}")
    headers = header_rows[0] if header_rows else []
    email_idx = _resolve_column(spec.email_column, headers, ("email", "mail"), "email", required=True)
    taken = {email_idx}
    link_idx = _resolve_column(
        spec.linkedin_column, headers, ("linkedin", "profile", "contact"), "linkedin",
        required=False, taken=taken,
    )
    taken.add(link_idx)
    status_idx = _resolve_column(
        spec.status_column, headers, ("status",), "status", required=False, taken=taken
    )
    if status_idx is None:
        # No status header: park it one column past the widest of header/used columns.
        status_idx = max(len(headers), email_idx + 1, (link_idx or 0) + 1)
    if status_idx == email_idx or status_idx == link_idx:
        raise OutreachError("The status column can't be the email or linkedin column.")

    width = max(email_idx, link_idx or 0, status_idx) + 1
    grid = client.read(ref, tab, f"A{spec.start_row}:{column_letter(width - 1)}{spec.end_row}")

    sent_before = db.already_sent_emails()
    seen_in_range: set[str] = set()
    rows: list[RowPlan] = []

    for offset in range(spec.end_row - spec.start_row + 1):
        cells = grid[offset] if offset < len(grid) else []
        raw = _cell(cells, email_idx)
        plan = RowPlan(
            row_number=spec.start_row + offset,
            email=extract_email(raw),
            linkedin=_cell(cells, link_idx),
        )
        if not raw:
            plan.status, plan.detail = "invalid", "no email in this row"
        elif not plan.email:
            plan.status, plan.detail = "invalid", f"can't read an address from {raw!r}"
        elif plan.email in seen_in_range:
            plan.status, plan.detail = "duplicated", "already in this range"
        elif plan.email in sent_before:
            plan.status, plan.detail = "duplicated", "emailed in an earlier run"
        else:
            seen_in_range.add(plan.email)
        rows.append(plan)

    return SheetPlan(
        ref=ref, tab=tab, status_column=column_letter(status_idx), rows=rows,
        email_column=column_letter(email_idx),
        linkedin_column=column_letter(link_idx) if link_idx is not None else "",
    )


# --- SMTP -------------------------------------------------------------------


DOH_URL = "https://dns.google/resolve"


def resolve_doh(host: str) -> str:
    """Look ``host`` up over DNS-over-HTTPS and return one IPv4 address.

    Some VPNs (Astrill, for one) answer mail-server lookups with the address of
    a proxy that silently drops SMTP, while ordinary HTTPS still gets through.
    Asking Google's resolver over HTTPS sidesteps that. The TLS certificate is
    still checked against ``host``, so a wrong answer fails loudly rather than
    delivering mail somewhere else.
    """
    try:
        resp = requests.get(DOH_URL, params={"name": host, "type": "A"}, timeout=10)
        resp.raise_for_status()
        answers = resp.json().get("Answer", [])
    except (requests.RequestException, ValueError) as exc:
        raise OutreachError(f"Couldn't look up {host} over DNS-over-HTTPS - {exc}") from exc
    addresses = [a["data"] for a in answers if a.get("type") == 1]  # type 1 = A record
    if not addresses:
        raise OutreachError(f"DNS-over-HTTPS returned no address for {host}.")
    return addresses[0]


class _SMTP(smtplib.SMTP):
    """smtplib.SMTP that can dial a pre-resolved ``address`` for its host.

    smtplib keeps the hostname it was given for EHLO and for TLS
    (``server_hostname``); only the TCP connection goes to ``address``.
    """

    address = ""

    def _get_socket(self, host, port, timeout):
        if not self.address:
            return super()._get_socket(host, port, timeout)
        return socket.create_connection((self.address, port), timeout, self.source_address)


class _SMTP_SSL(smtplib.SMTP_SSL, _SMTP):
    """Implicit-TLS variant. SMTP_SSL wraps whatever socket ``_SMTP`` dials."""


class Mailer:
    """A single authenticated SMTP session, reconnecting if the server drops it."""

    def __init__(self, settings: SmtpSettings) -> None:
        if not settings.configured:
            raise OutreachError(
                "Outgoing mail isn't configured. Set SMTP_HOST, SMTP_USER and "
                "SMTP_PASSWORD in .env (Gmail needs an app password)."
            )
        self.settings = settings
        self._conn: smtplib.SMTP | None = None

    def connect(self) -> None:
        self.close()
        cfg = self.settings
        address = resolve_doh(cfg.host) if cfg.resolver == "doh" else ""
        # Verify the server's certificate against the HOSTNAME. This matters
        # doubly when dialing a resolved IP: it proves we reached the real server.
        context = ssl.create_default_context()
        try:
            if cfg.use_ssl:
                conn = _SMTP_SSL(timeout=30, context=context)
            else:
                conn = _SMTP(timeout=30)
            conn.address = address
            conn._host = cfg.host  # smtplib sets this only in __init__; TLS checks the cert against it
            conn.connect(cfg.host, cfg.port)
            if not cfg.use_ssl:
                conn.ehlo()
                conn.starttls(context=context)
                conn.ehlo()
            conn.login(cfg.user, cfg.password)
        except smtplib.SMTPAuthenticationError as exc:
            raise OutreachError(
                "The mail server rejected those credentials. For Gmail use a "
                f"16-character app password, not your account password. ({exc.smtp_code})"
            ) from exc
        except ssl.SSLCertVerificationError as exc:
            server = address or cfg.host
            raise OutreachError(
                f"The server at {server} couldn't prove it is {cfg.host} - refusing to "
                f"send through it. ({exc.verify_message})"
            ) from exc
        except (OSError, smtplib.SMTPException) as exc:
            via = f" (via {address})" if address else ""
            hint = "" if address else (
                " If you're on a VPN such as Astrill, set SMTP_RESOLVER=\"doh\" in .env."
            )
            raise OutreachError(f"Couldn't connect to {cfg.host}:{cfg.port}{via} - {exc}.{hint}") from exc
        self._conn = conn

    def build(self, to: str, subject: str, body: str) -> EmailMessage:
        cfg = self.settings
        msg = EmailMessage()
        msg["From"] = formataddr((cfg.sender_name or None, cfg.sender))
        msg["To"] = to
        msg["Subject"] = subject
        msg["Date"] = formatdate(localtime=True)
        msg["Message-ID"] = make_msgid(domain=cfg.sender.partition("@")[2] or None)
        msg.set_content(body)
        return msg

    def send(self, to: str, subject: str, body: str) -> None:
        """Send one message; one reconnect-and-retry if the session went stale."""
        for attempt in (1, 2):
            if self._conn is None:
                self.connect()
            try:
                self._conn.send_message(self.build(to, subject, body))
                return
            except smtplib.SMTPRecipientsRefused as exc:
                raise OutreachError(f"Server refused the address: {exc.recipients}") from exc
            except (smtplib.SMTPServerDisconnected, smtplib.SMTPConnectError, OSError):
                self._conn = None
                if attempt == 2:
                    raise
            except smtplib.SMTPException as exc:
                raise OutreachError(f"Send failed: {exc}") from exc

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.quit()
            except (smtplib.SMTPException, OSError):
                pass
            self._conn = None


def check_smtp(settings: SmtpSettings) -> str:
    """Log in and hang up - used by the 'Test connection' button."""
    mailer = Mailer(settings)
    mailer.connect()
    mailer.close()
    via = " (address looked up over DNS-over-HTTPS)" if settings.resolver == "doh" else ""
    return f"Signed in to {settings.host} as {settings.user}{via}."


# --- Job runner -------------------------------------------------------------


@dataclass
class Job:
    """A run in progress. Polled by the browser for progress."""

    id: str
    spec: JobSpec
    state: str = "starting"       # starting | running | done | stopped | error
    error: str = ""
    slack: str = ""               # "" (not configured) | "sent" | "not sent: <reason>"
    plan: SheetPlan | None = None
    events: list[dict] = field(default_factory=list)
    started_at: str = field(default_factory=lambda: f"{datetime.now():%Y-%m-%d %H:%M:%S}")
    stop_flag: threading.Event = field(default_factory=threading.Event)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def note(self, row: RowPlan) -> None:
        with self.lock:
            self.events.append({
                "row": row.row_number,
                "email": row.email or "-",
                "status": row.status,
                "detail": row.detail,
                "at": f"{datetime.now():%H:%M:%S}",
            })

    def stop(self) -> None:
        self.stop_flag.set()

    def snapshot(self) -> dict:
        with self.lock:
            events = sorted(self.events, key=lambda e: e["row"])
        tally = {key: sum(1 for e in events if e["status"] == key)
                 for key in ("sent", "duplicated", "invalid", "failed", "skipped")}
        return {
            "id": self.id,
            "state": self.state,
            "error": self.error,
            "total": len(self.plan.rows) if self.plan else 0,
            "done": len(events),
            "events": events,
            "started_at": self.started_at,
            "slack": self.slack,
            **tally,
        }


_JOBS: dict[str, Job] = {}
_JOBS_LOCK = threading.Lock()


def get_job(job_id: str) -> Job | None:
    with _JOBS_LOCK:
        return _JOBS.get(job_id)


def _register(job: Job) -> None:
    """Keep the newest jobs; drop finished ones once the cap is exceeded."""
    with _JOBS_LOCK:
        _JOBS[job.id] = job
        finished = [k for k, v in _JOBS.items() if v.state in ("done", "stopped", "error")]
        for key in finished[:max(0, len(finished) - _JOB_LIMIT)]:
            _JOBS.pop(key, None)


def start_job(spec: JobSpec, settings: SmtpSettings) -> Job:
    """Validate everything up front, then run the sends on a background thread."""
    spec.validate()
    Mailer(settings)  # fails fast when .env is incomplete
    job = Job(id=uuid.uuid4().hex, spec=spec)
    _register(job)
    threading.Thread(target=_run, args=(job, settings), daemon=True).start()
    return job


def _run(job: Job, settings: SmtpSettings) -> None:
    state, error = "done", ""
    try:
        client = Sheets()
        job.plan = build_plan(job.spec, client)
        job.state = "running"
        _process(job, client, settings)
        if job.stop_flag.is_set():
            state = "stopped"
    except (OutreachError, SheetError) as exc:
        state, error = "error", str(exc)
    except Exception as exc:  # noqa: BLE001 - surface anything else in the UI
        state, error = "error", f"Unexpected error: {exc}"

    job.error = error
    try:
        notify_slack(job, state)
    finally:
        # Set last: the page stops polling once it sees a final state, so the
        # Slack outcome has to be recorded before then.
        job.state = state


# --- Slack ------------------------------------------------------------------

_HEADLINES = {
    "done": ":white_check_mark: Outreach run finished",
    "stopped": ":double_vertical_bar: Outreach run stopped",
    "error": ":x: Outreach run failed",
}
_MAX_FAILURES_LISTED = 10


def slack_summary(job: Job, state: str) -> str:
    """The message posted when a run ends in ``state`` (done / stopped / error)."""
    snap = job.snapshot()
    spec = job.spec
    where = f"rows {spec.start_row}-{spec.end_row}"
    if job.plan:
        where = f"tab \"{job.plan.tab}\", {where}"

    lines = [
        f"*{_HEADLINES.get(state, 'Outreach run ended')}*",
        f"{where} · subject: {spec.subject}",
        f"Started {snap['started_at']}, ended {datetime.now():%Y-%m-%d %H:%M:%S}",
    ]
    if state == "error":
        lines.append(f"Error: {job.error}")
    if job.plan:
        lines.append(
            f"Sent *{snap['sent']}* · duplicated {snap['duplicated']} · invalid {snap['invalid']}"
            f" · failed {snap['failed']} · {snap['done']}/{snap['total']} rows processed"
        )
    failures = [e for e in snap["events"] if e["status"] == "failed"]
    for event in failures[:_MAX_FAILURES_LISTED]:
        lines.append(f"• row {event['row']} {event['email']}: {event['detail'][:120]}")
    if len(failures) > _MAX_FAILURES_LISTED:
        lines.append(f"• …and {len(failures) - _MAX_FAILURES_LISTED} more failed rows")
    return "\n".join(lines)


def post_to_slack(text: str, url: str | None = None) -> None:
    """POST a message to the configured webhook. Raises OutreachError on failure."""
    url = url if url is not None else load_slack_webhook_url()
    if not url:
        raise OutreachError("SLACK_WEBHOOK_URL isn't set in .env.")
    try:
        resp = requests.post(url, json={"text": text}, timeout=15)
    except requests.RequestException as exc:
        raise OutreachError(f"Couldn't reach Slack - {exc}") from exc
    if resp.status_code != 200:
        raise OutreachError(f"Slack rejected the message ({resp.status_code}: {resp.text[:100]})")


def notify_slack(job: Job, state: str) -> None:
    """Best-effort: a Slack outage must never turn a finished run into a failure."""
    if not load_slack_webhook_url():
        return
    try:
        post_to_slack(slack_summary(job, state))
        job.slack = "sent"
    except OutreachError as exc:
        job.slack = f"not sent: {exc}"


def _process(job: Job, client: Sheets, settings: SmtpSettings) -> None:
    """Mark the skipped rows in one batch, then send the rest one at a time."""
    spec, plan = job.spec, job.plan
    _mark_skipped(job, client)

    pending = [r for r in plan.rows if r.will_send]
    mailer = Mailer(settings)
    try:
        for index, row in enumerate(pending):
            if job.stop_flag.is_set():
                break
            try:
                mailer.send(row.email, spec.subject, spec.body)
                row.status, row.detail = "sent", ""
            except (OutreachError, smtplib.SMTPException, OSError) as exc:
                row.status, row.detail = "failed", str(exc)

            _write_status(job, client, row)
            _log(job, row)
            job.note(row)

            # Pause between sends only - never after the last one.
            if index + 1 < len(pending):
                job.stop_flag.wait(spec.delay)
    finally:
        mailer.close()


def _mark_skipped(job: Job, client: Sheets) -> None:
    """Record every duplicate/invalid row up front, in a single sheet call."""
    plan = job.plan
    skipped = [r for r in plan.rows if not r.will_send]
    if not skipped:
        return
    try:
        client.write_cells(
            plan.ref, plan.tab,
            [(f"{plan.status_column}{r.row_number}", _cell_text(r)) for r in skipped],
        )
    except SheetError as exc:
        for row in skipped:
            row.detail = f"{row.detail} (sheet not updated: {exc})".strip()
    for row in skipped:
        _log(job, row)
        job.note(row)


def _write_status(job: Job, client: Sheets, row: RowPlan) -> None:
    plan = job.plan
    try:
        client.write_cell(
            plan.ref, plan.tab, f"{plan.status_column}{row.row_number}", _cell_text(row)
        )
    except SheetError as exc:
        row.detail = f"{row.detail} (sheet not updated: {exc})".strip()


def _log(job: Job, row: RowPlan) -> None:
    plan = job.plan
    db.log_outreach(
        user_id=job.spec.user_id,
        email=row.email,
        linkedin=row.linkedin,
        spreadsheet_id=plan.ref.spreadsheet_id,
        tab=plan.tab,
        row_number=row.row_number,
        subject=job.spec.subject,
        status=row.status,
        detail=row.detail,
    )


def _cell_text(row: RowPlan) -> str:
    stamp = f"{datetime.now():%Y-%m-%d %H:%M}"
    if row.status == "sent":
        return f"sent {stamp}"
    if row.status == "duplicated":
        return f"duplicated ({row.detail})" if row.detail else "duplicated"
    if row.status == "invalid":
        return f"invalid: {row.detail}"
    if row.status == "failed":
        return f"failed: {row.detail[:150]}"
    return "skipped"
