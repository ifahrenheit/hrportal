import pymysql
from datetime import date
from dotenv import load_dotenv
import os

load_dotenv('/var/www/html/leavesystem/.env')

db = pymysql.connect(
    host=os.getenv('DB_HOST', 'localhost'),
    user=os.getenv('DB_USER'),
    password=os.getenv('DB_PASSWORD'),
    database=os.getenv('DB_NAME'),
    cursorclass=pymysql.cursors.DictCursor,
    autocommit=False
)

try:
    with db.cursor() as c:
        # Update leave4day_requests: scheduled → approved if date has passed
        c.execute("""
            UPDATE leave4day_requests
            SET status = 'approved'
            WHERE status = 'scheduled'
              AND leave_date < %s
        """, (date.today(),))

        # Update ohrm_leave: SCHEDULED(2) → TAKEN(3) if date has passed
        c.execute("""
            UPDATE ohrm_leave
            SET status = 3
            WHERE status = 2
              AND date < %s
        """, (date.today(),))

    db.commit()
    print(f"Status update complete: {date.today()}")
except Exception as e:
    db.rollback()
    print(f"Error: {e}")
finally:
    db.close()