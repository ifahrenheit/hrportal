"""
Mailing Lists blueprint: group email addresses that live in cPanel, managed
from the portal instead of cPanel. Members can be viewed, added and removed;
creating/deleting the group addresses themselves is still done in cPanel.

Two kinds of group address exist on the cohere cPanel account:
  forwarder  cPanel Email forwarders (one address -> many inboxes). UAPI
             Email::list_forwarders / add_forwarder / delete_forwarder.
  mailman    Mailman lists. UAPI lists them (Email::list_lists) but has no
             member API, so members are read and changed through Mailman's
             own admin pages: UAPI Email::generate_mailman_otp gives a
             one-time admin password (what cPanel's "Manage" button uses), we
             log in at MAILMAN_BASE_URL/admin/<listid>, walk the letter-paged
             member list, and post its mass subscribe / mass remove forms
             (no welcome or goodbye emails unless asked for). The page's
             "N members total" is checked against what we parsed, so a short
             read is flagged, never silent.

Every change is verified by re-reading the list afterwards: a member counts
as added/removed only if the fresh read shows it. Each attempted change
(done, failed or skipped) is a row in central_db.mailing_list_audit
(DDL: scripts/mailing_list_audit_schema.sql).

Members are matched to central_db.gsheet_employees by email (Active/Training
preferred over Separated when an email appears more than once).

cPanel responses are cached in-process for CACHE_TTL seconds; ?refresh=1
bypasses the cache and a change refreshes the list it touched.

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
from contextlib import contextmanager
from functools import wraps
from urllib.parse import unquote

import requests
import urllib3
from flask import (
    Blueprint, abort, flash, jsonify, redirect, render_template, request,
    session, url_for,
)

from csrf import validate_csrf
from db_core import get_db_connection

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
logger = logging.getLogger(__name__)

mailing_lists_bp = Blueprint("mailing_lists", __name__, url_prefix="/admin/mailing-lists")

CACHE_TTL = 300
HTTP_TIMEOUT = 30
LIST_TYPES = ("forwarder", "mailman")
MAX_BATCH = 300
HISTORY_LIMIT = 50

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
_CSRF_RE = re.compile(r'<input[^>]*name="csrf_token"[^>]*value="([^"]*)"', re.I)
_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+'-]+@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}$")


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


def parse_email_input(text):
    """Free text (commas, semicolons, spaces, newlines, 'Name <a@b>') ->
    (valid unique lowercase emails in input order, invalid tokens)."""
    valid, invalid, seen = [], [], set()
    for tok in re.split(r"[\s,;]+", text or ""):
        tok = tok.strip().strip("<>\"'()").lower()
        if not tok:
            continue
        if not _EMAIL_RE.match(tok):
            invalid.append(tok)
        elif tok not in seen:
            seen.add(tok)
            valid.append(tok)
    return valid, invalid


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


def plan_change(action, emails, current):
    """Split requested emails into (todo, skipped) against the current members."""
    current = {c.lower() for c in current}
    if action == "add":
        return [e for e in emails if e not in current], [e for e in emails if e in current]
    return [e for e in emails if e in current], [e for e in emails if e not in current]


def verify_change(action, todo, after, errors=None):
    """Per-email (result, detail) for the emails we tried to change, judged
    by the fresh member list read back after the change."""
    errors = errors or {}
    after = {a.lower() for a in after}
    out = {}
    for e in todo:
        ok = (e in after) if action == "add" else (e not in after)
        if ok:
            out[e] = ("done", None)
        else:
            out[e] = ("failed", errors.get(e) or "The change did not show up when the list was read back.")
    return out


# ── cPanel / Mailman client ─────────────────────────────────────────────────

def _cpanel_base():
    host = os.environ.get("CPANEL_HOST", "65.254.92.49")
    port = os.environ.get("CPANEL_PORT", "2083")
    return f"https://{host}:{port}/execute"


def _cpanel_headers():
    return {"Authorization": f"cpanel {os.environ.get('CPANEL_USERNAME', 'cohere')}:"
                             f"{os.environ.get('CPANEL_API_TOKEN', '')}"}


def _uapi(fn, params=None, write=False):
    url = f"{_cpanel_base()}/Email/{fn}"
    if write:
        r = requests.post(url, headers=_cpanel_headers(), data=params or {}, verify=False, timeout=HTTP_TIMEOUT)
    else:
        r = requests.get(url, headers=_cpanel_headers(), params=params or {}, verify=False, timeout=HTTP_TIMEOUT)
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


@contextmanager
def _mailman_session(address, listid):
    """Logged-in requests.Session for one list's Mailman admin, plus the
    admin base URL and the HTML of the first members page."""
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
        yield s, base, r.text


def _walk_mailman_members(s, base, first_html):
    members = parse_mailman_members(first_html)
    total = parse_mailman_total(first_html)
    letters, chunks = parse_mailman_pages(first_html)

    def get(params):
        page = s.get(f"{base}/members", params=params, timeout=HTTP_TIMEOUT)
        page.raise_for_status()
        return page.text

    pages = [{"chunk": c} for c in chunks]
    for letter in letters:
        html = get({"letter": letter})
        members |= parse_mailman_members(html)
        pages += [{"letter": letter, "chunk": c} for c in parse_mailman_pages(html)[1]]
    for p in pages:
        members |= parse_mailman_members(get(p))
    return {"members": members, "total": total,
            "complete": total is None or total == len(members)}


def _read_mailman_members(address, listid):
    """Every member of one Mailman list.
    Returns {'members': set, 'total': int|None, 'complete': bool}."""
    with _mailman_session(address, listid) as (s, base, first_html):
        return _walk_mailman_members(s, base, first_html)


def _mailman_post_form(s, base, page, fields):
    """Load a Mailman admin form (for its csrf_token) and submit it."""
    form = s.get(f"{base}/{page}", timeout=HTTP_TIMEOUT)
    form.raise_for_status()
    m = _CSRF_RE.search(form.text)
    if m:
        fields = dict(fields, csrf_token=m.group(1))
    r = s.post(f"{base}/{page}", data=dict(fields, setmemberopts_btn="Submit Your Changes"),
               timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    return r.text


def mailman_change(address, listid, action, emails, notify=False):
    """Mass subscribe/remove on Mailman, then read the list back.
    Returns the fresh read ({'members', 'total', 'complete'})."""
    flag = "1" if notify else "0"
    with _mailman_session(address, listid) as (s, base, _):
        if action == "add":
            _mailman_post_form(s, base, "members/add", {
                "subscribe_or_invite": "0",
                "send_welcome_msg_to_this_batch": flag,
                "send_notifications_to_list_owner": "0",
                "subscribees": "\n".join(emails),
                "invitation": "",
            })
        else:
            _mailman_post_form(s, base, "members/remove", {
                "send_unsub_ack_to_this_batch": flag,
                "send_unsub_notifications_to_list_owner": "0",
                "unsubscribees": "\n".join(emails),
            })
        first = s.get(f"{base}/members", timeout=HTTP_TIMEOUT)
        first.raise_for_status()
        return _walk_mailman_members(s, base, first.text)


def forwarder_change(address, action, member):
    if action == "add":
        _uapi("add_forwarder", {"domain": address.split("@", 1)[1], "email": address,
                                "fwdopt": "fwd", "fwdemail": member}, write=True)
    else:
        _uapi("delete_forwarder", {"address": address, "forwarder": member}, write=True)


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


def _current_emails(list_type, address, refresh=False):
    if list_type == "forwarder":
        groups, fetched = fetch_forwarders(refresh)
        if address not in groups:
            raise KeyError(address)
        return groups[address], {"complete": True, "total": len(groups[address]), "fetched_at": fetched}
    data, fetched = fetch_mailman_members(address, refresh)
    return data["members"], {"complete": data["complete"], "total": data["total"], "fetched_at": fetched}


def _get_members(list_type, address, refresh=False):
    """(members_classified, meta) for one group address."""
    emails, meta = _current_emails(list_type, address, refresh)
    return classify_members(emails, load_employee_index()), meta


# ── Changes + audit ─────────────────────────────────────────────────────────

def _actor():
    user = session.get("user") or {}
    real = session.get("original_admin")
    if real:
        return real.get("email") or "", real.get("name"), user.get("email")
    return user.get("email") or "", user.get("name"), None


def _write_audit(list_type, address, action, results):
    """results: [(email, result, detail)]."""
    if not results:
        return
    actor_email, actor_name, acting_as = _actor()
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.executemany("""
                INSERT INTO mailing_list_audit
                    (actor_email, actor_name, acting_as, list_type, list_address,
                     action, member_email, result, detail)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, [(actor_email, actor_name, acting_as, list_type, address, action,
                   e, res, (detail or "")[:500] or None) for e, res, detail in results])
        conn.commit()
    finally:
        conn.close()


def load_history(address, limit=HISTORY_LIMIT):
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute("""
                SELECT created_at, actor_email, actor_name, acting_as, action, member_email, result, detail
                FROM mailing_list_audit
                WHERE list_address = %s
                ORDER BY id DESC
                LIMIT %s
            """, (address, limit))
            return c.fetchall()
    finally:
        conn.close()


def change_members(list_type, address, action, emails, notify=False):
    """Add or remove members, verify by reading the list back, audit every
    email. Returns [(email, result, detail)] in input order."""
    current, _ = _current_emails(list_type, address)
    todo, skipped = plan_change(action, emails, current)
    errors = {}
    after = None
    if todo:
        if list_type == "forwarder":
            for e in todo:
                try:
                    forwarder_change(address, action, e)
                except Exception as ex:
                    logger.exception("mailing lists: forwarder %s %s %s failed", action, address, e)
                    errors[e] = str(ex)
            after, _ = _current_emails(list_type, address, refresh=True)
        else:
            info = fetch_mailman_lists()[0][address]
            try:
                fresh = mailman_change(address, info["listid"], action, todo, notify=notify)
                with _cache_lock:
                    _cache[f"mailman:{address}"] = (time.time(), fresh)
                after = fresh["members"]
            except Exception as ex:
                logger.exception("mailing lists: mailman %s on %s failed", action, address)
                errors = {e: str(ex) for e in todo}
                try:
                    after, _ = _current_emails(list_type, address, refresh=True)
                except Exception:
                    after = set(current) if action == "remove" else set()
    verified = verify_change(action, todo, after or [], errors)
    skip_note = "Already a member." if action == "add" else "Not a member."
    results = []
    for e in emails:
        if e in verified:
            results.append((e,) + verified[e])
        else:
            results.append((e, "skipped", skip_note))
    _write_audit(list_type, address, action, results)
    return results


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
    try:
        history = load_history(address)
    except Exception:
        logger.exception("mailing lists: history for %s failed", address)
        history = []
    return render_template("mailing_lists/detail.html", list_type=list_type, address=address,
                           members=members, meta=meta, info=info, error=error, history=history,
                           summary=summarize(members), max_batch=MAX_BATCH,
                           fetched_at=time.strftime("%Y-%m-%d %H:%M", time.localtime(meta["fetched_at"]))
                           if meta.get("fetched_at") else None)


def _flash_results(action, results, invalid=()):
    verb = "Added" if action == "add" else "Removed"
    done = [e for e, r, _ in results if r == "done"]
    failed = [(e, d) for e, r, d in results if r == "failed"]
    skipped = [e for e, r, _ in results if r == "skipped"]
    if done:
        flash(f"{verb} {len(done)}: {', '.join(done)}", "success")
    if skipped:
        note = "already members" if action == "add" else "not members"
        flash(f"Skipped {len(skipped)} ({note}): {', '.join(skipped)}", "info")
    if failed:
        flash("Failed " + str(len(failed)) + ": " + "; ".join(f"{e} ({d})" for e, d in failed), "danger")
    if invalid:
        flash(f"Not valid email addresses, ignored: {', '.join(invalid)}", "warning")


@mailing_lists_bp.route("/<list_type>/<path:address>/members", methods=["POST"])
@_access_required
def change(list_type, address):
    if list_type not in LIST_TYPES:
        abort(404)
    address = address.lower()
    back = redirect(url_for("mailing_lists.detail", list_type=list_type, address=address))
    if not validate_csrf():
        flash("Your session expired. Please try again.", "danger")
        return back
    action = request.form.get("action")
    if action not in ("add", "remove"):
        abort(400)

    if action == "add":
        emails, invalid = parse_email_input(request.form.get("emails"))
    else:
        emails, invalid = parse_email_input(" ".join(request.form.getlist("member")))
    if address in emails:
        emails.remove(address)
        invalid.append(address)
    if not emails:
        flash("No email addresses to " + action + ".", "warning")
        _flash_results(action, [], invalid)
        return back
    if len(emails) > MAX_BATCH:
        flash(f"At most {MAX_BATCH} addresses at a time.", "warning")
        return back

    try:
        results = change_members(list_type, address, action, emails,
                                 notify=request.form.get("notify") == "1")
    except KeyError:
        abort(404)
    except Exception as e:
        logger.exception("mailing lists: %s on %s %s failed", action, list_type, address)
        flash(f"Could not {action} members: {e}", "danger")
        return back
    _flash_results(action, results, invalid)
    return back


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
