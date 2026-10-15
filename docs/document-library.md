# Document Library

A searchable archive of scanned HR documents: memos, notices, policies, contracts,
forms and certificates. HR uploads the files; the portal keeps them private, pulls
the text out of PDFs, and lets people search by words inside the document as well
as by title, memo number, employee and notes.

## 1. Access & Navigation

| | |
|---|---|
| Routes | `GET /documents` (library and search), `GET /documents/<id>` (document page), `GET /documents/<id>/file` (the file itself; `?download=1` to download) |
| Manage routes | `POST /documents/upload`, `POST /documents/<id>/edit`, `POST /documents/<id>/delete` |
| Code | `modules/documents.py` (`documents_bp`, registered in `app.py` ~line 75) |
| Templates | `templates/documents/library.html`, `templates/documents/detail.html` |
| Entry point | Top nav → **Main** → **Document Library** (also a "Documents" tab in the Main sub-nav, and searchable with Ctrl K: "documents", "memos", "policies"). Shown only to people with access |
| Permission | `can_documents` ("Document Library" checkbox under People & HR on the Sub-Admins page), or admin, or `DOCUMENTS_EDITORS` |
| Tests | `tests/test_documents.py` |

### Who can do what

The library is hidden from everyone without access: no menu item, and every
`/documents` URL returns 404.

| Who | Opens the library | Sees "Everyone with library access" docs | Sees "HR only" docs | Upload / edit / remove |
|---|---|---|---|---|
| Employee without `can_documents` (even with `can_memos`) | No | No | No | No |
| Sub-admin with **Document Library** (`can_documents`) | Yes | Yes | No | No |
| Sub-admin with `can_documents` **and** **Employee Memos** (`can_memos`) | Yes | Yes | Yes | No |
| Admin (`is_admin`) | Yes | Yes | Yes | Yes |
| Anyone listed in `DOCUMENTS_EDITORS` (`.env`) | Yes | Yes | Yes | Yes |

`DOCUMENTS_EDITORS` is a comma-separated list of emails or employee IDs. Changing it
needs a service restart. A `can_documents` change takes effect at the person's next
login, like other sub-admin permissions.

Inside the library, an HR-only document is invisible to people who can't see it:
it doesn't appear in search, and its page and file URLs return 404.

## 2. Using it (for HR)

### Upload

1. **Document Library** → **Upload documents**.
2. Pick one or more files: **PDF, JPG or PNG**, up to **25 MB each** and **50 files**
   per upload.
3. Fill in the details. They apply to every file in that upload, so batch files that
   share a category, date or employee:
   - **Title**: only shown for a single file; with several files each takes its
     file name (underscores become spaces).
   - **Category**: Memo, Notice to Explain, Notice of Decision, Policy,
     Announcement, Contract / Agreement, Form / Template, Certificate, Other.
   - **Document date**, **Memo / reference no.**, **Employee ID** (when the
     document is about one person).
   - **Notes / keywords**: searchable. Use it for scans that have no readable text
     yet (see below).
   - **Who can see it**: **HR only** (default) or **Everyone with library access**.
4. **Upload**. A summary appears at the top of the page, including any skipped files
   and why.

Files are skipped when they aren't PDF/JPG/PNG, when the content doesn't match the
extension (e.g. a renamed file), when they're over 25 MB, or when the exact same file
is already in the library (its existing title is shown).

### Search

- Type any words into **Search**. Results must contain every word (prefix match, so
  `attend` finds "attendance"). Matches inside the document show a highlighted
  excerpt.
- The same box also matches title, memo / reference no., notes, employee ID and
  employee name.
- Narrow with **Category** and **Year** (from the document date).
- On a document page, **All documents for this employee** lists everything linked to
  that employee ID.

### Edit or remove

Click a document's **title** to open its page. Admins and editors see an **Edit
details** card on the right (title, category, date, reference no., employee,
notes, visibility) and a **Remove document** button at its bottom.

**Remove** is a soft delete: the document disappears from the library and search
immediately, but the row and file stay on the server so a mistake can be undone
(clear `deleted_at` on the row). Permanent erasure is a manual database and file
clean-up (see section 6).

### Scans without readable text ("No text yet")

JPG/PNG files, and PDFs that are only pictures of pages, contain no text the portal
can read. They're flagged **No text yet** and are found only by title, reference
no., employee and notes until an OCR pass fills in their text. Admins and editors see a
link above the results, "N scans without readable text", that lists them all.

### Preferred file format

- Best: a **searchable PDF** (scanner set to "PDF with OCR"), **300 dpi**,
  **grayscale**, **one document per file**. Multi-page memos stay in one file, and
  the text is searchable immediately.
- Mixed PDF and JPG is accepted. JPGs are one page per file and have no text, so
  convert them to searchable PDFs first when possible (NAPS2 or OCRmyPDF), merging
  the pages of each memo.
- Not accepted: HEIC (phone photos), TIFF, Word files.
- Helpful file naming for later bulk imports:
  `Memos\2024\2024-05-01_MEMO-024_Tardiness_Dela-Cruz.pdf`.

## 3. Storage

- Files: `uploads/hr_documents/<upload year>/<random id>.<pdf|jpg|png>` in the repo
  root. `uploads/` is gitignored and outside `static/`, so there is no public URL;
  files are served only through `/documents/<id>/file` after the visibility check,
  with `Cache-Control: private, no-store` and `X-Content-Type-Options: nosniff`.
- The original file name is kept in the database and used for downloads.
- Server capacity (Oct 2026): about 20 GB free disk; scanned pages are roughly
  100–300 KB each.

## 4. Database (central_db)

Created by `modules.documents.ensure_schema()` (`CREATE TABLE IF NOT EXISTS`, safe
to re-run).

### `hr_documents`

| Column | Notes |
|---|---|
| `id` | PK |
| `title`, `category`, `doc_date`, `reference_no`, `employee_id`, `description` | Metadata from the upload/edit form. `employee_id` matches `gsheet_employees.employee_id` (name shown via a join) |
| `visibility` | `hr` or `everyone` |
| `content_text` | Text extracted from the PDF (MEDIUMTEXT) |
| `text_status` | `extracted` (PDF had text), `needs_ocr` (image or image-only PDF), `ocr_done` (reserved for the OCR pass) |
| `original_name`, `stored_path`, `mime_type`, `file_size`, `page_count` | File facts; `stored_path` is relative to `uploads/hr_documents/` |
| `sha256` | Duplicate detection (indexed, not unique, so a removed file can be re-uploaded) |
| `uploaded_by`/`uploaded_at`, `updated_by`/`updated_at`, `deleted_by`/`deleted_at` | Audit; `deleted_at IS NOT NULL` = removed |

FULLTEXT index `ft_doc (title, reference_no, description, content_text)`.

### `hr_document_access_log`

One row per `upload`, `view`, `download`, `edit` and `delete`: `document_id`,
`user_email`, `action`, `at`. The 25 most recent entries show under **Recent
activity** on the document page (admins and editors only).

## 5. How it works

- **Text extraction**: `_extract_pdf_text()` uses `pypdf`, reading up to 300 pages.
  If the text averages under 40 non-space characters per page, the PDF is treated
  as a scan (`needs_ocr`), since a scanned page often carries only a stray header or
  stamp. Images are always `needs_ocr`. Extraction runs during the upload request;
  there's no OCR on the server (2 GB RAM droplet).
- **Search**: `_boolean_query()` turns the search into
  `MATCH … AGAINST ('+word1* +word2*' IN BOOLEAN MODE)`. Operators are stripped,
  and words under MySQL's 3-character minimum (`innodb_ft_min_token_size`) are
  dropped from the full-text part. This is OR'ed with `LIKE` on title, reference
  no., notes and employee name, and an exact match on employee ID. If no word is 3+
  characters, it falls back to `LIKE` on the document text. Results are ranked by
  full-text score, then document date. MySQL's default stopword list applies (e.g.
  "the", "about"); such words are ignored.
- **Snippets**: `_snippet()` cuts about 220 characters around the first matching word
  from the notes and text. The highlighting is done in the browser with text nodes,
  never HTML.
- **CSRF**: every POST carries `csrf_token` (`csrf.validate_csrf`).

## 6. Admin tasks

**Give someone read access**: Admin → **Sub-Admins** → tick **Document Library**
(`can_documents`) for that person; also tick **Employee Memos** if they should see
HR-only documents.

**Let HR staff upload**: add them to `.env`, then restart:

```bash
DOCUMENTS_EDITORS=hr.person@cohere.ph,another@cohere.ph
sudo systemctl restart leavesystem
```

**Undo a removal**:

```sql
UPDATE hr_documents SET deleted_at = NULL, deleted_by = NULL WHERE id = <id>;
```

**Permanently erase a document**: run the SELECT first, then delete the rows, then
delete the file at `uploads/hr_documents/<stored_path>`:

```sql
SELECT id, title, stored_path, deleted_at FROM hr_documents WHERE id = <id>;
DELETE FROM hr_document_access_log WHERE document_id = <id>;
DELETE FROM hr_documents WHERE id = <id>;
```

**Run the tests** (they create and remove `ZZTEST-doc` rows and files):

```bash
set -a; . ./.env; set +a
./venv/bin/python -m unittest tests.test_documents -v
```

## 7. Not built yet

- **OCR pass** for `needs_ocr` documents: run OCR off-server (OCRmyPDF/NAPS2 on an
  office PC) or in small overnight batches, write `content_text`, and set
  `text_status = 'ocr_done'`.
- **Bulk import** for the scanned archive on USB: a script that reads a folder
  tree, takes category, date, reference no. and title from folder and file names, and
  reuses the upload checks (type, size, duplicates).
- Showing an employee's linked documents on their PIM profile.
