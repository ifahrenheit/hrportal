"""Mailing Lists blueprint: parsing, classification and access. Fully mocked —
no cPanel, Mailman or database calls."""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flask import Flask

from modules import mailing_lists as ml


MAILMAN_PAGE = """
<a href="/mailman/admin/gygccall_cohere.ph/members?letter=a">A</a>
<a href="/mailman/admin/gygccall_cohere.ph/members?letter=b">B</a>
<a href="/mailman/admin/gygccall_cohere.ph/members?letter=b&chunk=1">2</a>
<em>3 members total</em>
<input name="ann.cruz%40cohere.ph_realname" type="TEXT">
<input name="Ben.Lim%40Cohere.ph_realname" type="TEXT">
"""


class ParsingTests(unittest.TestCase):
    def test_group_forwarders(self):
        rows = [
            {"dest": "HR@cohere.ph", "forward": "a@cohere.ph"},
            {"dest": "hr@cohere.ph", "forward": "B@Cohere.ph"},
            {"dest": "hr@cohere.ph", "forward": "a@cohere.ph"},
            {"dest": "it@cohere.ph", "forward": "c@cohere.ph"},
            {"dest": "", "forward": "x@cohere.ph"},
        ]
        self.assertEqual(ml.group_forwarders(rows), {
            "hr@cohere.ph": ["a@cohere.ph", "b@cohere.ph"],
            "it@cohere.ph": ["c@cohere.ph"],
        })

    def test_mailman_members_total_and_pages(self):
        self.assertEqual(ml.parse_mailman_members(MAILMAN_PAGE), {"ann.cruz@cohere.ph", "ben.lim@cohere.ph"})
        self.assertEqual(ml.parse_mailman_total(MAILMAN_PAGE), 3)
        self.assertEqual(ml.parse_mailman_pages(MAILMAN_PAGE), (["a", "b"], [1]))
        self.assertIsNone(ml.parse_mailman_total("<p>nothing</p>"))

    def test_classify_and_summarize(self):
        employees = {
            "ann@cohere.ph": {"schedule_name": "Ann", "status": "Active", "group_name": "G1", "employee_id": "1"},
            "ben@cohere.ph": {"schedule_name": "Ben", "status": "Separated", "group_name": "G2", "employee_id": "2"},
            "cy@cohere.ph": {"schedule_name": "Cy", "status": "Training", "group_name": "G3", "employee_id": "3"},
        }
        out = ml.classify_members(["ben@cohere.ph", "ann@cohere.ph", "cy@cohere.ph", "vendor@x.com"], employees)
        self.assertEqual([m["kind"] for m in out], ["active", "separated", "active", "external"])
        self.assertEqual(ml.summarize(out), {"total": 4, "active": 2, "separated": 1, "external": 1})


class MailmanReadTests(unittest.TestCase):
    def test_flags_incomplete_read(self):
        sess = mock.MagicMock()
        sess.__enter__.return_value = sess
        sess.post.return_value = mock.Mock(text=MAILMAN_PAGE, raise_for_status=lambda: None)
        sess.get.return_value = mock.Mock(text="", raise_for_status=lambda: None)
        with mock.patch.object(ml, "_uapi", return_value="otp"), \
             mock.patch.object(ml.requests, "Session", return_value=sess):
            res = ml._read_mailman_members("gygccall@cohere.ph", "gygccall_cohere.ph")
        self.assertEqual(len(res["members"]), 2)
        self.assertEqual(res["total"], 3)
        self.assertFalse(res["complete"])
        # letter pages a and b, plus the chunk=1 page linked from the first page
        self.assertEqual(sess.get.call_count, 3)

    def test_rejects_refused_login_and_bad_listid(self):
        sess = mock.MagicMock()
        sess.__enter__.return_value = sess
        sess.post.return_value = mock.Mock(text='<input name="adminpw">', raise_for_status=lambda: None)
        with mock.patch.object(ml, "_uapi", return_value="otp"), \
             mock.patch.object(ml.requests, "Session", return_value=sess):
            with self.assertRaises(RuntimeError):
                ml._read_mailman_members("x@cohere.ph", "x_cohere.ph")
        with self.assertRaises(RuntimeError):
            ml._read_mailman_members("x@cohere.ph", "../etc")


class AccessTests(unittest.TestCase):
    def setUp(self):
        app = Flask(__name__, template_folder=os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "templates"))
        app.secret_key = "test"
        app.register_blueprint(ml.mailing_lists_bp)
        self.client = app.test_client()

    def _login(self, **sess):
        with self.client.session_transaction() as s:
            s["user"] = {"email": "x@cohere.ph"}
            s.update(sess)

    def test_anonymous_redirected(self):
        self.assertEqual(self.client.get("/admin/mailing-lists/").status_code, 302)

    def test_plain_user_gets_404(self):
        self._login(is_admin=False, permissions={"can_documents": True})
        self.assertEqual(self.client.get("/admin/mailing-lists/").status_code, 404)
        self.assertEqual(self.client.get("/admin/mailing-lists/api/summary/forwarder/hr@cohere.ph").status_code, 404)

    def test_permission_holder_allowed_and_unknown_address_404(self):
        self._login(is_admin=False, permissions={"can_mailing_lists": True})
        with mock.patch.object(ml, "fetch_forwarders", return_value=({"hr@cohere.ph": ["a@cohere.ph"]}, 0)), \
             mock.patch.object(ml, "load_employee_index", return_value={}):
            r = self.client.get("/admin/mailing-lists/api/summary/forwarder/hr@cohere.ph")
            self.assertEqual(r.get_json()["summary"]["total"], 1)
            self.assertEqual(self.client.get("/admin/mailing-lists/api/summary/forwarder/nope@cohere.ph").status_code, 404)
        self.assertEqual(self.client.get("/admin/mailing-lists/api/summary/other/hr@cohere.ph").status_code, 404)


if __name__ == "__main__":
    unittest.main()
