"""
leave_policy.py
VL / SL entitlement rules from the company leave policy (sections 3.1-3.3), shared by
scripts/year_end_rollover.py (Jan 1) and scripts/prorated_leave_grants.py (during the year).

- VL starts at the 1st anniversary of deployment (gsheet_employees.operational_date + 1 year).
- SL starts at regularization; there's no regularization column, so it's estimated as
  deployment + REG_MONTHS months (matches 2026 data) unless real dates are supplied.
- In the year it starts, leave is pro-rated at 1 day per month to December. If the start date
  is on or before the 15th, that month's day is credited in the same month; after the 15th,
  crediting starts the next month.
- After that year: 12 VL and 12 SL every Jan 1. Carry-Over = min(unused VL, 6).
"""
from datetime import date

FULL_VL = FULL_SL = 12
CO_CAP = 6
LWOP_DAYS = 99
REG_MONTHS = 6


def add_months(d, n):
    y, m = divmod(d.month - 1 + n, 12)
    y, m = d.year + y, m + 1
    for day in (d.day, 30, 29, 28):
        try:
            return date(y, m, day)
        except ValueError:
            continue


def vl_start(deployed):
    return add_months(deployed, 12)


def sl_start(deployed, regularized=None):
    return regularized or add_months(deployed, REG_MONTHS)


def credit_date(start):
    """When the first pro-rated credit is due: the start date if it's on or before the 15th,
    else the 1st of the next month."""
    if start.day <= 15:
        return start
    return date(start.year + (start.month == 12), start.month % 12 + 1, 1)


def prorated_days(start):
    """Days credited for the year leave starts in: 1 per month from the credit month to December
    (0 when the credit falls in the next year, e.g. a start after Dec 15)."""
    cd = credit_date(start)
    return 12 - cd.month + 1 if cd.year == start.year else 0
