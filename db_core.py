import pymysql  # <--- THIS IS THE MISSING LINE
import os

def get_db_connection():
    return pymysql.connect(
        host=os.environ.get('MAIN_DB_HOST', 'localhost'),
        port=int(os.environ.get('MAIN_DB_PORT', 3306)),
        user=os.environ.get('MAIN_DB_USER', 'employee_sync'),
        password=os.environ.get('MAIN_DB_PASSWORD'),
        database=os.environ.get('MAIN_DB_NAME', 'central_db'),
        charset='utf8mb4',
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=False
    )