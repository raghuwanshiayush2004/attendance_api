# REVIEW.md

Problems I found in the starter `app/main.py`, what was wrong, how you would notice it, and what I did about it.

| # | Where | What is wrong | How you'd notice it | Fix |
|---|---|---|---|---|
| 1 | `compute_late_minutes` | Minutes are floored first and then compared with `> 10`, so 09:40:01 gives 0 instead of 10. The time zone is not IST either. | Punch in at 09:40:01 on a 09:30 shift and you get 0. | Compare the raw seconds with `> 600`, then floor to minutes. The shift start is built in IST. |
| 2 | `compute_late_minutes` | Overnight shifts are not handled. A 02:00 punch-in on a 22:00 shift is compared with the wrong day. | Punch in an overnight employee after midnight. | Added `attendance_date` and `shift_bounds`, which find the right shift start and end. |
| 3 | `compute_work_hours` | Python's `round()` is not half-up. | 16218 seconds should give 4.51 but gives 4.5. | Use `Decimal` with `ROUND_HALF_UP`. |
| 4 | `compute_overtime` | No 30 minute minimum. Mixing naive and aware datetimes raises `TypeError`. The shift end is not moved to the next day for overnight shifts. | Punch out 5 minutes after shift end, or call any punch-out and get a 500. | Rewrote it as `calc_overtime`: aware datetimes, floor, 30 minute minimum, overnight end date. |
| 5 | `punch_in` | If the employee is not found, `emp` is `None` and `emp["shift_start"]` crashes (500). | Send an unknown `emp_code`. | Return 404 first. |
| 6 | `punch_in` | `fromtimestamp` and `now()` use the server's local time and are naive, so the date is not the IST date. `if body.punched_at` treats 0 as missing. Microseconds are kept. | Run the server in UTC and punch in at 00:30 IST: the date is the previous day. | `ms_to_dt` returns a UTC-aware datetime cut to whole seconds. The date comes from `attendance_date`. The check is `is not None`. |
| 7 | `punch_in` | `find_one` followed by `insert_one` is a race condition. Two parallel requests both pass the check. | Send two identical requests at the same time and get two 201 responses. | Unique index on `(emp_code, date)`. Insert directly and turn `DuplicateKeyError` into 409. |
| 8 | `punch_in` | `status` accepts any string. `punched_at` accepts seconds, negative numbers and floats. | Send `status: "LEAVE"` or `punched_at: 1783312500`. | Only the 3 presence statuses are allowed and `punched_at` is a strict int in the milliseconds range. Everything else is 422. |
| 9 | `punch_in` response | Returns raw datetimes instead of epoch milliseconds. | Compare the response with the contract. | `record_out` converts all instants to milliseconds. |
| 10 | `create_employee` | Race condition on `emp_code` with no unique index. No validation. `created_at` is naive local time. | Two parallel creates, or `emp_code: "abc"`. | Unique index with 409 on `DuplicateKeyError`, Pydantic patterns and validators, `created_at` in UTC returned as milliseconds. |
| 11 | `list_employees` | `skip = page * page_size` skips the whole first page. `total` ignores the department filter. No sorting. | Ask for page 1 and the list starts at the 21st employee. | `skip = (page-1)*page_size`, `count_documents(q)`, sort by `emp_code`, limits on the paging values. |
| 12 | `list_attendance` | Loads the whole collection into memory and sorts in Python. No tie-break. No validation, and `date_from > date_to` is allowed. | Slow at 100k records. | Sort, skip and limit in the database with an index. Validation with 422. |
| 13 | `/health` | Returns ok even when MongoDB is down. | Stop MongoDB and call it. | Ping MongoDB and return 503 on failure. |
| 14 | Client and startup | The client is not `tz_aware`, so datetimes come back naive. No indexes are created. | `TypeError` when mixing naive and aware datetimes. Slow queries. | `tz_aware=True` and `setup_indexes()` at startup. |

## Looked at and decided it was fine
- `load_dotenv()` does not override real environment variables, so "env vars win" already holds.
- Returning 409 for a duplicate punch-in was right. Only the way it was detected was wrong (point 7).
- `joined_on` stays a string. ISO dates also sort correctly as text.
