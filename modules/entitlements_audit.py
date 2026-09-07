"""
Entitlements-permission audit blueprint for the Cohere HR portal (leavesystem).

Purpose: a read-only admin page listing every sub-admin who currently holds
the `can_entitlements` permission (leave4day_sub_admins.can_entitlements = 1)
-- i.e. who can access the leave-entitlements admin tools. This is distinct
from templates/admin/entitlements.html, which manages employees' leave-day
entitlement balances and has nothing to do with permissions.

- Sub-admin identity: any row in leave4day_sub_admins (orangehrm2, same DB
  as get_db()) is a sub-admin; there's no separate role column.
- Names come from hs_hr_employee (orangehrm2, same connection) -- both the
  permission holder and the admin who assigned it (assigned_by, an
  emp_number), via a self-join.
- group_name is enriched from central_db.gsheet_employees, fully-qualified
  cross-DB from the same orangehrm2 connection (mirrors the exact pattern
  at app.py:2162, which already proves this DB user has cross-DB privileges
  to central_db) with COLLATE utf8mb4_unicode_ci on the join to avoid the
  collation-mismatch errors this codebase has hit before.
- Gated with the same rule as app.py's admin_required (app.py:256-265) --
  NOT permission_required('can_entitlements') -- since this is an audit of
  who holds the permission; holders alone shouldn't be the only ones who
  can review that list. The check is reimplemented locally (mirroring
  modules/requirements.py's _logged_in()/_can_view() helpers) rather than
  importing app.admin_required, because this module is imported by app.py
  near the top of the file, before admin_required is defined there -- a
  top-level `from app import admin_required` would fail with a
  circular-import error.

Register in app.py:
    from modules.entitlements_audit import entitlements_audit_bp
    app.register_blueprint(entitlements_audit_bp)
"""

from flask import Blueprint, render_template, redirect, url_for, session, flash

entitlements_audit_bp = Blueprint("entitlements_audit", __name__)


def _logged_in():
    return "user" in session


def _is_admin():
    # Same rule as app.py's admin_required decorator.
    return bool(session.get("is_admin")) or bool(
        session.get("permissions", {}).get("can_entitlements")
    )


@entitlements_audit_bp.route("/admin/can-entitlements-audit")
def can_entitlements_audit():
    if not _logged_in():
        return redirect(url_for("login"))
    if not _is_admin():
        flash("Admin access required.", "danger")
        return redirect(url_for("dashboard"))

    from app import get_db

    db = get_db()
    try:
        with db.cursor() as c:
            c.execute("""
                SELECT
                    sa.emp_number,
                    sa.assigned_by,
                    sa.assigned_at,
                    sa.updated_at,
                    h.employee_id,
                    h.emp_firstname,
                    h.emp_lastname,
                    ab.emp_firstname AS assigned_by_firstname,
                    ab.emp_lastname  AS assigned_by_lastname,
                    g.group_name
                FROM leave4day_sub_admins sa
                JOIN hs_hr_employee h ON h.emp_number = sa.emp_number
                LEFT JOIN hs_hr_employee ab ON ab.emp_number = sa.assigned_by
                LEFT JOIN central_db.gsheet_employees g
                    ON g.employee_id COLLATE utf8mb4_unicode_ci = h.employee_id
                WHERE sa.can_entitlements = 1
                ORDER BY h.emp_lastname, h.emp_firstname
            """)
            holders = c.fetchall()
    finally:
        db.close()

    return render_template(
        "admin/can_entitlements_audit.html",
        holders=holders,
    )
