"""
payroll_period.py
Shared payroll-period math, used by both tardiness.py (the blueprint) and
tardiness_notify.py (the standalone daily cron script). Kept in its own
module so neither has to import from the other or from app.py.

This company's payroll runs on two fixed cutoffs per month:
  - 8th to 22nd of the same month
  - 23rd of one month to the 7th of the next month
"""

from datetime import date


def _add_months(year: int, month: int, delta: int):
    """Returns (year, month) shifted by delta months, handling year rollover."""
    total = (year * 12 + (month - 1)) + delta
    return total // 12, (total % 12) + 1


def get_payroll_period_for_date(d: date):
    """Returns (start, end) of the payroll period containing date d."""
    if 8 <= d.day <= 22:
        return d.replace(day=8), d.replace(day=22)
    elif d.day >= 23:
        next_year, next_month = _add_months(d.year, d.month, 1)
        return d.replace(day=23), date(next_year, next_month, 7)
    else:  # d.day <= 7
        prev_year, prev_month = _add_months(d.year, d.month, -1)
        return date(prev_year, prev_month, 23), d.replace(day=7)


def get_previous_period(start: date, end: date):
    """Given a period's (start, end), returns the period immediately before it."""
    if start.day == 8:
        prev_year, prev_month = _add_months(start.year, start.month, -1)
        new_start = date(prev_year, prev_month, 23)
        new_end = date(start.year, start.month, 7)
    else:  # start.day == 23
        new_start = start.replace(day=8)
        new_end = start.replace(day=22)
    return new_start, new_end


def get_next_period(start: date, end: date):
    """Given a period's (start, end), returns the period immediately after it."""
    if start.day == 8:
        next_year, next_month = _add_months(start.year, start.month, 1)
        new_start = start.replace(day=23)
        new_end = date(next_year, next_month, 7)
    else:  # start.day == 23
        next_year, next_month = _add_months(start.year, start.month, 1)
        new_start = date(next_year, next_month, 8)
        new_end = date(next_year, next_month, 22)
    return new_start, new_end


def get_default_payroll_period(today: date = None):
    """
    The default date-range to show: the most recently COMPLETED payroll
    period as of today (not the one currently in progress). E.g. if today
    is June 24 (inside the June 23-July 7 period, still ongoing), the
    default is June 8-22 (the period that just closed).
    """
    if today is None:
        today = date.today()
    current_start, current_end = get_payroll_period_for_date(today)
    return get_previous_period(current_start, current_end)


def get_current_payroll_period(today: date = None):
    """
    The payroll period CURRENTLY IN PROGRESS as of today (not yet closed).
    Used by the notification cron, since tardiness accumulates against the
    in-progress cycle, not the most recently completed one.
    """
    if today is None:
        today = date.today()
    return get_payroll_period_for_date(today)