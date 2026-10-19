"""
Mailing Lists blueprint: group email addresses that live in cPanel, managed
from the portal instead of cPanel. Members can be viewed, added and removed,
and new group addresses created (same inputs as cPanel's own forms);
deleting a group address is still done in cPanel.

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


def _uapi(fn, params=None, write=False, module="Email"):
    url = f"{_cpanel_base()}/{module}/{fn}"
    if write:
        r = requests.post(url, headers=_cpanel_headers(), data=params or {}, verify=False, timeout=HTTP_TIMEOUT)
    else:
        r = requests.get(url, headers=_cpanel_headers(), params=params or {}, verify=False, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    j = r.json()
    if not j.get("status"):
        raise RuntimeError(f"cPanel {module}::{fn} failed: {'; '.join(j.get('errors') or []) or 'unknown error'}")
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


# ── Creating a group address ────────────────────────────────────────────────
# Mirrors cPanel's own forms: "Add Forwarder" (address, domain, forward-to
# address) and "Create a Mailing List" (list name, domain, password, access
# type Public/Private). The portal additionally takes several first members.

_LOCAL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9._-]{0,62}[a-z0-9])?$")


def validate_local_part(local):
    """None if ok, else a message. Letters, digits, '.', '_' and '-' only,
    starting and ending with a letter/digit, no '..'."""
    if not local:
        return "Enter the address name (the part before @)."
    if not _LOCAL_RE.match(local) or ".." in local:
        return ("Use only lowercase letters, numbers, dots, dashes and underscores, "
                "starting and ending with a letter or number.")
    return None


def find_conflict(address, forwarders, mailman_lists, mailboxes):
    """What already uses this address, or None."""
    if address in mailboxes:
        return "an email account"
    if address in forwarders:
        return "a forwarder group"
    if address in mailman_lists:
        return "a Mailman list"
    return None


def fetch_mail_domains(refresh=False):
    return _cached("mail_domains",
                   lambda: sorted((x.get("domain") or "").lower() for x in _uapi("list_mail_domains") or []
                                  if x.get("domain")), refresh)


def fetch_mailboxes(refresh=False):
    return _cached("mailboxes",
                   lambda: {(x.get("email") or "").lower() for x in _uapi("list_pops") or []}, refresh)


def required_password_strength():
    """cPanel's minimum for list passwords. cPanel has no API to score a
    password; Email::add_list itself rejects one that is too weak."""
    data, _ = _cached("pw_strength", lambda: _uapi("get_required_strength", {"app": "list"},
                                                   module="PasswdStrength"))
    return int((data or {}).get("strength") or 0)


def _audit_create(list_type, address, result, detail):
    _write_audit(list_type, address, "create", [(address, result, detail)])


def create_group(list_type, local, domain, members, password=None, private=False, notify=False):
    """Create a forwarder group or Mailman list, then add the first members.
    Returns (created: bool, message, member_results)."""
    address = f"{local}@{domain}"
    forwarders, _ = fetch_forwarders(refresh=True)
    mailman, _ = fetch_mailman_lists(refresh=True)
    mailboxes, _ = fetch_mailboxes(refresh=True)
    conflict = find_conflict(address, forwarders, mailman, mailboxes)
    if conflict:
        return False, f"{address} already exists as {conflict}.", []

    if list_type == "mailman":
        try:
            _uapi("add_list", {"list": local, "domain": domain, "password": password,
                               "private": "1" if private else "0"}, write=True)
        except Exception as ex:
            logger.exception("mailing lists: add_list %s failed", address)
            _audit_create(list_type, address, "failed", str(ex))
            return False, f"cPanel could not create the list: {ex}", []
        mailman, _ = fetch_mailman_lists(refresh=True)
        if address not in mailman:
            _audit_create(list_type, address, "failed", "List did not show up when read back.")
            return False, "cPanel did not report an error, but the new list did not show up.", []
        _audit_create(list_type, address, "done", "Private" if private else "Public")
        results = change_members(list_type, address, "add", members, notify=notify) if members else []
        return True, f"Mailman list {address} created.", results

    # Forwarder: the group exists once its first forward-to address is added.
    errors = {}
    for e in members:
        try:
            forwarder_change(address, "add", e)
        except Exception as ex:
            logger.exception("mailing lists: add_forwarder %s -> %s failed", address, e)
            errors[e] = str(ex)
    forwarders, _ = fetch_forwarders(refresh=True)
    after = forwarders.get(address, [])
    verified = verify_change("add", members, after, errors)
    results = [(e,) + verified[e] for e in members]
    created = bool(after)
    _audit_create(list_type, address, "done" if created else "failed",
                  None if created else "No forwarder showed up when read back.")
    _write_audit(list_type, address, "add", results)
    if not created:
        return False, "cPanel did not create the forwarder group.", results
    return True, f"Forwarder group {address} created.", results


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


@mailing_lists_bp.route("/new", methods=["GET", "POST"])
@_access_required
def new():
    try:
        domains, _ = fetch_mail_domains()
        min_strength = required_password_strength()
    except Exception as e:
        logger.exception("mailing lists: reading cPanel domains failed")
        flash(f"Could not read from cPanel: {e}", "danger")
        return redirect(url_for("mailing_lists.index"))
    default_domain = os.environ.get("CPANEL_DOMAIN", "cohere.ph").lower()
    form = {"list_type": "forwarder", "local": "", "domain": default_domain if default_domain in domains
            else (domains[0] if domains else ""), "members": "", "access": "public", "notify": False}

    def render(**kw):
        return render_template("mailing_lists/new.html", form=form, domains=domains,
                               min_strength=min_strength, max_batch=MAX_BATCH, **kw)

    if request.method == "GET":
        return render()
    if not validate_csrf():
        flash("Your session expired. Please try again.", "danger")
        return render()

    f = request.form
    form.update({
        "list_type": f.get("list_type") if f.get("list_type") in LIST_TYPES else "forwarder",
        "local": (f.get("local") or "").strip().lower(),
        "domain": (f.get("domain") or "").strip().lower(),
        "members": f.get("members") or "",
        "access": "private" if f.get("access") == "private" else "public",
        "notify": f.get("notify") == "1",
    })
    password, password2 = f.get("password") or "", f.get("password2") or ""
    members, invalid = parse_email_input(form["members"])
    address = f"{form['local']}@{form['domain']}"
    if address in members:
        members.remove(address)

    errors = []
    msg = validate_local_part(form["local"])
    if msg:
        errors.append(msg)
    if form["domain"] not in domains:
        errors.append("Choose one of the domains on the list.")
    if invalid:
        errors.append("These are not valid email addresses: " + ", ".join(invalid))
    if len(members) > MAX_BATCH:
        errors.append(f"At most {MAX_BATCH} members at a time.")
    if form["list_type"] == "forwarder" and not members:
        errors.append("A forwarder needs at least one address to forward to.")
    if form["list_type"] == "mailman":
        if not password:
            errors.append("Enter a password for the list.")
        elif password != password2:
            errors.append("The two passwords do not match.")
        elif len(password) < 8:
            errors.append("The password is too short. Use at least 8 characters, or Generate.")
    if errors:
        return render(errors=errors)

    try:
        created, message, results = create_group(
            form["list_type"], form["local"], form["domain"], members,
            password=password, private=form["access"] == "private", notify=form["notify"])
    except Exception as e:
        logger.exception("mailing lists: create %s failed", address)
        return render(errors=[f"Could not create {address}: {e}"])
    if not created:
        return render(errors=[message])
    flash(message, "success")
    _flash_results("add", results)
    return redirect(url_for("mailing_lists.detail", list_type=form["list_type"], address=address))


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
