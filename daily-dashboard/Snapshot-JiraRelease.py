#!/usr/bin/env python3
"""
Snapshot-JiraRelease.py
=======================
Daily snapshot of a JIRA Fix Version (default REL-AUG-26) for the MONTHLY RETRO.
Run it every day during the release; each run writes one dated JSON file, so the
sequence builds the history the monthly retro is computed from.

Per issue it captures (enough to derive every monthly-retro KPI later):
  * identity:   key, type, status, statusCategory, summary, parent, epic link
  * people:     assignee, dev assignee, qa assignee, team
  * sizing/sev: sizing (T-shirt), story points, severity, priority
  * quality:    root cause, root cause analysis
  * dates:      EVERY custom date field (resolved by name → captures Dev Complete
                Date, QA Complete Date, Ready for Tech Analysis Date, BA Analysis
                Started/Completed, etc.), plus created / resolutiondate
  * effort:     original estimate / time spent (own + aggregate), in hours
  * history:    full STATUS-transition changelog (from/to/at/by) — exact
                time-in-status and "date met vs status reached" without waiting
                weeks for daily snapshots to accumulate
  * scope:      fixVersions, labels, Original TFS ID

Output:
    snapshots/jira/<RELEASE>/<YYYY-MM-DD>.json   (idempotent per day)
    snapshots/jira/<RELEASE>/_index.json         (list of captured dates)

Usage:
    python Snapshot-JiraRelease.py                 # REL-AUG-26
    python Snapshot-JiraRelease.py "REL-AUG-26"
    python Snapshot-JiraRelease.py "REL-AUG-26" --no-changelog
"""

from __future__ import annotations

import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
import jira_auth  # noqa: E402

_REPO = _HERE.parent
DEFAULT_RELEASE = "REL-AUG-26"

# Named custom fields to capture (resolved to ids by display name at runtime).
_NAMED_FIELDS = [
    "Sizing", "Story Points", "Severity", "Root Cause", "Root Cause Analysis",
    "Dev Assignee", "QA Assignee", "Team", "Original TFS ID", "Sprint",
    "Goal for the Sprint", "Epic Link",
]


# ── helpers ─────────────────────────────────────────────────────────────────
def _opt(v: Any) -> str:
    if isinstance(v, dict):
        return v.get("value") or v.get("name") or v.get("displayName") or ""
    if isinstance(v, list) and v:
        return ", ".join(_opt(x) for x in v)
    return v if isinstance(v, (str, int, float)) else ""


def _adf_text(node: Any) -> str:
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    if isinstance(node, dict):
        if node.get("type") == "text":
            return node.get("text", "")
        return " ".join(_adf_text(c) for c in node.get("content", []) or [])
    if isinstance(node, list):
        return " ".join(_adf_text(c) for c in node)
    return ""


def _hours(seconds: Any) -> float:
    try:
        return round((seconds or 0) / 3600.0, 2)
    except (TypeError, ValueError):
        return 0.0


def _field_catalog(ctx: dict[str, Any]) -> tuple[dict[str, str], dict[str, str]]:
    """Return (named_ids, date_field_ids).
       named_ids:      {display name -> id} for _NAMED_FIELDS that exist.
       date_field_ids: {display name -> id} for every CUSTOM date/datetime field.
    """
    r = ctx["session"].get(f"{ctx['api_v3']}/field", timeout=ctx["timeout"])
    r.raise_for_status()
    by_name_lower: dict[str, dict] = {}
    date_ids: dict[str, str] = {}
    for f in r.json():
        name = (f.get("name") or "").strip()
        by_name_lower[name.lower()] = f
        schema = f.get("schema") or {}
        if f.get("custom") and schema.get("type") in ("date", "datetime"):
            date_ids[name] = f.get("id")
    named: dict[str, str] = {}
    for want in _NAMED_FIELDS:
        f = by_name_lower.get(want.lower())
        if f:
            named[want] = f.get("id")
    return named, date_ids


def _search(ctx: dict[str, Any], jql: str, fields: list[str]) -> list[dict[str, Any]]:
    """POST /rest/api/3/search/jql with nextPageToken paging."""
    out: list[dict[str, Any]] = []
    token = None
    while True:
        payload: dict[str, Any] = {"jql": jql, "maxResults": 100, "fields": fields}
        if token:
            payload["nextPageToken"] = token
        r = ctx["session"].post(f"{ctx['api_v3']}/search/jql", json=payload, timeout=ctx["timeout"])
        if not r.ok:
            raise RuntimeError(f"JQL failed ({r.status_code}): {r.text[:300]}\nJQL: {jql}")
        body = r.json()
        out.extend(body.get("issues", []))
        token = body.get("nextPageToken")
        if not token or body.get("isLast"):
            break
    return out


def _status_history(ctx: dict[str, Any], key: str) -> list[dict[str, Any]]:
    """Full status-transition history for one issue (from /issue/{key}/changelog)."""
    out: list[dict[str, Any]] = []
    start = 0
    while True:
        r = ctx["session"].get(
            f"{ctx['api_v3']}/issue/{key}/changelog?startAt={start}&maxResults=100",
            timeout=ctx["timeout"])
        if not r.ok:
            break
        body = r.json()
        vals = body.get("values", [])
        for h in vals:
            for it in h.get("items", []):
                if it.get("field") == "status":
                    out.append({
                        "at": h.get("created"),
                        "from": it.get("fromString"),
                        "to": it.get("toString"),
                        "by": (h.get("author") or {}).get("displayName", ""),
                    })
        start += len(vals)
        if start >= body.get("total", 0) or not vals:
            break
    return out


def _issue_record(iss: dict[str, Any], named: dict[str, str],
                  date_ids: dict[str, str]) -> dict[str, Any]:
    f = iss.get("fields", {})
    a = f.get("assignee") or {}
    parent = f.get("parent") or {}
    rec = {
        "key": iss["key"],
        "type": (f.get("issuetype") or {}).get("name", ""),
        "status": (f.get("status") or {}).get("name", ""),
        "status_category": (f.get("status") or {}).get("statusCategory", {}).get("name", ""),
        "summary": f.get("summary", "") or "",
        "assignee": a.get("displayName", "") if isinstance(a, dict) else "",
        "parent": parent.get("key", "") if isinstance(parent, dict) else "",
        "priority": (f.get("priority") or {}).get("name", "") if isinstance(f.get("priority"), dict) else "",
        "created": f.get("created", ""),
        "resolutiondate": f.get("resolutiondate", ""),
        "labels": f.get("labels", []) or [],
        "fix_versions": [v.get("name", "") for v in (f.get("fixVersions") or [])],
        "original_estimate_h": _hours(f.get("timeoriginalestimate")),
        "time_spent_h": _hours(f.get("timespent")),
        "agg_original_estimate_h": _hours(f.get("aggregatetimeoriginalestimate")),
        "agg_time_spent_h": _hours(f.get("aggregatetimespent")),
    }
    # Named custom fields.
    def nv(name):
        fid = named.get(name)
        return f.get(fid) if fid else None
    rec["sizing"] = _opt(nv("Sizing"))
    rec["story_points"] = nv("Story Points")
    rec["severity"] = _opt(nv("Severity"))
    rec["root_cause"] = _opt(nv("Root Cause"))
    rec["root_cause_analysis"] = _adf_text(nv("Root Cause Analysis")).strip()
    rec["dev_assignee"] = _opt(nv("Dev Assignee"))
    rec["qa_assignee"] = _opt(nv("QA Assignee"))
    rec["team"] = _opt(nv("Team"))
    rec["tfs_id"] = nv("Original TFS ID")
    rec["goal"] = _opt(nv("Goal for the Sprint"))
    rec["epic_link"] = _opt(nv("Epic Link"))
    # Sprint (list of sprint objects).
    sprint_raw = nv("Sprint")
    if isinstance(sprint_raw, list) and sprint_raw:
        last = sprint_raw[-1]
        rec["sprint"] = last.get("name", "") if isinstance(last, dict) else str(last)
    else:
        rec["sprint"] = ""
    # All custom date fields, by name.
    rec["dates"] = {name: f.get(fid) for name, fid in date_ids.items() if f.get(fid) is not None}
    return rec


def snapshot(release: str, with_changelog: bool = True,
             ctx: dict[str, Any] | None = None) -> dict[str, Any]:
    if ctx is None:
        ctx = jira_auth.get_context()
        health = jira_auth.test_auth(ctx)
        if not health.get("ok"):
            raise RuntimeError(f"JIRA auth failed: {health.get('error')}")
        print(f"  Authenticated to JIRA as {health.get('account') or '?'}")

    named, date_ids = _field_catalog(ctx)
    print(f"  Resolved {len(named)} named fields, {len(date_ids)} custom date field(s).")

    base_fields = ["summary", "issuetype", "status", "assignee", "parent", "priority",
                   "created", "resolutiondate", "labels", "fixVersions",
                   "timeoriginalestimate", "timespent",
                   "aggregatetimeoriginalestimate", "aggregatetimespent"]
    fields = base_fields + list(dict.fromkeys(list(named.values()) + list(date_ids.values())))

    jql = f'project = "{ctx["project"]}" AND fixVersion = "{release}" ORDER BY key ASC'
    issues = _search(ctx, jql, fields)
    print(f"  {len(issues)} issue(s) in {release}.")

    records = []
    for n, iss in enumerate(issues, 1):
        rec = _issue_record(iss, named, date_ids)
        if with_changelog:
            rec["status_history"] = _status_history(ctx, iss["key"])
        records.append(rec)
        if n % 50 == 0:
            print(f"    ... {n}/{len(issues)}")

    return {
        "release": release,
        "snapshot_date": date.today().isoformat(),
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "project": ctx["project"],
        "issue_count": len(records),
        "with_changelog": with_changelog,
        "issues": records,
    }


def _write(snap: dict[str, Any]) -> Path:
    out_dir = _REPO / "snapshots" / "jira" / snap["release"]
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{snap['snapshot_date']}.json"
    out_path.write_text(json.dumps(snap, indent=2, default=str), encoding="utf-8")
    # maintain a simple index of captured dates
    idx_path = out_dir / "_index.json"
    dates = sorted({p.stem for p in out_dir.glob("*.json") if p.stem != "_index"})
    idx_path.write_text(json.dumps({"release": snap["release"], "dates": dates}, indent=2),
                        encoding="utf-8")
    return out_path


def main() -> int:
    args = [a for a in sys.argv[1:]]
    with_changelog = "--no-changelog" not in args
    args = [a for a in args if not a.startswith("--")]
    release = args[0] if args else DEFAULT_RELEASE

    print(f"Snapshotting JIRA Fix Version {release!r} (changelog={'on' if with_changelog else 'off'}) ...")
    snap = snapshot(release, with_changelog=with_changelog)
    out = _write(snap)
    size_kb = out.stat().st_size // 1024
    print(f"\n✅ Wrote {out}  ({snap['issue_count']} issues, {size_kb} KB)")
    print(f"   History dir: {out.parent}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
