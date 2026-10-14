# DECISIONS.md

1. **Indexes.** Attendance has three: unique `(emp_code, date)` (stops duplicate punch-ins and serves one employee's month), `(date desc, emp_code)` (list sort and month/trend ranges) and `(status, date desc, emp_code)` (status filter). Employees has unique `emp_code`, `(department, emp_code)`, `(department, joined_on)` and `joined_on`. I did not add an index on `punch_in`, because the unique index already narrows a punch-out to one employee's few rows.

2. **Punch-in race.** Both requests call `insert_one`. The unique `(emp_code, date)` index lets only one succeed. The other gets `DuplicateKeyError` and I return 409. There is no read before the insert.

3. **Ties.** I use `$rank` (1, 2, 2, 4) and apply the limit to the rank. Everyone tied at the cutoff is returned, so there can be more rows than `limit`.

4. **Headcount.** The summary starts from `employees` (joined on or before month end) and attaches logs with `$lookup`. An employee with no logs is still one row and is counted.

5. **100x data.** I would keep a monthly summary per employee in a separate collection, updated on each punch or correction, so analytics do not scan raw logs.
