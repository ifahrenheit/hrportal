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

Access (anyone else gets a 404 and no menu item):
  open the library:            admins, DOCUMENTS_EDITORS, sub-admins with can_documents
  manage (upload/edit/delete): admins + DOCUMENTS_EDITORS (.env, emails or
      employee IDs, comma-separated)
  'hr' documents:              managers + library users who also have can_memos
  'everyone' documents:        everyone who can open the library

Browsing (left-hand tree on /documents):
  Employees:        automatic, A-Z by family name -> employee -> their documents
                    (any document with an employee_id)
  Company folders:  hr_document_folders, created/renamed/deleted by managers;
                    a document sits in at most one (hr_documents.folder_id)
  Unfiled:          no employee and no folder
"""

import hashlib
import logging
import math
import os
import re
import unicodedata
import uuid
from datetime import datetime
from functools import wraps

import pymysql
from flask import (
    Blueprint, render_template, request, session, redirect,
    url_for, flash, abort, send_file, jsonify,
)

from csrf import validate_csrf
from db_core import get_db_connection

logger = logging.getLogger(__name__)

documents_bp = Blueprint("documents", __name__)

STORAGE_ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "uploads", "hr_documents")
MAX_FILE_BYTES = 25 * 1024 * 1024
MAX_FILES_PER_UPLOAD = 50
MAX_FOLDERS_PER_MATCH = 500
MAX_FOLDER_DEPTH = 6
# family-name particles kept with the last word: "Juan Dela Cruz" -> "Dela Cruz"
NAME_PARTICLES = {"de", "del", "dela", "delas", "delos", "san", "sta", "sto", "santa", "santo", "la", "los", "van", "von"}
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

# "Detect from file name" in the upload form; first match wins, else "Other"
AUTO_CATEGORY = "auto"
CATEGORY_HINTS = [
    ("Notice to Explain", re.compile(r"\bnte\b|notice to explain", re.I)),
    ("Notice of Decision", re.compile(r"\bnod\b|notice of decision|decision|dismiss|terminat", re.I)),
    ("Contract / Agreement", re.compile(r"contract|agreement|\bnda\b", re.I)),
    ("Certificate", re.compile(r"certificat|\bcoe\b", re.I)),
    ("Policy", re.compile(r"policy|policies|handbook", re.I)),
    ("Memo", re.compile(r"memo|warning|reprimand|suspension|commendation", re.I)),
    ("Form / Template", re.compile(r"\bform\b|template", re.I)),
]

VISIBILITY = {"hr": "HR only", "everyone": "Everyone with library access"}

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
    CREATE TABLE IF NOT EXISTS hr_document_folders (
        id INT AUTO_INCREMENT PRIMARY KEY,
        parent_id INT NULL,
        name VARCHAR(120) NOT NULL,
        created_by VARCHAR(255) NOT NULL,
        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        deleted_at DATETIME NULL,
        KEY idx_parent (parent_id)
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


def docs_can_access():
    if docs_can_manage():
        return True
    return bool((session.get("permissions") or {}).get("can_documents"))


def docs_can_see_hr():
    if docs_can_manage():
        return True
    return docs_can_access() and bool((session.get("permissions") or {}).get("can_memos"))


def _visible_levels():
    return ("hr", "everyone") if docs_can_see_hr() else ("everyone",)


def _library_access_required(f):
    @wraps(f)
    def wrapped(*args, **kwargs):
        if "user" not in session:
            return redirect("/")
        if not docs_can_access():
            abort(404)
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


def _back_to_library():
    """The library page the form was sent from (?folder=, ?employee_id=, ...), else the library root."""
    back = request.form.get("return_to", "")
    if back.startswith("/documents") and not back.startswith("//") and "\\" not in back:
        return redirect(back)
    return redirect(url_for("documents.library"))


def _get_doc(cur, doc_id):
    levels = _visible_levels()
    cur.execute(
        f"SELECT * FROM hr_documents WHERE id = %s AND deleted_at IS NULL "
        f"AND visibility IN ({','.join(['%s'] * len(levels))})",
        [doc_id, *levels],
    )
    return cur.fetchone()


def _guess_category(filename):
    for category, pattern in CATEGORY_HINTS:
        if pattern.search(filename):
            return category
    return "Other"


def _metadata_from_form(form, allow_auto=False):
    category = form.get("category", "").strip()
    if category not in CATEGORIES and not (allow_auto and category == AUTO_CATEGORY):
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


# ---------- folders ----------

def _family_name(full_name):
    words = (full_name or "").split()
    if not words:
        return ""
    family = [words[-1]]
    for w in reversed(words[:-1]):
        if w.lower().rstrip(".") in NAME_PARTICLES and len(words) - len(family) > 1:
            family.insert(0, w)
        else:
            break
    return " ".join(family)


def _sort_letter(family):
    flat = unicodedata.normalize("NFKD", family).encode("ascii", "ignore").decode().upper()
    return flat[:1] if flat[:1].isalpha() else "#"


def _load_folders(cur, levels):
    """All live folders with direct document counts, children lists and full paths."""
    marks = ",".join(["%s"] * len(levels))
    cur.execute(f"""
        SELECT f.id, f.parent_id, f.name,
               (SELECT COUNT(*) FROM hr_documents d
                 WHERE d.folder_id = f.id AND d.deleted_at IS NULL AND d.visibility IN ({marks})) AS n
        FROM hr_document_folders f
        WHERE f.deleted_at IS NULL
        ORDER BY f.name
    """, list(levels))
    folders = {r["id"]: dict(r, children=[]) for r in cur.fetchall()}
    roots = []
    for f in folders.values():
        parent = folders.get(f["parent_id"])
        (parent["children"] if parent else roots).append(f)
    for f in folders.values():
        path, node, seen = [], f, set()
        while node and node["id"] not in seen:
            seen.add(node["id"])
            path.insert(0, node)
            node = folders.get(node["parent_id"])
        f["path"] = path
        f["label"] = " / ".join(x["name"] for x in path)
    return folders, roots


def _folder_options(roots):
    """Flat, depth-first list for <select>s: [(id, label)]."""
    out = []

    def walk(nodes):
        for f in nodes:
            out.append((f["id"], f["label"]))
            walk(f["children"])
    walk(roots)
    return out


def _parse_folder_id(cur, raw):
    """A live folder id from a form value, or None."""
    try:
        fid = int(raw or 0)
    except (TypeError, ValueError):
        return None
    if fid <= 0:
        return None
    cur.execute("SELECT id FROM hr_document_folders WHERE id = %s AND deleted_at IS NULL", (fid,))
    return fid if cur.fetchone() else None


def _employee_folders(cur, levels):
    """{letter: [{employee_id, name, family, n}]} for employees that have documents."""
    marks = ",".join(["%s"] * len(levels))
    cur.execute(f"""
        SELECT d.employee_id, MAX(g.schedule_name) AS name, MAX(g.status) AS status, COUNT(*) AS n
        FROM hr_documents d
        LEFT JOIN gsheet_employees g ON g.employee_id = d.employee_id COLLATE utf8mb4_unicode_ci
        WHERE d.deleted_at IS NULL AND d.employee_id IS NOT NULL AND d.employee_id <> ''
          AND d.visibility IN ({marks})
        GROUP BY d.employee_id
    """, list(levels))
    letters = {}
    for r in cur.fetchall():
        name = r["name"] or r["employee_id"]
        family = _family_name(r["name"]) if r["name"] else r["employee_id"]
        given = name[:len(name) - len(family)].strip() if r["name"] else ""
        r.update(name=name, family=family, display=f"{family}, {given}" if given else family)
        letters.setdefault(_sort_letter(family), []).append(r)
    for rows in letters.values():
        rows.sort(key=lambda r: r["display"].lower())
    return dict(sorted(letters.items()))


# ---------- pages ----------

@documents_bp.route("/documents")
@_library_access_required
def library():
    q = request.args.get("q", "").strip()
    category = request.args.get("category", "").strip()
    year = request.args.get("year", "").strip()
    employee_id = request.args.get("employee_id", "").strip()
    text_status = request.args.get("text_status", "").strip()
    folder_arg = request.args.get("folder", "").strip()
    letter = request.args.get("letter", "").strip().upper()[:1]
    view = request.args.get("view", "").strip()
    if view not in ("employees", "folders", "unfiled"):
        view = ""
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
    folder_id = int(folder_arg) if folder_arg.isdigit() else None
    if folder_id:
        where.append("d.folder_id = %s")
        params.append(folder_id)
    if view == "unfiled":
        where.append("(d.employee_id IS NULL OR d.employee_id = '') AND d.folder_id IS NULL")
    # the Employees root and A-Z pages list employee folders, not files (unless searching)
    folders_only = bool(view in ("employees", "folders") or letter) and not q

    join = ("LEFT JOIN gsheet_employees g "
            "ON g.employee_id = d.employee_id COLLATE utf8mb4_unicode_ci")
    where_sql = " AND ".join(where)
    order = "score DESC, d.doc_date DESC, d.id DESC" if q else "d.doc_date IS NULL, d.doc_date DESC, d.id DESC"

    conn = get_db_connection()
    try:
        cur = conn.cursor(pymysql.cursors.DictCursor)
        folders, folder_roots = _load_folders(cur, levels)
        if folder_id and folder_id not in folders:
            abort(404)
        current_folder = folders.get(folder_id)
        emp_letters = _employee_folders(cur, levels)
        if letter and letter not in emp_letters:
            emp_letters.setdefault(letter, [])
        current_employee = None
        if employee_id:
            for rows in emp_letters.values():
                for r in rows:
                    if r["employee_id"] == employee_id:
                        current_employee = r
            if not current_employee:
                name = _employee_names(cur, [employee_id]).get(employee_id)
                family = _family_name(name) if name else employee_id
                current_employee = {"employee_id": employee_id, "name": name or employee_id, "family": family,
                                    "display": name or employee_id, "n": 0}
            current_employee["letter"] = _sort_letter(current_employee["family"])
        marks = ",".join(["%s"] * len(levels))
        cur.execute(f"SELECT COUNT(*) AS n FROM hr_documents WHERE deleted_at IS NULL AND visibility IN ({marks})",
                    list(levels))
        all_count = cur.fetchone()["n"]
        cur.execute(f"SELECT COUNT(*) AS n FROM hr_documents WHERE deleted_at IS NULL AND visibility IN ({marks}) "
                    f"AND (employee_id IS NULL OR employee_id = '') AND folder_id IS NULL", list(levels))
        unfiled_count = cur.fetchone()["n"]

        cur.execute(f"SELECT COUNT(*) AS n FROM hr_documents d {join} WHERE {where_sql}", params)
        total = 0 if folders_only else cur.fetchone()["n"]
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
        """, score_params + params + [0 if folders_only else PER_PAGE, (page - 1) * PER_PAGE])
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
        filters={"q": q, "category": category, "year": year, "employee_id": employee_id,
                 "text_status": text_status, "folder": folder_id or "", "letter": letter, "view": view},
        folders=folders, folder_roots=folder_roots, folder_options=_folder_options(folder_roots),
        current_folder=current_folder, emp_letters=emp_letters, letter=letter,
        current_employee=current_employee, view=view, folders_only=folders_only,
        all_count=all_count, unfiled_count=unfiled_count, max_folder_depth=MAX_FOLDER_DEPTH,
    )


@documents_bp.route("/documents/<int:doc_id>")
@_library_access_required
def detail(doc_id):
    conn = get_db_connection()
    try:
        cur = conn.cursor(pymysql.cursors.DictCursor)
        doc = _get_doc(cur, doc_id)
        if not doc:
            abort(404)
        doc["employee_name"] = _employee_names(cur, [doc["employee_id"]]).get(doc["employee_id"])
        folders, folder_roots = _load_folders(cur, _visible_levels())
        doc["folder"] = folders.get(doc["folder_id"])
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
        doc=doc, history=history, categories=CATEGORIES, folder_options=_folder_options(folder_roots),
        visibility_labels=VISIBILITY, can_manage=docs_can_manage(),
    )


@documents_bp.route("/documents/<int:doc_id>/file")
@_library_access_required
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
@_library_access_required
def upload():
    if not docs_can_manage():
        abort(403)
    # the folder uploader posts in small batches and wants JSON back
    wants_json = request.accept_mimetypes.best == "application/json"

    def refuse(msg, level):
        if wants_json:
            return jsonify(error=msg), 400
        flash(msg, level)
        return redirect(url_for("documents.library"))

    if not validate_csrf():
        return refuse("Your session expired. Please try the upload again.", "danger")

    files = [f for f in request.files.getlist("files") if f and f.filename]
    if not files:
        return refuse("Choose at least one file to upload.", "warning")
    if len(files) > MAX_FILES_PER_UPLOAD:
        return refuse(f"Upload at most {MAX_FILES_PER_UPLOAD} files at a time.", "warning")

    meta = _metadata_from_form(request.form, allow_auto=True)
    typed_title = request.form.get("title", "").strip()[:255]
    added, skipped = [], []

    conn = get_db_connection()
    try:
        cur = conn.cursor(pymysql.cursors.DictCursor)
        folder_id = _parse_folder_id(cur, request.form.get("folder_id"))
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
            category = _guess_category(name) if meta["category"] == AUTO_CATEGORY else meta["category"]
            try:
                cur.execute("""
                    INSERT INTO hr_documents
                        (title, category, doc_date, reference_no, employee_id, folder_id, visibility, description,
                         content_text, text_status, original_name, stored_path, mime_type, file_size,
                         page_count, sha256, uploaded_by)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """, (title[:255], category, meta["doc_date"], meta["reference_no"],
                      meta["employee_id"], folder_id, meta["visibility"], meta["description"],
                      text or None, status, name, rel, mime, len(data), pages, sha, _user_email()))
                _log(cur, cur.lastrowid, "upload")
                conn.commit()
                added.append((cur.lastrowid, title, status, category))
            except Exception:
                conn.rollback()
                os.remove(full)
                logger.exception("Document insert failed for %s", name)
                skipped.append(f"{name} (could not be saved)")
    finally:
        conn.close()

    if wants_json:
        return jsonify(
            added=[{"id": i, "title": t, "status": s, "category": c} for i, t, s, c in added],
            skipped=skipped,
        )

    if added:
        ocr = sum(1 for _, _, s, _ in added if s == "needs_ocr")
        msg = f"Uploaded {len(added)} document{'s' if len(added) != 1 else ''}."
        if ocr:
            msg += (f" {ocr} {'is a scan' if ocr == 1 else 'are scans'} without readable text, "
                    "so only the title, reference no., employee and notes are searchable until OCR runs.")
        flash(msg, "success")
    if skipped:
        flash("Skipped: " + "; ".join(skipped), "warning")
    if len(added) == 1 and not skipped:
        return redirect(url_for("documents.detail", doc_id=added[0][0]))
    return _back_to_library()


def _name_tokens(text):
    flat = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode().lower()
    return [t for t in re.findall(r"[a-z]+", flat) if len(t) > 1]


@documents_bp.route("/documents/match-folders", methods=["POST"])
@_library_access_required
def match_folders():
    """Suggest employees for folder names like "Canonigo, Aaron" or "CANONIGO".

    A name matches when every word of the folder appears in the employee's
    name; an employee ID in the folder name (220510-04) is matched exactly.
    If nothing matches fully, employees sharing any longer word are offered.
    """
    if not docs_can_manage():
        abort(403)
    if not validate_csrf():
        return jsonify(error="Your session expired. Reload the page and try again."), 400
    folders = (request.get_json(silent=True) or {}).get("folders") or []
    folders = [str(f)[:255] for f in folders][:MAX_FOLDERS_PER_MATCH]

    conn = get_db_connection()
    try:
        cur = conn.cursor(pymysql.cursors.DictCursor)
        cur.execute("SELECT employee_id, schedule_name, status FROM gsheet_employees "
                    "WHERE employee_id IS NOT NULL AND employee_id <> ''")
        people = [(r, set(_name_tokens(r["schedule_name"]))) for r in cur.fetchall()]
    finally:
        conn.close()

    def card(r):
        return {"employee_id": r["employee_id"], "name": r["schedule_name"] or r["employee_id"],
                "status": r["status"] or ""}

    matches = {}
    for folder in folders:
        ids = set(re.findall(r"\d{6}-\d{2}", folder))
        if ids:
            found = [card(r) for r, _ in people if r["employee_id"] in ids]
            matches[folder] = {"exact": found, "maybe": []}
            continue
        words = set(_name_tokens(folder))
        exact = [card(r) for r, toks in people if words and words <= toks]
        maybe = []
        if not exact:
            long_words = {w for w in words if len(w) >= 4}
            maybe = [card(r) for r, toks in people if long_words & toks]
        sort_key = lambda c: (c["status"] != "Active", c["name"].lower())
        matches[folder] = {"exact": sorted(exact, key=sort_key)[:10],
                           "maybe": sorted(maybe, key=sort_key)[:10]}
    return jsonify(matches=matches)


def _clean_folder_name(raw):
    name = re.sub(r"\s+", " ", (raw or "").replace("/", "-")).strip()[:120]
    return name


def _name_taken(cur, parent_id, name, exclude_id=None):
    cur.execute(
        "SELECT id FROM hr_document_folders WHERE deleted_at IS NULL AND name = %s "
        "AND parent_id <=> %s AND id <> %s LIMIT 1",
        (name, parent_id, exclude_id or 0),
    )
    return cur.fetchone() is not None


@documents_bp.route("/documents/folders", methods=["POST"])
@_library_access_required
def create_folder():
    if not docs_can_manage():
        abort(403)
    if not validate_csrf():
        flash("Your session expired. Please try again.", "danger")
        return _back_to_library()
    name = _clean_folder_name(request.form.get("name"))
    if not name:
        flash("Give the folder a name.", "warning")
        return _back_to_library()

    conn = get_db_connection()
    try:
        cur = conn.cursor(pymysql.cursors.DictCursor)
        parent_id = _parse_folder_id(cur, request.form.get("parent_id"))
        if parent_id:
            folders, _ = _load_folders(cur, _visible_levels())
            if len(folders[parent_id]["path"]) >= MAX_FOLDER_DEPTH:
                flash(f"Folders can be nested at most {MAX_FOLDER_DEPTH} levels deep.", "warning")
                return _back_to_library()
        if _name_taken(cur, parent_id, name):
            flash(f'There is already a folder called "{name}" here.', "warning")
            return _back_to_library()
        cur.execute("INSERT INTO hr_document_folders (parent_id, name, created_by) VALUES (%s, %s, %s)",
                    (parent_id, name, _user_email()))
        new_id = cur.lastrowid
        conn.commit()
    finally:
        conn.close()
    flash(f'Folder "{name}" created.', "success")
    return redirect(url_for("documents.library", folder=new_id))


@documents_bp.route("/documents/folders/<int:folder_id>/rename", methods=["POST"])
@_library_access_required
def rename_folder(folder_id):
    if not docs_can_manage():
        abort(403)
    back = redirect(url_for("documents.library", folder=folder_id))
    if not validate_csrf():
        flash("Your session expired. Please try again.", "danger")
        return back
    name = _clean_folder_name(request.form.get("name"))
    if not name:
        flash("Give the folder a name.", "warning")
        return back

    conn = get_db_connection()
    try:
        cur = conn.cursor(pymysql.cursors.DictCursor)
        cur.execute("SELECT parent_id FROM hr_document_folders WHERE id = %s AND deleted_at IS NULL", (folder_id,))
        row = cur.fetchone()
        if not row:
            abort(404)
        if _name_taken(cur, row["parent_id"], name, exclude_id=folder_id):
            flash(f'There is already a folder called "{name}" here.', "warning")
            return back
        cur.execute("UPDATE hr_document_folders SET name = %s WHERE id = %s", (name, folder_id))
        conn.commit()
    finally:
        conn.close()
    flash("Folder renamed.", "success")
    return back


@documents_bp.route("/documents/folders/<int:folder_id>/delete", methods=["POST"])
@_library_access_required
def delete_folder(folder_id):
    """Only empty folders can be deleted, so nothing is ever lost or hidden by it."""
    if not docs_can_manage():
        abort(403)
    if not validate_csrf():
        flash("Your session expired. Please try again.", "danger")
        return redirect(url_for("documents.library", folder=folder_id))

    conn = get_db_connection()
    try:
        cur = conn.cursor(pymysql.cursors.DictCursor)
        cur.execute("SELECT parent_id, name FROM hr_document_folders WHERE id = %s AND deleted_at IS NULL",
                    (folder_id,))
        row = cur.fetchone()
        if not row:
            abort(404)
        # counts every document, HR-only included, whoever is looking
        cur.execute("SELECT COUNT(*) AS n FROM hr_documents WHERE folder_id = %s AND deleted_at IS NULL", (folder_id,))
        docs_in = cur.fetchone()["n"]
        cur.execute("SELECT COUNT(*) AS n FROM hr_document_folders WHERE parent_id = %s AND deleted_at IS NULL",
                    (folder_id,))
        subs = cur.fetchone()["n"]
        if docs_in or subs:
            flash("Only empty folders can be deleted. Move its documents and sub-folders out first.", "warning")
            return redirect(url_for("documents.library", folder=folder_id))
        cur.execute("UPDATE hr_document_folders SET deleted_at = NOW() WHERE id = %s", (folder_id,))
        conn.commit()
    finally:
        conn.close()
    flash(f'Folder "{row["name"]}" deleted.', "success")
    if row["parent_id"]:
        return redirect(url_for("documents.library", folder=row["parent_id"]))
    return redirect(url_for("documents.library"))


MAX_MOVE = 500


@documents_bp.route("/documents/move", methods=["POST"])
@_library_access_required
def move_documents():
    """Bulk move: set or clear the company folder and/or the employee link of ticked documents.

    folder:   "keep" (default), "" for no folder, or a folder id
    employee: "keep" (default), "clear", or "set" with employee_id (must exist in gsheet_employees)
    """
    if not docs_can_manage():
        abort(403)
    if not validate_csrf():
        flash("Your session expired. Please try again.", "danger")
        return _back_to_library()
    ids = sorted({int(x) for x in request.form.getlist("doc_ids") if str(x).isdigit()})[:MAX_MOVE]
    if not ids:
        flash("Tick at least one document to move.", "warning")
        return _back_to_library()
    folder_choice = request.form.get("folder", "keep")
    emp_choice = request.form.get("employee", "keep")
    if folder_choice == "keep" and emp_choice not in ("clear", "set"):
        flash("Choose a folder or an employee to move the documents to.", "warning")
        return _back_to_library()

    conn = get_db_connection()
    try:
        cur = conn.cursor(pymysql.cursors.DictCursor)
        sets, params, where_to = [], [], []
        if folder_choice != "keep":
            folder_id = _parse_folder_id(cur, folder_choice) if folder_choice else None
            if folder_choice and not folder_id:
                flash("That folder no longer exists.", "warning")
                return _back_to_library()
            sets.append("folder_id = %s")
            params.append(folder_id)
            where_to.append(f'folder "{_load_folders(cur, _visible_levels())[0][folder_id]["label"]}"'
                            if folder_id else "no company folder")
        if emp_choice == "clear":
            sets.append("employee_id = NULL")
            where_to.append("no employee")
        elif emp_choice == "set":
            emp_id = request.form.get("employee_id", "").strip()[:50]
            name = _employee_names(cur, [emp_id]).get(emp_id) if emp_id else None
            if not name:
                flash(f'No employee with ID "{emp_id}". Nothing was moved.', "warning")
                return _back_to_library()
            sets.append("employee_id = %s")
            params.append(emp_id)
            where_to.append(f"{name} ({emp_id})")

        marks = ",".join(["%s"] * len(ids))
        cur.execute(f"""
            UPDATE hr_documents SET {", ".join(sets)}, updated_by = %s
             WHERE id IN ({marks}) AND deleted_at IS NULL
        """, params + [_user_email()] + ids)
        cur.execute(f"SELECT id FROM hr_documents WHERE id IN ({marks}) AND deleted_at IS NULL", ids)
        moved = [r["id"] for r in cur.fetchall()]
        for doc_id in moved:
            _log(cur, doc_id, "edit")
        conn.commit()
    finally:
        conn.close()
    flash(f"Moved {len(moved)} document{'s' if len(moved) != 1 else ''} to " + " and ".join(where_to) + ".", "success")
    return _back_to_library()


@documents_bp.route("/documents/<int:doc_id>/edit", methods=["POST"])
@_library_access_required
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
        folder_id = _parse_folder_id(cur, request.form.get("folder_id"))
        cur.execute("""
            UPDATE hr_documents
               SET title=%s, category=%s, doc_date=%s, reference_no=%s, employee_id=%s, folder_id=%s,
                   visibility=%s, description=%s, updated_by=%s
             WHERE id=%s AND deleted_at IS NULL
        """, (title, meta["category"], meta["doc_date"], meta["reference_no"], meta["employee_id"], folder_id,
              meta["visibility"], meta["description"], _user_email(), doc_id))
        _log(cur, doc_id, "edit")
        conn.commit()
    finally:
        conn.close()
    flash("Document details saved.", "success")
    return redirect(url_for("documents.detail", doc_id=doc_id))


@documents_bp.route("/documents/<int:doc_id>/delete", methods=["POST"])
@_library_access_required
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


@documents_bp.app_context_processor
def _inject_access():
    return {"docs_can_access": docs_can_access}


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
        cur.execute("SELECT COUNT(*) AS n FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = DATABASE() "
                    "AND TABLE_NAME = 'hr_documents' AND COLUMN_NAME = 'folder_id'")
        if not cur.fetchone()["n"]:
            cur.execute("ALTER TABLE hr_documents ADD COLUMN folder_id INT NULL AFTER employee_id, "
                        "ADD KEY idx_folder (folder_id)")
        conn.commit()
    finally:
        conn.close()
