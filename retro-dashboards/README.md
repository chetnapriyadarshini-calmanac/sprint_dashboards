# Retro Dashboards

Two JIRA-driven retrospective reports, both generated **offline** from frozen snapshots:

- **Sprint retro** — one self-contained HTML per team, built from a frozen *sprint wave* snapshot.
- **Monthly release retro** — one HTML per release, built from the daily *release* snapshots.

---

## Files

| File | Purpose |
|------|---------|
| `Snapshot-JiraSprint.py` | Freeze a sprint wave into `snapshots/jira_sprint/<WAVE>/` |
| `Run-JiraSprintSnapshot.ps1` | Wrapper for the sprint snapshot (logs to `logs/`) |
| `generate_jira_sprint_retro.py` | Build the per-team **sprint retro** (offline) |
| `Generate-MonthlyRetro.py` | Build the **monthly release retro** (offline) |
| `jira_retro_fetch.py` | Loads `RetroData` from a frozen sprint snapshot |
| `retro_data.py` | `RetroData` container + root-cause helpers |
| `retro_config.py` | Team rosters, root-cause normalisation, custom-field candidates |
| `retro_combine.py` | Team filtering / tagging over `RetroData` |
| `retro_layout.py` | Shared HTML shell (page head, CSS, footer) |
| `jira_fetch.py`, `jira_auth.py` | JIRA fetch + auth |

---

## Sprint retro

> **For a new sprint there is nothing to edit in code.** Team membership and sections are read from the frozen snapshot itself. You only need to (1) freeze the new sprint's wave with the right `--wave` label, then (2) generate the retro pointing at that wave. The only config you keep current is the team roster in `retro_config.py` (see [Update team members](#update-team-members)).

### Step 1 — Freeze the sprint (once per sprint, before rollover)

The sprint retro is built from a **point-in-time snapshot** taken on the last day of the sprint, **before** the boards roll to the next sprint. Use a `--wave` label that matches the sprint number (e.g. `Sprint-3` for Sprint 3):

```bash
# freeze every active MPM sprint into snapshots/jira_sprint/Sprint-3/ :
python Snapshot-JiraSprint.py --wave "Sprint-3"

# (with no --wave it auto-labels from the active sprints)
python Snapshot-JiraSprint.py

# or freeze specific team sprints only:
python Snapshot-JiraSprint.py --wave "Sprint-3" --sprint "MPM Knackers Sprint 3" --sprint "MPM Calmers Sprint 3"

# faster, less exact (skips the per-issue changelog):
python Snapshot-JiraSprint.py --wave "Sprint-3" --no-changelog
```

On Windows:

```powershell
.\Run-JiraSprintSnapshot.ps1 -Wave "Sprint-3"
```

Output: `snapshots/jira_sprint/<WAVE>/<YYYY-MM-DD>.json`.

### Step 2 — Generate the retro (any time after freezing)

Runs fully offline against the frozen snapshot:

```bash
# newest snapshot across all waves (typical: right after freezing):
python generate_jira_sprint_retro.py

# a specific wave (recommended so you always target the intended sprint):
python generate_jira_sprint_retro.py --wave Sprint-3

# a specific snapshot file, or a single team:
python generate_jira_sprint_retro.py --snapshot ../snapshots/jira_sprint/Sprint-3/2026-07-31.json
python generate_jira_sprint_retro.py --team Knackers
```

Output: `reports/<Team>_JIRA_Sprint_Retro.html` (one per team). Sections per team: Summary · Sprint Goals · Deliverables (Epics) · Commitment vs Delivery · Estimation Accuracy · Time Split · Bugs by Root Cause · Bugs Open at Sprint End · Bugs per Epic.

### Update team members

Team attribution comes from the `TEAMS` dictionary in `retro_config.py` (same shape as the daily dashboard's). When someone joins, leaves, or switches teams, edit it — keys are team names, values are members' **exact JIRA assignee display names** (case-sensitive). Anyone not listed rolls into an "Other" bucket. Keep this roster in sync with `daily-dashboard/sprint_dashboard_config.py` so the dashboard and retro attribute people identically.

```python
# retro_config.py
TEAMS = {
    "Calmers":       ["Priya Mandhare", "Sumit Anpat", ...],
    "Crackers":      ["AbdulGani Shaikh", "Mugdha.Thakare", ...],
    "Knackers":      ["Abhisha Jain", "vivek ghorpade", ...],
    "QA Automation": ["Sudarshan Shinde", "Vrushali Sagare"],
}
```

---

## Monthly release retro

Built offline from the **daily release snapshots** produced by `daily-dashboard/Snapshot-JiraRelease.py` (so make sure that daily job has been running through the release — see `daily-dashboard/README.md`).

```bash
python Generate-MonthlyRetro.py                # default release REL-AUG-26
python Generate-MonthlyRetro.py REL-SEP-26     # a specific release
```

Output: `reports/<RELEASE>_Monthly_Retro.html`.

KPIs: time in Backlog / BA Analysis, Dev/Refinement/QA date-met, defects by severity, epics shipped by size, scope creep (needs ≥2 daily snapshots), analysis-vs-implementation-vs-QA time split, preprod bugs grouped by root cause.

---

## Notes

- Credentials (repo-root `.jira_pat` / `.jira_email` / `.jira_site`) are only needed for the **snapshot** step; generating the retros is fully offline.
- Snapshots and generated reports are git-ignored; the folders themselves are kept.
- Team attribution comes from the rosters in `retro_config.py` — keep them current as people move teams.
