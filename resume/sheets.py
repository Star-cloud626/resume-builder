"""Minimal Google Sheets client (read rows, write a status cell).

Talks to the Sheets REST API directly with a service-account key - no extra
client library needed. The key can come from its own project (``sheets-sa.json``)
or fall back to the Vertex key; see ``_credentials_path``.

Setup, once:
  1. Enable the *Google Sheets API* on the key's Google Cloud project.
  2. Share the spreadsheet with the service account's ``client_email``
     (Editor, so statuses can be written back).
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from urllib.parse import quote

import requests
from google.auth.transport.requests import AuthorizedSession
from google.oauth2 import service_account

from . import config  # also resolves GOOGLE_APPLICATION_CREDENTIALS / vertex-sa.json on import

API = "https://sheets.googleapis.com/v4/spreadsheets"
BATCH_LIMIT = 200   # cell updates per batchUpdate call
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
TIMEOUT = 30

_SHEET_ID_RE = re.compile(r"/spreadsheets/d/([a-zA-Z0-9-_]+)")
_GID_RE = re.compile(r"[#&]gid=(\d+)")


class SheetError(RuntimeError):
    """Anything that went wrong talking to the spreadsheet."""


@dataclass
class SheetRef:
    spreadsheet_id: str
    gid: int | None = None


def parse_sheet_url(url: str) -> SheetRef:
    """Pull the spreadsheet id (and tab gid, if present) out of a Sheets URL.

    Also accepts a bare spreadsheet id, so pasting either works.
    """
    url = (url or "").strip()
    if not url:
        raise SheetError("Paste the Google Sheet URL.")
    m = _SHEET_ID_RE.search(url)
    if m:
        gid = _GID_RE.search(url)
        return SheetRef(m.group(1), int(gid.group(1)) if gid else None)
    if re.fullmatch(r"[a-zA-Z0-9-_]{20,}", url):
        return SheetRef(url)
    raise SheetError("That doesn't look like a Google Sheet URL.")


# --- A1 notation helpers ----------------------------------------------------


def column_letter(index: int) -> str:
    """0 -> A, 25 -> Z, 26 -> AA."""
    if index < 0:
        raise ValueError("column index must be >= 0")
    letters = ""
    index += 1
    while index:
        index, rem = divmod(index - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


def column_index(letter: str) -> int:
    """A -> 0, Z -> 25, AA -> 26. Raises on anything that isn't a column."""
    letter = (letter or "").strip().upper()
    if not letter or not letter.isalpha():
        raise SheetError(f"'{letter}' is not a column letter (use A, B, ... AA).")
    idx = 0
    for ch in letter:
        idx = idx * 26 + (ord(ch) - 64)
    return idx - 1


def a1(tab: str, ref: str) -> str:
    """Quote the tab name so ranges survive spaces and apostrophes."""
    if not tab:
        return ref
    return f"'{tab.replace(chr(39), chr(39) * 2)}'!{ref}"


# --- Client -----------------------------------------------------------------


SHEETS_KEY = config.ROOT / "sheets-sa.json"


def _credentials_path() -> str:
    """Key used for Sheets, which may belong to a different project than Vertex.

    Looked for in order: ``GOOGLE_SHEETS_CREDENTIALS``, ``sheets-sa.json`` next
    to app.py, then the Vertex key (``GOOGLE_APPLICATION_CREDENTIALS`` /
    ``vertex-sa.json``).
    """
    candidates = [
        os.environ.get("GOOGLE_SHEETS_CREDENTIALS", "").strip().strip('"'),
        str(SHEETS_KEY),
        os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "").strip().strip('"'),
    ]
    for path in candidates:
        if path and not os.path.isabs(path):
            path = str(config.ROOT / path)
        if path and os.path.exists(path):
            return path
    raise SheetError(
        "No Google credentials found for Sheets. Put the service-account key at "
        "sheets-sa.json next to app.py, or set GOOGLE_SHEETS_CREDENTIALS."
    )


def service_account_email() -> str:
    """The address the spreadsheet has to be shared with ('' if unreadable)."""
    try:
        data = json.loads(open(_credentials_path(), encoding="utf-8").read())
    except (OSError, ValueError, SheetError):
        return ""
    return data.get("client_email", "")


class Sheets:
    """Thin wrapper over the handful of Sheets calls this feature needs."""

    def __init__(self) -> None:
        creds = service_account.Credentials.from_service_account_file(
            _credentials_path(), scopes=SCOPES
        )
        self._session = AuthorizedSession(creds)

    # -- internals --
    def _request(self, method: str, path: str, **kw):
        try:
            resp = self._session.request(method, f"{API}{path}", timeout=TIMEOUT, **kw)
        except requests.RequestException as exc:
            raise SheetError(f"Could not reach Google Sheets: {exc}") from exc
        if resp.status_code >= 400:
            raise SheetError(_explain(resp))
        return resp.json()

    # -- API --
    def tab_titles(self, ref: SheetRef) -> list[str]:
        """Worksheet names, with the gid from the URL (if any) placed first."""
        data = self._request(
            "GET", f"/{ref.spreadsheet_id}", params={"fields": "sheets.properties"}
        )
        props = [s["properties"] for s in data.get("sheets", [])]
        titles = [p["title"] for p in props]
        if ref.gid is not None:
            for p in props:
                if p.get("sheetId") == ref.gid and p["title"] in titles:
                    titles.remove(p["title"])
                    titles.insert(0, p["title"])
                    break
        if not titles:
            raise SheetError("That spreadsheet has no worksheets.")
        return titles

    def read(self, ref: SheetRef, tab: str, cell_range: str) -> list[list[str]]:
        """Values for an A1 range. Short/absent rows come back short - pad as needed."""
        path = f"/{ref.spreadsheet_id}/values/{quote(a1(tab, cell_range), safe='')}"
        data = self._request(
            "GET", path, params={"majorDimension": "ROWS", "valueRenderOption": "FORMATTED_VALUE"}
        )
        return [[str(c) for c in row] for row in data.get("values", [])]

    def write_cell(self, ref: SheetRef, tab: str, cell: str, value: str) -> None:
        path = f"/{ref.spreadsheet_id}/values/{quote(a1(tab, cell), safe='')}"
        self._request(
            "PUT", path,
            params={"valueInputOption": "RAW"},
            json={"values": [[value]]},
        )

    def write_cells(self, ref: SheetRef, tab: str, cells: list[tuple[str, str]]) -> None:
        """Write many single cells in one call.

        Sheets allows only ~60 write requests per minute, so anything that
        updates a lot of rows at once (e.g. marking every duplicate) has to come
        through here rather than as a cell-at-a-time loop.
        """
        if not cells:
            return
        for start in range(0, len(cells), BATCH_LIMIT):
            chunk = cells[start:start + BATCH_LIMIT]
            self._request(
                "POST", f"/{ref.spreadsheet_id}/values:batchUpdate",
                json={
                    "valueInputOption": "RAW",
                    "data": [{"range": a1(tab, cell), "values": [[value]]} for cell, value in chunk],
                },
            )


def _explain(resp: requests.Response) -> str:
    """Turn a Sheets API error into something worth showing a human."""
    try:
        message = resp.json().get("error", {}).get("message", "").strip()
    except ValueError:
        message = (resp.text or "").strip()[:200]
    if resp.status_code in (401, 403):
        who = service_account_email()
        share = f" Share the sheet with {who} (Editor)." if who else ""
        if "Sheets API has not been used" in message or "disabled" in message.lower():
            return f"Google Sheets API error: {message}"
        return f"No access to that spreadsheet.{share} ({message})"
    if resp.status_code == 404:
        return "Spreadsheet not found - check the URL."
    if resp.status_code == 429:
        return "Google Sheets rate limit hit. Wait a minute and try again."
    return f"Google Sheets error {resp.status_code}: {message}"
