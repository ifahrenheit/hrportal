"""
Read-only access to the biometric timestamp CSVs kept in Google Drive.

Layout (verified 2026-10-22):
    Cohere Raw Logs (ROOT_FOLDER_ID) / YYYY / YYYYMM / YYYYMMDD-.csv

Each CSV has the header `companyid,Day,Time,Type`, e.g.
    210528-02,10/21/2026,14:19,in
A file holds one shift date's punches, so an overnight clock-out lands in the
previous day's file with the next calendar Day -- rows are never filtered by
their file's date.

Auth, chosen by configuration (never switched automatically on failure):
- GOOGLE_DRIVE_SA_KEY_FILE set -> service account (path to its JSON key,
  kept outside the repo, chmod 600). Signs a JWT with authlib and trades it
  for a drive.readonly access token. The folders must be shared with the
  service account; it may not see the root, so year folders are reached
  through YEAR_FOLDERS first.
- otherwise GOOGLE_DRIVE_API_KEY against link-shared folders.
Credentials only go in request headers: never in URLs, logs or exception
text (requests' own exceptions are swallowed and replaced with DriveError).

Nothing here writes to Drive: only files.list and files.get?alt=media.

Downloads are cached in memory and on disk (instance/drive_timestamps/,
gitignored) keyed by file id + version, so a file is downloaded once per
version even across service restarts. Google throttles bulk downloads of
link-shared files from one IP (it answers 403 with a "Sorry..." HTML page),
so re-downloading must stay rare.
"""

import csv
import io
import logging
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta

import json

import requests

log = logging.getLogger(__name__)

ROOT_FOLDER_ID = "14ZDdB-v5GzFnl27A8hsx8tfjAALClRqj"
# Known year folders, so a service account shared only on a year folder can
# still reach it. Years missing here are looked up under the root. 2025 is
# deliberately absent: its owner has left and it can't be shared with the
# service account, so the portal covers 2026 onward only.
YEAR_FOLDERS = {
    2026: "1ubtSF822Y8pA5XEY3t_ipJfnv-MuFOiv",
}
SA_SCOPE = "https://www.googleapis.com/auth/drive.readonly"
API = "https://www.googleapis.com/drive/v3/files"
FOLDER_MIME = "application/vnd.google-apps.folder"
FILE_RE = re.compile(r"^(\d{4})(\d{2})(\d{2})-\.csv$")
TIMEOUT = (5, 20)        # (connect, read) seconds
LISTING_TTL = 300        # seconds a folder listing is reused without ?refresh
REFRESH_MIN_GAP = 30     # a ?refresh within this many seconds reuses the listing
MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_WORKERS = 4
MAX_CACHED_FILES = 400   # ~13 months of daily files
DISK_CACHE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                              "instance", "drive_timestamps")

# Source header (lower-cased) -> our field name.
HEADER_MAP = {"companyid": "employee_id", "day": "day", "time": "time", "type": "type"}


class DriveError(Exception):
    """Drive could not be read (network, auth, quota). Message is safe to show."""


class DriveNotFound(DriveError):
    """A listed file/folder id no longer exists (replaced or deleted)."""


class DriveConfigError(DriveError):
    """GOOGLE_DRIVE_API_KEY is missing."""


_local = threading.local()   # one requests.Session per thread
_lock = threading.Lock()
_listing_cache = {}      # parent_id -> (fetched_at, [files])
_file_cache = {}         # file_id -> (version, parsed_result); pruned per listing
_access_cache = {}       # folder_id -> checked_at (folder is visible to us)
_token = {"value": None, "expires": 0}


def auth_mode():
    return "service_account" if os.getenv("GOOGLE_DRIVE_SA_KEY_FILE", "").strip() else "api_key"


def _api_key():
    key = os.getenv("GOOGLE_DRIVE_API_KEY", "").strip()
    if not key:
        raise DriveConfigError("Google Drive API key is not configured on the server.")
    return key


def _sa_token(force=False):
    """Cached drive.readonly access token for the service account."""
    with _lock:
        if not force and _token["value"] and time.time() < _token["expires"] - 60:
            return _token["value"]
    path = os.getenv("GOOGLE_DRIVE_SA_KEY_FILE", "").strip()
    try:
        with open(path) as fh:
            info = json.load(fh)
        email, pem = info["client_email"], info["private_key"]
        token_uri = info.get("token_uri") or "https://oauth2.googleapis.com/token"
    except (OSError, ValueError, KeyError, TypeError):
        raise DriveConfigError("The Google service account key file can't be read on the server.") from None
    from authlib.jose import jwt
    now = int(time.time())
    assertion = jwt.encode({"alg": "RS256", "kid": info.get("private_key_id")},
                           {"iss": email, "scope": SA_SCOPE, "aud": token_uri, "iat": now, "exp": now + 3600},
                           pem)
    try:
        r = _session().post(token_uri, timeout=TIMEOUT, data={
            "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
            "assertion": assertion.decode() if isinstance(assertion, bytes) else assertion})
    except requests.RequestException as e:
        log.warning("Drive token request failed: %s", type(e).__name__)
        raise DriveError("Could not reach Google to sign in the service account.") from None
    if r.status_code != 200:
        log.warning("Drive token request returned HTTP %s", r.status_code)
        raise DriveError(f"Google rejected the service account sign-in (HTTP {r.status_code}).")
    try:
        data = r.json()
        value, ttl = data["access_token"], int(data.get("expires_in", 3600))
    except (ValueError, KeyError, TypeError):
        raise DriveError("Google returned an unreadable sign-in response.") from None
    with _lock:
        _token.update(value=value, expires=time.time() + ttl)
    return value


def _auth_headers(force=False):
    if auth_mode() == "service_account":
        return {"Authorization": f"Bearer {_sa_token(force)}"}
    return {"X-Goog-Api-Key": _api_key()}


def _session():
    if not hasattr(_local, "s"):
        _local.s = requests.Session()
    return _local.s


def _get(url, params, what):
    """
    GET with the configured credentials, one retry on 429/5xx (and, for the
    service account, on 401 with a fresh token). Credentials go in headers,
    so they are never part of a URL; exceptions are still swallowed in case
    a library ever echoes request details.
    """
    headers = _auth_headers()
    for attempt in (1, 2):
        try:
            r = _session().get(url, params=params, headers=headers, timeout=TIMEOUT)
        except requests.RequestException as e:
            log.warning("Drive %s failed: %s", what, type(e).__name__)
            if attempt == 2:
                raise DriveError(f"Could not reach Google Drive ({what}).") from None
            time.sleep(1)
            continue
        if r.status_code == 401 and attempt == 1 and auth_mode() == "service_account":
            headers = _auth_headers(force=True)
            continue
        if r.status_code in (429, 500, 502, 503, 504) and attempt == 1:
            time.sleep(2)
            continue
        if r.status_code != 200:
            log.warning("Drive %s returned HTTP %s", what, r.status_code)
            if r.status_code == 403 and "text/html" in r.headers.get("content-type", ""):
                # Google's "Sorry..." automated-traffic page, not a permission error.
                raise DriveError(f"Google is temporarily limiting downloads from this server ({what}). "
                                 "Wait a few minutes, then use Refresh Records.")
            if r.status_code in (401, 403):
                hint = ("Check that the folder is still shared with the service account."
                        if auth_mode() == "service_account"
                        else "Check the API key and that the folder is still shared by link.")
                raise DriveError(f"Google Drive refused access ({what}, HTTP {r.status_code}). {hint}")
            if r.status_code == 404:
                raise DriveNotFound(f"Google Drive item not found ({what}).")
            raise DriveError(f"Google Drive error ({what}, HTTP {r.status_code}).")
        return r
    raise DriveError(f"Google Drive error ({what}).")


def list_children(parent_id, refresh=False):
    """All non-trashed children of a folder, following nextPageToken."""
    if not re.fullmatch(r"[A-Za-z0-9_-]+", parent_id or ""):
        raise DriveError("Bad folder id.")
    now = time.time()
    with _lock:
        hit = _listing_cache.get(parent_id)
    if hit and now - hit[0] < (REFRESH_MIN_GAP if refresh else LISTING_TTL):
        return hit[1]

    files, token = [], None
    while True:
        params = {
            "q": f"'{parent_id}' in parents and trashed=false",
            "fields": "nextPageToken,files(id,name,mimeType,modifiedTime,md5Checksum,size)",
            "pageSize": 1000,
            "supportsAllDrives": "true",
            "includeItemsFromAllDrives": "true",
        }
        if token:
            params["pageToken"] = token
        try:
            data = _get(API, params, "list folder").json()
            files.extend(data["files"])
        except (ValueError, KeyError, TypeError):
            raise DriveError("Google Drive returned an unreadable folder listing.") from None
        token = data.get("nextPageToken")
        if not token:
            break
    with _lock:
        _listing_cache[parent_id] = (now, files)
    return files


def _check_folder(folder_id, label, refresh=False):
    """
    Confirm we can see a folder before trusting its listing: files.list on a
    folder we can't access returns an empty list, which would otherwise look
    like a month with no files.
    """
    with _lock:
        seen = _access_cache.get(folder_id)
    if seen and not refresh and time.time() - seen < LISTING_TTL:
        return
    try:
        _get(f"{API}/{folder_id}", {"fields": "id,mimeType", "supportsAllDrives": "true"}, f"open {label}")
    except DriveNotFound:
        who = "the service account" if auth_mode() == "service_account" else "this server"
        raise DriveError(f"The {label} folder in Google Drive isn't shared with {who}.") from None
    with _lock:
        _access_cache[folder_id] = time.time()


def _find_folder(parent_id, name, refresh):
    for f in list_children(parent_id, refresh):
        if f.get("mimeType") == FOLDER_MIME and f.get("name") == name:
            return f["id"]
    return None


def available_years(refresh=False):
    """Year folder names under the root, newest first."""
    return sorted((f["name"] for f in list_children(ROOT_FOLDER_ID, refresh)
                   if f.get("mimeType") == FOLDER_MIME and re.fullmatch(r"\d{4}", f.get("name", ""))),
                  reverse=True)


def month_files(year, month, refresh=False):
    """
    The selected month's daily CSV files, sorted by name.
    Returns (files, folder_found). folder_found=False means the year or month
    folder doesn't exist yet -- a real "no records", unlike a DriveError.
    """
    year_id = YEAR_FOLDERS.get(year)
    if year_id:
        _check_folder(year_id, f"{year:04d}", refresh)
    else:
        _check_folder(ROOT_FOLDER_ID, "Cohere Raw Logs", refresh)
        year_id = _find_folder(ROOT_FOLDER_ID, f"{year:04d}", refresh)
    if not year_id:
        return [], False
    month_id = _find_folder(year_id, f"{year:04d}{month:02d}", refresh)
    if not month_id:
        return [], False
    out = []
    for f in list_children(month_id, refresh):
        m = FILE_RE.match(f.get("name", ""))
        if f.get("mimeType") == FOLDER_MIME or not m:
            continue
        if int(m.group(1)) != year or int(m.group(2)) != month:
            continue
        out.append(f)
    return sorted(out, key=lambda f: f["name"]), True


def _decode(raw):
    for enc in ("utf-8-sig", "cp1252"):
        try:
            return raw.decode(enc), enc
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1"), "latin-1"


def parse_csv(text, filename):
    """
    Parse one timestamp CSV.
    Returns {"rows": [...], "problems": [...]}. A row is
      {employee_id, day, time, type, ts (datetime), file}
    Rows that fail validation are reported in problems (with line number and
    raw values), never dropped silently and never patched with guessed values.
    """
    rows, problems = [], []
    reader = csv.reader(io.StringIO(text))
    header = None
    for line_no, rec in enumerate(reader, start=1):
        if not any(c.strip() for c in rec):
            continue
        if header is None:
            header = [h.strip().lower() for h in rec]
            missing = [h for h in HEADER_MAP if h not in header]
            if missing:
                skipped = sum(1 for r in reader if any(c.strip() for c in r))
                problems.append({"file": filename, "line": line_no, "whole_file": True,
                                 "error": f"missing column(s): {', '.join(missing)}; "
                                          f"{skipped} data line(s) not loaded",
                                 "raw": ",".join(rec)})
                return {"rows": [], "problems": problems}
            idx = {HEADER_MAP[h]: header.index(h) for h in HEADER_MAP}
            continue
        if len(rec) <= max(idx.values()):
            problems.append({"file": filename, "line": line_no, "error": "too few columns",
                             "raw": ",".join(rec)})
            continue
        emp = rec[idx["employee_id"]].strip()
        day = rec[idx["day"]].strip()
        tm = rec[idx["time"]].strip()
        typ = rec[idx["type"]].strip().lower()
        err = None
        if not emp:
            err = "blank employee id"
        elif typ not in ("in", "out"):
            err = f"unknown type {rec[idx['type']]!r}"
        else:
            try:
                ts = datetime.strptime(f"{day} {tm}", "%m/%d/%Y %H:%M")
            except ValueError:
                err = "bad day/time"
        if err:
            problems.append({"file": filename, "line": line_no, "error": err, "raw": ",".join(rec)})
            continue
        rows.append({"employee_id": emp, "day": day, "time": tm, "type": typ, "ts": ts,
                     "file": filename})
    if header is None:
        problems.append({"file": filename, "line": 0, "error": "empty file", "raw": "",
                         "whole_file": True})
    return {"rows": rows, "problems": problems}


def _load_file(f):
    version = (f.get("modifiedTime"), f.get("md5Checksum"), f.get("size"))
    with _lock:
        hit = _file_cache.get(f["id"])
    if hit and hit[0] == version:
        return hit[1], False
    if int(f.get("size") or 0) > MAX_FILE_BYTES:
        return {"rows": [], "problems": [{"file": f["name"], "line": 0, "raw": "", "whole_file": True,
                                          "error": f"file too large ({f['size']} bytes), not loaded"}]}, False
    raw = _disk_read(f["id"], version)
    fresh = raw is None
    if fresh:
        raw = _get(f"{API}/{f['id']}", {"alt": "media", "supportsAllDrives": "true"},
                   f"download {f['name']}").content
        _disk_write(f["id"], version, raw)
    text, _enc = _decode(raw)
    parsed = parse_csv(text, f["name"])
    with _lock:
        _file_cache.pop(f["id"], None)
        _file_cache[f["id"]] = (version, parsed)
        while len(_file_cache) > MAX_CACHED_FILES:
            _file_cache.pop(next(iter(_file_cache)))
    return parsed, fresh


def _disk_path(file_id, version):
    tag = re.sub(r"[^A-Za-z0-9]", "", "".join(str(v or "") for v in version))
    return os.path.join(DISK_CACHE_DIR, f"{file_id}__{tag}.csv")


def _disk_read(file_id, version):
    try:
        with open(_disk_path(file_id, version), "rb") as fh:
            return fh.read()
    except OSError:
        return None


def _disk_write(file_id, version, raw):
    """Best effort: a failed cache write never fails the request."""
    try:
        os.makedirs(DISK_CACHE_DIR, mode=0o700, exist_ok=True)
        path = _disk_path(file_id, version)
        for old in os.listdir(DISK_CACHE_DIR):
            if old.startswith(f"{file_id}__"):
                os.remove(os.path.join(DISK_CACHE_DIR, old))
        tmp = path + ".tmp"
        with open(tmp, "wb") as fh:
            fh.write(raw)
        os.replace(tmp, path)
    except OSError as e:
        log.warning("Drive timestamp disk cache write failed: %s", type(e).__name__)


def file_date(f):
    """Shift date a daily file covers, from its YYYYMMDD-.csv name."""
    return datetime.strptime(f["name"][:8], "%Y%m%d").date()


def dedupe_by_name(files):
    """
    Keep one file per name: the most recently modified. Drive allows two
    files with the same name in a folder, which happens when a corrected
    CSV is uploaded as a new file instead of replacing the old one; loading
    both would show that day's punches twice.
    Returns (kept, duplicates) where duplicates lists what was ignored.
    """
    by_name = {}
    for f in files:
        by_name.setdefault(f["name"], []).append(f)
    kept, dups = [], []
    for name in sorted(by_name):
        group = sorted(by_name[name], key=lambda f: f.get("modifiedTime") or "", reverse=True)
        kept.append(group[0])
        if len(group) > 1:
            dups.append({"name": name, "copies": len(group),
                         "kept_modified": group[0].get("modifiedTime"),
                         "ignored_modified": [g.get("modifiedTime") for g in group[1:]]})
    return kept, dups


def _months_between(start, end):
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        yield y, m
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)


def _range_files(start, end, refresh):
    files, missing = [], []
    for y, m in _months_between(start, end):
        month, found = month_files(y, m, refresh)
        if not found:
            missing.append(f"{y:04d}{m:02d}")
        files.extend(f for f in month if start <= file_date(f) <= end)
    return files, missing


def load_range(start, end, refresh=False):
    """
    All punches from the daily files whose shift date is in [start, end],
    across as many month folders as the range touches. Rows are selected by
    file (shift date), so the last shift's overnight clock-out is included
    even though its Day is end + 1.
    Returns {rows, problems, files, duplicates, missing_months, downloaded}.
    Raises DriveError if Drive can't be read -- callers must not treat that as
    an empty range.
    """
    files, missing = _range_files(start, end, refresh)
    files, dups = dedupe_by_name(files)
    rows, problems, downloaded = [], [], 0
    if files:
        try:
            with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
                results = list(pool.map(_load_file, files))
        except DriveNotFound:
            # A cached listing named a file that was since replaced: relist once.
            with _lock:
                _listing_cache.clear()
            files, missing = _range_files(start, end, refresh=True)
            files, dups = dedupe_by_name(files)
            with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
                results = list(pool.map(_load_file, files))
        for parsed, fresh in results:
            rows.extend(parsed["rows"])
            problems.extend(parsed["problems"])
            downloaded += fresh
    return {"rows": rows, "problems": problems, "duplicates": dups, "missing_months": missing,
            "files": [{"name": f["name"], "modified": f.get("modifiedTime")} for f in files],
            "downloaded": downloaded}


def load_month(year, month, refresh=False):
    """load_range over one calendar month; adds folder_found for that month."""
    start = date(year, month, 1)
    end = (date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)) - timedelta(days=1)
    res = load_range(start, end, refresh)
    res["folder_found"] = not res["missing_months"]
    return res


def fmt_day(ts):
    return f"{ts.month}/{ts.day}/{ts.year}"


def fmt_time(ts):
    return f"{ts.hour}:{ts.minute:02d}"
