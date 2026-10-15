# DECISIONS.md

1. **Indexes.** Six indexes at startup. `employees.emp_code` (unique) enforces no-duplicate and powers every employee lookup. `employees.department` and `(department, joined_on)` serve summary filters. `attendance_logs.(emp_code, date)` (unique) is the natural key and prevents concurrent double punch-ins. `(date DESC, emp_code ASC)` covers the list sort. `(date, late_minutes DESC, emp_code)` serves the leaderboard. `(date, emp_code)` covers the trend aggregation. I considered a partial index on `late_minutes > 0` but MongoDB partial indexes cannot be used inside a `$lookup` sub-pipeline, so I rejected it.

2. **Punch-in race.** Both requests find the same employee, compute the same attendance date, and call `insert_one`. The unique compound index on `(emp_code, date)` lets only one succeed; the other raises `DuplicateKeyError`, which we catch and return as HTTP 409. No application lock needed.

3. **Ties.** After sorting by `total_late_minutes DESC, emp_code ASC`, rank is reset to the current 1-based position only when the minute total changes. If two employees tie at rank 2 and `limit=2`, both are returned because both satisfy `rank <= limit`.

4. **Headcount.** The summary pipeline starts on `employees` (filtered to `joined_on <= month_end`), then `$lookup` + `$unwind` with `preserveNullAndEmptyArrays: true`. Employees with zero logs produce one null-log row that still contributes their `emp_code` to the `$addToSet`, so they are counted.

5. **100x data.** Switch to Motor (async PyMongo) and run multiple uvicorn workers. The pipelines are already index-backed; async I/O would remove the event-loop blocking that limits concurrent throughput at scale.
