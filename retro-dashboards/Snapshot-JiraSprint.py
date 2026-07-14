#!/usr/bin/env python3
"""
Snapshot-JiraSprint.py
======================
FREEZE a JIRA sprint (or a whole "Sprint N" wave across every team board) into a
single self-contained JSON, so the SPRINT RETRO DASHBOARD can be generated /
regenerated OFFLINE afterwards — even after you roll the boards to the next
sprint.

Why this exists
---------------
The retro loader (jira_retro_fetch.py) reads LIVE from JIRA and uses
each issue's *current* State as its "state at sprint end". The moment you move to
the next sprint and work continues, those states drift and the retro becomes
wrong. Run this BEFORE moving the boards to Sprint 2 to capture the point-in-time
truth.

Naming convention
-----------------
Sprints are named per team, e.g. "MPM Calmers Sprint 1", "MPM Crackers Sprint 1",
"MPM Knackers Sprint 1", "MPM QA Automation Sprint 1". So one "wave" (Sprint 1)
is actually several active sprints — one per team board. By default this script
AUTO-DETECTS every ACTIVE sprint on the MPM boards (right now: the four
"... Sprint 1"s) and freezes them together, tagging each issue with its sprint
(which encodes the team).

What it captures (per issue — enough to rebuild RetroData + every retro section):
  * identity : key, type, status, statusCategory, summary, parent, epic link
  * people   : assignee, dev assignee, qa assignee, team
  * sizing   : sizing (T-shirt), story points, severity, priority
  * quality  : root cause, root cause analysis
  * dates    : every custom date field, plus created / resolutiondate
  * effort   : original estimate / time spent (own + aggregate), hours
  * history  : full STATUS-transition changelog (from/to/at/by) -> exact
               state-at-sprint-end + time-in-status without needing daily snaps
  * scope    : fixVersions, labels, Original TFS ID, sprint(s), derived team

Plus per-sprint metadata (id, name, state, start/end/complete dates, goal,
boardId) and, best-effort, the board Sprint Report (committed / completed /
not-completed / removed issue keys) so commitment-vs-delivery is exact.

Output:
    snapshots/jira_sprint/<WAVE>/<YYYY-MM-DD>.json     (e.g. WAVE = "Sprint-1")
    snapshots/jira_sprint/<WAVE>/_index.json

Usage:
    # freeze every active sprint on the MPM boards (the Sprint 1 wave) NOW:
    python Snapshot-JiraSprint.py

    # label the output folder explicitly:
    python Snapshot-JiraSprint.py --wave "Sprint-1"

    # freeze specific sprint(s) by exact name (repeatable):
    python Snapshot-JiraSprint.py --sprint "MPM Knackers Sprint 1" \
                                       --sprint "MPM Calmers Sprint 1"

    # only auto-detected sprints whose name contains this text:
    python Snapshot-JiraSprint.py --match "Sprint 1"

    python Snapshot-JiraSprint.py --no-changelog     # faster, less exact
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
import jira_auth  # noqa: E402

_REPO = _HERE.parent

# Named custom fields to capture (resolved to ids by display name at runtime).
_NAMED_FIELDS = [
    "Sizing", "Story Points", "Severity", "Root Cause", "Root Cause Analysis",
    "Dev Assignee", "QA Assignee", "Team", "Original TFS ID", "Sprint",
    "Goal for the Sprint", "Epic Link",
]

# Team tokens used to derive a team from a sprint name like "MPM Knackers Sprint 1".
_TEAM_TOKENS = ["Calmers", "Crackers", "Knackers", "QA Automation", "Platforms"]


# ── small helpers (kept local so this file is self-contained) ────────────────
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


def _team_from_sprint(sprint_name: str) -> str:
    low = (sprint_name or "").lower()
    for t in _TEAM_TOKENS:
        if t.lower() in low:
            return t
    return ""


# ── field catalog ────────────────────────────────────────────────────────────
def _field_catalog(ctx: dict[str, Any]) -> tuple[dict[str, str], dict[str, str]]:
    """(named_ids, date_field_ids) — named for _NAMED_FIELDS, plus every custom
    date/datetime field by display name."""
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


# ── JQL search + changelog ───────────────────────────────────────────────────
def _search(ctx: dict[str, Any], jql: str, fields: list[str]) -> list[dict[str, Any]]:
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


# ── sprint discovery (Agile API) ─────────────────────────────────────────────
def _agile_get(ctx: dict[str, Any], path: str, params: dict[str, Any] | None = None) -> dict:
    r = ctx["session"].get(f"{ctx['agile']}/{path}", params=params or {}, timeout=ctx["timeout"])
    r.raise_for_status()
    return r.json()


def _boards(ctx: dict[str, Any]) -> list[dict[str, Any]]:
    out, start = [], 0
    while True:
        body = _agile_get(ctx, "board", {"projectKeyOrId": ctx["project"],
                                         "startAt": start, "maxResults": 50})
        out.extend(body.get("values", []))
        if body.get("isLast") or not body.get("values"):
            break
        start += len(body["values"])
    return out


def _board_sprints(ctx: dict[str, Any], board_id: int, state: str = "active") -> list[dict[str, Any]]:
    out, start = [], 0
    while True:
        try:
            body = _agile_get(ctx, f"board/{board_id}/sprint",
                              {"state": state, "startAt": start, "maxResults": 50})
        except Exception:
            return out  # e.g. Kanban board -> no sprints
        for s in body.get("values", []):
            s["boardId"] = board_id
            out.append(s)
        if body.get("isLast") or not body.get("values"):
            break
        start += len(body["values"])
    return out


def _discover_sprints(ctx: dict[str, Any], match: str | None) -> list[dict[str, Any]]:
    """All ACTIVE sprints across the project's boards (dedup by sprint id)."""
    seen: dict[int, dict[str, Any]] = {}
    for b in _boards(ctx):
        for s in _board_sprints(ctx, b["id"], state="active"):
            if match and match.lower() not in (s.get("name") or "").lower():
                continue
            seen.setdefault(s["id"], s)
    return list(seen.values())


def _resolve_named_sprints(ctx: dict[str, Any], names: list[str]) -> list[dict[str, Any]]:
    """Resolve explicit sprint names to sprint objects across all boards
    (searches active + closed + future so it also works after a roll)."""
    want = {n.strip().lower() for n in names}
    found: dict[int, dict[str, Any]] = {}
    for b in _boards(ctx):
        for state in ("active", "closed", "future"):
            for s in _board_sprints(ctx, b["id"], state=state):
                if (s.get("name") or "").strip().lower() in want:
                    found.setdefault(s["id"], s)
    return list(found.values())


def _sprint_report(ctx: dict[str, Any], board_id: int, sprint_id: int) -> dict[str, Any]:
    """Best-effort GreenHopper sprint report -> committed/completed/not-completed/
    removed issue keys. Returns {} if the internal endpoint is unavailable."""
    try:
        url = (f"{ctx['base_url']}/rest/greenhopper/1.0/rapid/charts/sprintreport"
               f"?rapidViewId={board_id}&sprintId={sprint_id}")
        r = ctx["session"].get(url, timeout=ctx["timeout"])
        if not r.ok:
            return {}
        c = (r.json() or {}).get("contents", {}) or {}

        def keys(section):
            return [i.get("key") for i in (c.get(section) or []) if i.get("key")]

        return {
            "completed": keys("completedIssues"),
            "not_completed": keys("issuesNotCompletedInCurrentSprint"),
            "removed": keys("puntedIssues"),
            "completed_outside": keys("issuesCompletedInAnotherSprint"),
        }
    except Exception:
        return {}


# ── issue record ─────────────────────────────────────────────────────────────
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
    # Goal for the Sprint may hold MULTIPLE values; keep the full list, and use
    # the LAST (most-recent / current) as the single goal used for bucketing.
    goal_raw = nv("Goal for the Sprint")
    goal_list: list[str] = []
    if isinstance(goal_raw, list):
        for g in goal_raw:
            v = g.get("value") if isinstance(g, dict) else str(g)
            if v:
                goal_list.append(v)
    elif isinstance(goal_raw, dict):
        v = goal_raw.get("value")
        if v:
            goal_list.append(v)
    elif goal_raw:
        goal_list.append(str(goal_raw))
    rec["goals"] = goal_list
    rec["goal"] = goal_list[-1] if goal_list else ""
    rec["epic_link"] = _opt(nv("Epic Link"))

    # Sprint field: list of sprint objects -> keep every sprint name (history).
    sprint_raw = nv("Sprint")
    sprints: list[str] = []
    if isinstance(sprint_raw, list):
        for s in sprint_raw:
            nm = s.get("name", "") if isinstance(s, dict) else str(s)
            if nm:
                sprints.append(nm)
    rec["sprints"] = sprints
    rec["sprint"] = sprints[-1] if sprints else ""

    rec["dates"] = {name: f.get(fid) for name, fid in date_ids.items() if f.get(fid) is not None}
    return rec


# ── scope expansion (parents / children / bugs) ──────────────────────────────
def _chunks(seq, n=50):
    seq = list(seq)
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def _expand_scope(ctx: dict[str, Any], seed_keys: set[str],
                  epic_link_id: str | None) -> set[str]:
    """Given issues that carry the sprint, add the retro-relevant neighbourhood:
      * parent Stories of in-sprint sub-tasks / bugs
      * owning Epics of in-sprint Stories (parent link or classic Epic Link)
      * sub-tasks of in-sprint Stories
      * bugs/defects parented to in-sprint Stories/Epics (even without a sprint)
    """
    keys = set(seed_keys)
    if not keys:
        return keys

    lite = ["parent", "issuetype"] + ([epic_link_id] if epic_link_id else [])

    # 1) climb: parents + epic links of the seed
    story_keys: set[str] = set()
    for chunk in _chunks(keys):
        jql = f"key in ({','.join(chunk)})"
        for iss in _search(ctx, jql, lite):
            f = iss.get("fields", {})
            it = (f.get("issuetype") or {}).get("name", "")
            p = (f.get("parent") or {}).get("key")
            el = f.get(epic_link_id) if epic_link_id else None
            if p:
                keys.add(p)
            if el:
                keys.add(el)
            if it == "Story":
                story_keys.add(iss["key"])
            # a seed sub-task's parent is a Story -> track it for child expansion
            if it in ("Sub-task", "Sub-Task", "Subtask") and p:
                story_keys.add(p)

    # Also treat any Story already in keys as an expansion root.
    for chunk in _chunks(keys):
        for iss in _search(ctx, f"key in ({','.join(chunk)})", ["issuetype"]):
            if (iss.get("fields", {}).get("issuetype") or {}).get("name") == "Story":
                story_keys.add(iss["key"])

    # 2) descend from Stories: sub-tasks + parented bugs/defects
    for chunk in _chunks(story_keys):
        pc = ",".join(chunk)
        for iss in _search(ctx, f"parent in ({pc})", ["parent"]):
            keys.add(iss["key"])
        if epic_link_id:
            try:
                for iss in _search(ctx, f'"Epic Link" in ({pc}) AND issuetype in (Bug, Defect)', ["parent"]):
                    keys.add(iss["key"])
            except Exception:
                pass

    return keys


# ── main snapshot ────────────────────────────────────────────────────────────
def snapshot(wave: str, sprint_names: list[str] | None, match: str | None,
             with_changelog: bool = True, ctx: dict[str, Any] | None = None) -> dict[str, Any]:
    if ctx is None:
        ctx = jira_auth.get_context()
        health = jira_auth.test_auth(ctx)
        if not health.get("ok"):
            raise RuntimeError(f"JIRA auth failed: {health.get('error')}")
        print(f"  Authenticated to JIRA as {health.get('account') or '?'}")

    named, date_ids = _field_catalog(ctx)
    epic_link_id = named.get("Epic Link")
    print(f"  Resolved {len(named)} named fields, {len(date_ids)} custom date field(s).")

    # Which sprints?
    if sprint_names:
        sprints = _resolve_named_sprints(ctx, sprint_names)
        missing = {n.strip().lower() for n in sprint_names} - {
            (s.get("name") or "").strip().lower() for s in sprints}
        if missing:
            print(f"  [warn] could not resolve: {sorted(missing)}")
    else:
        sprints = _discover_sprints(ctx, match)

    if not sprints:
        raise RuntimeError("No sprints found to snapshot. Pass --sprint \"MPM <Team> Sprint 1\" "
                           "or check that the boards have an active sprint.")

    print(f"  Capturing {len(sprints)} sprint(s):")
    for s in sprints:
        print(f"    - {s.get('name')}  (id={s.get('id')}, state={s.get('state')}, "
              f"board={s.get('boardId')})")

    sprint_ids = [s["id"] for s in sprints]

    base_fields = ["summary", "issuetype", "status", "assignee", "parent", "priority",
                   "created", "resolutiondate", "labels", "fixVersions",
                   "timeoriginalestimate", "timespent",
                   "aggregatetimeoriginalestimate", "aggregatetimespent"]
    fields = base_fields + list(dict.fromkeys(list(named.values()) + list(date_ids.values())))

    # Seed = everything carrying any of the target sprints.
    id_list = ",".join(str(i) for i in sprint_ids)
    seed = _search(ctx, f"Sprint in ({id_list}) ORDER BY key ASC", ["issuetype"])
    seed_keys = {i["key"] for i in seed}
    print(f"  {len(seed_keys)} issue(s) carry the sprint(s); expanding scope ...")

    all_keys = _expand_scope(ctx, seed_keys, epic_link_id)
    print(f"  {len(all_keys)} issue(s) after adding parents/epics/sub-tasks/bugs.")

    # Full fetch of the whole set.
    records: list[dict[str, Any]] = []
    key_list = sorted(all_keys)
    n = 0
    for chunk in _chunks(key_list, 100):
        for iss in _search(ctx, f"key in ({','.join(chunk)}) ORDER BY key ASC", fields):
            rec = _issue_record(iss, named, date_ids)
            rec["team_from_sprint"] = _team_from_sprint(rec.get("sprint") or "")
            rec["in_sprint"] = iss["key"] in seed_keys
            if with_changelog:
                rec["status_history"] = _status_history(ctx, iss["key"])
            records.append(rec)
            n += 1
            if n % 50 == 0:
                print(f"    ... {n}/{len(all_keys)}")

    # Per-sprint metadata + best-effort sprint report.
    sprint_meta = []
    for s in sprints:
        meta = {
            "id": s.get("id"), "name": s.get("name"), "state": s.get("state"),
            "boardId": s.get("boardId"), "goal": s.get("goal", ""),
            "startDate": s.get("startDate", ""), "endDate": s.get("endDate", ""),
            "completeDate": s.get("completeDate", ""),
            "team": _team_from_sprint(s.get("name") or ""),
        }
        meta["report"] = _sprint_report(ctx, s.get("boardId"), s.get("id"))
        sprint_meta.append(meta)

    return {
        "wave": wave,
        "snapshot_date": date.today().isoformat(),
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "project": ctx["project"],
        "source": "jira",
        "sprints": sprint_meta,
        "issue_count": len(records),
        "seed_count": len(seed_keys),
        "with_changelog": with_changelog,
        "issues": records,
    }


def _write(snap: dict[str, Any]) -> Path:
    out_dir = _REPO / "snapshots" / "jira_sprint" / snap["wave"]
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{snap['snapshot_date']}.json"
    out_path.write_text(json.dumps(snap, indent=2, default=str), encoding="utf-8")
    dates = sorted({p.stem for p in out_dir.glob("*.json") if p.stem != "_index"})
    (out_dir / "_index.json").write_text(
        json.dumps({"wave": snap["wave"], "dates": dates}, indent=2), encoding="utf-8")
    return out_path


def _slug(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "-", s.strip()).strip("-") or "wave"


def main() -> int:
    ap = argparse.ArgumentParser(description="Freeze a JIRA sprint wave for the retro dashboard.")
    ap.add_argument("--wave", default=None,
                    help="Folder label for the output (default: derived from sprint names, "
                         "e.g. 'Sprint-1').")
    ap.add_argument("--sprint", action="append", default=[],
                    help="Exact sprint name to capture (repeatable). If omitted, auto-detects "
                         "every ACTIVE sprint on the MPM boards.")
    ap.add_argument("--match", default=None,
                    help="When auto-detecting, only keep sprints whose name contains this text "
                         "(e.g. \"Sprint 1\").")
    ap.add_argument("--no-changelog", action="store_true",
                    help="Skip per-issue status changelog (faster, but state-at-sprint-end and "
                         "time-in-status become approximate).")
    args = ap.parse_args()

    # Derive a wave label if not given.
    wave = args.wave
    if not wave:
        if args.sprint:
            m = re.search(r"(sprint\s*\d+)", args.sprint[0], re.I)
            wave = _slug(m.group(1)) if m else _slug(args.sprint[0])
        else:
            wave = "active"

    print(f"Freezing JIRA sprint wave -> label {wave!r} "
          f"(changelog={'off' if args.no_changelog else 'on'}) ...")
    snap = snapshot(wave, args.sprint or None, args.match,
                    with_changelog=not args.no_changelog)
    out = _write(snap)
    size_kb = out.stat().st_size // 1024
    print(f"\n✅ Wrote {out}  ({snap['issue_count']} issues, {snap['seed_count']} in-sprint, {size_kb} KB)")
    print(f"   Sprints captured: {', '.join(s['name'] for s in snap['sprints'])}")
    print(f"   Next: generate the retro from this frozen file (no JIRA needed).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
