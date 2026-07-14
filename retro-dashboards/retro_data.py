"""
retro_data.py
=============
Shared retro data structures and helpers used by the JIRA retro generators.

Holds the canonical `RetroData` container (PBIs / tasks / bugs as DataFrames)
plus the small pure helpers for normalising bug root-cause values. The JIRA
retro loaders (jira_retro_fetch.py) populate a RetroData from a frozen snapshot;
the renderers (generate_retro_dashboard.py, generate_jira_sprint_retro.py)
consume it. No external system calls happen here.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

import retro_config as CFG


# ── Root-cause helpers ───────────────────────────────────────────────────────
def _normalise_root_cause(raw: str) -> str:
    """Map a raw RootCauseType value to one of the display categories.
    Empty -> 'Pending Investigation'. Unknown non-empty -> 'Other'."""
    if not raw:
        return "Pending Investigation"
    key = raw.strip().lower()
    if key in CFG.ROOT_CAUSE_NORMALISATION:
        return CFG.ROOT_CAUSE_NORMALISATION[key]
    # Try a "contains" pass for messy values like "UI/Logic - code issue"
    for needle, cat in CFG.ROOT_CAUSE_NORMALISATION.items():
        if needle in key:
            return cat
    return "Other"


def _strip_html(s: str) -> str:
    """Root-cause / analysis fields may contain HTML. Strip tags + collapse
    whitespace for clean display in the dashboard's small text cells."""
    if not s:
        return ""
    import re
    out = re.sub(r"<[^>]+>", " ", s)
    out = (out.replace("&nbsp;", " ")
              .replace("&amp;", "&")
              .replace("&lt;", "<")
              .replace("&gt;", ">")
              .replace("&quot;", '"'))
    out = re.sub(r"\s+", " ", out).strip()
    return out


# ── Public dataclass ─────────────────────────────────────────────────────────
@dataclass
class RetroData:
    pbis_df: pd.DataFrame
    tasks_df: pd.DataFrame
    bugs_df: pd.DataFrame
    iteration_paths: list[str] = field(default_factory=list)
    sprint_name: str = ""
    # Per-member sprint capacity in hours, keyed by canonical roster name.
    # Empty dict when capacity was not captured for the sprint.
    member_capacity: dict[str, float] = field(default_factory=dict)

    def summary(self) -> str:
        cap_n = len(self.member_capacity)
        cap_total = round(sum(self.member_capacity.values()), 1) if self.member_capacity else 0
        return (f"Sprint: {self.sprint_name}\n"
                f"Iterations: {len(self.iteration_paths)} "
                f"({', '.join(self.iteration_paths)})\n"
                f"PBIs:  {len(self.pbis_df)}\n"
                f"Tasks: {len(self.tasks_df)}\n"
                f"Bugs:  {len(self.bugs_df)} "
                f"(with parent PBI: "
                f"{int(self.bugs_df['parent_pbi_id'].notna().sum()) if not self.bugs_df.empty else 0})\n"
                f"Capacity: {cap_n} member"
                f"{'s' if cap_n != 1 else ''}, {cap_total}h total")
