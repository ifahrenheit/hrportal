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


def _jpg():
    buf = io.BytesIO()
    Image.new("RGB", (40, 40), "white").save(buf, "JPEG")
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


if __name__ == "__main__":
    unittest.main()
