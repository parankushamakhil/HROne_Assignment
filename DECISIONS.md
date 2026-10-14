# Implementation decisions

1. **Indexes.** Startup creates unique indexes on `emp_code` and (`emp_code`, `date`) to enforce identity and make attendance writes race-safe. Employee indexes support department and join-date filters. Attendance indexes support employee-month queries, date-sorted lists, status filters, and late rankings. The date-leading index covers the leaderboard without a separate `late_minutes` index.

2. **Punch-in race.** Both requests may find the employee, but the unique attendance index permits only one record for that employee and IST date. The losing insert raises a duplicate-key error and returns 409.

3. **Ties.** `$rank` gives equal totals the same rank and skips the next rank. A tie at the limit is returned in full; tied employees are ordered by `emp_code`.

4. **Headcount.** Department summary starts from employees who joined by period-end, then looks up their logs. Employees with no logs count once; employees joining later are excluded.

5. **100x data.** On a synthetic 100,000-log Atlas fixture, all five explain plans used indexes with no collection scans; queries took 50 ms–1.3 s. At 10 million logs, I would profile the slowest workloads first, then consider monthly rollups only if measured latency required them. The temporary database was dropped.
