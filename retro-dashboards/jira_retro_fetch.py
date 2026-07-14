"""
jira_retro_data.py
===================
JIRA equivalent of retro_data.load_retro_data(), scoped to a JIRA Sprint
(e.g. "MPM Sprint 1"). Returns the SAME retro_data.RetroData container the
internal-retro generator already consumes, so the combined per-team retro can
produce the canonical RetroData the retro renderers consume.

Scope rules (confirmed):
  * PBIs  = EPICS ONLY — an Epic counts as a PBI if it has at least one Story in
            the sprint. Standalone Tasks are NOT counted as PBIs. The Epic's
            State is rolled up from its most-advanced in-sprint Story.
  * Tasks = sub-tasks of those in-sprint Stories (carry the hours).
  * Bugs  = bugs/defects whose PARENT Story/Epic is in the sprint (even if the
            bug itself has no Sprint field), unioned with any bug that does
            carry the sprint. Enriched with Root Cause / RCA and owning Epic.

Team attribution is left to the generator's member_to_team(assignee) (same people
across the sprint). Capacity is empty here.

CLI:
    python scripts/retro/jira_retro_data.py "MPM Sprint 1"
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pandas as pd

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))          # local retro + jira helper modules

import jira_auth   # noqa: E402
import jira_fetch  # noqa: E402
import retro_data  # noqa: E402  -- RetroData + root-cause helpers

# JIRA custom fields (confirmed via jira/Probe-JiraFields.py).
F_ROOT_CAUSE = "customfield_11563"   # Root Cause
F_RCA        = "customfield_11562"   # Root Cause Analysis
F_EPIC_LINK  = "customfield_10014"   # Epic Link (classic story->epic)
F_TEAM       = "customfield_10001"   # Team (e.g. "Calmers - RCM")

_BUG_EXTRA_COLS = ["parent_pbi_id", "root_cause_type", "root_cause_type_raw",
                   "root_cause_analysis", "first_assignee", "state_at_sprint_end",
                   "Team"]
_CANON_COLS = ["ID", "Title", "Work Item Type", "State", "Assigned To",
               "Iteration Path", "Tags", "Original Estimate", "Completed Work"]


def _opt_value(v: Any) -> str:
    if isinstance(v, dict):
        return v.get("value") or v.get("name") or ""
    if isinstance(v, list) and v:
        first = v[0]
        return first.get("value", "") if isinstance(first, dict) else str(first)
    return v or ""


def _adf_text(node: Any) -> str:
    """Flatten an Atlassian Document Format (ADF) value to plain text."""
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


def _assignee(f: dict[str, Any]) -> str:
    a = f.get("assignee") or {}
    return a.get("displayName", "") if isinstance(a, dict) else ""


def _team_value(v: Any) -> str:
    """JIRA Team field -> display name (handles object or string)."""
    if isinstance(v, dict):
        return v.get("name") or v.get("value") or v.get("title") or ""
    if isinstance(v, list) and v:
        first = v[0]
        return first.get("name", "") if isinstance(first, dict) else str(first)
    return v or ""


def _search(ctx: dict[str, Any], jql: str, fields: list[str]) -> list[dict[str, Any]]:
    """POST /rest/api/3/search/jql with nextPageToken paging."""
    out: list[dict[str, Any]] = []
    token = None
    while True:
        payload: dict[str, Any] = {"jql": jql, "maxResults": 100, "fields": fields}
        if token:
            payload["nextPageToken"] = token
        r = ctx["session"].post(f"{ctx['api_v3']}/search/jql", json=payload,
                                timeout=ctx["timeout"])
        if not r.ok:
            raise RuntimeError(f"JQL failed ({r.status_code}): {r.text[:200]}\nJQL: {jql}")
        body = r.json()
        out.extend(body.get("issues", []))
        token = body.get("nextPageToken")
        if not token or body.get("isLast"):
            break
    return out


def _insprint_story_keys(ctx: dict[str, Any], sprint: str) -> tuple[set[str], set[str]]:
    """Return (story_keys, epic_keys) for Stories in the sprint. epic_keys is the
    set of Epics that own at least one in-sprint Story (= the retro's PBIs)."""
    story_keys: set[str] = set()
    epic_keys: set[str] = set()
    jql = f'project = "{ctx["project"]}" AND Sprint = "{sprint}" AND issuetype = Story'
    for iss in _search(ctx, jql, ["parent", F_EPIC_LINK]):
        story_keys.add(iss["key"])
        f = iss.get("fields", {})
        ep = (f.get("parent") or {}).get("key") or f.get(F_EPIC_LINK)
        if ep:
            epic_keys.add(ep)
    return story_keys, epic_keys


def _fetch_team_map(ctx: dict[str, Any], keys: list[str]) -> dict[str, str]:
    """Map issue key -> JIRA Team field value (e.g. 'Calmers - RCM')."""
    keys = sorted({k for k in keys if k})
    out: dict[str, str] = {}
    for i in range(0, len(keys), 50):
        chunk = ",".join(keys[i:i + 50])
        for iss in _search(ctx, f"key in ({chunk})", [F_TEAM]):
            out[iss["key"]] = _team_value(iss.get("fields", {}).get(F_TEAM))
    return out


def _resolve_epics(ctx: dict[str, Any], keys: list[str]) -> dict[str, str]:
    """Map an issue key -> its owning Epic key (parent or Epic Link); Epics map
    to themselves. Used to attribute a bug to its PBI (Epic)."""
    keys = sorted({k for k in keys if k})
    out: dict[str, str] = {}
    for i in range(0, len(keys), 50):
        chunk = ",".join(keys[i:i + 50])
        for iss in _search(ctx, f"key in ({chunk})", ["parent", F_EPIC_LINK, "issuetype"]):
            f = iss.get("fields", {})
            if (f.get("issuetype") or {}).get("name") == "Epic":
                out[iss["key"]] = iss["key"]
            else:
                out[iss["key"]] = (f.get("parent") or {}).get("key") or f.get(F_EPIC_LINK) or iss["key"]
    return out


def _fetch_bugs(ctx: dict[str, Any], sprint: str,
                parent_keys: set[str]) -> pd.DataFrame:
    """Bugs/Defects whose parent Story/Epic is in the sprint, unioned with any
    bug that carries the sprint itself. Returns a canonical+extras DataFrame."""
    fields = ["summary", "status", "assignee", "parent", F_EPIC_LINK,
              F_ROOT_CAUSE, F_RCA, "timeoriginalestimate", "timespent", "labels", F_TEAM]
    found: dict[str, dict[str, Any]] = {}
    pk = sorted(parent_keys)
    for i in range(0, len(pk), 50):
        chunk = ",".join(pk[i:i + 50])
        # Bugs parented to an in-sprint Story/Epic (modern parent link).
        for iss in _search(ctx, f"parent in ({chunk}) AND issuetype in (Bug, Defect)", fields):
            found[iss["key"]] = iss
        # Bugs linked to an in-sprint Epic via the classic Epic Link.
        try:
            for iss in _search(ctx, f'"Epic Link" in ({chunk}) AND issuetype in (Bug, Defect)', fields):
                found[iss["key"]] = iss
        except Exception:
            pass
    # Plus any bug that carries the sprint itself.
    for iss in _search(ctx, f'project = "{ctx["project"]}" AND Sprint = "{sprint}" '
                            f'AND issuetype in (Bug, Defect)', fields):
        found[iss["key"]] = iss

    if not found:
        return pd.DataFrame(columns=_CANON_COLS + _BUG_EXTRA_COLS)

    bug_parents = [(b["fields"].get("parent") or {}).get("key") for b in found.values()]
    epic_of = _resolve_epics(ctx, [p for p in bug_parents if p])

    rows = []
    for iss in found.values():
        f = iss["fields"]
        parent = (f.get("parent") or {}).get("key")
        rc = _opt_value(f.get(F_ROOT_CAUSE))
        state = (f.get("status") or {}).get("name", "") or ""
        rows.append({
            "ID": iss["key"], "Title": f.get("summary", "") or "", "Work Item Type": "Bug",
            "State": state, "Assigned To": _assignee(f), "Iteration Path": sprint,
            "Tags": ", ".join(f.get("labels") or []),
            "Original Estimate": (f.get("timeoriginalestimate") or 0) / 3600.0,
            "Completed Work": (f.get("timespent") or 0) / 3600.0,
            "parent_pbi_id": epic_of.get(parent, parent),
            "root_cause_type_raw": rc,
            "root_cause_type": retro_data._normalise_root_cause(rc),
            "root_cause_analysis": _adf_text(f.get(F_RCA)).strip(),
            "first_assignee": _assignee(f),          # v1: current (no changelog)
            "state_at_sprint_end": state,            # v1: current
            "Team": _team_value(f.get(F_TEAM)),
        })
    return pd.DataFrame(rows, columns=_CANON_COLS + _BUG_EXTRA_COLS)


_FIELD_CACHE: dict[str, str] = {}


def _field_id_by_name(ctx: dict[str, Any], name: str, default: str | None = None) -> str | None:
    """Resolve a field's id from its display name via /rest/api/3/field (cached)."""
    if not _FIELD_CACHE:
        try:
            r = ctx["session"].get(f"{ctx['api_v3']}/field", timeout=ctx["timeout"])
            if r.ok:
                for fobj in r.json():
                    _FIELD_CACHE[(fobj.get("name") or "").strip().lower()] = fobj.get("id")
        except Exception:
            pass
    return _FIELD_CACHE.get(name.strip().lower(), default)


def _user_name(v: Any) -> str:
    if isinstance(v, dict):
        return v.get("displayName") or v.get("name") or ""
    if isinstance(v, list) and v:
        return v[0].get("displayName", "") if isinstance(v[0], dict) else str(v[0])
    return v or ""


def _story_dev_qa_map(ctx: dict[str, Any], story_keys: set[str],
                      dev_field: str | None, qa_field: str | None) -> dict[str, dict[str, str]]:
    """story key -> {'dev': dev-assignee name, 'qa': qa-assignee name}."""
    out: dict[str, dict[str, str]] = {}
    fields = [f for f in (dev_field, qa_field) if f]
    if not fields:
        return out
    keys = sorted(story_keys)
    for i in range(0, len(keys), 50):
        chunk = ",".join(keys[i:i + 50])
        for iss in _search(ctx, f"key in ({chunk})", fields):
            f = iss.get("fields", {})
            out[iss["key"]] = {
                "dev": _user_name(f.get(dev_field)) if dev_field else "",
                "qa":  _user_name(f.get(qa_field)) if qa_field else "",
            }
    return out


def _task_parent_map(ctx: dict[str, Any], story_keys: set[str]) -> dict[str, str]:
    """sub-task key -> parent Story key."""
    out: dict[str, str] = {}
    keys = sorted(story_keys)
    for i in range(0, len(keys), 50):
        chunk = ",".join(keys[i:i + 50])
        for iss in _search(ctx, f"parent in ({chunk}) AND issuetype = Sub-task", ["parent"]):
            out[iss["key"]] = (iss.get("fields", {}).get("parent") or {}).get("key")
    return out


def load_retro_data_jira(sprint: str,
                         sprint_number: int = 0,
                         ctx: dict[str, Any] | None = None) -> "retro_data.RetroData":
    """Return a RetroData for a JIRA Sprint (e.g. 'MPM Sprint 1')."""
    if ctx is None:
        ctx = jira_auth.get_context()
        health = jira_auth.test_auth(ctx)
        if not health.get("ok"):
            raise RuntimeError(f"JIRA auth failed: {health.get('error')}")
        print(f"  Authenticated to JIRA as {health.get('account') or '?'}")

    # Canonical pull (Epic-rollup PBIs + sub-task hours).
    df = jira_fetch.load_dataframe(sprint=sprint, sprint_number=sprint_number, ctx=ctx)

    # PBIs = EPICS ONLY: keep PBI rows whose ID is an Epic that owns an in-sprint
    # Story. This drops standalone Tasks that load_dataframe promotes to PBIs.
    story_keys, epic_keys = _insprint_story_keys(ctx, sprint)
    pbis_df = df[(df["Work Item Type"] == "Product Backlog Item")
                 & (df["ID"].isin(epic_keys))].copy().reset_index(drop=True)

    # JIRA Team field per Epic — lets the combiner attribute a PBI to its squad
    # even when the assignee is an off-roster lead/BA (shrinks the "Other" bucket).
    team_map = _fetch_team_map(ctx, list(epic_keys))
    pbis_df["Team"] = pbis_df["ID"].map(team_map).fillna("") if not pbis_df.empty else []

    tasks_df = df[df["Work Item Type"] == "Task"].copy().reset_index(drop=True)
    tasks_df["Team"] = ""   # tasks attribute by assignee / effective-assignee

    # For sub-tasks whose own assignee is off-roster, attribute via the parent
    # Story's Dev Assignee ([Dev ...] tasks) or QA Assignee ([QA ...] tasks).
    dev_field = _field_id_by_name(ctx, "Dev Assignee", "customfield_11975")
    qa_field  = _field_id_by_name(ctx, "QA Assignee")
    story_da = _story_dev_qa_map(ctx, story_keys, dev_field, qa_field)
    task_parent = _task_parent_map(ctx, story_keys)

    def _effective_assignee(row) -> str:
        title = str(row.get("Title", "")).strip().lower()
        st = story_da.get(task_parent.get(row.get("ID")), {})
        if title.startswith("[dev"):
            return st.get("dev", "")
        if title.startswith("[qa"):
            return st.get("qa", "")
        return ""

    tasks_df["effective_assignee"] = (
        tasks_df.apply(_effective_assignee, axis=1) if not tasks_df.empty else [])

    # Bugs scoped by parent (in-sprint Story/Epic), not the bug's own Sprint field.
    parent_scope = set(story_keys) | set(epic_keys)
    bugs_df = _fetch_bugs(ctx, sprint, parent_scope)

    return retro_data.RetroData(
        pbis_df=pbis_df,
        tasks_df=tasks_df,
        bugs_df=bugs_df,
        iteration_paths=[sprint],
        sprint_name=sprint,
        member_capacity={},
    )


# ─────────────────────────────────────────────────────────────────────────────
# OFFLINE PATH — rebuild RetroData from a frozen sprint snapshot (no JIRA).
# Produced by Snapshot-JiraSprint.py. Lets the retro dashboard be generated
# / regenerated AFTER the boards have rolled to the next sprint, from the exact
# point-in-time state captured before the move.
# ─────────────────────────────────────────────────────────────────────────────

# Status ladder (least → most advanced) for rolling an Epic's State up from its
# in-sprint Stories. Unknown statuses rank -1 (any known state beats them).
_STATE_LADDER = [
    "backlog", "new", "to do", "open", "reopened",
    "ba analysis", "analysis", "ready for tech analysis", "tech analysis",
    "refinement", "ready for development", "selected for development",
    "in progress", "development", "in development",
    "code review", "pr", "in review", "dev completed", "dev complete",
    "qa in progress", "in qa", "qa", "testing",
    "st to do", "st in progress", "system testing",
    "ready for live", "uat", "ready for release",
    "live", "done", "closed", "resolved", "released",
]
_STATE_RANK = {s: i for i, s in enumerate(_STATE_LADDER)}


def _rank(state: str) -> int:
    return _STATE_RANK.get((state or "").strip().lower(), -1)


def _epic_of(rec: dict[str, Any]) -> str:
    """Owning Epic key of a record: parent link first, then classic Epic Link."""
    return rec.get("parent") or rec.get("epic_link") or ""


def _canon_type(t: str) -> str:
    t = (t or "").strip().lower()
    if t == "epic":
        return "Product Backlog Item"
    if t in ("sub-task", "subtask", "sub task", "task"):
        return "Task"
    if t in ("bug", "defect"):
        return "Bug"
    return t.title()


def load_retro_data_jira_from_snapshot(snapshot_path: str,
                                       team: str | None = None,
                                       sprint: str | None = None) -> "retro_data.RetroData":
    """Rebuild a RetroData from a frozen sprint snapshot JSON.

    team   : keep only the sprint(s) for this team (e.g. "Knackers"), matched via
             the snapshot's per-sprint `team` (derived from the sprint name).
    sprint : keep only this exact sprint name (overrides `team`).
    Capacity is empty (attach separately if needed).
    """
    snap = json.loads(Path(snapshot_path).read_text(encoding="utf-8"))
    issues = snap.get("issues", [])
    by_key = {r["key"]: r for r in issues}

    # Which sprint name(s) are in scope?
    sprint_meta = snap.get("sprints", [])
    if sprint:
        scope_names = {sprint}
    elif team:
        tl = team.strip().lower()
        scope_names = {m["name"] for m in sprint_meta
                       if (m.get("team") or "").strip().lower() == tl}
        if not scope_names:  # fall back to substring match on the sprint name
            scope_names = {m["name"] for m in sprint_meta
                           if tl in (m.get("name") or "").lower()}
    else:
        scope_names = {m["name"] for m in sprint_meta} or None  # None = all

    def _in_scope(rec: dict[str, Any]) -> bool:
        if not rec.get("in_sprint"):
            return False
        if scope_names is None:
            return True
        names = set(rec.get("sprints") or ([rec.get("sprint")] if rec.get("sprint") else []))
        return bool(names & scope_names)

    label = sprint or (f"{team} Sprint" if team else snap.get("wave", "Sprint"))

    # In-sprint Stories → the Epics that own them (= PBIs).
    in_sprint_stories = [r for r in issues
                         if _in_scope(r) and (r.get("type") or "").lower() == "story"]
    story_keys = {r["key"] for r in in_sprint_stories}
    epic_keys: set[str] = set()
    epic_story_states: dict[str, list[str]] = {}
    for st in in_sprint_stories:
        ek = _epic_of(st)
        if ek:
            epic_keys.add(ek)
            epic_story_states.setdefault(ek, []).append(st.get("status", ""))

    # ── PBIs (Epics), State rolled up from the most-advanced in-sprint Story ──
    pbi_rows = []
    for ek in sorted(epic_keys):
        ep = by_key.get(ek, {})
        rolled = max(epic_story_states.get(ek, [""]), key=_rank) if epic_story_states.get(ek) else ep.get("status", "")
        pbi_rows.append({
            "ID": ek, "Title": ep.get("summary", ""), "Work Item Type": "Product Backlog Item",
            "State": rolled or ep.get("status", ""),
            "Assigned To": ep.get("assignee", ""),
            "Iteration Path": label,
            "Tags": ", ".join(ep.get("labels") or []),
            "Original Estimate": ep.get("agg_original_estimate_h") or ep.get("original_estimate_h") or 0.0,
            "Completed Work": ep.get("agg_time_spent_h") or ep.get("time_spent_h") or 0.0,
            "Team": ep.get("team") or _team_of_sprint_name(ep, sprint_meta),
        })
    pbis_df = pd.DataFrame(pbi_rows, columns=_CANON_COLS + ["Team"])

    # ── Tasks (sub-tasks of in-sprint Stories) ───────────────────────────────
    task_rows = []
    for r in issues:
        if (r.get("type") or "").lower() not in ("sub-task", "subtask", "sub task", "task"):
            continue
        if r.get("parent") not in story_keys:
            continue
        parent = by_key.get(r.get("parent"), {})
        title = (r.get("summary") or "").strip().lower()
        eff = ""
        if title.startswith("[dev"):
            eff = parent.get("dev_assignee", "")
        elif title.startswith("[qa"):
            eff = parent.get("qa_assignee", "")
        task_rows.append({
            "ID": r["key"], "Title": r.get("summary", ""), "Work Item Type": "Task",
            "State": r.get("status", ""), "Assigned To": r.get("assignee", ""),
            "Iteration Path": label, "Tags": ", ".join(r.get("labels") or []),
            "Original Estimate": r.get("original_estimate_h") or 0.0,
            "Completed Work": r.get("time_spent_h") or 0.0,
            "Team": "", "effective_assignee": eff,
        })
    tasks_df = pd.DataFrame(task_rows, columns=_CANON_COLS + ["Team", "effective_assignee"])

    # ── Bugs (parented to in-sprint Story/Epic, or carrying the sprint) ───────
    parent_scope = story_keys | epic_keys
    bug_rows = []
    for r in issues:
        if (r.get("type") or "").lower() not in ("bug", "defect"):
            continue
        keep = (r.get("parent") in parent_scope) or (r.get("epic_link") in parent_scope) or _in_scope(r)
        if not keep:
            continue
        parent = r.get("parent") or ""
        epic_owner = parent if parent in epic_keys else _epic_of(by_key.get(parent, {})) or parent
        rc_raw = r.get("root_cause", "")
        state = r.get("status", "")
        bug_rows.append({
            "ID": r["key"], "Title": r.get("summary", ""), "Work Item Type": "Bug",
            "State": state, "Assigned To": r.get("assignee", ""),
            "Iteration Path": label, "Tags": ", ".join(r.get("labels") or []),
            "Original Estimate": r.get("original_estimate_h") or 0.0,
            "Completed Work": r.get("time_spent_h") or 0.0,
            "parent_pbi_id": epic_owner or None,
            "root_cause_type_raw": rc_raw,
            "root_cause_type": retro_data._normalise_root_cause(rc_raw),
            "root_cause_analysis": r.get("root_cause_analysis", ""),
            "first_assignee": r.get("assignee", ""),
            "state_at_sprint_end": state,
            "Team": r.get("team", ""),
        })
    bugs_df = pd.DataFrame(bug_rows, columns=_CANON_COLS + _BUG_EXTRA_COLS)

    iters = sorted(scope_names) if scope_names else [m["name"] for m in sprint_meta]
    return retro_data.RetroData(
        pbis_df=pbis_df, tasks_df=tasks_df, bugs_df=bugs_df,
        iteration_paths=iters or [label], sprint_name=label, member_capacity={},
    )


def _team_of_sprint_name(rec: dict[str, Any], sprint_meta: list[dict]) -> str:
    """Best-effort team from the record's sprint name via the snapshot metadata."""
    names = set(rec.get("sprints") or ([rec.get("sprint")] if rec.get("sprint") else []))
    for m in sprint_meta:
        if m.get("name") in names and m.get("team"):
            return m["team"]
    return ""


if __name__ == "__main__":
    # Offline mode:  python jira_retro_data.py --snapshot <path> [--team Knackers]
    if "--snapshot" in sys.argv:
        i = sys.argv.index("--snapshot")
        path = sys.argv[i + 1]
        team = None
        if "--team" in sys.argv:
            team = sys.argv[sys.argv.index("--team") + 1]
        print(f"Loading JIRA retro data from snapshot {path!r}"
              f"{f' (team={team})' if team else ''} ...\n")
        data = load_retro_data_jira_from_snapshot(path, team=team)
        print(data.summary())
        sys.exit(0)

    sn = sys.argv[1] if len(sys.argv) > 1 else "MPM Sprint 1"
    print(f"Loading JIRA retro data for sprint '{sn}' ...\n")
    data = load_retro_data_jira(sn)
    print(data.summary())
    if not data.bugs_df.empty:
        print("\n─── Bug RCA sample (first 8) ───")
        cols = [c for c in ["ID", "State", "Assigned To", "parent_pbi_id",
                            "root_cause_type"] if c in data.bugs_df.columns]
        print(data.bugs_df[cols].head(8).to_string(index=False))
        print("\n─── root_cause_type distribution ───")
        print(data.bugs_df["root_cause_type"].value_counts().to_string())
    if not data.pbis_df.empty:
        print("\n─── PBIs (Epics, first 8) ───")
        cols = [c for c in ["ID", "Title", "State", "Assigned To"] if c in data.pbis_df.columns]
        print(data.pbis_df[cols].head(8).to_string(index=False))
