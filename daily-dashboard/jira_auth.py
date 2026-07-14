"""
jira_auth.py
------------
JIRA Cloud equivalent of scripts/dashboard/tfs_auth.py — single source of
truth for authenticating to your Atlassian Cloud site.

Why this mirrors tfs_auth.py:
    The dashboards never talk to the tracker directly. They call a fetch
    layer that returns a canonical pandas DataFrame. To move from TFS to
    JIRA we only need to swap the connection + fetch layer and keep the
    DataFrame shape identical. This module owns everything JIRA-Cloud
    specific (base URL, auth, session) so the fetch layer stays clean.

AUTH MODEL (JIRA Cloud):
    JIRA Cloud uses HTTP Basic auth where:
        username = your Atlassian account email
        password = an API token (NOT your password)
    Create a token at: https://id.atlassian.com/manage-profile/security/api-tokens

CONFIG (two small files at the repo root, same idea as .tfs_pat):
    .jira_pat    -> single line: the API token
    .jira_email  -> single line: your Atlassian account email
                    (or set JIRA_EMAIL env var; or hard-code DEFAULT_EMAIL below)

    JIRA_SITE can be set via the .jira_site file, the JIRA_SITE env var, or
    the DEFAULT_SITE constant below. Example: https://humanebits.atlassian.net

Public API:
    get_context() -> dict
        Returns {base_url, api_v3, agile, project, email, session, timeout}
    test_auth(ctx=None) -> dict
        Hits /rest/api/3/myself and returns {ok, account, ...}.

CLI:
    python jira/jira_auth.py
        Prints a one-line auth pre-flight result.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import requests
from requests.auth import HTTPBasicAuth

# ---------------------------------------------------------------------------
# Pin these to your instance (or supply via the dotfiles / env vars below).
# ---------------------------------------------------------------------------
DEFAULT_SITE    = "https://motivity.atlassian.net"     # confirmed from probe
DEFAULT_EMAIL   = ""                                    # optional fallback
PROJECT_KEY     = "MPM"                                 # project "MPM Development", key MPM
API_VERSION     = "3"                                   # Cloud REST = v3
DEFAULT_TIMEOUT = 30


def _read_dotfile(name: str) -> str | None:
    """Read a single-line secret file from the repo root, if present."""
    here = Path(__file__).resolve().parent           # jira/
    for candidate in (here.parent / name, here / name):
        if candidate.exists():
            val = candidate.read_text(encoding="utf-8").strip()
            if val:
                return val
    return None


def _load_token() -> str:
    tok = _read_dotfile(".jira_pat") or os.environ.get("JIRA_API_TOKEN")
    if not tok:
        raise FileNotFoundError(
            "JIRA API token not found. Create a single-line file '.jira_pat' "
            "at the repo root containing your Atlassian API token, or set the "
            "JIRA_API_TOKEN env var. Generate a token at "
            "https://id.atlassian.com/manage-profile/security/api-tokens"
        )
    return tok


def _load_email() -> str:
    email = (_read_dotfile(".jira_email")
             or os.environ.get("JIRA_EMAIL")
             or DEFAULT_EMAIL)
    if not email:
        raise ValueError(
            "JIRA account email not found. Put it in '.jira_email' at the repo "
            "root, set the JIRA_EMAIL env var, or fill DEFAULT_EMAIL in jira_auth.py."
        )
    return email


def _load_site() -> str:
    site = (_read_dotfile(".jira_site")
            or os.environ.get("JIRA_SITE")
            or DEFAULT_SITE)
    return site.rstrip("/")


def get_context() -> dict[str, Any]:
    """Return the context dict consumed by jira_fetch / jira_capacity."""
    email = _load_email()
    token = _load_token()
    base  = _load_site()

    session = requests.Session()
    session.auth = HTTPBasicAuth(email, token)
    session.headers.update({
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "em-standup-jira/1.0",
    })

    return {
        "base_url": base,
        "api_v3":   f"{base}/rest/api/{API_VERSION}",
        "agile":    f"{base}/rest/agile/1.0",
        "project":  PROJECT_KEY,
        "email":    email,
        "session":  session,
        "timeout":  DEFAULT_TIMEOUT,
    }


def test_auth(ctx: dict[str, Any] | None = None) -> dict[str, Any]:
    """Health check via /myself. Confirms site URL + email + token line up."""
    if ctx is None:
        ctx = get_context()
    url = f"{ctx['api_v3']}/myself"
    try:
        r = ctx["session"].get(url, timeout=ctx["timeout"])
    except requests.exceptions.ConnectionError as e:
        return {"ok": False, "error": f"Connection error: {e}",
                "hint": "Check the site URL in .jira_site / DEFAULT_SITE."}
    if r.status_code in (401, 403):
        return {"ok": False, "status": r.status_code,
                "error": "Auth rejected.",
                "hint": "Email + API token mismatch, or token revoked. "
                        "Regenerate at id.atlassian.com."}
    if not r.ok:
        return {"ok": False, "status": r.status_code, "error": r.text[:300]}
    me = r.json()
    return {
        "ok": True,
        "account": me.get("displayName"),
        "email": me.get("emailAddress"),
        "account_id": me.get("accountId"),
    }


if __name__ == "__main__":
    print("Authenticating to JIRA Cloud ...")
    res = test_auth()
    if res.get("ok"):
        print(f"[OK] Authenticated as {res['account']} ({res.get('email')})")
        sys.exit(0)
    print(f"[FAIL] {res.get('error')}  (status={res.get('status')})")
    if res.get("hint"):
        print(f"       Hint: {res['hint']}")
    sys.exit(1)
