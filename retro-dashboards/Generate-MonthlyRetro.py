#!/usr/bin/env python3
"""
Generate-MonthlyRetro.py
========================
Build the monthly release retro from the daily JIRA snapshots written by
Snapshot-JiraRelease.py. Reads snapshots/jira/<RELEASE>/*.json — works OFFLINE
(no JIRA call), using the latest snapshot's per-issue changelog for exact
time-in-status / date-met, and the snapshot sequence for scope creep.

KPIs:
  * Time in Backlog / Time in BA Analysis (avg + per item)            [changelog]
  * Dev / Refinement / QA complete-date MET?                          [planned date field vs status reached]
  * Defects by severity                                               [bugs]
  * Epics shipped by size (XS/S/M/L/XL)                               [sizing + done]
  * Scope creep (items added after release start)                     [snapshot diff]
  * Time split: analysis vs implementation vs test-case vs QA         [sub-task time_spent by title]
  * Total preprod bugs + group by RCA                                 [bugs]

Output: reports/<RELEASE>_Monthly_Retro.html

Usage:
    python Generate-MonthlyRetro.py            # REL-AUG-26
    python Generate-MonthlyRetro.py REL-AUG-26
"""

from __future__ import annotations

import html as _html
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent
DEFAULT_RELEASE = "REL-AUG-26"

# Team attribution (snapshot has no team field → map assignee via the roster).
sys.path.insert(0, str(_HERE))
try:
    from retro_combine import member_to_team as _member_to_team  # noqa: E402
except Exception:
    def _member_to_team(name):  # fallback if roster unavailable
        return "Other"

# Optional per-sprint executive-retro trend (82/83/84...). Config-driven so it
# renders whatever sprints are supplied; missing sprints are flagged.
try:
    from exec_trend_config import TREND as _EXEC_TREND  # noqa: E402
except Exception:
    _EXEC_TREND = {}

# ── date-met targets: planned-date field name match -> status(es) that satisfy it.
# 'reached' lists the JIRA status names that satisfy each target.
TARGETS = [
    {"label": "Refinement complete", "match": ["refinement complete", "refinement"],
     "reached": ["Ready for Tech Analysis"]},
    {"label": "Dev complete", "match": ["dev complete", "development complete"],
     "reached": ["PR", "Dev Completed", "Dev Complete"]},
    {"label": "QA complete", "match": ["qa complete"],
     "reached": ["ST To Do", "ST In Progress"]},
]

# Work-item taxonomy differs by source: JIRA = Story/Epic/Sub-task,
# The "unit" is the deliverable we measure (Epic / Story / Bug).
UNIT_TYPES = {"Story", "Product Backlog Item", "Feature"}
SUBTASK_TYPES = {"Sub-task", "Task"}
# Backlog/BA-Analysis status equivalents across systems.
BACKLOG_STATES = {"backlog", "new"}
BA_STATES = {"ba analysis", "analysis", "analysis in progress"}


def _norm_sev(s) -> str:
    """Severity may be '3 - Medium' or 'Medium'. Extract the word."""
    sl = str(s or "").lower()
    for w in ("critical", "high", "medium", "low"):
        if w in sl:
            return w
    return ""

# ── time-split categories: sub-task title keywords -> bucket
TIME_BUCKETS = [
    ("Analysis", ["analysis", "[ba", "refinement", "tech analysis"]),
    ("Test-case creation", ["test case", "test scenario", "task analysis", "testcase creation"]),
    ("QA", ["[qa", "qa server", "stage verification", "testcase execution", "regression"]),
    ("Implementation", ["[dev", "implementation", "code change", "development", "bug fix",
                        "bugfix", "rework", "unit test", "demo"]),
]

DONE_STATES = {"done", "live", "live in progress", "integrated", "ready for live", "closed"}
SIZE_ORDER = ["XS", "S", "M", "L", "XL"]

# Work-item deep links (JIRA issue keys like MPM-31).
JIRA_BROWSE_BASE = "https://motivity.atlassian.net/browse/"
_UID = [0]


def _uid() -> str:
    _UID[0] += 1
    return f"x{_UID[0]}"


def _item_link(source: str, rid) -> str:
    r = _html.escape(str(rid))
    return f"<a href='{JIRA_BROWSE_BASE}{r}' target='_blank'>{r}</a>"

# ── Release scoring (per Release_Scoring_Proposal) ──────────────────────────
SIZE_POINTS = {"xs": 5, "s": 10, "m": 15, "l": 20}
SEV_POINTS = {"critical": -15, "high": -10, "medium": -5, "low": -3}
SUGGESTION_POINTS = 3
POS_PREFIXES = {"product", "technical", "expedite", "integration"}
NOT_A_BUG = ["not a bug", "duplicate", "cannot reproduce", "cnr", "by design",
             "works as designed", "not reproducible"]
_SIZE_ALIAS = {"extra small": "xs", "small": "s", "medium": "m", "large": "l",
               "extra large": "xl"}


def _prefixes(title: str) -> set:
    return set(re.findall(r"\[([a-z]+)\]", (title or "").lower()))


def _norm_size(s) -> str:
    s = str(s or "").strip().lower().strip("[]")
    s = _SIZE_ALIAS.get(s, s)
    if s in SIZE_POINTS:
        return s
    return "m" if s in ("", "xl") else s   # XL not allowed → treat as L-cap 'm' default


def score_issue(it: dict) -> dict:
    """Score one item per the Release Scoring Proposal. Returns a dict with the
    category, points, bucket (delivery/suggestion/bug/overhead/excluded/untagged),
    and data-quality flags (assumed_size, no_sev)."""
    title = it.get("summary", "") or ""
    pre = _prefixes(title)
    typ = (it.get("type") or "").lower()
    reason = (it.get("reason") or "").lower()
    rc = (it.get("root_cause") or "").lower()
    is_bug = typ in ("bug", "defect") or ("internal" in pre and "bug" in title.lower())

    def out(category, points, bucket, assumed=False, no_sev=False):
        return {"category": category, "points": points, "bucket": bucket,
                "assumed_size": assumed, "no_sev": no_sev}

    if "suggestion" in pre:
        return out("Suggestion", SUGGESTION_POINTS, "suggestion")
    if is_bug:
        if any(x in reason or x in rc for x in NOT_A_BUG):
            return out("Bug (excluded)", 0, "excluded")
        sev = _norm_sev(it.get("severity"))
        return out("Bug", SEV_POINTS.get(sev, 0), "bug", no_sev=not bool(sev))
    pos = pre & POS_PREFIXES
    if pos:
        raw = str(it.get("sizing") or "").strip()
        explicit = (raw.lower().strip("[]") in SIZE_POINTS
                    or _SIZE_ALIAS.get(raw.lower()) in SIZE_POINTS)
        size = _norm_size(raw)
        pts = SIZE_POINTS.get(size, 0)
        assumed = (not explicit) and size == "m"     # unsized positive → assumed M (+15)
        cat = " + ".join(p.title() for p in sorted(pos))
        return out(cat, pts, "delivery", assumed=assumed)
    return out("Internal / overhead", 0, "overhead")


# Categories hidden from the score breakdown table (still counted in totals=0).
_HIDE_CATEGORIES = {"Internal / overhead"}


def compute_score(issues):
    cat_n, cat_pts = Counter(), Counter()
    cat_items = defaultdict(list)     # category -> [(id, title, points, assignee)]
    delivery = suggestion = bug_cost = total = 0
    assumed, no_sev = [], []
    t_net, t_val, t_bug, t_n = Counter(), Counter(), Counter(), Counter()
    t_items = defaultdict(list)       # team -> [(id, title, points, category, assignee)]
    for it in issues:
        r = score_issue(it)
        p, b, cat = r["points"], r["bucket"], r["category"]
        team = _member_to_team(it.get("assignee", "")) or "Other"
        # size (delivery) or severity (bug); excluded bugs show WHY they're 0
        if b == "delivery":
            size_disp = _norm_size(it.get("sizing")).upper()
        elif cat == "Bug (excluded)":
            size_disp = (it.get("root_cause") or it.get("reason") or "excluded")
        elif b == "bug":
            size_disp = _norm_sev(it.get("severity")).title() or "—"
        else:
            size_disp = "—"
        dev = it.get("dev_assignee") or ""
        qa = it.get("qa_assignee") or ""
        if not dev and b == "delivery":
            dev = it.get("assignee", "")          # fall back to PBI owner
        devqa = f"{dev or '—'} / {qa or '—'}"
        item = {"id": it.get("key"), "title": it.get("summary", ""), "pts": p,
                "size": size_disp, "devqa": devqa, "team": team, "cat": cat,
                "assignee": it.get("assignee", "") or "—"}
        cat_n[cat] += 1
        cat_pts[cat] += p
        cat_items[cat].append(item)
        total += p
        if b == "delivery":
            delivery += p
        elif b == "suggestion":
            suggestion += p
        elif b == "bug":
            bug_cost += p
        if r["assumed_size"]:
            assumed.append((it.get("key"), it.get("summary", "")))
        if r["no_sev"]:
            no_sev.append((it.get("key"), it.get("summary", "")))
        t_net[team] += p
        if p > 0:
            t_val[team] += p
        elif p < 0:
            t_bug[team] += p
        if b in ("delivery", "suggestion", "bug"):
            t_n[team] += 1
            t_items[team].append(item)
    rows = sorted(((c, cat_n[c], cat_pts[c]) for c in cat_n if c not in _HIDE_CATEGORIES),
                  key=lambda r: -r[2])
    team_rows = sorted(((t, t_n[t], t_val[t], t_bug[t], t_net[t]) for t in t_net),
                       key=lambda r: -r[4])
    return {"total": total, "value": delivery + suggestion, "delivery": delivery,
            "suggestion": suggestion, "bug_cost": bug_cost, "overhead": 0,
            "rows": rows, "assumed": assumed, "no_sev": no_sev, "team_rows": team_rows,
            "cat_items": dict(cat_items), "team_items": dict(t_items)}


def esc(s) -> str:
    return _html.escape(str(s)) if s not in (None, "") else ""


def _parse(ts) -> "datetime | None":
    if not ts:
        return None
    s = str(ts).strip()
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        pass
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def _aware(dt):
    return dt.replace(tzinfo=timezone.utc) if dt and dt.tzinfo is None else dt


# ── snapshot loading ────────────────────────────────────────────────────────
def load_snapshots(release: str):
    d = _REPO / "snapshots" / "jira" / release
    if d.exists():
        files = sorted(p for p in d.glob("*.json") if p.stem != "_index")
        if files:
            return [json.loads(p.read_text(encoding="utf-8")) for p in files]
    raise SystemExit(
        f"No snapshots found for {release} under snapshots/jira/{release}/.\n"
        f"Run the daily release snapshot (daily-dashboard/Snapshot-JiraRelease.py) first.")


# ── per-issue status timeline (from changelog) ──────────────────────────────
def _intervals(rec, now):
    hist = sorted(rec.get("status_history", []) or [], key=lambda h: h.get("at") or "")
    created = _aware(_parse(rec.get("created")))
    end = _aware(_parse(rec.get("resolutiondate"))) or now
    out = []
    if not hist:
        if created:
            out.append((rec.get("status", ""), created, end))
        return out
    prev_t = created
    prev_s = hist[0].get("from") or ""
    for h in hist:
        t = _aware(_parse(h.get("at")))
        if prev_t and t and t >= prev_t:
            out.append((prev_s, prev_t, t))
        prev_s = h.get("to") or prev_s
        prev_t = t
    if prev_t:
        out.append((prev_s, prev_t, end))
    return out


def _time_in_status(rec, now) -> dict:
    agg = defaultdict(float)
    for st, s, e in _intervals(rec, now):
        if s and e and e >= s:
            agg[(st or "").strip()] += (e - s).total_seconds()
    return agg


def _reached(rec, statuses) -> "datetime | None":
    """Earliest transition INTO any of `statuses` (str or list). Falls back to
    created if the item is currently at one of them with no recorded transition."""
    if isinstance(statuses, str):
        statuses = [statuses]
    sset = {s.strip().lower() for s in statuses}
    for h in sorted(rec.get("status_history", []) or [], key=lambda h: h.get("at") or ""):
        if (h.get("to") or "").strip().lower() in sset:
            return _aware(_parse(h.get("at")))
    if (rec.get("status") or "").strip().lower() in sset:
        return _aware(_parse(rec.get("created")))
    return None


def _days(seconds) -> float:
    return round(seconds / 86400.0, 1)


def _norm(s) -> str:
    return re.sub(r"[^a-z0-9]", "", str(s).lower())


def _find_planned(dates: dict, matchers):
    """Match a planned-date field by name, tolerant of spaces/punctuation so it
    works for JIRA display names ('Dev Complete Date') and reference
    names ('customfield_...')."""
    nm = [_norm(m) for m in matchers]
    for k, v in (dates or {}).items():
        kn = _norm(k)
        if any(m in kn for m in nm):
            return k, v
    return None, None


# ── KPI computation ─────────────────────────────────────────────────────────
def compute(snaps):
    latest = snaps[-1]
    now = _aware(_parse(latest.get("captured_at"))) or datetime.now(timezone.utc)
    issues = latest.get("issues", [])
    by_type = defaultdict(list)
    for r in issues:
        by_type[r.get("type", "")].append(r)
    # Source-agnostic taxonomy: "units" = deliverables (Story/PBI/Feature).
    units = [r for r in issues if r.get("type") in UNIT_TYPES]
    bugs = [r for r in issues if r.get("type") in ("Bug", "Defect")]
    subtasks = [r for r in issues if r.get("type") in SUBTASK_TYPES]

    # Dev assignee = the PBI's own (first / primary) assignee.
    # QA assignee  = owner(s) of the PBI's child Test Cases (any) + QA/test child
    # Tasks. Needs the snapshot to capture the PARENT link
    # and child Test Cases (recursive tree).
    _QA_KW = ("[qa", "qa ", "qa)", "q.a", "test", "verif", "regression", "uat",
              "stage verification")
    by_key = {r.get("key"): r for r in issues}
    unit_keys = {r.get("key") for r in units}

    def _pbi_ancestor(rec):
        """Walk the parent chain to the nearest PBI/unit ancestor (handles
        PBI → Task → Test Case nesting)."""
        seen, cur = set(), rec.get("parent")
        while cur and cur not in seen:
            seen.add(cur)
            node = by_key.get(cur)
            if node is None:
                return None
            if node.get("key") in unit_keys or node.get("type") in UNIT_TYPES:
                return node.get("key")
            cur = node.get("parent")
        return None

    qa_by_pbi = defaultdict(list)
    for c in issues:
        typ = c.get("type") or ""
        if typ not in ("Task", "Sub-task", "Test Case"):
            continue
        who = (c.get("assignee") or "").strip()
        if not who:
            continue
        t = (c.get("summary") or "").lower()
        if not (typ == "Test Case" or any(kw in t for kw in _QA_KW)):
            continue
        pid = _pbi_ancestor(c)
        if pid is not None and who not in qa_by_pbi[pid]:
            qa_by_pbi[pid].append(who)
    for r in issues:
        dev = r.get("assignee", "")
        r["dev_assignee"] = dev                              # Dev = first/primary assignee
        # QA = test owners, EXCLUDING the dev (don't repeat the same person)
        qa = [n for n in qa_by_pbi.get(r.get("key"), []) if n and n != dev]
        r["qa_assignee"] = ", ".join(dict.fromkeys(qa))     # de-dup, preserve order

    # Time in Backlog / BA Analysis (over units; sum across status aliases)
    def status_time_summary(records, states):
        states = {s.lower() for s in states}
        rows = []
        for r in records:
            secs = sum(v for st, v in _time_in_status(r, now).items() if st.lower() in states)
            if secs > 0:
                rows.append((r["key"], r.get("summary", ""), _days(secs)))
        rows.sort(key=lambda x: -x[2])
        avg = round(sum(x[2] for x in rows) / len(rows), 1) if rows else 0
        return {"avg_days": avg, "n": len(rows), "rows": rows}

    backlog = status_time_summary(units, BACKLOG_STATES)
    ba = status_time_summary(units, BA_STATES)

    # Date-met per target
    date_met = []
    for t in TARGETS:
        met = missed = no_target = pending = 0
        rows = []
        for r in units:
            pname, pval = _find_planned(r.get("dates", {}), t["match"])
            planned = _aware(_parse(pval))
            reached = _reached(r, t["reached"])
            if not planned:
                no_target += 1
                continue
            if not reached:
                pending += 1
                status = "pending"
            elif reached.date() <= planned.date():
                met += 1
                status = "met"
            else:
                missed += 1
                status = "missed"
            rows.append((r["key"], r.get("summary", ""),
                         planned.date().isoformat(),
                         reached.date().isoformat() if reached else "—", status))
        date_met.append({"label": t["label"], "reached": t["reached"],
                         "met": met, "missed": missed, "pending": pending,
                         "no_target": no_target, "rows": rows})

    # Defects by severity (normalized: '3 - Medium' -> 'Medium')
    sev = Counter((_norm_sev(b.get("severity")).title() or "Unspecified") for b in bugs)

    # Deliverables shipped by size (units that reached a done state)
    shipped_epics = [u for u in units if (u.get("status") or "").strip().lower() in DONE_STATES]
    epic_size = Counter((u.get("sizing") or "—").upper() for u in shipped_epics)

    # Scope creep: PBIs/Epics (units) present in latest but not in the first
    # snapshot. Only deliverable units count — child tasks/bugs are ignored.
    creep = {"supported": len(snaps) >= 2, "added": []}
    if len(snaps) >= 2:
        first_unit_keys = {r["key"] for r in snaps[0].get("issues", [])
                           if r.get("type") in UNIT_TYPES}
        for r in units:
            if r["key"] not in first_unit_keys:
                creep["added"].append((r["key"], r.get("type", ""), r.get("summary", "")))

    # Time split (sub-task time_spent by title bucket)
    split = Counter()
    for s in subtasks:
        title = (s.get("summary") or "").lower()
        hrs = float(s.get("time_spent_h") or 0)
        if hrs <= 0:
            continue
        placed = False
        for bucket, kws in TIME_BUCKETS:
            if any(k in title for k in kws):
                split[bucket] += hrs
                placed = True
                break
        if not placed:
            split["Other"] += hrs

    # Bugs: total + by RCA
    rca = Counter((b.get("root_cause") or "Pending Investigation") for b in bugs)

    return {
        "release": latest.get("release"),
        "source": latest.get("source", "jira"),
        "snapshot_date": latest.get("snapshot_date"),
        "n_snapshots": len(snaps),
        "counts": {k: len(v) for k, v in by_type.items()},
        "score": compute_score(issues),
        "backlog": backlog, "ba": ba, "date_met": date_met,
        "severity": sev, "epic_size": epic_size, "shipped_epics": len(shipped_epics),
        "creep": creep, "split": split, "bugs_total": len(bugs), "rca": rca,
    }


# ── rendering ───────────────────────────────────────────────────────────────
def _kv_table(counter, h1, h2, order=None):
    items = ([(k, counter.get(k, 0)) for k in order if counter.get(k, 0)]
             if order else counter.most_common())
    if not items:
        return "<p class='muted'>— none —</p>"
    body = "".join(f"<tr><td>{esc(k)}</td><td class='r'>{v}</td></tr>" for k, v in items)
    return (f"<table><thead><tr><th>{esc(h1)}</th><th class='r'>{esc(h2)}</th></tr></thead>"
            f"<tbody>{body}</tbody></table>")


def _date_met_card(d):
    total = d["met"] + d["missed"] + d["pending"]
    pct = round(d["met"] / total * 100) if total else 0
    rows = "".join(
        f"<tr class='dm-{st}'><td>{esc(k)}</td><td>{esc(title)[:60]}</td>"
        f"<td>{esc(pl)}</td><td>{esc(rc)}</td><td>{esc(st)}</td></tr>"
        for k, title, pl, rc, st in d["rows"])
    return (
        f"<div class='section'><h3>{esc(d['label'])} "
        f"<span class='muted'>(target date vs status “{esc(d['reached'])}”)</span></h3>"
        f"<div class='cards'>"
        f"<div class='card ok'><div class='v'>{d['met']}</div><div class='l'>Met</div></div>"
        f"<div class='card bad'><div class='v'>{d['missed']}</div><div class='l'>Missed</div></div>"
        f"<div class='card'><div class='v'>{d['pending']}</div><div class='l'>Pending</div></div>"
        f"<div class='card'><div class='v'>{pct}%</div><div class='l'>On-time</div></div>"
        f"<div class='card'><div class='v'>{d['no_target']}</div><div class='l'>No target date</div></div>"
        f"</div>"
        + (f"<table><thead><tr><th>Item</th><th>Title</th><th>Target</th>"
           f"<th>Reached</th><th>Status</th></tr></thead><tbody>{rows}</tbody></table>"
           if rows else "<p class='muted'>No items with a target date.</p>")
        + "</div>")


def _scoring_methodology() -> str:
    """Visible rubric so readers can see exactly how the score is computed.
    Built from the live constants (SIZE_POINTS / SEV_POINTS / SUGGESTION_POINTS)
    so it can never drift from the actual scoring."""
    size_rows = "".join(
        f"<tr><td>[{s.upper()}]</td><td class='r'>+{SIZE_POINTS[s]}</td></tr>"
        for s in ["xs", "s", "m", "l"] if s in SIZE_POINTS)
    sev_rows = "".join(
        f"<tr><td>{sev.title()}</td><td class='r'>{SEV_POINTS[sev]}</td></tr>"
        for sev in ["critical", "high", "medium", "low"] if sev in SEV_POINTS)
    return (
        "<details style='margin-top:12px'>"
        "<summary style='cursor:pointer;font-weight:600;font-size:13px'>ℹ️ How the score is calculated</summary>"
        "<div style='margin-top:8px'>"
        "<p class='muted' style='font-style:normal'>Only delivered value and introduced bugs count. "
        "The primary title prefix sets the category; domain modifiers (RCM | · INTAKE | · [INTEGRATION]) "
        "are for routing and do not change the score.</p>"
        "<div style='display:flex;flex-wrap:wrap;gap:18px'>"
        # Delivered
        "<div style='flex:1 1 220px'>"
        "<h3>➕ Delivered work</h3>"
        "<p class='muted' style='font-style:normal'>Prefixes: <strong>[PRODUCT] · [TECHNICAL] · "
        "[EXPEDITE] · [INTEGRATION]</strong>, scored by size:</p>"
        "<table><thead><tr><th>Size</th><th class='r'>Points</th></tr></thead><tbody>"
        + size_rows +
        "</tbody></table>"
        "<p class='muted'>L is the max — bigger items must be split. "
        f"Unsized positive items are assumed <strong>M (+{SIZE_POINTS.get('m', 15)})</strong>.</p>"
        f"<p class='muted' style='font-style:normal'><strong>[SUGGESTION]</strong> — flat "
        f"<strong>+{SUGGESTION_POINTS}</strong> (no size needed).</p>"
        "</div>"
        # Bugs
        "<div style='flex:1 1 220px'>"
        "<h3>➖ Bugs (by severity)</h3>"
        "<table><thead><tr><th>Severity</th><th class='r'>Points</th></tr></thead><tbody>"
        + sev_rows +
        "</tbody></table>"
        "<p class='muted' style='font-style:normal'>Bugs count regardless of origin (QA, product, "
        "or production). Resolved as <strong>Not a Bug / Duplicate / Cannot Reproduce → 0</strong> "
        "(excluded). Bugs with no severity set score 0 (flagged as data quality).</p>"
        "</div>"
        # Zero + formula
        "<div style='flex:1 1 220px'>"
        "<h3>0 Zero-score</h3>"
        "<p class='muted' style='font-style:normal'><strong>[INTERNAL]</strong> — refinement, "
        "sprint ceremonies, release &amp; deployment. Raw hours and task count are not scored; "
        "this is a team &amp; release metric, not individual performance.</p>"
        "<h3>Σ Formula</h3>"
        "<p class='muted' style='font-style:normal'>Net = Σ(delivered × size points) "
        "+ Σ(suggestions × 3) − Σ(bugs × severity points).</p>"
        "</div>"
        "</div></div></details>")


def _detail_rows(items, source, show_category) -> str:
    rows = []
    for it in sorted(items, key=lambda x: -abs(x["pts"])):
        cat_cell = f"<td>{esc(it['cat'])}</td>" if show_category else ""
        rows.append(
            f"<tr><td>{_item_link(source, it['id'])}</td>"
            f"<td>{esc(it['title'])[:80]}</td>"
            f"<td>{esc(it['size'])}</td>"
            f"{cat_cell}"
            f"<td>{esc(it['devqa'])}</td>"
            f"<td class='r'>{it['pts']:+d}</td></tr>")
    return "".join(rows)


def _detail_table(items, source, group_by_team=False, show_category=False) -> str:
    """Contributing-tickets table shown when a category/team row is expanded.
    Columns: Item · Title · Size · [Category] · Dev / QA · Pts.
    When group_by_team, tickets are grouped under a per-team sub-header."""
    ncols = 5 + (1 if show_category else 0)
    head = ("<tr><th>Item</th><th>Title</th><th>Size</th>"
            + ("<th>Category</th>" if show_category else "")
            + "<th>Dev / QA</th><th class='r'>Pts</th></tr>")
    if not group_by_team:
        return (f"<table class='inner'><thead>{head}</thead><tbody>"
                + _detail_rows(items, source, show_category) + "</tbody></table>")
    # group by team, teams ordered by net points desc
    by_team = defaultdict(list)
    for it in items:
        by_team[it["team"]].append(it)
    order = sorted(by_team, key=lambda t: -sum(i["pts"] for i in by_team[t]))
    body = ""
    for t in order:
        grp = by_team[t]
        net = sum(i["pts"] for i in grp)
        body += (f"<tr class='grp'><td colspan='{ncols}'><strong>{esc(t)}</strong> "
                 f"· {len(grp)} item(s) · net {net:+d}</td></tr>"
                 + _detail_rows(grp, source, show_category))
    return f"<table class='inner'><thead>{head}</thead><tbody>{body}</tbody></table>"


def _score_categories(score, source) -> str:
    """Category breakdown where each row expands to its contributing tickets."""
    cat_items = score.get("cat_items", {})
    body = ""
    for c, n, p in score["rows"]:
        uid = _uid()
        body += (f"<tr class='exp' onclick=\"tg('{uid}')\"><td>▸ {esc(c)}</td>"
                 f"<td class='r'>{n}</td><td class='r'>{p:+d}</td></tr>"
                 f"<tr id='{uid}' class='det' style='display:none'><td colspan='3'>"
                 f"{_detail_table(cat_items.get(c, []), source, group_by_team=True)}</td></tr>")
    return ("<table><thead><tr><th>Category</th><th class='r'>Items</th><th class='r'>Points</th>"
            f"</tr></thead><tbody>{body}</tbody></table>"
            "<p class='muted'>Click a category to see the tickets contributing to it.</p>")


def _score_by_team(score, source) -> str:
    rows = score.get("team_rows") or []
    if not rows:
        return ""
    team_items = score.get("team_items", {})
    body = ""
    for t, n, val, bug, net in rows:
        uid = _uid()
        body += (f"<tr class='exp' onclick=\"tg('{uid}')\"><td>▸ {esc(t)}</td>"
                 f"<td class='r'>{n}</td><td class='r'>+{val}</td>"
                 f"<td class='r'>{bug}</td><td class='r'><strong>{net:+d}</strong></td></tr>"
                 f"<tr id='{uid}' class='det' style='display:none'><td colspan='5'>"
                 f"{_detail_table(team_items.get(t, []), source, show_category=True)}</td></tr>")
    return (
        "<h3 style='margin-top:12px'>Score by team</h3>"
        "<table><thead><tr><th>Team</th><th class='r'>Scored items</th>"
        "<th class='r'>Value</th><th class='r'>Bug cost</th><th class='r'>Net</th>"
        "</tr></thead><tbody>" + body + "</tbody></table>"
        "<p class='muted'>Team from assignee → roster; “Other” = leads/BAs/unassigned "
        "(off-squad owners, or unassigned). Click a team to see its tickets. "
        "A release metric, not individual performance.</p>")


def _data_quality(score) -> str:
    assumed = score.get("assumed") or []
    no_sev = score.get("no_sev") or []
    if not assumed and not no_sev:
        return ""
    parts = ["<h3 style='margin-top:12px'>⚠ Data quality (affects score accuracy)</h3><ul style='margin:4px 0 0 18px;font-size:12.5px'>"]
    if assumed:
        parts.append(f"<li><strong>{len(assumed)}</strong> positive-prefix item(s) had "
                     f"<strong>no size set</strong> → assumed <strong>M (+15)</strong>. "
                     f"Set XS/S/M/L for an accurate score.</li>")
    if no_sev:
        parts.append(f"<li><strong>{len(no_sev)}</strong> bug(s) had <strong>no severity</strong> "
                     f"→ scored 0. Set severity during triage.</li>")
    parts.append("</ul>")
    return "".join(parts)


def _trend_section(k) -> str:
    """Executive-retro trend across sprints (e.g. 82→83→84). Config-driven via
    exec_trend_config.TREND; renders whatever sprints are supplied and flags gaps."""
    trend = _EXEC_TREND
    if not trend:
        return ""
    sprints = sorted(trend.keys(), key=lambda s: int(re.sub(r"\D", "", str(s)) or 0))
    # union of metric keys across sprints (excluding free-text 'talking_points')
    metric_keys = []
    for s in sprints:
        for mk in (trend[s].get("metrics") or {}):
            if mk not in metric_keys:
                metric_keys.append(mk)
    head = "".join(f"<th class='r'>{esc(s)}</th>" for s in sprints)
    body = ""
    for mk in metric_keys:
        cells = "".join(f"<td class='r'>{esc(trend[s].get('metrics', {}).get(mk, '—'))}</td>"
                        for s in sprints)
        body += f"<tr><td>{esc(mk)}</td>{cells}</tr>"
    tbl = (f"<table><thead><tr><th>Metric</th>{head}</tr></thead><tbody>{body}</tbody></table>"
           if metric_keys else "<p class='muted'>No metrics supplied yet.</p>")
    # talking points per sprint
    tps = []
    for s in sprints:
        pts = trend[s].get("talking_points") or []
        if pts:
            tps.append(f"<h3>{esc(s)}</h3><ul style='margin:4px 0 8px 18px;font-size:12.5px'>"
                       + "".join(f"<li>{esc(p)}</li>" for p in pts) + "</ul>")
    missing = trend_missing_note(sprints)
    return (f"<div class='section'><h2>📈 Executive-retro trend</h2>{tbl}"
            + "".join(tps) + missing + "</div>")


def trend_missing_note(sprints) -> str:
    want = {"Sprint 82", "Sprint 83", "Sprint 84"}
    have = set(sprints)
    gap = sorted(want - have)
    if not gap:
        return ""
    return (f"<p class='muted'>Missing {', '.join(gap)} — generate those executive retros "
            f"(or add them to exec_trend_config.TREND) to complete the trend.</p>")


def render(k) -> str:
    counts = " · ".join(f"{v} {kk}" for kk, v in sorted(k["counts"].items()))
    split_total = sum(k["split"].values()) or 1
    split_rows = "".join(
        f"<tr><td>{esc(b)}</td><td class='r'>{round(h,1)}</td>"
        f"<td class='r'>{round(h/split_total*100)}%</td></tr>"
        for b, h in k["split"].most_common())

    def status_block(title, s):
        rows = "".join(f"<tr><td>{esc(kk)}</td><td>{esc(t)[:60]}</td><td class='r'>{d}</td></tr>"
                       for kk, t, d in s["rows"][:25])
        return (f"<div class='section'><h3>{esc(title)}</h3>"
                f"<p>Average <strong>{s['avg_days']} days</strong> across {s['n']} item(s).</p>"
                + (f"<table><thead><tr><th>Item</th><th>Title</th><th class='r'>Days</th></tr></thead>"
                   f"<tbody>{rows}</tbody></table>" if rows else "<p class='muted'>— none —</p>")
                + "</div>")

    creep = k["creep"]
    if not creep["supported"]:
        creep_html = ("<p class='muted'>Needs ≥2 daily snapshots to detect scope creep "
                      "(only one captured so far).</p>")
    elif not creep["added"]:
        creep_html = "<p class='muted'>No items added since the first snapshot.</p>"
    else:
        creep_html = (f"<p><strong>{len(creep['added'])}</strong> item(s) added after the "
                      f"first snapshot:</p><table><thead><tr><th>Item</th><th>Type</th>"
                      f"<th>Title</th></tr></thead><tbody>" +
                      "".join(f"<tr><td>{esc(a)}</td><td>{esc(b)}</td><td>{esc(c)[:70]}</td></tr>"
                              for a, b, c in creep["added"]) + "</tbody></table>")

    css = """
    body{font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;color:#1e293b;background:#f1f5f9;margin:0;padding:28px}
    .wrap{max-width:1100px;margin:0 auto}
    h1{font-size:22px;margin:0 0 2px;color:#0f172a}.sub{color:#64748b;font-size:13px;margin-bottom:18px}
    .section{background:#fff;border:1px solid #e2e8f0;border-radius:12px;padding:14px 18px;margin-bottom:14px}
    .section h2{font-size:15px;margin:0 0 10px}.section h3{font-size:14px;margin:2px 0 8px}
    table{width:100%;border-collapse:collapse;font-size:12.5px;margin-top:6px}
    th{background:#0f172a;color:#fff;text-align:left;font-size:10.5px;text-transform:uppercase;letter-spacing:.03em;padding:6px 8px}
    td{padding:5px 8px;border-top:1px solid #eef2f7}.r{text-align:right}
    .muted{color:#94a3b8;font-style:italic;font-size:12px}
    .cards{display:flex;flex-wrap:wrap;gap:8px;margin:6px 0}
    .card{flex:1 1 80px;background:#f8fafc;border:1px solid #e2e8f0;border-radius:9px;padding:9px;text-align:center}
    .card .v{font-size:18px;font-weight:700}.card .l{font-size:10px;color:#64748b;text-transform:uppercase}
    .card.ok .v{color:#16a34a}.card.bad .v{color:#dc2626}
    tr.dm-met td{background:#f0fdf4}tr.dm-missed td{background:#fef2f2}tr.dm-pending td{background:#fffbeb}
    tr.exp{cursor:pointer}tr.exp:hover td{background:#eef2f7}
    table.inner{margin:0}table.inner th{background:#334155;padding:4px 7px}
    table.inner tr.grp td{background:#e2e8f0;color:#0f172a;font-weight:600;font-size:11.5px}
    tr.det>td{background:#f8fafc;padding:0 8px 8px}
    """
    return (
        f"<!DOCTYPE html><html><head><meta charset='utf-8'>"
        f"<title>{esc(k['release'])} — Monthly Retro</title><style>{css}</style></head><body><div class='wrap'>"
        f"<h1>{esc(k['release'])} — Monthly Release Retro</h1>"
        f"<div class='sub'>As of snapshot {esc(k['snapshot_date'])} · {k['n_snapshots']} daily "
        f"snapshot(s) · {esc(counts)}</div>"
        f"<div class='section'><h2>🏆 Release Score</h2>"
        f"<div class='cards'>"
        f"<div class='card ok'><div class='v'>+{k['score']['delivery']}</div><div class='l'>Delivery</div></div>"
        f"<div class='card ok'><div class='v'>+{k['score']['suggestion']}</div><div class='l'>Suggestions</div></div>"
        f"<div class='card bad'><div class='v'>{k['score']['bug_cost']}</div><div class='l'>Bug deductions</div></div>"
        f"<div class='card'><div class='v'>{k['score']['overhead']}</div><div class='l'>Internal overhead</div></div>"
        f"<div class='card'><div class='v'>{k['score']['total']}</div><div class='l'>Net score</div></div>"
        f"</div>"
        + _score_categories(k['score'], k['source'])
        + "<p class='muted'>Net = Σ delivered (size points) + Σ suggestions − Σ bug severity. "
          "Internal/overhead = 0 (hidden); Not-a-Bug / Duplicate / CNR excluded.</p>"
        + _score_by_team(k['score'], k['source']) + _data_quality(k['score'])
        + _scoring_methodology() + "</div>"
        + _trend_section(k)
        + f"<div class='section'><h2>🐞 Quality</h2>"
        f"<h3>Defects by severity ({k['bugs_total']} total)</h3>{_kv_table(k['severity'],'Severity','Bugs')}"
        f"<h3>Bugs by root cause</h3>{_kv_table(k['rca'],'Root cause','Bugs')}</div>"
        f"<div class='section'><h2>📦 Deliverables shipped by size ({k['shipped_epics']} shipped)</h2>"
        f"{_kv_table(k['epic_size'],'Size','Epics',order=SIZE_ORDER)}</div>"
        f"<div class='section'><h2>🧮 Time split (sub-task hours)</h2>"
        + (f"<table><thead><tr><th>Phase</th><th class='r'>Hours</th><th class='r'>Share</th></tr></thead>"
           f"<tbody>{split_rows}</tbody></table>" if split_rows else "<p class='muted'>No logged sub-task time yet.</p>")
        + "<p class='muted'>Heuristic: sub-tasks bucketed by title keywords; tune TIME_BUCKETS as needed.</p></div>"
        + "<script>function tg(id){var e=document.getElementById(id);"
          "e.style.display=(e.style.display==='none'?'':'none');}</script>"
        + "</div></body></html>"
    )


def main() -> int:
    release = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_RELEASE
    snaps = load_snapshots(release)
    k = compute(snaps)
    out = _REPO / "reports" / f"{release}_Monthly_Retro.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(k), encoding="utf-8")
    print(f"✅ Wrote {out}  (from {k['n_snapshots']} snapshot(s), {sum(k['counts'].values())} issues)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
