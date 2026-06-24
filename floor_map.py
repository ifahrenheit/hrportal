import os
import hmac
import hashlib
from datetime import datetime
from flask import Blueprint, render_template, jsonify, session, request, redirect, url_for
from functools import wraps
import pymysql
import pymysql.cursors

floor_map_bp = Blueprint("floor_map", __name__, url_prefix="/floor-map")

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user" not in session:
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated

def get_db():
    return pymysql.connect(
        host=os.environ.get("MAIN_DB_HOST", "localhost"),
        user=os.environ.get("MAIN_DB_USER", "employee_sync"),
        password=os.environ.get("MAIN_DB_PASSWORD", ""),
        database=os.environ.get("MAIN_DB_NAME", "central_db"),
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor
    )

@floor_map_bp.route("/")
@login_required
def index():
    return render_template("floor_map/index.html")

@floor_map_bp.route("/api/seats")
@login_required
def api_seats():
    db = get_db()
    try:
        with db.cursor() as cur:
            cur.execute("""
                SELECT station_group, seat_number, seat_label,
                       team, seat_type, computer_name
                FROM seat_computer_mapping
                ORDER BY station_group, seat_number
            """)
            seats = cur.fetchall()
        return jsonify(seats)
    finally:
        db.close()

@floor_map_bp.route("/api/presence")
@login_required
def api_presence():
    db = get_db()
    try:
        with db.cursor() as cur:
            cur.execute("""
                SELECT
                    m.station_group,
                    m.seat_number,
                    m.seat_label,
                    m.team,
                    m.seat_type,
                    m.computer_name,
                    p.active_user,
                    p.active_user_display,
                    p.last_user,
                    p.last_user_display,
                    p.login_time,
                    p.logoff_time,
                    p.status,
                    p.ip_address,
                    p.updated_at
                FROM seat_computer_mapping m
                LEFT JOIN floor_presence p
                    ON UPPER(m.computer_name) = UPPER(p.computer_name)
                ORDER BY m.station_group, m.seat_number
            """)
            rows = cur.fetchall()
        for row in rows:
            for key in ["login_time", "logoff_time", "updated_at"]:
                if row.get(key):
                    row[key] = str(row[key])
        return jsonify(rows)
    finally:
        db.close()

@floor_map_bp.route("/api/event", methods=["POST"])
def floor_event():
    SECRET = os.environ.get("FLOOR_MAP_SECRET", "changeme")
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "no data"}), 400

    sig      = data.get("sig", "")
    computer = data.get("computer", "").upper().strip()
    event    = data.get("event", "").upper().strip()
    username = data.get("user", "").strip()
    ip       = data.get("ip", request.remote_addr)

    payload  = f"{computer}{event}{username}"
    expected = hmac.new(SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return jsonify({"error": "unauthorized"}), 401

    display = username.replace("COHERE\\", "").replace(".", " ").title() if username else None
    now     = datetime.now()

    db = get_db()
    try:
        with db.cursor() as cur:
            cur.execute("""
                SELECT station_group, seat_number
                FROM seat_computer_mapping
                WHERE UPPER(computer_name) = %s
                LIMIT 1
            """, (computer,))
            seat = cur.fetchone()
            sg   = seat["station_group"] if seat else None
            snum = seat["seat_number"]   if seat else None

            if event == "LOGIN":
                cur.execute("""
                    INSERT INTO floor_presence
                        (computer_name, station_group, seat_number,
                         active_user, active_user_display,
                         login_time, logoff_time, status, ip_address)
                    VALUES (%s,%s,%s,%s,%s,%s,NULL,"active",%s)
                    ON DUPLICATE KEY UPDATE
                        station_group       = VALUES(station_group),
                        seat_number         = VALUES(seat_number),
                        last_user           = active_user,
                        last_user_display   = active_user_display,
                        active_user         = VALUES(active_user),
                        active_user_display = VALUES(active_user_display),
                        login_time          = VALUES(login_time),
                        logoff_time         = NULL,
                        status              = "active",
                        ip_address          = VALUES(ip_address)
                """, (computer, sg, snum, username, display, now, ip))

            elif event == "LOGOFF":
                cur.execute("""
                    INSERT INTO floor_presence
                        (computer_name, station_group, seat_number,
                         last_user, last_user_display,
                         logoff_time, status, ip_address)
                    VALUES (%s,%s,%s,%s,%s,%s,"logoff",%s)
                    ON DUPLICATE KEY UPDATE
                        last_user           = active_user,
                        last_user_display   = active_user_display,
                        active_user         = NULL,
                        active_user_display = NULL,
                        logoff_time         = VALUES(logoff_time),
                        status              = "logoff",
                        ip_address          = VALUES(ip_address)
                """, (computer, sg, snum, username, display, now, ip))

        db.commit()
        return jsonify({"ok": True, "event": event, "computer": computer})
    except Exception as e:
        db.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        db.close()


@floor_map_bp.route('/api/update-seat', methods=['POST'])
@login_required
def api_update_seat():
    data = request.get_json(silent=True)
    if not data:
        return jsonify({'error': 'no data'}), 400

    seats    = data.get('seats', [])   # list of {station_group, seat_number}
    team     = data.get('team')
    seat_type = data.get('seat_type')
    computer_name = data.get('computer_name')  # only for single seat

    if not seats:
        return jsonify({'error': 'no seats provided'}), 400

    db = get_db()
    try:
        with db.cursor() as cur:
            for s in seats:
                sg   = s.get('station_group')
                snum = s.get('seat_number')
                if not sg or snum is None:
                    continue
                fields, values = [], []
                if team is not None:
                    fields.append('team = %s'); values.append(team)
                if seat_type is not None:
                    fields.append('seat_type = %s'); values.append(seat_type)
                if computer_name is not None and len(seats) == 1:
                    fields.append('computer_name = %s')
                    values.append(computer_name.strip().upper() or None)
                if not fields:
                    continue
                values += [sg, snum]
                cur.execute(f"""
                    UPDATE seat_computer_mapping
                    SET {', '.join(fields)}
                    WHERE station_group = %s AND seat_number = %s
                """, values)
        db.commit()
        return jsonify({'ok': True, 'updated': len(seats)})
    except Exception as e:
        db.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        db.close()

@floor_map_bp.route('/admin')
@login_required
def admin():
    return render_template('floor_map/admin.html')


@floor_map_bp.route('/api/add-seat', methods=['POST'])
@login_required
def api_add_seat():
    data = request.get_json(silent=True)
    if not data:
        return jsonify({'error': 'no data'}), 400
    sg        = data.get('station_group')
    seat_num  = data.get('seat_number')
    team      = data.get('team', 'cs')
    seat_type = data.get('seat_type', 'agent')
    computer  = data.get('computer_name', None)
    if not sg or seat_num is None:
        return jsonify({'error': 'missing fields'}), 400
    db = get_db()
    try:
        with db.cursor() as cur:
            cur.execute("""
                INSERT INTO seat_computer_mapping
                    (station_group, seat_number, seat_label, team, seat_type, computer_name)
                VALUES (%s,%s,%s,%s,%s,%s)
            """, (sg, seat_num, f"{sg}-S{seat_num}", team, seat_type, computer))
        db.commit()
        return jsonify({'ok': True})
    except Exception as e:
        db.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        db.close()

@floor_map_bp.route('/api/delete-seat', methods=['POST'])
@login_required
def api_delete_seat():
    data = request.get_json(silent=True)
    if not data:
        return jsonify({'error': 'no data'}), 400
    sg   = data.get('station_group')
    snum = data.get('seat_number')
    db = get_db()
    try:
        with db.cursor() as cur:
            cur.execute("""
                DELETE FROM seat_computer_mapping
                WHERE station_group=%s AND seat_number=%s
            """, (sg, snum))
        db.commit()
        return jsonify({'ok': True})
    except Exception as e:
        db.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        db.close()
