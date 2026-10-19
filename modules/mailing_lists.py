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

import hashlib
import logging
import os
import re
import threading
import time
from contextlib import contextmanager
from functools import wraps
from html.parser import HTMLParser
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
MAX_SYNC_INPUT = 1000
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


def ml_can_offboard():
    """Offboarding staff may look up and remove a leaver from every list."""
    return ml_can_access() or bool((session.get("permissions") or {}).get("can_onboarding"))


def _offboard_access_required(f):
    @wraps(f)
    def wrapped(*args, **kwargs):
        if "user" not in session:
            return redirect("/")
        if not ml_can_offboard():
            abort(404)
        return f(*args, **kwargs)
    return wrapped


@mailing_lists_bp.app_context_processor
def _inject_access():
    return {"ml_can_access": ml_can_access, "ml_can_offboard": ml_can_offboard}


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


_NOMAIL_RE = re.compile(r'<input[^>]*name="([^"\s]+%40[^"\s]+)_nomail"[^>]*>\s*(\[[A-Z?]\])?', re.I)
NOMAIL_REASONS = {"B": "bounces", "A": "turned off by an admin", "U": "turned off by the member", "?": "unknown reason"}


def parse_mailman_nomail(html):
    """{email: reason code} for members whose delivery is switched off
    (Mailman ticks <addr>_nomail and appends [B]ounce/[A]dmin/[U]ser/[?])."""
    out = {}
    for m in _NOMAIL_RE.finditer(html or ""):
        if re.search(r"\bchecked\b", m.group(0), re.I):
            out[unquote(m.group(1)).lower()] = (m.group(2) or "[?]")[1]
    return out


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


def classify_members(emails, employees, nomail=None):
    """emails: iterable of addresses. employees: {email_lower: row} with
    schedule_name/status/group_name/employee_id. nomail: {email: reason
    code} for Mailman members with delivery off. Returns a list of dicts."""
    nomail = nomail or {}
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
            "delivery_off": nomail.get(e.lower()),
        })
    return out


def summarize(members):
    s = {"total": len(members), "active": 0, "separated": 0, "external": 0, "delivery_off": 0}
    for m in members:
        s[m["kind"]] += 1
        s["delivery_off"] += bool(m.get("delivery_off"))
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
        r = requests.post(url, headers=_cpanel_headers(), data=params or {}, verify=False, timeout=HTTP_TIMEOUT)  # dict or [(k, v)]
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
    nomail = parse_mailman_nomail(first_html)
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
        nomail.update(parse_mailman_nomail(html))
        pages += [{"letter": letter, "chunk": c} for c in parse_mailman_pages(html)[1]]
    for p in pages:
        html = get(p)
        members |= parse_mailman_members(html)
        nomail.update(parse_mailman_nomail(html))
    return {"members": members, "total": total, "nomail": nomail,
            "complete": total is None or total == len(members)}


def _read_mailman_members(address, listid):
    """Every member of one Mailman list.
    Returns {'members': set, 'total': int|None, 'complete': bool}."""
    with _mailman_session(address, listid) as (s, base, first_html):
        return _walk_mailman_members(s, base, first_html)


class _FormParser(HTMLParser):
    """Successful controls of the first <form>, the way a browser submits it
    (checked radios/checkboxes, text/hidden inputs, textareas, selects)."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.fields, self._in, self._done = [], False, False
        self._ta = self._sel = self._opt = None

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v if v is not None else "") for k, v in attrs}
        if self._done:
            return
        if tag == "form":
            self._in = True
            return
        if not self._in:
            return
        if tag == "input":
            t, n = a.get("type", "text").lower(), a.get("name")
            if not n or t in ("file", "reset", "button", "image", "submit"):
                return
            if t in ("radio", "checkbox"):
                if "checked" in a:
                    self.fields.append((n, a.get("value", "on")))
            else:
                self.fields.append((n, a.get("value", "")))
        elif tag == "textarea":
            self._ta = [a.get("name"), ""]
        elif tag == "select":
            self._sel = [a.get("name"), None, None]
        elif tag == "option" and self._sel is not None:
            self._opt = [a.get("value"), "", "selected" in a]

    def handle_data(self, data):
        if self._ta is not None:
            self._ta[1] += data
        if self._opt is not None:
            self._opt[1] += data

    def handle_endtag(self, tag):
        if tag == "form" and self._in:
            self._in, self._done = False, True
        elif tag == "textarea" and self._ta is not None:
            v = self._ta[1][1:] if self._ta[1].startswith("\n") else self._ta[1]
            if self._ta[0]:
                self.fields.append((self._ta[0], v))
            self._ta = None
        elif tag == "option" and self._opt is not None:
            val = self._opt[0] if self._opt[0] is not None else self._opt[1].strip()
            if self._sel[1] is None:
                self._sel[1] = val
            if self._opt[2]:
                self._sel[2] = val
            self._opt = None
        elif tag == "select" and self._sel is not None:
            v = self._sel[2] if self._sel[2] is not None else self._sel[1]
            if self._sel[0] and v is not None:
                self.fields.append((self._sel[0], v))
            self._sel = None


def form_fields(html_text):
    p = _FormParser()
    p.feed(html_text or "")
    return p.fields


def _norm(v):
    return (v or "").replace("\r\n", "\n").strip()


def mailman_form_update(s, url, set_fields=None, uncheck=()):
    """Submit the form at `url` exactly as a browser would, with
    `set_fields` ({name: value}) changed and the `uncheck` checkboxes cleared.
    Re-reads the form and checks that exactly those fields changed (and to
    the intended values). Returns the after-fields dict."""
    set_fields = dict(set_fields or {})
    uncheck = set(uncheck)
    page = s.get(url, timeout=HTTP_TIMEOUT)
    page.raise_for_status()
    before = form_fields(page.text)
    post, seen = [], set()
    for k, v in before:
        if k in uncheck:
            continue
        if k in set_fields:
            if k not in seen:
                post.append((k, set_fields[k]))
                seen.add(k)
            continue
        post.append((k, v))
    post += [(k, v) for k, v in set_fields.items() if k not in seen]
    post.append(("submit", "Submit Your Changes"))
    r = s.post(url, data=post, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    if "request forgery" in r.text.lower() or "form lifetime" in r.text.lower():
        raise RuntimeError("Mailman refused the change (request forgery check). Try again.")
    after_page = s.get(url, timeout=HTTP_TIMEOUT)
    after_page.raise_for_status()
    b = {k: _norm(v) for k, v in before if k != "csrf_token"}
    a = {k: _norm(v) for k, v in form_fields(after_page.text) if k != "csrf_token"}
    changed = {k for k in set(a) | set(b) if a.get(k) != b.get(k)}
    expected = {k for k in set_fields if _norm(set_fields[k]) != b.get(k)} | {k for k in uncheck if k in b}
    unexpected = changed - expected
    if unexpected:
        raise RuntimeError("Mailman changed more than intended: " + ", ".join(sorted(unexpected)))
    not_applied = [k for k in set_fields if a.get(k) != _norm(set_fields[k])] + [k for k in uncheck if k in a]
    if not_applied:
        raise RuntimeError("Mailman did not apply: " + ", ".join(sorted(not_applied)))
    return a


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
    return data["members"], {"complete": data["complete"], "total": data["total"], "fetched_at": fetched,
                             "nomail": data.get("nomail") or {}}


def _get_members(list_type, address, refresh=False):
    """(members_classified, meta) for one group address."""
    emails, meta = _current_emails(list_type, address, refresh)
    return classify_members(emails, load_employee_index(), meta.get("nomail")), meta


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


def plan_sync(emails, current):
    """Make the list exactly `emails`: (to_add, to_remove, unchanged)."""
    current = {c.lower() for c in current}
    wanted = set(emails)
    return ([e for e in emails if e not in current],
            sorted(c for c in current if c not in wanted),
            sorted(current & wanted))


def sync_signature(to_add, to_remove):
    """Stable fingerprint of a sync plan, so a confirm only applies the
    exact plan that was previewed."""
    raw = "+" + ",".join(sorted(to_add)) + "|-" + ",".join(sorted(to_remove))
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def sync_members(list_type, address, emails, notify=False):
    """Apply a sync: adds first (so a forwarder group never ends up with no
    targets mid-way), then removes. Returns (add_results, remove_results,
    unchanged)."""
    current, _ = _current_emails(list_type, address)
    to_add, to_remove, unchanged = plan_sync(emails, current)
    added = change_members(list_type, address, "add", to_add, notify=notify) if to_add else []
    removed = change_members(list_type, address, "remove", to_remove, notify=notify) if to_remove else []
    return added, removed, unchanged


# ── Mailman member delivery + sender filters ────────────────────────────────

def mailman_set_delivery(address, emails, enable=True):
    """Switch mail delivery on (or off) for members, through the members page
    form of the letter each member is listed under. Returns
    [(email, result, detail)]. The fresh member read is cached."""
    from urllib.parse import quote
    info = fetch_mailman_lists()[0][address]
    results = []
    with _mailman_session(address, info["listid"]) as (s, base, first_html):
        pending = {e.lower() for e in emails}
        pages = {}
        for e in sorted(pending):
            key = quote(e, safe="") + "_nomail"
            letter = e[0]
            found = None
            first = s.get(f"{base}/members", params={"letter": letter}, timeout=HTTP_TIMEOUT).text
            candidates = [f"{base}/members?letter={letter}"] + [
                f"{base}/members?letter={letter}&chunk={c}" for c in parse_mailman_pages(first)[1]]
            for url in candidates:
                html = first if url == candidates[0] else s.get(url, timeout=HTTP_TIMEOUT).text
                if f'name="{key}"' in html:
                    found = url
                    break
            if not found:
                results.append((e, "failed", "Not found on Mailman's member pages."))
                continue
            pages.setdefault(found, []).append((e, key))
        for url, members in pages.items():
            fields = [k for _, k in members]
            try:
                if enable:
                    mailman_form_update(s, url, uncheck=fields)
                else:
                    mailman_form_update(s, url, set_fields={k: "on" for k in fields})
                results += [(e, "done", None) for e, _ in members]
            except Exception as ex:
                logger.exception("mailing lists: delivery change on %s failed", address)
                results += [(e, "failed", str(ex)) for e, _ in members]
        first = s.get(f"{base}/members", timeout=HTTP_TIMEOUT)
        fresh = _walk_mailman_members(s, base, first.text)
    with _cache_lock:
        _cache[f"mailman:{address}"] = (time.time(), fresh)
    _write_audit("mailman", address, "delivery", [
        (e, r, ("Delivery switched " + ("on" if enable else "off")) + (f" — {d}" if d else "")) for e, r, d in results])
    return results


SENDER_FILTERS = (
    ("accept_these_nonmembers", "Always accept"),
    ("hold_these_nonmembers", "Always hold for approval"),
    ("reject_these_nonmembers", "Always reject"),
    ("discard_these_nonmembers", "Always discard"),
)
_FILTER_NAMES = dict(SENDER_FILTERS)


def sender_entry_problem(entry):
    """Why a sender-filter line won't work in Mailman 2, or None. Mailman
    matches exact addresses, or a regular expression starting with ^.
    Shell-style wildcards like *@domain are taken literally and never match."""
    e = (entry or "").strip()
    if e.startswith("^"):
        try:
            re.compile(e)
            return None
        except re.error:
            return "not a valid pattern"
    if "*" in e:
        return "wildcards (*) don't work in Mailman"
    if not _EMAIL_RE.match(e.lower()):
        return "not an email address"
    return None


def wildcard_to_regex(entry):
    """'*@cohere.ph' -> '^.*@cohere\\.ph$' (what the entry was meant to do)."""
    return "^" + ".*".join(re.escape(part) for part in entry.strip().lower().split("*")) + "$"


def parse_sender_entries(text):
    return [line.strip() for line in (text or "").replace("\r\n", "\n").split("\n") if line.strip()]


def fetch_sender_filters(address, refresh=False):
    info = fetch_mailman_lists()[0].get(address)
    if not info:
        raise KeyError(address)

    def load():
        with _mailman_session(address, info["listid"]) as (s, base, _):
            page = s.get(f"{base}/privacy/sender", timeout=HTTP_TIMEOUT)
            page.raise_for_status()
            f = dict(form_fields(page.text))
        return {name: parse_sender_entries(f.get(name)) for name, _ in SENDER_FILTERS}
    return _cached(f"senders:{address}", load, refresh)


def update_sender_filter(address, field, add=(), remove=(), replace=None):
    """Add/remove lines in one of Mailman's sender filters (or replace
    `replace` = {old: new}). Verified by re-reading the page; audited."""
    if field not in _FILTER_NAMES:
        raise ValueError(field)
    info = fetch_mailman_lists()[0][address]
    replace = replace or {}
    with _mailman_session(address, info["listid"]) as (s, base, _):
        url = f"{base}/privacy/sender"
        page = s.get(url, timeout=HTTP_TIMEOUT)
        page.raise_for_status()
        current = parse_sender_entries(dict(form_fields(page.text)).get(field))
        remove_l = {r.strip().lower() for r in remove}
        new = [replace.get(c, c) for c in current if c.strip().lower() not in remove_l]
        for a in add:
            if a.strip().lower() not in {n.lower() for n in new}:
                new.append(a.strip())
        if new == current:
            return current, current
        after = mailman_form_update(s, url, set_fields={field: "\n".join(new)})
        result = parse_sender_entries(after.get(field))
    with _cache_lock:
        _cache.pop(f"senders:{address}", None)
    label = _FILTER_NAMES[field]
    rows = [(a, "done", f"Added to '{label}' senders") for a in add if a.strip() not in current]
    rows += [(r, "done", f"Removed from '{label}' senders") for r in remove if r.strip().lower() in {c.lower() for c in current}]
    rows += [(o, "done", f"'{label}' entry {o} changed to {n}") for o, n in replace.items() if o in current]
    _write_audit("mailman", address, "senders", rows)
    return current, result


# ── Mailman moderation queue (admindb) ──────────────────────────────────────
# Mailman holds posts (e.g. from non-members) and, with "Confirm and approve",
# subscription requests until an admin decides. The portal reads
# admindb?details=all (one block per held post) and the admindb summary (for
# (un)subscription requests), and posts decisions back to the same form.
# Mailman's action codes: 0 defer, 1 approve, 2 reject, 3 discard,
# 4 subscribe, 5 unsubscribe.

_HELD_SPLIT_RE = re.compile(r"<h2>\s*Posting Held for Approval[^<]*</h2>", re.I)
_URL_RE = re.compile(r"https?://([A-Za-z0-9.-]+)", re.I)
MOD_ACTIONS = ("accept", "reject", "discard")
_MSG_CODES = {"accept": "1", "reject": "2", "discard": "3"}
# Mailman sender filters: 6 accept_these_nonmembers, 3 discard_these_nonmembers
_REMEMBER_CODES = {"accept": "6", "discard": "3"}
MOD_TIMEOUT = 300          # Mailman can take a while on a big batch
MAX_POSTS_SHOWN = 200


def _strip_tags(s):
    import html as _html
    return _html.unescape(re.sub(r"<[^>]+>", "", s or "")).strip()


def _cell(label, seg):
    m = re.search(rf"<strong>\s*{label}:\s*</strong>\s*</td>\s*<td[^>]*>(.*?)</td>", seg, re.S | re.I)
    return _strip_tags(m.group(1)) if m else ""


def _textarea(seg, name):
    import html as _html
    m = re.search(rf'<textarea[^>]*name="?{re.escape(name)}"?[\s>][^>]*>(.*?)</textarea>', seg, re.S | re.I)
    if not m:
        m = re.search(rf'<textarea[^>]*name="?{re.escape(name)}"?>(.*?)</textarea>', seg, re.S | re.I)
    return _html.unescape(m.group(1)) if m else ""


def parse_spam(headers):
    """What the mail server's spam check said, from the held post's headers."""
    m = re.search(r"^X-Spam-Status:\s*(Yes|No)\s*,\s*score=(-?[\d.]+)", headers or "", re.I | re.M)
    return {
        "flag": bool(m and m.group(1).lower() == "yes"),
        "score": float(m.group(2)) if m else None,
        "phish": bool(re.search(r"\bURI_PHISH\b|_PH_SURBL\b|\bPHISH", headers or "")),
    }


def body_preview(fulltext, limit=1500):
    """Plain-text part only (Mailman appends the raw HTML after it)."""
    text = re.split(r"<!DOCTYPE|<html", fulltext or "", maxsplit=1, flags=re.I)[0].strip()
    return text[:limit] + ("…" if len(text) > limit else "")


def parse_held_messages(details_html):
    out = []
    for seg in _HELD_SPLIT_RE.split(details_html or "")[1:]:
        m = re.search(r'<input[^>]*name="?(\d+)"?[^>]*type="?radio', seg, re.I)
        if not m:
            continue
        rid = int(m.group(1))
        headers = _textarea(seg, f"headers-{rid}")
        fulltext = _textarea(seg, f"fulltext-{rid}")
        out.append({
            "id": rid, "kind": "post",
            "sender": _cell("From", seg).lower(),
            "subject": _cell("Subject", seg),
            "reason": _cell("Reason", seg),
            "received": _cell("Received", seg),
            "spam": parse_spam(headers),
            "preview": body_preview(fulltext),
            "link_domains": sorted({d.lower() for d in _URL_RE.findall(fulltext)}),
            "default_comment": _textarea(seg, f"comment-{rid}").strip(),
        })
    return out


def parse_pending_subs(summary_html):
    """(Un)subscription requests from the admindb summary page: a radio group
    whose values include 4 (subscribe) or 5 (unsubscribe), with the address
    shown in <em> just before it."""
    html_ = summary_html or ""
    groups = {}
    for m in re.finditer(r'<input[^>]*name="?(\d+)"?[^>]*type="?radio"?[^>]*value="?(\d+)', html_, re.I):
        groups.setdefault(int(m.group(1)), set()).add(m.group(2))
    out = []
    for rid, values in sorted(groups.items()):
        kind = "subscribe" if "4" in values else "unsubscribe" if "5" in values else None
        if not kind:
            continue
        pos = re.search(rf'name="?{rid}"?', html_).start()
        addrs = re.findall(r"<em>\s*([^<\s]+@[^<\s]+)\s*</em>", html_[:pos], re.I)
        out.append({"id": rid, "kind": kind, "sender": _strip_tags(addrs[-1]).lower() if addrs else "",
                    "subject": "", "reason": "Asked to " + kind, "received": "", "spam": None,
                    "preview": "", "link_domains": [], "default_comment": ""})
    return out


def _admindb_base(admin_base):
    return admin_base.replace("/admin/", "/admindb/", 1)


def _read_pending(s, admin_base):
    adb = _admindb_base(admin_base)
    details = s.get(adb, params={"details": "all"}, timeout=HTTP_TIMEOUT)
    details.raise_for_status()
    summary = s.get(adb, timeout=HTTP_TIMEOUT)
    summary.raise_for_status()
    return parse_held_messages(details.text) + parse_pending_subs(summary.text), summary.text


def fetch_pending(address, refresh=False):
    lists, _ = fetch_mailman_lists()
    info = lists.get(address)
    if not info:
        raise KeyError(address)

    def load():
        with _mailman_session(address, info["listid"]) as (s, base, _):
            return _read_pending(s, base)[0]
    return _cached(f"pending:{address}", load, refresh)


def moderation_form(items, action, comment="", filter_senders=(), filter_code="3"):
    """Form fields for one decision on several queue items. filter_senders
    are added to a Mailman sender filter (filter_code 6 accept, 3 discard)."""
    from urllib.parse import quote_plus
    data = {}
    for it in items:
        if it["kind"] == "post":
            code = _MSG_CODES[action]
        else:
            code = {"accept": "4" if it["kind"] == "subscribe" else "5", "reject": "2", "discard": "3"}[action]
        data[str(it["id"])] = code
        if action == "reject":
            data[f"comment-{it['id']}"] = comment or it.get("default_comment") or ""
    for sender in filter_senders:
        key = quote_plus(sender)
        data[f"senderfilterp-{key}"] = "1"
        data[f"senderfilter-{key}"] = filter_code
    return data


def moderate(address, action, ids, comment="", filter_future=False, senders=()):
    """Apply one decision to queue items (by id, and/or every held post from
    `senders`), then re-read the queue. filter_future also puts the posts'
    senders on Mailman's always-accept (Accept) or always-discard (Discard)
    filter. Returns [(item, result, detail)]."""
    if action not in MOD_ACTIONS:
        raise ValueError(action)
    info = fetch_mailman_lists()[0][address]
    with _mailman_session(address, info["listid"]) as (s, base, _):
        current, summary_html = _read_pending(s, base)
        by_id = {it["id"]: it for it in current}
        wanted_senders = {x.lower() for x in senders}
        ids = list(dict.fromkeys(list(ids) + [it["id"] for it in current
                                              if it["kind"] == "post" and it["sender"] in wanted_senders]))
        items = [by_id[i] for i in ids if i in by_id]
        gone = [i for i in ids if i not in by_id]
        remember = filter_future and action in _REMEMBER_CODES
        results = []
        if items:
            filter_senders = sorted({it["sender"] for it in items if it["kind"] == "post" and it["sender"]}) \
                if remember else []
            fields = moderation_form(items, action, comment, filter_senders,
                                     _REMEMBER_CODES.get(action, "3"))
            m = _CSRF_RE.search(summary_html)
            if m:
                fields["csrf_token"] = m.group(1)
            fields["submit"] = "Submit All Data"
            r = s.post(_admindb_base(base), data=fields, timeout=MOD_TIMEOUT)
            r.raise_for_status()
            after, _ = _read_pending(s, base)
            left = {it["id"] for it in after}
            with _cache_lock:
                _cache[f"pending:{address}"] = (time.time(), after)
            for it in items:
                if it["id"] in left:
                    results.append((it, "failed", "Still in the queue after the decision was sent."))
                else:
                    results.append((it, "done", None))
        for i in gone:
            results.append(({"id": i, "sender": "", "subject": "", "kind": "post"}, "skipped",
                             "No longer in the queue (someone already handled it)."))
    verb = {"accept": "Accepted", "reject": "Rejected", "discard": "Discarded"}[action]
    _write_audit("mailman", address, "moderate", [
        (it.get("sender") or f"request #{it['id']}", res,
         f"{verb} {'post' if it['kind'] == 'post' else it['kind'] + ' request'}"
         f"{': ' + it['subject'] if it.get('subject') else ''}"
         f"{(' (future mail from sender always ' + ('accepted' if action == 'accept' else 'discarded') + ')') if remember and it['kind'] == 'post' else ''}"
         f"{' — ' + detail if detail else ''}")
        for it, res, detail in results])
    return results


# ── cPanel SpamAssassin blocklist ───────────────────────────────────────────
# SpamAssassin::update_user_preference REPLACES all values of a preference,
# so we always read the current list, append, write the whole list back and
# read it again to confirm.

FREEMAIL_DOMAINS = {
    "gmail.com", "googlemail.com", "yahoo.com", "ymail.com", "outlook.com", "hotmail.com",
    "live.com", "msn.com", "icloud.com", "me.com", "aol.com", "proton.me", "protonmail.com",
    "gmx.com", "gmx.net", "mail.com", "yandex.com", "zoho.com",
}


def blocklist_entry(sender, protected_domains, allow_patterns):
    """The blacklist_from entry to add for a spam sender, or None if it must
    not be blocked. Whole domain normally; the exact address for free-mail
    domains; never our own domains or anything on the allowlist."""
    sender = (sender or "").strip().lower()
    if "@" not in sender:
        return None
    domain = sender.rsplit("@", 1)[1]
    if any(domain == d or domain.endswith("." + d) for d in protected_domains):
        return None
    for pat in allow_patterns:
        p = pat.lower()
        if p in (sender, f"*@{domain}", domain) or (p.startswith("*.") and domain.endswith(p[1:])):
            return None
    return sender if domain in FREEMAIL_DOMAINS else f"*@{domain}"


def spam_blocklist_add(senders):
    """Add senders to SpamAssassin's blacklist_from. Returns (added, already,
    refused) lists of entries / senders."""
    prefs = _uapi("get_user_preferences", module="SpamAssassin") or {}
    current = list(prefs.get("blacklist_from") or [])
    allow = list(prefs.get("whitelist_from") or [])
    protected = set(fetch_mail_domains()[0]) | {os.environ.get("CPANEL_DOMAIN", "cohere.ph").lower()}
    added, already, refused = [], [], []
    for s in senders:
        entry = blocklist_entry(s, protected, allow)
        if entry is None:
            refused.append(s)
        elif entry in current or entry in added:
            already.append(entry)
        else:
            added.append(entry)
    if added:
        _uapi("update_user_preference",
              [("preference", "blacklist_from")] + [("value", v) for v in current + added],
              write=True, module="SpamAssassin")
        after = (_uapi("get_user_preferences", module="SpamAssassin") or {}).get("blacklist_from") or []
        missing = [e for e in current + added if e not in after]
        if missing:
            raise RuntimeError("cPanel did not keep these blocklist entries: " + ", ".join(missing))
    return added, already, refused


# ── People across all groups ────────────────────────────────────────────────

READ_WORKERS = 4


def all_memberships(refresh=False):
    """{email: [{"type", "address", "delivery_off"}]} over every forwarder
    group and Mailman list, plus a list of groups that could not be read.
    Mailman lists are read in parallel (cached per list)."""
    from concurrent.futures import ThreadPoolExecutor
    out, errors = {}, []
    forwarders, _ = fetch_forwarders(refresh)
    for addr, targets in forwarders.items():
        for t in targets:
            out.setdefault(t.lower(), []).append({"type": "forwarder", "address": addr, "delivery_off": None})
    lists, _ = fetch_mailman_lists(refresh)

    def read(addr):
        try:
            return addr, fetch_mailman_members(addr, refresh)[0], None
        except Exception as e:
            logger.exception("mailing lists: reading %s failed", addr)
            return addr, None, str(e)

    with ThreadPoolExecutor(READ_WORKERS) as ex:
        for addr, data, err in ex.map(read, sorted(lists)):
            if err:
                errors.append((addr, err))
                continue
            nomail = data.get("nomail") or {}
            for m in data["members"]:
                out.setdefault(m, []).append({"type": "mailman", "address": addr, "delivery_off": nomail.get(m)})
    for groups in out.values():
        groups.sort(key=lambda g: (g["address"], g["type"]))
    return out, errors


def memberships_of(email, refresh=False):
    allm, errors = all_memberships(refresh)
    return allm.get(email.lower(), []), errors


def search_employees(q, limit=20):
    q = (q or "").strip()
    if len(q) < 2:
        return []
    like = f"%{q}%"
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute("""
                SELECT employee_id, schedule_name, LOWER(TRIM(email)) AS email, status, group_name
                FROM gsheet_employees
                WHERE email IS NOT NULL AND email <> ''
                  AND (schedule_name LIKE %s OR email LIKE %s OR employee_id = %s)
                ORDER BY (status = 'Separated'), schedule_name
                LIMIT %s
            """, (like, like, q, limit))
            return c.fetchall()
    finally:
        conn.close()


def remove_everywhere(email, groups):
    """Remove one address from several groups. groups: [(type, address)].
    Returns [(type, address, result, detail)]."""
    out = []
    for list_type, address in groups:
        try:
            res = change_members(list_type, address, "remove", [email.lower()])
            r = res[0] if res else (email, "skipped", "Not a member.")
            out.append((list_type, address, r[1], r[2]))
        except Exception as e:
            logger.exception("mailing lists: removing %s from %s failed", email, address)
            out.append((list_type, address, "failed", str(e)))
    return out


def separated_everywhere(refresh=False):
    """People marked Separated in gsheet_employees who are still on a group:
    [{"email", "name", "employee_id", "exit_date", "groups"}], biggest first."""
    allm, errors = all_memberships(refresh)
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute("""
                SELECT employee_id, schedule_name, LOWER(TRIM(email)) AS email, status, exit_date
                FROM gsheet_employees
                WHERE email IS NOT NULL AND email <> ''
                ORDER BY (status = 'Separated'), last_modified_at
            """)
            index = {}
            for r in c.fetchall():
                index.setdefault(r["email"], r)
    finally:
        conn.close()
    people = []
    for email, groups in allm.items():
        emp = index.get(email)
        if emp and (emp.get("status") or "").lower() == "separated":
            people.append({"email": email, "name": emp.get("schedule_name"), "employee_id": emp.get("employee_id"),
                           "exit_date": emp.get("exit_date"), "groups": groups})
    people.sort(key=lambda p: (-len(p["groups"]), p["name"] or p["email"]))
    return people, errors


# ── Deleting a group address ────────────────────────────────────────────────

def delete_group(list_type, address):
    """Delete a forwarder group (all its targets) or a Mailman list. The
    members at the time are written to the audit log first, so the group can
    be rebuilt with Sync. Returns (ok, message)."""
    current, _ = _current_emails(list_type, address, refresh=True)
    members = sorted(current)
    _write_audit(list_type, address, "delete",
                 [(m, "done", "Member when the group was deleted") for m in members])
    if list_type == "forwarder":
        errors = []
        for m in members:
            try:
                forwarder_change(address, "remove", m)
            except Exception as ex:
                logger.exception("mailing lists: delete forwarder %s -> %s failed", address, m)
                errors.append(f"{m}: {ex}")
        groups, _ = fetch_forwarders(refresh=True)
        ok = address not in groups
        detail = None if ok else ("Still has: " + ", ".join(groups.get(address, [])) + ("; " + "; ".join(errors) if errors else ""))
    else:
        try:
            _uapi("delete_list", {"list": address}, write=True)
            detail = None
        except Exception as ex:
            logger.exception("mailing lists: delete_list %s failed", address)
            detail = str(ex)
        lists, _ = fetch_mailman_lists(refresh=True)
        ok = address not in lists
        if not ok and not detail:
            detail = "cPanel did not report an error, but the list still exists."
        with _cache_lock:
            for k in (f"mailman:{address}", f"pending:{address}", f"senders:{address}"):
                _cache.pop(k, None)
    _write_audit(list_type, address, "delete", [(address, "done" if ok else "failed",
                                                 f"Group deleted ({len(members)} members)" if ok else detail)])
    kind = "Forwarder group" if list_type == "forwarder" else "Mailman list"
    return ok, (f"{kind} {address} deleted. Its {len(members)} members are recorded in the change history."
                if ok else f"Could not delete {address}: {detail}")


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
    return _render_detail(list_type, address.lower(), refresh=request.args.get("refresh") == "1")


def _render_detail(list_type, address, refresh=False, sync_preview=None, mode="add", emails_text=""):
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
    filters, filters_error = None, None
    if list_type == "mailman" and not error:
        try:
            raw, _ = fetch_sender_filters(address, refresh)
            filters = [{"field": f, "label": label,
                        "entries": [{"value": e, "problem": sender_entry_problem(e),
                                     "fix": wildcard_to_regex(e) if "*" in e and not e.startswith("^") else None}
                                    for e in raw.get(f, [])]}
                       for f, label in SENDER_FILTERS]
        except Exception as e:
            logger.exception("mailing lists: sender filters of %s failed", address)
            filters_error = str(e)
    return render_template("mailing_lists/detail.html", list_type=list_type, address=address,
                           filters=filters, filters_error=filters_error, nomail_reasons=NOMAIL_REASONS,
                           members=members, meta=meta, info=info, error=error, history=history,
                           summary=summarize(members), max_batch=MAX_BATCH, max_sync=MAX_SYNC_INPUT,
                           sync_preview=sync_preview, mode=mode, emails_text=emails_text,
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
    if action not in ("add", "remove", "sync_preview", "sync"):
        abort(400)
    if action in ("sync_preview", "sync"):
        return _handle_sync(list_type, address, action)

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


def _parse_group_values(values):
    """'forwarder|hr@x.ph' form values -> [(type, address)] (validated shape)."""
    out = []
    for v in values:
        t, _, a = (v or "").partition("|")
        if t in LIST_TYPES and "@" in a:
            out.append((t, a.strip().lower()))
    return list(dict.fromkeys(out))


@mailing_lists_bp.route("/person", methods=["GET", "POST"])
@_access_required
def person():
    """One person across every group: see where they are, remove from or add
    to several groups at once."""
    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        back = redirect(url_for("mailing_lists.person", email=email))
        if not validate_csrf():
            flash("Your session expired. Please try again.", "danger")
            return back
        if not _EMAIL_RE.match(email):
            flash("Not a valid email address.", "warning")
            return redirect(url_for("mailing_lists.person"))
        action = request.form.get("action")
        groups = _parse_group_values(request.form.getlist("group"))
        if action not in ("add", "remove") or not groups:
            flash("Tick at least one group first.", "warning")
            return back
        known_f, _ = fetch_forwarders()
        known_m, _ = fetch_mailman_lists()
        groups = [(t, a) for t, a in groups if a in (known_f if t == "forwarder" else known_m)]
        done, failed = [], []
        for t, a in groups:
            try:
                res = change_members(t, a, action, [email])
                r = res[0] if res else (email, "skipped", None)
                (done if r[1] in ("done", "skipped") else failed).append((a, r[2]))
            except Exception as e:
                logger.exception("mailing lists: %s %s on %s failed", action, email, a)
                failed.append((a, str(e)))
        if done:
            flash(f"{'Added to' if action == 'add' else 'Removed from'} {len(done)}: {', '.join(a for a, _ in done)}", "success")
        if failed:
            flash("Failed: " + "; ".join(f"{a} ({d})" for a, d in failed), "danger")
        return back

    q = (request.args.get("q") or "").strip()
    email = (request.args.get("email") or "").strip().lower()
    results = search_employees(q) if q and not email else []
    if q and not email and not results and _EMAIL_RE.match(q.lower()):
        email = q.lower()
    groups, errors, emp, others = [], [], None, []
    if email:
        groups, errors = memberships_of(email, refresh=request.args.get("refresh") == "1")
        emp = load_employee_index().get(email)
        mine = {(g["type"], g["address"]) for g in groups}
        f, _ = fetch_forwarders()
        m, _ = fetch_mailman_lists()
        others = sorted([("mailman", a) for a in m if ("mailman", a) not in mine]
                        + [("forwarder", a) for a in f if ("forwarder", a) not in mine], key=lambda x: (x[0] != "mailman", x[1]))
    return render_template("mailing_lists/person.html", q=q, email=email, results=results, groups=groups,
                           errors=errors, emp=emp, others=others, nomail_reasons=NOMAIL_REASONS)


@mailing_lists_bp.route("/cleanup", methods=["GET", "POST"])
@_access_required
def cleanup():
    """Everyone marked Separated who is still on a group, across all groups."""
    if request.method == "POST":
        back = redirect(url_for("mailing_lists.cleanup"))
        if not validate_csrf():
            flash("Your session expired. Please try again.", "danger")
            return back
        picks = {}
        for v in request.form.getlist("pick"):
            email, _, rest = (v or "").partition("|")
            for t, a in _parse_group_values([rest]):
                if _EMAIL_RE.match(email):
                    picks.setdefault((t, a), []).append(email)
        if not picks:
            flash("Tick at least one person first.", "warning")
            return back
        removed, failed = 0, []
        for (t, a), emails in sorted(picks.items()):
            try:
                for e, r, d in change_members(t, a, "remove", sorted(set(emails))):
                    if r == "done":
                        removed += 1
                    elif r == "failed":
                        failed.append(f"{e} from {a} ({d})")
            except Exception as ex:
                logger.exception("mailing lists: cleanup on %s failed", a)
                failed.append(f"{a}: {ex}")
        if removed:
            flash(f"Removed {removed} memberships across {len(picks)} groups.", "success")
        if failed:
            flash("Failed: " + "; ".join(failed[:20]) + (f"; and {len(failed) - 20} more" if len(failed) > 20 else ""), "danger")
        return back
    people, errors = separated_everywhere(refresh=request.args.get("refresh") == "1")
    return render_template("mailing_lists/cleanup.html", people=people, errors=errors,
                           total=sum(len(p["groups"]) for p in people))


@mailing_lists_bp.route("/api/memberships")
@_offboard_access_required
def api_memberships():
    """Which groups an address is on (used by the offboarding page)."""
    email = (request.args.get("email") or "").strip().lower()
    if not _EMAIL_RE.match(email):
        return jsonify({"ok": False, "error": "Not a valid email address."}), 400
    try:
        groups, errors = memberships_of(email)
    except Exception as e:
        logger.exception("mailing lists: memberships of %s failed", email)
        return jsonify({"ok": False, "error": str(e)}), 502
    return jsonify({"ok": True, "email": email, "groups": groups,
                    "unreadable": [a for a, _ in errors]})


@mailing_lists_bp.route("/api/remove-everywhere", methods=["POST"])
@_offboard_access_required
def api_remove_everywhere():
    """Remove one address from every group it is on (offboarding page)."""
    if not validate_csrf():
        return jsonify({"ok": False, "error": "Security check failed, please try again."}), 403
    email = ((request.get_json(silent=True) or {}).get("email") or "").strip().lower()
    if not _EMAIL_RE.match(email):
        return jsonify({"ok": False, "error": "Not a valid email address."}), 400
    try:
        groups, errors = memberships_of(email, refresh=True)
        results = remove_everywhere(email, [(g["type"], g["address"]) for g in groups])
    except Exception as e:
        logger.exception("mailing lists: remove-everywhere %s failed", email)
        return jsonify({"ok": False, "error": str(e)}), 502
    return jsonify({"ok": True, "email": email,
                    "results": [{"type": t, "address": a, "result": r, "detail": d} for t, a, r, d in results],
                    "unreadable": [a for a, _ in errors]})


@mailing_lists_bp.route("/<list_type>/<path:address>/delete", methods=["POST"])
@_access_required
def delete(list_type, address):
    if list_type not in LIST_TYPES:
        abort(404)
    address = address.lower()
    back = redirect(url_for("mailing_lists.detail", list_type=list_type, address=address))
    if not validate_csrf():
        flash("Your session expired. Please try again.", "danger")
        return back
    if (request.form.get("confirm") or "").strip().lower() != address:
        flash("Type the full address exactly to confirm. Nothing was deleted.", "warning")
        return back
    try:
        ok, message = delete_group(list_type, address)
    except KeyError:
        abort(404)
    except Exception as e:
        logger.exception("mailing lists: delete %s %s failed", list_type, address)
        flash(f"Could not delete {address}: {e}", "danger")
        return back
    flash(message, "success" if ok else "danger")
    return redirect(url_for("mailing_lists.index", type="all" if list_type == "forwarder" else None)) if ok else back


@mailing_lists_bp.route("/mailman/<path:address>/delivery", methods=["POST"])
@_access_required
def delivery(address):
    """Switch mail delivery back on (or off) for selected Mailman members."""
    address = address.lower()
    back = redirect(url_for("mailing_lists.detail", list_type="mailman", address=address))
    if not validate_csrf():
        flash("Your session expired. Please try again.", "danger")
        return back
    try:
        known = address in fetch_mailman_lists()[0]
    except Exception as e:
        flash(f"Could not read from cPanel: {e}", "danger")
        return back
    if not known:
        abort(404)
    emails, _ = parse_email_input(" ".join(request.form.getlist("member")))
    if not emails:
        flash("Tick at least one member first.", "warning")
        return back
    enable = request.form.get("enable", "1") == "1"
    try:
        results = mailman_set_delivery(address, emails[:MAX_BATCH], enable=enable)
    except Exception as e:
        logger.exception("mailing lists: delivery change on %s failed", address)
        flash(f"Could not change delivery: {e}", "danger")
        return back
    done = [e for e, r, _ in results if r == "done"]
    failed = [(e, d) for e, r, d in results if r == "failed"]
    if done:
        flash(f"Delivery switched {'on' if enable else 'off'} for {len(done)}: {', '.join(done)}", "success")
    if failed:
        flash("Failed: " + "; ".join(f"{e} ({d})" for e, d in failed), "danger")
    return back


@mailing_lists_bp.route("/mailman/<path:address>/senders", methods=["POST"])
@_access_required
def senders(address):
    """Edit one of the list's sender filters (accept/hold/reject/discard)."""
    address = address.lower()
    back = redirect(url_for("mailing_lists.detail", list_type="mailman", address=address) + "#senders")
    if not validate_csrf():
        flash("Your session expired. Please try again.", "danger")
        return back
    try:
        known = address in fetch_mailman_lists()[0]
    except Exception as e:
        flash(f"Could not read from cPanel: {e}", "danger")
        return back
    if not known:
        abort(404)
    field, op = request.form.get("field"), request.form.get("op")
    if field not in _FILTER_NAMES or op not in ("add", "remove", "fix"):
        abort(400)
    label = _FILTER_NAMES[field]
    try:
        if op == "add":
            entries = [e for e in re.split(r"[\s,;]+", request.form.get("entries") or "") if e]
            bad = [(e, sender_entry_problem(e)) for e in entries if sender_entry_problem(e)]
            if bad:
                flash("Not added: " + "; ".join(f"{e} ({p})" for e, p in bad)
                      + ". Use exact addresses, or a pattern like ^.*@example\\.com$ for a whole domain.", "warning")
                return back
            if not entries:
                flash("Enter at least one address.", "warning")
                return back
            update_sender_filter(address, field, add=entries)
            flash(f"Added to '{label}': {', '.join(entries)}", "success")
        elif op == "remove":
            entry = request.form.get("entry") or ""
            update_sender_filter(address, field, remove=[entry])
            flash(f"Removed from '{label}': {entry}", "success")
        else:
            entry = request.form.get("entry") or ""
            if "*" not in entry or entry.startswith("^"):
                abort(400)
            fixed = wildcard_to_regex(entry)
            update_sender_filter(address, field, replace={entry: fixed})
            flash(f"Changed {entry} to {fixed} in '{label}', so Mailman now matches it.", "success")
    except Exception as e:
        logger.exception("mailing lists: sender filter change on %s failed", address)
        flash(f"Could not change '{label}': {e}", "danger")
    return back


def _handle_sync(list_type, address, action):
    """sync_preview: show what a sync would do (fresh read, nothing changed).
    sync: apply it, but only if the plan still matches what was previewed."""
    back = redirect(url_for("mailing_lists.detail", list_type=list_type, address=address))
    text = request.form.get("emails") or ""
    notify = request.form.get("notify") == "1"
    emails, invalid = parse_email_input(text)
    if address in emails:
        emails.remove(address)
        invalid.append(address)
    if not emails:
        flash("Sync needs the full list of addresses the group should have. Nothing was changed.", "warning")
        _flash_results("add", [], invalid)
        return back
    if len(emails) > MAX_SYNC_INPUT:
        flash(f"At most {MAX_SYNC_INPUT} addresses for a sync.", "warning")
        return back

    try:
        current, _ = _current_emails(list_type, address, refresh=(action == "sync_preview"))
    except KeyError:
        abort(404)
    except Exception as e:
        logger.exception("mailing lists: sync read of %s %s failed", list_type, address)
        flash(f"Could not read the list: {e}", "danger")
        return back
    to_add, to_remove, unchanged = plan_sync(emails, current)
    signature = sync_signature(to_add, to_remove)

    def preview(note=None):
        employees = load_employee_index()
        plan = {"to_add": classify_members(to_add, employees),
                "to_remove": classify_members(to_remove, employees),
                "unchanged": len(unchanged), "invalid": invalid, "signature": signature,
                "notify": notify, "note": note,
                "too_big": len(to_add) > MAX_BATCH or len(to_remove) > MAX_BATCH,
                "removes_all": bool(to_remove) and not unchanged}
        return _render_detail(list_type, address, sync_preview=plan, mode="sync", emails_text=text)

    if action == "sync_preview":
        return preview()
    if request.form.get("signature") != signature:
        return preview("The list changed since the preview, so nothing was applied. "
                       "Check the new preview below and confirm again.")
    if not to_add and not to_remove:
        flash(f"Already in sync: all {len(unchanged)} members match. Nothing to change.", "info")
        return back
    if len(to_add) > MAX_BATCH or len(to_remove) > MAX_BATCH:
        flash(f"A sync can add or remove at most {MAX_BATCH} members at a time.", "warning")
        return back
    try:
        added, removed, unchanged = sync_members(list_type, address, emails, notify=notify)
    except Exception as e:
        logger.exception("mailing lists: sync on %s %s failed", list_type, address)
        flash(f"Sync failed: {e}", "danger")
        return back
    flash(f"Sync done. {len(unchanged)} members were already on the list and were left as is.", "success")
    _flash_results("add", added)
    _flash_results("remove", removed, invalid)
    return back


@mailing_lists_bp.route("/mailman/<path:address>/pending", methods=["GET", "POST"])
@_access_required
def pending(address):
    """Mailman moderation queue for one list: held posts and (un)subscription
    requests, with Accept / Reject / Discard."""
    address = address.lower()
    try:
        known = address in fetch_mailman_lists()[0]
    except Exception as e:
        logger.exception("mailing lists: cPanel read failed")
        flash(f"Could not read from cPanel: {e}", "danger")
        return redirect(url_for("mailing_lists.index"))
    if not known:
        abort(404)
    here = url_for("mailing_lists.pending", address=address)

    if request.method == "POST":
        if not validate_csrf():
            flash("Your session expired. Please try again.", "danger")
            return redirect(here)
        action = request.form.get("action")
        if action not in MOD_ACTIONS:
            abort(400)
        ids = [int(x) for x in request.form.getlist("item") if x.isdigit()]
        senders = [x.strip().lower() for x in request.form.getlist("sender") if "@" in x]
        if not ids and not senders:
            flash("Tick at least one request or sender first.", "warning")
            return redirect(here)
        try:
            results = moderate(address, action, ids,
                               comment=(request.form.get("comment") or "").strip()[:500],
                               filter_future=request.form.get("filter_future") == "1",
                               senders=senders)
        except Exception as e:
            logger.exception("mailing lists: moderation on %s failed", address)
            flash(f"Could not apply the decision: {e}", "danger")
            return redirect(here)
        verb = {"accept": "Accepted", "reject": "Rejected", "discard": "Discarded"}[action]
        done = [it for it, r, _ in results if r == "done"]
        if action == "discard" and request.form.get("spam_block") == "1":
            spam_senders = sorted({it["sender"] for it in done if it.get("kind") == "post" and it.get("sender")})
            if spam_senders:
                _block_in_spamassassin(address, spam_senders)
        failed = [(it, d) for it, r, d in results if r == "failed"]
        skipped = [it for it, r, _ in results if r == "skipped"]
        if done:
            names = [(it.get("subject") or it.get("sender") or f"#{it['id']}") for it in done]
            flash(f"{verb} {len(done)}: " + "; ".join(names[:10])
                  + (f"; and {len(names) - 10} more" if len(names) > 10 else ""), "success")
        if skipped:
            flash(f"Skipped {len(skipped)}: already handled by someone else.", "info")
        if failed:
            flash("Failed " + str(len(failed)) + ": " + "; ".join(
                f"{it.get('subject') or it.get('sender')} ({d})" for it, d in failed), "danger")
        return redirect(here)

    error, items, fetched = None, [], None
    try:
        items, fetched = fetch_pending(address, refresh=request.args.get("refresh") == "1")
    except Exception as e:
        logger.exception("mailing lists: reading queue of %s failed", address)
        error = str(e)
    with _cache_lock:
        cached = _cache.get(f"mailman:{address}")
    member_count = len(cached[1]["members"]) if cached else None
    try:
        history = [h for h in load_history(address) if h["action"] == "moderate"]
    except Exception:
        logger.exception("mailing lists: history for %s failed", address)
        history = []
    posts = [i for i in items if i["kind"] == "post"]
    by_sender = {}
    for p in posts:
        g = by_sender.setdefault(p["sender"], {"sender": p["sender"], "count": 0, "suspicious": 0,
                                               "latest": None, "internal": p["sender"].endswith("@cohere.ph")})
        g["count"] += 1
        g["suspicious"] += bool(p["spam"] and (p["spam"]["flag"] or p["spam"]["phish"]))
        g["latest"] = p
    senders = sorted(by_sender.values(), key=lambda g: -g["count"])
    return render_template("mailing_lists/pending.html", address=address, error=error,
                           posts=list(reversed(posts))[:MAX_POSTS_SHOWN], total_posts=len(posts),
                           senders=senders, max_shown=MAX_POSTS_SHOWN,
                           subs=[i for i in items if i["kind"] != "post"],
                           member_count=member_count, history=history,
                           fetched_at=time.strftime("%Y-%m-%d %H:%M", time.localtime(fetched)) if fetched else None)


def _block_in_spamassassin(address, senders):
    try:
        added, already, refused = spam_blocklist_add(senders)
    except Exception as e:
        logger.exception("mailing lists: SpamAssassin blocklist update failed")
        flash(f"Discarded, but could not update the spam blocklist: {e}", "danger")
        _write_audit("mailman", address, "moderate",
                     [(s, "failed", f"SpamAssassin blocklist: {e}") for s in senders])
        return
    if added:
        flash("Added to cPanel's spam blocklist: " + ", ".join(added), "success")
    if already:
        flash("Already on the spam blocklist: " + ", ".join(already), "info")
    if refused:
        flash("Not blocked (our own domain or on the allowlist): " + ", ".join(refused), "warning")
    _write_audit("mailman", address, "moderate",
                 [(e, "done", "Added to SpamAssassin blocklist") for e in added]
                 + [(e, "skipped", "Already on SpamAssassin blocklist") for e in already]
                 + [(s, "skipped", "Not blocked: own domain or allowlisted") for s in refused])


@mailing_lists_bp.route("/api/pending/<path:address>")
@_access_required
def api_pending(address):
    """Queue counts for one Mailman list; the index page shows them."""
    try:
        items, _ = fetch_pending(address.lower())
    except KeyError:
        abort(404)
    except Exception as e:
        logger.exception("mailing lists: pending count of %s failed", address)
        return jsonify({"ok": False, "error": str(e)}), 502
    return jsonify({"ok": True,
                    "posts": sum(1 for i in items if i["kind"] == "post"),
                    "subs": sum(1 for i in items if i["kind"] != "post")})


@mailing_lists_bp.route("/api/pending-total")
@_access_required
def api_pending_total():
    """Menu badge: everything waiting in Mailman moderation queues, as last
    stored by scripts/mailing_list_pending_check.py (cheap: one query)."""
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute("""SELECT COALESCE(SUM(posts + subs), 0) AS n, MAX(checked_at) AS at
                         FROM mailing_list_pending WHERE checked_at >= NOW() - INTERVAL 1 DAY""")
            row = c.fetchone() or {}
    finally:
        conn.close()
    return jsonify({"count": int(row.get("n") or 0),
                    "checked_at": row["at"].strftime("%Y-%m-%d %H:%M") if row.get("at") else None})


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
