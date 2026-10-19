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
        self.assertEqual(ml.summarize(out), {"total": 4, "active": 2, "separated": 1, "external": 1, "delivery_off": 0})


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


class ChangeLogicTests(unittest.TestCase):
    def test_parse_email_input(self):
        valid, invalid = ml.parse_email_input("A@cohere.ph, b@cohere.ph;\n<c@x.com>  a@cohere.ph nope bad@ ")
        self.assertEqual(valid, ["a@cohere.ph", "b@cohere.ph", "c@x.com"])
        self.assertEqual(invalid, ["nope", "bad@"])

    def test_plan_and_verify(self):
        todo, skipped = ml.plan_change("add", ["a@x.ph", "b@x.ph"], {"B@x.ph"})
        self.assertEqual((todo, skipped), (["a@x.ph"], ["b@x.ph"]))
        todo, skipped = ml.plan_change("remove", ["a@x.ph", "b@x.ph"], {"b@x.ph"})
        self.assertEqual((todo, skipped), (["b@x.ph"], ["a@x.ph"]))
        v = ml.verify_change("remove", ["b@x.ph", "c@x.ph"], {"c@x.ph"}, {"c@x.ph": "boom"})
        self.assertEqual(v["b@x.ph"], ("done", None))
        self.assertEqual(v["c@x.ph"], ("failed", "boom"))
        v = ml.verify_change("add", ["d@x.ph"], set())
        self.assertEqual(v["d@x.ph"][0], "failed")

    def test_forwarder_change_members_verifies_and_audits(self):
        reads = iter([(["old@x.ph"], {}), (["old@x.ph", "new@x.ph"], {})])
        with mock.patch.object(ml, "_current_emails", side_effect=lambda *a, **k: next(reads)), \
             mock.patch.object(ml, "forwarder_change") as fc, \
             mock.patch.object(ml, "_write_audit") as audit:
            res = ml.change_members("forwarder", "hr@x.ph", "add", ["new@x.ph", "old@x.ph", "ghost@x.ph"])
        # ghost@ was sent but never showed up on read-back -> failed
        self.assertEqual([c.args for c in fc.call_args_list],
                         [("hr@x.ph", "add", "new@x.ph"), ("hr@x.ph", "add", "ghost@x.ph")])
        self.assertEqual([(e, r) for e, r, _ in res],
                         [("new@x.ph", "done"), ("old@x.ph", "skipped"), ("ghost@x.ph", "failed")])
        audit.assert_called_once()

    def test_forwarder_uapi_calls(self):
        with mock.patch.object(ml, "_uapi") as u:
            ml.forwarder_change("hr@cohere.ph", "add", "a@cohere.ph")
            ml.forwarder_change("hr@cohere.ph", "remove", "a@cohere.ph")
        self.assertEqual(u.call_args_list[0], mock.call("add_forwarder", {
            "domain": "cohere.ph", "email": "hr@cohere.ph", "fwdopt": "fwd", "fwdemail": "a@cohere.ph"}, write=True))
        self.assertEqual(u.call_args_list[1], mock.call("delete_forwarder", {
            "address": "hr@cohere.ph", "forwarder": "a@cohere.ph"}, write=True))

    def test_mailman_remove_posts_form_with_csrf_and_no_notices(self):
        sess = mock.MagicMock()
        sess.__enter__.return_value = sess
        login = mock.Mock(text="<p>ok</p>", raise_for_status=lambda: None)
        done = mock.Mock(text="<p>done</p>", raise_for_status=lambda: None)
        sess.post.side_effect = [login, done]
        form = mock.Mock(text='<input type="hidden" name="csrf_token" value="tok123">', raise_for_status=lambda: None)
        after = mock.Mock(text='<em>1 members total</em><input name="b%40x.ph_realname">', raise_for_status=lambda: None)
        sess.get.side_effect = [form, after]
        with mock.patch.object(ml, "_uapi", return_value="otp"), \
             mock.patch.object(ml.requests, "Session", return_value=sess):
            res = ml.mailman_change("l@x.ph", "l_x.ph", "remove", ["a@x.ph"])
        url, = sess.post.call_args_list[1].args
        data = sess.post.call_args_list[1].kwargs["data"]
        self.assertTrue(url.endswith("/admin/l_x.ph/members/remove"))
        self.assertEqual(data["csrf_token"], "tok123")
        self.assertEqual(data["unsubscribees"], "a@x.ph")
        self.assertEqual(data["send_unsub_ack_to_this_batch"], "0")
        self.assertEqual(data["send_unsub_notifications_to_list_owner"], "0")
        self.assertEqual(res["members"], {"b@x.ph"})


class ChangeRouteTests(unittest.TestCase):
    def setUp(self):
        app = Flask(__name__, template_folder=os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "templates"))
        app.secret_key = "test"
        app.register_blueprint(ml.mailing_lists_bp)
        self.client = app.test_client()
        with self.client.session_transaction() as s:
            s["user"] = {"email": "x@cohere.ph"}
            s["permissions"] = {"can_mailing_lists": True}
            s["_csrf_token"] = "good"

    URL = "/admin/mailing-lists/forwarder/hr@cohere.ph/members"

    def test_bad_csrf_changes_nothing(self):
        with mock.patch.object(ml, "change_members") as cm:
            r = self.client.post(self.URL, data={"csrf_token": "bad", "action": "add", "emails": "a@cohere.ph"})
        self.assertEqual(r.status_code, 302)
        cm.assert_not_called()

    def test_add_routes_to_change_members_and_drops_self(self):
        with mock.patch.object(ml, "change_members", return_value=[("a@cohere.ph", "done", None)]) as cm:
            r = self.client.post(self.URL, data={"csrf_token": "good", "action": "add",
                                                 "emails": "a@cohere.ph, hr@cohere.ph"})
        self.assertEqual(r.status_code, 302)
        cm.assert_called_once_with("forwarder", "hr@cohere.ph", "add", ["a@cohere.ph"], notify=False)

    def test_remove_uses_checked_members(self):
        with mock.patch.object(ml, "change_members", return_value=[]) as cm:
            self.client.post(self.URL, data={"csrf_token": "good", "action": "remove",
                                             "member": ["a@cohere.ph", "b@cohere.ph"]})
        self.assertEqual(cm.call_args.args[3], ["a@cohere.ph", "b@cohere.ph"])

    def test_no_permission_404(self):
        with self.client.session_transaction() as s:
            s["permissions"] = {}
        with mock.patch.object(ml, "change_members") as cm:
            r = self.client.post(self.URL, data={"csrf_token": "good", "action": "add", "emails": "a@cohere.ph"})
        self.assertEqual(r.status_code, 404)
        cm.assert_not_called()



class CreateTests(unittest.TestCase):
    def test_validate_local_part(self):
        for ok in ("team", "team-leads", "qa_2", "a.b", "x"):
            self.assertIsNone(ml.validate_local_part(ok), ok)
        for bad in ("", "-team", "team-", "a..b", "Team Leads", "a@b", "x" * 65, ".x"):
            self.assertIsNotNone(ml.validate_local_part(bad), bad)

    def test_find_conflict(self):
        self.assertEqual(ml.find_conflict("hr@x.ph", {}, {}, {"hr@x.ph"}), "an email account")
        self.assertEqual(ml.find_conflict("hr@x.ph", {"hr@x.ph": []}, {}, set()), "a forwarder group")
        self.assertEqual(ml.find_conflict("hr@x.ph", {}, {"hr@x.ph": {}}, set()), "a Mailman list")
        self.assertIsNone(ml.find_conflict("new@x.ph", {"hr@x.ph": []}, {}, set()))

    def _patch_reads(self, forwarders=None, mailman=None, mailboxes=None):
        return [mock.patch.object(ml, "fetch_forwarders", side_effect=forwarders or (lambda **k: ({}, 0))),
                mock.patch.object(ml, "fetch_mailman_lists", side_effect=mailman or (lambda **k: ({}, 0))),
                mock.patch.object(ml, "fetch_mailboxes", return_value=(mailboxes or set(), 0))]

    def test_create_refuses_existing_address(self):
        p = self._patch_reads(mailboxes={"hr@x.ph"})
        with p[0], p[1], p[2], mock.patch.object(ml, "_uapi") as u:
            created, msg, _ = ml.create_group("forwarder", "hr", "x.ph", ["a@x.ph"])
        self.assertFalse(created)
        self.assertIn("email account", msg)
        u.assert_not_called()

    def test_create_mailman_calls_add_list_then_adds_members(self):
        reads = iter([({}, 0), ({"new@x.ph": {"listid": "new_x.ph"}}, 0)])
        p = self._patch_reads(mailman=lambda **k: next(reads))
        with p[0], p[1], p[2], mock.patch.object(ml, "_uapi") as u, \
             mock.patch.object(ml, "_write_audit") as audit, \
             mock.patch.object(ml, "change_members", return_value=[("a@x.ph", "done", None)]) as cm:
            created, _, results = ml.create_group("mailman", "new", "x.ph", ["a@x.ph"],
                                                  password="pw", private=True)
        self.assertTrue(created)
        u.assert_called_once_with("add_list", {"list": "new", "domain": "x.ph", "password": "pw",
                                               "private": "1"}, write=True)
        cm.assert_called_once_with("mailman", "new@x.ph", "add", ["a@x.ph"], notify=False)
        self.assertEqual(audit.call_args.args[2], "create")
        # the password never goes to the audit log
        self.assertNotIn("pw", repr(audit.call_args_list))

    def test_create_forwarder_adds_each_target_and_verifies(self):
        reads = iter([({}, 0), ({"new@x.ph": ["a@x.ph"]}, 0)])
        p = self._patch_reads(forwarders=lambda **k: next(reads))
        with p[0], p[1], p[2], mock.patch.object(ml, "forwarder_change") as fc, \
             mock.patch.object(ml, "_write_audit") as audit:
            created, _, results = ml.create_group("forwarder", "new", "x.ph", ["a@x.ph", "b@x.ph"])
        self.assertTrue(created)
        self.assertEqual(fc.call_count, 2)
        self.assertEqual([(e, r) for e, r, _ in results], [("a@x.ph", "done"), ("b@x.ph", "failed")])
        self.assertEqual([c.args[2] for c in audit.call_args_list], ["create", "add"])


class NewRouteTests(unittest.TestCase):
    def setUp(self):
        app = Flask(__name__, template_folder=os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "templates"))
        app.secret_key = "test"
        app.register_blueprint(ml.mailing_lists_bp)
        app.jinja_env.globals["csrf_token"] = lambda: "good"
        self.client = app.test_client()
        with self.client.session_transaction() as s:
            s["user"] = {"email": "x@cohere.ph"}
            s["permissions"] = {"can_mailing_lists": True}
            s["_csrf_token"] = "good"
        # base.html needs the whole portal; render the context instead
        self.patches = [mock.patch.object(ml, "fetch_mail_domains", return_value=(["cohere.ph"], 0)),
                        mock.patch.object(ml, "required_password_strength", return_value=65),
                        mock.patch.object(ml, "render_template", side_effect=lambda t, **kw: repr(kw))]
        for p in self.patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in self.patches])

    def post(self, **data):
        base = {"csrf_token": "good", "list_type": "mailman", "local": "newlist", "domain": "cohere.ph",
                "password": "x", "password2": "x", "members": "", "access": "public"}
        base.update(data)
        return self.client.post("/admin/mailing-lists/new", data=base)

    def test_short_or_mismatched_password_is_refused_before_creating(self):
        with mock.patch.object(ml, "create_group") as cg:
            short = self.post(password="abc", password2="abc").get_data(as_text=True)
            mismatch = self.post(password="Abcdefgh1234!", password2="Abcdefgh1234?").get_data(as_text=True)
        self.assertIn("too short", short)
        self.assertIn("do not match", mismatch)
        cg.assert_not_called()

    def test_forwarder_needs_a_target_and_valid_domain(self):
        with mock.patch.object(ml, "create_group") as cg:
            r = self.post(list_type="forwarder", domain="evil.com")
        body = r.get_data(as_text=True)
        self.assertIn("at least one address", body)
        self.assertIn("Choose one of the domains", body)
        cg.assert_not_called()

    def test_good_request_creates_and_redirects(self):
        with mock.patch.object(ml, "create_group", return_value=(True, "ok", [])) as cg:
            r = self.post(password="Str0ng!pass", password2="Str0ng!pass", access="private")
        self.assertEqual(r.status_code, 302)
        self.assertTrue(r.headers["Location"].endswith("/admin/mailing-lists/mailman/newlist@cohere.ph"))
        cg.assert_called_once_with("mailman", "newlist", "cohere.ph", [], password="Str0ng!pass",
                                   private=True, notify=False)
        # the password is never echoed back into the page
        self.assertNotIn("Str0ng", r.get_data(as_text=True))



class SyncTests(unittest.TestCase):
    def test_plan_sync(self):
        to_add, to_remove, unchanged = ml.plan_sync(["b@x.ph", "c@x.ph", "d@x.ph"], {"A@x.ph", "b@x.ph", "c@x.ph"})
        self.assertEqual(to_add, ["d@x.ph"])
        self.assertEqual(to_remove, ["a@x.ph"])
        self.assertEqual(unchanged, ["b@x.ph", "c@x.ph"])

    def test_signature_depends_on_plan_not_order(self):
        self.assertEqual(ml.sync_signature(["a", "b"], ["c"]), ml.sync_signature(["b", "a"], ["c"]))
        self.assertNotEqual(ml.sync_signature(["a"], ["c"]), ml.sync_signature(["a"], ["c", "d"]))

    def test_sync_members_adds_before_removing(self):
        calls = []
        with mock.patch.object(ml, "_current_emails", return_value=(["a@x.ph", "b@x.ph"], {})), \
             mock.patch.object(ml, "change_members",
                               side_effect=lambda t, a, act, em, notify=False: calls.append((act, em)) or
                               [(e, "done", None) for e in em]):
            added, removed, unchanged = ml.sync_members("forwarder", "g@x.ph", ["b@x.ph", "c@x.ph"])
        self.assertEqual(calls, [("add", ["c@x.ph"]), ("remove", ["a@x.ph"])])
        self.assertEqual(unchanged, ["b@x.ph"])


class SyncRouteTests(unittest.TestCase):
    URL = "/admin/mailing-lists/forwarder/g@cohere.ph/members"

    def setUp(self):
        app = Flask(__name__)
        app.secret_key = "test"
        app.register_blueprint(ml.mailing_lists_bp)
        self.client = app.test_client()
        with self.client.session_transaction() as s:
            s["user"] = {"email": "x@cohere.ph"}
            s["permissions"] = {"can_mailing_lists": True}
            s["_csrf_token"] = "good"
        self.rendered = []
        patches = [mock.patch.object(ml, "_current_emails", return_value=(["a@cohere.ph", "b@cohere.ph"], {})),
                   mock.patch.object(ml, "load_employee_index", return_value={}),
                   mock.patch.object(ml, "_render_detail",
                                     side_effect=lambda *a, **kw: self.rendered.append(kw) or "page")]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def post(self, **data):
        return self.client.post(self.URL, data=dict({"csrf_token": "good"}, **data))

    def test_preview_changes_nothing(self):
        with mock.patch.object(ml, "sync_members") as sm:
            r = self.post(action="sync_preview", emails="b@cohere.ph c@cohere.ph")
        self.assertEqual(r.status_code, 200)
        sm.assert_not_called()
        plan = self.rendered[0]["sync_preview"]
        self.assertEqual([m["email"] for m in plan["to_add"]], ["c@cohere.ph"])
        self.assertEqual([m["email"] for m in plan["to_remove"]], ["a@cohere.ph"])
        self.assertEqual(plan["unchanged"], 1)
        self.assertFalse(plan["removes_all"])

    def test_confirm_with_matching_signature_applies(self):
        sig = ml.sync_signature(["c@cohere.ph"], ["a@cohere.ph"])
        with mock.patch.object(ml, "sync_members", return_value=([], [], [])) as sm:
            r = self.post(action="sync", emails="b@cohere.ph c@cohere.ph", signature=sig)
        self.assertEqual(r.status_code, 302)
        sm.assert_called_once_with("forwarder", "g@cohere.ph", ["b@cohere.ph", "c@cohere.ph"], notify=False)

    def test_confirm_after_list_changed_shows_new_preview_instead(self):
        stale = ml.sync_signature(["c@cohere.ph"], [])
        with mock.patch.object(ml, "sync_members") as sm:
            r = self.post(action="sync", emails="b@cohere.ph c@cohere.ph", signature=stale)
        self.assertEqual(r.status_code, 200)
        sm.assert_not_called()
        self.assertIn("changed since the preview", self.rendered[0]["sync_preview"]["note"])

    def test_empty_sync_is_refused(self):
        with mock.patch.object(ml, "sync_members") as sm:
            r = self.post(action="sync_preview", emails="  ")
        self.assertEqual(r.status_code, 302)
        sm.assert_not_called()

    def test_flags_when_every_member_would_be_removed(self):
        self.post(action="sync_preview", emails="z@cohere.ph")
        self.assertTrue(self.rendered[0]["sync_preview"]["removes_all"])



HELD_PAGE = """<FORM action="../admindb/l_x.ph" method="POST" >
<input type="hidden" name="csrf_token" value="tok9">
<center><h2>Posting Held for Approval (1 of 2)</h2></center>
<table><tr><td ALIGN="right"><strong>From:</strong></td><td>Bad@Spam.example</td></tr>
<tr><td ALIGN="right"><strong>Subject:</strong></td><td>***SPAM*** Verify &amp; win</td></tr>
<tr><td ALIGN="right"><strong>Reason:</strong></td><td>Post by non-member to a members-only list</td></tr>
<tr><td ALIGN="right"><strong>Received:</strong></td><td>Fri May  1 17:07:46 2026</td></tr></table>
<INPUT name="28" type="RADIO" value="0" CHECKED ><INPUT name="28" type="RADIO" value="1" >
<INPUT name="28" type="RADIO" value="2" ><INPUT name="28" type="RADIO" value="3" >
<TEXTAREA NAME=comment-28 ROWS=4 COLS=76 WRAP=soft>Non-members are not allowed.</TEXTAREA>
<TEXTAREA NAME=headers-28 ROWS=10 COLS=76 READONLY>From: Bad@Spam.example
X-Spam-Status: Yes, score=5.8
  4.0 URI_PHISH              Phishing using web form</TEXTAREA>
<TEXTAREA NAME=fulltext-28 ROWS=20 COLS=76 READONLY>Click https://evil.example/login now&lt;!DOCTYPE html&gt;<html>junk</TEXTAREA>
<center><h2>Posting Held for Approval (2 of 2)</h2></center>
<table><tr><td ALIGN="right"><strong>From:</strong></td><td>colleague@cohere.ph</td></tr>
<tr><td ALIGN="right"><strong>Subject:</strong></td><td>Shift swap</td></tr>
<tr><td ALIGN="right"><strong>Reason:</strong></td><td>Message has implicit destination</td></tr>
<tr><td ALIGN="right"><strong>Received:</strong></td><td>Tue Oct 20 09:00:00 2026</td></tr></table>
<INPUT name="31" type="RADIO" value="0" CHECKED >
<TEXTAREA NAME=headers-31 ROWS=10 COLS=76 READONLY>X-Spam-Status: No, score=-1.0</TEXTAREA>
<TEXTAREA NAME=fulltext-31 ROWS=20 COLS=76 READONLY>Can someone swap Friday?</TEXTAREA>
"""

SUBS_PAGE = """<input type="hidden" name="csrf_token" value="tok9">
<h2>Subscription Requests</h2><table><tr><td>Juan Cruz<br><em>Juan.Cruz@cohere.ph</em><br>Tue Oct 20</td>
<td><INPUT name="40" type="RADIO" value="0" CHECKED ><INPUT name="40" type="RADIO" value="4" >
<INPUT name="40" type="RADIO" value="2" ><INPUT name="40" type="RADIO" value="3" ></td></tr>
<tr><td><em>leaver@cohere.ph</em></td><td><INPUT name="41" type="RADIO" value="0" CHECKED >
<INPUT name="41" type="RADIO" value="5" ><INPUT name="41" type="RADIO" value="2" ></td></tr></table>
<INPUT name="senderaction-x%40y.z" type="RADIO" value="0" CHECKED >"""


class ModerationParsingTests(unittest.TestCase):
    def test_held_messages(self):
        a, b = ml.parse_held_messages(HELD_PAGE)
        self.assertEqual((a["id"], a["sender"], a["subject"]), (28, "bad@spam.example", "***SPAM*** Verify & win"))
        self.assertEqual(a["spam"], {"flag": True, "score": 5.8, "phish": True})
        self.assertEqual(a["link_domains"], ["evil.example"])
        self.assertEqual(a["preview"], "Click https://evil.example/login now")   # raw HTML part cut off
        self.assertEqual(a["default_comment"], "Non-members are not allowed.")
        self.assertEqual((b["id"], b["spam"]["flag"], b["spam"]["phish"]), (31, False, False))

    def test_subscription_requests(self):
        subs = ml.parse_pending_subs(SUBS_PAGE)
        self.assertEqual([(r["id"], r["kind"], r["sender"]) for r in subs],
                         [(40, "subscribe", "juan.cruz@cohere.ph"), (41, "unsubscribe", "leaver@cohere.ph")])

    def test_moderation_form_codes(self):
        post = {"id": 28, "kind": "post", "sender": "bad@spam.example", "default_comment": "Default."}
        sub = {"id": 40, "kind": "subscribe"}
        unsub = {"id": 41, "kind": "unsubscribe"}
        self.assertEqual(ml.moderation_form([post, sub, unsub], "accept"), {"28": "1", "40": "4", "41": "5"})
        self.assertEqual(ml.moderation_form([post], "reject"), {"28": "2", "comment-28": "Default."})
        self.assertEqual(ml.moderation_form([post], "reject", "Go away"), {"28": "2", "comment-28": "Go away"})
        self.assertEqual(ml.moderation_form([post], "discard", filter_senders=["bad@spam.example"]),
                         {"28": "3", "senderfilterp-bad%40spam.example": "1", "senderfilter-bad%40spam.example": "3"})


class ModerateTests(unittest.TestCase):
    def test_discard_posts_form_verifies_and_audits(self):
        after_details = HELD_PAGE.split('<center><h2>Posting Held for Approval (2 of 2)')[0].replace("(1 of 2)", "(1 of 1)")
        pages = iter([HELD_PAGE, SUBS_PAGE,              # before
                      after_details.replace('name="28"', 'name="31"'), ""])  # after: 28 gone, 31 left
        sess = mock.MagicMock()
        sess.__enter__.return_value = sess
        sess.post.side_effect = [mock.Mock(text="ok", raise_for_status=lambda: None),   # login
                                 mock.Mock(text="done", raise_for_status=lambda: None)]  # decision
        sess.get.side_effect = lambda *a, **k: mock.Mock(text=next(pages), raise_for_status=lambda: None)
        with mock.patch.object(ml, "fetch_mailman_lists", return_value=({"l@x.ph": {"listid": "l_x.ph"}}, 0)), \
             mock.patch.object(ml, "_uapi", return_value="otp"), \
             mock.patch.object(ml.requests, "Session", return_value=sess), \
             mock.patch.object(ml, "_write_audit") as audit:
            results = ml.moderate("l@x.ph", "discard", [28, 99], filter_future=True)
        url = sess.post.call_args_list[1].args[0]
        data = sess.post.call_args_list[1].kwargs["data"]
        self.assertTrue(url.endswith("/admindb/l_x.ph"))
        self.assertEqual(data["28"], "3")
        self.assertEqual(data["csrf_token"], "tok9")
        self.assertEqual(data["senderfilterp-bad%40spam.example"], "1")
        self.assertNotIn("31", data)                   # untouched items are never sent
        self.assertEqual([(it["id"], r) for it, r, _ in results], [(28, "done"), (99, "skipped")])
        self.assertEqual(audit.call_args.args[2], "moderate")

    def test_accept_by_sender_expands_ids_and_sets_accept_filter(self):
        pages = iter([HELD_PAGE, SUBS_PAGE, "", ""])     # after: queue empty
        sess = mock.MagicMock()
        sess.__enter__.return_value = sess
        sess.post.side_effect = [mock.Mock(text="ok", raise_for_status=lambda: None),
                                 mock.Mock(text="done", raise_for_status=lambda: None)]
        sess.get.side_effect = lambda *a, **k: mock.Mock(text=next(pages), raise_for_status=lambda: None)
        with mock.patch.object(ml, "fetch_mailman_lists", return_value=({"l@x.ph": {"listid": "l_x.ph"}}, 0)), \
             mock.patch.object(ml, "_uapi", return_value="otp"), \
             mock.patch.object(ml.requests, "Session", return_value=sess), \
             mock.patch.object(ml, "_write_audit"):
            results = ml.moderate("l@x.ph", "accept", [], filter_future=True, senders=["colleague@cohere.ph"])
        data = sess.post.call_args_list[1].kwargs["data"]
        self.assertEqual(data["31"], "1")
        self.assertNotIn("28", data)
        self.assertEqual(data["senderfilter-colleague%40cohere.ph"], "6")
        self.assertEqual([(it["id"], r) for it, r, _ in results], [(31, "done")])


class PendingRouteTests(unittest.TestCase):
    def setUp(self):
        app = Flask(__name__)
        app.secret_key = "test"
        app.register_blueprint(ml.mailing_lists_bp)
        self.client = app.test_client()
        with self.client.session_transaction() as s:
            s["user"] = {"email": "x@cohere.ph"}
            s["permissions"] = {"can_mailing_lists": True}
            s["_csrf_token"] = "good"
        p = mock.patch.object(ml, "fetch_mailman_lists", return_value=({"l@cohere.ph": {"listid": "l_cohere.ph"}}, 0))
        p.start()
        self.addCleanup(p.stop)

    def test_unknown_list_404(self):
        self.assertEqual(self.client.get("/admin/mailing-lists/mailman/nope@cohere.ph/pending").status_code, 404)

    def test_post_requires_csrf_and_valid_action(self):
        with mock.patch.object(ml, "moderate") as mod:
            self.client.post("/admin/mailing-lists/mailman/l@cohere.ph/pending",
                             data={"csrf_token": "bad", "action": "discard", "item": "28"})
            r = self.client.post("/admin/mailing-lists/mailman/l@cohere.ph/pending",
                                 data={"csrf_token": "good", "action": "approve-all", "item": "28"})
        self.assertEqual(r.status_code, 400)
        mod.assert_not_called()

    def test_post_passes_ids_and_options(self):
        with mock.patch.object(ml, "moderate", return_value=[]) as mod:
            r = self.client.post("/admin/mailing-lists/mailman/l@cohere.ph/pending",
                                 data={"csrf_token": "good", "action": "discard", "item": ["28", "x", "29"],
                                       "filter_future": "1", "comment": ""})
        self.assertEqual(r.status_code, 302)
        mod.assert_called_once_with("l@cohere.ph", "discard", [28, 29], comment="", filter_future=True, senders=[])

    def test_post_by_sender_only(self):
        with mock.patch.object(ml, "moderate", return_value=[]) as mod:
            self.client.post("/admin/mailing-lists/mailman/l@cohere.ph/pending",
                             data={"csrf_token": "good", "action": "accept", "sender": "Send_Email@cohere.ph"})
        self.assertEqual(mod.call_args.kwargs["senders"], ["send_email@cohere.ph"])

    def test_no_permission_404(self):
        with self.client.session_transaction() as s:
            s["permissions"] = {}
        self.assertEqual(self.client.get("/admin/mailing-lists/api/pending/l@cohere.ph").status_code, 404)



class SpamBlocklistTests(unittest.TestCase):
    def test_blocklist_entry_rules(self):
        protected = {"cohere.ph", "cohereph.com"}
        allow = ["*@okta.com", "*.oktaemail.com", "notification@slack.com"]
        e = lambda s: ml.blocklist_entry(s, protected, allow)
        self.assertEqual(e("cpanel@LYLTS.com"), "*@lylts.com")
        self.assertEqual(e("scammer@gmail.com"), "scammer@gmail.com")      # free mail: address only
        self.assertIsNone(e("send_email@cohere.ph"))
        self.assertIsNone(e("x@mail.cohere.ph"))
        self.assertIsNone(e("a@okta.com"))
        self.assertIsNone(e("a@eu.oktaemail.com"))
        self.assertIsNone(e("notification@slack.com"))
        self.assertIsNone(e("no-at-sign"))

    def test_add_appends_to_full_list_and_verifies(self):
        state = {"blacklist_from": ["*@old.example"], "whitelist_from": ["*@okta.com"]}
        def uapi(fn, params=None, write=False, module="Email"):
            if fn == "get_user_preferences":
                return {k: list(v) for k, v in state.items()}
            if fn == "update_user_preference":
                state["blacklist_from"] = [v for k, v in params if k == "value"]
                return None
            raise AssertionError(fn)
        with mock.patch.object(ml, "_uapi", side_effect=uapi), \
             mock.patch.object(ml, "fetch_mail_domains", return_value=(["cohere.ph"], 0)):
            added, already, refused = ml.spam_blocklist_add(["cpanel@lylts.com", "x@old.example", "hr@cohere.ph"])
        self.assertEqual(added, ["*@lylts.com"])
        self.assertEqual(already, ["*@old.example"])
        self.assertEqual(refused, ["hr@cohere.ph"])
        self.assertEqual(state["blacklist_from"], ["*@old.example", "*@lylts.com"])   # old entries kept

    def test_add_raises_if_cpanel_drops_entries(self):
        def uapi(fn, params=None, write=False, module="Email"):
            if fn == "get_user_preferences":
                return {"blacklist_from": ["*@old.example"], "whitelist_from": []}
            return None                                    # write silently ignored
        with mock.patch.object(ml, "_uapi", side_effect=uapi), \
             mock.patch.object(ml, "fetch_mail_domains", return_value=(["cohere.ph"], 0)):
            with self.assertRaises(RuntimeError):
                ml.spam_blocklist_add(["cpanel@lylts.com"])



SENDER_FORM = """<FORM action="../admin/l_x.ph/privacy/sender" method="POST">
<input type="hidden" name="csrf_token" value="t1">
<INPUT name="generic_nonmember_action" type="RADIO" value="1" CHECKED ><INPUT name="generic_nonmember_action" type="RADIO" value="3" >
<TEXTAREA NAME=accept_these_nonmembers ROWS=10>
*@cohere.ph
wfm@cohere.ph</TEXTAREA>
<TEXTAREA NAME=discard_these_nonmembers ROWS=10>spam@x.com</TEXTAREA>
<INPUT name="forward_auto_discards" type="RADIO" value="1" CHECKED >
<INPUT name="submit" type="SUBMIT" value="Submit Your Changes" ></FORM>"""


def _form_session(pages):
    """Fake logged-in Mailman session: GET returns pages in order; records POSTs."""
    sess = mock.MagicMock()
    it = iter(pages)
    sess.get.side_effect = lambda *a, **k: mock.Mock(text=next(it), raise_for_status=lambda: None)
    sess.post.return_value = mock.Mock(text="saved", raise_for_status=lambda: None)
    return sess


class FormUpdateTests(unittest.TestCase):
    def test_form_fields_like_a_browser(self):
        f = ml.form_fields(SENDER_FORM)
        self.assertIn(("accept_these_nonmembers", "*@cohere.ph\nwfm@cohere.ph"), f)
        self.assertIn(("generic_nonmember_action", "1"), f)
        self.assertNotIn("submit", dict(f))

    def test_update_posts_everything_and_checks_only_target_changed(self):
        after = SENDER_FORM.replace("wfm@cohere.ph</TEXTAREA>", "wfm@cohere.ph\nsend_email@cohere.ph</TEXTAREA>")
        sess = _form_session([SENDER_FORM, after])
        ml.mailman_form_update(sess, "u", set_fields={"accept_these_nonmembers": "*@cohere.ph\nwfm@cohere.ph\nsend_email@cohere.ph"})
        posted = dict(sess.post.call_args.kwargs["data"])
        self.assertEqual(posted["discard_these_nonmembers"], "spam@x.com")    # untouched fields re-sent as-is
        self.assertEqual(posted["generic_nonmember_action"], "1")
        self.assertEqual(posted["csrf_token"], "t1")

    def test_update_refuses_when_something_else_changed(self):
        after = SENDER_FORM.replace("wfm@cohere.ph</TEXTAREA>", "wfm@cohere.ph\nnew@x.ph</TEXTAREA>") \
                           .replace('value="1" CHECKED ><INPUT name="generic_nonmember_action" type="RADIO" value="3" >',
                                    'value="1" ><INPUT name="generic_nonmember_action" type="RADIO" value="3" CHECKED >')
        sess = _form_session([SENDER_FORM, after])
        with self.assertRaises(RuntimeError) as cm:
            ml.mailman_form_update(sess, "u", set_fields={"accept_these_nonmembers": "*@cohere.ph\nwfm@cohere.ph\nnew@x.ph"})
        self.assertIn("generic_nonmember_action", str(cm.exception))

    def test_update_refuses_when_not_applied(self):
        sess = _form_session([SENDER_FORM, SENDER_FORM])
        with self.assertRaises(RuntimeError):
            ml.mailman_form_update(sess, "u", set_fields={"accept_these_nonmembers": "x@y.ph"})


class DeliveryAndSenderTests(unittest.TestCase):
    def test_parse_nomail(self):
        html = ('<INPUT name="a%40x.ph_nomail" type="CHECKBOX" value="on" CHECKED >[B]'
                '<INPUT name="b%40x.ph_nomail" type="CHECKBOX" value="off" >'
                '<INPUT name="c%40x.ph_nomail" type="CHECKBOX" value="on" CHECKED >[A]')
        self.assertEqual(ml.parse_mailman_nomail(html), {"a@x.ph": "B", "c@x.ph": "A"})

    def test_classify_marks_delivery_off(self):
        out = ml.classify_members(["a@x.ph", "b@x.ph"], {}, {"a@x.ph": "B"})
        self.assertEqual([m["delivery_off"] for m in out], ["B", None])
        self.assertEqual(ml.summarize(out)["delivery_off"], 1)

    def test_sender_entry_rules(self):
        self.assertIsNone(ml.sender_entry_problem("send_email@cohere.ph"))
        self.assertIsNone(ml.sender_entry_problem(r"^.*@cohere\.ph$"))
        self.assertIn("wildcard", ml.sender_entry_problem("*@cohere.ph"))
        self.assertIsNotNone(ml.sender_entry_problem("^(broken"))
        self.assertIsNotNone(ml.sender_entry_problem("cohere.ph"))
        rx = ml.wildcard_to_regex("*@Cohere.ph")
        self.assertEqual(rx, r"^.*@cohere\.ph$")
        import re
        self.assertTrue(re.match(rx, "send_email@cohere.ph"))
        self.assertFalse(re.match(rx, "x@cohere.phish.example"))

    def test_update_sender_filter_adds_once(self):
        after = SENDER_FORM.replace("wfm@cohere.ph</TEXTAREA>", "wfm@cohere.ph\nsend_email@cohere.ph</TEXTAREA>")
        sess = _form_session([SENDER_FORM, SENDER_FORM, after])
        sess.__enter__.return_value = sess
        with mock.patch.object(ml, "fetch_mailman_lists", return_value=({"l@x.ph": {"listid": "l_x.ph"}}, 0)), \
             mock.patch.object(ml, "_mailman_session") as ms, \
             mock.patch.object(ml, "_write_audit") as audit:
            ms.return_value.__enter__.return_value = (sess, "https://h/mailman/admin/l_x.ph", "")
            before, result = ml.update_sender_filter("l@x.ph", "accept_these_nonmembers", add=["send_email@cohere.ph"])
        self.assertEqual(result, ["*@cohere.ph", "wfm@cohere.ph", "send_email@cohere.ph"])
        self.assertEqual(audit.call_args.args[2], "senders")



class DeleteTests(unittest.TestCase):
    def test_forwarder_delete_records_members_then_removes_each(self):
        reads = iter([({"g@x.ph": ["a@x.ph", "b@x.ph"]}, 0), ({}, 0)])
        with mock.patch.object(ml, "fetch_forwarders", side_effect=lambda *a, **k: next(reads)), \
             mock.patch.object(ml, "forwarder_change") as fc, \
             mock.patch.object(ml, "_write_audit") as audit:
            ok, _ = ml.delete_group("forwarder", "g@x.ph")
        self.assertTrue(ok)
        self.assertEqual([c.args for c in fc.call_args_list], [("g@x.ph", "remove", "a@x.ph"), ("g@x.ph", "remove", "b@x.ph")])
        first = audit.call_args_list[0].args[3]
        self.assertEqual([r[0] for r in first], ["a@x.ph", "b@x.ph"])      # members saved before deleting

    def test_mailman_delete_reports_failure_if_list_still_there(self):
        with mock.patch.object(ml, "_current_emails", return_value=({"a@x.ph"}, {})), \
             mock.patch.object(ml, "_uapi") as u, \
             mock.patch.object(ml, "fetch_mailman_lists", return_value=({"l@x.ph": {}}, 0)), \
             mock.patch.object(ml, "_write_audit"):
            ok, msg = ml.delete_group("mailman", "l@x.ph")
        u.assert_called_once_with("delete_list", {"list": "l@x.ph"}, write=True)
        self.assertFalse(ok)

    def test_route_needs_exact_typed_address(self):
        app = Flask(__name__)
        app.secret_key = "t"
        app.register_blueprint(ml.mailing_lists_bp)
        c = app.test_client()
        with c.session_transaction() as s:
            s["user"] = {"email": "x@cohere.ph"}; s["is_admin"] = True; s["_csrf_token"] = "good"
        with mock.patch.object(ml, "delete_group", return_value=(True, "ok")) as dg:
            c.post("/admin/mailing-lists/mailman/l@cohere.ph/delete", data={"csrf_token": "good", "confirm": "l@cohere"})
            dg.assert_not_called()
            c.post("/admin/mailing-lists/mailman/l@cohere.ph/delete", data={"csrf_token": "good", "confirm": " L@cohere.ph "})
            dg.assert_called_once_with("mailman", "l@cohere.ph")



class PeopleTests(unittest.TestCase):
    def setUp(self):
        app = Flask(__name__)
        app.secret_key = "t"
        app.register_blueprint(ml.mailing_lists_bp)
        self.client = app.test_client()

    def login(self, **perms):
        with self.client.session_transaction() as s:
            s["user"] = {"email": "x@cohere.ph"}
            s["permissions"] = perms
            s["_csrf_token"] = "good"

    def test_all_memberships_merges_forwarders_and_mailman(self):
        with mock.patch.object(ml, "fetch_forwarders", return_value=({"hr@x.ph": ["a@x.ph"]}, 0)), \
             mock.patch.object(ml, "fetch_mailman_lists", return_value=({"l@x.ph": {}, "m@x.ph": {}}, 0)), \
             mock.patch.object(ml, "fetch_mailman_members", side_effect=lambda a, r=False: (
                 {"members": {"a@x.ph"} if a == "l@x.ph" else {"b@x.ph"}, "nomail": {"a@x.ph": "B"}}, 0)):
            allm, errors = ml.all_memberships()
        self.assertEqual(allm["a@x.ph"], [{"type": "forwarder", "address": "hr@x.ph", "delivery_off": None},
                                          {"type": "mailman", "address": "l@x.ph", "delivery_off": "B"}])
        self.assertEqual([g["address"] for g in allm["b@x.ph"]], ["m@x.ph"])
        self.assertEqual(errors, [])

    def test_offboarding_staff_can_use_offboarding_api_only(self):
        self.login(can_onboarding=True)
        with mock.patch.object(ml, "memberships_of", return_value=([{"type": "mailman", "address": "l@x.ph"}], [])):
            r = self.client.get("/admin/mailing-lists/api/memberships?email=a@cohere.ph")
        self.assertEqual(r.get_json()["groups"][0]["address"], "l@x.ph")
        self.assertEqual(self.client.get("/admin/mailing-lists/person").status_code, 404)
        self.assertEqual(self.client.get("/admin/mailing-lists/cleanup").status_code, 404)
        self.assertEqual(self.client.get("/admin/mailing-lists/").status_code, 404)

    def test_remove_everywhere_needs_csrf_and_removes_each(self):
        self.login(can_onboarding=True)
        r = self.client.post("/admin/mailing-lists/api/remove-everywhere", json={"email": "a@cohere.ph"})
        self.assertEqual(r.status_code, 403)
        groups = [{"type": "forwarder", "address": "hr@x.ph"}, {"type": "mailman", "address": "l@x.ph"}]
        with mock.patch.object(ml, "memberships_of", return_value=(groups, [])), \
             mock.patch.object(ml, "change_members", return_value=[("a@cohere.ph", "done", None)]) as cm:
            r = self.client.post("/admin/mailing-lists/api/remove-everywhere", json={"email": "A@cohere.ph"},
                                 headers={"X-CSRF-Token": "good"})
        self.assertEqual([c.args for c in cm.call_args_list],
                         [("forwarder", "hr@x.ph", "remove", ["a@cohere.ph"]), ("mailman", "l@x.ph", "remove", ["a@cohere.ph"])])
        self.assertEqual([x["result"] for x in r.get_json()["results"]], ["done", "done"])

    def test_cleanup_groups_picks_per_list(self):
        self.login(can_mailing_lists=True)
        with mock.patch.object(ml, "change_members", return_value=[]) as cm:
            self.client.post("/admin/mailing-lists/cleanup", data={"csrf_token": "good", "pick": [
                "a@cohere.ph|mailman|l@x.ph", "b@cohere.ph|mailman|l@x.ph", "a@cohere.ph|forwarder|hr@x.ph",
                "bad|mailman|l@x.ph", "c@cohere.ph|evil|z@x.ph"]})
        self.assertEqual(sorted(c.args for c in cm.call_args_list),
                         [("forwarder", "hr@x.ph", "remove", ["a@cohere.ph"]),
                          ("mailman", "l@x.ph", "remove", ["a@cohere.ph", "b@cohere.ph"])])

    def test_person_post_ignores_unknown_groups(self):
        self.login(can_mailing_lists=True)
        with mock.patch.object(ml, "fetch_forwarders", return_value=({"hr@x.ph": []}, 0)), \
             mock.patch.object(ml, "fetch_mailman_lists", return_value=({"l@x.ph": {}}, 0)), \
             mock.patch.object(ml, "change_members", return_value=[("a@cohere.ph", "done", None)]) as cm:
            self.client.post("/admin/mailing-lists/person", data={"csrf_token": "good", "email": "a@cohere.ph",
                             "action": "add", "group": ["mailman|l@x.ph", "mailman|nope@x.ph", "forwarder|hr@x.ph"]})
        self.assertEqual([c.args[:2] for c in cm.call_args_list], [("mailman", "l@x.ph"), ("forwarder", "hr@x.ph")])



class PendingAlertTests(unittest.TestCase):
    def test_alert_email_escapes_and_flags(self):
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))
        import mailing_list_pending_check as chk
        items = [{"id": 5, "kind": "post", "sender": "x@evil.example", "subject": "<script>alert(1)</script>",
                  "spam": {"flag": True, "score": 9.0, "phish": True}},
                 {"id": 6, "kind": "subscribe", "sender": "new@cohere.ph", "subject": "", "spam": None}]
        subject, body = chk.build_email({"l@x.ph": items}, {"l@x.ph": (1, 1)})
        self.assertIn("2 new items", subject)
        self.assertNotIn("<script>", body)
        self.assertIn("&lt;script&gt;", body)
        self.assertIn("Phishing link", body)
        self.assertIn("Asks to join", body)
        self.assertIn("/admin/mailing-lists/mailman/l@x.ph/pending", body)



if __name__ == "__main__":
    unittest.main()
