"""
Document Library blueprint: scanned memos, policies and other HR documents,
searchable by title, notes and the text inside PDFs.

- Files live in uploads/hr_documents/<year>/ (outside static/, gitignored) and
  are only served through /documents/<id>/file, which checks access.
- Metadata + extracted text in central_db.hr_documents (FULLTEXT index),
  every view/download in central_db.hr_document_access_log.
- Text comes from the PDF's own text layer (pypdf). Image files and image-only
  PDFs are marked text_status='needs_ocr' until an OCR pass fills them in;
  until then they are found by title, reference no., employee and notes.

Access:
  manage (upload/edit/delete): admins + DOCUMENTS_EDITORS (.env, emails or
      employee IDs, comma-separated)
  'hr' documents:              managers + sub-admins with can_memos
  'everyone' documents:        any logged-in user
"""

import hashlib
import logging
import math
import os
import re
import uuid
from datetime import datetime
from functools import wraps

import pymysql
from flask import (
    Blueprint, render_template, request, session, redirect,
    url_for, flash, abort, send_file,
)

from csrf import validate_csrf
from db_core import get_db_connection

logger = logging.getLogger(__name__)

documents_bp = Blueprint("documents", __name__)

STORAGE_ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "uploads", "hr_documents")
MAX_FILE_BYTES = 25 * 1024 * 1024
MAX_FILES_PER_UPLOAD = 50
PER_PAGE = 30
MAX_PDF_PAGES = 300

CATEGORIES = [
    "Memo",
    "Notice to Explain",
    "Notice of Decision",
    "Policy",
    "Announcement",
    "Contract / Agreement",
    "Form / Template",
    "Certificate",
    "Other",
]

VISIBILITY = {"hr": "HR only", "everyone": "All employees"}

# extension -> (mime type, magic-byte prefixes)
ALLOWED = {
    "pdf":  ("application/pdf", (b"%PDF",)),
    "jpg":  ("image/jpeg", (b"\xff\xd8\xff",)),
    "jpeg": ("image/jpeg", (b"\xff\xd8\xff",)),
    "png":  ("image/png", (b"\x89PNG\r\n\x1a\n",)),
}

SCHEMA = [
    """
    CREATE TABLE IF NOT EXISTS hr_documents (
        id INT AUTO_INCREMENT PRIMARY KEY,
        title VARCHAR(255) NOT NULL,
        category VARCHAR(64) NOT NULL,
        doc_date DATE NULL,
        reference_no VARCHAR(64) NULL,
        employee_id VARCHAR(50) NULL,
        visibility ENUM('hr','everyone') NOT NULL DEFAULT 'hr',
        description TEXT NULL,
        content_text MEDIUMTEXT NULL,
        text_status ENUM('extracted','needs_ocr','ocr_done') NOT NULL DEFAULT 'needs_ocr',
        original_name VARCHAR(255) NOT NULL,
        stored_path VARCHAR(255) NOT NULL,
        mime_type VARCHAR(64) NOT NULL,
        file_size INT NOT NULL,
        page_count INT NULL,
        sha256 CHAR(64) NOT NULL,
        uploaded_by VARCHAR(255) NOT NULL,
        uploaded_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_by VARCHAR(255) NULL,
        updated_at DATETIME NULL ON UPDATE CURRENT_TIMESTAMP,
        deleted_at DATETIME NULL,
        deleted_by VARCHAR(255) NULL,
        KEY idx_category (category),
        KEY idx_doc_date (doc_date),
        KEY idx_employee (employee_id),
        KEY idx_sha (sha256),
        FULLTEXT KEY ft_doc (title, reference_no, description, content_text)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS hr_document_access_log (
        id BIGINT AUTO_INCREMENT PRIMARY KEY,
        document_id INT NOT NULL,
        user_email VARCHAR(255) NOT NULL,
        action ENUM('view','download','upload','edit','delete') NOT NULL,
        at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        KEY idx_doc (document_id),
        KEY idx_user (user_email)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
]


# ---------- access ----------

def _user_email():
    return str(session.get("user", {}).get("email", "")).lower()


def _editors():
    raw = os.getenv("DOCUMENTS_EDITORS", "")
    return {x.strip().lower() for x in raw.split(",") if x.strip()}


def docs_can_manage():
    if session.get("is_admin"):
        return True
    u = session.get("user", {})
    ident = {str(u.get("email", "")).lower(), str(u.get("employee_id", "")).lower()}
    return bool(ident & _editors())


def docs_can_see_hr():
    if docs_can_manage():
        return True
    return bool((session.get("permissions") or {}).get("can_memos"))


def _visible_levels():
    return ("hr", "everyone") if docs_can_see_hr() else ("everyone",)


def _page_login_required(f):
    @wraps(f)
    def wrapped(*args, **kwargs):
        if "user" not in session:
            return redirect("/")
        return f(*args, **kwargs)
    return wrapped


# ---------- helpers ----------

def _log(cur, doc_id, action):
    cur.execute(
        "INSERT INTO hr_document_access_log (document_id, user_email, action) VALUES (%s, %s, %s)",
        (doc_id, _user_email() or "unknown", action),
    )


def _parse_date(raw):
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date()
    except ValueError:
        return None


def _boolean_query(q):
    """'leave policy 2024' -> '+leave* +policy* +2024*'. Operators stripped;
    words under the 3-char full-text minimum are left to the LIKE fallback."""
    words = re.findall(r"[\w]+", q, flags=re.UNICODE)
    words = [w for w in words if len(w) >= 3]
    return " ".join(f"+{w}*" for w in words)


def _extract_pdf_text(path):
    """(text, page_count). Text is '' for scanned/image-only PDFs."""
    try:
        from pypdf import PdfReader
        reader = PdfReader(path)
        pages = len(reader.pages)
        parts = []
        for i, page in enumerate(reader.pages):
            if i >= MAX_PDF_PAGES:
                break
            try:
                parts.append(page.extract_text() or "")
            except Exception:
                parts.append("")
        return "\n".join(parts).strip(), pages
    except Exception as e:
        logger.warning("Document text extraction failed for %s: %s", path, e)
        return "", None


def _has_real_text(text, pages):
    # a scanned page often carries a stray header/stamp; ask for ~40 chars/page
    return len(re.sub(r"\s+", "", text)) >= 40 * max(pages or 1, 1)


def _snippet(text, q, width=220):
    if not text:
        return ""
    flat = re.sub(r"\s+", " ", text)
    pos = -1
    for w in re.findall(r"\w+", q or ""):
        pos = flat.lower().find(w.lower())
        if pos >= 0:
            break
    if pos < 0:
        return flat[:width] + ("…" if len(flat) > width else "")
    start = max(pos - width // 3, 0)
    out = flat[start:start + width]
    return ("…" if start else "") + out + ("…" if start + width < len(flat) else "")


def _employee_names(cur, ids):
    ids = [i for i in {i for i in ids if i}]
    if not ids:
        return {}
    cur.execute(
        f"SELECT employee_id, schedule_name FROM gsheet_employees WHERE employee_id IN ({','.join(['%s'] * len(ids))})",
        ids,
    )
    return {r["employee_id"]: r["schedule_name"] for r in cur.fetchall()}


def _get_doc(cur, doc_id):
    levels = _visible_levels()
    cur.execute(
        f"SELECT * FROM hr_documents WHERE id = %s AND deleted_at IS NULL "
        f"AND visibility IN ({','.join(['%s'] * len(levels))})",
        [doc_id, *levels],
    )
    return cur.fetchone()


def _metadata_from_form(form):
    category = form.get("category", "").strip()
    if category not in CATEGORIES:
        category = "Other"
    visibility = form.get("visibility", "hr")
    if visibility not in VISIBILITY:
        visibility = "hr"
    return {
        "category": category,
        "visibility": visibility,
        "doc_date": _parse_date(form.get("doc_date")),
        "reference_no": form.get("reference_no", "").strip()[:64] or None,
        "employee_id": form.get("employee_id", "").strip()[:50] or None,
        "description": form.get("description", "").strip() or None,
    }


# ---------- pages ----------

@documents_bp.route("/documents")
@_page_login_required
def library():
    q = request.args.get("q", "").strip()
    category = request.args.get("category", "").strip()
    year = request.args.get("year", "").strip()
    employee_id = request.args.get("employee_id", "").strip()
    text_status = request.args.get("text_status", "").strip()
    try:
        page = max(int(request.args.get("page", 1) or 1), 1)
    except ValueError:
        page = 1

    levels = _visible_levels()
    where = ["d.deleted_at IS NULL", f"d.visibility IN ({','.join(['%s'] * len(levels))})"]
    params = list(levels)
    score_sql, score_params = "0", []

    if q:
        bq = _boolean_query(q)
        like = f"%{q}%"
        conds = ["d.title LIKE %s", "d.reference_no LIKE %s", "d.employee_id = %s",
                 "d.description LIKE %s", "g.schedule_name LIKE %s"]
        cparams = [like, like, q, like, like]
        if bq:
            conds.insert(0, "MATCH(d.title, d.reference_no, d.description, d.content_text) AGAINST (%s IN BOOLEAN MODE)")
            cparams.insert(0, bq)
            score_sql = "MATCH(d.title, d.reference_no, d.description, d.content_text) AGAINST (%s IN BOOLEAN MODE)"
            score_params = [bq]
        else:
            conds.append("d.content_text LIKE %s")
            cparams.append(like)
        where.append("(" + " OR ".join(conds) + ")")
        params.extend(cparams)
    if category in CATEGORIES:
        where.append("d.category = %s")
        params.append(category)
    if year.isdigit():
        where.append("YEAR(d.doc_date) = %s")
        params.append(int(year))
    if employee_id:
        where.append("d.employee_id = %s")
        params.append(employee_id)
    if text_status == "needs_ocr" and docs_can_manage():
        where.append("d.text_status = 'needs_ocr'")

    join = ("LEFT JOIN gsheet_employees g "
            "ON g.employee_id = d.employee_id COLLATE utf8mb4_unicode_ci")
    where_sql = " AND ".join(where)
    order = "score DESC, d.doc_date DESC, d.id DESC" if q else "d.doc_date IS NULL, d.doc_date DESC, d.id DESC"

    conn = get_db_connection()
    try:
        cur = conn.cursor(pymysql.cursors.DictCursor)
        cur.execute(f"SELECT COUNT(*) AS n FROM hr_documents d {join} WHERE {where_sql}", params)
        total = cur.fetchone()["n"]
        total_pages = max(math.ceil(total / PER_PAGE), 1)
        page = min(page, total_pages)

        cur.execute(f"""
            SELECT d.id, d.title, d.category, d.doc_date, d.reference_no, d.employee_id,
                   d.visibility, d.mime_type, d.file_size, d.page_count, d.text_status,
                   d.uploaded_at, g.schedule_name AS employee_name,
                   LEFT(CONCAT_WS(' ', d.description, d.content_text), 200000) AS body,
                   {score_sql} AS score
            FROM hr_documents d {join}
            WHERE {where_sql}
            ORDER BY {order}
            LIMIT %s OFFSET %s
        """, score_params + params + [PER_PAGE, (page - 1) * PER_PAGE])
        docs = cur.fetchall()
        for d in docs:
            d["snippet"] = _snippet(d.pop("body"), q) if q else ""

        cur.execute(
            f"SELECT DISTINCT YEAR(doc_date) AS y FROM hr_documents "
            f"WHERE deleted_at IS NULL AND doc_date IS NOT NULL "
            f"AND visibility IN ({','.join(['%s'] * len(levels))}) ORDER BY y DESC",
            list(levels),
        )
        years = [r["y"] for r in cur.fetchall()]

        needs_ocr = 0
        if docs_can_manage():
            cur.execute("SELECT COUNT(*) AS n FROM hr_documents WHERE deleted_at IS NULL AND text_status = 'needs_ocr'")
            needs_ocr = cur.fetchone()["n"]
    finally:
        conn.close()

    return render_template(
        "documents/library.html",
        docs=docs, total=total, page=page, total_pages=total_pages,
        years=years, categories=CATEGORIES, visibility_labels=VISIBILITY,
        can_manage=docs_can_manage(), needs_ocr=needs_ocr,
        filters={"q": q, "category": category, "year": year,
                 "employee_id": employee_id, "text_status": text_status},
    )


@documents_bp.route("/documents/<int:doc_id>")
@_page_login_required
def detail(doc_id):
    conn = get_db_connection()
    try:
        cur = conn.cursor(pymysql.cursors.DictCursor)
        doc = _get_doc(cur, doc_id)
        if not doc:
            abort(404)
        doc["employee_name"] = _employee_names(cur, [doc["employee_id"]]).get(doc["employee_id"])
        history = []
        if docs_can_manage():
            cur.execute(
                "SELECT user_email, action, at FROM hr_document_access_log "
                "WHERE document_id = %s ORDER BY at DESC LIMIT 25",
                (doc_id,),
            )
            history = cur.fetchall()
    finally:
        conn.close()
    return render_template(
        "documents/detail.html",
        doc=doc, history=history, categories=CATEGORIES,
        visibility_labels=VISIBILITY, can_manage=docs_can_manage(),
    )


@documents_bp.route("/documents/<int:doc_id>/file")
@_page_login_required
def serve_file(doc_id):
    download = request.args.get("download") == "1"
    conn = get_db_connection()
    try:
        cur = conn.cursor(pymysql.cursors.DictCursor)
        doc = _get_doc(cur, doc_id)
        if not doc:
            abort(404)
        path = os.path.realpath(os.path.join(STORAGE_ROOT, doc["stored_path"]))
        if not path.startswith(os.path.realpath(STORAGE_ROOT) + os.sep) or not os.path.isfile(path):
            logger.error("Document %s file missing: %s", doc_id, doc["stored_path"])
            abort(404)
        _log(cur, doc_id, "download" if download else "view")
        conn.commit()
    finally:
        conn.close()

    resp = send_file(path, mimetype=doc["mime_type"], as_attachment=download,
                     download_name=doc["original_name"], max_age=0)
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Cache-Control"] = "private, no-store"
    return resp


# ---------- manage ----------

@documents_bp.route("/documents/upload", methods=["POST"])
@_page_login_required
def upload():
    if not docs_can_manage():
        abort(403)
    if not validate_csrf():
        flash("Your session expired. Please try the upload again.", "danger")
        return redirect(url_for("documents.library"))

    files = [f for f in request.files.getlist("files") if f and f.filename]
    if not files:
        flash("Choose at least one file to upload.", "warning")
        return redirect(url_for("documents.library"))
    if len(files) > MAX_FILES_PER_UPLOAD:
        flash(f"Upload at most {MAX_FILES_PER_UPLOAD} files at a time.", "warning")
        return redirect(url_for("documents.library"))

    meta = _metadata_from_form(request.form)
    typed_title = request.form.get("title", "").strip()[:255]
    added, skipped = [], []

    conn = get_db_connection()
    try:
        cur = conn.cursor(pymysql.cursors.DictCursor)
        for f in files:
            name = os.path.basename(f.filename.replace("\\", "/"))[:255]
            ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
            if ext not in ALLOWED:
                skipped.append(f"{name} (only PDF, JPG and PNG are accepted)")
                continue
            data = f.read(MAX_FILE_BYTES + 1)
            if len(data) > MAX_FILE_BYTES:
                skipped.append(f"{name} (over {MAX_FILE_BYTES // (1024 * 1024)} MB)")
                continue
            mime, magics = ALLOWED[ext]
            if not any(data.startswith(m) for m in magics):
                skipped.append(f"{name} (not a real {ext.upper()} file)")
                continue

            sha = hashlib.sha256(data).hexdigest()
            cur.execute(
                "SELECT id, title FROM hr_documents WHERE sha256 = %s AND deleted_at IS NULL LIMIT 1",
                (sha,),
            )
            dup = cur.fetchone()
            if dup:
                skipped.append(f"{name} (already in the library as \"{dup['title']}\")")
                continue

            rel = os.path.join(str(datetime.now().year), f"{uuid.uuid4().hex}.{ 'jpg' if ext == 'jpeg' else ext }")
            full = os.path.join(STORAGE_ROOT, rel)
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "wb") as out:
                out.write(data)

            text, pages = "", 1
            if ext == "pdf":
                text, pages = _extract_pdf_text(full)
            status = "extracted" if text and _has_real_text(text, pages) else "needs_ocr"

            title = typed_title if (typed_title and len(files) == 1) else os.path.splitext(name)[0].replace("_", " ").strip()
            try:
                cur.execute("""
                    INSERT INTO hr_documents
                        (title, category, doc_date, reference_no, employee_id, visibility, description,
                         content_text, text_status, original_name, stored_path, mime_type, file_size,
                         page_count, sha256, uploaded_by)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """, (title[:255], meta["category"], meta["doc_date"], meta["reference_no"],
                      meta["employee_id"], meta["visibility"], meta["description"],
                      text or None, status, name, rel, mime, len(data), pages, sha, _user_email()))
                _log(cur, cur.lastrowid, "upload")
                conn.commit()
                added.append((cur.lastrowid, title, status))
            except Exception:
                conn.rollback()
                os.remove(full)
                logger.exception("Document insert failed for %s", name)
                skipped.append(f"{name} (could not be saved)")
    finally:
        conn.close()

    if added:
        ocr = sum(1 for _, _, s in added if s == "needs_ocr")
        msg = f"Uploaded {len(added)} document{'s' if len(added) != 1 else ''}."
        if ocr:
            msg += (f" {ocr} {'is a scan' if ocr == 1 else 'are scans'} without readable text, "
                    "so only the title, reference no., employee and notes are searchable until OCR runs.")
        flash(msg, "success")
    if skipped:
        flash("Skipped: " + "; ".join(skipped), "warning")
    if len(added) == 1 and not skipped:
        return redirect(url_for("documents.detail", doc_id=added[0][0]))
    return redirect(url_for("documents.library"))


@documents_bp.route("/documents/<int:doc_id>/edit", methods=["POST"])
@_page_login_required
def edit(doc_id):
    if not docs_can_manage():
        abort(403)
    if not validate_csrf():
        flash("Your session expired. Please try again.", "danger")
        return redirect(url_for("documents.detail", doc_id=doc_id))

    title = request.form.get("title", "").strip()[:255]
    if not title:
        flash("Title can't be empty.", "warning")
        return redirect(url_for("documents.detail", doc_id=doc_id))
    meta = _metadata_from_form(request.form)

    conn = get_db_connection()
    try:
        cur = conn.cursor(pymysql.cursors.DictCursor)
        if not _get_doc(cur, doc_id):
            abort(404)
        cur.execute("""
            UPDATE hr_documents
               SET title=%s, category=%s, doc_date=%s, reference_no=%s, employee_id=%s,
                   visibility=%s, description=%s, updated_by=%s
             WHERE id=%s AND deleted_at IS NULL
        """, (title, meta["category"], meta["doc_date"], meta["reference_no"], meta["employee_id"],
              meta["visibility"], meta["description"], _user_email(), doc_id))
        _log(cur, doc_id, "edit")
        conn.commit()
    finally:
        conn.close()
    flash("Document details saved.", "success")
    return redirect(url_for("documents.detail", doc_id=doc_id))


@documents_bp.route("/documents/<int:doc_id>/delete", methods=["POST"])
@_page_login_required
def delete(doc_id):
    """Soft delete: the row and file stay so a mistake can be undone in SQL."""
    if not docs_can_manage():
        abort(403)
    if not validate_csrf():
        flash("Your session expired. Please try again.", "danger")
        return redirect(url_for("documents.detail", doc_id=doc_id))

    conn = get_db_connection()
    try:
        cur = conn.cursor(pymysql.cursors.DictCursor)
        if not _get_doc(cur, doc_id):
            abort(404)
        cur.execute(
            "UPDATE hr_documents SET deleted_at = NOW(), deleted_by = %s WHERE id = %s AND deleted_at IS NULL",
            (_user_email(), doc_id),
        )
        _log(cur, doc_id, "delete")
        conn.commit()
    finally:
        conn.close()
    flash("Document removed from the library.", "success")
    return redirect(url_for("documents.library"))


@documents_bp.app_template_filter("filesize")
def _filesize(n):
    n = n or 0
    if n < 1024 * 1024:
        return f"{max(n // 1024, 1)} KB"
    return f"{n / (1024 * 1024):.1f} MB"


def ensure_schema():
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        for stmt in SCHEMA:
            cur.execute(stmt)
        conn.commit()
    finally:
        conn.close()
