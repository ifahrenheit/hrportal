# hrportal · Leave4Day

> An internal HR portal for **Cohere Outsourcing Philippines Inc.** built with Flask and MySQL.  
> Handles leave management, file requests, attendance tracking, incident reports, and admin reporting — running live at `hrportal.cohere.ph`.

---

## Features

### 🗓️ Leave Management
- File, view, and track leave requests
- Leave balance pulled from OrangeHRM entitlements
- Supervisor approval workflow with email notifications
- Leave history and status tracking per employee

### 📋 File Requests
- **OT** — Overtime requests
- **FTS** — Failure to Swipe corrections
- **CWS** — Change Work Schedule
- **RDW** — Rest Day Work
- **Magic CWS** — Shift swap system with balance tracking (deducted on approval)
- **Material Requests** — Multi-step procurement workflow with PDF generation and edit history
- **Facilities & Repair** — Multi-stage review → final approval workflow with file attachments

### 👥 Supervisor & Admin Tools
- Supervisor dashboard: pending approvals, active leaves, team calendar
- Bulk approve for leaves and file requests
- Absence Report with FTS IN/OUT/Absent classification, date range filters, clipboard export
- OT260 report, night differential report, analytics dashboard
- Sub-admin permission system with granular access control
- Login As feature for admin impersonation

### 🧑‍💼 Employee Management
- PIM (Personnel Information Module) — employee profiles
- Onboarding / offboarding workflows
- Schedule management
- Inventory and medicine tracking

### 🚨 Incident Reports
- File incident reports with photo attachments
- Comment thread per incident
- Admin review and resolution tracking
- Cron-based reminder emails for open incidents

### 🔒 Authentication
- Keycloak SSO (primary login)
- Fallback portal login
- Role-based access: Admin, Sub-admin, Supervisor (SOM), Team Lead, Employee

---

## Tech Stack

| Layer | Technology |
|---|---|
| Backend | Python 3 / Flask |
| Database | MySQL (`central_db`, `orangehrm2`) |
| Frontend | Jinja2, Bootstrap 5, Vanilla JS |
| Auth | Keycloak (Docker) — SSO via OIDC |
| PDF Generation | ReportLab |
| Email | SMTP via Flask-Mail |
| Employee Sync | Google Sheets → MySQL via Google Apps Script |
| Web Server | Apache2 (reverse proxy) |
| Process Manager | systemd (`leavesystem.service`) |
| Hosting | DigitalOcean Ubuntu Droplet |
| Version Control | Git / GitHub |

---

## Project Structure

```
hrportal/
├── app.py                  # Main Flask application (routes, helpers)
├── config.py               # App configuration
├── incident_reports.py     # Incident report blueprint
├── requirements.txt        # Python dependencies
│
├── break_log/              # Break log blueprint
│   ├── __init__.py
│   └── routes.py
│
├── scripts/                # Utility & cron scripts
│   ├── bulk_offboard.py
│   ├── cron_incident_reminder.py
│   ├── sync_ohrm_emails.py
│   └── update_status.py
│
├── rag/                    # AI assistant (RAG API)
│   └── rag_api.py
│
├── static/
│   └── img/                # Logo assets
│
└── templates/
    ├── base.html            # Shared layout with sidebar
    ├── dashboard.html
    ├── admin/               # Admin-only pages
    ├── file_requests/       # All file request forms and queues
    ├── supervisor/          # Supervisor views
    ├── tickets/             # Ticket system
    ├── break_log/           # Break log views
    └── pim/                 # Employee profiles
```

---

## Architecture Overview

```
Browser
  │
  ▼
Apache2 (reverse proxy)
  │
  ▼
Flask app (systemd: leavesystem.service)
  │
  ├── central_db (MySQL)         ← primary data store
  │     ├── gsheet_employees     ← synced from Google Sheets (source of truth)
  │     ├── leave4day_requests
  │     ├── file_requests_*
  │     ├── magic_cws_requests
  │     ├── facilities_requests
  │     └── incident_reports
  │
  └── orangehrm2 (MySQL)         ← OrangeHRM legacy data
        ├── ohrm_leave           ← leave balances (payroll source of truth)
        └── ohrm_leave_entitlement

Keycloak (Docker) ──────────────► SSO / OIDC authentication

Google Apps Script ─────────────► Syncs employee data to gsheet_employees
```

---

## Key Design Decisions

- **`gsheet_employees` as source of truth** — employee records sync automatically from Google Sheets via Apps Script, keeping HR data in one place
- **Dual database** — `central_db` for portal data, `orangehrm2` for legacy OrangeHRM leave balances; actively migrating away from OrangeHRM dependency
- **Sub-admin permission system** — granular permissions (`can_material_requests`, `can_final_approval`, `can_facilities_review`, etc.) without giving full admin access
- **No build step frontend** — Bootstrap + Vanilla JS for simplicity; CDN React/Babel used where interactivity needs it
- **PDF generation server-side** — ReportLab generates request forms on the fly, no external service needed

---

## Setup (Development)

```bash
# Clone the repo
git clone https://github.com/ifahrenheit/hrportal.git
cd hrportal

# Create virtualenv
python3 -m venv venv
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Copy and configure environment
cp .env.example .env
# Edit .env with your DB credentials, Keycloak config, SMTP settings

# Run
flask run
```

> ⚠️ This app is built for a specific internal infrastructure (Keycloak, OrangeHRM, Google Sheets sync). A standalone dev setup requires mocking several external services.

---

## Screenshots

> _Coming soon_

---

## Author

**Binbin** — IT Admin / Developer, Cohere Outsourcing Philippines Inc.  
GitHub: [@ifahrenheit](https://github.com/ifahrenheit)