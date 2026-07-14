#!/usr/bin/env python3
"""
Sprint Dashboard Generator
===========================
Usage:
    python generate_sprint_dashboard.py

Edit sprint_dashboard_config.py for sprint-specific settings.
Edit corrections.json for per-item overrides (no Excel editing needed).

REFERENCE TEMPLATE
------------------
Sprint_Dashboard_Template.html  ← FROZEN canonical layout (frozen 2026-04-15)
All structural changes (tabs, CSS, JS, section order) MUST match the template.
Do not alter the template itself — update this generator to stay in sync.

Zero Claude tokens — runs 100% locally.
"""

import os, sys, json, math, re
import pandas as pd
from pathlib import Path
from datetime import date

# ── JIRA Cloud auth (inlined — no separate jira_auth import needed) ─────────────
# Single source of truth for authenticating to Atlassian Cloud. Mirrors the old
# jira_auth.py so the generator is self-contained. Credentials come from repo-root
# dotfiles (.jira_pat / .jira_email / .jira_site) or env vars.
import requests
from requests.auth import HTTPBasicAuth

JIRA_DEFAULT_SITE = "https://motivity.atlassian.net"
JIRA_DEFAULT_EMAIL = ""
JIRA_PROJECT_KEY = "MPM"
JIRA_API_VERSION = "3"
JIRA_TIMEOUT = 30


def _jira_read_dotfile(name):
    """Read a single-line secret file from the repo root or this folder."""
    here = Path(__file__).resolve().parent
    for candidate in (here.parent / name, here / name):
        if candidate.exists():
            val = candidate.read_text(encoding="utf-8").strip()
            if val:
                return val
    return None


def jira_get_context():
    """Return the context dict consumed by jira_fetch (base_url, session, etc.)."""
    email = (_jira_read_dotfile(".jira_email") or os.environ.get("JIRA_EMAIL")
             or JIRA_DEFAULT_EMAIL)
    if not email:
        raise ValueError("JIRA account email not found. Put it in '.jira_email' "
                         "at the repo root or set the JIRA_EMAIL env var.")
    token = _jira_read_dotfile(".jira_pat") or os.environ.get("JIRA_API_TOKEN")
    if not token:
        raise FileNotFoundError(
            "JIRA API token not found. Create a single-line file '.jira_pat' at the "
            "repo root, or set JIRA_API_TOKEN. Generate one at "
            "https://id.atlassian.com/manage-profile/security/api-tokens")
    base = (_jira_read_dotfile(".jira_site") or os.environ.get("JIRA_SITE")
            or JIRA_DEFAULT_SITE).rstrip("/")
    session = requests.Session()
    session.auth = HTTPBasicAuth(email, token)
    session.headers.update({"Accept": "application/json",
                            "Content-Type": "application/json",
                            "User-Agent": "em-standup-jira/1.0"})
    return {"base_url": base, "api_v3": f"{base}/rest/api/{JIRA_API_VERSION}",
            "agile": f"{base}/rest/agile/1.0", "project": JIRA_PROJECT_KEY,
            "email": email, "session": session, "timeout": JIRA_TIMEOUT}


def jira_test_auth(ctx=None):
    """Health check via /myself. Confirms site URL + email + token line up."""
    if ctx is None:
        ctx = jira_get_context()
    url = f"{ctx['api_v3']}/myself"
    try:
        r = ctx["session"].get(url, timeout=ctx["timeout"])
    except requests.exceptions.ConnectionError as e:
        return {"ok": False, "error": f"Connection error: {e}",
                "hint": "Check the site URL in .jira_site / JIRA_DEFAULT_SITE."}
    if r.status_code in (401, 403):
        return {"ok": False, "status": r.status_code, "error": "Auth rejected.",
                "hint": "Email + API token mismatch, or token revoked."}
    if not r.ok:
        return {"ok": False, "status": r.status_code, "error": r.text[:300]}
    me = r.json()
    return {"ok": True, "account": me.get("displayName"),
            "email": me.get("emailAddress"), "account_id": me.get("accountId")}


def _jira_browse_base():
    return (_jira_read_dotfile(".jira_site") or os.environ.get("JIRA_SITE")
            or JIRA_DEFAULT_SITE).rstrip("/")


def _issue_link(issue_id, color="#94a3b8"):
    """Render an issue key as a clickable link to the JIRA issue."""
    iid = str(issue_id)
    return (f'<a href="{_jira_browse_base()}/browse/{iid}" target="_blank" '
            f'rel="noopener" style="color:{color};text-decoration:none;font-weight:600">#{iid}</a>')


def _jira_base_url():
    """Site URL for building issue links (e.g. https://motivity.atlassian.net)."""
    site = (_jira_read_dotfile(".jira_site") or os.environ.get("JIRA_SITE")
            or JIRA_DEFAULT_SITE)
    return site.rstrip("/")


def _issue_link(issue_id):
    """Render a JIRA issue key as a clickable link to that issue."""
    iid = str(issue_id)
    return (f'<a href="{_jira_base_url()}/browse/{iid}" target="_blank" '
            f'style="color:#2563eb;text-decoration:none;font-weight:600">{iid}</a>')


# ── Load config ────────────────────────────────────────────────────────────────
script_dir = Path(__file__).parent
sys.path.insert(0, str(script_dir))
import sprint_dashboard_config as CFG
from dashboard_tabs_extra import (
    load_history, save_history, take_snapshot, upsert_snapshot,
    build_daily_tracking_tab, build_dsm_tab, build_risk_health_tab,
)

# ── Load corrections ───────────────────────────────────────────────────────────
corrections_path = script_dir / "corrections.json"
CORRECTIONS = {}
if corrections_path.exists():
    with open(corrections_path) as f:
        CORRECTIONS = json.load(f)

EXCLUDED_IDS = set(map(int, CORRECTIONS.get("excluded_ids", CFG.EXCLUDED_IDS)))
PBI_STATE_OVERRIDES = {int(k): v for k, v in CORRECTIONS.get("pbi_state_overrides", {}).items()
                       if not k.startswith(("example", "_"))}
GOAL_OVERRIDES = {int(k): v for k, v in CORRECTIONS.get("goal_overrides", {}).items()
                  if not k.startswith(("example", "_"))}

# ── Colour / style helpers ─────────────────────────────────────────────────────
STATE_COLORS = {
    "Done":             ("#dcfce7", "#16a34a"),
    "LIVE":             ("#dcfce7", "#16a34a"),
    "Ready For LIVE":   ("#d1fae5", "#059669"),
    "ST To Do":         ("#ede9fe", "#7c3aed"),
    "Dev Completed":    ("#dbeafe", "#1d4ed8"),
    "QA Complete":      ("#dbeafe", "#1d4ed8"),
    "Dev In Progress":  ("#fef3c7", "#d97706"),
    "QA In Progress":   ("#fef3c7", "#d97706"),
    "Analysis In Progress": ("#fef3c7", "#d97706"),
    "Analysis Complete":("#dbeafe", "#1d4ed8"),
    "New":              ("#f1f5f9", "#64748b"),
    "Removed":          ("#f1f5f9", "#94a3b8"),
    # JIRA Story workflow states (ladder-coloured: grey pre-approval → amber dev
    # → blue QA/ST → green live).
    "Backlog":                  ("#f1f5f9", "#64748b"),
    "Ready For Refinement":     ("#f1f5f9", "#64748b"),
    "In Refinement":            ("#f1f5f9", "#64748b"),
    "BA Analysis":              ("#f1f5f9", "#64748b"),
    "PO Approved":              ("#e2e8f0", "#475569"),
    "Ready for Tech Analysis":  ("#ede9fe", "#7c3aed"),
    "Tech Analysis In Progress":("#ede9fe", "#7c3aed"),
    "Technical Review":         ("#f1f5f9", "#64748b"),
    "Ready for Dev":            ("#fef3c7", "#d97706"),
    "Selected for Development": ("#fef3c7", "#d97706"),
    "In Progress":              ("#fef3c7", "#d97706"),
    "PR":                       ("#fde68a", "#b45309"),
    "Ready for QA":             ("#dbeafe", "#1d4ed8"),
    "QA":                       ("#dbeafe", "#1d4ed8"),
    "QA Passed":                ("#cffafe", "#0e7490"),
    "ST In Progress":           ("#ede9fe", "#7c3aed"),
    "Ready for Live":           ("#d1fae5", "#059669"),
    "Live in Progress":         ("#d1fae5", "#059669"),
    "Integrated":               ("#dcfce7", "#16a34a"),
    "Monitoring":               ("#dcfce7", "#16a34a"),
    "Reopened":                 ("#fee2e2", "#dc2626"),
    "On Hold":                  ("#fef9c3", "#854d0e"),
}

def state_badge(state):
    bg, fg = STATE_COLORS.get(state, ("#f1f5f9", "#64748b"))
    return (f'<span style="background:{bg};color:{fg};padding:1px 5px;border-radius:6px;'
            f'font-size:10px;font-weight:600">{state}</span>')

def progress_badge(label, bg, fg):
    return (f'<span style="background:{bg};color:{fg};padding:2px 8px;border-radius:10px;'
            f'font-size:11px;font-weight:600;white-space:nowrap">{label}</span>')

def pbi_progress_badge(state, done_states, inprogress_states):
    if state in done_states:
        return progress_badge("Done", "#dcfce7", "#16a34a")
    elif state in inprogress_states:
        return progress_badge(state, "#fef3c7", "#d97706")
    else:
        return progress_badge(state, "#f1f5f9", "#64748b")

def donut_svg(done, total, color="#6366f1"):
    r = 22; cx = cy = 28
    circ = 2 * math.pi * r
    if total == 0:
        dash, gap = 0.0, round(circ, 1)
    else:
        dash = round(done / total * circ, 1)
        gap  = round(circ - dash, 1)
    return f"""<svg width="56" height="56" viewBox="0 0 56 56">
  <circle cx="{cx}" cy="{cy}" r="{r}" fill="none" stroke="#e2e8f0" stroke-width="6"/>
  <circle cx="{cx}" cy="{cy}" r="{r}" fill="none" stroke="{color}" stroke-width="6"
    stroke-dasharray="{dash} {gap}" stroke-dashoffset="34.6" stroke-linecap="round"/>
  <text x="{cx}" y="{cy}" text-anchor="middle" dominant-baseline="middle"
    style="font-size:9px;font-weight:700;fill:#1e293b">{done}/{total}</text>
</svg>"""

# ── Load data (JIRA live) ─────────────────────────────────────────────────────
def _normalise_raw(df_raw):
    """Apply the column renames + dtype coercions both data sources need."""
    renames = {}
    for c in df_raw.columns:
        lc = c.lower().strip()
        if lc in ("id", "work item id"):             renames[c] = "ID"
        elif lc == "title":                          renames[c] = "Title"
        elif lc == "work item type":                 renames[c] = "Work Item Type"
        elif lc == "state":                          renames[c] = "State"
        elif lc in ("assigned to", "assignedto"):   renames[c] = "Assigned To"
        elif lc in ("iteration path", "iteration"): renames[c] = "Iteration Path"
        elif lc in ("tags",):                        renames[c] = "Tags"
        elif any(x in lc for x in ("original estimate", "estimated effort",
                                    "estimated hours", "original estimates")):
            renames[c] = "Original Estimate"
        elif any(x in lc for x in ("completed work", "spent effort",
                                    "spent hours", "completed")):
            renames[c] = "Completed Work"
    df_raw.rename(columns=renames, inplace=True)

    for col in ["ID", "Title", "Work Item Type", "State", "Assigned To",
                "Iteration Path", "Tags", "Original Estimate", "Completed Work"]:
        if col not in df_raw.columns:
            df_raw[col] = ""

    # TFS IDs are integers; JIRA issue keys are strings ("MPM-105"). Coercing
    # the JIRA keys to numeric would turn them all into NaN, so only do the
    # numeric coercion for non-JIRA sources.
    if getattr(CFG, "DATA_SOURCE", "excel").lower() == "jira":
        df_raw["ID"] = df_raw["ID"].fillna("").astype(str)
    else:
        df_raw["ID"] = pd.to_numeric(df_raw["ID"], errors="coerce")
    df_raw = df_raw[~df_raw["ID"].isin(EXCLUDED_IDS)]
    df_raw["Original Estimate"]  = pd.to_numeric(df_raw["Original Estimate"], errors="coerce").fillna(0)
    df_raw["Completed Work"]     = pd.to_numeric(df_raw["Completed Work"], errors="coerce").fillna(0)
    df_raw["Tags"]               = df_raw["Tags"].fillna("").astype(str)
    df_raw["State"]              = df_raw["State"].fillna("").astype(str)
    df_raw["Assigned To"]        = df_raw["Assigned To"].fillna("Unassigned").astype(str)
    df_raw["Iteration Path"]     = df_raw["Iteration Path"].fillna("").astype(str)
    return df_raw


def _load_capacity():
    """Per-member capacity (holiday-adjusted) from the maintained capacity
    workbook — the source of truth for capacity.

    Capacity model:
        Sprint cap = Capacity/day × (Working days − Team days off − Days off)
    so the "Sprint cap" column is the holiday-adjusted availability we compare
    JIRA allocation against. The workbook is resolved from CFG.CAPACITY_XLSX,
    which may be a local .xlsx path or a Google Sheet / .xlsx URL (read via a
    service account when CFG.CAPACITY_SA_KEY is set — see capacity_excel.py).
    """
    import capacity_excel
    src = getattr(CFG, "CAPACITY_XLSX", "Team_Capacity.xlsx")
    sa_key = getattr(CFG, "CAPACITY_SA_KEY", None)
    if isinstance(src, str) and src.lower().startswith(("http://", "https://")):
        source = src                       # URL — pass through unchanged
        label = src
    else:
        source = Path(src) if Path(src).is_absolute() else (script_dir / src)
        label = source.name
    print(f"📒 Reading capacity from {label} ...")
    df_cap = capacity_excel.load_dataframe(source, sa_key=sa_key)
    n_members = df_cap["Member"].nunique() if not df_cap.empty else 0
    total = df_cap["Sprint cap"].sum() if not df_cap.empty else 0
    print(f"📒 Capacity rows: {len(df_cap)} ({n_members} members, {total:.0f}h total)")
    return df_cap


def _load_from_jira():
    """Live pull from JIRA Cloud — the single source for work items.

    Work items (PBIs/Stories/Tasks/Sub-tasks/Bugs) come exclusively from JIRA
    via jira_fetch, producing the canonical DataFrame columns the generator
    consumes. Capacity is loaded separately from Team_Capacity.xlsx (see
    _load_capacity) — no data of any kind is pulled from TFS.
    """
    import jira_fetch

    ctx = jira_get_context()
    health = jira_test_auth(ctx)
    if not health.get("ok"):
        raise RuntimeError(
            f"JIRA auth failed: {health.get('error')}\n"
            f"Hint: {health.get('hint', '(none)')}"
        )
    print(f"🔐 Authenticated to JIRA as {health.get('account') or 'PAT user'}")

    release = getattr(CFG, "RELEASE_NAME", None)
    sprint  = _sprint_names()   # list of per-team sprint names (OR-ed in the JQL)
    # The daily board IS the sprint, so scope work items by Sprint only. The
    # Fix Version (RELEASE_NAME) is NOT AND-ed into the board — doing so returns
    # only the intersection, which is usually near-empty because the sprint and
    # the release rarely line up 1:1. The release is still used separately for the
    # Sprint<->Release discrepancy report (see the mismatch check below). Set
    # BOARD_RELEASE_FILTER = True in the config to go back to AND-ing both.
    board_release = release if getattr(CFG, "BOARD_RELEASE_FILTER", False) else None
    print(f"📡 Fetching JIRA work items (sprint={sprint!r}"
          f"{', release='+repr(release) if board_release else ''}) ...")
    df_raw_orig = jira_fetch.load_dataframe(
        release=board_release, sprint=sprint,
        sprint_number=CFG.SPRINT_NUMBER, ctx=ctx,
    )
    print(f"📡 Fetched {len(df_raw_orig)} work items from JIRA.")
    if df_raw_orig.empty:
        print("   ⚠ 0 work items — check that the Sprint name matches JIRA exactly "
              f"(JIRA_SPRINT_NAME={sprint!r}). Sprint names are case/space-sensitive.")

    df_cap = _load_capacity()
    return df_raw_orig, df_cap


def load_data():
    """Jira-only loader. Work items from JIRA; capacity from Team_Capacity.xlsx."""
    source = getattr(CFG, "DATA_SOURCE", "jira").lower()
    if source != "jira":
        raise ValueError(
            f"This is the Jira-only dashboard — DATA_SOURCE must be 'jira', "
            f"got {CFG.DATA_SOURCE!r}. (TFS/Excel work-item paths were removed; "
            f"the legacy generator lives under scripts/dashboard/.)"
        )
    df_raw_orig, df_cap = _load_from_jira()

    df_raw = _normalise_raw(df_raw_orig.copy())

    # Bugs flow into BOTH df_pbis (so they count toward sprint velocity in
    # the PBI Done card / Goal Buckets / PBI tab) and df_tasks (so the hours
    # they hold directly via Custom.EstimatedEfforts / Custom.SpentEfforts
    # show up in compute_capacity() and the Overview "Hours Spent" total).
    # classify_pbis() reads each Bug row's own est/spent rather than walking
    # children, so the same Bug isn't double-counted across the two paths.
    df_pbis  = df_raw[df_raw["Work Item Type"].isin(["Product Backlog Item", "Bug"])].copy()
    df_tasks = df_raw[df_raw["Work Item Type"].isin(["Task", "Bug"])].copy()

    n_tasks = int((df_tasks["Work Item Type"] == "Task").sum())
    n_bugs  = int((df_tasks["Work Item Type"] == "Bug").sum())
    print(f"📊 PBIs: {len(df_pbis)}, Tasks: {n_tasks}, Bugs: {n_bugs}")
    return df_pbis, df_tasks, df_cap, df_raw_orig

# ── Extract goal tag from PBI tags ─────────────────────────────────────────────
def extract_goal(tags_str):
    """Extract sprint goal using full normalisation.

    Current sprint (Sprint N) → canonical goal name, e.g. 'DevComplete'.
    Other sprints             → 'SprintM-GoalName', e.g. 'Sprint83-DevComplete',
                                so it is visible but clearly marked as out-of-sprint.

    All of these resolve to 'DevComplete' for the current sprint:
      Sprint82Goal-DevComplete · Sprint 82 Goal Dev Complete · Sprint_82_Goal_Dev_Complete
    """
    GOAL_ALIASES = {
        'devcomplete':            'DevComplete',
        'qacomplete':             'QAComplete',
        'devqacomplete':          'DevQAComplete',
        'live':                   'Live',
        'analysiscomplete':       'AnalysisComplete',
        'analysisanddevcomplete': 'AnalysisAndDevComplete',
    }
    current_prefix = f'sprint{CFG.SPRINT_NUMBER}goal'
    other_sprint_goal = None   # recorded as fallback; current sprint takes priority

    for tag in re.split(r'\s*[;,]\s*', str(tags_str)):
        norm = _normalise_tag(tag)
        # Current sprint — highest priority, return immediately
        if norm.startswith(current_prefix):
            goal_key = norm[len(current_prefix):]
            return GOAL_ALIASES.get(goal_key, goal_key)
        # Other sprint — keep first match as fallback
        if other_sprint_goal is None:
            m = re.match(r'sprint(\d+)goal(.+)', norm)
            if m:
                sprint_num = m.group(1)
                goal_key   = m.group(2)
                canonical  = GOAL_ALIASES.get(goal_key, goal_key)
                other_sprint_goal = f'Sprint{sprint_num}-{canonical}'

    return other_sprint_goal   # None if no sprint goal tag found at all

def extract_hotfix(tags_str):
    if CFG.HOTFIX_TAG_PATTERN.lower() in tags_str.lower():
        return CFG.HOTFIX_TAG_PATTERN
    return None

def _coerce_id(v):
    """Work-item IDs are integers in TFS but JIRA issue KEYS are strings
    (e.g. 'MPM-31'). Keep ints for TFS so legacy int-keyed overrides still
    match; pass JIRA keys through unchanged as strings."""
    try:
        return int(v)
    except (TypeError, ValueError):
        return str(v)


def _sprint_names():
    """Every per-team Jira sprint name that makes up this delivery sprint.
    Reads JIRA_SPRINT_NAMES (list); falls back to the single JIRA_SPRINT_NAME."""
    names = getattr(CFG, "JIRA_SPRINT_NAMES", None)
    if names:
        return [n for n in names if n]
    one = getattr(CFG, "JIRA_SPRINT_NAME", None) or CFG.SPRINT_NAME
    return [one] if one else []


def _active_sprint_label():
    """Display label for the combined sprint (used in titles/fallbacks)."""
    names = _sprint_names()
    return names[0] if names else CFG.SPRINT_NAME


def _in_sprint_tasks(df_tasks):
    """Return the in-sprint task/bug rows.

    The JIRA fetch already scopes its query to the team sprints AND pulls each
    parent's sub-tasks by parent key — so every returned row is in-sprint by
    construction. JIRA sub-tasks usually DON'T carry the Sprint field, so their
    'Iteration Path' comes back empty; filtering by sprint-name substring (the old
    TFS safety net) would drop those sub-task hours. We therefore keep rows whose
    Iteration Path matches ANY of the team sprint names OR is blank (sub-tasks)."""
    ip = df_tasks["Iteration Path"].fillna("").astype(str)
    mask = (ip.str.strip() == "")
    for name in _sprint_names():
        mask = mask | ip.str.contains(re.escape(name), na=False)
    return df_tasks[mask].copy()


def _dashboard_title():
    """Header/title text naming the source system + sprint."""
    names = _sprint_names()
    if len(names) > 1:
        # Show the common "… Sprint N" tail + a team count, e.g. "MPM Sprint 1 (3 teams)"
        tail = names[0].split(" Sprint ")[-1]
        spr = f"MPM Sprint {tail} ({len(names)} teams)" if tail else f"{len(names)} sprints"
    else:
        spr = names[0] if names else CFG.SPRINT_NAME
    return f"{spr} [JIRA] Dashboard — hBITS Calmanac"


# ── Classify PBIs ──────────────────────────────────────────────────────────────
def classify_pbis(df_pbis, df_tasks, df_raw_all):
    """Returns list of enriched PBI dicts.
    Uses row-order proximity to assign tasks to their parent PBI
    (TFS flat export: tasks always follow their parent PBI row).
    """
    # Build parent→tasks map from raw row order
    task_by_parent = {}
    current_pbi_id = None
    for _, row in df_raw_all.iterrows():
        wtype = str(row.get("Work Item Type", ""))
        rid   = row.get("ID")
        if not pd.notna(rid):
            continue
        rid = _coerce_id(rid)
        if wtype == "Product Backlog Item":
            current_pbi_id = rid
        elif wtype == "_OrphanBreak":
            # Sentinel emitted by tfs_fetch._df_from_tree_plus_flat() right
            # before "orphan" tasks (Sprint-N tasks whose parent PBI lives
            # in a different sprint). Resetting here prevents those tasks
            # from being misattributed to the last tree PBI.
            current_pbi_id = None
        elif wtype == "Task" and current_pbi_id is not None and rid not in EXCLUDED_IDS:
            task_by_parent.setdefault(current_pbi_id, []).append(row)

    pbis_out = []
    for _, row in df_pbis.iterrows():
        pbi_id   = _coerce_id(row["ID"])
        wtype    = str(row.get("Work Item Type", ""))
        state    = PBI_STATE_OVERRIDES.get(pbi_id, str(row["State"]))
        tags     = str(row["Tags"])
        goal     = GOAL_OVERRIDES.get(pbi_id, extract_goal(tags))
        hotfix   = extract_hotfix(tags)
        assignee = str(row["Assigned To"])
        title    = str(row["Title"])

        if wtype == "Bug":
            # Bugs are leaf items on this template — they hold their own
            # Custom.EstimatedEfforts / Custom.SpentEfforts and have no child
            # Tasks. Treat each Bug as a single-task PBI for velocity counting
            # and use the default "done" state list (Bugs typically aren't
            # tagged with a SprintNGoal, so the goal-keyed lookup would fall
            # through to [] otherwise and the Bug would never count as done).
            my_tasks    = []
            est_h       = float(row.get("Original Estimate", 0) or 0)
            spent_h     = float(row.get("Completed Work", 0) or 0)
            done_states = CFG.GOAL_DONE_STATES.get(goal, CFG.GOAL_DONE_STATES["_default"])
            task_done   = 1 if state in done_states else 0
            task_total  = 1
        else:
            my_tasks    = task_by_parent.get(pbi_id, [])
            task_done   = sum(1 for t in my_tasks if str(t["State"]) == "Done")
            task_total  = len(my_tasks)
            est_h       = sum(float(t.get("Original Estimate", 0) or 0) for t in my_tasks)
            spent_h     = sum(float(t.get("Completed Work", 0) or 0) for t in my_tasks)
            done_states = CFG.GOAL_DONE_STATES.get(goal, CFG.GOAL_DONE_STATES["_default"]) if goal else []

        pbis_out.append({
            "id":          pbi_id,
            "type":        wtype,            # "Product Backlog Item" or "Bug"
            "title":       title,
            "state":       state,
            "assignee":    assignee,
            "tags":        tags,
            "goal":        goal,
            "hotfix":      hotfix,
            "task_done":   task_done,
            "task_total":  task_total,
            "est_h":       est_h,
            "spent_h":     spent_h,
            "done_states": done_states,
            "tasks":       my_tasks,
        })
    return pbis_out

# ── Compute capacity per team / member ────────────────────────────────────────
def compute_capacity(df_tasks, df_cap):
    s79 = _in_sprint_tasks(df_tasks)

    # Per-person Sprint 79 metrics
    pstats = {}
    for member_col in ["Assigned To"]:
        grp = s79.groupby(member_col)
        for name, g in grp:
            done  = int((g["State"] == "Done").sum())
            est   = float(g["Original Estimate"].sum())
            spent = float(g["Completed Work"].sum())
            pstats[str(name)] = {"done": done, "estimated": est, "spent": spent}

    # Base capacity from capacity sheet — try to find by name
    cap_lookup = {}
    name_col = None
    cap_col  = None
    for c in df_cap.columns:
        lc = c.lower()
        if "name" in lc or "member" in lc or "resource" in lc:
            name_col = c
        if "capacity" in lc or "hours" in lc or "base" in lc or "avail" in lc or "cap" in lc:
            cap_col = c

    if name_col and cap_col:
        # Multiple activity rows per member — sum them up; values may be strings like "50h"
        def _parse_hours(v):
            if pd.isna(v):
                return 0.0
            s = str(v).strip().lower().rstrip("h").strip()
            try:
                return float(s)
            except ValueError:
                return 0.0
        raw_totals = {}
        for _, r in df_cap.iterrows():
            n = str(r[name_col]).strip()
            if not n or n.lower() in ("nan", ""):
                continue
            raw_totals[n] = raw_totals.get(n, 0.0) + _parse_hours(r[cap_col])
        cap_lookup = raw_totals

    # Normalise via CAPACITY_NAME_MAP
    for team_name, excel_name in CFG.CAPACITY_NAME_MAP.items():
        if excel_name in cap_lookup and team_name not in cap_lookup:
            cap_lookup[team_name] = cap_lookup[excel_name]

    # Apply capacity adjustments (leave / sick days)
    adj = getattr(CFG, "CAPACITY_ADJUSTMENTS", {})
    for name, hrs_lost in adj.items():
        if name in cap_lookup:
            cap_lookup[name] = max(0.0, cap_lookup[name] - hrs_lost)

    team_data = {}
    for team, members in CFG.TEAMS.items():
        rows = []
        for m in members:
            stats = pstats.get(m, {"done": 0, "estimated": 0.0, "spent": 0.0})
            base  = cap_lookup.get(m, cap_lookup.get(CFG.CAPACITY_NAME_MAP.get(m, "__"), 0.0))
            task_total = int(s79[s79["Assigned To"] == m].shape[0])
            rows.append({
                "name":        m,
                "capacity":    base,
                "estimated":   stats["estimated"],          # sub-task effort estimate (allocation)
                "spent":       stats["spent"],              # logged / time spent
                "remaining":   max(0.0, stats["estimated"] - stats["spent"]),
                "done":        stats["done"],
                "tasks_total": task_total,
            })
        team_data[team] = rows
    return team_data

# ── Dev / QA Completion Date Tracker ──────────────────────────────────────────

_MONTH_MAP = {
    "jan":1,"feb":2,"mar":3,"apr":4,"may":5,"jun":6,
    "jul":7,"aug":8,"sep":9,"oct":10,"nov":11,"dec":12,
    "january":1,"february":2,"march":3,"april":4,"june":6,
    "july":7,"august":8,"september":9,"october":10,"november":11,"december":12,
}

def _normalise_tag(s):
    """Remove leading/trailing whitespace; strip internal spaces, hyphens, underscores; lowercase.

    Examples:
      'Sprint 82 Goal Dev Complete' → 'sprint82goaldevcomplete'
      'QA ready-06/04/2026'        → 'qaready06/04/2026'
      'Dev completion - 04/16/2026'→ 'devcompletion04/16/2026'
      'DevCompletion17thApril'     → 'devcompletion17thapril'
    """
    return re.sub(r'[\s\-_]+', '', str(s).strip()).lower()


def _extract_date_from_norm(norm):
    """Extract a date from a normalised tag string (spaces/hyphens/underscores removed).

    Handles:
      MM/DD/YYYY  e.g. '06/04/2026'
      MM/DD       e.g. '06/04'  (assumes 2026)
      DDMonthName e.g. '17thapril', '22apr', '8april'
    Returns datetime.date or None.
    """
    from datetime import datetime as _dt
    # MM/DD/YYYY
    m = re.search(r'(\d{1,2})/(\d{1,2})/(\d{2,4})', norm)
    if m:
        mm, dd, yy = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if yy < 100:
            yy += 2000
        try:
            return _dt(yy, mm, dd).date()
        except Exception:
            pass
    # MM/DD  (no year)
    m = re.search(r'(\d{1,2})/(\d{1,2})', norm)
    if m:
        mm, dd = int(m.group(1)), int(m.group(2))
        try:
            return _dt(2026, mm, dd).date()
        except Exception:
            pass
    # DDMonthName  (e.g. 22apr, 17thapril, 8april)
    # The (?:st|nd|rd|th)? consumes ordinal suffixes so '17thapril' → dd=17, mon_str='april'
    m = re.search(r'(\d{1,2})(?:st|nd|rd|th)?([a-z]{3,})', norm)
    if m:
        dd      = int(m.group(1))
        mon_str = m.group(2)
        mon     = _MONTH_MAP.get(mon_str[:3]) or _MONTH_MAP.get(mon_str)
        if mon:
            try:
                return _dt(2026, mon, dd).date()
            except Exception:
                pass
    return None


def _parse_date_near_keyword(tags_str, patterns):
    """Generic: try each regex pattern in order against tags_str.
    Returns (datetime.date, matched_keyword_label) or (None, None)."""
    from datetime import datetime as _dt
    if not tags_str:
        return None
    t = str(tags_str)
    for pat_list in patterns:
        for rx in pat_list["rx"]:
            m = re.search(rx, t, re.I)
            if not m:
                continue
            gs = m.groups()
            try:
                if len(gs) == 3 and gs[2]:              # MM/DD/YYYY
                    mm, dd, yy = int(gs[0]), int(gs[1]), int(gs[2])
                    if yy < 100: yy += 2000
                    return _dt(yy, mm, dd).date()
                elif len(gs) == 2 and gs[1].isdigit():   # MM/DD
                    return _dt(2026, int(gs[0]), int(gs[1])).date()
                elif len(gs) == 2:                        # DD + MonthName
                    dd  = int(gs[0])
                    mon = _MONTH_MAP.get(gs[1].lower())
                    if mon:
                        return _dt(2026, mon, dd).date()
            except Exception:
                continue
    return None


def parse_qa_date(tags_str):
    """Extract ReadyForQA / Ready-for-QA target date from tags.

    Normalisation makes all these equivalent:
      ReadyForQA-22Apr · Ready for QA - 22 Apr · Ready_for_QA 22Apr · ReadyForQA04/22
    Note: 'QA ready / QAReady' (QA first) is intentionally NOT matched here —
    it is treated as a dev-completion date in parse_dev_date instead.
    """
    for tag in re.split(r'\s*[;,]\s*', str(tags_str)):
        norm = _normalise_tag(tag)
        # 'qacomplete' = the JIRA "QA Complete Date" field (injected by jira_fetch);
        # 'readyforqa'/'readyqa' = legacy tag form.
        if norm.startswith('qacomplete') or norm.startswith('readyforqa') or norm.startswith('readyqa'):
            d = _extract_date_from_norm(norm)
            if d:
                return d
    return None


def parse_dev_date(tags_str):
    """Extract dev-completion date from tags.

    'QA ready / QAReady' (QA-first format) is also treated as dev completion —
    semantically it means dev is done and the item has been handed to QA.

    Normalisation makes all these equivalent:
      Dev completion - 04/16/2026 · DevCompletion04/16/2026 · devcompletion04/16/2026
      Dev Complete 20th April     · DevComplete20thApril     · dev complete 20 april
      QA ready-06/04/2026         · QAReady06/04/2026        · qa_ready 06/04/2026
    """
    for tag in re.split(r'\s*[;,]\s*', str(tags_str)):
        norm = _normalise_tag(tag)
        if (norm.startswith('devcompl') or norm.startswith('devdone')
                or norm.startswith('qaready')):
            d = _extract_date_from_norm(norm)
            if d:
                return d
    return None


def parse_tech_date(tags_str):
    """Extract the Tech Analysis Complete Date (injected by jira_fetch as
    'TechAnalysisComplete-MM/DD/YYYY')."""
    for tag in re.split(r'\s*[;,]\s*', str(tags_str)):
        norm = _normalise_tag(tag)
        if norm.startswith('techanalysiscomplete'):
            d = _extract_date_from_norm(norm)
            if d:
                return d
    return None


def parse_target_date(tags_str):
    """Priority: ReadyForQA date first, then Dev Completion date.
    Returns (date, label) or (None, None)."""
    d = parse_qa_date(tags_str)
    if d:
        return d, "QA Ready"
    d = parse_dev_date(tags_str)
    if d:
        return d, "Dev Done"
    return None, None


def build_dev_tracker(pbis):
    """Build Dev/QA Completion Tracker — priority: ReadyForQA date, then Dev
    Completion date.  Shows team column and flags PBIs with no date tag."""
    from datetime import date as _date
    today = _date.today()

    # QA release tracking: "Done" = item is available for QA or beyond
    # Dev Completed does NOT count — item hasn't reached QA yet
    # Use lowercase set for case-insensitive matching
    QA_DONE_STATES_LC = {s.lower() for s in [
        "QA To Do", "QA in progress", "QA In Progress", "QA Complete",
        "ST To Do", "Ready For LIVE", "LIVE", "Done",
    ]}

    member_to_team = {m: t for t, ms in CFG.TEAMS.items() for m in ms}
    TEAM_COLORS_T = {"Calmers": "#6366f1", "Knackers": "#0891b2", "Crackers": "#16a34a"}

    # Only track true PBIs: exclude Bugs and items without [PRODUCT] or [TECHNICAL] title prefix
    pbis = [p for p in pbis
            if p.get("type") != "Bug"
            and re.match(r'^\[(product|technical)\]', str(p.get("title", "")).strip(), re.I)]

    # A completion date is expected once the story reaches that stage. Each is
    # checked INDEPENDENTLY, so an item can be flagged for more than one:
    #  - Tech Analysis Complete Date expected at "Ready for Tech Analysis" or later
    #  - Dev Complete Date            expected at "Ready for Dev" or later
    #  - QA Complete Date             expected at "Ready for QA" or later
    # (built from the goal ladder done-sets = "that stage or beyond").
    TECHA_OR_AFTER = set(CFG.GOAL_DONE_STATES.get("readyfortechanalysis", []))
    DEV_OR_AFTER   = set(CFG.GOAL_DONE_STATES.get("readyfordev", []))
    QA_OR_AFTER    = set(CFG.GOAL_DONE_STATES.get("readyforqa", []))

    rows_data = []
    no_date_rows = []

    def _track(p, target, label, state, assignee, team, qa_done):
        if qa_done or target is None:
            status, color, icon = "Done", "#16a34a", "✅"
        elif today <= target:
            status, color, icon = "On Track", "#0891b2", "🟢"
        else:
            status, color, icon = "Behind", "#dc2626", "🔴"
        rows_data.append({
            "id": p["id"], "title": p["title"], "assignee": assignee,
            "team": team, "target": target, "state": state, "status": status,
            "color": color, "icon": icon,
            "days_left": (target - today).days if target else 999,
            "goal": p.get("goal", ""), "label": label,
        })

    def _flag(p, needs, state, assignee, team):
        no_date_rows.append({
            "id": p["id"], "title": p["title"], "assignee": assignee,
            "team": team, "state": state, "goal": p.get("goal") or "", "needs": needs,
        })

    for p in pbis:
        state    = p["state"]
        assignee = p.get("assignee", "")
        team     = member_to_team.get(assignee, "—")
        qa_done  = state.lower() in QA_DONE_STATES_LC
        tech_date = parse_tech_date(p.get("tags"))  # JIRA "Tech Analysis Complete Date"
        dev_date  = parse_dev_date(p.get("tags"))   # JIRA "Dev Complete Date"
        qa_date   = parse_qa_date(p.get("tags"))    # JIRA "QA Complete Date"

        # Dev/QA estimates = sum of the story's [Dev]/[QA] sub-task Original
        # Estimates. By Ready for Dev the dev work should be estimated; by Ready
        # for QA the QA work should be estimated.
        _tasks = p.get("tasks", []) or []
        def _sum_est(prefix):
            return sum(float(t.get("Original Estimate", 0) or 0) for t in _tasks
                       if str(t.get("Title", "")).strip().lower().startswith(prefix))
        dev_est = _sum_est("[dev]")
        qa_est  = _sum_est("[qa]")

        # Independent, stage-gated checks — an item can be missing more than one.
        missing = []
        if state in TECHA_OR_AFTER and not tech_date:
            missing.append("Tech Analysis complete date")
        if state in DEV_OR_AFTER and dev_est <= 0:
            missing.append("Dev estimate")
        if state in DEV_OR_AFTER and not dev_date:
            missing.append("Dev complete date")
        if state in QA_OR_AFTER and qa_est <= 0:
            missing.append("QA estimate")
        if state in QA_OR_AFTER and not qa_date:
            missing.append("QA complete date")

        if missing:
            _flag(p, ", ".join(missing), state, assignee, team)
            continue

        # All expected dates present → track against the most advanced one.
        if qa_date:
            _track(p, qa_date, "QA", state, assignee, team, qa_done)
        elif dev_date:
            _track(p, dev_date, "Dev", state, assignee, team, qa_done)
        elif qa_done:
            _track(p, None, "—", state, assignee, team, True)
        # else: earlier than tech analysis with no dates — nothing to show.

    if not rows_data and not no_date_rows:
        return ""

    order = {"Behind": 0, "On Track": 1, "Done": 2}
    rows_data.sort(key=lambda r: (order.get(r["status"], 9), r["days_left"]))

    done_c   = sum(1 for r in rows_data if r["status"] == "Done")
    track_c  = sum(1 for r in rows_data if r["status"] == "On Track")
    behind_c = sum(1 for r in rows_data if r["status"] == "Behind")
    total_c  = len(rows_data)
    nodate_c = len(no_date_rows)

    summary = (
        f'<div style="display:flex;gap:16px;margin-bottom:12px;flex-wrap:wrap">'
        f'<div style="background:#f0fdf4;border:1px solid #86efac;border-radius:8px;padding:8px 14px;text-align:center">'
        f'  <div style="font-size:20px;font-weight:800;color:#16a34a">{done_c}</div>'
        f'  <div style="font-size:10px;color:#64748b">Done</div></div>'
        f'<div style="background:#ecfeff;border:1px solid #67e8f9;border-radius:8px;padding:8px 14px;text-align:center">'
        f'  <div style="font-size:20px;font-weight:800;color:#0891b2">{track_c}</div>'
        f'  <div style="font-size:10px;color:#64748b">On Track</div></div>'
        f'<div style="background:#fef2f2;border:1px solid #fca5a5;border-radius:8px;padding:8px 14px;text-align:center">'
        f'  <div style="font-size:20px;font-weight:800;color:#dc2626">{behind_c}</div>'
        f'  <div style="font-size:10px;color:#64748b">Behind</div></div>'
        f'<div style="background:#fef9c3;border:1px solid #fcd34d;border-radius:8px;padding:8px 14px;text-align:center">'
        f'  <div style="font-size:20px;font-weight:800;color:#854d0e">{nodate_c}</div>'
        f'  <div style="font-size:10px;color:#64748b">No Date</div></div>'
        f'<div style="background:#fff;border:1px solid #e2e8f0;border-radius:8px;padding:8px 14px;text-align:center">'
        f'  <div style="font-size:20px;font-weight:800;color:#6366f1">{total_c}</div>'
        f'  <div style="font-size:10px;color:#64748b">Tracked</div></div>'
        f'</div>'
    )

    # ── Main tracked table ────────────────────────────────────────────────────
    trows = ""
    for r in rows_data:
        target_str = r["target"].strftime("%b %d") if r["target"] else "—"
        days_str = "—" if not r["target"] else (f'{r["days_left"]}d' if r["days_left"] >= 0 else f'{abs(r["days_left"])}d overdue')
        tc = TEAM_COLORS_T.get(r["team"], "#64748b")
        label_bg = "#e0f2fe" if r["label"] == "QA Ready" else "#ede9fe"
        label_fg = "#0369a1" if r["label"] == "QA Ready" else "#6d28d9"
        trows += (
            f'<tr style="border-bottom:1px solid #f1f5f9">'
            f'<td style="padding:6px 8px;font-size:11px">{_issue_link(r["id"])}</td>'
            f'<td style="padding:6px 8px;font-size:12px;max-width:250px;white-space:nowrap;'
            f'overflow:hidden;text-overflow:ellipsis">{r["title"]}</td>'
            f'<td style="padding:6px 8px;font-size:11px;color:#64748b">{r["assignee"]}</td>'
            f'<td style="padding:6px 8px;font-size:11px;font-weight:600;color:{tc}">{r["team"]}</td>'
            f'<td style="padding:6px 8px"><span style="background:{label_bg};color:{label_fg};'
            f'padding:1px 6px;border-radius:6px;font-size:9px;font-weight:700">{r["label"]}</span></td>'
            f'<td style="padding:6px 8px">{state_badge(r["state"])}</td>'
            f'<td style="padding:6px 8px;font-size:11px;font-weight:600;color:#6366f1">{target_str}</td>'
            f'<td style="padding:6px 8px;font-size:11px;color:{r["color"]};font-weight:600">{days_str}</td>'
            f'<td style="padding:6px 8px">'
            f'  <span style="background:{r["color"]}18;color:{r["color"]};padding:2px 8px;'
            f'border-radius:10px;font-size:10px;font-weight:700">{r["icon"]} {r["status"]}</span></td>'
            f'</tr>'
        )

    th_style = 'padding:6px 8px;text-align:left;color:#64748b;font-size:10px;border-bottom:1px solid #e2e8f0'
    table = (
        f'<div style="overflow-x:auto"><table style="width:100%;border-collapse:collapse;font-size:12px">'
        f'<thead><tr style="background:#f8fafc">'
        f'<th style="{th_style}">ID</th>'
        f'<th style="{th_style}">PBI</th>'
        f'<th style="{th_style}">Assignee</th>'
        f'<th style="{th_style}">Team</th>'
        f'<th style="{th_style}">Type</th>'
        f'<th style="{th_style}">State</th>'
        f'<th style="{th_style}">Target</th>'
        f'<th style="{th_style}">Days</th>'
        f'<th style="{th_style}">Status</th>'
        f'</tr></thead><tbody>{trows}</tbody></table></div>'
    )

    # ── No-date section ───────────────────────────────────────────────────────
    nodate_html = ""
    if no_date_rows:
        nd_trows = ""
        for r in no_date_rows:
            bg, fg = STATE_COLORS.get(r["state"], ("#f1f5f9", "#64748b"))
            tc = TEAM_COLORS_T.get(r["team"], "#64748b")
            nd_trows += (
                f'<tr style="border-bottom:1px solid #f1f5f9">'
                f'<td style="padding:6px 8px;font-size:11px">{_issue_link(r["id"])}</td>'
                f'<td style="padding:6px 8px;font-size:12px;max-width:250px;white-space:nowrap;'
                f'overflow:hidden;text-overflow:ellipsis">{r["title"]}</td>'
                f'<td style="padding:6px 8px;font-size:11px;color:#64748b">{r["assignee"]}</td>'
                f'<td style="padding:6px 8px;font-size:11px;font-weight:600;color:{tc}">{r["team"]}</td>'
                f'<td style="padding:6px 8px"><span style="background:{bg};color:{fg};padding:1px 6px;'
                f'border-radius:8px;font-size:10px;font-weight:600">{r["state"]}</span></td>'
                f'<td style="padding:6px 8px;font-size:11px;color:#6366f1">{r["goal"]}</td>'
                f'<td style="padding:6px 8px"><span style="background:#fee2e2;color:#b91c1c;padding:1px 6px;'
                f'border-radius:8px;font-size:10px;font-weight:700">missing {r.get("needs","date")}</span></td>'
                f'</tr>'
            )
        nd_th = 'padding:6px 8px;text-align:left;color:#854d0e;font-size:10px;border-bottom:1px solid #fcd34d'
        nodate_html = (
            f'<div style="margin-top:14px;border-top:1px solid #e2e8f0;padding-top:12px">'
            f'<div style="font-size:12px;font-weight:700;color:#854d0e;margin-bottom:8px">'
            f'⚠ Missing Dates &amp; Estimates ({nodate_c}: Tech Analysis date at Ready-for-Tech-Analysis+; Dev estimate &amp; date at Ready-for-Dev+; QA estimate &amp; date at QA+)</div>'
            f'<div style="overflow-x:auto"><table style="width:100%;border-collapse:collapse;font-size:12px">'
            f'<thead><tr style="background:#fef9c3">'
            f'<th style="{nd_th}">ID</th><th style="{nd_th}">PBI</th>'
            f'<th style="{nd_th}">Assignee</th><th style="{nd_th}">Team</th>'
            f'<th style="{nd_th}">State</th><th style="{nd_th}">Goal</th><th style="{nd_th}">Needs</th>'
            f'</tr></thead><tbody>{nd_trows}</tbody></table></div></div>'
        )

    return (
        f'<div style="background:#fff;border:1px solid #e2e8f0;border-radius:12px;padding:16px;'
        f'box-shadow:0 1px 3px rgba(0,0,0,.06);margin-bottom:20px">'
        f'<div style="font-size:13px;font-weight:700;color:#1e293b;margin-bottom:12px">'
        f'📦 QA Release Tracker ({behind_c} behind · {track_c} on track · {done_c} done'
        f'{f" · {nodate_c} no date" if nodate_c else ""})</div>'
        f'{summary}{table}{nodate_html}</div>'
    )


# ── HTML builders ──────────────────────────────────────────────────────────────

def build_overview_cards(pbis, s79_tasks):
    # Velocity items = PBIs proper + Bugs (added in classify_pbis with type="Bug").
    # Tag-related stats only consider PBIs proper since Bugs aren't expected
    # to carry SprintNGoal tags.
    pbis_only   = [p for p in pbis if p.get("type") != "Bug"
                   and re.match(r'^\[(product|technical)\]', str(p.get("title", "")).strip(), re.I)]
    bugs_only   = [p for p in pbis if p.get("type") == "Bug"]
    total_pbis  = len(pbis_only)
    total_bugs  = len(bugs_only)
    tagged      = sum(1 for p in pbis_only if p["goal"])
    untagged    = total_pbis - tagged

    total_tasks = len(s79_tasks)
    tasks_done  = int((s79_tasks["State"] == "Done").sum())
    tasks_ip    = int((s79_tasks["State"] == CFG.TASK_INPROGRESS_STATE).sum())
    est_h       = int(s79_tasks["Original Estimate"].sum())
    spent_h     = int(s79_tasks["Completed Work"].sum())
    pct_tasks   = round(tasks_done / total_tasks * 100) if total_tasks else 0
    pct_spent   = round(spent_h   / est_h       * 100) if est_h       else 0

    # Velocity tally — both PBIs and Bugs count. Each row already carries its
    # own done_states (set in classify_pbis: goal-keyed for PBIs, _default for
    # Bugs), so we trust that rather than re-deriving here.
    velocity_total = len(pbis)
    velocity_done  = sum(1 for p in pbis if p["state"] in p["done_states"])
    pct_velocity   = round(velocity_done / velocity_total * 100) if velocity_total else 0

    # Discrepancies = untagged PBIs + PBIs with multiple SprintNGoal tags.
    # Bugs intentionally excluded — they aren't expected to be tagged.
    multi_goal    = sum(1 for p in pbis_only
                        if len(re.findall(r"Sprint\d+Goal-\w+", p["tags"], re.I)) > 1)
    discrepancies = untagged + multi_goal

    cards = [
        (str(total_pbis), "#6366f1", f"Total {CFG.SPRINT_NAME} PBIs",
         f"{tagged} with goal tags, {untagged} untagged"),
        (str(total_bugs), "#dc2626", "Bugs in Sprint",
         "counted toward velocity"),
        (str(total_tasks), "#0891b2", "Tasks in Sprint", ""),
        (str(tasks_done), "#16a34a", "Tasks Done", f"{pct_tasks}% complete"),
        (str(tasks_ip),   "#d97706", "Tasks In Progress", ""),
        (f"{est_h}h",     "#6366f1", "Estimated Hours", ""),
        (f"{spent_h}h",   "#16a34a", "Hours Spent", f"{pct_spent}% utilised"),
        (str(discrepancies), "#dc2626", "Discrepancies",
         "tag conflicts / no goal", "#fff5f5", "#fca5a5"),
        (str(velocity_done), "#16a34a", "Velocity Done",
         f"{pct_velocity}% of {velocity_total} (PBIs + Bugs)"),
    ]
    # Backward-compat aliases for the metrics dict consumers below.
    pbis_done = velocity_done
    pct_pbis  = pct_velocity

    def card_html(val, color, label, sub="", bg="#ffffff", border="#e2e8f0"):
        sub_html = f'<div style="font-size:11px;color:#94a3b8;margin-top:2px">{sub}</div>' if sub else ""
        return (f'<div style="background:{bg};border:1px solid {border};border-radius:10px;'
                f'padding:16px 18px;box-shadow:0 1px 3px rgba(0,0,0,.06)">'
                f'<div style="font-size:26px;font-weight:800;color:{color}">{val}</div>'
                f'<div style="font-size:12px;color:#64748b;margin-top:3px;font-weight:500">{label}</div>'
                f'{sub_html}</div>')

    items = []
    for c in cards:
        items.append(card_html(*c))
    grid = ('<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));'
            'gap:12px;margin-bottom:20px">\n  ' + "\n  ".join(items) + "\n</div>")
    return grid, {
        "total_pbis": total_pbis, "total_bugs": total_bugs,
        "tagged": tagged, "untagged": untagged,
        "total_tasks": total_tasks, "tasks_done": tasks_done, "tasks_ip": tasks_ip,
        "est_h": est_h, "spent_h": spent_h,
        "pbis_done": pbis_done, "pct_pbis": pct_pbis,
        "velocity_total": velocity_total, "velocity_done": velocity_done,
    }

def build_sprint_progress(pbis, metrics):
    total     = metrics["total_pbis"]
    done_count = metrics["pbis_done"]
    pct        = metrics["pct_pbis"]

    # Only count true PBIs ([PRODUCT]/[TECHNICAL], not Bugs) for the breakdown
    true_pbis = [p for p in pbis
                 if p.get("type") != "Bug"
                 and re.match(r'^\[(product|technical)\]',
                              str(p.get("title", "")).strip(), re.I)]

    ip_count = 0
    ns_count = 0
    ng_count = 0
    for p in true_pbis:
        goal = p["goal"]
        s    = p["state"]
        if goal is None:
            ng_count += 1
            continue
        done_st = CFG.GOAL_DONE_STATES.get(goal, CFG.GOAL_DONE_STATES["_default"])
        if s in done_st:
            continue
        elif s in CFG.INPROGRESS_STATES:
            ip_count += 1
        else:
            ns_count += 1

    bar_w = min(pct, 100)
    html  = f"""<div style="background:#fff;border:1px solid #e2e8f0;border-radius:10px;padding:14px 16px;box-shadow:0 1px 3px rgba(0,0,0,.06)">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
        <span style="font-size:13px;font-weight:700;color:#1e293b">Sprint Progress (Day {CFG.SPRINT_DAY} of {CFG.SPRINT_TOTAL_DAYS})</span>
        <span style="font-weight:700;color:#6366f1">{pct}%</span>
      </div>
      <div style="background:#e2e8f0;border-radius:4px;height:10px;width:100%"><div style="background:#6366f1;border-radius:4px;height:10px;width:{bar_w}%"></div></div>
    </div>
    <div style="margin-top:12px;display:flex;gap:20px">
      <div style="text-align:center">
        <div style="font-size:22px;font-weight:800;color:#22c55e">{done_count}</div>
        <div style="font-size:10px;color:#64748b">PBIs Done</div>
      </div>
      <div style="text-align:center">
        <div style="font-size:22px;font-weight:800;color:#d97706">{ip_count}</div>
        <div style="font-size:10px;color:#64748b">In Progress</div>
      </div>
      <div style="text-align:center">
        <div style="font-size:22px;font-weight:800;color:#94a3b8">{ns_count}</div>
        <div style="font-size:10px;color:#64748b">Not Started</div>
      </div>
      <div style="text-align:center">
        <div style="font-size:22px;font-weight:800;color:#f59e0b">{ng_count}</div>
        <div style="font-size:10px;color:#64748b">No Goal Tag</div>
      </div>
    </div>"""
    return html, {"done": done_count, "ip": ip_count, "ns": ns_count, "ng": ng_count}

def build_task_row(task):
    state = str(task.get("State", ""))
    title = str(task.get("Title", ""))
    bg, fg = STATE_COLORS.get(state, ("#f1f5f9", "#64748b"))
    est    = task.get("Original Estimate", 0) or 0
    spent  = task.get("Completed Work", 0) or 0
    tid    = _coerce_id(task.get("ID", 0))
    return (f'<tr style="border-bottom:1px solid #f1f5f9">'
            f'<td style="padding:5px 8px;font-size:11px;color:#94a3b8">#{tid}</td>'
            f'<td style="padding:5px 8px;font-size:12px;max-width:300px;white-space:nowrap;'
            f'overflow:hidden;text-overflow:ellipsis">{title}</td>'
            f'<td style="padding:5px 8px;font-size:11px;color:#64748b">'
            f'{task.get("Assigned To","")}</td>'
            f'<td style="padding:5px 8px">'
            f'<span style="background:{bg};color:{fg};padding:1px 5px;border-radius:4px;'
            f'font-size:10px;font-weight:600">{state}</span></td>'
            f'<td style="padding:5px 8px;text-align:center;font-size:11px;color:#6366f1">{int(est)}h</td>'
            f'<td style="padding:5px 8px;text-align:center;font-size:11px;color:#16a34a">{int(spent)}h</td>'
            f'</tr>')

def build_pbi_card(pbi, idx, goal, scope="g"):
    pid    = pbi["id"]
    title  = pbi["title"]
    state  = pbi["state"]
    done_st = pbi["done_states"]

    hotfix_badge = ""
    if pbi["hotfix"]:
        hotfix_badge = (f'<span style="background:#fef2f2;color:#dc2626;padding:1px 6px;'
                        f'border-radius:8px;font-size:10px;font-weight:700">{pbi["hotfix"]}</span>')

    prg_badge = pbi_progress_badge(state, done_st, CFG.INPROGRESS_STATES)
    task_sum  = (f'{pbi["task_done"]}/{pbi["task_total"]} tasks&nbsp;|&nbsp;'
                 f'{int(pbi["est_h"])}h est&nbsp;|&nbsp;{int(pbi["spent_h"])}h spent')

    task_rows = "".join(build_task_row(t) for t in pbi["tasks"])
    task_table = ""
    if task_rows:
        task_table = f"""<div style="overflow-x:auto;margin-top:4px">
<table style="width:100%;border-collapse:collapse;font-size:12px">
<thead><tr style="background:#f8fafc">
  <th style="padding:5px 8px;text-align:left;color:#64748b;font-size:11px;border-bottom:1px solid #e2e8f0">ID</th>
  <th style="padding:5px 8px;text-align:left;color:#64748b;font-size:11px;border-bottom:1px solid #e2e8f0">Title</th>
  <th style="padding:5px 8px;text-align:left;color:#64748b;font-size:11px;border-bottom:1px solid #e2e8f0">Assignee</th>
  <th style="padding:5px 8px;text-align:left;color:#64748b;font-size:11px;border-bottom:1px solid #e2e8f0">State</th>
  <th style="padding:5px 8px;text-align:center;color:#64748b;font-size:11px;border-bottom:1px solid #e2e8f0">Est</th>
  <th style="padding:5px 8px;text-align:center;color:#64748b;font-size:11px;border-bottom:1px solid #e2e8f0">Spent</th>
</tr></thead>
<tbody>{task_rows}</tbody>
</table></div>"""

    uid = f"pb-{scope}-{pid}"
    return f"""<div onclick="var e=document.getElementById('{uid}'),i=document.getElementById('{uid}-i');if(e.style.display==='none'){{e.style.display='block';i.textContent='−'}}else{{e.style.display='none';i.textContent='+'}}" style="display:flex;align-items:center;gap:8px;padding: 10px 14px; cursor: pointer; background: rgb(255, 255, 255); user-select: none;" onmouseover="this.style.background='#f8fafc'" onmouseout="this.style.background='#fff'"><span id="{uid}-i" style="color:#94a3b8;font-size:12px;flex-shrink:0;width:12px;text-align:center">+</span><div style="flex:1;min-width:0"><div style="display:flex;align-items:center;gap:5px;flex-wrap:wrap;margin-bottom:2px"><span style="font-size:11px;color:#94a3b8">#{pid}</span>{hotfix_badge}{state_badge(state)} </div><div style="font-size:13px;font-weight:500;color:#1e293b;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">{title}</div><div style="font-size:11px;color:#64748b;margin-top:1px">{pbi["assignee"]}</div></div><div style="display:flex;flex-direction:column;align-items:flex-end;gap:4px;flex-shrink:0">{prg_badge}<span style="font-size:10px;color:#94a3b8">{task_sum}</span></div></div><div id="{uid}" style="display:none;padding:12px 14px;border-top:1px solid #f1f5f9;background:#fafafa">{task_table}</div>"""

def build_goal_bucket(goal_name, pbis_in_goal):
    done_states = CFG.GOAL_DONE_STATES.get(goal_name, CFG.GOAL_DONE_STATES["_default"])
    total = len(pbis_in_goal)
    done  = sum(1 for p in pbis_in_goal if p["state"] in done_states)
    ip    = sum(1 for p in pbis_in_goal
                if p["state"] not in done_states and p["state"] in CFG.INPROGRESS_STATES)
    ns    = total - done - ip

    GOAL_META = {
        "Live":                    ("#fef3c7", "#b45309", "#b45309", "Must go LIVE in Sprint 79"),
        "QAComplete":              ("#ede9fe", "#7c3aed", "#7c3aed", "QA complete by end of sprint"),
        "DevComplete":             ("#dbeafe", "#1d4ed8", "#1d4ed8", "Development complete target"),
        "DevQAComplete":           ("#ccfbf1", "#0f766e", "#0f766e", "Dev + QA both complete"),
        "AnalysisComplete":        ("#ffedd5", "#c2410c", "#c2410c", "Analysis phase completed"),
        "AnalysisAndDevComplete":  ("#dcfce7", "#15803d", "#15803d", "Analysis + Dev complete"),
        "nogoal":                  ("#fef9c3", "#854d0e", "#854d0e", "No Sprint Goal assigned"),
    }
    hdr_bg, hdr_color, badge_bg, desc = GOAL_META.get(
        goal_name, ("#f1f5f9", "#64748b", "#64748b", "Sprint goal bucket")
    )
    if goal_name == "nogoal":
        label = "No Sprint Goal"
    elif re.match(r'^Sprint\d+-', goal_name):
        label = goal_name   # already carries its sprint label, e.g. 'Sprint83-DevComplete'
    else:
        label = f"Sprint{CFG.SPRINT_NUMBER}Goal-{goal_name}"
    svg   = donut_svg(done, total, hdr_color)
    pbi_cards = "\n".join(build_pbi_card(p, i, goal_name) for i, p in enumerate(pbis_in_goal))
    bucket_id  = f"g-{goal_name}"

    return (
        f'<div style="background:#fff;border:1px solid #e2e8f0;border-radius:10px;margin-bottom:14px;overflow:hidden">'
        f'<div onclick="tgl(\'{bucket_id}\')" '
        f'style="display:flex;align-items:center;gap:14px;padding:14px 18px;cursor:pointer;'
        f'background:{hdr_bg};border-bottom:2px solid {badge_bg}33;opacity:1" '
        f'onmouseover="this.style.opacity=0.9" onmouseout="this.style.opacity=1">'
        f'{svg}'
        f'<div style="flex:1">'
        f'  <div style="font-size:15px;font-weight:700;color:{hdr_color}">{label}</div>'
        f'  <div style="font-size:12px;color:#374151;margin-top:2px">{desc}</div>'
        f'  <div style="font-size:11px;color:#64748b;margin-top:4px">'
        f'    {total} PBIs &nbsp;•&nbsp; '
        f'    <span style="color:#16a34a;font-weight:600">{done} done</span> &nbsp;•&nbsp; '
        f'    <span style="color:#d97706;font-weight:600">{ip} in progress</span> &nbsp;•&nbsp; '
        f'    <span style="color:#94a3b8">{ns} not started</span>'
        f'  </div>'
        f'</div>'
        f'<div style="display:flex;flex-direction:column;align-items:flex-end;gap:6px">'
        f'  <span style="background:{badge_bg};color:#fff;border-radius:20px;padding:3px 14px;font-size:13px;font-weight:700">{done}/{total}</span>'
        f'  <span id="{bucket_id}-i" style="color:{hdr_color};font-size:18px;font-weight:700">+</span>'
        f'</div>'
        f'</div>'
        f'<div id="{bucket_id}" style="display:none;padding:14px 18px">'
        f'{pbi_cards}'
        f'</div>'
        f'</div>'
    )

# ── Allocation model (capacity from TFS vs. JIRA estimates) ────────────────────
# Thresholds (% of holiday-adjusted capacity that JIRA estimates fill):
ALLOC_UNDER_PCT = 80     # below this = under-allocated (room for more)
ALLOC_OVER_PCT  = 100    # above this = over-allocated (more work than hours)


def _alloc_status(capacity, allocated):
    """Classify a member's allocation. Returns (key, label, color)."""
    if capacity <= 0:
        return ("nocap", "No capacity", "#94a3b8")
    pct = allocated / capacity * 100
    if pct > ALLOC_OVER_PCT:
        return ("over", "Over-allocated", "#dc2626")
    if pct < ALLOC_UNDER_PCT:
        return ("under", "Under-allocated", "#d97706")
    return ("balanced", "Balanced", "#16a34a")


def _alloc_bar(name, capacity, estimate, logged, remaining, scale_max):
    """One horizontal bar in the Azure DevOps 'Work details' style, showing all
    three sub-task numbers on a SHARED scale so bars are comparable:

    - full bar length = ESTIMATE (sub-task effort estimate / allocation);
    - split into LOGGED (teal, time already spent) + REMAINING (green when within
      capacity, RED when over-allocated);
    - a BLACK vertical tick marks the member's holiday-adjusted capacity;
    - over/under is judged on REMAINING vs capacity;
    - the label reads "est X · logged Y · rem Z / cap C".
    """
    key, _, _ = _alloc_status(capacity, remaining)
    scale     = scale_max if scale_max and scale_max > 0 else 1.0
    logged_w  = max(0.0, min(logged   / scale * 100, 100))
    est_w     = max(0.0, min(estimate / scale * 100, 100))
    rem_w     = max(0.0, min(est_w - logged_w, 100 - logged_w))
    tick_pos  = min(capacity / scale * 100, 100)
    rem_col   = "#e5403a" if key == "over" else "#69ad3c"
    lbl_col   = "#dc2626" if key == "over" else "#475569"
    def _g(x):  # 8 not 8.0; keep 32.5
        return f"{x:g}"
    return (
        f'<div style="margin-bottom:12px">'
        f'  <div style="font-size:12px;color:#1e293b;margin-bottom:3px">{name}</div>'
        f'  <div style="position:relative;background:#e6e6e6;border-radius:2px;height:18px;width:100%">'
        f'    <div style="position:absolute;left:0;top:0;background:#0891b2;'
        f'border-radius:2px 0 0 2px;height:18px;width:{logged_w:.2f}%"></div>'
        f'    <div style="position:absolute;left:{logged_w:.2f}%;top:0;background:{rem_col};'
        f'height:18px;width:{rem_w:.2f}%"></div>'
        f'    <div style="position:absolute;left:{tick_pos:.2f}%;top:-1px;height:20px;'
        f'width:2px;background:#1a1a1a"></div>'
        f'  </div>'
        f'  <div style="font-size:11px;color:{lbl_col};margin-top:3px">'
        f'est {_g(estimate)}h · <span style="color:#0891b2">logged {_g(logged)}h</span> · '
        f'rem {_g(remaining)}h / cap {_g(capacity)}h</div>'
        f'</div>'
    )


def build_capacity_section(team_data):
    total_cap_all   = sum(m["capacity"]  for ms in team_data.values() for m in ms)
    total_alloc_all = sum(m["estimated"] for ms in team_data.values() for m in ms)
    total_logged_all = sum(m["spent"] for ms in team_data.values() for m in ms)
    # Overall under/over tallies across members with capacity — judged on
    # REMAINING (estimate − logged) vs capacity.
    n_over = n_under = n_bal = 0
    for ms in team_data.values():
        for m in ms:
            rem = m.get("remaining", max(0.0, m["estimated"] - m["spent"]))
            k, _, _ = _alloc_status(m["capacity"], rem)
            n_over  += k == "over"
            n_under += k == "under"
            n_bal   += k == "balanced"

    info_banner = (
        f'<div style="background:#eff6ff;border:1px solid #bfdbfe;border-radius:8px;'
        f'padding:12px 16px;margin-bottom:16px;font-size:13px;color:#1e3a8a">'
        f'<strong>Allocation basis:</strong> capacity (base hours &minus; holidays) from the '
        f'<strong>TFS capacity tab</strong> for {CFG.SPRINT_DATES}; estimate / logged / '
        f'remaining = sum of each assignee&rsquo;s <strong>JIRA sub-task</strong> hours. '
        f'Team capacity <strong>{total_cap_all:.0f}h</strong> · estimate '
        f'<strong>{total_alloc_all:.0f}h</strong> · logged '
        f'<strong>{total_logged_all:.0f}h</strong>. Over/under judged on remaining: '
        f'<span style="color:#16a34a;font-weight:600">●</span> Balanced ({ALLOC_UNDER_PCT}–{ALLOC_OVER_PCT}%) '
        f'<span style="color:#d97706;font-weight:600">●</span> Under (&lt;{ALLOC_UNDER_PCT}%) '
        f'<span style="color:#dc2626;font-weight:600">●</span> Over (&gt;{ALLOC_OVER_PCT}%) · '
        f'{n_over} over · {n_under} under · {n_bal} balanced.'
        f'</div>'
    )
    blocks = [info_banner]
    for team, members in team_data.items():
        total_cap    = sum(m["capacity"]  for m in members)
        total_est    = sum(m["estimated"] for m in members)
        total_spent  = sum(m["spent"]     for m in members)
        total_rem    = sum(m.get("remaining", max(0.0, m["estimated"] - m["spent"])) for m in members)
        total_done   = sum(m["done"]      for m in members)
        total_tasks  = sum(m.get("tasks_total", 0) for m in members)

        # ── Horizontal allocation bars (Azure DevOps "Work details" style) ────
        # Shared scale across this team's members so the bars are comparable and
        # the black capacity ticks line up meaningfully (a 15h person's tick sits
        # left of a 32.5h person's). Each bar = estimate length, logged + remaining
        # segments, capacity tick; green within capacity, red when over.
        member_scale = max(
            [m["capacity"] for m in members] + [m["estimated"] for m in members] + [1.0]
        ) * 1.05
        member_bars = "".join(
            _alloc_bar(m["name"], m["capacity"], m["estimated"], m["spent"],
                       m.get("remaining", max(0.0, m["estimated"] - m["spent"])),
                       member_scale)
            for m in members
        )
        # Team-total bar (its own scale).
        team_scale = max(total_cap, total_est, 1.0) * 1.05
        team_bar   = _alloc_bar("Team", total_cap, total_est, total_spent,
                                total_rem, team_scale)

        alloc_block = (
            f'<div style="background:#fff;border:1px solid #e2e8f0;border-radius:10px;'
            f'padding:14px 16px;margin-bottom:12px;box-shadow:0 1px 3px rgba(0,0,0,.06)">'
            f'  <div style="font-size:13px;font-weight:700;color:#1e293b;margin-bottom:4px">'
            f'    📊 {team} — Allocation</div>'
            f'  <div style="font-size:11px;color:#94a3b8;margin-bottom:10px">'
            f'    Bar length = estimate · '
            f'    <span style="color:#0891b2;font-weight:700">teal</span> logged · '
            f'    <span style="color:#69ad3c;font-weight:700">green</span> remaining within capacity · '
            f'    <span style="color:#e5403a;font-weight:700">red</span> remaining over · '
            f'    black tick = capacity</div>'
            f'  <div style="font-weight:700;color:#0f172a;font-size:12px;margin-bottom:6px">Work</div>'
            f'  {team_bar}'
            f'  <div style="font-weight:700;color:#0f172a;font-size:12px;margin:10px 0 6px">Work By: Assigned To</div>'
            f'  {member_bars}'
            f'</div>'
        )
        blocks.append(alloc_block)

        rows = ""
        for m in members:
            util     = round(m["spent"] / m["capacity"] * 100) if m["capacity"] else 0
            over     = m["estimated"] > m["capacity"] * 1.0 and m["capacity"] > 0
            row_bg   = "background:#fff5f5" if over else ""
            est_col  = f'<td style="padding:10px 12px;text-align:center;font-size:12px;color:#dc2626;font-weight:600">{int(m["estimated"])}h</td>' if over else f'<td style="padding:10px 12px;text-align:center;font-size:12px;color:#0891b2">{int(m["estimated"])}h</td>'
            bar_w    = min(util, 100)
            bar_color= "#dc2626" if over else "#6366f1"
            rows += (
                f'<tr style="border-bottom:1px solid #f1f5f9;{row_bg}">'
                f'<td style="padding:10px 12px;font-size:12px;font-weight:500;color:#1e293b">{m["name"]}</td>'
                f'<td style="padding:10px 12px;text-align:center;font-size:12px;color:#6366f1;font-weight:600">{m["capacity"]:.0f}h</td>'
                f'{est_col}'
                f'<td style="padding:10px 12px;text-align:center;font-size:12px;color:#16a34a">{int(m["spent"])}h</td>'
                f'<td style="padding:10px 12px;text-align:center;font-size:12px;color:#374151">{m.get("tasks_total", "—")}</td>'
                f'<td style="padding:10px 12px;text-align:center"><span style="background:#dcfce7;color:#16a34a;padding:1px 8px;border-radius:10px;font-size:11px;font-weight:600">{m["done"]}</span></td>'
                f'<td style="padding:10px 12px;text-align:left">'
                f'  <div style="background:#e2e8f0;border-radius:4px;height:8px;width:80px;display:inline-block;vertical-align:middle">'
                f'    <div style="background:{bar_color};border-radius:4px;height:8px;width:{bar_w}%"></div>'
                f'  </div>'
                f'  <span style="font-size:10px;color:#64748b;margin-left:4px">{util}%</span>'
                f'</td>'
                f'</tr>'
            )

        team_util = round(total_spent / total_cap * 100) if total_cap else 0
        blocks.append(
            f'<div style="background:#fff;border:1px solid #e2e8f0;border-radius:10px;overflow:hidden;margin-bottom:20px">'
            f'  <div style="padding:10px 14px;background:#f8fafc;border-bottom:2px solid #e2e8f0;display:flex;justify-content:space-between;align-items:center">'
            f'    <span style="font-size:14px;font-weight:700;color:#1e293b">Team {team}</span>'
            f'    <span style="font-size:12px;color:#64748b">'
            f'      {total_cap:.0f}h capacity &nbsp;|&nbsp; {int(total_est)}h estimated &nbsp;|&nbsp; '
            f'      {int(total_spent)}h spent &nbsp;|&nbsp; {total_done} done &nbsp;|&nbsp; {team_util}% utilised'
            f'    </span>'
            f'  </div>'
            f'  <table style="width:100%;border-collapse:collapse">'
            f'    <thead>'
            f'      <tr style="background:#f8fafc;border-bottom:2px solid #e2e8f0">'
            f'        <th style="padding:10px 12px;text-align:left;font-size:12px;color:#64748b;font-weight:600">Member</th>'
            f'        <th style="padding:10px 12px;text-align:center;font-size:12px;color:#64748b;font-weight:600">Base</th>'
            f'        <th style="padding:10px 12px;text-align:center;font-size:12px;color:#64748b;font-weight:600">Estimated</th>'
            f'        <th style="padding:10px 12px;text-align:center;font-size:12px;color:#64748b;font-weight:600">Spent</th>'
            f'        <th style="padding:10px 12px;text-align:center;font-size:12px;color:#64748b;font-weight:600">Tasks</th>'
            f'        <th style="padding:10px 12px;text-align:center;font-size:12px;color:#64748b;font-weight:600">Done</th>'
            f'        <th style="padding:10px 12px;text-align:left;font-size:12px;color:#64748b;font-weight:600">Utilisation</th>'
            f'      </tr>'
            f'    </thead>'
            f'    <tbody>{rows}</tbody>'
            f'  </table>'
            f'</div>'
        )
    return "\n".join(blocks)

def build_goal_distribution(pbis):
    # Use the SAME population as the Goal Buckets tab — true PBIs only
    # ([PRODUCT]/[TECHNICAL] stories/tasks, not Bugs) — and the SAME dynamic goal
    # set, so this table always matches the buckets. JIRA goals (the ladder) are
    # ordered first, then any legacy/other goals, then "No Sprint Goal" last.
    true_pbis = [p for p in pbis
                 if p.get("type") != "Bug"
                 and re.match(r'^\[(product|technical)\]', str(p.get("title", "")).strip(), re.I)]
    LADDER = ["poapproved", "readyfortechanalysis", "readyfordev",
              "readyforqa", "sttodo", "readyforlive"]
    GOAL_LABELS = {"poapproved": "PO Approved", "readyfortechanalysis": "Ready for Tech Analysis",
                   "readyfordev": "Ready for Dev", "readyforqa": "Ready for QA",
                   "sttodo": "ST To Do", "readyforlive": "Ready for Live",
                   "nogoal": "No Sprint Goal"}

    present = []
    for p in true_pbis:
        g = p["goal"] or "nogoal"
        if g not in present:
            present.append(g)

    def _order(g):
        gl = str(g).lower().replace(" ", "")
        if g == "nogoal":
            return (3, 0, str(g))
        if gl in LADDER:
            return (0, LADDER.index(gl), str(g))
        return (1, 0, str(g))   # legacy/other goal names
    present.sort(key=_order)

    rows = ""
    for goal in present:
        subset = [p for p in true_pbis if (p["goal"] or "nogoal") == goal]
        if not subset:
            continue
        done_st = CFG.GOAL_DONE_STATES.get(goal, CFG.GOAL_DONE_STATES["_default"])
        done  = sum(1 for p in subset if p["state"] in done_st)
        ip    = sum(1 for p in subset
                    if p["state"] not in done_st and p["state"] in CFG.INPROGRESS_STATES)
        ns    = len(subset) - done - ip
        pct   = round(done / len(subset) * 100) if subset else 0
        label = GOAL_LABELS.get(str(goal).lower().replace(" ", ""), goal)
        rows += f"""<tr style="border-bottom:1px solid #f1f5f9">
  <td style="padding:8px 10px;font-size:12px;font-weight:500;color:#1e293b">{label}</td>
  <td style="padding:8px 10px;text-align:center;font-size:12px;font-weight:700;color:#1e293b">{len(subset)}</td>
  <td style="padding:8px 10px;text-align:center"><span style="background:#dcfce7;color:#16a34a;padding:1px 8px;border-radius:10px;font-size:11px;font-weight:600">{done}</span></td>
  <td style="padding:8px 10px;text-align:center"><span style="background:#fef3c7;color:#d97706;padding:1px 8px;border-radius:10px;font-size:11px;font-weight:600">{ip}</span></td>
  <td style="padding:8px 10px;text-align:center"><span style="background:#f1f5f9;color:#64748b;padding:1px 8px;border-radius:10px;font-size:11px;font-weight:600">{ns}</span></td>
  <td style="padding:8px 10px;text-align:center">
    <div style="background:#e2e8f0;border-radius:4px;height:8px;width:80px;display:inline-block">
      <div style="background:#6366f1;border-radius:4px;height:8px;width:{pct}%"></div></div>
    <span style="font-size:10px;color:#64748b;margin-left:4px">{done}/{len(subset)}</span>
  </td>
</tr>"""
    return f"""<table style="width:100%;border-collapse:collapse;font-size:12px">
<thead><tr style="background:#f8fafc">
  <th style="padding:8px 10px;text-align:left;color:#64748b;font-size:11px;border-bottom:1px solid #e2e8f0">Goal</th>
  <th style="padding:8px 10px;text-align:center;color:#64748b;font-size:11px;border-bottom:1px solid #e2e8f0">Total</th>
  <th style="padding:8px 10px;text-align:center;color:#64748b;font-size:11px;border-bottom:1px solid #e2e8f0">Done</th>
  <th style="padding:8px 10px;text-align:center;color:#64748b;font-size:11px;border-bottom:1px solid #e2e8f0">In Progress</th>
  <th style="padding:8px 10px;text-align:center;color:#64748b;font-size:11px;border-bottom:1px solid #e2e8f0">Not Started</th>
  <th style="padding:8px 10px;text-align:center;color:#64748b;font-size:11px;border-bottom:1px solid #e2e8f0">Progress</th>
</tr></thead>
<tbody>{rows}</tbody>
</table>"""

# ── Full HTML assembly ─────────────────────────────────────────────────────────

CSS = """
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#f1f5f9;color:#1e293b;min-height:100vh}
.hdr{background:#ffffff;border-bottom:1px solid #e2e8f0;padding:16px 28px;position:sticky;top:0;z-index:100;box-shadow:0 1px 4px rgba(0,0,0,.06)}
.hdr h1{font-size:18px;font-weight:800;color:#1e293b}
.hdr .meta{font-size:12px;color:#64748b;margin-top:2px}
.nb{background:none;border:none;padding:8px 16px;font-size:13px;font-weight:600;color:#64748b;cursor:pointer;border-bottom:2px solid transparent;transition:all .15s;white-space:nowrap}
.nb:hover{color:#6366f1;background:#f8fafc}
.nb.active{color:#6366f1;border-bottom-color:#6366f1}
.nav{display:flex;gap:2px;overflow-x:auto;border-bottom:1px solid #e2e8f0;background:#fff;padding:0 28px}
.wrap{max-width:1280px;margin:0 auto;padding:24px 28px}
.tc{display:none}
</style>
<script>
function tgl(id){
  var e=document.getElementById(id);
  var i=document.getElementById(id+'-i');
  if(!e) return;
  if(e.style.display==='none'||e.style.display===''){
    e.style.display='block';
    if(i) i.textContent='-';
  } else {
    e.style.display='none';
    if(i) i.textContent='+';
  }
}
function showTab(name,btn){
  document.querySelectorAll('.tc').forEach(function(t){t.style.display='none'});
  document.querySelectorAll('.nb').forEach(function(b){b.classList.remove('active')});
  var el=document.getElementById('t-'+name);
  if(el){el.style.display='block';}
  if(btn){btn.classList.add('active');}
}
window.onload=function(){
  var first=document.querySelector('.nb');
  if(first){first.click();}
};
</script>
"""

def build_categories_tab(pbis):
    """Groups PBIs into 6 buckets: Hotfix-79, Hotfix-80, Tech Debt, Ad-Hoc,
    Release Verification, Feature Work."""
    import re as _re

    # 6-bucket definitions: (key, label, description, bg, color)
    BUCKETS = [
        (f"Hotfix-{CFG.SPRINT_NUMBER}",
         f"Hotfix Sprint {CFG.SPRINT_NUMBER}",
         "Bug-fix items targeted at the current sprint release",
         "#fee2e2", "#dc2626"),
        (f"Hotfix-{CFG.SPRINT_NUMBER+1}",
         f"Hotfix Sprint {CFG.SPRINT_NUMBER+1}",
         "Bug-fix items earmarked for next sprint",
         "#fef3c7", "#d97706"),
        ("Tech Debt",
         "Tech Debt",
         "Technical improvement items (title prefix [Technical])",
         "#ede9fe", "#7c3aed"),
        ("Ad-Hoc",
         "Ad-Hoc",
         "Unplanned / ad-hoc work (title prefix [Ad Hoc] or [Adhoc])",
         "#ffedd5", "#ea580c"),
        ("Release Verification",
         "Release Verification",
         "Release validation & verification items (title prefix [Sprint])",
         "#dcfce7", "#16a34a"),
        ("Feature Work",
         "Feature Work",
         "Planned feature development (all remaining PBIs)",
         "#eff6ff", "#1d4ed8"),
    ]

    def classify_pbi(p):
        title = (p.get("title") or "").strip()
        tags  = (p.get("tags")  or "")
        # Hotfix check (tags take priority)
        hotfixes = _re.findall(r'Hotfix-(\d+)', tags, _re.I)
        if str(CFG.SPRINT_NUMBER) in hotfixes:
            return f"Hotfix-{CFG.SPRINT_NUMBER}"
        if str(CFG.SPRINT_NUMBER+1) in hotfixes:
            return f"Hotfix-{CFG.SPRINT_NUMBER+1}"
        # Title-prefix checks
        tl = title.lower()
        if tl.startswith("[technical]"):
            return "Tech Debt"
        if tl.startswith("[ad hoc]") or tl.startswith("[adhoc]"):
            return "Ad-Hoc"
        if tl.startswith("[sprint]"):
            return "Release Verification"
        if tl.startswith("[product]"):
            return "Feature Work"
        return None   # exclude items with no recognised title prefix

    cat_groups = {b[0]: [] for b in BUCKETS}
    for p in pbis:
        cat = classify_pbi(p)
        if cat is not None:
            cat_groups[cat].append(p)

    blocks = []
    for key, label, desc, bg, color in BUCKETS:
        pbi_list = cat_groups[key]
        if not pbi_list:
            continue
        done = ip = ns = 0
        for p in pbi_list:
            ds = CFG.GOAL_DONE_STATES.get(p.get("goal") or "_default", CFG.GOAL_DONE_STATES["_default"])
            if p["state"] in ds:                        done += 1
            elif p["state"] in CFG.INPROGRESS_STATES:   ip   += 1
            else:                                        ns   += 1
        total     = len(pbi_list)
        uid       = f"cat-{key.replace(' ','_').replace('-','_')}"
        pbi_cards = "\n".join(build_pbi_card(p, i, p.get("goal") or "nogoal", scope="cat")
                              for i, p in enumerate(pbi_list))
        blocks.append(
            f'<div style="background:#fff;border:1px solid #e2e8f0;border-radius:10px;margin-bottom:14px;overflow:hidden">'
            f'<div onclick="tgl(\'{uid}\')" style="display:flex;align-items:center;gap:14px;padding:14px 18px;cursor:pointer;background:{bg};border-bottom:2px solid {color}33" onmouseover="this.style.opacity=0.9" onmouseout="this.style.opacity=1">'
            f'{donut_svg(done, total, color)}'
            f'<div style="flex:1">'
            f'  <div style="font-size:15px;font-weight:700;color:{color}">{label}</div>'
            f'  <div style="font-size:12px;color:#374151;margin-top:2px">{desc}</div>'
            f'  <div style="font-size:11px;color:#64748b;margin-top:4px">'
            f'    {total} PBIs &nbsp;•&nbsp; <span style="color:#16a34a;font-weight:600">{done} done</span>'
            f'    &nbsp;•&nbsp; <span style="color:#d97706;font-weight:600">{ip} in progress</span>'
            f'    &nbsp;•&nbsp; <span style="color:#94a3b8">{ns} not started</span>'
            f'  </div>'
            f'</div>'
            f'<div style="display:flex;flex-direction:column;align-items:flex-end;gap:6px">'
            f'  <span style="background:{color};color:#fff;border-radius:20px;padding:3px 14px;font-size:13px;font-weight:700">{done}/{total}</span>'
            f'  <span id="{uid}-i" style="color:{color};font-size:18px;font-weight:700">+</span>'
            f'</div>'
            f'</div>'
            f'<div id="{uid}" style="display:none;padding:14px 18px">{pbi_cards}</div>'
            f'</div>'
        )
    return "\n".join(blocks)


def build_discrepancies_tab(pbis, df_cap=None, s79_tasks=None, stale_df=None):
    """Surface anything that needs the EM's attention this sprint:

    - PBIs missing a Sprint goal tag
    - PBIs carrying multiple Sprint goal tags (conflict)
    - Team members with capacity but zero tickets in the current sprint
      (the "Vrushali case" — capacity allocated, work invisible)
    - Open Tasks/Bugs assigned to current team members that still sit in
      a prior sprint (carry-overs)
    """
    import re as _re

    def _is_true_pbi(p):
        return (p.get("type") != "Bug"
                and _re.match(r'^\[(product|technical)\]',
                              str(p.get("title", "")).strip(), _re.I))

    no_goal    = [p for p in pbis if not p.get("goal") and _is_true_pbi(p)]
    multi_goal = [p for p in pbis
                  if len(_re.findall(r"Sprint\d+Goal-\w+", p.get("tags","") or "", _re.I)) > 1
                  and _is_true_pbi(p)]

    # ── Members with capacity but no Sprint N tickets ────────────────────
    # Cross-reference the capacity API (df_cap) against actual assignees in
    # the current-sprint task set (s79_tasks). Anyone with capacity > 0 but
    # zero tickets gets flagged.
    capacity_no_tickets: list[dict] = []
    if df_cap is not None and not df_cap.empty and s79_tasks is not None:
        # Sum capacity per member (df_cap has multiple Activity rows each)
        cap_by_member = (df_cap.groupby("Member")["Sprint cap"].sum()
                         .to_dict() if "Member" in df_cap.columns else {})
        # Only consider members on a configured team — strangers in the
        # capacity API (rare, but happens when someone is briefly added
        # to a team) shouldn't fire this flag.
        team_members = {m for ms in CFG.TEAMS.values() for m in ms}
        # Apply CAPACITY_NAME_MAP so the dashboard's display name lines up
        # with whatever the capacity API returns.
        name_map = getattr(CFG, "CAPACITY_NAME_MAP", {})
        # Reverse map: api_name -> dashboard_name (CAPACITY_NAME_MAP is
        # dashboard_name -> excel_name in compute_capacity()).
        api_to_dash = {v: k for k, v in name_map.items()}

        # Who has at least one ticket assigned in the current sprint?
        assignees_with_tickets = set(
            str(a) for a in s79_tasks["Assigned To"].dropna().unique() if a
        )
        # Member-to-team lookup
        member_to_team = {m: t for t, ms in CFG.TEAMS.items() for m in ms}

        for api_name, hours in cap_by_member.items():
            if not hours or hours <= 0:
                continue
            dash_name = api_to_dash.get(api_name, api_name)
            if dash_name not in team_members:
                continue
            if dash_name in assignees_with_tickets:
                continue
            capacity_no_tickets.append({
                "name":     dash_name,
                "team":     member_to_team.get(dash_name, "—"),
                "capacity": float(hours),
            })
        capacity_no_tickets.sort(key=lambda r: (-r["capacity"], r["name"]))

    # ── Stale prior-sprint tickets ───────────────────────────────────────
    stale_rows: list[dict] = []
    if stale_df is not None and not stale_df.empty:
        # Annotate each row with team for display + sort
        member_to_team = {m: t for t, ms in CFG.TEAMS.items() for m in ms}
        for _, r in stale_df.iterrows():
            stale_rows.append({
                "id":       _coerce_id(r["ID"]),
                "title":    str(r.get("Title", "") or ""),
                "type":     str(r.get("Work Item Type", "") or ""),
                "state":    str(r.get("State", "") or ""),
                "assignee": str(r.get("Assigned To", "") or ""),
                "team":     member_to_team.get(
                                str(r.get("Assigned To", "") or ""), "—"),
                "iter":     str(r.get("Iteration Path", "") or ""),
            })
        stale_rows.sort(key=lambda r: (r["team"], r["assignee"], r["id"]))

    total_disc = (len(no_goal) + len(multi_goal)
                  + len(capacity_no_tickets) + len(stale_rows))

    # ── Section renderers ────────────────────────────────────────────────
    def disc_section(pbi_list, scope_prefix):
        if not pbi_list:
            return ""
        return "\n".join(
            build_pbi_card(p, i, p.get("goal") or "nogoal", scope=scope_prefix)
            for i, p in enumerate(pbi_list)
        )

    ng_html = (disc_section(no_goal, "disc_ng")
               or '<div style="color:#16a34a;font-size:12px;padding:8px">✅ All PBIs have goal tags</div>')
    mg_html = (disc_section(multi_goal, "disc_mg")
               or '<div style="color:#16a34a;font-size:12px;padding:8px">✅ No multi-goal conflicts found</div>')

    # Capacity-without-tickets: simple table
    if capacity_no_tickets:
        cnt_rows = "".join(
            f'<tr style="border-bottom:1px solid #f1f5f9">'
            f'<td style="padding:8px 12px;font-size:12px;font-weight:600;color:#1e293b">{r["name"]}</td>'
            f'<td style="padding:8px 12px;font-size:12px;color:#64748b">{r["team"]}</td>'
            f'<td style="padding:8px 12px;font-size:12px;text-align:right;color:#dc2626;font-weight:600">{r["capacity"]:.0f}h</td>'
            f'</tr>'
            for r in capacity_no_tickets
        )
        cnt_html = (
            '<div style="background:#fff;border:1px solid #e2e8f0;border-radius:8px;overflow:hidden">'
            '<table style="width:100%;border-collapse:collapse;font-size:12px">'
            '<thead><tr style="background:#f8fafc">'
            '<th style="padding:8px 12px;text-align:left;color:#64748b;font-size:11px;border-bottom:1px solid #e2e8f0">Member</th>'
            '<th style="padding:8px 12px;text-align:left;color:#64748b;font-size:11px;border-bottom:1px solid #e2e8f0">Team</th>'
            '<th style="padding:8px 12px;text-align:right;color:#64748b;font-size:11px;border-bottom:1px solid #e2e8f0">Capacity</th>'
            f'</tr></thead><tbody>{cnt_rows}</tbody></table></div>'
        )
    else:
        cnt_html = ('<div style="color:#16a34a;font-size:12px;padding:8px">'
                    '✅ Every member with capacity has at least one ticket in the sprint</div>')

    # Stale prior-sprint tickets table
    if stale_rows:
        st_rows = "".join(
            f'<tr style="border-bottom:1px solid #f1f5f9">'
            f'<td style="padding:8px 12px;font-size:12px;color:#1e293b;font-weight:600">{r["id"]}</td>'
            f'<td style="padding:8px 12px;font-size:12px;color:#374151">{r["title"][:80]}</td>'
            f'<td style="padding:8px 12px;font-size:11px;color:#64748b">'
            f'  <span style="background:#f1f5f9;padding:1px 8px;border-radius:10px">{r["type"]}</span></td>'
            f'<td style="padding:8px 12px;font-size:11px;color:#374151">{r["state"]}</td>'
            f'<td style="padding:8px 12px;font-size:12px;color:#1e293b">{r["assignee"]}</td>'
            f'<td style="padding:8px 12px;font-size:11px;color:#64748b">{r["team"]}</td>'
            f'<td style="padding:8px 12px;font-size:11px;color:#64748b;font-family:ui-monospace,monospace">{r["iter"]}</td>'
            f'</tr>'
            for r in stale_rows
        )
        st_html = (
            '<div style="background:#fff;border:1px solid #e2e8f0;border-radius:8px;overflow:auto">'
            '<table style="width:100%;border-collapse:collapse;font-size:12px;min-width:880px">'
            '<thead><tr style="background:#f8fafc">'
            '<th style="padding:8px 12px;text-align:left;color:#64748b;font-size:11px;border-bottom:1px solid #e2e8f0">ID</th>'
            '<th style="padding:8px 12px;text-align:left;color:#64748b;font-size:11px;border-bottom:1px solid #e2e8f0">Title</th>'
            '<th style="padding:8px 12px;text-align:left;color:#64748b;font-size:11px;border-bottom:1px solid #e2e8f0">Type</th>'
            '<th style="padding:8px 12px;text-align:left;color:#64748b;font-size:11px;border-bottom:1px solid #e2e8f0">State</th>'
            '<th style="padding:8px 12px;text-align:left;color:#64748b;font-size:11px;border-bottom:1px solid #e2e8f0">Assignee</th>'
            '<th style="padding:8px 12px;text-align:left;color:#64748b;font-size:11px;border-bottom:1px solid #e2e8f0">Team</th>'
            '<th style="padding:8px 12px;text-align:left;color:#64748b;font-size:11px;border-bottom:1px solid #e2e8f0">Iteration</th>'
            f'</tr></thead><tbody>{st_rows}</tbody></table></div>'
        )
    else:
        st_html = ('<div style="color:#16a34a;font-size:12px;padding:8px">'
                   '✅ No open carry-over tickets in prior sprints</div>')

    summary = (
        f'<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:12px;margin-bottom:20px">'
        f'<div style="background:{"#fff5f5" if no_goal else "#f0fdf4"};border:1px solid {"#fca5a5" if no_goal else "#86efac"};border-radius:10px;padding:14px 16px">'
        f'  <div style="font-size:24px;font-weight:800;color:{"#dc2626" if no_goal else "#16a34a"}">{len(no_goal)}</div>'
        f'  <div style="font-size:12px;color:#64748b;margin-top:2px;font-weight:500">No Goal Tag</div>'
        f'</div>'
        f'<div style="background:{"#fff5f5" if multi_goal else "#f0fdf4"};border:1px solid {"#fca5a5" if multi_goal else "#86efac"};border-radius:10px;padding:14px 16px">'
        f'  <div style="font-size:24px;font-weight:800;color:{"#dc2626" if multi_goal else "#16a34a"}">{len(multi_goal)}</div>'
        f'  <div style="font-size:12px;color:#64748b;margin-top:2px;font-weight:500">Tag Conflicts</div>'
        f'</div>'
        f'<div style="background:{"#fff5f5" if capacity_no_tickets else "#f0fdf4"};border:1px solid {"#fca5a5" if capacity_no_tickets else "#86efac"};border-radius:10px;padding:14px 16px">'
        f'  <div style="font-size:24px;font-weight:800;color:{"#dc2626" if capacity_no_tickets else "#16a34a"}">{len(capacity_no_tickets)}</div>'
        f'  <div style="font-size:12px;color:#64748b;margin-top:2px;font-weight:500">Capacity, no tickets</div>'
        f'</div>'
        f'<div style="background:{"#fff5f5" if stale_rows else "#f0fdf4"};border:1px solid {"#fca5a5" if stale_rows else "#86efac"};border-radius:10px;padding:14px 16px">'
        f'  <div style="font-size:24px;font-weight:800;color:{"#dc2626" if stale_rows else "#16a34a"}">{len(stale_rows)}</div>'
        f'  <div style="font-size:12px;color:#64748b;margin-top:2px;font-weight:500">Stale carry-overs</div>'
        f'</div>'
        f'<div style="background:#fff;border:1px solid #e2e8f0;border-radius:10px;padding:14px 16px">'
        f'  <div style="font-size:24px;font-weight:800;color:{"#dc2626" if total_disc else "#16a34a"}">{total_disc}</div>'
        f'  <div style="font-size:12px;color:#64748b;margin-top:2px;font-weight:500">Total Issues</div>'
        f'</div>'
        f'</div>'
    )
    return (
        f'{summary}'
        f'<div style="background:#fff;border:1px solid #e2e8f0;border-radius:10px;overflow:hidden;margin-bottom:14px">'
        f'<div style="padding:10px 14px;background:#fef9c3;border-bottom:1px solid #e2e8f0">'
        f'  <strong style="font-size:13px;color:#854d0e">⚠ No Sprint Goal Tag ({len(no_goal)})</strong></div>'
        f'<div style="padding:12px 14px">{ng_html}</div>'
        f'</div>'
        f'<div style="background:#fff;border:1px solid #e2e8f0;border-radius:10px;overflow:hidden;margin-bottom:14px">'
        f'<div style="padding:10px 14px;background:#fee2e2;border-bottom:1px solid #e2e8f0">'
        f'  <strong style="font-size:13px;color:#dc2626">🔴 Multi-Goal Tag Conflicts ({len(multi_goal)})</strong></div>'
        f'<div style="padding:12px 14px">{mg_html}</div>'
        f'</div>'
        f'<div style="background:#fff;border:1px solid #e2e8f0;border-radius:10px;overflow:hidden;margin-bottom:14px">'
        f'<div style="padding:10px 14px;background:#fef3c7;border-bottom:1px solid #e2e8f0">'
        f'  <strong style="font-size:13px;color:#92400e">👤 Capacity Allocated, No Tickets ({len(capacity_no_tickets)})</strong>'
        f'  <span style="color:#92400e;font-size:11px;margin-left:8px;font-weight:400">— member has hours booked but isn\'t assigned to anything in this sprint</span>'
        f'</div>'
        f'<div style="padding:12px 14px">{cnt_html}</div>'
        f'</div>'
        f'<div style="background:#fff;border:1px solid #e2e8f0;border-radius:10px;overflow:hidden">'
        f'<div style="padding:10px 14px;background:#ffedd5;border-bottom:1px solid #e2e8f0">'
        f'  <strong style="font-size:13px;color:#9a3412">⏳ Stale Prior-Sprint Tickets ({len(stale_rows)})</strong>'
        f'  <span style="color:#9a3412;font-size:11px;margin-left:8px;font-weight:400">— open Tasks/Bugs assigned to team members but parked in a different sprint</span>'
        f'</div>'
        f'<div style="padding:12px 14px">{st_html}</div>'
        f'</div>'
    )


def build_team_pbi_status(pbis):
    """Team-wise PBI status: cards + table for the Goals tab bottom."""
    member_to_team = {m: t for t, ms in CFG.TEAMS.items() for m in ms}
    team_stats = {t: {"done":0,"ip":0,"ns":0,"total":0} for t in CFG.TEAMS}
    for p in pbis:
        team = member_to_team.get(p.get("assignee",""))
        if not team:
            continue
        ds = CFG.GOAL_DONE_STATES.get(p.get("goal") or "_default", CFG.GOAL_DONE_STATES["_default"])
        team_stats[team]["total"] += 1
        if p["state"] in ds:                           team_stats[team]["done"] += 1
        elif p["state"] in CFG.INPROGRESS_STATES:      team_stats[team]["ip"]   += 1
        else:                                          team_stats[team]["ns"]   += 1

    TEAM_COLORS = {"Calmers":"#6366f1","Knackers":"#0891b2","Crackers":"#16a34a"}
    cards_html = '<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:12px;margin-bottom:16px">'
    for team, st in team_stats.items():
        color = TEAM_COLORS.get(team, "#6366f1")
        total = max(st["total"], 1)
        pct   = round(st["done"]/total*100)
        cards_html += (
            f'<div style="background:#fff;border:1px solid #e2e8f0;border-radius:10px;padding:14px 16px">'
            f'  <div style="display:flex;align-items:center;gap:10px;margin-bottom:10px">'
            f'    {donut_svg(st["done"], st["total"], color)}'
            f'    <div><div style="font-size:13px;font-weight:700;color:#1e293b">{team}</div>'
            f'    <div style="font-size:11px;color:{color};font-weight:600">{pct}% complete</div></div>'
            f'  </div>'
            f'  <div style="font-size:11px;color:#64748b">'
            f'    <span style="color:#16a34a;font-weight:600">{st["done"]} done</span> &nbsp;•&nbsp; '
            f'    <span style="color:#d97706;font-weight:600">{st["ip"]} in progress</span> &nbsp;•&nbsp; '
            f'    <span style="color:#94a3b8">{st["ns"]} not started</span>'
            f'  </div>'
            f'</div>'
        )
    cards_html += '</div>'
    trows = ""
    for team, st in team_stats.items():
        color = TEAM_COLORS.get(team, "#6366f1")
        total = max(st["total"], 1)
        pct   = round(st["done"]/total*100)
        trows += (
            f'<tr style="border-bottom:1px solid #f1f5f9">'
            f'<td style="padding:8px 12px;font-size:12px;font-weight:600;color:#1e293b">{team}</td>'
            f'<td style="padding:8px 12px;text-align:center;font-size:12px;font-weight:700">{st["total"]}</td>'
            f'<td style="padding:8px 12px;text-align:center"><span style="background:#dcfce7;color:#16a34a;padding:2px 8px;border-radius:10px;font-size:11px;font-weight:600">{st["done"]}</span></td>'
            f'<td style="padding:8px 12px;text-align:center"><span style="background:#fef3c7;color:#d97706;padding:2px 8px;border-radius:10px;font-size:11px;font-weight:600">{st["ip"]}</span></td>'
            f'<td style="padding:8px 12px;text-align:center"><span style="background:#f1f5f9;color:#64748b;padding:2px 8px;border-radius:10px;font-size:11px;font-weight:600">{st["ns"]}</span></td>'
            f'<td style="padding:8px 12px;text-align:left">'
            f'  <div style="background:#e2e8f0;border-radius:4px;height:8px;width:80px;display:inline-block;vertical-align:middle">'
            f'    <div style="background:{color};border-radius:4px;height:8px;width:{min(pct,100)}%"></div></div>'
            f'  <span style="font-size:10px;color:#64748b;margin-left:4px">{pct}%</span>'
            f'</td></tr>'
        )
    table_html = (
        '<div style="background:#fff;border:1px solid #e2e8f0;border-radius:10px;overflow:hidden">'
        '<table style="width:100%;border-collapse:collapse;font-size:12px">'
        '<thead><tr style="background:#f8fafc">'
        '<th style="padding:8px 12px;text-align:left;color:#64748b;font-size:11px;border-bottom:1px solid #e2e8f0">Team</th>'
        '<th style="padding:8px 12px;text-align:center;color:#64748b;font-size:11px;border-bottom:1px solid #e2e8f0">Total PBIs</th>'
        '<th style="padding:8px 12px;text-align:center;color:#64748b;font-size:11px;border-bottom:1px solid #e2e8f0">Done</th>'
        '<th style="padding:8px 12px;text-align:center;color:#64748b;font-size:11px;border-bottom:1px solid #e2e8f0">In Progress</th>'
        '<th style="padding:8px 12px;text-align:center;color:#64748b;font-size:11px;border-bottom:1px solid #e2e8f0">Not Started</th>'
        '<th style="padding:8px 12px;text-align:left;color:#64748b;font-size:11px;border-bottom:1px solid #e2e8f0">Progress</th>'
        f'</tr></thead><tbody>{trows}</tbody></table></div>'
    )
    return (
        '<div style="margin-top:28px;border-top:2px solid #e2e8f0;padding-top:20px">'
        '<div style="font-size:14px;font-weight:700;color:#1e293b;margin-bottom:14px">👥 Team-wise PBI Standing</div>'
        f'{cards_html}{table_html}'
        '</div>'
    )


def build_html(pbis, s79_tasks, team_capacity_data, extra_tabs=None,
               df_cap=None, stale_df=None):
    # Group PBIs by goal
    GOAL_ORDER = ["Live","QAComplete","DevComplete","DevQAComplete",
                  "AnalysisComplete","AnalysisAndDevComplete"]
    goal_groups = {g: [] for g in GOAL_ORDER}
    goal_groups["nogoal"] = []
    for p in pbis:
        # Only true PBIs ([PRODUCT] or [TECHNICAL] title prefix) belong in goal buckets
        if p.get("type") == "Bug":
            continue
        if not re.match(r'^\[(product|technical)\]', str(p.get("title", "")).strip(), re.I):
            continue
        g = p["goal"] or "nogoal"
        if g not in goal_groups:
            goal_groups[g] = []
        goal_groups[g].append(p)

    overview_cards, metrics = build_overview_cards(pbis, s79_tasks)
    sprint_progress_html, sp = build_sprint_progress(pbis, metrics)
    capacity_html  = build_capacity_section(team_capacity_data)
    goal_dist_html    = build_goal_distribution(pbis)
    dev_tracker_html  = build_dev_tracker(pbis)
    # Collect any goal names not in the standard order (e.g. 'Sprint83-DevComplete')
    extra_goals = sorted(
        g for g in goal_groups
        if g not in GOAL_ORDER and g != "nogoal" and goal_groups.get(g)
    )
    goal_buckets   = "\n".join(
        build_goal_bucket(g, goal_groups[g])
        for g in (GOAL_ORDER + extra_goals + ["nogoal"])
        if goal_groups.get(g)
    )

    # State distribution bar for overview — true PBIs only ([PRODUCT]/[TECHNICAL])
    state_counts = {}
    for p in pbis:
        if p.get("type") == "Bug":
            continue
        if not re.match(r'^\[(product|technical)\]', str(p.get("title", "")).strip(), re.I):
            continue
        state_counts[p["state"]] = state_counts.get(p["state"], 0) + 1

    state_bars = ""
    total_p = len(pbis)
    for st, cnt in sorted(state_counts.items(), key=lambda x: -x[1]):
        pct = round(cnt / total_p * 100)
        bg, fg = STATE_COLORS.get(st, ("#f1f5f9", "#64748b"))
        state_bars += (f'<div style="display:flex;align-items:center;gap:10px;margin-bottom:7px">'
                       f'<div style="width:150px;font-size:12px;color:#374151;text-align:right;flex-shrink:0">{st}</div>'
                       f'<div style="flex:1;background:#f1f5f9;border-radius:4px;height:22px;position:relative">'
                       f'<div style="background:{bg};border:1px solid {fg}55;border-radius:4px;height:22px;'
                       f'width:{pct}%;display:flex;align-items:center;padding-left:6px">'
                       f'<span style="font-size:11px;font-weight:600;color:{fg}">{cnt}</span></div></div>'
                       f'<div style="width:32px;font-size:11px;color:#94a3b8;flex-shrink:0">{pct}%</div></div>')

    generated_at = pd.Timestamp.now().strftime("%d %b %Y %H:%M")

    extra_tabs_html = extra_tabs or ""

    # ── Build additional tab content ───────────────────────────────────────────
    categories_html    = build_categories_tab(pbis)
    discrepancies_html = build_discrepancies_tab(
        pbis, df_cap=df_cap, s79_tasks=s79_tasks, stale_df=stale_df,
    )
    team_pbi_html      = build_team_pbi_status(pbis)

    # Nav badge counts — use already-classified goal data (same source as build_discrepancies_tab)
    goal_count = sum(1 for g in (GOAL_ORDER + ["nogoal"]) if goal_groups.get(g))
    _no_goal   = [p for p in pbis if not p.get("goal")
                  and p.get("type") != "Bug"
                  and re.match(r'^\[(product|technical)\]', str(p.get("title","")).strip(), re.I)]
    _multi     = [p for p in pbis
                  if len(re.findall(r"Sprint\d+Goal-\w+", p.get("tags","") or "", re.I)) > 1
                  and p.get("type") != "Bug"
                  and re.match(r'^\[(product|technical)\]', str(p.get("title","")).strip(), re.I)]
    # Capacity-no-tickets count for nav badge
    _cnt_count = 0
    if df_cap is not None and not df_cap.empty and "Member" in df_cap.columns:
        _team_members = {m for ms in CFG.TEAMS.values() for m in ms}
        _name_map     = getattr(CFG, "CAPACITY_NAME_MAP", {})
        _api_to_dash  = {v: k for k, v in _name_map.items()}
        _cap_by_member = df_cap.groupby("Member")["Sprint cap"].sum().to_dict()
        _assignees     = set(
            str(a) for a in s79_tasks["Assigned To"].dropna().unique() if a
        )
        for _api_name, _hrs in _cap_by_member.items():
            if not _hrs or _hrs <= 0:
                continue
            _dash = _api_to_dash.get(_api_name, _api_name)
            if _dash in _team_members and _dash not in _assignees:
                _cnt_count += 1
    _stale_count = 0 if stale_df is None else len(stale_df)
    disc_count = len(_no_goal) + len(_multi) + _cnt_count + _stale_count

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_dashboard_title()}</title>
{CSS}
</head>
<body>

<!-- ── Sticky Header ── -->
<div class="hdr">
  <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px">
    <div>
      <h1>{_dashboard_title()}</h1>
      <div class="meta">{CFG.SPRINT_DATES} &nbsp;·&nbsp; Day {CFG.SPRINT_DAY} of {CFG.SPRINT_TOTAL_DAYS} &nbsp;·&nbsp; {metrics["total_pbis"]} PBIs &nbsp;·&nbsp; {metrics["pbis_done"]} Done &nbsp;·&nbsp; Generated {generated_at}</div>
    </div>
  </div>
</div>

<!-- ── Tab Nav ── -->
<div class="nav">
  <button class="nb" onclick="showTab('overview',this)">Overview</button>
  <button class="nb" onclick="showTab('goals',this)">Goal Buckets ({goal_count})</button>
  <button class="nb" onclick="showTab('categories',this)">Categories</button>
  <button class="nb" onclick="showTab('capacity',this)">Capacity</button>
  <button class="nb" onclick="showTab('discrepancies',this)">Discrepancies ({disc_count})</button>
  <button class="nb" onclick="showTab('daily',this)">📅 Daily Tracking</button>
  <button class="nb" onclick="showTab('dsm',this)">🎯 DSM Insights</button>
  <button class="nb" onclick="showTab('risk',this)">⚡ Risk &amp; Health</button>
</div>

<!-- ── Tab Content Wrapper ── -->
<div class="wrap">

<!-- Tab: Overview -->
<div id="t-overview" class="tc">
{overview_cards}
<div style="display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:20px">
  <div style="background:#fff;border:1px solid #e2e8f0;border-radius:12px;padding:16px;box-shadow:0 1px 3px rgba(0,0,0,.06)">
    <div style="font-size:13px;font-weight:700;color:#1e293b;margin-bottom:12px">PBI State Distribution</div>
    {state_bars}
  </div>
  <div style="background:#fff;border:1px solid #e2e8f0;border-radius:12px;padding:16px;box-shadow:0 1px 3px rgba(0,0,0,.06)">
    {sprint_progress_html}
  </div>
</div>
<div style="background:#fff;border:1px solid #e2e8f0;border-radius:12px;padding:16px;box-shadow:0 1px 3px rgba(0,0,0,.06);margin-bottom:20px">
  <div style="font-size:13px;font-weight:700;color:#1e293b;margin-bottom:12px">🎯 Sprint Goal Distribution</div>
  {goal_dist_html}
</div>
{dev_tracker_html}
</div>

<!-- Tab: Goal Buckets -->
<div id="t-goals" class="tc">
<div style="display:flex;flex-direction:column;gap:12px">
{goal_buckets}
</div>
{team_pbi_html}
</div>

<!-- Tab: Categories -->
<div id="t-categories" class="tc">
{categories_html}
</div>

<!-- Tab: Capacity -->
<div id="t-capacity" class="tc">
{capacity_html}
</div>

<!-- Tab: Discrepancies -->
<div id="t-discrepancies" class="tc">
{discrepancies_html}
</div>

{extra_tabs_html}

</div><!-- /.wrap -->
</body>
</html>"""

# ── Main ───────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(
        description="Generate the daily sprint dashboard from JIRA Cloud. "
                    "Work items come from JIRA; capacity base hours + holidays "
                    "come from the TFS capacity tab.")
    parser.add_argument("--release", help="JIRA Fix Version (overrides RELEASE_NAME).")
    parser.add_argument("--sprint",  help="JIRA Sprint name (overrides JIRA_SPRINT_NAME).")
    args = parser.parse_args()

    # Jira-only generator.
    CFG.DATA_SOURCE = "jira"
    if args.release is not None:
        CFG.RELEASE_NAME = args.release
    if args.sprint is not None:
        CFG.JIRA_SPRINT_NAME = args.sprint

    CFG.OUTPUT_HTML  = f"Sprint{CFG.SPRINT_NUMBER}_Dashboard_Day{CFG.DISPLAY_SPRINT_DAY}.html"
    CFG.HISTORY_FILE = f"Sprint{CFG.SPRINT_NUMBER}_history.json"
    print(f"🧭 Source: JIRA  →  {CFG.OUTPUT_HTML}")

    df_pbis, df_tasks, df_cap, df_raw_orig = load_data()

    pbis       = classify_pbis(df_pbis, df_tasks, df_raw_orig)
    team_cap   = compute_capacity(df_tasks, df_cap)
    # In-sprint tasks/bugs: the JIRA fetch already scopes to the sprint+release
    # and pulls sub-tasks by parent (which usually lack the Sprint field), so we
    # keep rows matching the sprint name OR with a blank Iteration Path.
    s79_tasks  = _in_sprint_tasks(df_tasks)

    # ── Overview metrics (needed for extra tabs) ───────────────────────────────
    _, metrics = build_overview_cards(pbis, s79_tasks)

    # ── Build capacity lookup (name→hours) for extra tabs ─────────────────────
    cap_lookup = {}
    for team, members in team_cap.items():
        for m in members:
            cap_lookup[m["name"]] = m["capacity"]

    # ── History management ─────────────────────────────────────────────────────
    history_path = script_dir / CFG.HISTORY_FILE
    history      = load_history(history_path)
    today_snap   = take_snapshot(
        date.today().isoformat(), CFG.SPRINT_DAY, s79_tasks, pbis, metrics
    )
    history      = upsert_snapshot(history, today_snap)
    save_history(history_path, history)
    print(f"💾 History saved: {len(history['snapshots'])} day(s) tracked")

    # ── Build extra tabs ───────────────────────────────────────────────────────
    print("📅 Building Daily Tracking tab ...")
    daily_tab = build_daily_tracking_tab(
        s79_tasks, history, cap_lookup,
        CFG.SPRINT_DAY, CFG.SPRINT_TOTAL_DAYS
    )
    print("🎯 Building DSM Insights tab ...")
    dsm_tab = build_dsm_tab(
        s79_tasks, history, pbis, metrics,
        cap_lookup, CFG.SPRINT_DAY, CFG.SPRINT_TOTAL_DAYS
    )
    print("⚡ Building Risk & Health tab ...")
    risk_tab = build_risk_health_tab(
        s79_tasks, history, pbis, metrics,
        cap_lookup, CFG.SPRINT_DAY, CFG.SPRINT_TOTAL_DAYS,
        CFG.SPRINT_START_DATE
    )
    extra_tabs = daily_tab + "\n" + dsm_tab + "\n" + risk_tab

    # ── Sprint <-> Release scope mismatches (for the Discrepancies tab) ────────
    # JIRA equivalent of the old TFS stale-ticket probe: flag issues where the
    # Sprint and the Fix Version disagree (in the release but not the sprint, or
    # vice-versa). Surfaced to console + CSV.
    stale_df = None
    rel = getattr(CFG, "RELEASE_NAME", None)
    spr = _sprint_names()   # list of per-team sprint names
    if rel and spr:
        try:
            import jira_fetch
            print("🔍 Checking Sprint <-> Release mismatches ...")
            mm = jira_fetch.load_mismatches(rel, spr, ctx=jira_get_context())
            print(f"   Found {len(mm)} Sprint/Release mismatch(es).")
            if not mm.empty:
                mm_path = script_dir / f"Sprint{CFG.SPRINT_NUMBER}_mismatches.csv"
                mm.to_csv(mm_path, index=False)
                print(f"   ⚠ Mismatches written to {mm_path.name}")
        except Exception as e:
            print(f"   ⚠ Mismatch check failed ({e}); continuing without.")

    # ── Assemble HTML ──────────────────────────────────────────────────────────
    print(f"🔨 Assembling 7-tab dashboard for {len(pbis)} PBIs ...")
    html = build_html(
        pbis, s79_tasks, team_cap,
        extra_tabs=extra_tabs,
        df_cap=df_cap,
        stale_df=stale_df,
    )

    out_path = script_dir / CFG.OUTPUT_HTML
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)

    size_kb = out_path.stat().st_size // 1024
    print(f"✅ Saved: {out_path.name}  ({size_kb} KB)")
    print(f"   PBIs: {len(pbis)}  |  Tasks: {len(s79_tasks)}  |  History: {len(history['snapshots'])} days")
