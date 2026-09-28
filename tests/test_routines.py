"""
Tests for the Routine Tracker blueprint (modules/routines.py).

Run from the repo root:
    ./venv/bin/python -m unittest tests.test_routines -v   (after `set -a; . ./.env`)

* Pure tests cover the due-window logic.
* Route tests run against the real central_db (the tables are new and the app
  has no test DB). Every row they create carries the TEST_PREFIX title and is
  removed in tearDownClass, FK-safe (logs first).
"""
import os
import sys
import unittest
from datetime import date, datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as appmod  # noqa: E402
from db_core import get_db_connection  # noqa: E402
from modules import routines as R  # noqa: E402

app = appmod.app
app.config["TESTING"] = True
TEST_PREFIX = "ZZTEST-routines-"


class WindowLogicTests(unittest.TestCase):
    def test_window_starts(self):
        wed = date(2026, 9, 23)  # Wednesday
        self.assertEqual(R.window_start("daily", wed), wed)
        self.assertEqual(R.window_start("weekly", wed), date(2026, 9, 21))  # Monday
        self.assertEqual(R.window_start("monthly", wed), date(2026, 9, 1))

    def test_weekly_sunday_belongs_to_week_that_started_previous_monday(self):
        self.assertEqual(R.window_start("weekly", date(2026, 9, 27)), date(2026, 9, 21))

    def test_biweekly_blocks_are_14_days_from_anchor_monday(self):
        self.assertEqual(R.BIWEEKLY_ANCHOR.weekday(), 0)
        self.assertEqual(R.window_start("biweekly", R.BIWEEKLY_ANCHOR), R.BIWEEKLY_ANCHOR)
        d = R.BIWEEKLY_ANCHOR + timedelta(days=13)
        self.assertEqual(R.window_start("biweekly", d), R.BIWEEKLY_ANCHOR)
        d = R.BIWEEKLY_ANCHOR + timedelta(days=14)
        self.assertEqual(R.window_start("biweekly", d), d)
        self.assertEqual(R.window_start("biweekly", date(2026, 1, 1)).weekday(), 0)

    def test_unknown_frequency_raises(self):
        with self.assertRaises(ValueError):
            R.window_start("hourly", date.today())

    def test_is_due(self):
        today = date(2026, 9, 23)
        self.assertTrue(R.is_due("daily", None, today))
        self.assertFalse(R.is_due("daily", datetime(2026, 9, 23, 0, 0), today))
        self.assertTrue(R.is_due("daily", datetime(2026, 9, 22, 23, 59), today))
        self.assertFalse(R.is_due("weekly", datetime(2026, 9, 21, 8, 0), today))
        self.assertTrue(R.is_due("weekly", datetime(2026, 9, 20, 23, 0), today))
        self.assertFalse(R.is_due("monthly", datetime(2026, 9, 1, 0, 0), today))
        self.assertTrue(R.is_due("monthly", datetime(2026, 8, 31, 23, 0), today))


class RouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._cleanup()
        conn = get_db_connection()
        with conn.cursor() as cur:
            ids = []
            for title, freq, active in [("daily", "daily", 1), ("weekly", "weekly", 1),
                                        ("inactive", "monthly", 0)]:
                cur.execute(
                    "INSERT INTO routine_tasks (title, category, frequency, active, created_by)"
                    " VALUES (%s, 'TEST', %s, %s, 'test@example.com')",
                    (TEST_PREFIX + title, freq, active),
                )
                ids.append(cur.lastrowid)
        conn.commit()
        conn.close()
        cls.daily_id, cls.weekly_id, cls.inactive_id = ids

    @classmethod
    def tearDownClass(cls):
        cls._cleanup()

    @staticmethod
    def _cleanup():
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute("DELETE l FROM routine_logs l JOIN routine_tasks t ON t.id = l.task_id"
                        " WHERE t.title LIKE %s", (TEST_PREFIX + "%",))
            cur.execute("DELETE FROM routine_tasks WHERE title LIKE %s", (TEST_PREFIX + "%",))
        conn.commit()
        conn.close()

    def client(self, admin=False, it=False, anon=False):
        c = app.test_client()
        if anon:
            return c
        with c.session_transaction() as s:
            s["user"] = {"email": "tester@example.com", "name": "Tester", "employee_id": "T1"}
            s["is_admin"] = admin
            s["permissions"] = {"can_it_tickets": it}
            s["_csrf_token"] = "tok"
        return c

    # ── auth ────────────────────────────────────────────────────────────────
    def test_anonymous_redirected_to_login(self):
        for path in ("/routines/", "/routines/log", "/routines/logs",
                     "/routines/logs.csv", "/routines/dashboard"):
            r = self.client(anon=True).get(path)
            self.assertEqual(r.status_code, 302, path)
            self.assertIn("/login", r.headers["Location"], path)

    def test_user_without_role_denied(self):
        for path in ("/routines/", "/routines/log", "/routines/logs",
                     "/routines/logs.csv", "/routines/dashboard"):
            r = self.client().get(path)
            self.assertEqual(r.status_code, 302, path)
            self.assertNotIn("/routines", r.headers["Location"], path)

    def test_it_user_can_view_but_not_manage_tasks(self):
        c = self.client(it=True)
        self.assertEqual(c.get("/routines/").status_code, 200)
        r = c.post("/routines/tasks/save", data={"csrf_token": "tok", "title": TEST_PREFIX + "x",
                                                  "frequency": "daily"})
        self.assertEqual(r.status_code, 302)
        r = c.post(f"/routines/tasks/{self.daily_id}/toggle", data={"csrf_token": "tok"})
        self.assertEqual(r.status_code, 302)
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) n FROM routine_tasks WHERE title=%s", (TEST_PREFIX + "x",))
            self.assertEqual(cur.fetchone()["n"], 0)
        conn.close()

    def test_pages_render_for_it_and_admin(self):
        for kw in ({"it": True}, {"admin": True}):
            c = self.client(**kw)
            for path in ("/routines/", "/routines/log", "/routines/logs",
                         "/routines/dashboard", "/routines/dashboard?days=7"):
                self.assertEqual(c.get(path).status_code, 200, (kw, path))

    def test_pages_render_with_no_logs_in_range(self):
        # Regression: PyMySQL returns () for empty results; the dashboard once called .sort() on it.
        c = self.client(admin=True)
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) n FROM routine_logs")
            has_logs = cur.fetchone()["n"] > 0
        conn.close()
        if has_logs:
            self.skipTest("table not empty; empty-state covered by the mocked variant below")
        for path in ("/routines/dashboard", "/routines/logs", "/routines/logs.csv"):
            self.assertEqual(c.get(path).status_code, 200, path)

    def test_dashboard_empty_result_sets_are_tuples(self):
        from unittest.mock import patch, MagicMock

        class FakeCur:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def execute(self, *a, **k): pass
            def fetchall(self): return ()

        fake = MagicMock()
        fake.cursor.return_value = FakeCur()
        with patch.object(R, "get_db_connection", return_value=fake):
            r = self.client(admin=True).get("/routines/dashboard")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"No checks logged in this range", r.data)

    def test_csrf_required_on_posts(self):
        c = self.client(admin=True)
        r = c.post("/routines/tasks/save", data={"title": TEST_PREFIX + "nocsrf", "frequency": "daily"})
        self.assertEqual(r.status_code, 403)
        r = c.post("/routines/log", data={"task_id": self.daily_id, "result": "pass"})
        self.assertEqual(r.status_code, 403)

    # ── task CRUD ───────────────────────────────────────────────────────────
    def test_admin_add_edit_toggle(self):
        c = self.client(admin=True)
        title = TEST_PREFIX + "crud"
        c.post("/routines/tasks/save", data={"csrf_token": "tok", "title": title,
                                              "frequency": "biweekly", "category": "TEST"})
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM routine_tasks WHERE title=%s", (title,))
            row = cur.fetchone()
        self.assertEqual((row["frequency"], row["active"], row["created_by"]),
                         ("biweekly", 1, "tester@example.com"))
        c.post("/routines/tasks/save", data={"csrf_token": "tok", "id": row["id"], "title": title,
                                              "frequency": "monthly", "category": "TEST"})
        c.post(f"/routines/tasks/{row['id']}/toggle", data={"csrf_token": "tok"})
        with conn.cursor() as cur:
            conn.commit()
            cur.execute("SELECT frequency, active FROM routine_tasks WHERE id=%s", (row["id"],))
            row2 = cur.fetchone()
        conn.close()
        self.assertEqual((row2["frequency"], row2["active"]), ("monthly", 0))

    def test_save_rejects_bad_frequency_and_blank_title(self):
        c = self.client(admin=True)
        c.post("/routines/tasks/save", data={"csrf_token": "tok", "title": TEST_PREFIX + "bad",
                                              "frequency": "hourly"})
        c.post("/routines/tasks/save", data={"csrf_token": "tok", "title": "  ", "frequency": "daily"})
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) n FROM routine_tasks WHERE title=%s", (TEST_PREFIX + "bad",))
            self.assertEqual(cur.fetchone()["n"], 0)
        conn.close()

    def test_toggle_unknown_task_404(self):
        r = self.client(admin=True).post("/routines/tasks/999999999/toggle", data={"csrf_token": "tok"})
        self.assertEqual(r.status_code, 404)

    # ── logging, history, csv, dashboard ────────────────────────────────────
    def _log(self, task_id, result, comment="ok"):
        return self.client(it=True).post("/routines/log", data={
            "csrf_token": "tok", "task_id": task_id, "result": result, "comment": comment})

    def test_log_rejects_inactive_task_and_bad_result(self):
        def n():
            conn = get_db_connection()
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) n FROM routine_logs WHERE task_id IN (%s,%s)",
                            (self.inactive_id, self.daily_id))
                v = cur.fetchone()["n"]
            conn.close()
            return v
        before = n()
        self._log(self.inactive_id, "pass")
        self._log(self.daily_id, "maybe")
        self.assertEqual(n(), before)

    def test_log_history_csv_and_due_flow(self):
        due_before = {d["id"] for d in self._due()}
        self.assertIn(self.daily_id, due_before)
        self.assertIn(self.weekly_id, due_before)
        self.assertNotIn(self.inactive_id, due_before)

        self.assertEqual(self._log(self.daily_id, "fail", "=cmd|' /C calc'!A0").status_code, 302)
        self._log(self.weekly_id, "pass", "fine, with comma\nand newline")

        due_after = {d["id"] for d in self._due()}
        self.assertNotIn(self.daily_id, due_after)   # a failed check still counts as performed
        self.assertNotIn(self.weekly_id, due_after)

        c = self.client(it=True)
        r = c.get(f"/routines/logs?task_id={self.daily_id}&result=fail")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"cmd|", r.data)              # the daily/fail log row
        self.assertNotIn(b"fine, with comma", r.data)  # the weekly/pass row is filtered out

        r = c.get(f"/routines/logs.csv?task_id={self.daily_id}")
        self.assertEqual(r.mimetype, "text/csv")
        text = r.data.decode("utf-8-sig")
        header, first = text.splitlines()[0], text.splitlines()[1]
        self.assertEqual(header, ",".join(R.CSV_COLUMNS))
        self.assertIn(",fail,", first)
        self.assertIn("'=cmd", first)  # formula neutralised
        # typed datetime column: YYYY-MM-DD HH:MM:SS
        datetime.strptime(first.split(",")[6], "%Y-%m-%d %H:%M:%S")

        r = c.get(f"/routines/logs.csv?task_id={self.weekly_id}")
        self.assertEqual(len(list(__import__("csv").reader(r.data.decode("utf-8-sig").splitlines(True)))), 2)

        # frequency + date-range filters
        r = c.get(f"/routines/logs.csv?frequency=weekly&date_from={date.today().isoformat()}"
                  f"&date_to={date.today().isoformat()}")
        self.assertIn("ZZTEST-routines-weekly", r.data.decode("utf-8-sig"))
        r = c.get("/routines/logs.csv?date_to=2000-01-01")
        self.assertEqual(len(r.data.decode("utf-8-sig").splitlines()), 1)  # header only

        d = c.get("/routines/dashboard")
        self.assertEqual(d.status_code, 200)
        self.assertIn(b"ZZTEST-routines-daily", d.data)  # in fail-rate table

    def test_csv_bearer_token(self):
        anon = self.client(anon=True)
        old = os.environ.pop("ROUTINES_EXPORT_TOKEN", None)
        try:
            self.assertEqual(anon.get("/routines/logs.csv",
                                      headers={"Authorization": "Bearer s3cret"}).status_code, 302)
            os.environ["ROUTINES_EXPORT_TOKEN"] = "s3cret"
            self.assertEqual(anon.get("/routines/logs.csv",
                                      headers={"Authorization": "Bearer s3cret"}).status_code, 200)
            self.assertEqual(anon.get("/routines/logs.csv",
                                      headers={"Authorization": "Bearer wrong"}).status_code, 302)
            # the token only opens the CSV, not the HTML pages
            self.assertEqual(anon.get("/routines/logs",
                                      headers={"Authorization": "Bearer s3cret"}).status_code, 302)
        finally:
            os.environ.pop("ROUTINES_EXPORT_TOKEN", None)
            if old is not None:
                os.environ["ROUTINES_EXPORT_TOKEN"] = old

    @staticmethod
    def _due():
        conn = get_db_connection()
        try:
            with conn.cursor() as cur:
                return [d for d in R._due_tasks(cur, date.today())
                        if d["title"].startswith(TEST_PREFIX)]
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
