"""
orangehrm_db.py
Connection helper for the orangehrm2 database — separate server credentials
from central_db (db_core.py). Leave records (ohrm_leave) and the employee
roster (hs_hr_employee) live here, not in central_db.
"""

import os
import pymysql


def get_orangehrm_connection():
    return pymysql.connect(
        host=os.environ.get("DB_HOST", "localhost"),
        port=int(os.environ.get("DB_PORT", 3306)),
        user=os.environ.get("DB_USER", "root"),
        password=os.environ.get("DB_PASSWORD"),
        database=os.environ.get("DB_NAME", "orangehrm2"),
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=False,
    )