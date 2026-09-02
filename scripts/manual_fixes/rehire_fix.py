import os
#!/usr/bin/env python3
"""
rehire_fix.py - Cohere Outsourcing Re-hire Employee ID Fix Script
=================================================================
Handles all DB updates across cloud, blade, 3 locals, and Ubuntu
when an agent returns with a new employee ID or new personid.

Usage:
  python rehire_fix.py --old-personid 24021205 --new-personid 26061801 \
                       --old-companyid 240212-05 --new-companyid 260618-01 \
                       --name "Jia Basiga" --email jia.basiga@cohere.ph \
                       [--dry-run] [--since 2026-06-01]

Options:
  --old-personid    Old biometric personid (integer)
  --new-personid    New biometric personid (integer). Same as old if not re-enrolled.
  --old-companyid   Old employee ID (e.g. 240212-05)
  --new-companyid   New employee ID (e.g. 260618-01)
  --name            Agent name (for logging only)
  --email           Agent email (migrated to new personid in cloud userdata)
  --dry-run         Preview all changes without committing
  --since           Migrate records from this date onwards (default: 90 days ago)
"""

import argparse
import pymysql
from datetime import datetime, timedelta
from dotenv import load_dotenv
load_dotenv()

# ─── Server Configurations ────────────────────────────────────────────────────

SERVERS = {
    "cloud": {
        "host": "157.245.192.63",
        "user": "employee_sync",
        "password": os.environ.get("MAIN_DB_PASSWORD"),
        "database": "central_db",
        "port": 3306,
        "date_format": "datetime",
        "has_userdata": True,
        "has_dtr": True,
        "has_dtr_filtered": True,
        "has_employees": True,
        "has_test_timerecords": False,
        "has_fingerprint": False,
        "is_ubuntu": False,
    },
    "blade": {
        "host": "172.12.7.203",
        "user": "root",
        "password": "",
        "database": "timekeep",
        "port": 3306,
        "date_format": "unix",
        "has_userdata": True,
        "has_dtr": True,
        "has_dtr_filtered": False,
        "has_employees": False,
        "has_test_timerecords": False,
        "has_fingerprint": True,
        "is_ubuntu": False,
    },
    "local_timeout": {
        "host": "172.12.6.153",
        "user": "root",
        "password": "",
        "database": "timekeep",
        "port": 3306,
        "date_format": "unix",
        "has_userdata": True,
        "has_dtr": True,
        "has_dtr_filtered": False,
        "has_employees": False,
        "has_test_timerecords": False,
        "has_fingerprint": True,
        "is_ubuntu": False,
    },
    "local_timein": {
        "host": "172.12.6.152",
        "user": "root",
        "password": "",
        "database": "timekeep",
        "port": 3306,
        "date_format": "yyyymmddhhmmss",
        "has_userdata": True,
        "has_dtr": True,
        "has_dtr_filtered": False,
        "has_employees": False,
        "has_test_timerecords": False,
        "has_fingerprint": True,
        "is_ubuntu": False,
    },
    "local_training": {
        "host": "172.12.6.251",
        "user": "root",
        "password": "",
        "database": "timekeep",
        "port": 3306,
        "date_format": "unix",
        "has_userdata": True,
        "has_dtr": True,
        "has_dtr_filtered": False,
        "has_employees": False,
        "has_test_timerecords": False,
        "has_fingerprint": True,
        "is_ubuntu": False,
    },
    "ubuntu": {
        "host": "172.12.6.51",
        "user": "root",
        "password": os.environ.get("DB_PASSWORD"),
        "database": "central_db",
        "port": 3306,
        "date_format": "date",
        "has_userdata": False,
        "has_dtr": False,
        "has_dtr_filtered": False,
        "has_employees": True,
        "has_test_timerecords": True,
        "has_fingerprint": False,
        "is_ubuntu": True,
    },
}

# ─── Logging ──────────────────────────────────────────────────────────────────

LOG_LINES = []

def log(msg, level="INFO"):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    icons = {"INFO": "  ", "OK": "[OK]", "WARN": "[WARN]", "ERROR": "[ERR]", "DRY": "[DRY]", "SKIP": "[SKIP]"}
    icon = icons.get(level, "  ")
    line = f"[{ts}] {icon} {msg}"
    print(line)
    LOG_LINES.append(line)

def save_log(name):
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"rehire_fix_{name.replace(' ', '_')}_{ts}.log"
    with open(filename, "w", encoding="utf-8") as f:
        f.write("\n".join(LOG_LINES))
    print(f"Log saved to {filename}")

# ─── DB Connection ────────────────────────────────────────────────────────────

def connect(server_name, config):
    try:
        conn = pymysql.connect(
            host=config["host"],
            user=config["user"],
            password=config["password"],
            database=config["database"],
            port=config["port"],
            connect_timeout=5,
            cursorclass=pymysql.cursors.DictCursor
        )
        return conn
    except Exception as e:
        log(f"[{server_name}] Connection failed: {e}", "ERROR")
        return None

# ─── Date Range Helpers ───────────────────────────────────────────────────────

def get_date_filter(date_format, since_dt):
    if date_format == "unix":
        ts = int(since_dt.timestamp())
        return f"date >= {ts}"
    elif date_format == "yyyymmddhhmmss":
        ds = since_dt.strftime("%Y%m%d%H%M%S")
        return f"date >= {ds}"
    elif date_format == "datetime":
        ds = since_dt.strftime("%Y-%m-%d %H:%M:%S")
        return f"date >= '{ds}'"
    return "1=1"

# ─── Core Operations ──────────────────────────────────────────────────────────

def check_conflicts(conn, server_name, old_pid, new_pid, date_filter):
    if old_pid == new_pid:
        return []
    try:
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT old.date, old.type
                FROM dailytimerecord old
                JOIN dailytimerecord new ON old.date = new.date AND old.type = new.type
                WHERE old.personid = %s AND new.personid = %s
                AND old.{date_filter}
            """, (old_pid, new_pid))
            return cur.fetchall()
    except Exception as e:
        log(f"[{server_name}] Conflict check error: {e}", "WARN")
        return []

def delete_conflicts(conn, server_name, old_pid, conflicts, dry_run):
    if not conflicts:
        return
    for row in conflicts:
        if dry_run:
            log(f"[{server_name}] DRY: DELETE conflict personid={old_pid} date={row['date']} type={row['type']}", "DRY")
        else:
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        "DELETE FROM dailytimerecord WHERE personid=%s AND date=%s AND type=%s",
                        (old_pid, row['date'], row['type'])
                    )
                conn.commit()
                log(f"[{server_name}] Deleted conflict: personid={old_pid} date={row['date']} type={row['type']}", "OK")
            except Exception as e:
                log(f"[{server_name}] Delete conflict error: {e}", "ERROR")

def migrate_dtr(conn, server_name, old_pid, new_pid, date_filter, dry_run):
    if old_pid == new_pid:
        log(f"[{server_name}] Same personid - skipping DTR migration", "SKIP")
        return
    if dry_run:
        try:
            with conn.cursor() as cur:
                cur.execute(f"SELECT COUNT(*) as cnt FROM dailytimerecord WHERE personid=%s AND {date_filter}", (old_pid,))
                row = cur.fetchone()
                log(f"[{server_name}] DRY: Would migrate {row['cnt']} DTR records {old_pid} -> {new_pid}", "DRY")
        except Exception as e:
            log(f"[{server_name}] DTR count error: {e}", "WARN")
        return
    try:
        with conn.cursor() as cur:
            cur.execute(f"UPDATE dailytimerecord SET personid=%s WHERE personid=%s AND {date_filter}", (new_pid, old_pid))
        conn.commit()
        log(f"[{server_name}] Migrated {conn.affected_rows()} DTR records {old_pid} -> {new_pid}", "OK")
    except Exception as e:
        log(f"[{server_name}] DTR migration error: {e}", "ERROR")

def migrate_dtr_filtered(conn, server_name, old_pid, new_pid, since_dt, dry_run):
    if old_pid == new_pid:
        return
    ds = since_dt.strftime("%Y-%m-%d %H:%M:%S")
    if dry_run:
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) as cnt FROM dailytimerecordsfiltered WHERE personid=%s AND date>=%s", (old_pid, ds))
                row = cur.fetchone()
                log(f"[cloud] DRY: Would migrate {row['cnt']} filtered DTR records {old_pid} -> {new_pid}", "DRY")
        except Exception as e:
            log(f"[cloud] Filtered DTR count error: {e}", "WARN")
        return
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE dailytimerecordsfiltered SET personid=%s WHERE personid=%s AND date>=%s", (new_pid, old_pid, ds))
        conn.commit()
        log(f"[cloud] Migrated {conn.affected_rows()} filtered DTR records {old_pid} -> {new_pid}", "OK")
    except Exception as e:
        log(f"[cloud] Filtered DTR migration error: {e}", "ERROR")

def deactivate_old_personid(conn, server_name, old_pid, new_pid, dry_run):
    if old_pid == new_pid:
        log(f"[{server_name}] Same personid - skipping deactivation", "SKIP")
        return
    if dry_run:
        log(f"[{server_name}] DRY: Would deactivate personid={old_pid} in userdata", "DRY")
        return
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE userdata SET active=0 WHERE personid=%s", (old_pid,))
        conn.commit()
        log(f"[{server_name}] Deactivated old personid={old_pid}", "OK")
    except Exception as e:
        log(f"[{server_name}] Deactivate error: {e}", "ERROR")

def update_companyid_userdata(conn, server_name, old_cid, new_cid, dry_run):
    if dry_run:
        log(f"[{server_name}] DRY: Would update userdata companyid {old_cid} -> {new_cid}", "DRY")
        return
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE userdata SET companyid=%s WHERE companyid=%s", (new_cid, old_cid))
        conn.commit()
        log(f"[{server_name}] Updated userdata companyid {old_cid} -> {new_cid} ({conn.affected_rows()} rows)", "OK")
    except Exception as e:
        log(f"[{server_name}] userdata companyid update error: {e}", "ERROR")

def migrate_email(conn, server_name, old_pid, new_pid, email, dry_run):
    """Move email from old personid to new personid in cloud userdata."""
    if not email:
        log(f"[{server_name}] No email provided - skipping email migration", "SKIP")
        return
    if old_pid == new_pid:
        log(f"[{server_name}] Same personid - skipping email migration", "SKIP")
        return
    if dry_run:
        log(f"[{server_name}] DRY: Would move email {email} from personid={old_pid} to personid={new_pid}", "DRY")
        return
    try:
        with conn.cursor() as cur:
            # Clear email from old personid first (avoid unique constraint)
            cur.execute("UPDATE userdata SET email=NULL WHERE personid=%s AND email=%s", (old_pid, email))
            # Set email on new personid
            cur.execute("UPDATE userdata SET email=%s WHERE personid=%s", (email, new_pid))
        conn.commit()
        log(f"[{server_name}] Migrated email {email} -> personid={new_pid}", "OK")
    except Exception as e:
        log(f"[{server_name}] Email migration error: {e}", "ERROR")

def copy_fingerprints(conn, server_name, old_pid, new_pid, dry_run):
    if old_pid == new_pid:
        log(f"[{server_name}] Same personid - skipping fingerprint copy", "SKIP")
        return
    try:
        with conn.cursor() as cur:
            cur.execute("SHOW TABLES LIKE 'fingerprint'")
            if not cur.fetchone():
                log(f"[{server_name}] No fingerprint table - skipping", "SKIP")
                return
            cur.execute("SELECT COUNT(*) as cnt FROM fingerprint WHERE personid=%s", (old_pid,))
            count = cur.fetchone()['cnt']
            if count == 0:
                log(f"[{server_name}] No fingerprints found for old personid={old_pid}", "WARN")
                return
            if dry_run:
                log(f"[{server_name}] DRY: Would copy {count} fingerprint(s) {old_pid} -> {new_pid}", "DRY")
                return
            # Delete overlapping fingers on new personid first
            cur.execute("""
                DELETE FROM fingerprint WHERE personid=%s
                AND finger IN (SELECT finger FROM (SELECT finger FROM fingerprint WHERE personid=%s) AS tmp)
            """, (new_pid, old_pid))
            # Copy fingerprints
            cur.execute("""
                INSERT INTO fingerprint (personid, finger, template, creationdate, updationdate)
                SELECT %s, finger, template, creationdate, updationdate
                FROM fingerprint WHERE personid=%s
            """, (new_pid, old_pid))
        conn.commit()
        log(f"[{server_name}] Copied {count} fingerprint(s) {old_pid} -> {new_pid}", "OK")
    except Exception as e:
        log(f"[{server_name}] Fingerprint copy error: {e}", "ERROR")

def update_employees(conn, server_name, old_cid, new_cid, dry_run):
    if dry_run:
        log(f"[{server_name}] DRY: Would update Employees {old_cid} -> {new_cid}", "DRY")
        return
    try:
        with conn.cursor() as cur:
            cur.execute("SET FOREIGN_KEY_CHECKS = 0")
            cur.execute("UPDATE Employees SET EmployeeID=%s WHERE EmployeeID=%s", (new_cid, old_cid))
            affected = conn.affected_rows()
            # Update known FK tables referencing EmployeeID
            for fk_table, fk_col in [
                ("cws_requests", "employee_id"),
                ("leave4day_requests", "employee_id"),
                ("ot_requests", "employee_id"),
                ("fts_requests", "employee_id"),
                ("rdw_requests", "employee_id"),
                ("magic_cws_requests", "employee_id"),
            ]:
                try:
                    cur.execute(f"UPDATE {fk_table} SET {fk_col}=%s WHERE {fk_col}=%s", (new_cid, old_cid))
                    if conn.affected_rows() > 0:
                        log(f"[{server_name}] Updated {fk_table}.{fk_col} {old_cid} -> {new_cid} ({conn.affected_rows()} rows)", "OK")
                except Exception:
                    pass
            cur.execute("SET FOREIGN_KEY_CHECKS = 1")
        conn.commit()
        log(f"[{server_name}] Updated Employees {old_cid} -> {new_cid} ({affected} rows)", "OK")
    except Exception as e:
        log(f"[{server_name}] Employees update error: {e}", "ERROR")

def update_test_timerecords(conn, server_name, old_cid, new_cid, dry_run):
    for table in ["Test", "Timerecords"]:
        if dry_run:
            log(f"[{server_name}] DRY: Would update {table} companyid {old_cid} -> {new_cid}", "DRY")
            continue
        try:
            with conn.cursor() as cur:
                cur.execute(f"UPDATE {table} SET companyid=%s WHERE companyid=%s", (new_cid, old_cid))
            conn.commit()
            log(f"[{server_name}] Updated {table} companyid {old_cid} -> {new_cid} ({conn.affected_rows()} rows)", "OK")
        except Exception as e:
            log(f"[{server_name}] {table} update error: {e}", "ERROR")

# ─── Summary Check ────────────────────────────────────────────────────────────

def verify_cloud(conn, new_pid, new_cid, old_pid):
    log("--- Cloud Verification ---")
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) as cnt FROM dailytimerecord WHERE personid=%s", (new_pid,))
            log(f"[cloud] dailytimerecord under new personid {new_pid}: {cur.fetchone()['cnt']} records", "OK")

            cur.execute("SELECT COUNT(*) as cnt FROM dailytimerecordsfiltered WHERE personid=%s", (new_pid,))
            log(f"[cloud] dailytimerecordsfiltered under new personid {new_pid}: {cur.fetchone()['cnt']} records", "OK")

            if old_pid != new_pid:
                cur.execute("SELECT COUNT(*) as cnt FROM dailytimerecord WHERE personid=%s", (old_pid,))
                remaining = cur.fetchone()['cnt']
                level = "OK" if remaining == 0 else "WARN"
                log(f"[cloud] dailytimerecord remaining under old personid {old_pid}: {remaining} records (historical)", level)

            cur.execute("SELECT personid, companyid, fname, lname, email, active FROM userdata WHERE companyid=%s", (new_cid,))
            row = cur.fetchone()
            if row:
                log(f"[cloud] userdata: {row['fname']} {row['lname']} | companyid={row['companyid']} | email={row['email']} | active={row['active']}", "OK")
            else:
                log(f"[cloud] userdata: No record found for companyid={new_cid}", "WARN")
    except Exception as e:
        log(f"[cloud] Verification error: {e}", "ERROR")

# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Cohere Re-hire Fix Script")
    parser.add_argument("--old-personid", type=int, required=True)
    parser.add_argument("--new-personid", type=int, required=True)
    parser.add_argument("--old-companyid", required=True)
    parser.add_argument("--new-companyid", required=True)
    parser.add_argument("--name", default="Unknown Agent")
    parser.add_argument("--email", default=None, help="Agent email to migrate to new personid in cloud userdata")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--since", default=None, help="Migrate records from this date (YYYY-MM-DD). Default: 90 days ago.")
    args = parser.parse_args()

    old_pid = args.old_personid
    new_pid = args.new_personid
    old_cid = args.old_companyid
    new_cid = args.new_companyid
    same_personid = (old_pid == new_pid)

    since_dt = datetime.strptime(args.since, "%Y-%m-%d") if args.since else datetime.now() - timedelta(days=90)

    log("=" * 60)
    log(f"Re-hire Fix: {args.name}")
    log(f"Old personid: {old_pid} | New personid: {new_pid}")
    log(f"Old companyid: {old_cid} | New companyid: {new_cid}")
    log(f"Email: {args.email or '(not provided)'}")
    log(f"Same personid: {same_personid}")
    log(f"Migrating records since: {since_dt.strftime('%Y-%m-%d')}")
    log(f"Dry run: {args.dry_run}")
    log("=" * 60)

    cloud_conn = None

    for server_name, config in SERVERS.items():
        log(f"\n--- {server_name.upper()} ({config['host']}) ---")
        conn = connect(server_name, config)
        if conn is None:
            log(f"[{server_name}] Skipping - could not connect", "WARN")
            continue

        date_filter = get_date_filter(config["date_format"], since_dt)

        # userdata updates
        if config["has_userdata"]:
            if same_personid:
                update_companyid_userdata(conn, server_name, old_cid, new_cid, args.dry_run)
            else:
                deactivate_old_personid(conn, server_name, old_pid, new_pid, args.dry_run)
                # Migrate email on cloud only
                if server_name == "cloud" and args.email:
                    migrate_email(conn, server_name, old_pid, new_pid, args.email, args.dry_run)

        # Fingerprint copy (locals + blade only)
        if config["has_fingerprint"] and not same_personid:
            copy_fingerprints(conn, server_name, old_pid, new_pid, args.dry_run)

        # DTR migration
        if config["has_dtr"] and not same_personid:
            conflicts = check_conflicts(conn, server_name, old_pid, new_pid, date_filter)
            if conflicts:
                log(f"[{server_name}] Found {len(conflicts)} conflicting records - removing from old personid", "WARN")
                delete_conflicts(conn, server_name, old_pid, conflicts, args.dry_run)
            migrate_dtr(conn, server_name, old_pid, new_pid, date_filter, args.dry_run)

        # dailytimerecordsfiltered (cloud only)
        if config["has_dtr_filtered"] and not same_personid:
            migrate_dtr_filtered(conn, server_name, old_pid, new_pid, since_dt, args.dry_run)

        # Employees table
        if config["has_employees"]:
            update_employees(conn, server_name, old_cid, new_cid, args.dry_run)

        # Test + Timerecords (Ubuntu only)
        if config["has_test_timerecords"]:
            update_test_timerecords(conn, server_name, old_cid, new_cid, args.dry_run)

        if server_name == "cloud":
            cloud_conn = conn
        else:
            conn.close()

    # Final verification on cloud
    if cloud_conn:
        log("")
        verify_cloud(cloud_conn, new_pid, new_cid, old_pid)
        cloud_conn.close()

    log("\n" + "=" * 60)
    log("Done!")
    save_log(args.name)


if __name__ == "__main__":
    main()