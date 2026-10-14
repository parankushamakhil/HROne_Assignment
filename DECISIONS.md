# Implementation decisions

1. **Indexes.** Startup creates unique indexes for `emp_code` and (`emp_code`, `date`) to enforce natural-key uniqueness and make punch races safe. Employee indexes support department, join-date, and sorted-code queries. Attendance indexes support employee-month lookups, date-sorted lists, status filters, and monthly late ranking. I considered a standalone `late_minutes` index; the date-leading compound index serves the leaderboard without that extra write cost.

2. **Punch-in race.** Concurrent requests may both pass the employee lookup, but the unique attendance index allows one insert for that employee and IST day. The other insert raises a duplicate-key error and returns 409.

3. **Ties.** `$rank` gives equal totals the same rank and skips the next rank. With `limit=10`, every employee tied at rank 10 is included. Equal totals are ordered by `emp_code`.

4. **Headcount.** Department summary starts from employees joined by month-end, then looks up monthly logs. An employee with no logs still counts once; a later joiner is excluded.

5. **Scale check.** A synthetic Atlas fixture of 400 employees and 100,000 attendance logs exercised five representative queries. All five plans used an index and showed no collection scan; query times ranged from 50 ms to 1.3 s, with the 31-day department trend slowest. The temporary database was dropped after the check. These timings are a point-in-time check, not a latency guarantee for other Atlas tiers or workloads.
