"""
Bulk offboarding script — runs account disabling for already-separated employees
that were missing from employee_offboarding table.

Run from the leavesystem directory:
  cd /var/www/html/leavesystem
  source venv/bin/activate
  python bulk_offboard.py
"""

import os
import sys
import secrets
import requests
from datetime import datetime

# Load .env
from dotenv import load_dotenv
load_dotenv()

import pymysql
import pymysql.cursors

# ── Target employees ───────────────────────────────────────
EMPLOYEES = [
    ('260323-12', '2026-05-21', 'Voluntary Resignation', 'Andrew Vincent Tacdoro'),
    ('260514-13', '2026-05-14', 'Abandonment',           'Honey Bea Cortes'),
    ('260514-06', '2026-05-14', 'Abandonment',           'Honey Bea Cortes'),
    ('260514-02', '2026-05-14', 'Abandonment',           'Honey Bea Cortes'),
    ('260216-06', '2026-04-30', 'Voluntary Resignation', 'Andrew Vincent Tacdoro'),
    ('260309-01', '2026-05-01', 'Abandonment',           'System'),
    ('260406-09', '2026-05-01', 'Abandonment',           'System'),
    ('260401-01', '2026-05-07', 'Abandonment',           'System'),
    ('260323-14', '2026-05-08', 'Abandonment',           'System'),
    ('260323-02', '2026-05-01', 'Abandonment',           'System'),
    ('260317-01', '2026-05-07', 'Abandonment',           'System'),
    ('260309-12', '2026-05-17', 'Abandonment',           'System'),
    ('210528-09', '2026-04-30', 'Abandonment',           'System'),
    ('260302-01', '2026-05-07', 'Abandonment',           'System'),
    ('260216-07', '2026-05-07', 'Abandonment',           'System'),
    ('260126-11', '2026-05-05', 'Abandonment',           'System'),
    ('260126-07', '2026-05-07', 'Abandonment',           'System'),
    ('260126-03', '2026-05-18', 'Abandonment',           'System'),
    ('260126-01', '2026-05-14', 'Abandonment',           'System'),
    ('250210-05', '2026-05-08', 'Abandonment',           'System'),
    ('240304-14', '2026-04-28', 'Abandonment',           'System'),
]

# ── DB connections ─────────────────────────────────────────
def get_central_db():
    return pymysql.connect(
        host=os.environ.get('MAIN_DB_HOST', 'localhost'),
        port=int(os.environ.get('MAIN_DB_PORT', 3306)),
        user=os.environ.get('MAIN_DB_USER', 'employee_sync'),
        password=os.environ.get('MAIN_DB_PASSWORD', '***REMOVED***'),
        database=os.environ.get('MAIN_DB_NAME', 'central_db'),
        charset='utf8mb4',
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=False
    )

def get_ohrm_db():
    import mysql.connector
    return mysql.connector.connect(
        host=os.environ.get('DB_HOST', 'localhost'),
        port=int(os.environ.get('DB_PORT', 3306)),
        user=os.environ.get('DB_USER', 'root'),
        password=os.environ.get('DB_PASSWORD'),
        database=os.environ.get('DB_NAME', 'orangehrm2')
    )

# ── Config ─────────────────────────────────────────────────
AD_BASE_URL  = os.environ.get('AD_API_URL', 'http://10.56.71.245:5001')
AD_HEADERS   = {
    'Content-Type': 'application/json',
    'X-API-Key':    os.environ.get('AD_API_KEY', 'Z0QFAz2BnixhPkVeNLGRuCw1WMKp6f78')
}
CPANEL_HOST  = os.environ.get('CPANEL_HOST', '65.254.92.49')
CPANEL_PORT  = os.environ.get('CPANEL_PORT', '2083')
CPANEL_AUTH  = f"cpanel {os.environ.get('CPANEL_USERNAME', 'cohere')}:{os.environ.get('CPANEL_API_TOKEN', 'G5TJ370XU26A0CEINNEPZDL8LEC4SFQK')}"
CPANEL_USER  = os.environ.get('CPANEL_USERNAME', 'cohere')
CPANEL_DOMAIN= os.environ.get('CPANEL_DOMAIN', 'cohere.ph')
CPANEL_URL   = f"https://{CPANEL_HOST}:{CPANEL_PORT}/json-api/cpanel"

def get_keycloak_admin():
    from keycloak import KeycloakAdmin
    return KeycloakAdmin(
        server_url=os.environ.get('KEYCLOAK_URL', 'https://keycloak.cohere.ph'),
        realm_name=os.environ.get('KEYCLOAK_REALM', 'COHERE'),
        client_id=os.environ.get('KEYCLOAK_ADMIN_CLIENT_ID', 'employee-management-service'),
        client_secret_key=os.environ.get('KEYCLOAK_ADMIN_CLIENT_SECRET'),
        verify=False
    )

# ── Process one employee ───────────────────────────────────
def offboard_employee(employee_id, exit_date, reason, processed_by):
    print(f"\n{'='*60}")
    print(f"Processing: {employee_id} | {reason} | {exit_date}")
    print(f"{'='*60}")

    conn = get_central_db()
    results = {
        'keycloak_disabled':  False,
        'ad_disabled':        False,
        'email_suspended':    False,
        'dashboard_disabled': False,
        'orangehrm_updated':  False,
        'status_updated':     True,   # already separated
    }

    # Get employee details
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM gsheet_employees WHERE employee_id = %s", [employee_id])
        employee = cur.fetchone()

    if not employee:
        print(f"  ❌ Employee not found in gsheet_employees")
        conn.close()
        return

    email         = employee['email']
    schedule_name = employee['schedule_name']
    name_parts    = schedule_name.split()
    last_name     = name_parts[-1] if name_parts else ''
    first_name    = ' '.join(name_parts[:-1]) if len(name_parts) > 1 else (name_parts[0] if name_parts else '')

    print(f"  Name:  {schedule_name}")
    print(f"  Email: {email}")

    # 1. KEYCLOAK ──────────────────────────────────────────
    try:
        kc    = get_keycloak_admin()
        users = kc.get_users({"email": email})
        if users:
            kc.update_user(users[0]['id'], {"enabled": False})
            results['keycloak_disabled'] = True
            print(f"  ✅ Keycloak disabled")
        else:
            print(f"  ⚠️  Keycloak user not found")
    except Exception as e:
        print(f"  ❌ Keycloak error: {e}")

    # 2. ACTIVE DIRECTORY ──────────────────────────────────
    try:
        first_name_parts = first_name.split()
        ad_username = (first_name_parts[0] + last_name).lower().replace(' ', '')
        ar = requests.post(
            f"{AD_BASE_URL}/api/user/disable",
            headers=AD_HEADERS,
            json={'username': ad_username, 'reason': reason},
            timeout=30
        )
        results['ad_disabled'] = ar.status_code == 200
        if results['ad_disabled']:
            print(f"  ✅ AD disabled: {ad_username}")
        else:
            print(f"  ⚠️  AD disable failed: {ar.text[:100]}")
    except Exception as e:
        print(f"  ❌ AD error: {e}")

    # 3. EMAIL — rotate password ───────────────────────────
    try:
        random_pw = f"OFFBOARDED_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{secrets.token_hex(16)}"
        er = requests.get(
            CPANEL_URL,
            params={
                'cpanel_jsonapi_user':   CPANEL_USER,
                'cpanel_jsonapi_module': 'Email',
                'cpanel_jsonapi_func':   'passwdpop',
                'email':                 email.split('@')[0],
                'domain':                CPANEL_DOMAIN,
                'password':              random_pw
            },
            headers={'Authorization': CPANEL_AUTH},
            verify=False, timeout=10
        )
        if er.json().get('cpanelresult', {}).get('data', [{}])[0].get('result') == 1:
            results['email_suspended'] = True
            print(f"  ✅ Email locked")
        else:
            reason_msg = er.json().get('cpanelresult', {}).get('data', [{}])[0].get('reason', 'Unknown')
            print(f"  ⚠️  Email lock failed: {reason_msg}")
    except Exception as e:
        print(f"  ❌ Email error: {e}")

    # 4. DASHBOARD ─────────────────────────────────────────
    try:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE Employees
                SET IsActive = 0, DisabledAt = NOW(),
                    DisabledBy = %s, DisabledReason = %s
                WHERE Email = %s AND IsActive = 1
            """, [processed_by, reason, email])
            conn.commit()
            results['dashboard_disabled'] = cur.rowcount > 0
        if results['dashboard_disabled']:
            print(f"  ✅ Dashboard disabled")
        else:
            print(f"  ⚠️  Dashboard already disabled or not found")
            results['dashboard_disabled'] = True  # already disabled counts as done
    except Exception as e:
        print(f"  ❌ Dashboard error: {e}")

    # 5. ORANGEHRM ─────────────────────────────────────────
    try:
        ohrm = get_ohrm_db()
        ohrm_cur = ohrm.cursor(buffered=True)
        ohrm_cur.execute(
            "SELECT emp_number FROM hs_hr_employee WHERE employee_id = %s", [employee_id]
        )
        row = ohrm_cur.fetchone()
        if row:
            emp_num = row[0]
            ohrm_cur.execute("""
                INSERT INTO ohrm_emp_termination (emp_number, reason_id, termination_date, note)
                VALUES (%s, 1, %s, %s)
                ON DUPLICATE KEY UPDATE
                    termination_date = VALUES(termination_date)
            """, [emp_num, exit_date, reason])
            ohrm_cur.execute(
                "SELECT id FROM ohrm_emp_termination WHERE emp_number = %s", [emp_num]
            )
            tid = ohrm_cur.fetchone()
            if tid:
                ohrm_cur.execute(
                    "UPDATE hs_hr_employee SET termination_id = %s WHERE emp_number = %s",
                    [tid[0], emp_num]
                )
            ohrm.commit()
            results['orangehrm_updated'] = True
            print(f"  ✅ OrangeHRM termination recorded")
        else:
            print(f"  ⚠️  OrangeHRM employee not found")
        ohrm_cur.close()
        ohrm.close()
    except Exception as e:
        print(f"  ❌ OrangeHRM error: {e}")

    # 6. SAVE AUDIT RECORD ─────────────────────────────────
    try:
        with conn.cursor() as cur:
            # Check if record already exists
            cur.execute(
                "SELECT id FROM employee_offboarding WHERE employee_id = %s", [employee_id]
            )
            existing = cur.fetchone()
            if existing:
                # Update existing record
                cur.execute("""
                    UPDATE employee_offboarding
                    SET ad_disabled = %s, keycloak_disabled = %s,
                        email_suspended = %s, dashboard_disabled = %s,
                        orangehrm_updated = %s, status_updated = 1
                    WHERE employee_id = %s
                """, [
                    results['ad_disabled'], results['keycloak_disabled'],
                    results['email_suspended'], results['dashboard_disabled'],
                    results['orangehrm_updated'], employee_id
                ])
                print(f"  ✅ Audit record updated")
            else:
                cur.execute("""
                    INSERT INTO employee_offboarding
                    (employee_id, exit_date, reason, processed_by, processed_at,
                     ad_disabled, keycloak_disabled, email_suspended,
                     dashboard_disabled, biometric_deleted, biometric_deactivated,
                     orangehrm_updated, status_updated)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """, [
                    employee_id, exit_date, reason, processed_by, datetime.now(),
                    results['ad_disabled'], results['keycloak_disabled'],
                    results['email_suspended'], results['dashboard_disabled'],
                    False, False,
                    results['orangehrm_updated'], True
                ])
                print(f"  ✅ Audit record created")
        conn.commit()
    except Exception as e:
        print(f"  ❌ Audit record error: {e}")

    conn.close()

    # Summary
    score = sum([
        results['keycloak_disabled'], results['ad_disabled'],
        results['email_suspended'], results['dashboard_disabled'],
        results['orangehrm_updated']
    ])
    print(f"\n  Summary: {score}/5 systems processed")
    print(f"  Keycloak: {'✅' if results['keycloak_disabled'] else '❌'}  "
          f"AD: {'✅' if results['ad_disabled'] else '❌'}  "
          f"Email: {'✅' if results['email_suspended'] else '❌'}  "
          f"Dashboard: {'✅' if results['dashboard_disabled'] else '❌'}  "
          f"OrangeHRM: {'✅' if results['orangehrm_updated'] else '❌'}")


# ── Main ───────────────────────────────────────────────────
if __name__ == '__main__':
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    print(f"\nBulk Offboarding Script")
    print(f"Processing {len(EMPLOYEES)} employees...")
    print(f"Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    for emp_id, exit_date, reason, processed_by in EMPLOYEES:
        offboard_employee(emp_id, exit_date, reason, processed_by)

    print(f"\n{'='*60}")
    print(f"Done. {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Check employee_offboarding table to verify all records.")
