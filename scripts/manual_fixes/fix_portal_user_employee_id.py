import ast
import shutil
from datetime import datetime

APP_PATH = "/var/www/html/leavesystem/app.py"

# --- Backup ---
ts = datetime.now().strftime("%Y%m%d_%H%M%S")
backup_path = f"{APP_PATH}.bak_{ts}"
shutil.copy2(APP_PATH, backup_path)
print(f"Backup created: {backup_path}")

with open(APP_PATH, "r") as f:
    content = f.read()

OLD = """        session['user'] = {
            'email':      email,
            'emp_number': portal_user['emp_number'],
            'name':       f"{portal_user['firstname']} {portal_user['lastname']}",
            'employee_id': ''
        }"""

NEW = """        session['user'] = {
            'email':      email,
            'emp_number': portal_user['emp_number'],
            'name':       f"{portal_user['firstname']} {portal_user['lastname']}",
            'employee_id': gs_emp['employee_id']
        }"""

if OLD not in content:
    print("ERROR: anchor string not found. No changes made.")
    print("The file may have already been patched, or whitespace/formatting differs.")
    raise SystemExit(1)

occurrences = content.count(OLD)
if occurrences != 1:
    print(f"ERROR: expected exactly 1 occurrence of anchor, found {occurrences}. No changes made.")
    raise SystemExit(1)

new_content = content.replace(OLD, NEW)

# --- Syntax validate before writing ---
try:
    ast.parse(new_content)
except SyntaxError as e:
    print(f"ERROR: patched content has a syntax error: {e}")
    raise SystemExit(1)

with open(APP_PATH, "w") as f:
    f.write(new_content)

print("Patch applied successfully.")
print("Changed: 'employee_id': '' -> 'employee_id': gs_emp['employee_id']")
