# Starter review

| # | Where | What is wrong | How you'd notice it | How you fixed it |
|---|---|---|---|---|
| 1 | `compute_late_minutes` | It compares floored minutes to the grace period, so a punch at 09:40:01 for a 09:30 shift is treated as on time. | Check the exact 10-minute boundary and one second after it. | Compare elapsed seconds to 600, then floor elapsed minutes. |
| 2 | `compute_late_minutes` | It assumes the punch and shift start use the same local timezone and calendar day. Overnight and non-IST hosts produce incorrect results. | Run with a UTC host timezone and test the early-morning part of a 22:00–06:00 shift. | Convert instants to IST and calculate against the attendance date. |
| 3 | `compute_work_hours` | Python's `round` uses ties-to-even, not the required half-up rounding. | Use a duration whose hours round exactly halfway at two decimals. | Use `Decimal` with `ROUND_HALF_UP`. |
| 4 | `compute_overtime` | It counts any positive overtime, even when it is under the 30-minute minimum. | Punch out 29 minutes after shift end. | Return zero below 30 minutes; otherwise floor elapsed minutes. |
| 5 | `compute_overtime` | It always places shift end on the attendance date, so overnight shifts calculate negative or incorrect overtime. | Test a 22:00–06:00 shift ending the following morning. | Place overnight shift end on the next IST calendar day. |
| 6 | All timestamp calculations | Milliseconds are not truncated to whole seconds before storage and calculation. | Supply an epoch-millisecond value with a fractional second. | Convert to UTC and truncate to whole seconds at input. |
| 7 | `/health` | It returns success without checking MongoDB. | Stop MongoDB and request the health route. | Ping MongoDB and return 503 on connection failure. |
| 8 | Startup / employee creation | There is no unique database index for `emp_code`; the check-then-insert can create duplicates under concurrent requests. | Send concurrent creates with one employee code. | Create a unique index at startup and map duplicate-key errors to 409. |
| 9 | `create_employee` | It stores a naive creation time and returns a datetime instead of epoch milliseconds. | Inspect `created_at` in a response and compare across host timezones. | Store an aware UTC instant and serialize it as epoch milliseconds. |
| 10 | `EmployeeIn` | Input constraints are missing, including employee code, email, date/time formats, lengths, and distinct shift times. | Submit malformed values and equal shift times. | Add Pydantic validation matching the OpenAPI contract. |
| 11 | `list_employees` | `skip` starts at `page * page_size`, skipping the first page. | Request page 1 from a collection with more than one page. | Use `(page - 1) * page_size`. |
| 12 | `list_employees` | `total` ignores the department filter and results have no required stable sort. | Filter a department and compare total or repeat page requests. | Count with the applied filter and sort by `emp_code`. |
| 13 | `punch_in` | An unknown employee is dereferenced and becomes a 500. | Punch in using an unknown `emp_code`. | Return 404 before reading the employee's shift. |
| 14 | `punch_in` | `fromtimestamp` makes the instant depend on the host timezone, and the attendance date is not calculated in IST or for overnight shifts. | Change host timezone or punch in shortly after midnight during an overnight shift. | Parse epoch milliseconds as UTC and derive the attendance date in IST. |
| 15 | `punch_in` | A truthiness check treats timestamp zero as omitted, and input type/range is not validated. | Send zero, a seconds-looking value, a float, or a string. | Accept only supported integer epoch milliseconds; default only when omitted. |
| 16 | `punch_in` | The duplicate check and insert are separate, so concurrent punches can both succeed. | Send parallel punch-ins for the same employee and date. | Enforce unique (`emp_code`, `date`) in MongoDB and handle duplicate-key as 409. |
| 17 | `punch_in` | The employee lookup result is unchecked for existence and the status is not limited to presence statuses. | Try an unknown employee or `ABSENT` punch-in. | Return 404 for an unknown employee and validate the allowed statuses. |
| 18 | `list_attendance` | It reads and sorts the entire matching collection in Python, which does not scale to the grader's dataset. | Inspect memory/query work with a large attendance collection. | Filter, count, sort, skip, and limit in MongoDB with supporting indexes. |
| 19 | `list_attendance` | It returns BSON datetimes in FastAPI's default representation, not epoch milliseconds, and legacy fields can be absent. | Inspect a regularized response or the legacy sample record. | Serialize instants (including history values) and supply documented defaults. |
| 20 | `list_attendance` | It does not reject reversed date ranges or enforce pagination bounds, and the date/employee sort is not explicit in MongoDB. | Request reversed dates, page 0, or page size over 100. | Validate dates and pagination, and apply the required stable sort. |
| 21 | Missing routes | Punch-out, regularization, all four analytics routes, and explain are absent. | Compare registered routes with `openapi.yaml`. | Implement each route and its required response/error behavior. |
| 22 | `punch_in` response | It adds an `id` field even though attendance records are addressed by (`emp_code`, `date`) and the response schema has no `id`. | Compare the response with `AttendanceRecord`. | Return the contract fields only. |

## Reviewed and not defects

- The starter uses `emp_code` rather than MongoDB `_id` to find employees; that matches the natural-key contract.
- Its attendance date range comparison uses ISO date strings, which sort chronologically when dates are validated as `YYYY-MM-DD`.
- `compute_work_hours` correctly measures elapsed time, including across midnight; its rounding mode, not the subtraction, needed correction.
