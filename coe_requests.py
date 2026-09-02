"""
Certificate of Employment (CoE) request module.

Employee-facing: submit a CoE request, view own request history.
HR-facing: queue view, move requests through status, add internal notes, soft-delete.

Integration checklist:
  1. Run coe_requests_schema.sql against central_db.
  2. Copy this file into /var/www/html/leavesystem/ (same level as
     incident_reports.py / qa_updates.py).
  3. In app.py:
       from coe_requests import coe_bp
       app.register_blueprint(coe_bp)
  4. Add nav links in base.html (employee link + HR link, gated by permission).
  5. (Optional) Add can_coe_requests to the sub-admin permission system if you
     want granular access instead of the COE_HR_ACCESS env list below.

Uses db_core.get_db_connection() and the same session['user'] shape as
incident_reports.py / qa_updates.py — no import from app.py, so no circular
import risk.
"""

from flask import Blueprint, render_template, request, redirect, url_for, session, jsonify, flash
from datetime import datetime
from functools import wraps
import os
import smtplib
import threading
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

from db_core import get_db_connection
from csrf import validate_csrf


# ─── Email ────────────────────────────────────────────────────────────────────
# Reuses the same SMTP relay as the rest of the portal (no STARTTLS support).
SMTP_SERVER = os.environ.get('SMTP_SERVER')
SMTP_PORT = int(os.environ.get('SMTP_PORT', 25))
SMTP_USER = os.environ.get('SMTP_USER')
SMTP_PASSWORD = os.environ.get('SMTP_PASSWORD')
COE_FROM_NAME = os.environ.get('COE_FROM_NAME', 'Cohere HR Portal - CoE Requests')
COE_NOTIFY_EMAIL = os.environ.get('COE_NOTIFY_EMAIL', 'HR@cohere.ph')
COE_AUTO_RELEASE_DAYS = int(os.environ.get('COE_AUTO_RELEASE_DAYS', 3))
PORTAL_BASE_URL = os.environ.get('PORTAL_BASE_URL', 'https://hrportal.cohere.ph')


def _send_email(to_addrs, subject, body):
    """Send email in a background thread so it never blocks the request/response cycle.
    ADJUST if your actual _send() in incident_reports.py differs (e.g. no server.login,
    different port handling) — this mirrors the documented 'no STARTTLS' constraint."""
    if isinstance(to_addrs, str):
        to_addrs = [to_addrs]

    def _worker():
        try:
            msg = MIMEMultipart()
            msg['From'] = f"{COE_FROM_NAME} <{SMTP_USER}>"
            msg['To'] = ', '.join(to_addrs)
            msg['Subject'] = subject
            msg.attach(MIMEText(body, 'plain'))

            with smtplib.SMTP(SMTP_SERVER, SMTP_PORT, timeout=10) as server:
                server.login(SMTP_USER, SMTP_PASSWORD)
                server.sendmail(SMTP_USER, to_addrs, msg.as_string())
        except Exception as e:
            print(f"[COE EMAIL ERROR] Failed to send '{subject}' to {to_addrs}: {e}")

    threading.Thread(target=_worker, daemon=True).start()


# ─── Auth ─────────────────────────────────────────────────────────────────────
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user' not in session:
            return redirect('/login')
        return f(*args, **kwargs)
    return decorated


def coe_user():
    """Return a flat dict of current user info from Flask session."""
    u = session.get('user', {})
    return {
        'employee_id': u.get('employee_id'),
        'name': u.get('name'),
        'email': u.get('email'),
    }


COE_HR_ACCESS = set(
    e.strip().lower() for e in os.environ.get("COE_HR_ACCESS", "").split(",") if e.strip()
)

def coe_can_manage():
    """Whether the current user can access the CoE HR queue.
    Used both as a route guard and as a Jinja global for nav gating."""
    if 'user' not in session:
        return False
    email = (session['user'].get('email') or '').lower()
    return bool(
        session.get('is_admin')
        or email in COE_HR_ACCESS
        or session.get('permissions', {}).get('can_coe_requests')
    )

def coe_hr_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not coe_can_manage():
            flash("You do not have access to the CoE queue.", "danger")
            return redirect(url_for("coe.my_requests"))
        return f(*args, **kwargs)
    return wrapper


coe_bp = Blueprint("coe", __name__, url_prefix="/coe")

PURPOSE_OPTIONS = [
    "Loan Application",
    "Visa Application",
    "New Employer / Job Application",
    "Government Transaction (SSS, PhilHealth, Pag-IBIG, etc.)",
    "Credit Card Application",
    "Proof of Employment for Immigration",
    "Other",
]

STATUS_LABELS = {
    "pending": "Pending",
    "processing": "Processing",
    "ready_for_release": "Ready for Release",
    "released": "Released",
    "cancelled": "Cancelled",
}

# Allowed forward/back transitions for the HR queue
STATUS_FLOW = ["pending", "processing", "ready_for_release", "released"]


# ---------------------------------------------------------------------------
# Employee-facing routes
# ---------------------------------------------------------------------------

def _fetch_my_requests(employee_id):
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, purpose, purpose_other, addressee, additional_notes, status,
                       requested_at, processed_at, ready_for_release_at, released_at, released_via
                FROM coe_requests
                WHERE employee_id = %s AND is_deleted = 0
                ORDER BY requested_at DESC
                """,
                (employee_id,),
            )
            return cur.fetchall()
    finally:
        conn.close()


@coe_bp.route("/request", methods=["GET", "POST"])
@login_required
def request_coe():
    if request.method == "POST":
        if not validate_csrf():
            flash('Security check failed, please try again.', 'danger')
            return redirect(url_for('coe.request_coe'))
        purpose = request.form.get("purpose", "").strip()
        purpose_other = request.form.get("purpose_other", "").strip()
        addressee = request.form.get("addressee", "").strip()
        additional_notes = request.form.get("additional_notes", "").strip()

        if not purpose:
            flash("Please select a purpose for your CoE request.", "warning")
            return redirect(url_for("coe.request_coe"))

        if purpose == "Other" and not purpose_other:
            flash("Please specify the purpose.", "warning")
            return redirect(url_for("coe.request_coe"))

        if not addressee:
            flash("Please specify who this Certificate of Employment should be addressed to.", "warning")
            return redirect(url_for("coe.request_coe"))

        cu = coe_user()
        employee_id = cu['employee_id']
        employee_name = cu['name']
        employee_email = cu['email']

        conn = get_db_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO coe_requests
                        (employee_id, employee_name, employee_email,
                         purpose, purpose_other, addressee, additional_notes, status)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, 'pending')
                    """,
                    (employee_id, employee_name, employee_email,
                     purpose, purpose_other or None, addressee, additional_notes or None),
                )
            conn.commit()
        finally:
            conn.close()

        flash("Your CoE request has been submitted. HR will process it shortly.", "success")

        _send_email(
            COE_NOTIFY_EMAIL,
            f"New CoE Request - {employee_name} ({employee_id})",
            (
                f"A new Certificate of Employment request has been submitted.\n\n"
                f"Employee: {employee_name} ({employee_id})\n"
                f"Email: {employee_email}\n"
                f"Purpose: {purpose}{' - ' + purpose_other if purpose_other else ''}\n"
                f"Addressee: {addressee}\n"
                f"Notes: {additional_notes or '-'}\n\n"
                f"View and process this request: {PORTAL_BASE_URL}/coe/manage\n"
            ),
        )

        return redirect(url_for("coe.request_coe") + "#my-requests")

    cu = coe_user()
    history = _fetch_my_requests(cu['employee_id'])
    return render_template(
        "coe/request_form.html",
        purpose_options=PURPOSE_OPTIONS,
        requests=history,
        status_labels=STATUS_LABELS,
    )


@coe_bp.route("/my-requests")
@login_required
def my_requests():
    # Kept for backward-compat with existing email links; the history is now
    # a tab on the main request page rather than a separate view.
    return redirect(url_for("coe.request_coe") + "#my-requests")


@coe_bp.route("/my-requests/<int:req_id>/confirm-received", methods=["POST"])
@login_required
def confirm_received(req_id):
    if not validate_csrf():
        return jsonify({"success": False, "error": "Security check failed, please try again."}), 403
    employee_id = coe_user()['employee_id']
    now = datetime.now()

    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT employee_id, status FROM coe_requests WHERE id = %s AND is_deleted = 0",
                (req_id,),
            )
            row = cur.fetchone()

            if not row or row['employee_id'] != employee_id:
                return jsonify({"success": False, "error": "Request not found"}), 404

            if row['status'] != 'ready_for_release':
                return jsonify({"success": False, "error": "This request is not ready for release."}), 400

            cur.execute(
                """
                UPDATE coe_requests
                SET status = 'released',
                    released_at = %s,
                    released_via = 'employee_confirmed'
                WHERE id = %s
                """,
                (now, req_id),
            )
        conn.commit()
    finally:
        conn.close()

    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return jsonify({"success": True})

    flash("Thanks for confirming! Marked as received.", "success")
    return redirect(url_for("coe.my_requests"))


# ---------------------------------------------------------------------------
# HR-facing routes
# ---------------------------------------------------------------------------

@coe_bp.route("/manage")
@login_required
@coe_hr_required
def manage_queue():
    status_filter = request.args.get("status", "").strip()

    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            if status_filter and status_filter in STATUS_LABELS:
                cur.execute(
                    """
                    SELECT * FROM coe_requests
                    WHERE is_deleted = 0 AND status = %s
                    ORDER BY requested_at ASC
                    """,
                    (status_filter,),
                )
            else:
                cur.execute(
                    """
                    SELECT * FROM coe_requests
                    WHERE is_deleted = 0
                    ORDER BY
                        FIELD(status, 'pending','processing','ready_for_release','released','cancelled'),
                        requested_at ASC
                    """
                )
            rows = cur.fetchall()
    finally:
        conn.close()

    return render_template(
        "coe/manage_queue.html",
        requests=rows,
        status_labels=STATUS_LABELS,
        status_flow=STATUS_FLOW,
        active_filter=status_filter,
    )


@coe_bp.route("/manage/<int:req_id>/update-status", methods=["POST"])
@login_required
@coe_hr_required
def update_status(req_id):
    if not validate_csrf():
        return jsonify({"success": False, "error": "Security check failed, please try again."}), 400
    new_status = request.form.get("status", "").strip()
    hr_notes = request.form.get("hr_notes", "").strip()

    if new_status not in STATUS_LABELS:
        return jsonify({"success": False, "error": "Invalid status"}), 400

    cu = coe_user()
    processed_by = cu['employee_id']
    processed_by_name = cu['name']
    now = datetime.now()

    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT status, employee_name, employee_email FROM coe_requests WHERE id = %s",
                (req_id,),
            )
            existing = cur.fetchone()
            if not existing:
                return jsonify({"success": False, "error": "Request not found"}), 404
            old_status = existing['status']

            ready_for_release_at_sql = ""
            released_at_sql = ""
            released_via_sql = ""
            params = [new_status, hr_notes or None, processed_by, processed_by_name, now]

            if new_status == "ready_for_release" and old_status != "ready_for_release":
                ready_for_release_at_sql = ", ready_for_release_at = %s"
                params.append(now)

            if new_status == "released" and old_status != "released":
                released_at_sql = ", released_at = %s"
                released_via_sql = ", released_via = %s"
                params.append(now)
                params.append("hr_manual")

            params.append(req_id)

            cur.execute(
                f"""
                UPDATE coe_requests
                SET status = %s,
                    hr_notes = %s,
                    processed_by = %s,
                    processed_by_name = %s,
                    processed_at = %s
                    {ready_for_release_at_sql}
                    {released_at_sql}
                    {released_via_sql}
                WHERE id = %s AND is_deleted = 0
                """,
                tuple(params),
            )
        conn.commit()
    finally:
        conn.close()

    if new_status == "ready_for_release" and old_status != "ready_for_release" and existing.get('employee_email'):
        _send_email(
            existing['employee_email'],
            "Your Certificate of Employment is Ready for Release",
            (
                f"Hi {existing['employee_name']},\n\n"
                f"Your Certificate of Employment request is ready for pickup at HR.\n\n"
                f"Please confirm once you've received it here: "
                f"{PORTAL_BASE_URL}/coe/my-requests\n\n"
                f"If we don't hear from you within {COE_AUTO_RELEASE_DAYS} day(s), "
                f"this request will be automatically marked as released.\n"
            ),
        )

    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return jsonify({"success": True, "status": new_status, "label": STATUS_LABELS[new_status]})

    flash("Request updated.", "success")
    return redirect(url_for("coe.manage_queue"))


@coe_bp.route("/manage/<int:req_id>/delete", methods=["POST"])
@login_required
@coe_hr_required
def delete_request(req_id):
    if not validate_csrf():
        return jsonify({"success": False, "error": "Security check failed, please try again."}), 400
    reason = request.form.get("reason", "").strip()
    if not reason:
        return jsonify({"success": False, "error": "A reason is required"}), 400

    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE coe_requests
                SET is_deleted = 1, deleted_reason = %s, deleted_at = %s
                WHERE id = %s
                """,
                (reason, datetime.now(), req_id),
            )
        conn.commit()
    finally:
        conn.close()

    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return jsonify({"success": True})

    flash("Request removed.", "success")
    return redirect(url_for("coe.manage_queue"))


# JSON endpoint, in case you want to React-ify the manage queue later
# (same pattern as /api/approvals)
@coe_bp.route("/api/manage")
@login_required
@coe_hr_required
def api_manage_queue():
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT * FROM coe_requests
                WHERE is_deleted = 0
                ORDER BY
                    FIELD(status, 'pending','processing','ready_for_release','released','cancelled'),
                    requested_at ASC
                """
            )
            rows = cur.fetchall()
    finally:
        conn.close()

    for r in rows:
        for key in ("requested_at", "processed_at", "deleted_at"):
            if r.get(key):
                r[key] = r[key].isoformat()

    return jsonify(rows)