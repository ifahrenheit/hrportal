"""
Mailing Lists blueprint: group email addresses that live in cPanel, viewed
from the portal instead of cPanel. Step 1 is read-only — nothing here changes
cPanel.

Two kinds of group address exist on the cohere cPanel account:
  forwarder  cPanel Email forwarders (one address -> many inboxes). Read with
             UAPI Email::list_forwarders.
  mailman    Mailman lists. UAPI lists them (Email::list_lists) but has no
             member API, so members are read from Mailman's own admin pages:
             UAPI Email::generate_mailman_otp gives a one-time admin password
             (what cPanel's "Manage" button uses), we log in at
             MAILMAN_BASE_URL/admin/<listid> and walk the letter-paged
             member list. The page's "N members total" is checked against
             what we parsed, so a short read is flagged, never silent.

Members are matched to central_db.gsheet_employees by email (Active/Training
preferred over Separated when an email appears more than once).

cPanel responses are cached in-process for CACHE_TTL seconds; ?refresh=1
bypasses the cache.

Access (anyone else gets a 404 and no menu item): admins and sub-admins with
can_mailing_lists.

.env: CPANEL_HOST, CPANEL_PORT, CPANEL_USERNAME, CPANEL_API_TOKEN (existing),
      MAILMAN_BASE_URL (optional, default https://cohere.ph/mailman)
"""

import logging
import os
import re
import threading
import time
from functools import wraps
from urllib.parse import unquote

import requests
import urllib3
from flask import (
    Blueprint, abort, jsonify, redirect, render_template, request, session,
    url_for,
)

from db_core import get_db_connection

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
logger = logging.getLogger(__name__)

mailing_lists_bp = Blueprint("mailing_lists", __name__, url_prefix="/admin/mailing-lists")

CACHE_TTL = 300
HTTP_TIMEOUT = 30
LIST_TYPES = ("forwarder", "mailman")

_cache = {}
_cache_lock = threading.Lock()


# ── Access ──────────────────────────────────────────────────────────────────

def ml_can_access():
    if session.get("is_admin"):
        return True
    return bool((session.get("permissions") or {}).get("can_mailing_lists"))


def _access_required(f):
    @wraps(f)
    def wrapped(*args, **kwargs):
        if "user" not in session:
            return redirect("/")
        if not ml_can_access():
            abort(404)
        return f(*args, **kwargs)
    return wrapped


@mailing_lists_bp.app_context_processor
def _inject_access():
    return {"ml_can_access": ml_can_access}


# ── Parsing (pure, unit-tested) ─────────────────────────────────────────────

_MEMBER_RE = re.compile(r'name="([^"\s]+%40[^"\s]+)_realname"')
_TOTAL_RE = re.compile(r"(\d+)\s+members\s+total", re.I)
_LETTER_RE = re.compile(r"[?&]letter=([a-z0-9])", re.I)
_CHUNK_RE = re.compile(r"[?&]chunk=(\d+)")


def group_forwarders(rows):
    """UAPI list_forwarders rows -> {address: sorted unique targets}.
    In those rows 'dest' is the forwarder address and 'forward' the target."""
    groups = {}
    for r in rows or []:
        addr = (r.get("dest") or "").strip().lower()
        target = (r.get("forward") or "").strip()
        if not addr or not target:
            continue
        groups.setdefault(addr, set()).add(target.lower() if "@" in target else target)
    return {a: sorted(t) for a, t in groups.items()}


def parse_mailman_members(html):
    """Member emails on one Mailman admin members page."""
    return {unquote(m).lower() for m in _MEMBER_RE.findall(html or "")}


def parse_mailman_total(html):
    m = _TOTAL_RE.search(html or "")
    return int(m.group(1)) if m else None


def parse_mailman_pages(html):
    """(letters, chunks) pagination links on a Mailman members page."""
    html = html or ""
    letters = sorted({l.lower() for l in _LETTER_RE.findall(html)})
    chunks = sorted({int(c) for c in _CHUNK_RE.findall(html)})
    return letters, chunks


def classify_members(emails, employees):
    """emails: iterable of addresses. employees: {email_lower: row} with
    schedule_name/status/group_name/employee_id. Returns a list of dicts."""
    out = []
    for e in sorted(set(emails)):
        emp = employees.get(e.lower()) if "@" in e else None
        if emp is None:
            kind = "external"
        elif (emp.get("status") or "").lower() == "separated":
            kind = "separated"
        else:
            kind = "active"
        out.append({
            "email": e,
            "kind": kind,
            "name": emp.get("schedule_name") if emp else None,
            "status": emp.get("status") if emp else None,
            "group_name": emp.get("group_name") if emp else None,
            "employee_id": emp.get("employee_id") if emp else None,
        })
    return out


def summarize(members):
    s = {"total": len(members), "active": 0, "separated": 0, "external": 0}
    for m in members:
        s[m["kind"]] += 1
    return s


# ── cPanel / Mailman client ─────────────────────────────────────────────────

def _cpanel_base():
    host = os.environ.get("CPANEL_HOST", "65.254.92.49")
    port = os.environ.get("CPANEL_PORT", "2083")
    return f"https://{host}:{port}/execute"


def _cpanel_headers():
    return {"Authorization": f"cpanel {os.environ.get('CPANEL_USERNAME', 'cohere')}:"
                             f"{os.environ.get('CPANEL_API_TOKEN', '')}"}


def _uapi(fn, params=None):
    r = requests.get(f"{_cpanel_base()}/Email/{fn}", headers=_cpanel_headers(),
                     params=params or {}, verify=False, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    j = r.json()
    if not j.get("status"):
        raise RuntimeError(f"cPanel Email::{fn} failed: {'; '.join(j.get('errors') or []) or 'unknown error'}")
    return j.get("data")


def _cached(key, loader, refresh=False):
    now = time.time()
    with _cache_lock:
        hit = _cache.get(key)
        if hit and not refresh and now - hit[0] < CACHE_TTL:
            return hit[1], hit[0]
    value = loader()
    with _cache_lock:
        _cache[key] = (now, value)
    return value, now


def fetch_forwarders(refresh=False):
    return _cached("forwarders", lambda: group_forwarders(_uapi("list_forwarders")), refresh)


def fetch_mailman_lists(refresh=False):
    def load():
        lists = {}
        for x in _uapi("list_lists") or []:
            addr = (x.get("list") or "").lower()
            if not addr:
                continue
            lists[addr] = {
                "address": addr,
                "listid": x.get("listid"),
                "admins": [a.strip() for a in (x.get("listadmin") or "").split(",") if a.strip()],
                "access": x.get("accesstype"),
                "disk": x.get("humandiskused"),
            }
        return lists
    return _cached("mailman_lists", load, refresh)


def _mailman_base():
    return os.environ.get("MAILMAN_BASE_URL", "https://cohere.ph/mailman").rstrip("/")


def _read_mailman_members(address, listid):
    """Log into one list's Mailman admin with a one-time password and read
    every member. Returns {'members': set, 'total': int|None, 'complete': bool}."""
    if not listid or not re.fullmatch(r"[A-Za-z0-9._-]+", listid):
        raise RuntimeError(f"Unexpected Mailman list id for {address}")
    otp = _uapi("generate_mailman_otp", {"list": address})
    if not isinstance(otp, str) or not otp:
        raise RuntimeError("cPanel did not return a Mailman one-time password")

    base = f"{_mailman_base()}/admin/{listid}"
    with requests.Session() as s:
        s.verify = False
        r = s.post(f"{base}/members/list", data={"adminpw": otp, "admlogin": "Let me in..."},
                   timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        if 'name="adminpw"' in r.text:
            raise RuntimeError("Mailman login was refused")
        members = parse_mailman_members(r.text)
        total = parse_mailman_total(r.text)
        letters, chunks = parse_mailman_pages(r.text)

        def walk(params):
            page = s.get(f"{base}/members", params=params, timeout=HTTP_TIMEOUT)
            page.raise_for_status()
            return page.text

        pages = [{"chunk": c} for c in chunks]
        for letter in letters:
            html = walk({"letter": letter})
            members |= parse_mailman_members(html)
            pages += [{"letter": letter, "chunk": c} for c in parse_mailman_pages(html)[1]]
        for p in pages:
            members |= parse_mailman_members(walk(p))

    return {"members": members, "total": total,
            "complete": total is None or total == len(members)}


def fetch_mailman_members(address, refresh=False):
    lists, _ = fetch_mailman_lists()
    info = lists.get(address)
    if not info:
        raise KeyError(address)
    return _cached(f"mailman:{address}",
                   lambda: _read_mailman_members(address, info["listid"]), refresh)


# ── Employee matching ───────────────────────────────────────────────────────

def load_employee_index():
    """{email_lower: row}; when an email repeats, Active/Training wins."""
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute("""
                SELECT employee_id, schedule_name, LOWER(TRIM(email)) AS email, status, group_name
                FROM gsheet_employees
                WHERE email IS NOT NULL AND email <> ''
                ORDER BY (status = 'Separated'), last_modified_at
            """)
            rows = c.fetchall()
    finally:
        conn.close()
    index = {}
    for r in rows:
        index.setdefault(r["email"], r)
    return index


def _get_members(list_type, address, refresh=False):
    """(members_classified, meta) for one group address."""
    if list_type == "forwarder":
        groups, fetched = fetch_forwarders(refresh)
        if address not in groups:
            raise KeyError(address)
        emails, meta = groups[address], {"complete": True, "total": len(groups[address])}
    else:
        data, fetched = fetch_mailman_members(address, refresh)
        emails, meta = data["members"], {"complete": data["complete"], "total": data["total"]}
    meta["fetched_at"] = fetched
    return classify_members(emails, load_employee_index()), meta


# ── Routes ──────────────────────────────────────────────────────────────────

@mailing_lists_bp.route("/")
@_access_required
def index():
    refresh = request.args.get("refresh") == "1"
    error = None
    forwarders, mailman = {}, {}
    try:
        forwarders, _ = fetch_forwarders(refresh)
        mailman, _ = fetch_mailman_lists(refresh)
    except Exception as e:
        logger.exception("mailing lists: cPanel read failed")
        error = str(e)

    employees = load_employee_index() if forwarders else {}
    rows = []
    for addr, targets in forwarders.items():
        rows.append({"type": "forwarder", "address": addr,
                     "summary": summarize(classify_members(targets, employees))})
    for addr, info in mailman.items():
        rows.append({"type": "mailman", "address": addr, "admins": info["admins"],
                     "summary": None})
    rows.sort(key=lambda r: (r["address"], r["type"]))
    both = sorted(set(forwarders) & set(mailman))
    return render_template("mailing_lists/index.html", rows=rows, error=error, both=both,
                           n_forwarders=len(forwarders), n_mailman=len(mailman))


@mailing_lists_bp.route("/<list_type>/<path:address>")
@_access_required
def detail(list_type, address):
    if list_type not in LIST_TYPES:
        abort(404)
    address = address.lower()
    refresh = request.args.get("refresh") == "1"
    error, members, meta, info = None, [], {}, None
    try:
        if list_type == "mailman":
            info = fetch_mailman_lists()[0].get(address)
        members, meta = _get_members(list_type, address, refresh)
    except KeyError:
        abort(404)
    except Exception as e:
        logger.exception("mailing lists: read of %s %s failed", list_type, address)
        error = str(e)
    return render_template("mailing_lists/detail.html", list_type=list_type, address=address,
                           members=members, meta=meta, info=info, error=error,
                           summary=summarize(members),
                           fetched_at=time.strftime("%Y-%m-%d %H:%M", time.localtime(meta["fetched_at"]))
                           if meta.get("fetched_at") else None)


@mailing_lists_bp.route("/api/summary/<list_type>/<path:address>")
@_access_required
def api_summary(list_type, address):
    """Member counts for one group; the index page fills Mailman rows with it."""
    if list_type not in LIST_TYPES:
        abort(404)
    try:
        members, meta = _get_members(list_type, address.lower())
    except KeyError:
        abort(404)
    except Exception as e:
        logger.exception("mailing lists: summary of %s %s failed", list_type, address)
        return jsonify({"ok": False, "error": str(e)}), 502
    return jsonify({"ok": True, "summary": summarize(members), "complete": meta["complete"]})
