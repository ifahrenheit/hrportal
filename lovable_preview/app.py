"""
Isolated design preview — NOT part of the production leavesystem app.

Renders a mock-data dashboard that adapts the "HR Portal Refresh" Lovable
design onto the portal's existing look (same CSS custom properties / fonts
as templates/base.html), so it can be compared side-by-side with the real
/dashboard page.

This script:
  - does not import app.py, db_core.py, or any production module
  - does not touch orangehrm2 or central_db
  - does not require login / Keycloak
  - runs on its own port (5057), separate from the production app (5001)

Run it with:
    /var/www/html/leavesystem/venv/bin/python lovable_preview/app.py

Then view http://127.0.0.1:5057/ (tunnel over SSH if browsing from your
own machine — see the README in this folder).
"""
from flask import Flask, render_template
from datetime import date

app = Flask(__name__)

STATS = [
    {"label": "Total employees", "value": "248", "detail": "+12 this month", "note": "vs. last month", "icon": "users", "tone": "mint", "spark": "up"},
    {"label": "Present today", "value": "231", "detail": "93.1% attendance", "note": "Looking good today", "icon": "clock", "tone": "blue", "spark": "flat"},
    {"label": "On leave", "value": "9", "detail": "6 planned · 3 sick", "note": "Across all departments", "icon": "calendar", "tone": "amber", "spark": "down"},
    {"label": "Pending requests", "value": "8", "detail": "5 leave · 3 attendance", "note": "Awaiting review", "icon": "file-text", "tone": "rose", "spark": "flat"},
]

ATTENDANCE = {
    "weighted": "93.1", "delta": "2.4%", "range_label": "Oct 19 – 23, 2026",
    "days": [
        {"label": "Mon", "present": 90, "leave": 10, "absent": 5},
        {"label": "Tue", "present": 87, "leave": 12, "absent": 4},
        {"label": "Wed", "present": 94, "leave": 7, "absent": 6},
        {"label": "Thu", "present": 85, "leave": 13, "absent": 4},
        {"label": "Fri", "present": 96, "leave": 6, "absent": 3},
    ],
}

EVENTS = [
    {"icon": "gift", "tone": "amber", "title": "Sofia's birthday", "sub": "Marketing team", "when": "Tomorrow"},
    {"icon": "briefcase-business", "tone": "blue", "title": "All-hands meeting", "sub": "2:00 PM · Main conference room", "when": "Oct 23"},
]

EMPLOYEES = [
    {"initials": "IS", "tone": "mint", "name": "Isabella Santos", "role": "Product Designer", "department": "Design", "status": "Present", "time": "8:52 AM"},
    {"initials": "MR", "tone": "blue", "name": "Miguel Reyes", "role": "Software Engineer", "department": "Engineering", "status": "Present", "time": "8:45 AM"},
    {"initials": "SC", "tone": "rose", "name": "Sofia Cruz", "role": "Marketing Specialist", "department": "Marketing", "status": "On leave", "time": "—"},
    {"initials": "DG", "tone": "amber", "name": "Daniel Garcia", "role": "HR Specialist", "department": "People", "status": "Present", "time": "8:58 AM"},
    {"initials": "OR", "tone": "blue", "name": "Olivia Ramos", "role": "Account Executive", "department": "Sales", "status": "Late", "time": "9:24 AM"},
    {"initials": "EL", "tone": "mint", "name": "Ethan Lim", "role": "Frontend Developer", "department": "Engineering", "status": "Present", "time": "8:50 AM"},
]

LEAVE_REQUESTS = [
    {"emp": EMPLOYEES[0], "type": "Annual leave", "dates": "Oct 22 – 23, 2026", "status": "Pending"},
    {"emp": EMPLOYEES[1], "type": "Annual leave", "dates": "Oct 23 – 24, 2026", "status": "Pending"},
    {"emp": EMPLOYEES[2], "type": "Sick leave", "dates": "Oct 24 – 25, 2026", "status": "Pending"},
    {"emp": EMPLOYEES[3], "type": "Annual leave", "dates": "Oct 25 – 26, 2026", "status": "Pending"},
    {"emp": EMPLOYEES[4], "type": "Annual leave", "dates": "Oct 26 – 27, 2026", "status": "Pending"},
]

NOTICES = [
    {"icon": "clock-alert", "title": "3 tardiness cases pending review", "sub": "Current payroll period"},
    {"icon": "inbox", "title": "8 requests awaiting approval", "sub": "Leave, OT, CWS, RDW, FTS"},
]

# Illustrative only — grounded in the real section labels from
# templates/nav/_sidebar.html, condensed into groups. The real version would
# render only the groups/items the signed-in user has permission for (same
# has_permission() checks already used per-link in _sidebar.html).
NAV_GROUPS = [
    {"key": "people", "label": "People", "icon": "users", "links": [
        {"label": "Employee Database", "icon": "database"},
        {"label": "Onboarding / Offboarding", "icon": "user-plus"},
        {"label": "My Records", "icon": "folder"},
        {"label": "Employee Memos", "icon": "file-text"},
    ]},
    {"key": "time", "label": "Time & Attendance", "icon": "clock-3", "links": [
        {"label": "Attendance", "icon": "calendar-check"},
        {"label": "Break Log", "icon": "coffee"},
        {"label": "Break Monitoring", "icon": "activity"},
        {"label": "Night Differential", "icon": "moon"},
        {"label": "Overbreak Report", "icon": "alarm-clock"},
    ]},
    {"key": "leave", "label": "Leave Management", "icon": "calendar-days", "links": [
        {"label": "File Leave", "icon": "calendar-plus"},
        {"label": "Leave Calendar", "icon": "calendar"},
        {"label": "Leave Types", "icon": "list"},
        {"label": "Entitlements", "icon": "badge-check"},
        {"label": "My History", "icon": "history"},
    ]},
    {"key": "requests", "label": "Requests & Approvals", "icon": "file-plus", "links": [
        {"label": "File a Request (OT / CWS / FTS / RDW)", "icon": "file-plus"},
        {"label": "Leave Approvals", "icon": "check-circle"},
        {"label": "Material Request", "icon": "package"},
        {"label": "Material Approvals", "icon": "package-check"},
        {"label": "Facilities & Repair", "icon": "wrench"},
        {"label": "All Requests", "icon": "list-checks"},
    ]},
    {"key": "org", "label": "Organization", "icon": "building-2", "links": [
        {"label": "Floor Map", "icon": "map"},
        {"label": "Mailing Lists", "icon": "mail"},
        {"label": "Coaching", "icon": "message-square"},
        {"label": "QA Updates", "icon": "megaphone"},
    ]},
    {"key": "admin", "label": "Administration", "icon": "shield", "links": [
        {"label": "Reports", "icon": "bar-chart-2"},
        {"label": "Entitlements Audit", "icon": "search-check"},
        {"label": "Incident Dashboard", "icon": "alert-triangle"},
        {"label": "CSAT Dashboard", "icon": "smile"},
        {"label": "Attrition Report", "icon": "trending-down"},
        {"label": "Inventory", "icon": "boxes"},
    ]},
]


@app.route("/preview/lovable-dashboard")
def preview():
    return render_template(
        "dashboard.html",
        today_label=date.today().strftime("%A, %B %-d, %Y") if hasattr(date, "today") else "",
        stats=STATS,
        attendance=ATTENDANCE,
        events=EVENTS,
        employees=EMPLOYEES,
        leave_requests=LEAVE_REQUESTS,
    )


@app.route("/preview/topnav-dashboard")
def preview_topnav():
    return render_template(
        "dashboard_topnav.html",
        today_label=date.today().strftime("%A, %B %-d, %Y") if hasattr(date, "today") else "",
        stats=STATS,
        attendance=ATTENDANCE,
        events=EVENTS,
        notices=NOTICES,
        employees=EMPLOYEES,
        leave_requests=LEAVE_REQUESTS,
        nav_groups=NAV_GROUPS,
    )


@app.route("/")
@app.route("/preview/dashboard")
def preview_unified():
    return render_template(
        "dashboard_unified.html",
        today_label=date.today().strftime("%A, %B %-d, %Y") if hasattr(date, "today") else "",
        stats=STATS,
        attendance=ATTENDANCE,
        events=EVENTS,
        notices=NOTICES,
        employees=EMPLOYEES,
        leave_requests=LEAVE_REQUESTS,
        nav_groups=NAV_GROUPS,
    )


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5057, debug=False)
