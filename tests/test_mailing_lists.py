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



if __name__ == "__main__":
    unittest.main()
