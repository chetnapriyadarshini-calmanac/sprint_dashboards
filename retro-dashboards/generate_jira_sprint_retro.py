"""
generate_jira_sprint_retro.py
=============================
Build the JIRA sprint retro dashboard — ONE self-contained HTML per team — from
a FROZEN sprint snapshot (Snapshot-JiraSprint.py). Runs fully OFFLINE, so it
can be generated / regenerated any time after the boards have rolled to the next
sprint, from the exact point-in-time state captured before the move.

Why purpose-built:
    JIRA goals live in the "Goal for the Sprint" FIELD (captured per issue as
    `goal`). The goal / commitment sections here read that field; the rest is
    computed from the canonical RetroData produced by jira_retro_fetch.

Sections per team:
    Summary cards · Sprint Goals · Deliverables (Epics) · Commitment vs Delivery ·
    Estimation Accuracy (per person) · Time Split (Dev/QA/Analysis) ·
    Bugs by Root Cause · Bugs Open at Sprint End · Bugs per Epic

Output: reports/<Team>_JIRA_Sprint_Retro.html

Usage:
    # newest snapshot under snapshots/jira_sprint/Sprint-1/:
    python generate_jira_sprint_retro.py

    python generate_jira_sprint_retro.py --wave Sprint-1
    python generate_jira_sprint_retro.py --snapshot path/to/2026-07-04.json
    python generate_jira_sprint_retro.py --team Knackers
"""

from __future__ import annotations

import argparse
import html as _html
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

import pandas as pd

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))

import jira_retro_fetch as JRF          # noqa: E402  -- offline RetroData loader
import jira_fetch as JF                 # noqa: E402  -- authoritative goal_met() ladder
import retro_config as CFG              # noqa: E402
import retro_layout as RD               # noqa: E402  -- shared PAGE_HEAD/FOOT/CSS

_REPO = _HERE.parent
_SNAP_ROOT = _REPO / "snapshots" / "jira_sprint"
_REPORTS = _REPO / "reports"

# A PBI/Story is "done" once it reaches ST To Do or later (project convention:
# goal done-states = ST To Do, ST In Progress, Ready For LIVE, LIVE, Done).
_DONE = {"st to do", "st in progress", "ready for live", "live",
         "live in progress", "integrated", "done", "closed", "resolved", "released"}
_OPEN_BUG_DONE = {"done", "closed", "resolved", "ready for live", "live", "integrated"}


def _jira_browse_base() -> str:
    """<site>/browse/ for linking JIRA issues (e.g. .../browse/MPM-368)."""
    try:
        import jira_auth
        return jira_auth._load_site().rstrip("/") + "/browse/"
    except Exception:
        return "https://motivity.atlassian.net/browse/"


_JIRA_BROWSE = _jira_browse_base()


def _jlink(key: str) -> str:
    """Linked JIRA issue key."""
    return (f'<a href="{esc(_JIRA_BROWSE)}{esc(key)}" target="_blank" '
            f'style="color:#2563eb;text-decoration:none;font-weight:600">{esc(key)}</a>')


def esc(s) -> str:
    return _html.escape(str(s)) if s not in (None, "") else ""


def _done(state: str) -> bool:
    return str(state or "").strip().lower() in _DONE


def _num(series) -> float:
    return float(pd.to_numeric(series, errors="coerce").fillna(0).sum()) if len(series) else 0.0


# ── snapshot access ──────────────────────────────────────────────────────────
def _latest_snapshot(wave: str | None) -> Path:
    if wave:
        d = _SNAP_ROOT / wave
        cands = sorted(p for p in d.glob("*.json") if p.stem != "_index")
        if not cands:
            raise FileNotFoundError(f"No snapshot JSON under {d}")
        return cands[-1]
    # newest across all waves
    cands = sorted((p for p in _SNAP_ROOT.glob("*/*.json") if p.stem != "_index"),
                   key=lambda p: p.stat().st_mtime)
    if not cands:
        raise FileNotFoundError(f"No sprint snapshots found under {_SNAP_ROOT}")
    return cands[-1]


def _teams_in(snap: dict) -> list[str]:
    teams = [m.get("team") for m in snap.get("sprints", []) if m.get("team")]
    if not teams:  # fall back to per-issue derived team
        teams = [r.get("team_from_sprint") for r in snap.get("issues", [])
                 if r.get("team_from_sprint")]
    seen, out = set(), []
    for t in teams:
        if t not in seen:
            seen.add(t); out.append(t)
    return out


def _team_sprint_names(snap: dict, team: str) -> set[str]:
    tl = team.strip().lower()
    names = {m["name"] for m in snap.get("sprints", [])
             if (m.get("team") or "").strip().lower() == tl}
    if not names:
        names = {m["name"] for m in snap.get("sprints", []) if tl in (m.get("name") or "").lower()}
    return names


def _team_stories(snap: dict, sprint_names: set[str]) -> list[dict]:
    out = []
    for r in snap.get("issues", []):
        if (r.get("type") or "").lower() != "story" or not r.get("in_sprint"):
            continue
        names = set(r.get("sprints") or ([r.get("sprint")] if r.get("sprint") else []))
        if names & sprint_names:
            out.append(r)
    return out


def _sprint_report(snap: dict, sprint_names: set[str]) -> dict:
    """Merge GreenHopper reports across the team's sprint(s), if captured."""
    merged = {"completed": [], "not_completed": [], "removed": []}
    for m in snap.get("sprints", []):
        if m.get("name") in sprint_names:
            rep = m.get("report") or {}
            for k in merged:
                merged[k] += rep.get(k, []) or []
    return merged if any(merged.values()) else {}


# ── sections ─────────────────────────────────────────────────────────────────
def _summary_cards(rd, stories: list[dict]) -> str:
    pbis = rd.pbis_df
    n = len(pbis)
    done = int(pbis["State"].map(_done).sum()) if not pbis.empty else 0
    est = _num(rd.tasks_df["Original Estimate"]) if not rd.tasks_df.empty else 0.0
    spent = _num(rd.tasks_df["Completed Work"]) if not rd.tasks_df.empty else 0.0
    var = round((spent - est) / est * 100) if est else 0
    n_stories = len(stories)
    stories_done = sum(1 for s in stories if _done(s.get("status")))

    def card(val, lbl, color=""):
        st = f' style="color:{color}"' if color else ""
        return f'<div class="card"><div class="val"{st}>{val}</div><div class="lbl">{esc(lbl)}</div></div>'

    return (
        '<div class="section"><h2>📌 Summary</h2><div class="grid">'
        + card(n, "Epics (PBIs)")
        + card(f"{done} ({round(done / n * 100) if n else 0}%)", "Epics done", "#16a34a")
        + card(f"{stories_done}/{n_stories}", "Stories done")
        + card(len(rd.bugs_df), "Bugs", "#dc2626")
        + card(round(est, 1), "Est h")
        + card(round(spent, 1), "Spent h")
        + card(f"{'+' if var >= 0 else ''}{var}%", "Est→Spent", "#b45309" if var > 0 else "#16a34a")
        + '</div></div>')


def _goals_section(stories: list[dict]) -> str:
    """A story meets its goal when its status is AT or PAST the goal's target
    stage. Uses jira_fetch.goal_met() (cumulative GOAL_RANK/STATUS_RANK) — the
    exact rule the daily sprint dashboard applies, so numbers reconcile."""
    goals: dict[str, list[dict]] = defaultdict(list)
    for s in stories:
        g = (s.get("goal") or "").strip()
        if g:
            goals[g].append(s)
    if not goals:
        return ('<div class="section"><h2>🎯 Sprint Goals</h2>'
                '<p>No stories carry a "Goal for the Sprint" value.</p></div>')
    # order goals by their ladder rank (early stages first)
    order = sorted(goals, key=lambda g: (JF.GOAL_RANK.get(g, 99), g))
    rows = []
    for g in order:
        items = goals[g]
        total = len(items)
        mapped = g in JF.GOAL_RANK
        if not mapped:
            rows.append(
                f'<tr><td>{esc(g)}</td><td class="r">—/{total}</td><td class="r">—</td>'
                f'<td style="color:#b45309;font-weight:600">⚠ Unmapped goal</td></tr>')
            continue
        met = sum(1 for s in items if JF.goal_met(g, s.get("status", "")))
        pct = round(met / total * 100) if total else 0
        status = ("✅ Achieved" if met == total else
                  "🟡 Partial" if met else "🔴 Not met")
        color = "#16a34a" if met == total else "#b45309" if met else "#dc2626"
        # list the stories that missed, with their current state
        missed = [s for s in items if not JF.goal_met(g, s.get("status", ""))]
        miss_txt = ""
        if missed:
            miss_txt = ("<div style='font-size:11px;color:#64748b;margin-top:3px'>Behind: "
                        + ", ".join(f"{esc(s.get('key'))} ({esc(s.get('status',''))})"
                                    for s in missed[:12])
                        + ("…" if len(missed) > 12 else "") + "</div>")
        rows.append(
            f'<tr><td>{esc(g)}<br><span style="font-size:11px;color:#94a3b8">target: reach '
            f'&ldquo;{esc(g)}&rdquo; or later</span>{miss_txt}</td>'
            f'<td class="r">{met}/{total}</td>'
            f'<td class="r">{pct}%</td>'
            f'<td style="color:{color};font-weight:600">{status}</td></tr>')
    n_goals = sum(1 for g in order if g in JF.GOAL_RANK)
    n_hit = sum(1 for g in order if g in JF.GOAL_RANK
                and all(JF.goal_met(g, s.get("status", "")) for s in goals[g]))
    return (
        '<div class="section"><h2>🎯 Sprint Goals (target state reached — matches daily dashboard)</h2>'
        f'<div class="grid"><div class="card"><div class="val">{n_hit}/{n_goals}</div>'
        '<div class="lbl">Goals fully met</div></div></div>'
        '<table><thead><tr><th>Goal (target state)</th><th class="r">Stories met</th>'
        '<th class="r">%</th><th>Status</th></tr></thead><tbody>'
        + "".join(rows) + '</tbody></table></div>')


def _deliverables_section(rd) -> str:
    pbis = rd.pbis_df
    if pbis.empty:
        return '<div class="section"><h2>📦 Deliverables (Epics)</h2><p>No Epics in scope.</p></div>'
    rows = []
    for _, p in pbis.sort_values("State").iterrows():
        d = _done(p.get("State"))
        mark = "✅" if d else "⏳"
        rid = p.get("ID")
        link = _jlink(rid)
        rows.append(
            f'<tr><td style="white-space:nowrap">{mark} {link}</td>'
            f'<td>{esc(p.get("Title", ""))}</td>'
            f'<td style="white-space:nowrap">{esc(p.get("State", ""))}</td>'
            f'<td style="white-space:nowrap">{esc(p.get("Assigned To", "")) or "&mdash;"}</td></tr>')
    return (
        '<div class="section"><h2>📦 Deliverables (Epics — state rolled up from in-sprint Stories)</h2>'
        '<table><thead><tr><th>Epic</th><th>Title</th><th>State</th><th>Owner</th></tr></thead><tbody>'
        + "".join(rows) + '</tbody></table></div>')


def _commitment_section(stories: list[dict], report: dict) -> str:
    committed = len(stories)
    delivered = sum(1 for s in stories if _done(s.get("status")))
    body = (
        '<div class="grid">'
        f'<div class="card"><div class="val">{committed}</div><div class="lbl">Stories committed</div></div>'
        f'<div class="card"><div class="val" style="color:#16a34a">{delivered}</div>'
        f'<div class="lbl">Delivered</div></div>'
        f'<div class="card"><div class="val" style="color:#dc2626">{committed - delivered}</div>'
        f'<div class="lbl">Carried / open</div></div></div>')
    note = ""
    if report:
        note = (f'<p style="font-size:12px;color:#64748b;margin-top:8px">Board sprint report: '
                f'{len(report.get("completed", []))} completed, '
                f'{len(report.get("not_completed", []))} not completed, '
                f'{len(report.get("removed", []))} removed.</p>')
    # list not-delivered stories
    open_rows = []
    for s in stories:
        if not _done(s.get("status")):
            open_rows.append(f'<tr><td style="white-space:nowrap">{_jlink(s.get("key"))}</td>'
                             f'<td>{esc(s.get("summary",""))}</td>'
                             f'<td style="white-space:nowrap">{esc(s.get("status",""))}</td>'
                             f'<td>{esc(s.get("assignee","")) or "&mdash;"}</td></tr>')
    tbl = ""
    if open_rows:
        tbl = ('<table style="margin-top:10px"><thead><tr><th>Story</th><th>Title</th>'
               '<th>State</th><th>Owner</th></tr></thead><tbody>' + "".join(open_rows) + '</tbody></table>')
    return f'<div class="section"><h2>📈 Commitment vs Delivery</h2>{body}{note}{tbl}</div>'


def _estimation_section(rd) -> str:
    if rd.tasks_df.empty:
        return ('<div class="section"><h2>⏱ Estimation Accuracy (per person)</h2>'
                '<p>No sub-tasks with effort.</p></div>')
    df = rd.tasks_df.copy()
    df["_e"] = pd.to_numeric(df["Original Estimate"], errors="coerce").fillna(0)
    df["_s"] = pd.to_numeric(df["Completed Work"], errors="coerce").fillna(0)
    # attribute by effective assignee when the sub-task owner is off-roster
    who = df.get("effective_assignee")
    base = df["Assigned To"].fillna("").replace("", "(unassigned)")
    if who is not None:
        base = who.where(who.astype(bool), base)
    df["_who"] = base.replace("", "(unassigned)")
    rows = [(str(n), round(g["_e"].sum(), 1), round(g["_s"].sum(), 1), len(g))
            for n, g in df.groupby("_who")]
    rows.sort(key=lambda r: -r[2])
    body = []
    for n, e, s, nt in rows:
        var = round((s - e) / e * 100) if e else 0
        body.append(f'<tr><td>{esc(n)}</td><td class="r">{e}</td><td class="r">{s}</td>'
                    f'<td class="r">{"+" if var >= 0 else ""}{var}%</td><td class="r">{nt}</td></tr>')
    return (
        '<div class="section"><h2>⏱ Estimation Accuracy (per person)</h2>'
        '<table><thead><tr><th>Member</th><th class="r">Est h</th><th class="r">Spent h</th>'
        '<th class="r">Δ%</th><th class="r">Tasks</th></tr></thead><tbody>'
        + "".join(body) + '</tbody></table></div>')


_SPLIT_BUCKETS = [
    ("Analysis", ["analysis", "ba ", "refine", "grooming", "spec"]),
    ("Development", ["[dev", "dev ", "develop", "implement", "coding", "code"]),
    ("Test-case creation", ["test case", "testcase", "write test", "test design"]),
    ("QA / Testing", ["[qa", "qa ", "test", "verify", "regression"]),
]


def _time_split_section(rd) -> str:
    if rd.tasks_df.empty:
        return ('<div class="section"><h2>🔀 Time Split</h2><p>No sub-task effort captured.</p></div>')
    split = Counter()
    for _, t in rd.tasks_df.iterrows():
        title = str(t.get("Title", "")).lower()
        hrs = float(pd.to_numeric(pd.Series([t.get("Completed Work", 0)]),
                                  errors="coerce").fillna(0).iloc[0])
        if hrs <= 0:
            continue
        placed = False
        for bucket, kws in _SPLIT_BUCKETS:
            if any(k in title for k in kws):
                split[bucket] += hrs
                placed = True
                break
        if not placed:
            split["Other"] += hrs
    if not split:
        return ('<div class="section"><h2>🔀 Time Split</h2><p>No logged hours on sub-tasks.</p></div>')
    total = sum(split.values())
    order = [b for b, _ in _SPLIT_BUCKETS] + ["Other"]
    rows = []
    for b in order:
        if split.get(b):
            v = round(split[b], 1)
            rows.append(f'<tr><td>{esc(b)}</td><td class="r">{v}</td>'
                        f'<td class="r">{round(v / total * 100)}%</td></tr>')
    return (
        '<div class="section"><h2>🔀 Time Split (by sub-task type, hours)</h2>'
        '<table><thead><tr><th>Category</th><th class="r">Hours</th><th class="r">Share</th>'
        '</tr></thead><tbody>' + "".join(rows) + '</tbody></table></div>')


def _bugs_rootcause_section(rd) -> str:
    if rd.bugs_df.empty or "root_cause_type" not in rd.bugs_df.columns:
        return ('<div class="section"><h2>🐛 Bugs by Root Cause</h2><p>No bugs in scope.</p></div>')
    rc = Counter(str(x or "—") for x in rd.bugs_df["root_cause_type"]).most_common()
    body = "".join(f'<tr><td>{esc(k)}</td><td class="r">{v}</td></tr>' for k, v in rc)
    return (
        '<div class="section"><h2>🐛 Bugs by Root Cause</h2>'
        '<table><thead><tr><th>Root Cause</th><th class="r">Bugs</th></tr></thead>'
        '<tbody>' + body + '</tbody></table></div>')


def _bugs_open_section(rd) -> str:
    if rd.bugs_df.empty:
        return ('<div class="section"><h2>🔓 Bugs Open at Sprint End</h2><p>No bugs in scope.</p></div>')
    df = rd.bugs_df
    open_mask = ~df["State"].map(lambda s: str(s).strip().lower() in _OPEN_BUG_DONE)
    op = df[open_mask]
    if op.empty:
        return ('<div class="section"><h2>🔓 Bugs Open at Sprint End</h2>'
                '<p>None — all bugs resolved by sprint end. 🎉</p></div>')
    rows = "".join(
        f'<tr><td style="white-space:nowrap">{_jlink(b["ID"])}</td><td>{esc(b.get("Title",""))}</td>'
        f'<td style="white-space:nowrap">{esc(b.get("State",""))}</td>'
        f'<td>{esc(b.get("Assigned To","")) or "&mdash;"}</td></tr>'
        for _, b in op.sort_values("State").iterrows())
    return (
        f'<div class="section"><h2>🔓 Bugs Open at Sprint End ({len(op)})</h2>'
        '<table><thead><tr><th>Bug</th><th>Title</th><th>State</th><th>Owner</th></tr></thead>'
        '<tbody>' + rows + '</tbody></table></div>')


def _bugs_per_epic_section(rd) -> str:
    if rd.bugs_df.empty or "parent_pbi_id" not in rd.bugs_df.columns:
        return ('<div class="section"><h2>🧩 Bugs per Epic</h2><p>No bugs in scope.</p></div>')
    cnt = Counter(str(x or "(unlinked)") for x in rd.bugs_df["parent_pbi_id"]).most_common()
    title_by_id = {p["ID"]: p["Title"] for _, p in rd.pbis_df.iterrows()} if not rd.pbis_df.empty else {}
    rows = "".join(
        f'<tr><td style="white-space:nowrap">{_jlink(k) if k != "(unlinked)" else esc(k)}</td>'
        f'<td>{esc(title_by_id.get(k, ""))}</td><td class="r">{v}</td></tr>'
        for k, v in cnt)
    return (
        '<div class="section"><h2>🧩 Bugs per Epic</h2>'
        '<table><thead><tr><th>Epic</th><th>Title</th><th class="r">Bugs</th></tr></thead>'
        '<tbody>' + rows + '</tbody></table></div>')


# ── page ─────────────────────────────────────────────────────────────────────
def build_team_page(team: str, rd, stories: list[dict], report: dict,
                    sprint_names: set[str]) -> str:
    title = f"{team} — JIRA Sprint Retrospective"
    meta = (f"Team: {esc(team)} &nbsp;·&nbsp; {esc(', '.join(sorted(sprint_names)))} "
            f"&nbsp;·&nbsp; {len(rd.pbis_df)} Epics · {len(rd.tasks_df)} Tasks · "
            f"{len(rd.bugs_df)} Bugs &nbsp;·&nbsp; hBITS Calmanac")
    sections = [
        _summary_cards(rd, stories),
        _goals_section(stories),
        _deliverables_section(rd),
        _commitment_section(stories, report),
        _estimation_section(rd),
        _time_split_section(rd),
        _bugs_rootcause_section(rd),
        _bugs_open_section(rd),
        _bugs_per_epic_section(rd),
    ]
    return RD.PAGE_HEAD.format(title=title, meta=meta) + "".join(sections) + RD.PAGE_FOOT


def main() -> int:
    ap = argparse.ArgumentParser(description="Per-team JIRA sprint retro from a frozen snapshot.")
    ap.add_argument("--snapshot", default=None, help="Path to a specific snapshot JSON.")
    ap.add_argument("--wave", default=None, help="Snapshot wave label (e.g. Sprint-1).")
    ap.add_argument("--team", default=None, help="Only build this team's report.")
    args = ap.parse_args()

    path = Path(args.snapshot) if args.snapshot else _latest_snapshot(args.wave)
    snap = json.loads(path.read_text(encoding="utf-8"))
    print(f"Loading frozen snapshot: {path}")
    print(f"  wave={snap.get('wave')}  issues={snap.get('issue_count')}  "
          f"sprints={[m.get('name') for m in snap.get('sprints', [])]}")

    teams = [args.team] if args.team else _teams_in(snap)
    if not teams:
        print("  [warn] no teams found in snapshot metadata; nothing to build.")
        return 1

    _REPORTS.mkdir(parents=True, exist_ok=True)
    written = []
    for team in teams:
        sprint_names = _team_sprint_names(snap, team)
        rd = JRF.load_retro_data_jira_from_snapshot(str(path), team=team)
        stories = _team_stories(snap, sprint_names)
        report = _sprint_report(snap, sprint_names)
        out = _REPORTS / f"{team.replace(' ', '_')}_JIRA_Sprint_Retro.html"
        out.write_text(
            build_team_page(team, rd, stories, report, sprint_names),
            encoding="utf-8")
        written.append(out)
        print(f"  {team:<16} -> {out.name}  "
              f"(Epics {len(rd.pbis_df)}, Tasks {len(rd.tasks_df)}, Bugs {len(rd.bugs_df)})")

    print(f"\n✅ Wrote {len(written)} team report(s) to {_REPORTS}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
