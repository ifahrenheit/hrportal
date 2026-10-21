# Dashboard redesign preview (on hold)

Design exploration from Oct 2026, based on the Lovable project "HR Portal Refresh".
Nothing here is used by the live portal. All data is made up.

- `topnav_on_real_dashboard.html`: static page. Lovable's top-nav look applied to the
  real `/dashboard` sections (two-row header, breadcrumb strip with the sub-page tabs,
  white cards with icon chips, "Coming up"). Open it directly in a browser.
- `app.py` + `templates/`: small standalone Flask app (port 5057, no database, no login)
  with a sidebar / top-nav toggle. Run `venv/bin/python lovable_preview/app.py`.

When picking this up again:
- The real nav has many more items than the mockups; keep `nav/_topnav.html` groups and
  `has_permission()` checks.
- Implementing means restyling `templates/nav/_topnav.html` plus colour tokens in
  `base.html` / `dashboard.html`. The header is shared, so every page changes; do it on a branch.
- Lovable's company-wide stats (headcount, attendance chart) have no query behind them,
  and `/dashboard` is personal. The live "on shift" pill (`/api/current-headcount`) already exists.
