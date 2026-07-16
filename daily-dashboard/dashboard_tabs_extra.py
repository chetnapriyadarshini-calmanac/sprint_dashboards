"""
Extra tab builders: Daily Tracking, DSM Insights, Risk & Health
Imported by generate_sprint_dashboard.py — do not run directly.

Sprint-health flagging (Risk & Health tab, Scope Creep panel):
added tasks are scored "absorbed" / "watch" / "at_risk" rather than
flagged universally in red. See _classify_added in build_risk_health_tab.
"""
import json, math, re
from datetime import datetime, date, timedelta
from pathlib import Path
import pandas as pd


def _name_tokens(s) -> frozenset:
    """Token set for tolerant name matching ('suyog.joshi' == 'Suyog Joshi')."""
    s = re.sub(r"[._\-]+", " ", str(s or "").lower())
    return frozenset(t for t in s.split() if t)


def _roster_canonicaliser(teams: dict):
    """Return (roster_names_set, canon_fn) where canon_fn maps an assignee to its
    roster display name (tolerant of dotted usernames), or '' if not on a team."""
    roster = [(_name_tokens(m), m) for ms in teams.values() for m in ms]
    names = {m for ms in teams.values() for m in ms}

    def _canon(n):
        tk = _name_tokens(n)
        if not tk:
            return ""
        for rt, disp in roster:
            if rt and (rt == tk or rt <= tk or tk <= rt):
                return disp
        return ""   # not on any roster team

    return names, _canon


def _tid(v):
    """Work-item id: int for TFS numeric IDs, the string key for JIRA
    ("MPM-105"), and "" for missing/NaN. Replaces the old int(...) casts
    that assumed numeric TFS IDs and broke on JIRA keys."""
    try:
        if v is None:
            return ""
        if isinstance(v, float) and v != v:   # NaN
            return ""
        return int(v)
    except (TypeError, ValueError):
        return str(v)

# ── History helpers ────────────────────────────────────────────────────────────

def load_history(path: Path) -> dict:
    if path.exists():
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            # Corrupted file (e.g. interrupted write) — back it up and start fresh.
            import shutil
            backup = path.with_suffix(".json.bak")
            try:
                shutil.copy2(path, backup)
                print(f"⚠ History file corrupt ({e}); backed up to {backup.name} and starting fresh.")
            except Exception:
                print(f"⚠ History file corrupt ({e}); starting fresh (backup failed).")
    return {"snapshots": []}


def save_history(path: Path, history: dict):
    """Write atomically: serialise to a temp file then rename so a crash
    mid-write never leaves a corrupt history file."""
    tmp = path.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(history, f, indent=2, default=str)
    tmp.replace(path)   # atomic on POSIX; near-atomic on Windows


def take_snapshot(today_str: str, sprint_day: int, s79_tasks, pbis, metrics: dict) -> dict:
    """Capture today's state into a snapshot dict."""
    per_member = {}
    for _, t in s79_tasks.iterrows():
        name  = str(t.get("Assigned To", "Unassigned"))
        state = str(t.get("State", ""))
        spent = float(t.get("Completed Work", 0) or 0)
        est   = float(t.get("Original Estimate", 0) or 0)
        tid   = _tid(t.get("ID", 0))
        if name not in per_member:
            per_member[name] = {"spent": 0.0, "est": 0.0, "done": 0, "ip": 0, "todo": 0, "task_ids": []}
        per_member[name]["spent"] += spent
        per_member[name]["est"]   += est
        per_member[name]["task_ids"].append(tid)
        if state == "Done":
            per_member[name]["done"] += 1
        elif state == "In Progress":
            per_member[name]["ip"] += 1
        else:
            per_member[name]["todo"] += 1

    # Per-task state snapshot for change detection
    task_states = {}
    for _, t in s79_tasks.iterrows():
        tid = _tid(t.get("ID", 0))
        task_states[str(tid)] = {
            "state":   str(t.get("State", "")),
            "spent":   float(t.get("Completed Work", 0) or 0),
            "est":     float(t.get("Original Estimate", 0) or 0),
            "assignee": str(t.get("Assigned To", "")),
            "title":   str(t.get("Title", "")),
        }

    return {
        "date":        today_str,
        "sprint_day":  sprint_day,
        "tasks_total": metrics.get("total_tasks", 0),
        "tasks_done":  metrics.get("tasks_done", 0),
        "tasks_ip":    metrics.get("tasks_ip", 0),
        "est_h":       metrics.get("est_h", 0),
        "spent_h":     metrics.get("spent_h", 0),
        "pbis_done":   metrics.get("pbis_done", 0),
        "per_member":  per_member,
        "task_states": task_states,
    }


def upsert_snapshot(history: dict, snapshot: dict) -> dict:
    """Add or replace today's snapshot in history."""
    snaps = history.get("snapshots", [])
    today = snapshot["date"]
    snaps = [s for s in snaps if s["date"] != today]
    snaps.append(snapshot)
    snaps.sort(key=lambda s: s["date"])
    history["snapshots"] = snaps
    return history

# ── Colour helpers ─────────────────────────────────────────────────────────────

def risk_badge(level):
    colors = {
        "HIGH":   ("🔴", "#fee2e2", "#dc2626"),
        "MEDIUM": ("🟡", "#fef3c7", "#d97706"),
        "LOW":    ("🟢", "#dcfce7", "#16a34a"),
        "OK":     ("✅", "#dcfce7", "#16a34a"),
    }
    em, bg, fg = colors.get(level.upper(), ("⚪", "#f1f5f9", "#64748b"))
    return (f'<span style="background:{bg};color:{fg};padding:2px 10px;border-radius:10px;'
            f'font-size:11px;font-weight:700">{em} {level}</span>')


def delta_chip(val, unit="h", good_direction="up"):
    """Green chip for positive delta, red for negative."""
    if val > 0:
        color = "#16a34a" if good_direction == "up" else "#dc2626"
        sym   = "▲"
    elif val < 0:
        color = "#dc2626" if good_direction == "up" else "#16a34a"
        sym   = "▼"
    else:
        color = "#94a3b8"; sym = "━"
    return (f'<span style="color:{color};font-size:11px;font-weight:700">'
            f'{sym} {abs(val)}{unit}</span>')


def mini_bar(pct, color="#6366f1", width=80):
    pct = min(max(pct, 0), 100)
    return (f'<div style="background:#e2e8f0;border-radius:4px;height:8px;'
            f'width:{width}px;display:inline-block;vertical-align:middle">'
            f'<div style="background:{color};border-radius:4px;height:8px;width:{pct}%"></div></div>')


def section_card(title, icon, content, accent="#6366f1"):
    return f"""<div style="background:#fff;border:1px solid #e2e8f0;border-radius:12px;
overflow:hidden;margin-bottom:14px;box-shadow:0 1px 3px rgba(0,0,0,.06)">
  <div style="padding:10px 14px;background:#f8fafc;border-bottom:1px solid #e2e8f0;display:flex;align-items:center;gap:8px">
    <span style="font-size:16px">{icon}</span>
    <span style="font-size:13px;font-weight:700;color:#1e293b">{title}</span>
  </div>
  <div style="padding:14px">{content}</div>
</div>"""


# ═══════════════════════════════════════════════════════════════════════════════
#  TAB 1 — DAILY TRACKING
# ═══════════════════════════════════════════════════════════════════════════════

def build_daily_tracking_tab(s79_tasks, history: dict, cap_lookup: dict, sprint_day: int, sprint_total_days: int):
    snaps = history.get("snapshots", [])
    today_str = date.today().isoformat()

    # Get the most recent prior snapshot (skip today which was just upserted).
    # Under normal daily cadence this is yesterday; if the scheduler skipped a
    # day the baseline will be older — we surface that in the UI so the label
    # "no hours logged" is never misleading.
    prev = None
    for s in reversed(snaps[:-1]):   # skip last (today already upserted)
        prev = s
        break
    # If only one snapshot, prev = None (Day 1, nothing to compare)

    # Human-readable baseline label, e.g. "since Mon 04 May" when a day was skipped
    if prev:
        from datetime import datetime as _dt
        prev_date_obj = _dt.fromisoformat(prev["date"]).date()
        yesterday     = date.today() - timedelta(days=1)
        if prev_date_obj == yesterday:
            since_label = "yesterday"
        else:
            since_label = f"since {prev_date_obj.strftime('%a %d %b')}"
    else:
        since_label = "sprint start"

    # ── Effort delta ──────────────────────────────────────────────────────────
    today_spent_total = float(s79_tasks["Completed Work"].sum())
    prev_spent_total  = float(prev["spent_h"]) if prev else 0.0
    delta_today       = round(today_spent_total - prev_spent_total, 1)

    # Per-member delta
    prev_member = prev.get("per_member", {}) if prev else {}
    member_rows = []

    for _, t in s79_tasks.groupby("Assigned To"):
        pass  # just groupby below

    member_data = {}
    for _, t in s79_tasks.iterrows():
        name  = str(t.get("Assigned To", "Unassigned"))
        spent = float(t.get("Completed Work", 0) or 0)
        est   = float(t.get("Original Estimate", 0) or 0)
        state = str(t.get("State", ""))
        if name not in member_data:
            member_data[name] = {"spent": 0, "est": 0, "done": 0, "ip": 0, "todo": 0}
        member_data[name]["spent"] += spent
        member_data[name]["est"]   += est
        if state == "Done":     member_data[name]["done"] += 1
        elif state == "In Progress": member_data[name]["ip"] += 1
        else:                   member_data[name]["todo"] += 1

    # Expected daily hours per member
    expected_daily = {}
    for name, cap in cap_lookup.items():
        expected_daily[name] = round(cap / sprint_total_days, 1) if sprint_total_days else 0

    from sprint_dashboard_config import TEAMS
    TEAM_COLORS_DT = {"Calmers": "#6366f1", "Knackers": "#0891b2", "Crackers": "#16a34a"}

    alert_rows    = []
    member_rows   = []  # will contain both header and data rows
    no_hours_list = []
    under_list    = []

    def _member_row(name, data):
        # Cumulative spent so far this sprint (column main value + utilisation base)
        cum_h      = round(float(data.get("spent", 0)), 1)
        prev_spent = prev_member.get(name, {}).get("spent", 0.0) if prev_member else 0.0
        today_h    = round(cum_h - prev_spent, 1)             # delta vs yesterday
        exp_h      = expected_daily.get(name, 0)
        cap_h      = cap_lookup.get(name, 0)
        has_tasks  = (data.get("done", 0) + data.get("ip", 0) + data.get("todo", 0)) > 0
        util_pct   = round(cum_h / cap_h * 100) if cap_h else 0
        delta_chip_html = delta_chip(today_h)

        status_icon  = "✅"
        status_class = "color:#16a34a"
        if cap_h > 0 and not has_tasks:
            # Capacity allocated but member has no tasks — flagged in Discrepancies tab,
            # don't double-fire as a HIGH "no hours logged" alert here.
            status_icon  = "📭"
            status_class = "color:#64748b"
        elif today_h == 0 and sprint_day > 1 and has_tasks:
            status_icon  = "⚠️"
            status_class = "color:#dc2626"
            no_hours_list.append(name)
        elif today_h < exp_h * 0.5 and exp_h > 0 and sprint_day > 1 and has_tasks:
            status_icon  = "🔸"
            status_class = "color:#d97706"
            under_list.append((name, today_h, exp_h))

        bar_color = "#16a34a" if util_pct >= 80 else "#d97706" if util_pct >= 40 else "#dc2626"
        return (
            f'<tr style="border-bottom:1px solid #f1f5f9">'
            f'<td style="padding:8px 10px;font-size:12px;font-weight:500">'
            f'<span style="{status_class}">{status_icon}</span> {name}</td>'
            f'<td style="padding:8px 10px;text-align:center;font-size:12px;font-weight:700;color:#6366f1">'
            f'{cum_h}h {delta_chip_html}</td>'
            f'<td style="padding:8px 10px;text-align:center;font-size:11px;color:#64748b">{exp_h}h</td>'
            f'<td style="padding:8px 10px;text-align:center;font-size:11px">'
            f'{mini_bar(util_pct, bar_color)} <span style="font-size:10px;color:#64748b">{util_pct}%</span></td>'
            f'<td style="padding:8px 10px;text-align:center"><span style="background:#dcfce7;color:#16a34a;'
            f'padding:1px 6px;border-radius:8px;font-size:11px;font-weight:600">{data["done"]}</span></td>'
            f'<td style="padding:8px 10px;text-align:center"><span style="background:#fef3c7;color:#d97706;'
            f'padding:1px 6px;border-radius:8px;font-size:11px;font-weight:600">{data["ip"]}</span></td>'
            f'</tr>'
        )

    # Render every configured team member so 0-task / 0-hour members stay visible.
    seen_names = set()
    zero_data = {"spent": 0, "est": 0, "done": 0, "ip": 0, "todo": 0}
    for team, members in TEAMS.items():
        team_color = TEAM_COLORS_DT.get(team, "#6366f1")
        member_rows.append(
            f'<tr style="background:#f1f5f9">'
            f'<td colspan="6" style="padding:6px 10px;font-size:11px;font-weight:700;'
            f'color:{team_color};letter-spacing:0.5px">▸ {team.upper()}</td>'
            f'</tr>'
        )
        for name in members:
            seen_names.add(name)
            member_rows.append(_member_row(name, member_data.get(name, dict(zero_data))))

    # Any members not in a team (unassigned)
    unassigned = [n for n in sorted(member_data) if n not in seen_names]
    if unassigned:
        member_rows.append(
            '<tr style="background:#f1f5f9">'
            '<td colspan="6" style="padding:6px 10px;font-size:11px;font-weight:700;'
            'color:#64748b;letter-spacing:0.5px">▸ OTHER</td>'
            '</tr>'
        )
        for name in unassigned:
            member_rows.append(_member_row(name, member_data[name]))

    # ── Task movements ────────────────────────────────────────────────────────
    prev_task_states = prev.get("task_states", {}) if prev else {}
    moved_to_done  = []
    newly_started  = []
    for _, t in s79_tasks.iterrows():
        tid   = str(_tid(t.get("ID", 0)))
        state = str(t.get("State", ""))
        prev_state = prev_task_states.get(tid, {}).get("state", "") if prev_task_states else ""
        title = str(t.get("Title", ""))[:60]
        assignee = str(t.get("Assigned To", ""))

        if state == "Done" and prev_state != "Done" and prev:
            moved_to_done.append((tid, title, assignee))
        if state == "In Progress" and prev_state not in ("In Progress", "Done") and prev:
            newly_started.append((tid, title, assignee))

    def task_list_html(items, color):
        if not items:
            return '<span style="color:#94a3b8;font-size:12px">None today</span>'
        rows = ""
        for tid, title, assignee in items:
            rows += (f'<div style="display:flex;align-items:center;gap:8px;padding:5px 0;'
                     f'border-bottom:1px solid #f1f5f9">'
                     f'<span style="font-size:10px;color:#94a3b8">#{tid}</span>'
                     f'<span style="font-size:12px;flex:1">{title}</span>'
                     f'<span style="font-size:11px;color:#64748b">{assignee}</span>'
                     f'<span style="background:{color}22;color:{color};padding:1px 6px;'
                     f'border-radius:6px;font-size:10px;font-weight:600">✓</span></div>')
        return rows

    # ── Alerts ────────────────────────────────────────────────────────────────
    alert_items = []
    for name in no_hours_list:
        alert_items.append((f"🔴 No hours logged ({since_label})", name, "HIGH"))
    for name, got, exp in under_list:
        alert_items.append((f"🟡 Under target ({got}h vs {exp}h expected, {since_label})", name, "MEDIUM"))

    # Spike/drop detection
    if prev and len(snaps) >= 3:
        daily_history = []
        for s in snaps[-6:]:
            prev_s = None
            for ss in snaps:
                if ss["date"] < s["date"]:
                    prev_s = ss
            if prev_s:
                daily_history.append(s["spent_h"] - prev_s["spent_h"])
        if daily_history:
            avg = sum(daily_history) / len(daily_history)
            from sprint_dashboard_config import RISK
            if avg > 0:
                if delta_today > avg * (RISK["spike_threshold_pct"] / 100):
                    alert_items.append(("🔴 Effort spike detected", f"+{delta_today}h vs avg {round(avg,1)}h/day", "HIGH"))
                elif delta_today < avg * (RISK["drop_threshold_pct"] / 100) and sprint_day > 2:
                    alert_items.append(("🟡 Effort drop detected", f"{delta_today}h vs avg {round(avg,1)}h/day", "MEDIUM"))

    alert_html = ""
    if alert_items:
        for label, detail, level in alert_items:
            em = "🔴" if level == "HIGH" else "🟡"
            bg = "#fee2e2" if level == "HIGH" else "#fef3c7"
            fg = "#dc2626" if level == "HIGH" else "#d97706"
            alert_html += (f'<div style="background:{bg};border-left:3px solid {fg};'
                           f'padding:8px 12px;border-radius:4px;margin-bottom:6px;'
                           f'display:flex;justify-content:space-between;align-items:center">'
                           f'<div><span style="font-size:12px;font-weight:600;color:{fg}">{label}</span>'
                           f'<span style="font-size:11px;color:#64748b;margin-left:8px">{detail}</span></div>'
                           f'{risk_badge(level)}</div>')
    else:
        alert_html = '<div style="color:#16a34a;font-size:12px">✅ No alerts today</div>'

    # ── Trend sparkline (text-based) ──────────────────────────────────────────
    trend_html = ""
    if len(snaps) >= 2:
        trend_html = '<div style="display:flex;gap:8px;align-items:flex-end;margin-top:8px">'
        for snap in snaps[-7:]:
            ph = round(float(snap.get("spent_h", 0)))
            is_today = snap["date"] == today_str
            bg = "#6366f1" if is_today else "#c7d2fe"
            trend_html += (f'<div style="display:flex;flex-direction:column;align-items:center;gap:3px">'
                           f'<span style="font-size:9px;color:#64748b">{ph}h</span>'
                           f'<div style="width:22px;background:{bg};border-radius:3px 3px 0 0;'
                           f'height:{max(4, min(60, ph))}px"></div>'
                           f'<span style="font-size:9px;color:#94a3b8">'
                           f'D{snap.get("sprint_day","?")} </span></div>')
        trend_html += "</div>"

    # ── Summary header cards ──────────────────────────────────────────────────
    comp_pct = round(float(s79_tasks[s79_tasks["State"]=="Done"]["Completed Work"].sum()) /
                     today_spent_total * 100) if today_spent_total else 0

    # The dashboard runs once a day at the same time (10 AM by default).
    # Today's_snapshot − yesterday's_snapshot = previous day's logged hours,
    # so the delta card labels itself "Since Yesterday" — no run-time
    # detection needed. If the EM ad-hoc-runs the dashboard at a different
    # time, the delta still represents "hours logged since the previous run"
    # which on the standard cadence = previous-day logging.
    summary_html = f"""<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:10px;margin-bottom:16px">
  <div style="background:#fff;border:1px solid #e2e8f0;border-radius:10px;padding:12px 14px">
    <div style="font-size:22px;font-weight:800;color:#6366f1">+{delta_today}h</div>
    <div style="font-size:11px;color:#64748b;margin-top:2px">Logged Since Yesterday</div>
    <div style="font-size:10px;color:#94a3b8">{today_spent_total}h cumulative</div>
  </div>
  <div style="background:#fff;border:1px solid #e2e8f0;border-radius:10px;padding:12px 14px">
    <div style="font-size:22px;font-weight:800;color:#16a34a">{len(moved_to_done)}</div>
    <div style="font-size:11px;color:#64748b;margin-top:2px">Tasks → Done</div>
  </div>
  <div style="background:#fff;border:1px solid #e2e8f0;border-radius:10px;padding:12px 14px">
    <div style="font-size:22px;font-weight:800;color:#d97706">{len(newly_started)}</div>
    <div style="font-size:11px;color:#64748b;margin-top:2px">Tasks Started</div>
  </div>
  <div style="background:#fff;border:1px solid #e2e8f0;border-radius:10px;padding:12px 14px">
    <div style="font-size:22px;font-weight:800;color:#dc2626">{len(no_hours_list)}</div>
    <div style="font-size:11px;color:#64748b;margin-top:2px">No Hours Logged</div>
  </div>
  <div style="background:#fff;border:1px solid #e2e8f0;border-radius:10px;padding:12px 14px">
    <div style="font-size:22px;font-weight:800;color:#0891b2">{sprint_day}/{sprint_total_days}</div>
    <div style="font-size:11px;color:#64748b;margin-top:2px">Sprint Day</div>
    {trend_html}
  </div>
</div>"""

    member_table_html = f"""<table style="width:100%;border-collapse:collapse;font-size:12px">
<thead><tr style="background:#f8fafc">
  <th style="padding:8px 10px;text-align:left;color:#64748b;font-size:11px;border-bottom:1px solid #e2e8f0">Member</th>
  <th style="padding:8px 10px;text-align:center;color:#64748b;font-size:11px;border-bottom:1px solid #e2e8f0">Spent (+Today)</th>
  <th style="padding:8px 10px;text-align:center;color:#64748b;font-size:11px;border-bottom:1px solid #e2e8f0">Expected/day</th>
  <th style="padding:8px 10px;text-align:center;color:#64748b;font-size:11px;border-bottom:1px solid #e2e8f0">Utilisation</th>
  <th style="padding:8px 10px;text-align:center;color:#64748b;font-size:11px;border-bottom:1px solid #e2e8f0">Done</th>
  <th style="padding:8px 10px;text-align:center;color:#64748b;font-size:11px;border-bottom:1px solid #e2e8f0">In Progress</th>
</tr></thead>
<tbody>{"".join(member_rows)}</tbody>
</table>"""

    content = f"""{summary_html}
{section_card("🚨 Alerts", "🚨", alert_html, "#dc2626")}
{section_card("👥 Member Effort Tracking", "👥", member_table_html, "#6366f1")}
{section_card("✅ Tasks → Done Today", "✅", task_list_html(moved_to_done, "#16a34a"), "#16a34a")}
{section_card("▶️ Tasks Newly Started", "▶️", task_list_html(newly_started, "#d97706"), "#d97706")}"""

    return f'<div id="t-daily" class="tc">{content}</div>'


# ═══════════════════════════════════════════════════════════════════════════════
#  TAB 2 — DSM INSIGHTS
# ═══════════════════════════════════════════════════════════════════════════════

def build_dsm_tab(s79_tasks, history: dict, pbis: list, metrics: dict,
                  cap_lookup: dict, sprint_day: int, sprint_total_days: int):
    snaps     = history.get("snapshots", [])
    prev      = snaps[-2] if len(snaps) >= 2 else None
    prev_task = prev.get("task_states", {}) if prev else {}

    # Human-readable baseline label (same logic as Daily Tracking tab)
    if prev:
        from datetime import datetime as _dt, date as _date, timedelta as _td
        prev_date_obj = _dt.fromisoformat(prev["date"]).date()
        yesterday     = _date.today() - _td(days=1)
        since_label   = "yesterday" if prev_date_obj == yesterday else f"since {prev_date_obj.strftime('%a %d %b')}"
    else:
        since_label = "sprint start"

    # Phrase for the delta window (since the previous daily snapshot), so blocker
    # reasons read "…logged since yesterday" / "…since Tue 15 Jul" rather than the
    # misleading "today".
    window = ("since yesterday" if since_label == "yesterday"
              else "since sprint start" if since_label == "sprint start"
              else since_label)

    # Talking points
    points = []
    tasks_done_today = 0
    no_progress_members = []
    stuck_tasks = []

    from sprint_dashboard_config import TEAMS
    _roster_names, _canon = _roster_canonicaliser(TEAMS)
    # Canonical assignee column so all per-member/team lookups below match the
    # roster spelling regardless of the raw JIRA username format.
    s79_tasks = s79_tasks.copy()
    s79_tasks["_canon"] = s79_tasks["Assigned To"].map(lambda a: _canon(str(a)))

    member_activity = {}
    for _, t in s79_tasks.iterrows():
        tid     = str(_tid(t.get("ID", 0)))
        state   = str(t.get("State", ""))
        spent   = float(t.get("Completed Work", 0) or 0)
        # Attribute to the roster member (tolerant of dotted usernames); skip
        # anyone not on a team roster so non-members aren't reported.
        assignee = _canon(str(t.get("Assigned To", "")))
        if not assignee:
            continue
        title   = str(t.get("Title", ""))[:55]

        ps = prev_task.get(tid, {})
        prev_state = ps.get("state", "")
        prev_spent = ps.get("spent", 0.0) if ps else 0.0

        # Hours logged since the previous run (= previous day under the
        # standard 10 AM cadence).
        delta = round(spent - prev_spent, 1)

        if assignee not in member_activity:
            member_activity[assignee] = {"delta": 0, "done_today": 0, "started_today": 0}
        member_activity[assignee]["delta"] += delta
        if state == "Done" and prev_state != "Done" and prev:
            member_activity[assignee]["done_today"] += 1
            tasks_done_today += 1
        if state == "In Progress" and prev_state not in ("In Progress","Done") and prev:
            member_activity[assignee]["started_today"] += 1

        # Stuck: In Progress but no hours added since the previous run.
        if state == "In Progress" and sprint_day > 2:
            if delta == 0:
                stuck_tasks.append((tid, title, assignee))

    # Members with zero activity — roster members only (canonicalised).
    for _, t in s79_tasks.iterrows():
        name = _canon(str(t.get("Assigned To", "")))
        if name and name not in member_activity:
            member_activity[name] = {"delta": 0, "done_today": 0, "started_today": 0}

    for name, act in member_activity.items():
        if act["delta"] == 0 and sprint_day > 1 and prev:
            no_progress_members.append(name)

    # Auto talking points
    _total_tasks = metrics.get("total_tasks", 0) or 0
    completed_pct = round(metrics.get("tasks_done", 0) / _total_tasks * 100) if _total_tasks else 0
    if tasks_done_today > 0:
        points.append(f"✅ <b>{tasks_done_today} task(s)</b> completed today — good momentum")
    if completed_pct >= 50:
        points.append(f"📈 Sprint is <b>{completed_pct}% complete</b> by task count — on track")
    elif sprint_day >= sprint_total_days // 2 and completed_pct < 30:
        points.append(f"⚠️ Halfway through sprint but only <b>{completed_pct}% tasks done</b> — needs attention")

    remaining_days = sprint_total_days - sprint_day
    remaining_tasks = metrics.get("total_tasks", 0) - metrics.get("tasks_done", 0)
    if remaining_days > 0:
        tasks_per_day_needed = round(remaining_tasks / remaining_days, 1)
        avg_done_per_day = round(metrics.get("tasks_done", 0) / max(sprint_day, 1), 1)
        if tasks_per_day_needed > avg_done_per_day * 1.5:
            points.append(f"🔴 Need <b>{tasks_per_day_needed} tasks/day</b> to finish — current pace is {avg_done_per_day}/day")

    if not points:
        points.append("📊 Sprint progressing normally — no major blockers detected")

    points_html = "".join(
        f'<div style="padding:8px 0;border-bottom:1px solid #f1f5f9;font-size:12px;color:#1e293b">{p}</div>'
        for p in points
    )

    # Blockers section — grouped by person with context + task list
    blockers_html = ""
    if stuck_tasks:
        # Group by assignee
        stuck_by_person = {}
        for tid, title, assignee in stuck_tasks:
            stuck_by_person.setdefault(assignee, []).append((tid, title))

        for person, p_tasks in stuck_by_person.items():
            m_tasks  = s79_tasks[s79_tasks["_canon"] == person]
            m_spent  = float(m_tasks["Completed Work"].sum())
            m_est    = float(m_tasks["Original Estimate"].sum())
            m_done   = int((m_tasks["State"] == "Done").sum())
            m_total  = int(m_tasks.shape[0])

            # Check whether this person actually logged hours on OTHER tasks today.
            # member_activity[person]["delta"] is the total effort delta across ALL
            # their sprint tasks.  A person can have individual In-Progress tasks
            # with zero delta (task-level stall) while still having logged hours
            # elsewhere — they should NOT be labelled "0 hours logged today".
            person_total_delta = round(member_activity.get(person, {}).get("delta", 0.0), 1)

            if person_total_delta > 0:
                # Person is active today — specific tasks just had no new hours.
                reason       = (f"No progress on these tasks {window} "
                                f"(logged {person_total_delta}h on other tasks) — "
                                f"{m_done}/{m_total} tasks done, {m_spent:.0f}h/{m_est:.0f}h spent")
                badge_label  = "STALLED"
                badge_bg     = "#fef3c7"
                badge_color  = "#d97706"
                card_bg      = "#fffbeb"
                card_border  = "#fcd34d"
                row_divider  = "#fef3c7"
                reason_color = "#d97706"
            else:
                # Person has truly logged nothing today.
                reason       = (f"In Progress with 0 hours logged {window} — "
                                f"{m_done}/{m_total} tasks done, {m_spent:.0f}h/{m_est:.0f}h spent")
                badge_label  = "STUCK"
                badge_bg     = "#fee2e2"
                badge_color  = "#dc2626"
                card_bg      = "#fff5f5"
                card_border  = "#fca5a5"
                row_divider  = "#fee2e2"
                reason_color = "#dc2626"

            task_rows = ""
            for tid, title in p_tasks[:8]:
                t_df = m_tasks[m_tasks["ID"].astype(str) == str(tid)]
                est_h = float(t_df.iloc[0]["Original Estimate"]) if not t_df.empty and t_df.iloc[0]["Original Estimate"] else 0
                sp_h  = float(t_df.iloc[0]["Completed Work"])    if not t_df.empty and t_df.iloc[0]["Completed Work"]    else 0
                task_rows += (
                    f'<div style="padding:4px 0;border-bottom:1px solid {row_divider};'
                    f'font-size:11px;color:#374151;display:flex;gap:6px;align-items:center">'
                    f'<span style="color:#94a3b8;flex-shrink:0">#{tid}</span>'
                    f'<span style="flex:1">{title}</span>'
                    f'<span style="color:#94a3b8;flex-shrink:0">{sp_h:.0f}h / {est_h:.0f}h</span>'
                    f'</div>'
                )
            blockers_html += (
                f'<div style="background:{card_bg};border:1px solid {card_border};border-radius:8px;'
                f'padding:10px 12px;margin-bottom:8px">'
                f'<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:4px">'
                f'<span style="font-size:12px;font-weight:700;color:#1e293b">{person}</span>'
                f'<span style="background:{badge_bg};color:{badge_color};padding:1px 8px;border-radius:6px;'
                f'font-size:10px;font-weight:600">{badge_label}</span></div>'
                f'<div style="font-size:11px;color:{reason_color};margin-bottom:6px">📍 {reason}</div>'
                f'{task_rows}'
                f'</div>'
            )
    else:
        blockers_html = '<span style="color:#16a34a;font-size:12px">✅ No stuck tasks detected</span>'

    # Follow-up members — reason + task list
    followup_html = ""
    if no_progress_members and prev:
        for name in sorted(set(no_progress_members)):
            m_tasks  = s79_tasks[s79_tasks["_canon"] == name]
            m_spent  = float(m_tasks["Completed Work"].sum())
            m_cap    = cap_lookup.get(name, 0)
            active   = m_tasks[m_tasks["State"].isin(["In Progress", "To Do"])]

            task_rows = ""
            for _, t_row in active.head(5).iterrows():
                t_id    = _tid(t_row.get("ID", 0))
                t_title = str(t_row.get("Title", ""))[:55]
                t_state = str(t_row.get("State", ""))
                t_est   = float(t_row.get("Original Estimate", 0) or 0)
                t_sp    = float(t_row.get("Completed Work",    0) or 0)
                st_color = "#d97706" if t_state == "In Progress" else "#94a3b8"
                task_rows += (
                    f'<div style="padding:3px 0;border-bottom:1px solid #fde68a;'
                    f'font-size:11px;display:flex;gap:6px;align-items:center">'
                    f'<span style="color:#94a3b8;flex-shrink:0">#{t_id}</span>'
                    f'<span style="flex:1;color:#374151">{t_title}</span>'
                    f'<span style="color:{st_color};font-weight:600;flex-shrink:0">{t_state}</span>'
                    f'<span style="color:#94a3b8;flex-shrink:0">{t_sp:.0f}h/{t_est:.0f}h</span>'
                    f'</div>'
                )
            pending_hdr = ('<div style="font-size:10px;color:#92400e;font-weight:600;margin-bottom:3px">'
                           'Pending tasks:</div>' + task_rows) if task_rows else ""
            followup_html += (
                f'<div style="background:#fffbeb;border:1px solid #fde68a;border-radius:8px;'
                f'padding:10px 12px;margin-bottom:8px">'
                f'<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:4px">'
                f'<span style="font-size:12px;font-weight:700;color:#1e293b">{name}</span>'
                f'<span style="background:#fef3c7;color:#d97706;padding:1px 8px;border-radius:8px;'
                f'font-size:10px;font-weight:600">No update ({since_label})</span></div>'
                f'<div style="font-size:11px;color:#d97706;margin-bottom:6px">'
                f'📍 Reason: No hours logged {since_label} — {m_spent:.1f}h total spent vs {m_cap}h capacity</div>'
                f'{pending_hdr}'
                f'</div>'
            )
    else:
        followup_html = '<span style="color:#16a34a;font-size:12px">✅ Everyone has logged activity</span>'

    # Team-wise DSM summary cards
    from sprint_dashboard_config import TEAMS
    team_cards = '<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:10px;margin-bottom:14px">'
    for team, members in TEAMS.items():
        team_tasks  = s79_tasks[s79_tasks["_canon"].isin(members)]
        team_done   = int((team_tasks["State"] == "Done").sum())
        team_ip     = int((team_tasks["State"] == "In Progress").sum())
        team_todo   = int(team_tasks.shape[0]) - team_done - team_ip
        team_spent  = float(team_tasks["Completed Work"].sum())
        team_cap    = sum(cap_lookup.get(m, 0) for m in members)
        team_util   = round(team_spent / team_cap * 100) if team_cap else 0
        bar_color   = "#16a34a" if team_util >= 50 else "#d97706" if team_util >= 25 else "#dc2626"
        team_cards += (f'<div style="background:#fff;border:1px solid #e2e8f0;border-radius:10px;padding:12px 14px">'
                       f'<div style="font-size:13px;font-weight:700;color:#1e293b;margin-bottom:8px">Team {team}</div>'
                       f'<div style="display:flex;gap:10px;margin-bottom:6px">'
                       f'<span style="background:#dcfce7;color:#16a34a;padding:1px 7px;border-radius:8px;font-size:11px;font-weight:600">{team_done} done</span>'
                       f'<span style="background:#fef3c7;color:#d97706;padding:1px 7px;border-radius:8px;font-size:11px;font-weight:600">{team_ip} IP</span>'
                       f'<span style="background:#f1f5f9;color:#64748b;padding:1px 7px;border-radius:8px;font-size:11px;font-weight:600">{team_todo} todo</span>'
                       f'</div>'
                       f'<div style="font-size:11px;color:#64748b">{mini_bar(team_util, bar_color)} {team_util}% capacity used</div>'
                       f'</div>')
    team_cards += "</div>"

    content = f"""{team_cards}
{section_card("💡 Key Talking Points", "💡", points_html, "#6366f1")}
{section_card("🚧 Blockers (No Movement on Active Tasks)", "🚧", blockers_html, "#dc2626")}
{section_card("👀 Members to Follow Up", "👀", followup_html, "#d97706")}"""

    return f'<div id="t-dsm" class="tc">{content}</div>'


# ═══════════════════════════════════════════════════════════════════════════════
#  TAB 3 — RISK & HEALTH
# ═══════════════════════════════════════════════════════════════════════════════

def build_risk_health_tab(s79_tasks, history: dict, pbis: list, metrics: dict,
                          cap_lookup: dict, sprint_day: int, sprint_total_days: int,
                          sprint_start_date_str: str):
    snaps = history.get("snapshots", [])

    from sprint_dashboard_config import TEAMS, RISK, GOAL_DONE_STATES, INPROGRESS_STATES

    # Canonical assignee column so team rollups match roster names regardless of
    # the raw JIRA username format ('suyog.joshi' == 'Suyog Joshi').
    _roster_names, _canon = _roster_canonicaliser(TEAMS)
    s79_tasks = s79_tasks.copy()
    s79_tasks["_canon"] = s79_tasks["Assigned To"].map(lambda a: _canon(str(a)))

    remaining_days = max(sprint_total_days - sprint_day, 1)
    remaining_tasks_count = metrics["total_tasks"] - metrics["tasks_done"]
    capacity_used_pct = round(metrics["spent_h"] / metrics["est_h"] * 100) if metrics["est_h"] else 0
    time_elapsed_pct  = round(sprint_day / sprint_total_days * 100)

    # ── Pod-level risk ────────────────────────────────────────────────────────
    # Risk levels are escalated through this rank so we never rely on the
    # lexicographic order of "HIGH"/"MEDIUM"/"LOW" by accident.
    RISK_RANK = {"LOW": 0, "MEDIUM": 1, "HIGH": 2}
    def _bump(current: str, new: str) -> str:
        return new if RISK_RANK[new] > RISK_RANK[current] else current

    # Time elapsed signal — used by burn-rate check below. Computed once.
    time_elapsed_pct = round(sprint_day / sprint_total_days * 100) if sprint_total_days else 0
    pod_risks = []
    for team, members in TEAMS.items():
        team_tasks = s79_tasks[s79_tasks["_canon"].isin(members)]
        team_done  = int((team_tasks["State"] == "Done").sum())
        team_total = int(team_tasks.shape[0])
        team_spent = float(team_tasks["Completed Work"].sum())
        team_cap   = sum(cap_lookup.get(m, 0) for m in members)
        team_est   = float(team_tasks["Original Estimate"].sum())
        util_pct   = round(team_spent / team_cap * 100) if team_cap else 0
        completion = round(team_done / team_total * 100) if team_total else 0

        # Effort burn signal: how much of the planned work (estimate, NOT
        # capacity) has been consumed in hours so far. This is what TFS uses
        # for burndown. If effort_consumed_pct trails time_elapsed_pct by a
        # wide margin past Day 4, the team is behind pace and almost
        # certainly won't finish on time.
        effort_consumed_pct = round(team_spent / team_est * 100) if team_est else 0

        # Forecast capacity: how much work remains vs how much capacity remains.
        #   remaining_work  = sum(Original Estimate − Completed Work) over
        #                     non-Done tasks (clipped at zero per task).
        #   remaining_cap   = team_cap × (remaining_days / total_days) — i.e.
        #                     how much team capacity is left in the sprint.
        # If remaining_work > remaining_cap the sprint will overrun.
        non_done = team_tasks[team_tasks["State"] != "Done"]
        remaining_work = float(
            (non_done["Original Estimate"].astype(float)
             - non_done["Completed Work"].astype(float)
            ).clip(lower=0).sum()
        )
        remaining_cap = team_cap * (remaining_days / sprint_total_days) if sprint_total_days else 0
        forecast_overrun_pct = (
            round((remaining_work / remaining_cap - 1) * 100)
            if remaining_cap > 0 else 0
        )

        # Per-member: estimate over 120% of capacity (planning view).
        overloaded_members = [m for m in members
                               if cap_lookup.get(m, 0) > 0 and
                               s79_tasks[s79_tasks["_canon"]==m]["Original Estimate"].sum() >
                               cap_lookup.get(m, 0) * RISK["overloaded_pct"] / 100]
        # Per-member: spent already exceeds full sprint capacity (burning view).
        burning_members = [m for m in members
                           if cap_lookup.get(m, 0) > 0 and
                           float(s79_tasks[s79_tasks["_canon"]==m]
                                 ["Completed Work"].sum()) > cap_lookup.get(m, 0)]

        risks = []
        level = "LOW"

        # Existing checks ───────────────────────────────────────────
        if sprint_day >= 5 and util_pct < RISK["low_effort_pct_by_day5"]:
            risks.append(f"Low effort: only {util_pct}% capacity used by Day {sprint_day}")
            level = _bump(level, "HIGH")
        if sprint_day >= 6 and (100 - completion) > RISK["high_remaining_by_day6"]:
            risks.append(f"High remaining: {100-completion}% tasks not done by Day {sprint_day}")
            level = _bump(level, "HIGH")
        if overloaded_members:
            risks.append(f"Planned overload (>120% of cap by estimate): "
                         f"{', '.join(overloaded_members)}")
            level = _bump(level, "MEDIUM")

        # New: forecast capacity overrun ─────────────────────────────
        # Gate at Day 3+ so a fresh sprint isn't flagged before any logging
        # has happened. The check fires whenever remaining work won't fit
        # in remaining capacity.
        if (sprint_day >= 3 and remaining_cap > 0
                and remaining_work > remaining_cap):
            risks.append(
                f"Forecast overrun: {remaining_work:.0f}h work remaining vs "
                f"~{remaining_cap:.0f}h capacity remaining "
                f"({forecast_overrun_pct}% over) — won't finish at current scope")
            level = _bump(level, "HIGH")

        # New: burn-rate gap (behind pace) ───────────────────────────
        # On Day 4+ we expect effort_consumed to roughly track time_elapsed.
        # A gap of 15+ percentage points means the team is far behind pace
        # — they may be blocked, under-logging, or have over-committed.
        # Suppress when there's no meaningful estimate (team_est == 0).
        if (sprint_day >= 4 and team_est > 0
                and time_elapsed_pct - effort_consumed_pct >= 15):
            risks.append(
                f"Behind pace: {effort_consumed_pct}% of effort consumed vs "
                f"{time_elapsed_pct}% of sprint elapsed "
                f"(gap {time_elapsed_pct - effort_consumed_pct} pts)")
            level = _bump(level, "HIGH" if sprint_day >= 6 else "MEDIUM")

        # New: members already over their full sprint capacity by spent ──
        if burning_members:
            risks.append(f"Burning over capacity (spent > cap): "
                         f"{', '.join(burning_members)}")
            level = _bump(level, "HIGH")

        # New: team total spent already exceeds team capacity ─────────
        if team_cap > 0 and team_spent > team_cap:
            risks.append(
                f"Team total over capacity: {team_spent:.0f}h spent vs "
                f"{team_cap:.0f}h cap ({util_pct}%)")
            level = _bump(level, "HIGH")

        if not risks:
            risks = ["No significant risk detected"]
            level = "LOW"

        pod_risks.append((team, level, risks, completion, util_pct, team_done, team_total))

    pod_html = ""
    for team, level, risks, completion, util_pct, done, total in pod_risks:
        lvl_colors = {"HIGH": ("#fee2e2","#dc2626"), "MEDIUM": ("#fef3c7","#d97706"), "LOW": ("#dcfce7","#16a34a")}
        bg, fg = lvl_colors.get(level, ("#f1f5f9","#64748b"))
        pod_html += (f'<div style="background:#fff;border:1px solid {fg}55;border-radius:10px;'
                     f'padding:12px 14px;margin-bottom:8px">'
                     f'<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px">'
                     f'<span style="font-size:13px;font-weight:700;color:#1e293b">Team {team}</span>'
                     f'{risk_badge(level)}</div>'
                     f'<div style="display:flex;gap:12px;margin-bottom:6px">'
                     f'<span style="font-size:11px;color:#64748b">{done}/{total} tasks done</span>'
                     f'<span style="font-size:11px;color:#64748b">{util_pct}% effort used</span>'
                     f'{mini_bar(completion,"#6366f1")} <span style="font-size:10px;color:#94a3b8">{completion}%</span></div>'
                     f'<div style="background:{bg};border-radius:6px;padding:6px 10px">'
                     + "".join(f'<div style="font-size:11px;color:{fg};margin-bottom:2px">• {r}</div>' for r in risks)
                     + '</div></div>')

    # ── Predictive burn ───────────────────────────────────────────────────────
    tasks_to_do = s79_tasks[s79_tasks["State"] == "To Do"]
    at_risk_tasks = []
    for _, t in tasks_to_do.iterrows():
        est   = float(t.get("Original Estimate", 0) or 0)
        name  = str(t.get("Assigned To", ""))
        cap   = cap_lookup.get(name, 0)
        used  = float(s79_tasks[s79_tasks["_canon"]==name]["Completed Work"].sum())
        remaining_cap = cap - used
        title = str(t.get("Title", ""))[:55]
        tid   = _tid(t.get("ID", 0))
        if est > 0 and remaining_cap < est and remaining_cap >= 0:
            at_risk_tasks.append((tid, title, name, est, round(remaining_cap,1)))

    burn_html = ""
    if at_risk_tasks:
        burn_html = (
            '<table style="width:100%;border-collapse:collapse;font-size:12px;table-layout:fixed">'
            '<colgroup>'
            '<col style="width:40%"><col style="width:25%">'
            '<col style="width:8%"><col style="width:14%"><col style="width:13%">'
            '</colgroup>'
            '<thead><tr style="background:#f8fafc">'
            '<th style="padding:7px 10px;text-align:left;color:#64748b;font-size:11px;border-bottom:1px solid #e2e8f0">Task</th>'
            '<th style="padding:7px 10px;text-align:left;color:#64748b;font-size:11px;border-bottom:1px solid #e2e8f0">Assignee</th>'
            '<th style="padding:7px 10px;text-align:center;color:#64748b;font-size:11px;border-bottom:1px solid #e2e8f0">Est&nbsp;(h)</th>'
            '<th style="padding:7px 10px;text-align:center;color:#64748b;font-size:11px;border-bottom:1px solid #e2e8f0">Rem&nbsp;Cap</th>'
            '<th style="padding:7px 10px;text-align:center;color:#64748b;font-size:11px;border-bottom:1px solid #e2e8f0">Risk</th>'
            '</tr></thead><tbody>'
        )
        for tid, title, name, est, rem_cap in at_risk_tasks[:15]:
            gap   = round(est - rem_cap, 1)
            lvl   = "HIGH" if gap > 16 else "MEDIUM" if gap > 8 else "LOW"
            burn_html += (
                f'<tr style="border-bottom:1px solid #f1f5f9">'
                f'<td style="padding:6px 10px;font-size:11px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">'
                f'<span style="color:#94a3b8">#{tid}</span> {title}</td>'
                f'<td style="padding:6px 10px;font-size:11px;color:#64748b;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">{name}</td>'
                f'<td style="padding:6px 10px;text-align:center;font-size:11px;color:#6366f1;font-weight:600">{int(est)}h</td>'
                f'<td style="padding:6px 10px;text-align:center;font-size:11px;color:#dc2626;font-weight:600">{rem_cap}h</td>'
                f'<td style="padding:6px 10px;text-align:center">{risk_badge(lvl)}</td>'
                f'</tr>'
            )
        burn_html += "</tbody></table>"
    else:
        burn_html = '<span style="color:#16a34a;font-size:12px">✅ All tasks appear achievable within remaining capacity</span>'

    # ── Scope creep ───────────────────────────────────────────────────────────
    try:
        sprint_start = date.fromisoformat(sprint_start_date_str)
    except Exception:
        sprint_start = None

    scope_html = '<span style="color:#94a3b8;font-size:12px">Sprint start date not configured</span>'
    scope_level = "LOW"   # default until ≥1 prior snapshot enables scope-creep analysis
    if sprint_start and len(snaps) == 0:
        scope_html = '<span style="color:#94a3b8;font-size:12px">📸 Taking first snapshot today — scope creep tracking starts from this baseline.</span>'
    elif sprint_start and len(snaps) >= 1:
        first_snap        = snaps[0]
        first_task_states = first_snap.get("task_states", {})
        first_task_ids    = set(first_task_states.keys())
        current_task_ids  = set(str(_tid(t.get("ID", 0))) for _, t in s79_tasks.iterrows())
        new_ids           = current_task_ids - first_task_ids
        gone_ids          = first_task_ids   - current_task_ids

        # Build member → team lookup
        member_to_team = {}
        for _tname, _tmembers in TEAMS.items():
            for _m in _tmembers:
                member_to_team[_m] = _tname

        # ── Severity classification for added tasks ──────────────────────────
        # An added task is "at_risk" only when BOTH:
        #   (a) the assignee is overloaded -- their total committed Original
        #       Estimate exceeds capacity * RISK["overloaded_pct"]/100
        #   (b) the team is already behind on delivery -- task-completion %
        #       lags sprint-elapsed % by more than 15 pp (only meaningful
        #       from day 4 onward; pre-day-4 we treat any team as on pace)
        # If only one flag trips it's "watch" (amber, heads-up but not red).
        # If neither trips it's "absorbed" -- the addition fits within the
        # developer's bandwidth and doesn't threaten goal delivery, so we
        # render it in neutral blue rather than red.
        assignee_committed_h = {}
        for _, _row in s79_tasks.iterrows():
            _n = str(_row.get("Assigned To", ""))
            assignee_committed_h[_n] = (
                assignee_committed_h.get(_n, 0.0)
                + float(_row.get("Original Estimate", 0) or 0)
            )

        _elapsed_pct = (sprint_day / sprint_total_days * 100) if sprint_total_days else 0
        team_completion_pct = {}
        for _tname, _tmembers in TEAMS.items():
            _ttasks = s79_tasks[s79_tasks["_canon"].isin(_tmembers)]
            _ttotal = int(_ttasks.shape[0])
            _tdone  = int((_ttasks["State"] == "Done").sum()) if _ttotal else 0
            team_completion_pct[_tname] = (_tdone / _ttotal * 100) if _ttotal else 100.0

        def _classify_added(assignee, team):
            cap = float(cap_lookup.get(assignee, 0) or 0)
            committed = assignee_committed_h.get(assignee, 0.0)
            overloaded = (cap > 0) and (committed > cap * RISK["overloaded_pct"] / 100.0)
            has_bandwidth = not overloaded
            # Team-behind only matters once we're a few days into the sprint;
            # in the first three days completion % is naturally low.
            team_pct = team_completion_pct.get(team, 100.0)
            team_behind = (sprint_day >= 4) and (team_pct < _elapsed_pct - 15)
            if has_bandwidth and not team_behind:
                return "absorbed"
            if not has_bandwidth and team_behind:
                return "at_risk"
            return "watch"

        # Added tasks: read from current XLSX
        added_tasks = []   # (tid, title, assignee, team, est_h, severity)
        for _, t in s79_tasks.iterrows():
            tid = str(_tid(t.get("ID", 0)))
            if tid in new_ids:
                est      = float(t.get("Original Estimate", 0) or 0)
                assignee = str(t.get("Assigned To", ""))
                team     = member_to_team.get(assignee, "Other")
                title    = str(t.get("Title", ""))[:60]
                severity = _classify_added(assignee, team)
                added_tasks.append((tid, title, assignee, team, est, severity))

        # Removed tasks: read from first snapshot's task_states.
        # Sixth tuple slot (severity) is unused for removals -- they don't
        # need triage -- but kept so add/remove tuples stay symmetric.
        removed_tasks = []
        for tid in gone_ids:
            ts       = first_task_states.get(tid, {})
            est      = float(ts.get("est", 0) or 0)
            assignee = str(ts.get("assignee", ""))
            team     = member_to_team.get(assignee, "Other")
            title    = str(ts.get("title", ""))[:60]
            removed_tasks.append((tid, title, assignee, team, est, "n/a"))

        # Team-wise aggregation, split by severity so the table can render
        # absorbed additions in neutral and only flag the concerning portion.
        team_added_h          = {}     # all added hours
        team_added_h_atrisk   = {}     # only watch + at_risk
        team_added_h_hard     = {}     # only at_risk (hard red)
        team_removed_h        = {}
        all_teams_seen = set()
        for _, _, _, team, est, sev in added_tasks:
            team_added_h[team] = team_added_h.get(team, 0.0) + est
            if sev != "absorbed":
                team_added_h_atrisk[team] = team_added_h_atrisk.get(team, 0.0) + est
            if sev == "at_risk":
                team_added_h_hard[team]   = team_added_h_hard.get(team, 0.0) + est
            all_teams_seen.add(team)
        for _, _, _, team, est, _ in removed_tasks:
            team_removed_h[team] = team_removed_h.get(team, 0.0) + est
            all_teams_seen.add(team)

        total_added_h        = sum(team_added_h.values())
        total_added_h_atrisk = sum(team_added_h_atrisk.values())
        total_added_h_hard   = sum(team_added_h_hard.values())
        total_added_absorbed = total_added_h - total_added_h_atrisk
        cnt_at_risk          = sum(1 for *_ , s in added_tasks if s == "at_risk")
        cnt_watch            = sum(1 for *_ , s in added_tasks if s == "watch")
        cnt_absorbed         = sum(1 for *_ , s in added_tasks if s == "absorbed")
        total_removed_h = sum(team_removed_h.values())
        net_h           = total_added_h - total_removed_h
        scope_pct       = round(len(new_ids) / max(len(first_task_ids), 1) * 100, 1)
        # Scope-creep severity is now driven by impact, not just count:
        # HIGH only if there are genuinely at-risk additions; MEDIUM if any
        # additions need watching; LOW when everything fits within capacity.
        if total_added_h_hard > 0:
            scope_level = "HIGH"
        elif total_added_h_atrisk > 0:
            scope_level = "MEDIUM"
        else:
            scope_level = "LOW"
        net_color       = "#dc2626" if net_h > 0 else "#16a34a" if net_h < 0 else "#64748b"
        # Color of the "Tasks Added" summary card subtitle: red only when
        # there's a real risk; neutral blue when everything is absorbable.
        added_subtitle_color = "#dc2626" if total_added_h_hard > 0 else \
                               "#d97706" if total_added_h_atrisk > 0 else "#0891b2"

        # ── Summary cards ─────────────────────────────────────────────────────
        # Soften the "Tasks Added" card when nothing is at-risk. A neutral
        # blue background reads as "noted, not alarming" -- which matches
        # the rule: don't flag additions when developers have bandwidth and
        # delivery isn't threatened.
        if total_added_h_hard > 0:
            added_card_bg, added_card_brd = "#fff5f5", "#fca5a5"
            added_count_color = "#dc2626"
        elif total_added_h_atrisk > 0:
            added_card_bg, added_card_brd = "#fffbeb", "#fde68a"
            added_count_color = "#d97706"
        else:
            added_card_bg, added_card_brd = "#eff6ff", "#bfdbfe"
            added_count_color = "#0891b2"

        # Subtitle now shows the absorbed/at-risk split when meaningful.
        if total_added_h_atrisk > 0:
            added_subtitle = (
                f'+{total_added_h:.1f}h estimated · '
                f'<span style="color:#dc2626">{total_added_h_atrisk:.1f}h at risk</span>'
            )
        elif total_added_h > 0:
            added_subtitle = f'+{total_added_h:.1f}h estimated · within capacity'
        else:
            added_subtitle = "no additions yet"

        scope_html = (
            f'<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(110px,1fr));'
            f'gap:10px;margin-bottom:16px">'
            f'<div style="background:{added_card_bg};border:1px solid {added_card_brd};border-radius:10px;padding:12px 14px">'
            f'  <div style="font-size:22px;font-weight:800;color:{added_count_color}">{len(added_tasks)}</div>'
            f'  <div style="font-size:11px;color:#64748b;margin-top:2px;font-weight:500">Tasks Added</div>'
            f'  <div style="font-size:10px;color:{added_subtitle_color};margin-top:2px;font-weight:600">{added_subtitle}</div>'
            f'</div>'
            f'<div style="background:#f0fdf4;border:1px solid #86efac;border-radius:10px;padding:12px 14px">'
            f'  <div style="font-size:22px;font-weight:800;color:#16a34a">{len(removed_tasks)}</div>'
            f'  <div style="font-size:11px;color:#64748b;margin-top:2px;font-weight:500">Tasks Removed</div>'
            f'  <div style="font-size:10px;color:#16a34a;margin-top:2px;font-weight:600">-{total_removed_h:.1f}h freed</div>'
            f'</div>'
            f'<div style="background:#fff;border:1px solid #e2e8f0;border-radius:10px;padding:12px 14px">'
            f'  <div style="font-size:22px;font-weight:800;color:{net_color}">'
            f'    {("+" if net_h >= 0 else "")}{net_h:.1f}h</div>'
            f'  <div style="font-size:11px;color:#64748b;margin-top:2px;font-weight:500">Net Hour Impact</div>'
            f'  <div style="font-size:10px;color:#94a3b8;margin-top:2px">{scope_pct}% scope change</div>'
            f'</div>'
            f'<div style="background:#fef3c7;border:1px solid #d9770644;border-radius:10px;'
            f'padding:12px 14px;display:flex;flex-direction:column;justify-content:center">'
            f'  <div style="font-size:11px;font-weight:600;color:#d97706;margin-bottom:4px">Scope Creep Level</div>'
            f'  {risk_badge(scope_level)}'
            f'</div>'
            f'</div>'
            # Brief inline legend so the next operator immediately understands
            # what the colors mean. Suppressed when there's nothing to explain.
            + (
                f'<div style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:8px;'
                f'padding:8px 12px;margin-bottom:14px;font-size:11px;color:#64748b;line-height:1.5">'
                f'<b>How additions are scored:</b> an added task is flagged only when both '
                f'(a) the assignee\'s committed estimate already exceeds {RISK["overloaded_pct"]}% '
                f'of their capacity <i>and</i> (b) the team is behind on completion vs sprint '
                f'elapsed. Otherwise it\'s within bandwidth and the panel stays neutral. '
                f'<span style="color:#0891b2">● {cnt_absorbed} absorbed</span> · '
                f'<span style="color:#d97706">● {cnt_watch} watch</span> · '
                f'<span style="color:#dc2626">● {cnt_at_risk} at risk</span></div>'
                if added_tasks else ""
            )
        )

        # ── Team-wise hour impact table ────────────────────────────────────────
        if all_teams_seen:
            scope_html += (
                '<div style="margin-bottom:16px">'
                '<div style="font-size:12px;font-weight:700;color:#1e293b;margin-bottom:8px">'
                '📊 Team-wise Hour Impact</div>'
                '<table style="width:100%;border-collapse:collapse;font-size:12px;table-layout:fixed">'
                '<colgroup>'
                # Added an extra "At Risk (h)" column so the absorbable bulk
                # of additions stays neutral and only the concerning slice
                # is highlighted in red.
                '<col style="width:20%"><col style="width:13%"><col style="width:11%">'
                '<col style="width:12%"><col style="width:14%"><col style="width:13%">'
                '<col style="width:17%">'
                '</colgroup>'
                '<thead><tr style="background:#f8fafc">'
                '<th style="padding:7px 10px;text-align:left;color:#64748b;font-size:11px;'
                'border-bottom:1px solid #e2e8f0">Team</th>'
                '<th style="padding:7px 10px;text-align:center;color:#0891b2;font-size:11px;'
                'border-bottom:1px solid #e2e8f0">+ Added (h)</th>'
                '<th style="padding:7px 10px;text-align:center;color:#dc2626;font-size:11px;'
                'border-bottom:1px solid #e2e8f0">At Risk (h)</th>'
                '<th style="padding:7px 10px;text-align:center;color:#64748b;font-size:11px;'
                'border-bottom:1px solid #e2e8f0">Tasks Added</th>'
                '<th style="padding:7px 10px;text-align:center;color:#16a34a;font-size:11px;'
                'border-bottom:1px solid #e2e8f0">− Removed (h)</th>'
                '<th style="padding:7px 10px;text-align:center;color:#16a34a;font-size:11px;'
                'border-bottom:1px solid #e2e8f0">Tasks Removed</th>'
                '<th style="padding:7px 10px;text-align:center;color:#6366f1;font-size:11px;'
                'border-bottom:1px solid #e2e8f0">Net Impact</th>'
                '</tr></thead><tbody>'
            )
            # per-team task counts (overall + at-risk subset)
            team_added_cnt          = {}
            team_added_cnt_atrisk   = {}
            team_removed_cnt        = {}
            for _, _, _, team, _, sev in added_tasks:
                team_added_cnt[team] = team_added_cnt.get(team, 0) + 1
                if sev != "absorbed":
                    team_added_cnt_atrisk[team] = team_added_cnt_atrisk.get(team, 0) + 1
            for _, _, _, team, _, _ in removed_tasks:
                team_removed_cnt[team] = team_removed_cnt.get(team, 0) + 1

            # Sort by team name; put known teams first in config order
            known_order = list(TEAMS.keys())
            def _team_sort(t):
                return known_order.index(t) if t in known_order else len(known_order)
            for team in sorted(all_teams_seen, key=_team_sort):
                a       = team_added_h.get(team, 0.0)
                a_risk  = team_added_h_atrisk.get(team, 0.0)
                a_hard  = team_added_h_hard.get(team, 0.0)
                r       = team_removed_h.get(team, 0.0)
                n       = a - r
                ac      = team_added_cnt.get(team, 0)
                ac_risk = team_added_cnt_atrisk.get(team, 0)
                rc      = team_removed_cnt.get(team, 0)
                # "+ Added (h)" cell color now reflects this team's mix:
                # red when there are at-risk additions, amber when only
                # "watch", neutral blue when everything is absorbed.
                if a_hard > 0:
                    a_color = "#dc2626"
                elif a_risk > 0:
                    a_color = "#d97706"
                elif a > 0:
                    a_color = "#0891b2"
                else:
                    a_color = "#94a3b8"
                # Net impact: only red when the net add is at-risk hours.
                if n <= 0:
                    nc = "#16a34a" if n < 0 else "#64748b"
                else:
                    nc = "#dc2626" if a_hard > 0 else "#d97706" if a_risk > 0 else "#0891b2"
                # "At Risk (h)" cell — emphasise only when > 0.
                if a_risk > 0:
                    risk_cell = (
                        f'<span style="background:#fee2e2;color:#dc2626;padding:1px 7px;'
                        f'border-radius:8px;font-size:11px;font-weight:700">'
                        f'{a_risk:.1f}h · {ac_risk}</span>'
                    )
                else:
                    risk_cell = '<span style="color:#94a3b8;font-size:11px">—</span>'
                scope_html += (
                    f'<tr style="border-bottom:1px solid #f1f5f9">'
                    f'<td style="padding:6px 10px;font-size:12px;font-weight:600;color:#1e293b">{team}</td>'
                    f'<td style="padding:6px 10px;text-align:center;font-size:12px;color:{a_color};font-weight:600">'
                    f'{"+" if a>0 else ""}{a:.1f}h</td>'
                    f'<td style="padding:6px 10px;text-align:center">{risk_cell}</td>'
                    f'<td style="padding:6px 10px;text-align:center;font-size:12px;color:#64748b">{ac}</td>'
                    f'<td style="padding:6px 10px;text-align:center;font-size:12px;color:#16a34a">'
                    f'{"-" if r>0 else ""}{r:.1f}h</td>'
                    f'<td style="padding:6px 10px;text-align:center;font-size:12px;color:#64748b">{rc}</td>'
                    f'<td style="padding:6px 10px;text-align:center;font-size:12px;font-weight:700;color:{nc}">'
                    f'{("+" if n>=0 else "")}{n:.1f}h</td></tr>'
                )
            # Totals row
            tn  = total_added_h - total_removed_h
            if tn <= 0:
                tnc = "#16a34a" if tn < 0 else "#64748b"
            else:
                tnc = "#dc2626" if total_added_h_hard > 0 else \
                      "#d97706" if total_added_h_atrisk > 0 else "#0891b2"
            ta_color = "#dc2626" if total_added_h_hard > 0 else \
                       "#d97706" if total_added_h_atrisk > 0 else \
                       "#0891b2" if total_added_h > 0 else "#94a3b8"
            if total_added_h_atrisk > 0:
                total_risk_cell = (
                    f'<span style="background:#fee2e2;color:#dc2626;padding:1px 8px;'
                    f'border-radius:8px;font-size:12px;font-weight:800">'
                    f'{total_added_h_atrisk:.1f}h · {cnt_at_risk}</span>'
                )
            else:
                total_risk_cell = '<span style="color:#94a3b8;font-size:12px">—</span>'
            scope_html += (
                f'<tr style="background:#f8fafc;border-top:2px solid #e2e8f0">'
                f'<td style="padding:7px 10px;font-size:12px;font-weight:800;color:#1e293b">Total</td>'
                f'<td style="padding:7px 10px;text-align:center;font-size:12px;font-weight:700;color:{ta_color}">'
                f'{"+" if total_added_h>0 else ""}{total_added_h:.1f}h</td>'
                f'<td style="padding:7px 10px;text-align:center">{total_risk_cell}</td>'
                f'<td style="padding:7px 10px;text-align:center;font-size:12px;font-weight:700;color:#64748b">'
                f'{len(added_tasks)}</td>'
                f'<td style="padding:7px 10px;text-align:center;font-size:12px;font-weight:700;color:#16a34a">'
                f'{"-" if total_removed_h>0 else ""}{total_removed_h:.1f}h</td>'
                f'<td style="padding:7px 10px;text-align:center;font-size:12px;font-weight:700;color:#64748b">'
                f'{len(removed_tasks)}</td>'
                f'<td style="padding:7px 10px;text-align:center;font-size:12px;font-weight:800;color:{tnc}">'
                f'{("+" if tn>=0 else "")}{tn:.1f}h</td></tr>'
                '</tbody></table></div>'
            )

        # ── Individual Task Detail ─────────────────────────────────────────────
        # Per-task badges now reflect severity, not a blanket red "+added".
        # Sort: at_risk first, then watch, then absorbed; within each by hours desc.
        if added_tasks or removed_tasks:
            scope_html += (
                '<div style="font-size:12px;font-weight:700;color:#1e293b;margin-bottom:8px">'
                '🔍 Individual Task Detail</div>'
                '<div style="overflow-x:auto">'
                '<table style="width:100%;border-collapse:collapse;font-size:12px">'
                '<thead><tr style="background:#f8fafc">'
                '<th style="padding:7px 8px;text-align:left;color:#64748b;font-size:11px;'
                'border-bottom:1px solid #e2e8f0">Change</th>'
                '<th style="padding:7px 8px;text-align:left;color:#64748b;font-size:11px;'
                'border-bottom:1px solid #e2e8f0">Task</th>'
                '<th style="padding:7px 8px;text-align:left;color:#64748b;font-size:11px;'
                'border-bottom:1px solid #e2e8f0">Assignee</th>'
                '<th style="padding:7px 8px;text-align:left;color:#64748b;font-size:11px;'
                'border-bottom:1px solid #e2e8f0">Team</th>'
                '<th style="padding:7px 8px;text-align:center;color:#64748b;font-size:11px;'
                'border-bottom:1px solid #e2e8f0">Est. Hours</th>'
                '</tr></thead><tbody>'
            )
            _badge_style = {
                "at_risk":  ("+at risk",  "#fee2e2", "#dc2626"),
                "watch":    ("+watch",    "#fef3c7", "#d97706"),
                "absorbed": ("+absorbed", "#e0f2fe", "#0891b2"),
            }
            _sev_rank = {"at_risk": 0, "watch": 1, "absorbed": 2}
            for tid, title, assignee, team, est_h, sev in sorted(
                added_tasks,
                key=lambda x: (_sev_rank.get(x[5], 3), -x[4])
            ):
                label, bg, fg = _badge_style.get(sev, _badge_style["watch"])
                scope_html += (
                    f'<tr style="border-bottom:1px solid #f1f5f9">'
                    f'<td style="padding:5px 8px;white-space:nowrap">'
                    f'<span style="background:{bg};color:{fg};padding:1px 7px;'
                    f'border-radius:8px;font-size:10px;font-weight:700">{label}</span></td>'
                    f'<td style="padding:5px 8px;font-size:11px">'
                    f'<span style="color:#94a3b8">#{tid}</span> {str(title)[:60]}</td>'
                    f'<td style="padding:5px 8px;font-size:11px;color:#64748b;white-space:nowrap">{assignee}</td>'
                    f'<td style="padding:5px 8px;font-size:11px;color:#6366f1;font-weight:600;white-space:nowrap">{team}</td>'
                    f'<td style="padding:5px 8px;text-align:center;font-size:11px;'
                    f'font-weight:700;color:{fg}">+{est_h:.1f}h</td></tr>'
                )
            for tid, title, assignee, team, est_h, _ in sorted(removed_tasks, key=lambda x: -x[4]):
                scope_html += (
                    f'<tr style="border-bottom:1px solid #f1f5f9;background:#fafafa">'
                    f'<td style="padding:5px 8px;white-space:nowrap">'
                    f'<span style="background:#dcfce7;color:#16a34a;padding:1px 7px;'
                    f'border-radius:8px;font-size:10px;font-weight:700">−removed</span></td>'
                    f'<td style="padding:5px 8px;font-size:11px;color:#94a3b8">'
                    f'<span style="color:#c4c8d0">#{tid}</span> {str(title)[:60]}</td>'
                    f'<td style="padding:5px 8px;font-size:11px;color:#94a3b8;white-space:nowrap">{assignee}</td>'
                    f'<td style="padding:5px 8px;font-size:11px;color:#94a3b8;white-space:nowrap">{team}</td>'
                    f'<td style="padding:5px 8px;text-align:center;font-size:11px;'
                    f'font-weight:700;color:#16a34a">-{est_h:.1f}h</td></tr>'
                )
            scope_html += '</tbody></table></div>'

    # ── Weekly Health Check ─────────────────────────────────────────────
    health_items = []

    # Effort alignment: capacity_used_pct vs time_elapsed_pct
    eff_gap = capacity_used_pct - time_elapsed_pct
    if abs(eff_gap) <= 15:
        health_items.append(("✅", "Effort Alignment",
                             f"On track ({capacity_used_pct}% effort, {time_elapsed_pct}% time elapsed)",
                             "LOW"))
    elif eff_gap < -15:
        health_items.append(("⚠️", "Effort Alignment",
                             f"Behind on effort ({capacity_used_pct}% spent vs {time_elapsed_pct}% time elapsed)",
                             "MEDIUM"))
    else:
        health_items.append(("⚠️", "Effort Alignment",
                             f"Burning fast ({capacity_used_pct}% spent vs {time_elapsed_pct}% time elapsed)",
                             "MEDIUM"))

    # Task velocity: done/day so far vs needed/day to finish
    done_so_far = metrics.get("tasks_done", 0)
    pace_done   = round(done_so_far / sprint_day, 1) if sprint_day else 0
    pace_needed = round(remaining_tasks_count / remaining_days, 1) if remaining_days else 0
    if pace_needed == 0 or pace_done >= pace_needed * 0.9:
        health_items.append(("✅", "Task Velocity",
                             f"Pace healthy: {pace_done} done/day, need {pace_needed}/day",
                             "LOW"))
    elif pace_done >= pace_needed * 0.6:
        health_items.append(("⚠️", "Task Velocity",
                             f"Pace low: {pace_done} done/day, need {pace_needed}/day",
                             "MEDIUM"))
    else:
        health_items.append(("⚠️", "Task Velocity",
                             f"Pace critical: {pace_done} done/day, need {pace_needed}/day",
                             "HIGH"))

    # Allocation: any overloaded members?
    overloaded = []
    for m, cap in cap_lookup.items():
        if cap and cap > 0:
            committed = float(s79_tasks[s79_tasks["_canon"] == m]["Original Estimate"].sum())
            if committed > cap * RISK["overloaded_pct"] / 100:
                overloaded.append(m)
    if overloaded:
        health_items.append(("⚠️", "Allocation",
                             f"Overloaded: {', '.join(overloaded)}",
                             "HIGH" if len(overloaded) > 1 else "MEDIUM"))
    else:
        health_items.append(("✅", "Allocation", "All members within capacity", "LOW"))

    # Data quality: tasks with no estimate
    no_est = int((s79_tasks["Original Estimate"] == 0).sum()) if not s79_tasks.empty else 0
    if no_est == 0:
        health_items.append(("✅", "Data Quality", "All tasks estimated", "LOW"))
    elif no_est < 10:
        health_items.append(("⚠️", "Data Quality", f"{no_est} tasks with no estimate", "LOW"))
    else:
        health_items.append(("⚠️", "Data Quality", f"{no_est} tasks with no estimate", "MEDIUM"))

    health_html = ""
    for icon, title, detail, lvl in health_items:
        health_html += (
            f'<div style="display:flex;align-items:flex-start;gap:10px;'
            f'padding:8px 0;border-bottom:1px solid #f1f5f9">'
            f'<span style="font-size:16px">{icon}</span>'
            f'<div style="flex:1">'
            f'<div style="font-size:12px;font-weight:600;color:#1e293b">{title}</div>'
            f'<div style="font-size:11px;color:#64748b;margin-top:2px">{detail}</div>'
            f'</div>{risk_badge(lvl)}</div>'
        )

    # ── Overall Sprint Risk ─────────────────────────────────────────────
    # Aggregate the worst level across pod_risks, burn (at-risk count),
    # scope (already classified), and the health items above.
    all_levels = []
    all_levels += [p[1] for p in pod_risks]
    if at_risk_tasks:
        # any HIGH-gap task pushes overall to HIGH
        if any((est - rem) > 16 for _, _, _, est, rem in at_risk_tasks):
            all_levels.append("HIGH")
        elif any((est - rem) > 8 for _, _, _, est, rem in at_risk_tasks):
            all_levels.append("MEDIUM")
        else:
            all_levels.append("LOW")
    all_levels.append(scope_level)
    all_levels += [h[3] for h in health_items]

    rank = {"HIGH": 2, "MEDIUM": 1, "LOW": 0}
    overall = max(all_levels, key=lambda x: rank.get(x, 0)) if all_levels else "LOW"
    overall_colors = {
        "HIGH":   ("#fee2e2", "#dc2626"),
        "MEDIUM": ("#fef3c7", "#d97706"),
        "LOW":    ("#dcfce7", "#16a34a"),
    }
    ob, of = overall_colors.get(overall, ("#f1f5f9", "#64748b"))

    days_left = max(sprint_total_days - sprint_day, 0)

    header_card = (
        f'<div style="background:{ob};border:1px solid {of}55;border-radius:12px;'
        f'padding:16px 20px;margin-bottom:16px;display:flex;'
        f'justify-content:space-between;align-items:center">'
        f'<div>'
        f'<div style="font-size:16px;font-weight:800;color:{of}">Overall Sprint Risk: {overall}</div>'
        f'<div style="font-size:12px;color:#64748b;margin-top:4px">'
        f'Day {sprint_day} of {sprint_total_days} · {remaining_tasks_count} tasks remaining · '
        f'{days_left} days left</div>'
        f'</div>{risk_badge(overall)}</div>'
    )

    content = (
        header_card
        + section_card("Pod Risk Assessment", "🏃", pod_html, accent="#6366f1")
        + section_card("Burn Analysis — Tasks at Risk", "🔥", burn_html, accent="#dc2626")
        + section_card("Scope Creep — Team & Task Level Detail", "📈", scope_html, accent="#0891b2")
        + section_card("Weekly Health Check", "🏥", health_html, accent="#16a34a")
    )

    return f'<div id="t-risk" class="tc">{content}</div>'
