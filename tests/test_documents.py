"""
Document Library (modules/documents.py) against the real central_db.

Rows are created with a ZZTEST- title and removed, with their files, in
tearDownClass. Run from the repo root with .env loaded:
    ./venv/bin/python -m unittest tests.test_documents -v
"""
import io
import os
import unittest

from reportlab.pdfgen import canvas
from PIL import Image

import app as app_module
from db_core import get_db_connection
from modules.documents import STORAGE_ROOT, ensure_schema

PREFIX = "ZZTEST-doc"
WORD = "zzquokkaclause"   # unique word to find through full-text search


def _pdf(text):
    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    y = 800
    for line in text.split("\n"):
        c.drawString(72, y, line)
        y -= 16
    c.save()
    return buf.getvalue()


def _jpg(color="white"):
    buf = io.BytesIO()
    Image.new("RGB", (40, 40), color).save(buf, "JPEG")
    return buf.getvalue()


def _login(client, admin=False, perms=None, email="zztest.user@cohere.ph"):
    with client.session_transaction() as s:
        s["user"] = {"email": email, "emp_number": 0, "name": "ZZ Test", "employee_id": "ZZTEST-0"}
        s["is_admin"] = admin
        s["is_supervisor"] = False
        s["is_sub_admin"] = bool(perms)
        s["permissions"] = perms or {}
        s["_csrf_token"] = "zztoken"


class DocumentLibraryTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        ensure_schema()
        app_module.app.config["TESTING"] = True
        cls.app = app_module.app

    @classmethod
    def tearDownClass(cls):
        conn = get_db_connection()
        try:
            cur = conn.cursor()
            cur.execute("SELECT id, stored_path FROM hr_documents WHERE title LIKE %s", (PREFIX + "%",))
            rows = cur.fetchall()
            for r in rows:
                path = os.path.join(STORAGE_ROOT, r["stored_path"])
                if os.path.isfile(path):
                    os.remove(path)
            ids = [r["id"] for r in rows]
            if ids:
                marks = ",".join(["%s"] * len(ids))
                cur.execute(f"DELETE FROM hr_document_access_log WHERE document_id IN ({marks})", ids)
                cur.execute(f"DELETE FROM hr_documents WHERE id IN ({marks})", ids)
            cur.execute("DELETE FROM hr_document_folders WHERE name LIKE %s", (PREFIX + "%",))
            conn.commit()
        finally:
            conn.close()

    def _upload(self, client, files, **form):
        data = {"csrf_token": "zztoken", "category": "Memo", "visibility": "hr", **form}
        data["files"] = [(io.BytesIO(b), name) for name, b in files]
        return client.post("/documents/upload", data=data,
                           content_type="multipart/form-data", follow_redirects=False)

    def _id_for(self, title):
        conn = get_db_connection()
        try:
            cur = conn.cursor()
            cur.execute("SELECT id, text_status, visibility FROM hr_documents "
                        "WHERE title = %s AND deleted_at IS NULL", (title,))
            return cur.fetchone()
        finally:
            conn.close()

    def test_full_flow(self):
        c = self.app.test_client()
        _login(c, admin=True)

        self.assertEqual(c.get("/documents").status_code, 200)

        pdf = _pdf(f"Memorandum\nSubject: {WORD} on attendance policy\n" + "Body text line. " * 10)
        r = self._upload(c, [(f"{PREFIX}-text.pdf", pdf),
                             (f"{PREFIX}-scan.jpg", _jpg()),
                             (f"{PREFIX}-bad.pdf", b"not a pdf"),
                             (f"{PREFIX}-x.exe", b"MZ")],
                         reference_no="ZZ-001")
        self.assertEqual(r.status_code, 302)

        text_doc = self._id_for(f"{PREFIX}-text")
        scan_doc = self._id_for(f"{PREFIX}-scan")
        self.assertEqual(text_doc["text_status"], "extracted")
        self.assertEqual(scan_doc["text_status"], "needs_ocr")
        self.assertIsNone(self._id_for(f"{PREFIX}-bad"))

        # same bytes again is skipped as a duplicate
        self._upload(c, [(f"{PREFIX}-again.pdf", pdf)])
        self.assertIsNone(self._id_for(f"{PREFIX}-again"))

        # full-text search finds the word inside the PDF and shows a snippet
        page = c.get(f"/documents?q={WORD}").get_data(as_text=True)
        self.assertIn(f"{PREFIX}-text", page)
        self.assertIn("attendance policy", page)
        self.assertNotIn(f"{PREFIX}-scan", page)
        # short / reference searches go through LIKE
        self.assertIn(f"{PREFIX}-scan", c.get("/documents?q=ZZ-001").get_data(as_text=True))

        did = text_doc["id"]
        self.assertEqual(c.get(f"/documents/{did}").status_code, 200)
        f = c.get(f"/documents/{did}/file")
        self.assertEqual(f.status_code, 200)
        self.assertEqual(f.mimetype, "application/pdf")
        self.assertTrue(f.data.startswith(b"%PDF"))
        self.assertIn("attachment", c.get(f"/documents/{did}/file?download=1").headers["Content-Disposition"])

        # no can_documents: the whole library is hidden (404) and not in the menu
        for perms in (None, {"can_memos": True}):
            n = self.app.test_client()
            _login(n, perms=perms)
            self.assertEqual(n.get("/documents").status_code, 404)
            self.assertEqual(n.get(f"/documents/{did}").status_code, 404)
            self.assertEqual(n.get(f"/documents/{did}/file").status_code, 404)
            self.assertEqual(self._upload(n, [(f"{PREFIX}-emp.pdf", _pdf("x"))]).status_code, 404)

        # can_documents alone: library opens, HR-only docs stay invisible, no managing
        e = self.app.test_client()
        _login(e, perms={"can_documents": True})
        self.assertEqual(e.get("/documents").status_code, 200)
        self.assertEqual(e.get(f"/documents/{did}").status_code, 404)
        self.assertEqual(e.get(f"/documents/{did}/file").status_code, 404)
        self.assertNotIn(f"{PREFIX}-text", e.get(f"/documents?q={WORD}").get_data(as_text=True))
        self.assertEqual(self._upload(e, [(f"{PREFIX}-emp.pdf", _pdf("x"))]).status_code, 403)

        # can_documents + can_memos sees HR docs but can't manage
        h = self.app.test_client()
        _login(h, perms={"can_documents": True, "can_memos": True})
        self.assertEqual(h.get(f"/documents/{did}").status_code, 200)
        self.assertEqual(h.post(f"/documents/{did}/delete", data={"csrf_token": "zztoken"}).status_code, 403)

        # edit to "everyone" -> a can_documents user can open it
        r = c.post(f"/documents/{did}/edit", data={
            "csrf_token": "zztoken", "title": f"{PREFIX}-text", "category": "Policy",
            "visibility": "everyone", "doc_date": "2024-05-01"})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(e.get(f"/documents/{did}").status_code, 200)

        # missing CSRF token changes nothing
        c.post(f"/documents/{did}/delete", data={})
        self.assertIsNotNone(self._id_for(f"{PREFIX}-text"))

        c.post(f"/documents/{did}/delete", data={"csrf_token": "zztoken"})
        self.assertIsNone(self._id_for(f"{PREFIX}-text"))
        self.assertEqual(c.get(f"/documents/{did}").status_code, 404)

    def test_menu_item_follows_permission(self):
        for perms, shown in ((None, False), ({"can_memos": True}, False), ({"can_documents": True}, True)):
            n = self.app.test_client()
            _login(n, perms=perms)
            html = n.get("/history").get_data(as_text=True)
            self.assertEqual("Document Library" in html, shown, perms)

    def test_requires_login(self):
        c = self.app.test_client()
        self.assertEqual(c.get("/documents").status_code, 302)
        self.assertEqual(c.get("/documents/1/file").status_code, 302)

    def test_folder_batch_upload_json(self):
        c = self.app.test_client()
        _login(c, admin=True)
        r = c.post("/documents/upload", headers={"Accept": "application/json"},
                   content_type="multipart/form-data", data={
                       "csrf_token": "zztoken", "category": "auto", "visibility": "hr",
                       "employee_id": "ZZTEST-EMP",
                       "files": [(io.BytesIO(_jpg("gray")), f"{PREFIX}-NTE late.jpg"),
                                 (io.BytesIO(_pdf("Dismissal notice " + "x " * 40)), f"{PREFIX}-dismissal.pdf"),
                                 (io.BytesIO(b"hello"), "notes.docx")]})
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        cats = {a["title"]: a["category"] for a in body["added"]}
        self.assertEqual(cats, {f"{PREFIX}-NTE late": "Notice to Explain",
                                f"{PREFIX}-dismissal": "Notice of Decision"})
        self.assertEqual(len(body["skipped"]), 1)

        # "auto" is only for uploads; editing can't store it as a category
        did = self._id_for(f"{PREFIX}-NTE late")["id"]
        c.post(f"/documents/{did}/edit", data={"csrf_token": "zztoken", "title": f"{PREFIX}-NTE late",
                                               "category": "auto", "visibility": "hr"})
        conn = get_db_connection()
        try:
            cur = conn.cursor()
            cur.execute("SELECT category, employee_id FROM hr_documents WHERE id = %s", (did,))
            row = cur.fetchone()
        finally:
            conn.close()
        self.assertEqual(row["category"], "Other")

        bad = c.post("/documents/upload", headers={"Accept": "application/json"},
                     data={"csrf_token": "nope"}, content_type="multipart/form-data")
        self.assertEqual(bad.status_code, 400)
        self.assertIn("error", bad.get_json())

    def test_match_folders(self):
        c = self.app.test_client()
        _login(c, admin=True)
        conn = get_db_connection()
        try:
            cur = conn.cursor()
            cur.execute("SELECT employee_id, schedule_name FROM gsheet_employees "
                        "WHERE schedule_name LIKE '%% %%' AND employee_id REGEXP '^[0-9]{6}-[0-9]{2}$' LIMIT 1")
            emp = cur.fetchone()
        finally:
            conn.close()
        first, last = emp["schedule_name"].split()[0], emp["schedule_name"].split()[-1]
        folders = [f"{last.upper()}, {first}", f"scan {emp['employee_id']}", "zzqx nobody"]
        r = c.post("/documents/match-folders", json={"folders": folders},
                   headers={"X-CSRF-Token": "zztoken"})
        m = r.get_json()["matches"]
        self.assertIn(emp["employee_id"], [x["employee_id"] for x in m[folders[0]]["exact"]])
        self.assertEqual([x["employee_id"] for x in m[folders[1]]["exact"]], [emp["employee_id"]])
        self.assertEqual(m[folders[2]], {"exact": [], "maybe": []})

        self.assertEqual(c.post("/documents/match-folders", json={"folders": []}).status_code, 400)
        viewer = self.app.test_client()
        _login(viewer, perms={"can_documents": True})
        self.assertEqual(viewer.post("/documents/match-folders", json={"folders": folders},
                                     headers={"X-CSRF-Token": "zztoken"}).status_code, 403)

    def _folder(self, name):
        conn = get_db_connection()
        try:
            cur = conn.cursor()
            cur.execute("SELECT id, parent_id FROM hr_document_folders WHERE name = %s AND deleted_at IS NULL", (name,))
            return cur.fetchone()
        finally:
            conn.close()

    def test_company_folders(self):
        c = self.app.test_client()
        _login(c, admin=True)
        tok = {"csrf_token": "zztoken"}

        r = c.post("/documents/folders", data={**tok, "name": f"{PREFIX}-Policies"})
        self.assertEqual(r.status_code, 302)
        top = self._folder(f"{PREFIX}-Policies")
        self.assertIsNone(top["parent_id"])
        c.post("/documents/folders", data={**tok, "name": f"{PREFIX}-2026", "parent_id": top["id"]})
        sub = self._folder(f"{PREFIX}-2026")
        self.assertEqual(sub["parent_id"], top["id"])
        # same name in the same place is refused
        c.post("/documents/folders", data={**tok, "name": f"{PREFIX}-2026", "parent_id": top["id"]})
        conn = get_db_connection()
        try:
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) AS n FROM hr_document_folders WHERE name = %s AND deleted_at IS NULL",
                        (f"{PREFIX}-2026",))
            self.assertEqual(cur.fetchone()["n"], 1)
        finally:
            conn.close()

        # upload straight into the sub-folder, then land back on it
        back = f"/documents?folder={sub['id']}"
        r = self._upload(c, [(f"{PREFIX}-in-folder.jpg", _jpg("navy"))], folder_id=sub["id"], return_to=back)
        did = self._id_for(f"{PREFIX}-in-folder")["id"]
        page = c.get(back).get_data(as_text=True)
        self.assertIn(f"{PREFIX}-in-folder", page)
        self.assertIn(f"{PREFIX}-Policies", page)          # breadcrumb
        self.assertNotIn(f"{PREFIX}-in-folder", c.get("/documents?view=unfiled").get_data(as_text=True))
        # outside redirect targets are ignored
        r = self._upload(c, [(f"{PREFIX}-x.jpg", _jpg("olive")), (f"{PREFIX}-y.jpg", _jpg("teal"))],
                         return_to="//evil.example")
        self.assertTrue(r.headers["Location"].endswith("/documents"))

        # a folder with something in it can't be deleted
        c.post(f"/documents/folders/{top['id']}/delete", data=tok)
        self.assertIsNotNone(self._folder(f"{PREFIX}-Policies"))

        # move the document out (edit), then the sub-folder can go
        c.post(f"/documents/{did}/edit", data={**tok, "title": f"{PREFIX}-in-folder", "category": "Memo",
                                               "visibility": "hr", "folder_id": ""})
        self.assertIn(f"{PREFIX}-in-folder", c.get("/documents?view=unfiled").get_data(as_text=True))
        c.post(f"/documents/folders/{sub['id']}/rename", data={**tok, "name": f"{PREFIX}-2026b"})
        self.assertIsNotNone(self._folder(f"{PREFIX}-2026b"))
        c.post(f"/documents/folders/{sub['id']}/delete", data=tok)
        self.assertIsNone(self._folder(f"{PREFIX}-2026b"))
        self.assertEqual(c.get(f"/documents?folder={sub['id']}").status_code, 404)

        # library users without manage rights can browse but not change folders
        viewer = self.app.test_client()
        _login(viewer, perms={"can_documents": True})
        self.assertEqual(viewer.get(f"/documents?folder={top['id']}").status_code, 200)
        self.assertEqual(viewer.post("/documents/folders", data={**tok, "name": f"{PREFIX}-nope"}).status_code, 403)
        self.assertEqual(viewer.post(f"/documents/folders/{top['id']}/delete", data=tok).status_code, 403)

    def test_employee_tree(self):
        c = self.app.test_client()
        _login(c, admin=True)
        conn = get_db_connection()
        try:
            cur = conn.cursor()
            cur.execute("SELECT employee_id, schedule_name FROM gsheet_employees "
                        "WHERE schedule_name LIKE '%% %%' AND employee_id REGEXP '^[0-9]{6}-[0-9]{2}$' LIMIT 1")
            emp = cur.fetchone()
        finally:
            conn.close()
        self._upload(c, [(f"{PREFIX}-emp.jpg", _jpg("maroon"))], employee_id=emp["employee_id"])
        family = emp["schedule_name"].split()[-1]
        letter = family[0].upper()
        self.assertIn(letter, c.get("/documents?view=employees").get_data(as_text=True))
        listing = c.get(f"/documents?letter={letter}").get_data(as_text=True)
        self.assertIn(emp["employee_id"], listing)
        self.assertNotIn(f"{PREFIX}-emp", listing)          # letter pages list folders, not files
        self.assertIn(f"{PREFIX}-emp", c.get(f"/documents?employee_id={emp['employee_id']}").get_data(as_text=True))

    def test_bulk_move(self):
        c = self.app.test_client()
        _login(c, admin=True)
        tok = {"csrf_token": "zztoken"}
        c.post("/documents/folders", data={**tok, "name": f"{PREFIX}-Moves"})
        folder = self._folder(f"{PREFIX}-Moves")
        self._upload(c, [(f"{PREFIX}-m1.jpg", _jpg("purple")), (f"{PREFIX}-m2.jpg", _jpg("lime"))])
        ids = [self._id_for(f"{PREFIX}-m1")["id"], self._id_for(f"{PREFIX}-m2")["id"]]
        conn = get_db_connection()
        try:
            cur = conn.cursor()
            cur.execute("SELECT employee_id FROM gsheet_employees "
                        "WHERE employee_id REGEXP '^[0-9]{6}-[0-9]{2}$' LIMIT 1")
            emp_id = cur.fetchone()["employee_id"]
        finally:
            conn.close()

        def state():
            conn = get_db_connection()
            try:
                cur = conn.cursor()
                cur.execute("SELECT folder_id, employee_id FROM hr_documents WHERE id IN (%s, %s) ORDER BY id", ids)
                return [(r["folder_id"], r["employee_id"]) for r in cur.fetchall()]
            finally:
                conn.close()

        c.post("/documents/move", data={**tok, "doc_ids": ids, "folder": folder["id"],
                                        "employee": "set", "employee_id": emp_id})
        self.assertEqual(state(), [(folder["id"], emp_id)] * 2)
        # unknown employee: nothing changes
        c.post("/documents/move", data={**tok, "doc_ids": ids, "folder": "", "employee": "set",
                                        "employee_id": "ZZ-NOPE"})
        self.assertEqual(state(), [(folder["id"], emp_id)] * 2)
        # keep folder, drop the employee link
        c.post("/documents/move", data={**tok, "doc_ids": ids[:1], "folder": "keep", "employee": "clear"})
        self.assertEqual(state(), [(folder["id"], None), (folder["id"], emp_id)])
        c.post("/documents/move", data={**tok, "doc_ids": ids, "folder": "", "employee": "keep"})
        self.assertEqual(state(), [(None, None), (None, emp_id)])

        viewer = self.app.test_client()
        _login(viewer, perms={"can_documents": True})
        self.assertEqual(viewer.post("/documents/move", data={**tok, "doc_ids": ids, "folder": ""}).status_code, 403)

    def test_family_name(self):
        from modules.documents import _family_name
        self.assertEqual(_family_name("Aaron Dave Canonigo"), "Canonigo")
        self.assertEqual(_family_name("Juan Dela Cruz"), "Dela Cruz")
        self.assertEqual(_family_name("Maria De Los Santos"), "De Los Santos")
        self.assertEqual(_family_name("Ma. Kim Bolongaita"), "Bolongaita")
        self.assertEqual(_family_name("Cher"), "Cher")


if __name__ == "__main__":
    unittest.main()
