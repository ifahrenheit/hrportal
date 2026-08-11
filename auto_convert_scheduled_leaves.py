import os
#!/usr/bin/env python3
"""
Daily cron: converts past 'scheduled' leaves to 'approved' in leave4day_requests,
and syncs ohrm_leave.status from 2 (Scheduled) to 3 (Taken).
"""
import pymysql
from datetime import datetime
from dotenv import load_dotenv
load_dotenv()

DB = dict(host='localhost', user='root', password=os.environ.get('DB_PASSWORD'),
          database='orangehrm2', cursorclass=pymysql.cursors.DictCursor)

def main():
    conn = pymysql.connect(**DB)
    try:
        with conn.cursor() as c:
            c.execute("""
                UPDATE leave4day_requests SET status = 'approved'
                WHERE status = 'scheduled' AND leave_date <= CURDATE()
            """)
            r1 = c.rowcount
            c.execute("""
                UPDATE ohrm_leave ol
                JOIN leave4day_requests r
                    ON r.emp_number = ol.emp_number
                   AND r.leave_date = ol.date
                   AND r.leave_type_id = ol.leave_type_id
                SET ol.status = 3
                WHERE ol.status = 2 AND ol.date <= CURDATE() AND r.status = 'approved'
            """)
            r2 = c.rowcount
        conn.commit()
        print(f"[{datetime.now()}] Converted {r1} leave4day_requests, {r2} ohrm_leave records.")
    except Exception as e:
        print(f"[{datetime.now()}] ERROR: {e}")
    finally:
        conn.close()

if __name__ == '__main__':
    main()
