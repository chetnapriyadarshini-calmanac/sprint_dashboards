# em-standup — JIRA Sprint Dashboards & Retros

Tooling for the hBITS Calmanac engineering team, driven entirely by **JIRA Cloud**:

- **`daily-dashboard/`** — the morning EM sprint dashboard (one self-contained HTML file) and the **daily release snapshot** job that accumulates the history the monthly retro is built from.
- **`retro-dashboards/`** — the end-of-sprint **sprint retro** (one HTML per team) and the **monthly release retro**, both generated offline from frozen snapshots.
- **`snapshots/`** — captured point-in-time JIRA state (`jira/` = daily release snapshots, `jira_sprint/` = frozen sprint waves). *Data is git-ignored; the folders are kept.*
- **`reports/`** — generated retro / monthly HTML output. *Git-ignored.*
- **`logs/`** — scheduled-run logs. *Git-ignored.*

## Prerequisites

- **Python 3.10+**. Install dependencies with `pip install -r requirements.txt` (core: `pandas`, `requests`, `openpyxl`; plus `google-api-python-client` + `google-auth` for the Google-Sheet capacity source).
- **JIRA Cloud API credentials** (see below). JIRA Cloud is reachable over the public internet — no VPN needed.
- **Capacity source access** — see `daily-dashboard/README.md`. The default is a Google Sheet read via a service-account key.

## Credentials (one-time setup)

Create these single-line files at the **repo root** (they are git-ignored):

| File          | Contents                                              |
|---------------|-------------------------------------------------------|
| `.jira_pat`   | Your Atlassian API token                              |
| `.jira_email` | Your Atlassian account email                          |
| `.jira_site`  | Site base URL, e.g. `https://motivity.atlassian.net`  |

Generate an API token at <https://id.atlassian.com/manage-profile/security/api-tokens>.

Alternatively set the `JIRA_API_TOKEN`, `JIRA_EMAIL`, and `JIRA_SITE` environment variables.

## Quick start

```bash
# 1. Daily dashboard (edit the sprint in daily-dashboard/sprint_dashboard_config.py first)
cd daily-dashboard && python generate_dashboard.py

# 2. Daily release snapshot — run EVERY day (see daily-dashboard/README.md)
python Snapshot-JiraRelease.py

# 3. Sprint retro — after freezing the sprint wave (see retro-dashboards/README.md)
cd ../retro-dashboards && python generate_jira_sprint_retro.py
```

See **`daily-dashboard/README.md`** and **`retro-dashboards/README.md`** for full, step-by-step instructions.

## What is NOT committed

Secrets, and all generated data (snapshots, reports, logs, dashboard HTML) are git-ignored — see `.gitignore`. Clone the repo, add your credentials, and the scripts regenerate everything.
