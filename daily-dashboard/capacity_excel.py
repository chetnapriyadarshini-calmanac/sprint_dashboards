"""
capacity_excel.py
-----------------
Read per-member sprint capacity from the maintained capacity workbook.

The workbook is the source of truth for per-member capacity. `CAPACITY_XLSX`
(in sprint_dashboard_config.py) may point at any of:

  * a LOCAL .xlsx path (e.g. a Drive/OneDrive synced file), or
  * a GOOGLE SHEET URL read via a service account (recommended when several
    people / an unattended job must run this against a company-restricted
    Sheet — see CAPACITY_SA_KEY), or
  * a PUBLIC Google Sheet / direct .xlsx URL (unauthenticated download).

Capacity model:

    Sprint cap(row) = Capacity/day x (Working days - Team days off - Days off)

We compute this from the raw inputs (Capacity/day, Days off + the Settings
Working days / Team days off) rather than trusting the workbook's cached formula
value, so it is correct even right after a hand-edit that hasn't recalculated
yet. Multiple activity rows per member are summed downstream by
compute_capacity() in the generator.

Returns the canonical shape the generator consumes:
    Member | Activity | Sprint cap

CLI:
    python capacity_excel.py            # prints the resolved capacity table
"""

from __future__ import annotations

import io
import os
import re
from pathlib import Path
from typing import Any

import pandas as pd
from openpyxl import load_workbook

DEFAULT_FILE = "Team_Capacity.xlsx"

# Default service-account key locations tried when CAPACITY_SA_KEY is not set.
# All are git-ignored (see .gitignore).
_DEFAULT_SA_KEY_NAMES = (".gcp_sa.json", "service_account.json")

# .xlsx files are ZIP archives - they start with the "PK" local-file header.
# Used to tell a real workbook from an HTML login/permission page that Google
# returns when a link is NOT actually accessible.
_XLSX_MAGIC = b"PK\x03\x04"

# Drive export MIME type for an .xlsx workbook.
_XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _num(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _is_url(src: str) -> bool:
    return src.lower().startswith(("http://", "https://"))


def _extract_sheet_id(url: str) -> str | None:
    """Return the Google Sheet file ID from a share/edit/export URL, else None."""
    if "docs.google.com/spreadsheets" not in url:
        return None
    m = re.search(r"/spreadsheets/d/([a-zA-Z0-9-_]+)", url)
    return m.group(1) if m else None


def _normalise_gsheet_url(url: str) -> str:
    """Turn a Google Sheets share/edit link into an .xlsx export URL (used only
    for the UNAUTHENTICATED public-link path). Non-Sheet URLs pass through."""
    sid = _extract_sheet_id(url)
    if not sid or "export?format=" in url or "/export?" in url:
        return url
    return f"https://docs.google.com/spreadsheets/d/{sid}/export?format=xlsx"


def _find_sa_key(explicit: str | None) -> str | None:
    """Resolve the service-account key path: explicit arg -> the
    GOOGLE_APPLICATION_CREDENTIALS env var -> a default file at the repo root or
    next to this script. Returns None if none is found."""
    candidates: list[str | Path] = []
    if explicit:
        candidates.append(explicit)
    env = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    if env:
        candidates.append(env)
    here = Path(__file__).resolve().parent
    for name in _DEFAULT_SA_KEY_NAMES:
        candidates.append(here.parent / name)   # repo root
        candidates.append(here / name)           # daily-dashboard/
    for c in candidates:
        if c and Path(c).exists():
            return str(c)
    return None


def _export_gsheet_via_service_account(sheet_id: str, key_path: str) -> io.BytesIO:
    """Authenticated export of a Google Sheet to .xlsx bytes using a service
    account. The Sheet must be shared (Viewer) with the service account's
    client_email, and the Drive API enabled on its project."""
    try:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build
    except ImportError as e:
        raise RuntimeError(
            "Google API libraries are required to read a Google Sheet via a "
            "service account. Install them with:\n"
            "  pip install google-api-python-client google-auth\n"
            f"(import error: {e})"
        )
    scopes = ["https://www.googleapis.com/auth/drive.readonly"]
    try:
        creds = service_account.Credentials.from_service_account_file(key_path, scopes=scopes)
        svc = build("drive", "v3", credentials=creds, cache_discovery=False)
        data = svc.files().export(fileId=sheet_id, mimeType=_XLSX_MIME).execute()
    except Exception as e:
        raise RuntimeError(
            f"Could not export the Google Sheet (id={sheet_id}) with the service "
            f"account key '{key_path}'.\n"
            "Checklist: (1) the Sheet is shared with the service account's "
            "client_email as Viewer; (2) the Google Drive API is enabled on the "
            "service account's project; (3) the key file is valid.\n"
            f"Underlying error: {e}"
        )
    if isinstance(data, str):
        data = data.encode("utf-8", "ignore")
    if not data or not data.startswith(_XLSX_MAGIC):
        raise RuntimeError(
            "The Google Sheet export did not return a valid .xlsx. Confirm the "
            "file ID points at a Google Sheet (not a folder or uploaded file)."
        )
    return io.BytesIO(data)


def _fetch_workbook_bytes(url: str) -> io.BytesIO:
    """UNAUTHENTICATED download of a workbook from a URL into memory. Only works
    when the link is readable without signing in. Raises a clear error if the
    link returns a login/permission page instead of an .xlsx file."""
    import requests  # local import - only needed for the URL path

    fetch_url = _normalise_gsheet_url(url)
    try:
        r = requests.get(fetch_url, timeout=60, allow_redirects=True)
        r.raise_for_status()
    except Exception as e:
        raise RuntimeError(f"Could not download the capacity workbook from {fetch_url}\n{e}")

    data = r.content
    if not data.startswith(_XLSX_MAGIC):
        raise RuntimeError(
            "The capacity link did not return an .xlsx file - it looks like an "
            "HTML page (usually a sign-in or 'no access' page).\n"
            "A company-restricted Sheet cannot be read this way. Either set a "
            "service-account key (CAPACITY_SA_KEY / GOOGLE_APPLICATION_CREDENTIALS) "
            "and share the Sheet with its client_email, or use a local synced "
            ".xlsx path. See the README.\n"
            f"URL tried: {fetch_url}"
        )
    return io.BytesIO(data)


def _resolve_source(xlsx_path: str | Path | None, sa_key: str | None = None):
    """Return something load_workbook() can open: a local Path or an in-memory
    BytesIO downloaded from a URL (authenticated when a service-account key is
    available for a Google Sheet, otherwise an unauthenticated fetch)."""
    if xlsx_path is None:
        return Path(__file__).resolve().parent / DEFAULT_FILE
    src = str(xlsx_path)
    if _is_url(src):
        sheet_id = _extract_sheet_id(src)
        if sheet_id:
            key = _find_sa_key(sa_key)
            if key:
                return _export_gsheet_via_service_account(sheet_id, key)
            # No key configured -> fall back to the public-link path (which will
            # raise a helpful error if the Sheet is not actually public).
        return _fetch_workbook_bytes(src)
    path = Path(xlsx_path)
    if not path.exists():
        raise FileNotFoundError(
            f"Capacity workbook not found: {path}\n"
            f"Set CAPACITY_XLSX to a local .xlsx path or a shared workbook URL. "
            f"The workbook needs a 'Settings' sheet and a 'Capacity' sheet."
        )
    return path


def load_dataframe(xlsx_path: str | Path | None = None,
                   ctx: dict[str, Any] | None = None,
                   sa_key: str | None = None) -> pd.DataFrame:
    """Return Member | Activity | Sprint cap from the capacity workbook.

    `xlsx_path` may be a local file path OR an http(s) URL (Google Sheet link or
    a direct .xlsx download link). `sa_key` is an optional service-account key
    path used to read a Google Sheet via the Drive API. `ctx` is accepted and
    ignored so the signature matches the generator's loader swap.
    """
    source = _resolve_source(xlsx_path, sa_key=sa_key)
    src_name = getattr(source, "name", None) or "capacity workbook"

    wb = load_workbook(source, data_only=False)
    if "Settings" not in wb.sheetnames or "Capacity" not in wb.sheetnames:
        raise ValueError(
            f"{src_name} must have a 'Settings' sheet (Working days in B5, Team "
            f"days off in B6) and a 'Capacity' sheet (Team, Member, Activity, "
            f"Capacity/day, Days off, Sprint Capacity)."
        )

    st = wb["Settings"]
    workdays = _num(st["B5"].value)
    team_off = _num(st["B6"].value)

    cap = wb["Capacity"]
    rows: list[dict[str, Any]] = []
    for r in cap.iter_rows(min_row=2, values_only=True):
        cells = (list(r) + [None] * 6)[:6]
        team, member, activity, perday, daysoff, _computed = cells
        if not member or str(member).strip().upper() == "TOTAL":
            continue
        sprint_cap = max(0.0, _num(perday) * (workdays - team_off - _num(daysoff)))
        rows.append({
            "Member":     str(member).strip(),
            "Activity":   (activity or "Development"),
            "Sprint cap": round(sprint_cap, 2),
        })

    return pd.DataFrame(rows, columns=["Member", "Activity", "Sprint cap"])


if __name__ == "__main__":
    df = load_dataframe()
    if df.empty:
        print("No capacity rows found.")
    else:
        print(df.to_string(index=False))
        print("\nPer-member totals:")
        totals = df.groupby("Member")["Sprint cap"].sum().sort_values(ascending=False)
        print(totals.to_string())
        print(f"\nTeam total: {df['Sprint cap'].sum():.1f}h")
