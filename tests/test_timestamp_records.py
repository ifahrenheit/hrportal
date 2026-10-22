"""
Tests for the Timestamp Records page (modules/timestamp_records.py) and its
Google Drive reader (helpers/drive_timestamps.py).

Mostly mocked: Drive calls are patched, and the name lookup is patched so no
DB is touched. LiveDriveTests hits the real Drive folder read-only and is
skipped when GOOGLE_DRIVE_API_KEY isn't set.
"""

import os
import sys
import unittest
from datetime import date, datetime
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests  # noqa: E402

import app as appmod  # noqa: E402
from helpers import drive_timestamps as dt  # noqa: E402
from modules import timestamp_records as tr  # noqa: E402

if "timestamp_records" not in appmod.app.blueprints:
    appmod.app.register_blueprint(tr.timestamp_records_bp)

SAMPLE = (
    "companyid,Day,Time,Type\n"
    "210528-02,10/21/2026,14:19,in\n"
    "210528-02,10/22/2026,0:03,out\n"
    "260812-05,10/21/2026,0:32,in\n"
)


import json  # noqa: E402
import tempfile  # noqa: E402

_TMP_CACHE = tempfile.mkdtemp(prefix="zztest-drive-cache-")
dt.DISK_CACHE_DIR = _TMP_CACHE   # never touch instance/ from tests (the live test re-points it)


def _reset_caches():
    dt._listing_cache.clear()
    dt._file_cache.clear()
    dt._access_cache.clear()
    dt._token.update(value=None, expires=0)
    if dt.DISK_CACHE_DIR == _TMP_CACHE:
        for n in os.listdir(_TMP_CACHE):
            os.remove(os.path.join(_TMP_CACHE, n))


class ParseTests(unittest.TestCase):
    def test_maps_actual_headers(self):
        out = dt.parse_csv(SAMPLE, "20261021-.csv")
        self.assertEqual(out["problems"], [])
        self.assertEqual(len(out["rows"]), 3)
        r = out["rows"][1]
        self.assertEqual(r["employee_id"], "210528-02")
        self.assertEqual(r["type"], "out")
        self.assertEqual(r["ts"], datetime(2026, 10, 22, 0, 3))

    def test_header_case_and_order_insensitive(self):
        out = dt.parse_csv("Type,Time,Day,CompanyID\nIN,7:05,1/2/2026,000123-01\n", "f")
        r = out["rows"][0]
        self.assertEqual((r["employee_id"], r["type"], r["ts"]), ("000123-01", "in", datetime(2026, 1, 2, 7, 5)))

    def test_employee_id_preserved(self):
        out = dt.parse_csv("companyid,Day,Time,Type\n007-00A,10/1/2026,8:00,in\n", "f")
        self.assertEqual(out["rows"][0]["employee_id"], "007-00A")

    def test_duplicates_preserved(self):
        text = "companyid,Day,Time,Type\n" + "1,10/1/2026,8:00,in\n" * 2
        self.assertEqual(len(dt.parse_csv(text, "f")["rows"]), 2)

    def test_malformed_rows_reported_not_dropped_silently(self):
        text = ("companyid,Day,Time,Type\n"
                "1,10/1/2026,8:00,in\n"
                "2,13/40/2026,8:00,in\n"
                "3,10/1/2026,25:00,out\n"
                "4,10/1/2026,8:00,lunch\n"
                ",10/1/2026,8:00,in\n"
                "5,10/1/2026\n"
                "\n")
        out = dt.parse_csv(text, "f.csv")
        self.assertEqual(len(out["rows"]), 1)
        self.assertEqual([p["line"] for p in out["problems"]], [3, 4, 5, 6, 7])

    def test_trailing_comma_in_header(self):
        out = dt.parse_csv("companyid,Day,Time,Type,\n1,10/1/2026,8:00,in\n", "f")
        self.assertEqual((len(out["rows"]), out["problems"]), (1, []))

    def test_missing_header_column(self):
        out = dt.parse_csv("id,Day,Time,Type\n1,10/1/2026,8:00,in\n", "f")
        self.assertEqual(out["rows"], [])
        self.assertIn("companyid", out["problems"][0]["error"])
        self.assertIn("1 data line(s) not loaded", out["problems"][0]["error"])
        self.assertTrue(out["problems"][0]["whole_file"])

    def test_empty_file(self):
        out = dt.parse_csv("", "f")
        self.assertEqual(out["rows"], [])
        self.assertEqual(out["problems"][0]["error"], "empty file")

    def test_decode_bom_and_cp1252(self):
        self.assertEqual(dt._decode("﻿companyid".encode("utf-8"))[0], "companyid")
        self.assertEqual(dt._decode("Pe\xf1a".encode("cp1252"))[0], "Pe\xf1a")

    def test_formatting(self):
        ts = datetime(2026, 10, 2, 0, 4)
        self.assertEqual(dt.fmt_day(ts), "10/2/2026")
        self.assertEqual(dt.fmt_time(ts), "0:04")
        self.assertEqual(dt.fmt_time(datetime(2026, 1, 1, 14, 8)), "14:08")


def _folder(id_, name):
    return {"id": id_, "name": name, "mimeType": dt.FOLDER_MIME}


def _csv(id_, name, mod="2026-10-22T00:00:00Z"):
    return {"id": id_, "name": name, "mimeType": "text/csv", "modifiedTime": mod, "md5Checksum": "x", "size": "1"}


def tearDownModule():
    import shutil
    shutil.rmtree(_TMP_CACHE, ignore_errors=True)


class DriveTests(unittest.TestCase):
    def setUp(self):
        _reset_caches()
        self.env = mock.patch.dict(os.environ, {"GOOGLE_DRIVE_API_KEY": "SECRET-KEY-123",
                                                "GOOGLE_DRIVE_SA_KEY_FILE": ""})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        _reset_caches()

    def _tree(self, month_children):
        tree = {
            dt.ROOT_FOLDER_ID: [_folder("Y24", "2024")],
            dt.YEAR_FOLDERS[2026]: [_folder("M10", "202610")],
            "M10": month_children,
        }
        stack = mock.patch.multiple(dt, list_children=mock.Mock(side_effect=lambda pid, refresh=False: tree.get(pid, [])),
                                    _check_folder=mock.DEFAULT)
        return stack

    def test_month_files_filters_names_and_month(self):
        kids = [_csv("a", "20261002-.csv"), _csv("b", "20261001-.csv"), _csv("c", "notes.csv"),
                _csv("d", "20261101-.csv"), _csv("e", "20261003.csv"), _folder("f", "20261004-.csv")]
        with self._tree(kids):
            files, found = dt.month_files(2026, 10)
        self.assertTrue(found)
        self.assertEqual([f["name"] for f in files], ["20261001-.csv", "20261002-.csv"])

    def test_missing_month_folder_is_not_an_error(self):
        with self._tree([]):
            self.assertEqual(dt.month_files(2026, 11), ([], False))
            self.assertEqual(dt.month_files(2024, 1), ([], False))

    def test_unshared_folder_is_an_error_not_empty(self):
        with mock.patch.object(dt, "_get", side_effect=dt.DriveNotFound("x")):
            with self.assertRaises(dt.DriveError) as cm:
                dt.month_files(2026, 10)
        self.assertIn("isn't shared", str(cm.exception))
        self.assertNotIsInstance(cm.exception, dt.DriveNotFound)

    def test_list_children_follows_pagination(self):
        pages = [mock.Mock(json=lambda: {"files": [_csv("1", "a")], "nextPageToken": "T"}),
                 mock.Mock(json=lambda: {"files": [_csv("2", "b")]})]
        with mock.patch.object(dt, "_get", side_effect=pages) as g:
            files = dt.list_children("P")
        self.assertEqual([f["id"] for f in files], ["1", "2"])
        self.assertEqual(g.call_args_list[1][0][1]["pageToken"], "T")

    def test_listing_cached_until_refresh(self):
        resp = mock.Mock(json=lambda: {"files": []})
        with mock.patch.object(dt, "_get", return_value=resp) as g:
            dt.list_children("P"); dt.list_children("P")
            self.assertEqual(g.call_count, 1)
            dt.list_children("P", refresh=True)          # within REFRESH_MIN_GAP
            self.assertEqual(g.call_count, 1)
            dt._listing_cache["P"] = (0, [])
            dt.list_children("P", refresh=True)
            self.assertEqual(g.call_count, 2)

    def test_rejects_bad_parent_id(self):
        with self.assertRaises(dt.DriveError):
            dt.list_children("x' or name contains 'y")

    def test_oversized_file_reported_not_downloaded(self):
        big = dict(_csv("z", "20261005-.csv"), size=str(dt.MAX_FILE_BYTES + 1))
        with mock.patch.object(dt, "_get") as g:
            parsed, _ = dt._load_file(big)
        g.assert_not_called()
        self.assertIn("too large", parsed["problems"][0]["error"])

    def test_file_cache_keyed_on_version(self):
        resp = mock.Mock(content=SAMPLE.encode())
        with mock.patch.object(dt, "_get", return_value=resp) as g:
            dt._load_file(_csv("a", "20261021-.csv"))
            _, fresh = dt._load_file(_csv("a", "20261021-.csv"))
            self.assertFalse(fresh)
            self.assertEqual(g.call_count, 1)
            _, fresh = dt._load_file(_csv("a", "20261021-.csv", mod="2026-10-23T00:00:00Z"))
            self.assertTrue(fresh)
            self.assertEqual(g.call_count, 2)

    def test_disk_cache_survives_memory_reset(self):
        resp = mock.Mock(content=SAMPLE.encode())
        with mock.patch.object(dt, "_get", return_value=resp) as g:
            dt._load_file(_csv("d1", "20261021-.csv"))
            dt._file_cache.clear()
            parsed, fresh = dt._load_file(_csv("d1", "20261021-.csv"))
        self.assertEqual(g.call_count, 1)
        self.assertFalse(fresh)
        self.assertEqual(len(parsed["rows"]), 3)
        self.assertEqual([n for n in os.listdir(_TMP_CACHE) if n.startswith("d1__")], [os.path.basename(dt._disk_path("d1", ("2026-10-22T00:00:00Z", "x", "1")))])

    def test_load_month(self):
        with self._tree([_csv("a", "20261021-.csv")]), \
             mock.patch.object(dt, "_get", return_value=mock.Mock(content=SAMPLE.encode())):
            res = dt.load_month(2026, 10)
        self.assertEqual(len(res["rows"]), 3)
        self.assertEqual(res["downloaded"], 1)
        self.assertTrue(res["folder_found"])

    def test_api_error_raises_and_hides_key(self):
        err = requests.ConnectionError("https://x/?key=SECRET-KEY-123")
        with mock.patch.object(dt._session(), "get", side_effect=err), mock.patch.object(dt.time, "sleep"), \
             self.assertLogs(dt.log, "WARNING") as logs:
            with self.assertRaises(dt.DriveError) as cm:
                dt.list_children("P")
        self.assertNotIn("SECRET", str(cm.exception))
        self.assertNotIn("SECRET", "\n".join(logs.output))

    def test_http_403_message(self):
        with mock.patch.object(dt._session(), "get",
                               return_value=mock.Mock(status_code=403, headers={"content-type": "application/json"})):
            with self.assertRaises(dt.DriveError) as cm:
                dt.list_children("P")
        self.assertIn("403", str(cm.exception))
        self.assertNotIn("SECRET", str(cm.exception))

    def test_google_sorry_page_is_a_throttle_message(self):
        sorry = mock.Mock(status_code=403, headers={"content-type": "text/html; charset=utf-8"})
        with mock.patch.object(dt._session(), "get", return_value=sorry):
            with self.assertRaises(dt.DriveError) as cm:
                dt.list_children("P")
        self.assertIn("temporarily limiting", str(cm.exception))

    def test_key_sent_in_header_not_url(self):
        ok = mock.Mock(status_code=200, json=lambda: {"files": []})
        with mock.patch.object(dt._session(), "get", return_value=ok) as g:
            dt.list_children("P")
        _, kw = g.call_args
        self.assertEqual(kw["headers"]["X-Goog-Api-Key"], "SECRET-KEY-123")
        self.assertNotIn("key", kw["params"])

    def test_non_json_listing_is_drive_error(self):
        bad = mock.Mock(status_code=200, json=mock.Mock(side_effect=ValueError))
        with mock.patch.object(dt._session(), "get", return_value=bad):
            with self.assertRaises(dt.DriveError):
                dt.list_children("P")

    def test_replaced_file_relists_once(self):
        calls = {"n": 0}
        def load(f):
            calls["n"] += 1
            if f["id"] == "old":
                raise dt.DriveNotFound("gone")
            return dt.parse_csv(SAMPLE, f["name"]), True
        listings = iter([[_csv("old", "20261021-.csv")], [_csv("new", "20261021-.csv")]])
        with mock.patch.object(dt, "month_files", side_effect=lambda y, m, refresh=False: (next(listings), True)), \
             mock.patch.object(dt, "_load_file", side_effect=load):
            res = dt.load_month(2026, 10)
        self.assertEqual(len(res["rows"]), 3)

    def test_file_cache_capped(self):
        resp = mock.Mock(content=SAMPLE.encode())
        with mock.patch.object(dt, "_get", return_value=resp), mock.patch.object(dt, "MAX_CACHED_FILES", 2):
            for i in range(3):
                dt._load_file(_csv(f"f{i}", "20261021-.csv"))
        self.assertEqual(list(dt._file_cache), ["f1", "f2"])

    def test_retry_on_429(self):
        ok = mock.Mock(status_code=200, json=lambda: {"files": []})
        with mock.patch.object(dt._session(), "get", side_effect=[mock.Mock(status_code=429), ok]) as g, \
             mock.patch.object(dt.time, "sleep"):
            dt.list_children("P")
        self.assertEqual(g.call_count, 2)

    def test_missing_key(self):
        with mock.patch.dict(os.environ, {"GOOGLE_DRIVE_API_KEY": ""}):
            with self.assertRaises(dt.DriveConfigError):
                dt.list_children("P")


class NameKeyTests(unittest.TestCase):
    def test_id_key_normalises_like_mysql(self):
        self.assertEqual(tr._id_key(" 210528-02a "), tr._id_key("210528-02A"))


EMPTY = {"rows": [], "problems": [], "files": [], "duplicates": [], "missing_months": [], "downloaded": 0}


class RangeTests(unittest.TestCase):
    """Cross-month loading and the duplicate-file-name guard."""

    def setUp(self):
        _reset_caches()

    def test_period_choices(self):
        ch = tr._period_choices(date(2026, 10, 22))
        self.assertEqual(len(ch), tr.PERIOD_CHOICES)
        self.assertEqual((ch[0]["start"], ch[0]["end"]), ("2026-10-08", "2026-10-22"))
        self.assertIn("in progress", ch[0]["label"])
        self.assertEqual((ch[1]["start"], ch[1]["end"]), ("2026-09-23", "2026-10-07"))

    def test_range_spans_month_folders_by_shift_date(self):
        sep = [_csv(f"s{d}", f"202609{d:02d}-.csv") for d in range(20, 31)]
        oct_ = [_csv(f"o{d}", f"202610{d:02d}-.csv") for d in range(1, 12)]
        with mock.patch.object(dt, "month_files", side_effect=lambda y, m, refresh=False: ({9: sep, 10: oct_}[m], True)) as mf, \
             mock.patch.object(dt, "_load_file", side_effect=lambda f: ({"rows": [], "problems": []}, False)):
            res = dt.load_range(date(2026, 9, 23), date(2026, 10, 7))
        self.assertEqual([c.args[:2] for c in mf.call_args_list], [(2026, 9), (2026, 10)])
        names = [f["name"] for f in res["files"]]
        self.assertEqual(names[0], "20260923-.csv")
        self.assertEqual(names[-1], "20261007-.csv")
        self.assertEqual(len(names), 15)

    def test_no_2025_folder_or_dates(self):
        self.assertNotIn(2025, dt.YEAR_FOLDERS)
        self.assertEqual(tr._parse_range({"from": "2025-12-23", "to": "2026-01-07"})[0], None)
        self.assertIsNotNone(tr._parse_range({"from": "2026-01-01", "to": "2026-01-07"})[0])
        ch = tr._period_choices(date(2026, 2, 1))
        self.assertEqual(ch[-1]["start"], "2026-01-08")   # Dec 23 - Jan 7 starts in 2025
        self.assertTrue(all(c["start"] >= "2026-01-01" for c in ch))

    def test_year_boundary_and_missing_folder(self):
        with mock.patch.object(dt, "month_files",
                               side_effect=lambda y, m, refresh=False: ([_csv("d", "20251231-.csv")], True) if y == 2025 else ([], False)), \
             mock.patch.object(dt, "_load_file", side_effect=lambda f: ({"rows": [], "problems": []}, False)):
            res = dt.load_range(date(2025, 12, 23), date(2026, 1, 7))
        self.assertEqual(res["missing_months"], ["202601"])
        self.assertEqual([f["name"] for f in res["files"]], ["20251231-.csv"])

    def test_duplicate_names_keep_newest(self):
        files = [_csv("old", "20261005-.csv", mod="2026-10-06T05:00:00Z"),
                 _csv("new", "20261005-.csv", mod="2026-10-07T09:00:00Z"),
                 _csv("x", "20261006-.csv")]
        kept, dups = dt.dedupe_by_name(files)
        self.assertEqual([f["id"] for f in kept], ["new", "x"])
        self.assertEqual(dups, [{"name": "20261005-.csv", "copies": 2, "kept_modified": "2026-10-07T09:00:00Z",
                                 "ignored_modified": ["2026-10-06T05:00:00Z"]}])

    def test_duplicates_never_double_rows(self):
        files = [_csv("old", "20261021-.csv", mod="2026-10-22T01:00:00Z"),
                 _csv("new", "20261021-.csv", mod="2026-10-22T02:00:00Z")]
        with mock.patch.object(dt, "month_files", return_value=(files, True)), \
             mock.patch.object(dt, "_get", return_value=mock.Mock(content=SAMPLE.encode())) as g:
            res = dt.load_range(date(2026, 10, 21), date(2026, 10, 21))
        self.assertEqual(len(res["rows"]), 3)
        self.assertEqual(g.call_count, 1)
        self.assertEqual(res["duplicates"][0]["copies"], 2)

    def test_endpoint_reports_duplicates_and_missing(self):
        c = appmod.app.test_client()
        with c.session_transaction() as s:
            s["user"] = {"email": "zztest@example.com"}; s["is_admin"] = True
        dup = [{"name": "20261005-.csv", "copies": 2, "kept_modified": "x", "ignored_modified": ["y"]}]
        with mock.patch.object(dt, "load_range", return_value=dict(EMPTY, duplicates=dup, missing_months=["202611"])):
            d = c.get("/admin/timestamp-records/data?from=2026-10-23&to=2026-11-07").get_json()
        self.assertEqual(d["duplicates"], dup)
        self.assertEqual(d["missing_months"], ["202611"])


class EndpointTests(unittest.TestCase):
    URL = "/admin/timestamp-records/data"

    def setUp(self):
        self.client = appmod.app.test_client()

    def _login(self, is_admin=False, perms=None):
        with self.client.session_transaction() as s:
            s["user"] = {"email": "zztest@example.com"}
            s["is_admin"] = is_admin
            s["permissions"] = perms or {}

    def test_requires_login(self):
        self.assertEqual(self.client.get(self.URL + "?from=2026-09-23&to=2026-10-07").status_code, 401)
        self.assertEqual(self.client.get("/admin/timestamp-records").status_code, 302)

    def test_requires_permission(self):
        self._login(perms={"can_memos": True})
        self.assertEqual(self.client.get(self.URL + "?from=2026-09-23&to=2026-10-07").status_code, 403)
        self.assertEqual(self.client.get("/admin/timestamp-records").status_code, 302)

    def test_only_dedicated_permission_grants_access(self):
        self.assertEqual(tr.VIEW_PERMISSIONS, ("can_timestamp_records",))
        self._login(perms={"can_absences": True, "can_tardiness": True, "can_reports": True})
        self.assertEqual(self.client.get(self.URL + "?from=2026-09-23&to=2026-10-07").status_code, 403)

    def test_permission_in_catalog(self):
        import permission
        self.assertIn("can_timestamp_records", permission.ALL_PERMISSIONS)

    def test_sub_admin_permissions_allowed(self):
        for p in tr.VIEW_PERMISSIONS:
            self._login(perms={p: True})
            with mock.patch.object(dt, "load_range", return_value=EMPTY):
                self.assertEqual(self.client.get(self.URL + "?from=2026-09-23&to=2026-10-07").status_code, 200, p)

    def test_validates_input(self):
        self._login(is_admin=True)
        for q in ("", "?from=2026-10-01", "?from=x&to=2026-10-02", "?from=2026-10-08&to=2026-10-07",
                  "?from=2024-12-01&to=2024-12-05", "?from=2026-01-01&to=2026-03-31",
                  "?from=2026-02-30&to=2026-03-01", "?year=2026&month=10"):
            self.assertEqual(self.client.get(self.URL + q).status_code, 400, q)

    def test_drive_failure_is_502_not_empty(self):
        self._login(is_admin=True)
        with mock.patch.object(dt, "load_range", side_effect=dt.DriveError("Could not reach Google Drive.")):
            r = self.client.get(self.URL + "?from=2026-09-23&to=2026-10-07")
        self.assertEqual(r.status_code, 502)
        self.assertFalse(r.get_json()["ok"])

    def test_rows_sorted_newest_first_with_names(self):
        self._login(is_admin=True)
        parsed = dt.parse_csv(SAMPLE, "20261021-.csv")
        loaded = dict(EMPTY, rows=parsed["rows"], files=[{"name": "20261021-.csv", "modified": None}], downloaded=1)
        with mock.patch.object(dt, "load_range", return_value=loaded) as lr, \
             mock.patch.object(tr, "_employee_names", return_value={"210528-02": ("Juan", "Dela Cruz")}):
            r = self.client.get(self.URL + "?from=2026-10-08&to=2026-10-22&refresh=1")
        lr.assert_called_once_with(date(2026, 10, 8), date(2026, 10, 22), refresh=True)
        d = r.get_json()
        self.assertTrue(d["ok"])
        self.assertEqual([(x["day"], x["time"]) for x in d["rows"]],
                         [("10/22/2026", "0:03"), ("10/21/2026", "14:19"), ("10/21/2026", "0:32")])
        self.assertLessEqual({"employee_id", "first_name", "last_name", "day", "time", "type"},
                             set(d["rows"][0].keys()))
        self.assertEqual(d["rows"][0]["first_name"], "Juan")
        self.assertEqual(d["rows"][2]["first_name"], "")
        self.assertEqual(d["unmatched_ids"], ["260812-05"])
        self.assertNotIn("SECRET", r.get_data(as_text=True))
        self.assertEqual(r.headers["Cache-Control"], "no-store")

    def test_page_renders(self):
        self._login(is_admin=True)
        with mock.patch.object(dt, "list_children", side_effect=AssertionError("page must not call Drive")):
            r = self.client.get("/admin/timestamp-records")
        self.assertEqual(r.status_code, 200)
        html = r.get_data(as_text=True)
        self.assertIn("Review employee clock-in and clock-out records for payroll verification.", html)
        self.assertIn("Payroll period", html)
        from helpers.payroll_period import get_default_payroll_period
        start, end = get_default_payroll_period()
        self.assertIn(f'value="{start.isoformat()}|{end.isoformat()}" selected', html)
        key = os.getenv("GOOGLE_DRIVE_API_KEY")
        self.assertFalse(key and key in html, "API key found in page")


class ServiceAccountTests(unittest.TestCase):
    def setUp(self):
        _reset_caches()
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        pem = rsa.generate_private_key(public_exponent=65537, key_size=2048).private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
        self.keyfile = os.path.join(_TMP_CACHE, "zztest-sa.json")
        with open(self.keyfile, "w") as fh:
            json.dump({"type": "service_account", "client_email": "zz@test.iam.gserviceaccount.com",
                       "private_key": pem, "private_key_id": "kid1",
                       "token_uri": "https://oauth2.googleapis.com/token"}, fh)
        self.env = mock.patch.dict(os.environ, {"GOOGLE_DRIVE_SA_KEY_FILE": self.keyfile,
                                                "GOOGLE_DRIVE_API_KEY": "SECRET-KEY-123"})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        _reset_caches()

    def test_bearer_token_used_and_cached(self):
        tok = mock.Mock(status_code=200, json=lambda: {"access_token": "AT-1", "expires_in": 3600})
        ok = mock.Mock(status_code=200, json=lambda: {"files": []})
        with mock.patch.object(dt._session(), "post", return_value=tok) as post, \
             mock.patch.object(dt._session(), "get", return_value=ok) as get:
            dt.list_children("P1"); dt.list_children("P2")
        self.assertEqual(post.call_count, 1)
        sent = post.call_args.kwargs["data"]
        self.assertEqual(sent["grant_type"], "urn:ietf:params:oauth:grant-type:jwt-bearer")
        self.assertEqual(get.call_args.kwargs["headers"], {"Authorization": "Bearer AT-1"})

    def test_jwt_claims_are_readonly_scope(self):
        tok = mock.Mock(status_code=200, json=lambda: {"access_token": "AT", "expires_in": 3600})
        with mock.patch.object(dt._session(), "post", return_value=tok) as post:
            dt._sa_token()
        import base64
        payload = post.call_args.kwargs["data"]["assertion"].split(".")[1]
        payload = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        self.assertEqual(payload["scope"], "https://www.googleapis.com/auth/drive.readonly")
        self.assertEqual(payload["iss"], "zz@test.iam.gserviceaccount.com")

    def test_401_refreshes_token_once(self):
        toks = iter([{"access_token": "OLD"}, {"access_token": "NEW"}])
        tok = mock.Mock(status_code=200)
        tok.json = lambda: next(toks)
        ok = mock.Mock(status_code=200, json=lambda: {"files": []})
        with mock.patch.object(dt._session(), "post", return_value=tok), \
             mock.patch.object(dt._session(), "get", side_effect=[mock.Mock(status_code=401), ok]) as get:
            dt.list_children("P")
        self.assertEqual(get.call_args.kwargs["headers"], {"Authorization": "Bearer NEW"})

    def test_bad_key_file(self):
        with mock.patch.dict(os.environ, {"GOOGLE_DRIVE_SA_KEY_FILE": "/nonexistent/zz.json"}):
            with self.assertRaises(dt.DriveConfigError):
                dt.list_children("P")

    def test_rejected_signin_message_has_no_secret(self):
        with mock.patch.object(dt._session(), "post", return_value=mock.Mock(status_code=400)):
            with self.assertRaises(dt.DriveError) as cm:
                dt._sa_token()
        self.assertIn("HTTP 400", str(cm.exception))
        self.assertNotIn("PRIVATE", str(cm.exception))


@unittest.skipUnless(os.getenv("GOOGLE_DRIVE_API_KEY") or os.getenv("GOOGLE_DRIVE_SA_KEY_FILE"),
                     "no Drive credentials configured")
class LiveDriveTests(unittest.TestCase):
    """Read-only calls against the real Drive folder."""

    def test_october_2026(self):
        # Uses the real disk cache so repeat runs don't re-download (Google
        # throttles bulk downloads of link-shared files).
        dt.DISK_CACHE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                         "instance", "drive_timestamps")
        self.addCleanup(setattr, dt, "DISK_CACHE_DIR", _TMP_CACHE)
        _reset_caches()
        self.assertIn("2026", dt.available_years())
        res = dt.load_month(2026, 10, refresh=True)
        self.assertTrue(res["folder_found"])
        self.assertGreaterEqual(len(res["files"]), 21)
        self.assertIn("20261021-.csv", [f["name"] for f in res["files"]])
        self.assertEqual(res["problems"], [])
        self.assertGreater(len(res["rows"]), 1000)
        again = dt.load_month(2026, 10)
        self.assertEqual(again["downloaded"], 0)
