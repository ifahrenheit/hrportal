# 2025 Leave Entitlement Reconciliation

*Prepared 2026-10-14. This report is read-only: nothing in the database was changed. The queries ran in a read-only MySQL session.*

## Summary

In 90 of the 842 active 2025 rows in `ohrm_leave_entitlement`, `days_used` doesn't match the 2025 leave that was actually taken or is pending.

| | Rows | Days |
|---|---:|---:|
| 2025 entitlement rows checked | 842 | |
| Correct | 752 | |
| **Under-counted** (days_used too low) | **80** | **215.75** missing |
| **Over-counted** (days_used too high) | **10** | **28.875** extra |
| Total days_used now | | 2181.72 |
| Total days_used if corrected | | 2368.59 |

8 of the 90 rows belong to employees who have since been separated.

## Cause

Both kinds of error come from the same bug in the portal, which was fixed on 2026-10-14 (finding #3 in the system audit). When leave was rejected, cancelled, or filed by an admin, the code updated *every* year's entitlement row for that leave type, not only the row for the leave date's year. So 2026 activity changed the 2025 rows.

- **Under-counted (2026 rejections and cancellations).** Each rejected or cancelled 2026 leave also gave the days back to the employee's 2025 row. 78 of the 80 under-counted rows have 2026 rejections or cancellations for the same leave type. The code never let `days_used` go below 0, so the error on a row can be smaller than its 2026 refunds.
- **Over-counted (2026 leave filed by an admin).** "File for Employee" added the days to both the 2025 and 2026 rows. All 10 over-counted rows have 2026 leave filed by an admin (no approver name, auto-approved). In most rows the extra days match those amounts exactly.

## How "should be" was calculated

OrangeHRM links each leave day to the entitlement row it was charged to, in `ohrm_leave_leave_entitlement`. For each 2025 entitlement row:

> **Should be** = sum of `length_days` in `ohrm_leave_leave_entitlement` for linked leave that is still Pending (1), Scheduled (2) or Taken (3). Rejected (-1) and cancelled (0) leave is not counted.

Checks on the source data:
- Every active 2025 leave in `ohrm_leave` is linked to an entitlement row (0 unlinked).
- No 2025 portal leave (`leave4day_requests`) is missing from `ohrm_leave` (0 found).
- No row's corrected total is more than its entitlement.
- No 2025 rows exist in the portal's own `leave4day_entitlements` table, so only `ohrm_leave_entitlement` is affected.

## Over-counted rows (10)

These employees are shown as having used more 2025 leave than they did.

| # | Employee | Employee ID | Leave type | Entitled | days_used now | Should be | Difference | Separated |
|---:|---|---|---|---:|---:|---:|---:|---|
| 1 | Abrao, Abegail | 220525-01 | Vacation Leave | 12 | 13 | 12 | -1 |  |
| 2 | Aluben, Wilkent | 220827-10 | Sick Leave | 12 | 2.5 | 0 | -2.5 | Yes |
| 3 | Baynos, Ana Marie | 240401-10 | Parental leave | 15 | 26.25 | 15 | -11.25 | Yes |
| 4 | Becari, Jareth Lou | 210701-01 | Carry-Over Leave | 2 | 3 | 2 | -1 |  |
| 5 | Borgonia, Lee Anne | 210705-03 | Vacation Leave | 12 | 12 | 10 | -2 |  |
| 6 | Calumpang, Jonathan | 220801-01 | Vacation Leave | 12 | 12.5 | 10 | -2.5 |  |
| 7 | Jumao-as, Kemberly Rose | 230227-06 | Leave Without Pay (LWOP) | 99 | 19.125 | 18.5 | -0.625 |  |
| 8 | Maning, Joevien Rey | 230411-24 | Vacation Leave | 12 | 11 | 8 | -3 |  |
| 9 | Oliveros, Camille | 250303-08 | Magic Leave | 1 | 1.25 | 0 | -1.25 |  |
| 10 | Sumalinog, Reysen | 240212-24 | Vacation Leave | 10 | 7.75 | 4 | -3.75 |  |

**Note on Baynos, Ana Marie (Parental leave):** `days_used` is 26.25, but the entitlement is 15 and the linked leave is 15. Please confirm with HR before correcting this row.

## Under-counted rows (80)

These employees are shown as having used less 2025 leave than they did. If any payroll calculation or leave conversion read these balances, it would have treated these days as unused.

| # | Employee | Employee ID | Leave type | Entitled | days_used now | Should be | Difference | 2026 rejected/cancelled days | Separated |
|---:|---|---|---|---:|---:|---:|---:|---:|---|
| 1 | Abubakar, Meradz | 230411-01 | Carry-Over Leave | 6 | 1.25 | 5 | +3.75 | 3.75 |  |
| 2 | Adan, Kate Julia | 220502-01 | Vacation Leave | 12 | 0.75 | 2 | +1.25 | 1.25 |  |
| 3 | Alcoseba, Ana Marie | 210705-01 | Carry-Over Leave | 4 | 1.5 | 4 | +2.5 | 2.5 |  |
| 4 | Aluben, Wilkent | 220827-10 | Vacation Leave | 12 | 0 | 7 | +7 | 11.25 | Yes |
| 5 | Armodia, Jean-Bee | 231128-03 | Vacation Leave | 12 | 3.5 | 6 | +2.5 | 2.5 |  |
| 6 | Asugao, Cricy | 230206-03 | Carry-Over Leave | 5 | 2.5 | 5 | +2.5 | 2.5 |  |
| 7 | Asugao, Cricy | 230206-03 | Sick Leave | 12 | 7.75 | 9 | +1.25 | 1.25 |  |
| 8 | Baga, Jully Ann | 220404-01 | Vacation Leave | 12 | 3.5 | 6 | +2.5 | 2.5 |  |
| 9 | Barcastigue, Jessa | 230306-37 | Carry-Over Leave | 3 | 0 | 3 | +3 | 10 |  |
| 10 | Batoctoy, Christian | 240401-09 | Magic Leave | 1 | 0 | 1 | +1 | 13.5 | Yes |
| 11 | Batoctoy, Christian | 240401-09 | Vacation Leave | 8 | 0 | 4 | +4 | 6.25 | Yes |
| 12 | Beronga, Alixa May | 240520-02 | Magic Leave | 3 | 1.75 | 3 | +1.25 | 1.25 |  |
| 13 | Beronga, Alixa May | 240520-02 | Sick Leave | 12 | 1.875 | 5 | +3.125 | 3.125 |  |
| 14 | Besabella, Carl Michael | 240520-03 | Vacation Leave | 7 | 0 | 2 | +2 | 6.25 | Yes |
| 15 | Bolongaita, Ma. Kim | 210528-02 | Vacation Leave | 12 | 3 | 4 | +1 | 1 |  |
| 16 | Bontilao, Shanen Clair | 220827-04 | Carry-Over Leave | 6 | 3.5 | 6 | +2.5 | 2.5 |  |
| 17 | Bontilao, Shanen Clair | 220827-04 | Leave Without Pay (LWOP) | 99 | 0 | 1 | +1 | 0 |  |
| 18 | Borgonia, Lyra | 220502-03 | Vacation Leave | 12 | 3.5 | 6 | +2.5 | 2.5 |  |
| 19 | Bucasas, Dorothea | 220606-08 | Sick Leave | 12 | 10.75 | 12 | +1.25 | 1.25 |  |
| 20 | Castillo, Honelyn | 220704-03 | Leave Without Pay (LWOP) | 99 | 4 | 9 | +5 | 5 |  |
| 21 | Comeda, Renalyn | 240411-01 | Vacation Leave | 9 | 0 | 5 | +5 | 7 |  |
| 22 | Comendador , Abby Marie | 231128-09 | Vacation Leave | 12 | 5.75 | 7 | +1.25 | 1.25 |  |
| 23 | Cuajao, Jayne | 220404-14 | Vacation Leave | 12 | 0 | 8 | +8 | 8.75 |  |
| 24 | Damaolao, James Paul | 230227-04 | Leave Without Pay (LWOP) | 99 | 0 | 1 | +1 | 1.25 |  |
| 25 | Damaolao, James Paul | 230227-04 | Vacation Leave | 12 | 7.75 | 9 | +1.25 | 1.25 |  |
| 26 | Dujali, Randolf | 250303-04 | Sick Leave | 3 | 1.75 | 3 | +1.25 | 1.25 |  |
| 27 | Estrobo-Almario, Vanessa | 210531-15 | Vacation Leave | 12 | 9.5 | 12 | +2.5 | 2.5 |  |
| 28 | Etang, Christy Ann | 240304-10 | Vacation Leave | 9 | 0 | 5 | +5 | 5 |  |
| 29 | Fedillaga, Thea | 220307-04 | Carry-Over Leave | 4 | 3 | 4 | +1 | 1 |  |
| 30 | Fedillaga, Thea | 220307-04 | Vacation Leave | 12 | 9 | 10 | +1 | 1 |  |
| 31 | Figuracion, Vincent Kevin | 210531-17 | Vacation Leave | 12 | 0 | 7 | +7 | 8.75 |  |
| 32 | Gabotero, Rizza | 220512-01 | Vacation Leave | 12 | 0.5 | 3 | +2.5 | 2.5 |  |
| 33 | Ignacio, Princess | 240301-01 | Vacation Leave | 10 | 2 | 7 | +5 | 5 |  |
| 34 | Impas, Viverly | 230306-18 | Sick Leave | 12 | 0 | 1 | +1 | 1.25 |  |
| 35 | Jackson, Frances Eula | 210531-19 | Vacation Leave | 12 | 5 | 7 | +2 | 2 |  |
| 36 | Jacob, Juneroy | 210528-04 | Vacation Leave | 12 | 7.5 | 10.5 | +3 | 3 |  |
| 37 | Jovero, Johnace Faith | 240411-02 | Vacation Leave | 9 | 1 | 7 | +6 | 3 |  |
| 38 | Jumao-as, Kemberly Rose | 230227-06 | Vacation Leave | 12 | 3.5 | 6 | +2.5 | 2.5 |  |
| 39 | Labad, Giovanni | 240429-11 | Magic Leave | 2 | 0 | 2 | +2 | 5 |  |
| 40 | Labrador, Kim Rose | 220307-07 | Sick Leave | 12 | 7.5 | 10 | +2.5 | 2.5 |  |
| 41 | Labrador, Kim Rose | 220307-07 | Vacation Leave | 12 | 0 | 9 | +9 | 11.25 |  |
| 42 | Lagoy, Roland | 230404-01 | Vacation Leave | 12 | 4.75 | 6 | +1.25 | 1.25 |  |
| 43 | Lungtad, Michelle | 240212-18 | Vacation Leave | 10 | 0.75 | 2 | +1.25 | 2.5 |  |
| 44 | Mag-asin, Ana Marie | 220606-18 | Carry-Over Leave | 5 | 0 | 4 | +4 | 5 |  |
| 45 | Mag-asin, Ana Marie | 220606-18 | Vacation Leave | 12 | 5.5 | 8 | +2.5 | 2.5 |  |
| 46 | Mahumas, Jenisel | 230306-21 | Vacation Leave | 12 | 0 | 4 | +4 | 5 |  |
| 47 | Mahusay, Jose Maria | 240401-13 | Sick Leave | 12 | 5.75 | 7 | +1.25 | 1.25 |  |
| 48 | Malinao, Florence May | 220307-08 | Carry-Over Leave | 2 | -1 | 1 | +2 | 0 |  |
| 49 | Malunjao, Luke Andrie | 240304-14 | Vacation Leave | 9 | 0.25 | 4 | +3.75 | 3.75 | Yes |
| 50 | Manto, Jhon Mike | 240304-27 | Sick Leave | 12 | 7.75 | 9 | +1.25 | 1.25 |  |
| 51 | Miro, Christiad | 250120-15 | Sick Leave | 5 | 2.75 | 4 | +1.25 | 1.25 |  |
| 52 | Padayogdog, Venz Romer | 220827-08 | Vacation Leave | 12 | 5.75 | 12 | +6.25 | 6.25 |  |
| 53 | Patual, Abegail | 210802-11 | Vacation Leave | 12 | 7.25 | 11 | +3.75 | 3.75 |  |
| 54 | Perez, Aileen | 230411-29 | Vacation Leave | 12 | 3.5 | 6 | +2.5 | 2.5 |  |
| 55 | Perez, Juvy | 220704-05 | Leave Without Pay (LWOP) | 99 | 0 | 1 | +1 | 1.25 |  |
| 56 | Pino, Jake Adriane | 210825-02 | Vacation Leave | 12 | 9 | 12 | +3 | 3 |  |
| 57 | Rubas, Melvin | 220425-01 | Vacation Leave | 12 | 1.75 | 8 | +6.25 | 6.25 |  |
| 58 | Sacedon, Kristine Joy | 230411-31 | Magic Leave | 1 | 0 | 1 | +1 | 1.25 |  |
| 59 | Sacedon, Kristine Joy | 230411-31 | Vacation Leave | 12 | 0 | 6 | +6 | 6.25 |  |
| 60 | Salabao, Francis Nikolle | 250210-14 | Leave Without Pay (LWOP) | 99 | 0.75 | 2 | +1.25 | 1.25 |  |
| 61 | Salabao-Alcibar, Linelle | 240401-24 | Magic Leave | 1 | 0 | 1 | +1 | 2.5 |  |
| 62 | Salazar, Jessamae | 220502-07 | Leave Without Pay (LWOP) | 99 | 2.75 | 4 | +1.25 | 1.25 |  |
| 63 | Salazar, Jessamae | 220502-07 | Vacation Leave | 12 | 0 | 5 | +5 | 6.25 |  |
| 64 | Sevilla, Glenda Marie | 240304-22 | Vacation Leave | 9 | 1.5 | 4 | +2.5 | 2.5 |  |
| 65 | Tabares, Jennille | 220827-09 | Carry-Over Leave | 6 | 2.25 | 6 | +3.75 | 3.75 |  |
| 66 | Tagaytayan, Jubeth | 230411-35 | Leave Without Pay (LWOP) | 99 | 3 | 6 | +3 | 3 |  |
| 67 | Tagupa, Shela Mie | 220606-31 | Carry-Over Leave | 5 | 0 | 2 | +2 | 7.5 |  |
| 68 | Tagupa, Shela Mie | 220606-31 | Vacation Leave | 12 | 0 | 1 | +1 | 1.25 |  |
| 69 | Tanza, Jasmin Shaine | 250825-09 | Leave Without Pay (LWOP) | 99 | 0.75 | 2 | +1.25 | 1.25 |  |
| 70 | Tejerero, Ivy Joe | 230411-38 | Carry-Over Leave | 6 | 3.75 | 5 | +1.25 | 1.25 |  |
| 71 | Tradio, Honey Mae | 230306-35 | Carry-Over Leave | 1 | 0 | 1 | +1 | 6.75 |  |
| 72 | Tradio, Honey Mae | 230306-35 | Vacation Leave | 12 | 2 | 5 | +3 | 3 |  |
| 73 | Velayo, Jake | 220307-16 | Vacation Leave | 12 | 1.25 | 5 | +3.75 | 3.75 | Yes |
| 74 | Villa, Divina Chris | 230923-01 | Leave Without Pay (LWOP) | 99 | 0 | 1 | +1 | 1.25 |  |
| 75 | Villa, Divina Chris | 230923-01 | Vacation Leave | 12 | 9.375 | 10 | +0.625 | 0.625 |  |
| 76 | Ybanez, Lisa | 230411-41 | Vacation Leave | 12 | 2.5 | 5 | +2.5 | 2.5 |  |
| 77 | Yonting, Ellaisa Mae | 240123-15 | Magic Leave | 3 | 1.75 | 3 | +1.25 | 1.25 |  |
| 78 | Yonting, Ellaisa Mae | 240123-15 | Sick Leave | 12 | 0 | 1 | +1 | 7.5 |  |
| 79 | Yonting, Ellaisa Mae | 240123-15 | Vacation Leave | 11 | 0 | 3 | +3 | 3.75 |  |
| 80 | Zozobrado, Jhon Michael | 230411-42 | Carry-Over Leave | 4 | 1.5 | 4 | +2.5 | 2.5 |  |

## Before applying any correction

1. **Decide whether 2025 should change at all.** If the 2025 balances were already closed out for payroll or carry-over, changing them now could disagree with what was paid. You may prefer to leave 2025 as it is and only record these differences.
2. **Check against payroll or HR records,** at least for the larger differences and the separated employees.
3. **2026 rows were not reviewed in this report.** Admin-filed 2026 leave was charged to the 2026 rows correctly, so the 2026 figures are expected to be accurate, but they haven't been checked.

## Appendix: correction SQL (NOT run)

This is for review only. Each statement changes a row only if its `days_used` still equals the value seen on 2026-10-14. If the value has changed since, the statement does nothing. Back up the table before running any of it.

```sql
UPDATE ohrm_leave_entitlement SET days_used = 4 WHERE id = 53 AND days_used = 2.75;  -- Salazar, Jessamae, Leave Without Pay (LWOP)
UPDATE ohrm_leave_entitlement SET days_used = 9 WHERE id = 67 AND days_used = 4;  -- Castillo, Honelyn, Leave Without Pay (LWOP)
UPDATE ohrm_leave_entitlement SET days_used = 1 WHERE id = 69 AND days_used = 0;  -- Perez, Juvy, Leave Without Pay (LWOP)
UPDATE ohrm_leave_entitlement SET days_used = 1 WHERE id = 75 AND days_used = 0;  -- Bontilao, Shanen Clair, Leave Without Pay (LWOP)
UPDATE ohrm_leave_entitlement SET days_used = 1 WHERE id = 96 AND days_used = 0;  -- Damaolao, James Paul, Leave Without Pay (LWOP)
UPDATE ohrm_leave_entitlement SET days_used = 18.5 WHERE id = 97 AND days_used = 19.125;  -- Jumao-as, Kemberly Rose, Leave Without Pay (LWOP)
UPDATE ohrm_leave_entitlement SET days_used = 6 WHERE id = 113 AND days_used = 3;  -- Tagaytayan, Jubeth, Leave Without Pay (LWOP)
UPDATE ohrm_leave_entitlement SET days_used = 1 WHERE id = 119 AND days_used = 0;  -- Villa, Divina Chris, Leave Without Pay (LWOP)
UPDATE ohrm_leave_entitlement SET days_used = 2 WHERE id = 223 AND days_used = 0.75;  -- Salabao, Francis Nikolle, Leave Without Pay (LWOP)
UPDATE ohrm_leave_entitlement SET days_used = 4 WHERE id = 238 AND days_used = 3;  -- Bolongaita, Ma. Kim, Vacation Leave
UPDATE ohrm_leave_entitlement SET days_used = 10.5 WHERE id = 239 AND days_used = 7.5;  -- Jacob, Juneroy, Vacation Leave
UPDATE ohrm_leave_entitlement SET days_used = 12 WHERE id = 249 AND days_used = 9.5;  -- Estrobo-Almario, Vanessa, Vacation Leave
UPDATE ohrm_leave_entitlement SET days_used = 7 WHERE id = 250 AND days_used = 0;  -- Figuracion, Vincent Kevin, Vacation Leave
UPDATE ohrm_leave_entitlement SET days_used = 7 WHERE id = 251 AND days_used = 5;  -- Jackson, Frances Eula, Vacation Leave
UPDATE ohrm_leave_entitlement SET days_used = 10 WHERE id = 254 AND days_used = 12;  -- Borgonia, Lee Anne, Vacation Leave
UPDATE ohrm_leave_entitlement SET days_used = 11 WHERE id = 257 AND days_used = 7.25;  -- Patual, Abegail, Vacation Leave
UPDATE ohrm_leave_entitlement SET days_used = 12 WHERE id = 261 AND days_used = 9;  -- Pino, Jake Adriane, Vacation Leave
UPDATE ohrm_leave_entitlement SET days_used = 10 WHERE id = 263 AND days_used = 9;  -- Fedillaga, Thea, Vacation Leave
UPDATE ohrm_leave_entitlement SET days_used = 9 WHERE id = 264 AND days_used = 0;  -- Labrador, Kim Rose, Vacation Leave
UPDATE ohrm_leave_entitlement SET days_used = 5 WHERE id = 268 AND days_used = 1.25;  -- Velayo, Jake, Vacation Leave
UPDATE ohrm_leave_entitlement SET days_used = 6 WHERE id = 269 AND days_used = 3.5;  -- Baga, Jully Ann, Vacation Leave
UPDATE ohrm_leave_entitlement SET days_used = 8 WHERE id = 272 AND days_used = 0;  -- Cuajao, Jayne, Vacation Leave
UPDATE ohrm_leave_entitlement SET days_used = 8 WHERE id = 273 AND days_used = 1.75;  -- Rubas, Melvin, Vacation Leave
UPDATE ohrm_leave_entitlement SET days_used = 2 WHERE id = 274 AND days_used = 0.75;  -- Adan, Kate Julia, Vacation Leave
UPDATE ohrm_leave_entitlement SET days_used = 6 WHERE id = 275 AND days_used = 3.5;  -- Borgonia, Lyra, Vacation Leave
UPDATE ohrm_leave_entitlement SET days_used = 5 WHERE id = 277 AND days_used = 0;  -- Salazar, Jessamae, Vacation Leave
UPDATE ohrm_leave_entitlement SET days_used = 3 WHERE id = 280 AND days_used = 0.5;  -- Gabotero, Rizza, Vacation Leave
UPDATE ohrm_leave_entitlement SET days_used = 12 WHERE id = 283 AND days_used = 13;  -- Abrao, Abegail, Vacation Leave
UPDATE ohrm_leave_entitlement SET days_used = 8 WHERE id = 286 AND days_used = 5.5;  -- Mag-asin, Ana Marie, Vacation Leave
UPDATE ohrm_leave_entitlement SET days_used = 1 WHERE id = 289 AND days_used = 0;  -- Tagupa, Shela Mie, Vacation Leave
UPDATE ohrm_leave_entitlement SET days_used = 10 WHERE id = 297 AND days_used = 12.5;  -- Calumpang, Jonathan, Vacation Leave
UPDATE ohrm_leave_entitlement SET days_used = 12 WHERE id = 300 AND days_used = 5.75;  -- Padayogdog, Venz Romer, Vacation Leave
UPDATE ohrm_leave_entitlement SET days_used = 7 WHERE id = 302 AND days_used = 0;  -- Aluben, Wilkent, Vacation Leave
UPDATE ohrm_leave_entitlement SET days_used = 9 WHERE id = 320 AND days_used = 7.75;  -- Damaolao, James Paul, Vacation Leave
UPDATE ohrm_leave_entitlement SET days_used = 6 WHERE id = 321 AND days_used = 3.5;  -- Jumao-as, Kemberly Rose, Vacation Leave
UPDATE ohrm_leave_entitlement SET days_used = 4 WHERE id = 323 AND days_used = 0;  -- Mahumas, Jenisel, Vacation Leave
UPDATE ohrm_leave_entitlement SET days_used = 5 WHERE id = 325 AND days_used = 2;  -- Tradio, Honey Mae, Vacation Leave
UPDATE ohrm_leave_entitlement SET days_used = 6 WHERE id = 328 AND days_used = 4.75;  -- Lagoy, Roland, Vacation Leave
UPDATE ohrm_leave_entitlement SET days_used = 8 WHERE id = 334 AND days_used = 11;  -- Maning, Joevien Rey, Vacation Leave
UPDATE ohrm_leave_entitlement SET days_used = 6 WHERE id = 335 AND days_used = 3.5;  -- Perez, Aileen, Vacation Leave
UPDATE ohrm_leave_entitlement SET days_used = 6 WHERE id = 336 AND days_used = 0;  -- Sacedon, Kristine Joy, Vacation Leave
UPDATE ohrm_leave_entitlement SET days_used = 5 WHERE id = 340 AND days_used = 2.5;  -- Ybanez, Lisa, Vacation Leave
UPDATE ohrm_leave_entitlement SET days_used = 10 WHERE id = 343 AND days_used = 9.375;  -- Villa, Divina Chris, Vacation Leave
UPDATE ohrm_leave_entitlement SET days_used = 6 WHERE id = 347 AND days_used = 3.5;  -- Armodia, Jean-Bee, Vacation Leave
UPDATE ohrm_leave_entitlement SET days_used = 7 WHERE id = 350 AND days_used = 5.75;  -- Comendador , Abby Marie, Vacation Leave
UPDATE ohrm_leave_entitlement SET days_used = 3 WHERE id = 359 AND days_used = 0;  -- Yonting, Ellaisa Mae, Vacation Leave
UPDATE ohrm_leave_entitlement SET days_used = 2 WHERE id = 374 AND days_used = 0.75;  -- Lungtad, Michelle, Vacation Leave
UPDATE ohrm_leave_entitlement SET days_used = 4 WHERE id = 376 AND days_used = 7.75;  -- Sumalinog, Reysen, Vacation Leave
UPDATE ohrm_leave_entitlement SET days_used = 7 WHERE id = 378 AND days_used = 2;  -- Ignacio, Princess, Vacation Leave
UPDATE ohrm_leave_entitlement SET days_used = 5 WHERE id = 383 AND days_used = 0;  -- Etang, Christy Ann, Vacation Leave
UPDATE ohrm_leave_entitlement SET days_used = 4 WHERE id = 385 AND days_used = 0.25;  -- Malunjao, Luke Andrie, Vacation Leave
UPDATE ohrm_leave_entitlement SET days_used = 4 WHERE id = 390 AND days_used = 1.5;  -- Sevilla, Glenda Marie, Vacation Leave
UPDATE ohrm_leave_entitlement SET days_used = 5 WHERE id = 403 AND days_used = 0;  -- Comeda, Renalyn, Vacation Leave
UPDATE ohrm_leave_entitlement SET days_used = 7 WHERE id = 404 AND days_used = 1;  -- Jovero, Johnace Faith, Vacation Leave
UPDATE ohrm_leave_entitlement SET days_used = 10 WHERE id = 488 AND days_used = 7.5;  -- Labrador, Kim Rose, Sick Leave
UPDATE ohrm_leave_entitlement SET days_used = 12 WHERE id = 509 AND days_used = 10.75;  -- Bucasas, Dorothea, Sick Leave
UPDATE ohrm_leave_entitlement SET days_used = 0 WHERE id = 526 AND days_used = 2.5;  -- Aluben, Wilkent, Sick Leave
UPDATE ohrm_leave_entitlement SET days_used = 9 WHERE id = 534 AND days_used = 7.75;  -- Asugao, Cricy, Sick Leave
UPDATE ohrm_leave_entitlement SET days_used = 1 WHERE id = 546 AND days_used = 0;  -- Impas, Viverly, Sick Leave
UPDATE ohrm_leave_entitlement SET days_used = 1 WHERE id = 583 AND days_used = 0;  -- Yonting, Ellaisa Mae, Sick Leave
UPDATE ohrm_leave_entitlement SET days_used = 9 WHERE id = 616 AND days_used = 7.75;  -- Manto, Jhon Mike, Sick Leave
UPDATE ohrm_leave_entitlement SET days_used = 7 WHERE id = 620 AND days_used = 5.75;  -- Mahusay, Jose Maria, Sick Leave
UPDATE ohrm_leave_entitlement SET days_used = 5 WHERE id = 632 AND days_used = 1.875;  -- Beronga, Alixa May, Sick Leave
UPDATE ohrm_leave_entitlement SET days_used = 4 WHERE id = 649 AND days_used = 2.75;  -- Miro, Christiad, Sick Leave
UPDATE ohrm_leave_entitlement SET days_used = 6 WHERE id = 682 AND days_used = 3.5;  -- Bontilao, Shanen Clair, Carry-Over Leave
UPDATE ohrm_leave_entitlement SET days_used = 2 WHERE id = 684 AND days_used = 0;  -- Tagupa, Shela Mie, Carry-Over Leave
UPDATE ohrm_leave_entitlement SET days_used = 4 WHERE id = 687 AND days_used = 1.5;  -- Alcoseba, Ana Marie, Carry-Over Leave
UPDATE ohrm_leave_entitlement SET days_used = 6 WHERE id = 714 AND days_used = 2.25;  -- Tabares, Jennille, Carry-Over Leave
UPDATE ohrm_leave_entitlement SET days_used = 5 WHERE id = 723 AND days_used = 1.25;  -- Abubakar, Meradz, Carry-Over Leave
UPDATE ohrm_leave_entitlement SET days_used = 5 WHERE id = 728 AND days_used = 2.5;  -- Asugao, Cricy, Carry-Over Leave
UPDATE ohrm_leave_entitlement SET days_used = 3 WHERE id = 729 AND days_used = 0;  -- Barcastigue, Jessa, Carry-Over Leave
UPDATE ohrm_leave_entitlement SET days_used = 2 WHERE id = 732 AND days_used = 3;  -- Becari, Jareth Lou, Carry-Over Leave
UPDATE ohrm_leave_entitlement SET days_used = 4 WHERE id = 746 AND days_used = 3;  -- Fedillaga, Thea, Carry-Over Leave
UPDATE ohrm_leave_entitlement SET days_used = 4 WHERE id = 753 AND days_used = 0;  -- Mag-asin, Ana Marie, Carry-Over Leave
UPDATE ohrm_leave_entitlement SET days_used = 1 WHERE id = 755 AND days_used = -1;  -- Malinao, Florence May, Carry-Over Leave
UPDATE ohrm_leave_entitlement SET days_used = 5 WHERE id = 768 AND days_used = 3.75;  -- Tejerero, Ivy Joe, Carry-Over Leave
UPDATE ohrm_leave_entitlement SET days_used = 1 WHERE id = 770 AND days_used = 0;  -- Tradio, Honey Mae, Carry-Over Leave
UPDATE ohrm_leave_entitlement SET days_used = 4 WHERE id = 771 AND days_used = 1.5;  -- Zozobrado, Jhon Michael, Carry-Over Leave
UPDATE ohrm_leave_entitlement SET days_used = 4 WHERE id = 786 AND days_used = 0;  -- Batoctoy, Christian, Vacation Leave
UPDATE ohrm_leave_entitlement SET days_used = 3 WHERE id = 805 AND days_used = 1.75;  -- Beronga, Alixa May, Magic Leave
UPDATE ohrm_leave_entitlement SET days_used = 3 WHERE id = 813 AND days_used = 1.75;  -- Yonting, Ellaisa Mae, Magic Leave
UPDATE ohrm_leave_entitlement SET days_used = 2 WHERE id = 817 AND days_used = 0;  -- Labad, Giovanni, Magic Leave
UPDATE ohrm_leave_entitlement SET days_used = 1 WHERE id = 821 AND days_used = 0;  -- Salabao-Alcibar, Linelle, Magic Leave
UPDATE ohrm_leave_entitlement SET days_used = 1 WHERE id = 837 AND days_used = 0;  -- Batoctoy, Christian, Magic Leave
UPDATE ohrm_leave_entitlement SET days_used = 2 WHERE id = 841 AND days_used = 0;  -- Besabella, Carl Michael, Vacation Leave
UPDATE ohrm_leave_entitlement SET days_used = 0 WHERE id = 854 AND days_used = 1.25;  -- Oliveros, Camille, Magic Leave
UPDATE ohrm_leave_entitlement SET days_used = 1 WHERE id = 861 AND days_used = 0;  -- Sacedon, Kristine Joy, Magic Leave
UPDATE ohrm_leave_entitlement SET days_used = 2 WHERE id = 874 AND days_used = 0.75;  -- Tanza, Jasmin Shaine, Leave Without Pay (LWOP)
UPDATE ohrm_leave_entitlement SET days_used = 3 WHERE id = 888 AND days_used = 1.75;  -- Dujali, Randolf, Sick Leave
UPDATE ohrm_leave_entitlement SET days_used = 15 WHERE id = 895 AND days_used = 26.25;  -- Baynos, Ana Marie, Parental leave
```
