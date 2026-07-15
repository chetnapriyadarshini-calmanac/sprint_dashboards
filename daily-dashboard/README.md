# Daily Dashboard

The morning EM **sprint dashboard** — a single self-contained HTML file (Overview, Goal Buckets, Categories, Capacity, Discrepancies, Daily Tracking, DSM Insights, Risk & Health) — plus the **daily release snapshot** job.

Work items are pulled live from **JIRA Cloud**. Per-member capacity comes from a maintained Excel workbook (`Team_Capacity.xlsx`).

---

## Files

| File | Purpose |
|------|---------|
| `generate_dashboard.py` | Builds the dashboard HTML |
| `sprint_dashboard_config.py` | **Edit each sprint** — sprint identity, team rosters, JIRA sprint names, capacity path |
| `jira_fetch.py`, `jira_auth.py` | JIRA fetch + auth |
| `capacity_excel.py` | Reads capacity from `Team_Capacity.xlsx` |
| `dashboard_tabs_extra.py` | Daily-tracking / DSM / risk tabs + history helpers |
| `corrections.json` | Manual data corrections applied at build time |
| `Snapshot-JiraRelease.py` | The daily release snapshot (feeds the monthly retro) |
| `Run-JiraDashboard.ps1` | Convenience wrapper for the dashboard |
| `Run-JiraSnapshot.ps1` | Headless wrapper for the daily snapshot (logs to `logs/`) |
| `Register-JiraSnapshot.ps1` | Registers the snapshot as a Windows Task Scheduler job |

---

## 1. Start a new sprint (edit `sprint_dashboard_config.py`)

At sprint rollover, open `sprint_dashboard_config.py` and update the two blocks below. **These are the only edits needed to point the dashboard at a new sprint.**

### a) Sprint Identity

```python
# -- Sprint Identity --
SPRINT_NUMBER     = 3                       # bump to the new sprint number
SPRINT_NAME       = "Sprint 3"              # "Sprint <N>"
SPRINT_TOTAL_DAYS = 10                       # working days (usually 10)
SPRINT_DATES      = "July 20 - July 31, 2026"   # for the dashboard header
SPRINT_START_DATE = "2026-07-20"            # ISO YYYY-MM-DD, first working day
```

`SPRINT_DAY` is computed automatically from `SPRINT_START_DATE` (working days, Mon–Fri). Leave `SPRINT_DAY_OVERRIDE = None` unless you need to force a specific day number.

### b) JIRA scoping

```python
RELEASE_NAME      = "REL-AUG-26"            # current Fix Version (or None)
JIRA_SPRINT_NAMES = [                        # one per team, EXACTLY as named in JIRA
    "MPM Calmers Sprint 3",
    "MPM Crackers Sprint 3",
    "MPM Knackers Sprint 3",
    # "MPM QA Automation Sprint 3",          # uncomment if QA Automation runs its own
]
```

> **Important:** each team runs its own JIRA sprint named `MPM <Team> Sprint <N>`. List every one — work items are OR-ed across them in the JQL. Names are **case- and space-sensitive** and must match JIRA exactly, or that team's issues won't be pulled.

### c) Capacity workbook (only if the path changes)

```python
CAPACITY_XLSX = r"G:\My Drive\Team_Capacity.xlsx"
```

Each sprint, make sure the team has updated the workbook's **Settings** sheet (Working days / Team days off) — capacity math reads from there.

---

## 2. Update team members (`TEAMS` in `sprint_dashboard_config.py`)

When someone joins, leaves, or changes teams, edit the `TEAMS` dictionary. Keys are the team names shown on the dashboard; values are the members' **exact JIRA assignee display names** (case-sensitive), which must also match the names used in the capacity workbook.

```python
TEAMS = {
    "Calmers": [
        "Priya Mandhare", "Sumit Anpat", "Sandesh Tendulkar",
        "Suraj Marathe", "Gautam Gehlot", "Sandip Sutar",
    ],
    "Crackers": [
        "AbdulGani Shaikh", "Mugdha.Thakare", "Priyanka Kusal",
    ],
    "Knackers": [
        "Abhisha Jain", "vivek ghorpade", "Heeru Gujar",
        # "Parth Biramwar" — excluded: QA Manager (management-focused)
        "Sneha Dafale", "Rahul Patil", "Suyog Joshi",
    ],
    "QA Automation": [
        "Sudarshan Shinde", "Vrushali Sagare",
    ],
}
```

Tips:
- The display name must be **exactly** what JIRA shows (e.g. `Mugdha.Thakare` and `vivek ghorpade` are intentionally spelled/cased as they appear in JIRA). If a person's hours don't show up, a name mismatch is the usual cause.
- To exclude someone from delivery metrics (e.g. a manager), leave them out or comment them out with the reason.
- Keep the roster in sync with the retro's `retro-dashboards/retro_config.py` `TEAMS` so both reports attribute people the same way.

---

## 3. Generate the dashboard

From this folder:

```bash
python generate_dashboard.py
# optional one-off overrides (otherwise the config values are used):
python generate_dashboard.py --release "REL-AUG-26" --sprint "MPM Calmers Sprint 3"
```

or on Windows:

```powershell
.\Run-JiraDashboard.ps1
```

Output: `Sprint<N>_Dashboard_Day<D>.html` in this folder — open it in any browser or drop it into Slack/Teams/email. (It shows the **previous** day's logged hours, so the day number reflects the data shown.)

---

## 4. Run every day at 10:00

Two things should run daily during a sprint:

1. **`Snapshot-JiraRelease.py`** — freezes the release's current state (full issue set + per-issue status changelog) into `snapshots/jira/<RELEASE>/<YYYY-MM-DD>.json`. Idempotent per day. These accumulate into the history the **monthly retro** reads and power scope-creep detection.
2. **`generate_dashboard.py`** — rebuilds the morning dashboard HTML.

Run the snapshot **first** (the dashboard's Daily Tracking reflects the freshest data), then generate the dashboard.

### Manual (from this folder)

```bash
python Snapshot-JiraRelease.py            # default release REL-AUG-26
python generate_dashboard.py
```

Or on Windows, one line:

```powershell
.\Run-JiraSnapshot.ps1 ; .\Run-JiraDashboard.ps1
```

### Automate at 10:00 (recommended)

Register the snapshot as a daily Windows Task Scheduler job at 10:00:

```powershell
.\Register-JiraSnapshot.ps1 -Time "10:00" -Release "REL-AUG-26"
```

To also generate the dashboard at 10:00, register a second task that runs both in sequence:

```powershell
schtasks /Create /SC DAILY /ST 10:00 /TN "hBITS Daily Dashboard" /F ^
  /TR "powershell -NoProfile -ExecutionPolicy Bypass -File \"%CD%\Run-JiraDashboard.ps1\""
```

Manage the jobs:

```powershell
schtasks /Query /TN "hBITS JIRA Release Snapshot" /V /FO LIST
schtasks /Run   /TN "hBITS JIRA Release Snapshot"     # run on demand
schtasks /Run   /TN "hBITS Daily Dashboard"
.\Register-JiraSnapshot.ps1 -Unregister                # remove the snapshot job
```

The machine must be awake at 10:00 and the repo-root credential files present. JIRA Cloud is on the public internet — no VPN needed.

---

## Capacity workbook setup

`CAPACITY_XLSX` in `sprint_dashboard_config.py` decides where per-member capacity is read from. The workbook (whichever form) must have a **`Settings`** sheet (Working days in `B5`, Team days off in `B6`) and a **`Capacity`** sheet (`Team, Member, Activity, Capacity/day, Days off`).

### Live Google Sheet, read as you via OAuth (current setup)

The dashboard reads the capacity **Google Sheet** using your own Google identity, so the company-domain restriction is satisfied without a service account. First run opens a browser to sign in and grant read-only access; the token is cached and refreshed automatically, so later runs are non-interactive.

One-time setup:

1. In **Google Cloud Console**, create a project (or reuse one) and **enable the Google Drive API**.
2. Create an **OAuth Client ID** of type **Desktop app** and download its JSON. If you can make the OAuth consent screen **Internal** (motivity Workspace), do so — the token then never expires. (An *External/Testing* app works too, but Google expires the refresh token every 7 days, so an unattended job would need re-consent weekly.)
3. Save the downloaded file as **`.gcp_oauth_client.json` at the repo root** (git-ignored), or set `CAPACITY_OAUTH_CLIENT` in the config to its path.
4. Install libs: `pip install -r requirements.txt` (adds `google-auth-oauthlib`).
5. Set `CAPACITY_XLSX` in the config to the **Sheet URL** (e.g. `https://docs.google.com/spreadsheets/d/<SHEET_ID>/edit`). The Sheet must have tabs named exactly **`Settings`** and **`Capacity`**.
6. Run `python capacity_excel.py` once to complete the browser sign-in. A `.gcp_oauth_token.json` (git-ignored) is written and reused afterward.

For the **daily 10:00 job**: do step 6 once, signed in, on the machine that will run it. After that it runs headless — as long as the token doesn't expire (hence the *Internal* app recommendation). Delete `.gcp_oauth_token.json` to force a fresh sign-in. The token/key are personal secrets — never commit them (already git-ignored).

### Alternatives

- **Committed `.xlsx` (no auth, most reliable for automation)** — a `Team_Capacity.xlsx` is committed in this folder as a fallback/reference. To use it instead, set `CAPACITY_XLSX = "Team_Capacity.xlsx"`; edit the file and commit when capacity changes. `.gitignore` tracks only this workbook (stray `*.xlsx` and Excel lock files stay out).
- **Absolute local `.xlsx`** — a real `.xlsx` in a Drive/OneDrive synced folder (e.g. `G:\My Drive\Team_Capacity.xlsx`). Must be a real `.xlsx`, not a native Google Sheet (which syncs only as a `.gsheet` pointer).
- **Service account** — set `CAPACITY_SA_KEY` and share the Sheet with the service account's `client_email`. Needs IT to allow a service account.
- **Public URL** — a Sheet shared "Anyone with the link – Viewer"; no credentials.

The loader tries service account → OAuth → public in that order for a Sheet URL. If a run can't read the workbook, it prints a specific message telling you what to fix.

---

## Notes

- Credentials: see the repo-root `README.md`. The scripts look for `.jira_pat` / `.jira_email` / `.jira_site` at the repo root.
- **Capacity workbook.** `CAPACITY_XLSX` in `sprint_dashboard_config.py` can be a Google Sheet URL, a local `.xlsx` path, or a public URL — see the [Capacity workbook setup](#capacity-workbook-setup) section below. Whichever you use, it must have a `Settings` sheet (Working days in B5, Team days off in B6) and a `Capacity` sheet (Team, Member, Activity, Capacity/day, Days off). The workbook / key are **not** in git.
- Generated HTML and `Sprint<N>_history.json` are git-ignored.
