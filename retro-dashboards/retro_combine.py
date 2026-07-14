"""
retro_combine.py
================
Team-filtering + tagging helpers over RetroData for the JIRA retro, plus
the per-team side-by-side generator. It does three small, well-defined things:

  1. tag_release(data, label)   -> add a "Release" column to each DataFrame so a
                                   row knows which release it came from
                                   or "MPM Sprint 1 (JIRA)") it came from.
  2. member_to_team(name)        -> map an assignee display name to a CFG.TEAMS
                                   team (Calmers / Crackers / Knackers / QA
                                   Automation), else "Other". Works for both
                                   systems because the people are the same.
  3. for_team(data, team)        -> return a RetroData filtered to just the rows
                                   whose assignee is on that team (PBIs, tasks,
                                   bugs). Used to produce one report per team.

No analysis lives here — the generator computes metrics from the filtered data.

CLI (sanity check, needs both systems reachable):
    python scripts/retro/retro_combine.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pandas as pd

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))

import retro_data   # noqa: E402  -- RetroData
import retro_config as CFG  # noqa: E402


# ── Team resolution ─────────────────────────────────────────────────────────
def _norm(name: str) -> str:
    """Lowercase, drop punctuation/space, for tolerant name matching
    ('Mugdha.Thakare' == 'mugdha thakare')."""
    return re.sub(r"[^a-z0-9]", "", str(name or "").lower())


# Precompute normalised roster -> team once.
_ROSTER = {}
for _team, _members in CFG.TEAMS.items():
    for _m in _members:
        _ROSTER[_norm(_m)] = _team


def member_to_team(name: str) -> str:
    """Map an assignee display name to its retro team, else 'Other'."""
    n = _norm(name)
    if not n:
        return "Other"
    if n in _ROSTER:
        return _ROSTER[n]
    # token-subset fallback: 'abhisha.jain' vs 'Abhisha Jain', first+last in any order
    name_tokens = set(re.findall(r"[a-z0-9]+", str(name or "").lower()))
    for member, team in ((m, t) for t, ms in CFG.TEAMS.items() for m in ms):
        mt = set(re.findall(r"[a-z0-9]+", member.lower()))
        if mt and (mt <= name_tokens or name_tokens <= mt):
            return team
    return "Other"


def teams() -> list[str]:
    """The configured team names (report is generated once per team)."""
    return list(CFG.TEAMS.keys())


def _team_from_field(value: str) -> str:
    """Normalise a JIRA Team field ('Calmers - RCM') to a CFG.TEAMS key."""
    v = str(value or "").strip().lower()
    if not v:
        return ""
    for t in sorted(CFG.TEAMS.keys(), key=len, reverse=True):
        if v.startswith(t.lower()):
            return t
    return ""


def _host_team_from_iteration(path: str) -> str:
    """Infer team from an iteration path ('...\\Sprint 84 Calmers')."""
    seg = str(path or "").replace("/", "\\").split("\\")[-1].lower()
    if not seg:
        return ""
    for t in sorted(CFG.TEAMS.keys(), key=len, reverse=True):
        if t.lower() in seg:
            return t
    return ""


def resolve_row_team(row) -> str:
    """Team for a single row, in priority order:
       1. assignee → roster
       2. effective assignee (parent Story's Dev/QA Assignee, for [DEV]/[QA] sub-tasks)
       3. JIRA 'Team' field
       4. iteration path."""
    t = member_to_team(row.get("Assigned To", ""))
    if t != "Other":
        return t
    eff = row.get("effective_assignee", "")
    if eff:
        t2 = member_to_team(eff)
        if t2 != "Other":
            return t2
    nt = _team_from_field(row.get("Team", ""))
    if nt:
        return nt
    ht = _host_team_from_iteration(row.get("Iteration Path", ""))
    if ht:
        return ht
    return "Other"


# ── Release tagging ─────────────────────────────────────────────────────────
def _with_release(df: pd.DataFrame, label: str) -> pd.DataFrame:
    out = df.copy()
    out["Release"] = label
    return out


def tag_release(data: "retro_data.RetroData", label: str) -> "retro_data.RetroData":
    """Return a copy of `data` with a 'Release' column on every DataFrame."""
    return retro_data.RetroData(
        pbis_df=_with_release(data.pbis_df, label),
        tasks_df=_with_release(data.tasks_df, label),
        bugs_df=_with_release(data.bugs_df, label),
        iteration_paths=list(data.iteration_paths),
        sprint_name=data.sprint_name,
        member_capacity=dict(data.member_capacity),
    )


# ── Per-team filtering ──────────────────────────────────────────────────────
def _team_mask(df: pd.DataFrame, team: str) -> pd.Series:
    if df.empty:
        return pd.Series([False] * len(df), index=df.index)
    return df.apply(lambda r: resolve_row_team(r) == team, axis=1)


def for_team(data: "retro_data.RetroData", team: str) -> "retro_data.RetroData":
    """Filter a RetroData to rows whose assignee is on `team`."""
    pbis = data.pbis_df[_team_mask(data.pbis_df, team)].copy().reset_index(drop=True)
    tasks = data.tasks_df[_team_mask(data.tasks_df, team)].copy().reset_index(drop=True)
    bugs = data.bugs_df[_team_mask(data.bugs_df, team)].copy().reset_index(drop=True)
    cap = {m: h for m, h in data.member_capacity.items() if member_to_team(m) == team}
    return retro_data.RetroData(
        pbis_df=pbis, tasks_df=tasks, bugs_df=bugs,
        iteration_paths=list(data.iteration_paths),
        sprint_name=data.sprint_name, member_capacity=cap,
    )


def other_detail(data: "retro_data.RetroData"):
    """Return (assignee_counts, samples) for rows that resolve to 'Other', so we
    can see who/what is unattributed. samples = list of (kind, id, assignee,
    title, team_field, iteration)."""
    from collections import Counter
    counts: "Counter[str]" = Counter()
    samples = []
    for kind, df in (("PBI", data.pbis_df), ("Task", data.tasks_df), ("Bug", data.bugs_df)):
        if df.empty:
            continue
        for _, r in df.iterrows():
            if resolve_row_team(r) == "Other":
                a = str(r.get("Assigned To", "") or "(unassigned)")
                counts[a] += 1
                if len(samples) < 40:
                    samples.append((kind, str(r.get("ID", "")), a,
                                    str(r.get("Title", ""))[:55],
                                    str(r.get("Team", "") or ""),
                                    str(r.get("Iteration Path", "") or "")))
    return counts, samples


def team_counts(data: "retro_data.RetroData") -> dict[str, dict[str, int]]:
    """Quick {team: {pbis, tasks, bugs}} tally for sanity checks."""
    out = {}
    for t in teams() + ["Other"]:
        out[t] = {
            "pbis": int(_team_mask(data.pbis_df, t).sum()),
            "tasks": int(_team_mask(data.tasks_df, t).sum()),
            "bugs": int(_team_mask(data.bugs_df, t).sum()),
        }
    return out


if __name__ == "__main__":
    # Sanity check: load a JIRA sprint, tag it, and print per-team counts.
    import jira_retro_fetch as _jrf
    jira_sprint = sys.argv[1] if len(sys.argv) > 1 else "MPM Sprint 1"

    print(f"Loading JIRA {jira_sprint!r} ...")
    jira = tag_release(_jrf.load_retro_data_jira(jira_sprint), f"{jira_sprint} (JIRA)")

    print(f"\n=== {jira_sprint} (JIRA) — per-team counts (pbis / tasks / bugs) ===")
    for t, c in team_counts(jira).items():
        print(f"  {t:<16} {c['pbis']:>3} / {c['tasks']:>3} / {c['bugs']:>3}")
    counts, samples = other_detail(jira)
    if counts:
        print("  --- OTHER: assignees (count across pbis/tasks/bugs) ---")
        for a, n in counts.most_common():
            print(f"     {n:>3}  {a}")
