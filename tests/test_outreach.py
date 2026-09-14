"""Offline tests for the outreach sender.

No network, no Google account, no mailbox: the Google Sheet and the SMTP
connection are both replaced with fakes, and the send log goes to a throwaway
database. Run it after touching ``resume/outreach.py``:

    .venv/Scripts/python.exe tests/test_outreach.py     (Windows)
    .venv/bin/python tests/test_outreach.py             (macOS/Linux)

Prints a line per check and exits non-zero if anything fails.
"""

from __future__ import annotations

import pathlib
import sys
import tempfile
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from resume import db

# Point the send log at a temp file BEFORE anything opens the real resume.db.
db.DB_PATH = pathlib.Path(tempfile.mkdtemp()) / "test.db"
db.init_db()

from resume import outreach  # noqa: E402
from resume.config import SmtpSettings  # noqa: E402

SHEET = "https://docs.google.com/spreadsheets/d/1aaaaaaaaaaaaaaaaaaaaaaaaaa/edit"
SMTP = SmtpSettings(host="smtp.test", port=587, user="me@test.com", password="pw",
                    sender="me@test.com", sender_name="Me", use_ssl=False)

checks = {"passed": 0, "failed": 0}


def check(label: str, got, want) -> None:
    ok = got == want
    checks["passed" if ok else "failed"] += 1
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + ("" if ok else f"\n        got {got!r}, want {want!r}"))


# --- Fakes ------------------------------------------------------------------


class FakeSheets:
    """Stands in for a real spreadsheet; records every write."""

    headers = ["Name", "Email", "LinkedIn", "Status"]
    rows: dict[int, list[str]] = {}
    cell_writes: list[tuple[str, str]] = []
    batch_writes: list[list[tuple[str, str]]] = []

    def tab_titles(self, ref):
        return ["Sheet1"]

    def read(self, ref, tab, cell_range):
        if cell_range.startswith("A1:"):
            return [self.headers]
        first, last = cell_range.split(":")
        lo = int(first[1:])
        hi = int("".join(c for c in last if c.isdigit()))
        return [self.rows.get(r, []) for r in range(lo, hi + 1)]

    def write_cell(self, ref, tab, cell, value):
        FakeSheets.cell_writes.append((cell, value))

    def write_cells(self, ref, tab, cells):
        FakeSheets.batch_writes.append(list(cells))


class FakeMailer:
    """Stands in for the SMTP session. ``fail_for`` simulates a rejection."""

    sent: list[str] = []
    fail_for: set[str] = set()
    delay = 0.0

    def __init__(self, settings):
        pass

    def send(self, to, subject, body):
        if FakeMailer.delay:
            time.sleep(FakeMailer.delay)
        if to in FakeMailer.fail_for:
            raise OSError("simulated server hiccup")
        FakeMailer.sent.append(to)

    def close(self):
        pass


RealMailer = outreach.Mailer
REAL_MIN_DELAY = outreach.MIN_DELAY
outreach.Sheets = FakeSheets
outreach.Mailer = FakeMailer
# Real runs pause >= 1s between sends. Relax that here so the suite finishes in
# seconds; the real guard is exercised in test_bad_input_is_refused below.
outreach.MIN_DELAY = 0.0


def run(rows: dict[int, list[str]], *, start: int, end: int, delay: float = 0.01,
        stop_after: float | None = None) -> dict:
    """Run a job over ``rows`` and wait for it to finish."""
    FakeSheets.rows = rows
    FakeSheets.cell_writes, FakeSheets.batch_writes = [], []
    FakeMailer.sent = []
    job = outreach.start_job(outreach.JobSpec(
        sheet_url=SHEET, start_row=start, end_row=end,
        subject="Hello", body="Hi there", delay=delay, user_id=1), SMTP)
    if stop_after:
        time.sleep(stop_after)
        job.stop()
    while job.state in ("starting", "running"):
        time.sleep(0.05)
    return job.snapshot()


def statuses(snap: dict) -> dict[int, str]:
    return {e["row"]: e["status"] for e in snap["events"]}


# --- Tests ------------------------------------------------------------------


def test_first_run():
    print("\nA fresh range: duplicates and bad rows are skipped, the rest are sent")
    snap = run({
        10: ["Ann", "ann@x.com", "li/ann", ""],
        11: ["Bob", "bob@x.com", "li/bob", ""],
        12: ["Ann", "Ann Again <ANN@x.com>", "li/ann", ""],  # same address, different spelling
        13: ["Cid", "", "", ""],                             # no address
        14: ["Dee", "not-an-email", "", ""],                 # unreadable
        15: ["Eve", "eve@x.com", "li/eve", ""],
    }, start=10, end=15)

    check("three addresses emailed", FakeMailer.sent, ["ann@x.com", "bob@x.com", "eve@x.com"])
    check("row statuses", statuses(snap), {
        10: "sent", 11: "sent", 12: "duplicated", 13: "invalid", 14: "invalid", 15: "sent",
    })
    check("duplicate note written to the sheet",
          dict(FakeSheets.batch_writes[0])["D12"], "duplicated (already in this range)")
    check("skips written in ONE batched call (rate limit)", len(FakeSheets.batch_writes), 1)
    check("one write per send", len(FakeSheets.cell_writes), 3)
    check("run finished", snap["state"], "done")


def test_second_run_uses_history():
    print("\nThe same range again: everything already emailed is skipped")
    snap = run({
        10: ["Ann", "ann@x.com", "li/ann", ""],
        11: ["Bob", "bob@x.com", "li/bob", ""],
        15: ["Eve", "eve@x.com", "li/eve", ""],
    }, start=10, end=15)

    check("nothing sent a second time", FakeMailer.sent, [])
    check("marked as duplicates", snap["duplicated"], 3)
    check("reason recorded",
          dict(FakeSheets.batch_writes[0])["D10"], "duplicated (emailed in an earlier run)")
    check("log remembers the three addresses", sorted(db.already_sent_emails()),
          ["ann@x.com", "bob@x.com", "eve@x.com"])


def test_failed_send_is_retried_later():
    print("\nA send that fails is recorded, and is NOT treated as a duplicate next time")
    FakeMailer.fail_for = {"zoe@x.com"}
    rows = {20: ["Zoe", "zoe@x.com", "", ""]}
    snap = run(rows, start=20, end=20)
    check("marked failed", statuses(snap), {20: "failed"})
    check("reason written to the sheet", FakeSheets.cell_writes[0][1], "failed: simulated server hiccup")

    FakeMailer.fail_for = set()
    snap = run(rows, start=20, end=20)
    check("retried on the next run", statuses(snap), {20: "sent"})
    check("and delivered", FakeMailer.sent, ["zoe@x.com"])


def test_stop_halts_the_run():
    print("\nStop: no further sends, and untouched rows keep an empty status cell")
    FakeMailer.delay = 0.3
    # Four columns, to match the header row: Name, Email, LinkedIn, Status.
    snap = run({r: [f"P{r}", f"p{r}@x.com", "", ""] for r in range(30, 40)},
               start=30, end=39, delay=0.3, stop_after=1.0)
    FakeMailer.delay = 0.0

    sent = snap["sent"]
    check("state is 'stopped'", snap["state"], "stopped")
    check("stopped early", 0 < sent < 10, True)
    check("sent count matches the mailbox", len(FakeMailer.sent), sent)
    check("only the sent rows were stamped", len(FakeSheets.cell_writes), sent)
    check("the rest were never touched", snap["done"], sent)


def test_bad_input_is_refused():
    print("\nBad input is refused before anything is read or sent")
    cases = [
        ({"start_row": 200, "end_row": 100}, "The end row must not be before the start row."),
        ({"start_row": 1, "end_row": 9999}, "That's more than 500 rows - narrow the range."),
        ({"subject": ""}, "Enter a subject."),
        ({"body": ""}, "Enter the message body."),
        ({"delay": 0}, "Keep at least 1s between sends."),
    ]
    outreach.MIN_DELAY = REAL_MIN_DELAY   # check the guard users actually hit
    try:
        for override, expected in cases:
            spec = outreach.JobSpec(**{
                "sheet_url": SHEET, "start_row": 10, "end_row": 20,
                "subject": "Hi", "body": "Body", "delay": 5, **override})
            try:
                spec.validate()
                check(f"refused {override}", "no error raised", expected)
            except outreach.OutreachError as exc:
                check(f"refused {override}", str(exc), expected)
    finally:
        outreach.MIN_DELAY = 0.0


def test_message_looks_normal():
    print("\nThe message itself: one real recipient, plain text")
    msg = RealMailer(SMTP).build("someone@example.com", "Subject here", "Line 1\nLine 2")
    check("one To: header", msg["To"], "someone@example.com")
    check("From: is the configured sender", msg["From"], "Me <me@test.com>")
    check("no Bcc", msg["Bcc"], None)
    check("plain text", msg.get_content_type(), "text/plain")
    check("body intact", msg.get_content().strip(), "Line 1\nLine 2")


def test_connects_to_resolved_address():
    print("\nSMTP_RESOLVER: the connection goes to the looked-up address, not system DNS")
    import socket
    import threading

    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    greeting = []

    def fake_smtp():
        conn, _ = server.accept()
        with conn:
            conn.sendall(b"220 fake ESMTP\r\n")
            greeting.append(conn.recv(200).decode(errors="replace").strip())
            conn.sendall(b"250 ok\r\n")
            conn.recv(200)                      # QUIT
            conn.sendall(b"221 bye\r\n")

    threading.Thread(target=fake_smtp, daemon=True).start()
    smtp = outreach._SMTP(timeout=5)
    smtp.address = "127.0.0.1"
    smtp._host = "smtp.example.invalid"         # unresolvable: system DNS would fail
    code, _ = smtp.connect("smtp.example.invalid", server.getsockname()[1])
    smtp.ehlo("tester")
    smtp.quit()
    server.close()
    check("reached the server at the given address", code, 220)
    check("spoke SMTP to it", greeting[:1], ["ehlo tester"])


def test_slack_notification():
    print("\nSlack: a summary is posted when a run ends, and a Slack outage can't break the run")

    class FakeResponse:
        def __init__(self, status):
            self.status_code, self.text = status, "no_service" if status != 200 else "ok"

    posts = []
    reply = {"status": 200}

    def fake_post(url, json, timeout):
        posts.append((url, json["text"]))
        return FakeResponse(reply["status"])

    real_post, real_url = outreach.requests.post, outreach.load_slack_webhook_url
    outreach.requests.post = fake_post
    try:
        # Not configured: nothing is posted.
        outreach.load_slack_webhook_url = lambda: ""
        snap = run({50: ["Al", "al@x.com", "", ""]}, start=50, end=50)
        check("no webhook -> nothing posted", posts, [])
        check("no webhook -> slack field empty", snap["slack"], "")

        # Configured: one summary with the counts and the failed row.
        outreach.load_slack_webhook_url = lambda: "https://hooks.slack.test/abc"
        FakeMailer.fail_for = {"bad@x.com"}
        snap = run({51: ["Bo", "bo@x.com", "", ""], 52: ["Bad", "bad@x.com", "", ""],
                    53: ["Bo", "bo@x.com", "", ""]}, start=51, end=53)
        FakeMailer.fail_for = set()
        text = posts[0][1] if posts else ""
        check("one message posted", len(posts), 1)
        check("to the configured webhook", posts[0][0] if posts else "", "https://hooks.slack.test/abc")
        check("headline says finished", text.startswith("*:white_check_mark: Outreach run finished*"), True)
        check("counts included", "Sent *1* · duplicated 1 · invalid 0 · failed 1" in text, True)
        check("failed row listed", "row 52 bad@x.com: simulated server hiccup" in text, True)
        check("page told Slack was notified", snap["slack"], "sent")

        # Slack down: the run still finishes normally.
        posts.clear()
        reply["status"] = 404
        snap = run({54: ["Cy", "cy@x.com", "", ""]}, start=54, end=54)
        check("run still 'done' when Slack fails", snap["state"], "done")
        check("email still sent", FakeMailer.sent, ["cy@x.com"])
        check("page told why Slack wasn't notified", snap["slack"].startswith("not sent: Slack rejected"), True)
    finally:
        outreach.requests.post, outreach.load_slack_webhook_url = real_post, real_url


for test in (test_first_run, test_second_run_uses_history, test_failed_send_is_retried_later,
             test_stop_halts_the_run, test_bad_input_is_refused, test_message_looks_normal,
             test_connects_to_resolved_address, test_slack_notification):
    test()

print(f"\n{checks['passed']} passed, {checks['failed']} failed")
sys.exit(1 if checks["failed"] else 0)
