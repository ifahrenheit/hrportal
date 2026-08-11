import os
#!/var/www/html/leavesystem/venv/bin/python3
import mysql.connector
from datetime import datetime
from dotenv import load_dotenv
load_dotenv()

DB_CONFIG = dict(host='localhost', user='root', password=os.environ.get('DB_PASSWORD'), database='orangehrm2')

def sync_emails():
    conn = mysql.connector.connect(**DB_CONFIG)
    try:
        with conn.cursor() as c:
            c.execute("""
                UPDATE orangehrm2.hs_hr_employee e
                JOIN central_db.gsheet_employees g 
                    ON g.employee_id COLLATE utf8mb4_unicode_ci = e.employee_id
                SET e.emp_work_email = g.email
                WHERE (e.emp_work_email IS NULL OR e.emp_work_email = '')
                AND g.status != 'Separated'
                AND g.email IS NOT NULL AND g.email != ''
            """)
            updated = c.rowcount
            conn.commit()
            print(f"[{datetime.now()}] Updated {updated} OrangeHRM email(s).")
    finally:
        conn.close()

if __name__ == '__main__':
    sync_emails()
