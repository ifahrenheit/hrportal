#!/usr/bin/env python3
"""
rotate_smtp.py — rotate the send_email@cohere.ph SMTP password everywhere.

Reusable ops tool. Nothing hardcoded — pass the old and new passwords as args.

USAGE:
  1. Change the mailbox password in cPanel/WHM FIRST.
  2. Run:
       python3 rotate_smtp.py --old 'CurrentPassword' --new 'NewPassword'
     Add --dry-run to preview without writing.
  3. Restart:  sudo systemctl restart leavesystem
     (PHP crons + calendar-app pick up the change on their next run.)

Backs up every file first (.bak.TIMESTAMP) and verifies each replacement.
Safe to keep in the repo — it contains no passwords.
"""
import argparse, shutil, sys
from datetime import datetime

# Files that contain the SMTP password, with the surrounding literal pattern.
# {pw} is substituted with old/new. Precise anchors so nothing else is touched.
FILE_PATTERNS = [
    ('/var/www/html/leavesystem/.env',                     'SMTP_PASSWORD={pw}'),
    ('/var/www/html/calendar-app/.env',                    'MAIL_PASS={pw}'),
    ('/var/www/html/automation/test_absence_alert.php',    "$mail->Password = '{pw}';"),
    ('/var/www/html/automation/send_absence_report.php',   "$mail->Password = '{pw}';"),
]

def main():
    ap = argparse.ArgumentParser(description="Rotate SMTP password across all known files.")
    ap.add_argument('--old', required=True, help="Current SMTP password to replace")
    ap.add_argument('--new', required=True, help="New SMTP password")
    ap.add_argument('--dry-run', action='store_true', help="Preview only, do not write")
    args = ap.parse_args()

    if args.old == args.new:
        print("STOP: old and new passwords are identical.")
        sys.exit(1)

    ts = datetime.now().strftime('%Y%m%d%H%M%S')
    mode = "DRY RUN" if args.dry_run else "LIVE"
    print("SMTP rotation [" + mode + "] across " + str(len(FILE_PATTERNS)) + " files")
    print("-" * 55)

    all_ok = True
    for path, pattern in FILE_PATTERNS:
        old_literal = pattern.format(pw=args.old)
        new_literal = pattern.format(pw=args.new)
        try:
            with open(path) as f:
                c = f.read()
        except FileNotFoundError:
            print("MISSING: " + path)
            all_ok = False
            continue

        count = c.count(old_literal)
        if count == 0:
            print("NOT FOUND (skipped): " + path)
            all_ok = False
            continue

        if args.dry_run:
            print("WOULD replace " + str(count) + "x in: " + path)
            continue

        shutil.copy(path, path + '.bak.' + ts)
        c = c.replace(old_literal, new_literal)
        with open(path, 'w') as f:
            f.write(c)
        print("OK (" + str(count) + " replaced): " + path)

    print("-" * 55)
    if args.dry_run:
        print("Dry run complete. Re-run without --dry-run to apply.")
        return

    if all_ok:
        print("Done. Backups saved with suffix .bak." + ts)
        print("NEXT: sudo systemctl restart leavesystem")
        print("")
        print("Verifying old password is gone...")
        remaining = 0
        for path, pattern in FILE_PATTERNS:
            try:
                with open(path) as f:
                    if args.old in f.read():
                        print("  STILL PRESENT: " + path)
                        remaining += 1
            except FileNotFoundError:
                pass
        if remaining == 0:
            print("  Clean — old password no longer in any target file.")
    else:
        print("Some files skipped/missing — review above before restarting.")

if __name__ == '__main__':
    main()