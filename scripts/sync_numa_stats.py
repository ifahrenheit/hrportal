"""Refresh central_db.numa_stats from the NUMA Stat Dashboard sheet (cron).

    venv/bin/python scripts/sync_numa_stats.py           # dry run: read + parse, write nothing
    venv/bin/python scripts/sync_numa_stats.py --live    # replace numa_stats with the sheet's rows
    --force   load even if a tab shrank below half its stored rows (after checking the sheet)

Cron (root):
    5 * * * *  cd /var/www/html/leavesystem && venv/bin/python scripts/sync_numa_stats.py --live >> numa_stats_sync.log 2>&1

See helpers/numa_stats.py and docs/numa-stats.md.
"""
import os
import sys
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(ROOT, '.env'))

from db_core import get_db_connection  # noqa: E402
from helpers.numa_stats import sync  # noqa: E402


def main(argv):
    live = '--live' in argv
    force = '--force' in argv
    conn = get_db_connection()
    try:
        ok, lines = sync(conn, triggered_by='cron', live=live, force=force)
    finally:
        conn.close()
    stamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    print(f"[{stamp}] numa stats sync {'LIVE' if live else 'DRY RUN'}: {'ok' if ok else 'PROBLEM'}")
    for line in lines:
        print('   ' + line)
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
