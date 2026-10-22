"""
permission.py
Centralized permission definitions + helpers, extracted gradually from app.py.

This module is the single source of truth for:
  - what permissions exist (PERMISSION_DEFINITIONS / ALL_PERMISSIONS)
  - parsing a submitted sub-admin form into a perms dict
  - building the INSERT/UPDATE SQL + values for leave4day_sub_admins
  - converting a DB row into a session-ready perms dict
  - the SELECT column list for the GET query on the sub-admins page
  - the route-level @require_permission decorator

IMPORTANT: ALL_PERMISSIONS order doesn't need to match the DB column order
exactly, since build_upsert_sql() always builds the column list and the
placeholders from the same list, in the same order, every time. That's the
whole point — once a permission is added here, the INSERT/UPDATE/SELECT
all pick it up automatically without manual placeholder counting.
"""

# ---------------------------------------------------------------------------
# NOTE: app.py already has a `permission_required` decorator (line ~217)
# that does the same job as a has_permission()/require_permission() pair
# would. To avoid two parallel patterns, this module intentionally does NOT
# define its own decorator — blueprints should import permission_required
# directly from app.py instead:
#
#     from app import permission_required
#
#     @some_bp.route("/")
#     @permission_required("can_tardiness")
#     def some_view():
#         ...
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Single source of truth: (db_column, lucide_icon, display_label) -- all 40
# columns of leave4day_sub_admins, grouped as on the Sub-Admins page.
# This list also drives the sub-admin permissions page UI (sub_admins.html
# should eventually import PERMISSION_DEFINITIONS instead of hardcoding its
# own copy of this list — see migration notes at the bottom of this file).
# ---------------------------------------------------------------------------
PERMISSION_DEFINITIONS = [
    # ── Leave & Requests
    ('can_all_leaves'            , 'users'             , 'All Leaves'),
    ('can_all_requests'          , 'inbox'             , 'All Requests'),
    ('can_approve'               , 'circle-check'      , 'Approve / Reject'),
    ('can_file_for_emp'          , 'user-plus'         , 'File for Employee'),
    ('can_file_requests'         , 'file-text'         , 'File Requests'),
    ('can_view_tickets'          , 'ticket'            , 'View OT/RDW Tickets'),
    ('can_team_calendar'         , 'calendar'          , 'Leave Calendar (all leavers)'),
    ('can_sl_verification'       , 'stethoscope'       , 'SL Verification (HR)'),
    # ── People & HR
    ('can_pim'                   , 'scan-face'         , 'Employee Database (PIM)'),
    ('can_requirements'          , 'clipboard-check'   , 'Requirements'),
    ('can_coaching'              , 'presentation'      , 'Coaching'),
    ('can_coaching_reports'      , 'chart-line'        , 'Coaching Reports'),
    ('can_memos'                 , 'file-text'         , 'Employee Memos'),
    ('can_documents'             , 'library'           , 'Document Library'),
    ('can_work_mode'             , 'users'             , 'Work Mode'),
    ('can_onboarding'            , 'user-check'        , 'Onboarding / Offboarding'),
    ('can_wfm'                   , 'cloud-upload'      , 'WFM Onboarding'),
    ('can_schedules'             , 'table'             , 'Schedules'),
    ('can_settings'              , 'settings'          , 'Settings'),
    # ── Analytics & Reports
    ('can_reports'               , 'chart-column'      , 'Reports'),
    ('can_absences'              , 'user-x'            , 'Absence Report'),
    ('can_tardiness'             , 'clock-arrow-up'    , 'Tardiness Report'),
    ('can_attrition_report'      , 'user-minus'        , 'Attrition Report'),
    ('can_night_differential'    , 'moon-star'         , 'Night Differential'),
    ('can_holiday_awol'          , 'calendar-x'        , 'Holiday AWOL'),
    ('can_overbreak'             , 'cup-soda'          , 'Overbreak Report'),
    ('can_qa_updates'            , 'megaphone'         , 'QA Updates'),
    ('can_ot_hours'              , 'timer'             , 'OT Hours Report'),
    ('can_timestamp_records'     , 'fingerprint'       , 'Timestamp Records'),
    # ── Finance & Operations
    ('can_entitlements'          , 'credit-card'       , 'Entitlements'),
    ('can_material_requests'     , 'package'           , 'Material Requests'),
    ('can_final_approval'        , 'badge-check'       , 'Final Approval'),
    ('can_view_approved_items'   , 'package-check'     , 'Purchaser View (Materials)'),
    ('can_facilities_review'     , 'wrench'            , 'Facilities Reviewer'),
    ('can_facilities_final'      , 'badge-check'       , 'Facilities Final Approver'),
    ('can_inventory'             , 'boxes'             , 'Inventory Management'),
    # ── Tools & Access
    ('can_csat'                  , 'heart'             , 'CSAT Dashboard'),
    ('can_surveys'               , 'clipboard-list'    , 'Survey Results'),
    ('can_floor_map'             , 'map-pin'           , 'Floor Map'),
    ('can_it_tickets'            , 'headset'           , 'IT Ticket Queue'),
    ('can_hr_tickets'            , 'id-card'           , 'HR Ticket Queue'),
    ('can_mailing_lists'         , 'mails'             , 'Mailing Lists'),
    ('can_mailing_lists_view'    , 'eye'               , 'Mailing Lists (view only)'),
]

ALL_PERMISSIONS = [key for key, _, _ in PERMISSION_DEFINITIONS]


# ---------------------------------------------------------------------------
# Form parsing (replaces the inline perms dicts in app.py's sub-admin
# save handlers — both the per-employee edit AND the bulk-group-assign
# action should use this same function, so they can never drift apart)
# ---------------------------------------------------------------------------

def parse_permissions_form(form) -> dict:
    """
    Build the perms dict from a submitted form (request.form), using
    ALL_PERMISSIONS as the canonical key list. Any checkbox not present
    in the form is treated as unchecked (0).
    """
    return {key: (1 if form.get(key) else 0) for key in ALL_PERMISSIONS}


# ---------------------------------------------------------------------------
# DB row -> session perms dict (replaces the inline block around app.py:342)
# ---------------------------------------------------------------------------

def perms_from_db_row(row) -> dict:
    """
    Convert a DB row (dict-like, from a dictionary cursor) into the
    session-ready perms dict. Missing/NULL columns default to False.
    """
    return {key: bool(row.get(key, 0)) for key in ALL_PERMISSIONS}


# ---------------------------------------------------------------------------
# SQL helpers for the sub-admin INSERT/UPDATE (leave4day_sub_admins)
# ---------------------------------------------------------------------------

def build_upsert_sql(table: str = "leave4day_sub_admins") -> str:
    """
    Builds the INSERT ... ON DUPLICATE KEY UPDATE statement for the given
    table, using ALL_PERMISSIONS for the column list. Column order, the
    VALUES placeholder count, and the UPDATE clause are always derived from
    the same list, so they can never drift out of sync with each other.

    Expects the table to have: emp_number, <every column in ALL_PERMISSIONS>,
    and assigned_by.
    """
    cols = ALL_PERMISSIONS
    col_list = ", ".join(cols)
    # +2 placeholders: emp_number (start) and assigned_by (end)
    placeholders = ", ".join(["%s"] * (len(cols) + 2))
    update_clause = ", ".join(f"{c}=%s" for c in cols) + ", assigned_by=%s"

    return f"""
        INSERT INTO {table} (emp_number, {col_list}, assigned_by)
        VALUES ({placeholders})
        ON DUPLICATE KEY UPDATE {update_clause}
    """


def build_upsert_values(emp_number, perms: dict, assigned_by) -> tuple:
    """
    Builds the full values tuple matching build_upsert_sql()'s placeholder
    order: [emp_number, <perm values for INSERT>, assigned_by,
            <perm values for UPDATE>, assigned_by]
    """
    perm_values = [perms[key] for key in ALL_PERMISSIONS]
    insert_part = [emp_number] + perm_values + [assigned_by]
    update_part = perm_values + [assigned_by]
    return tuple(insert_part + update_part)


# ---------------------------------------------------------------------------
# SELECT column list helper (replaces the inline sa.can_x, sa.can_y, ...
# list around app.py:1994)
# ---------------------------------------------------------------------------

def select_columns(prefix: str = "sa") -> str:
    """
    Returns a comma-separated 'prefix.col' list for every permission,
    suitable for dropping into a SELECT statement.

    Example: select_columns("sa") ->
        "sa.can_all_leaves, sa.can_all_requests, ..., sa.can_hr_tickets"
    """
    return ", ".join(f"{prefix}.{key}" for key in ALL_PERMISSIONS)


# ---------------------------------------------------------------------------
# Migration notes (delete this section once fully migrated):
#
# 1. sub_admins.html currently has its own hardcoded copy of the
#    (key, icon, label) tuple list. Once the route rendering that template
#    passes PERMISSION_DEFINITIONS into the template context, remove the
#    hardcoded list from the template and reference the passed-in variable
#    instead (e.g. {% for key, icon, label in permission_definitions %}).
#
# 2. The line ~342 inline dict (`'can_absences': bool(row.get(...))`, etc.)
#    can be replaced with: perms_from_db_row(row)
#
# 3. The line ~1860 inline dict (`'can_absences': 1 if request.form.get(...)`)
#    can be replaced with: parse_permissions_form(request.form)
#
# 4. The INSERT/UPDATE block (~1882-1905) can be replaced with:
#        sql = build_upsert_sql()
#        values = build_upsert_values(emp_number, perms, session['user']['emp_number'])
#        c.execute(sql, values)
#
# 5. The SELECT at ~1994 can replace its long sa.can_x, sa.can_y, ... list
#    with: {select_columns()}
# ---------------------------------------------------------------------------