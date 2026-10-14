"""
Employee Attendance & Analytics API  (FastAPI + MongoDB)

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB  (a local .env is read if present, real env vars win)
"""
import json
import logging
import os
import time
from calendar import monthrange
from contextlib import asynccontextmanager
from datetime import date as Date
from datetime import datetime, timedelta, timezone
from datetime import time as dtime
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Literal, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, Field, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()  # does not override variables that are already set
log = logging.getLogger("attendance")

# tz_aware so every datetime we read back is UTC-aware, never naive
client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True,
    serverSelectionTimeoutMS=5000,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
PRESENT_STATES = ["PRESENT", "WFH", "ON_DUTY"]

HHMM = r"^([01][0-9]|2[0-3]):[0-5][0-9]$"
DAY_RE = r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$"
MONTH_RE = r"^[0-9]{4}-(0[1-9]|1[0-2])$"

# index names (we hint them on the analytics queries, see DECISIONS.md)
IX_EMP_DATE = "emp_date_uq"
IX_DATE_EMP = "date_emp"
IX_STATUS = "status_date_emp"
IX_DEPT_JOINED = "dept_joined"
IX_JOINED = "joined_on"


def setup_indexes():
    emp, att = db.employees, db.attendance_logs
    wanted = [
        (emp, [("emp_code", ASCENDING)], {"name": "emp_code_uq", "unique": True}),
        (emp, [("department", ASCENDING), ("emp_code", ASCENDING)], {"name": "dept_emp"}),
        (emp, [("department", ASCENDING), ("joined_on", ASCENDING)], {"name": IX_DEPT_JOINED}),
        (emp, [("joined_on", ASCENDING)], {"name": IX_JOINED}),
        (att, [("emp_code", ASCENDING), ("date", ASCENDING)], {"name": IX_EMP_DATE, "unique": True}),
        (att, [("date", DESCENDING), ("emp_code", ASCENDING)], {"name": IX_DATE_EMP}),
        (att, [("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)], {"name": IX_STATUS}),
    ]
    for coll, keys, opts in wanted:
        try:
            coll.create_index(keys, **opts)
        except PyMongoError as exc:
            # e.g. the database already has duplicates; don't kill the whole app for it
            log.error("could not create index %s: %s", opts["name"], exc)


@asynccontextmanager
async def lifespan(_app):
    for _ in range(10):  # mongo may still be starting up
        try:
            client.admin.command("ping")
            break
        except PyMongoError:
            time.sleep(1)
    setup_indexes()
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# Small helpers: validation, time conversion, rounding
# --------------------------------------------------------------------------- #
def invalid(where, name, msg):
    """Raise a normal FastAPI 422 for a value that the type hints can't check."""
    raise RequestValidationError([{"type": "value_error", "loc": (where, name), "msg": msg, "input": None}])


def parse_day(text, where, name):
    try:
        if len(text) != 10 or text[4] != "-" or text[7] != "-":
            raise ValueError
        return Date.fromisoformat(text)
    except (ValueError, TypeError):
        invalid(where, name, "must be a valid date in YYYY-MM-DD format")


def month_bounds(month):
    try:
        year, mon = int(month[:4]), int(month[5:7])
        first = Date(year, mon, 1)
        last = Date(year, mon, monthrange(year, mon)[1])
    except (ValueError, TypeError):
        invalid("query", "month", "must be a valid month in YYYY-MM format")
    return first, last


def ms_to_dt(ms):
    """epoch milliseconds -> UTC datetime cut down to whole seconds (R1)"""
    return datetime.fromtimestamp(ms // 1000, tz=UTC)


def now_utc():
    return datetime.now(UTC).replace(microsecond=0)


def to_ms(value):
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return (value - EPOCH) // timedelta(milliseconds=1)


def half_up(value, places):
    quant = Decimal(1).scaleb(-places)
    return float(Decimal(value).quantize(quant, rounding=ROUND_HALF_UP))


def epoch_ms_field(**extra):
    return Field(strict=True, ge=100000000000, le=4102444800000, **extra)


EpochMs = Annotated[int, epoch_ms_field()]


# --------------------------------------------------------------------------- #
# Business rules R1 - R5
# --------------------------------------------------------------------------- #
def hm(text):
    hour, minute = text.split(":")
    return int(hour), int(minute)


def is_overnight(emp):
    return hm(emp["shift_end"]) <= hm(emp["shift_start"])


def attendance_date(punch_in, emp):
    """R1: IST date of the punch-in, or the previous day for an early overnight punch."""
    local = punch_in.astimezone(IST)
    day = local.date()
    if is_overnight(emp) and (local.hour, local.minute, local.second) < (*hm(emp["shift_end"]), 0):
        day -= timedelta(days=1)
    return day


def shift_bounds(emp, day):
    start = datetime.combine(day, dtime(*hm(emp["shift_start"])), tzinfo=IST)
    end_day = day + timedelta(days=1) if is_overnight(emp) else day
    end = datetime.combine(end_day, dtime(*hm(emp["shift_end"])), tzinfo=IST)
    return start, end


def calc_late(punch_in, emp, day):
    """R2: strictly more than 10:00 after shift start, then whole minutes since shift start."""
    start, _ = shift_bounds(emp, day)
    secs = (punch_in - start).total_seconds()
    return int(secs // 60) if secs > 600 else 0


def calc_overtime(punch_out, emp, day):
    """R3: whole minutes after shift end, only if that is 30 or more."""
    _, end = shift_bounds(emp, day)
    secs = (punch_out - end).total_seconds()
    mins = int(secs // 60) if secs > 0 else 0
    return mins if mins >= 30 else 0


def calc_work_hours(punch_in, punch_out):
    """R4: hours with 2 decimals, half-up."""
    secs = int((punch_out - punch_in).total_seconds())
    return half_up(Decimal(secs) / Decimal(3600), 2)


def derive(emp, day, status, punch_in, punch_out):
    """All stored derived fields for a record, from the final state of the record."""
    out = {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    if status in PRESENT_STATES and punch_in is not None:
        out["late_minutes"] = calc_late(punch_in, emp, day)
        if punch_out is not None:
            out["work_hours"] = calc_work_hours(punch_in, punch_out)
            out["overtime_minutes"] = calc_overtime(punch_out, emp, day)
            out["half_day"] = out["work_hours"] < 4.5  # R5, on the rounded value
    return out


# --------------------------------------------------------------------------- #
# Output shaping (Mongo document -> API json)
# --------------------------------------------------------------------------- #
def employee_out(doc):
    out = {k: doc.get(k) for k in
           ("emp_code", "name", "email", "department", "shift_start", "shift_end", "joined_on")}
    out["created_at"] = to_ms(doc.get("created_at"))
    return out


def _ms_if_date(value):
    return to_ms(value) if isinstance(value, datetime) else value


def record_out(doc):
    history = []
    for h in doc.get("history") or []:
        changes = {
            field: {"from": _ms_if_date(c.get("from")), "to": _ms_if_date(c.get("to"))}
            for field, c in (h.get("changes") or {}).items()
        }
        history.append({"at": to_ms(h.get("at")), "by": h.get("by"), "reason": h.get("reason"), "changes": changes})
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": to_ms(doc.get("punch_in")),
        "punch_out": to_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes") or 0,
        "overtime_minutes": doc.get("overtime_minutes") or 0,
        "half_day": bool(doc.get("half_day", False)),
        "history": history,
    }


# --------------------------------------------------------------------------- #
# Request models
# --------------------------------------------------------------------------- #
class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP[0-9]{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=HHMM)
    shift_end: str = Field(default="18:30", pattern=HHMM)
    joined_on: str = Field(pattern=DAY_RE)

    @field_validator("joined_on")
    @classmethod
    def joined_on_is_real_date(cls, v):
        Date.fromisoformat(v)  # ValueError becomes a 422
        return v

    @model_validator(mode="after")
    def shifts_differ(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start and shift_end must be different")
        return self


class PunchInIn(BaseModel):
    emp_code: str = Field(min_length=1)
    punched_at: Optional[EpochMs] = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str = Field(min_length=1)
    punched_at: Optional[EpochMs] = None


class RegularizeIn(BaseModel):
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None
    punch_in: Optional[EpochMs] = None
    punch_out: Optional[EpochMs] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


# --------------------------------------------------------------------------- #
# System + employees
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
    except PyMongoError:
        raise HTTPException(503, "database not reachable")
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = now_utc()
    try:
        db.employees.insert_one(doc)  # the unique index decides, no check-then-insert
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return employee_out(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {"department": department} if department else {}
    total = db.employees.count_documents(q)
    cur = db.employees.find(q, {"_id": 0}).sort("emp_code", ASCENDING)
    items = [employee_out(d) for d in cur.skip((page - 1) * page_size).limit(page_size)]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


# --------------------------------------------------------------------------- #
# Attendance
# --------------------------------------------------------------------------- #
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")

    ts = ms_to_dt(body.punched_at) if body.punched_at is not None else now_utc()
    day = attendance_date(ts, emp)
    doc = {
        "emp_code": body.emp_code,
        "date": day.isoformat(),
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        **derive(emp, day, body.status, ts, None),
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)  # unique (emp_code, date) index makes this race-safe
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return record_out(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")

    ts = ms_to_dt(body.punched_at) if body.punched_at is not None else now_utc()
    # latest record that started at or before this punch (null punch_in never matches $lte)
    rec = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": ts}},
        sort=[("date", DESCENDING)],
    )
    if not rec:
        raise HTTPException(404, "no punch-in found")
    if rec.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")

    p_in = rec["punch_in"]
    if ts <= p_in or ts - p_in > timedelta(hours=24):
        invalid("body", "punched_at", "must be after punch-in and within 24 hours of it")

    day = Date.fromisoformat(rec["date"])
    fields = derive(emp, day, rec["status"], p_in, ts)
    # only succeeds if nobody else closed the record in the meantime
    updated = db.attendance_logs.find_one_and_update(
        {"_id": rec["_id"], "punch_out": None},
        {"$set": {"punch_out": ts, **fields}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "already punched out")
    return record_out(updated)


def attendance_filter(emp_code, date_from, date_to, status):
    q = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        start = parse_day(date_from, "query", "date_from") if date_from else None
        end = parse_day(date_to, "query", "date_to") if date_to else None
        if start and end and start > end:
            invalid("query", "date_from", "date_from must not be after date_to")
        q["date"] = {}
        if start:
            q["date"]["$gte"] = start.isoformat()
        if end:
            q["date"]["$lte"] = end.isoformat()
    if status:
        q["status"] = status
    return q


ATTENDANCE_SORT = [("date", DESCENDING), ("emp_code", ASCENDING)]
StatusT = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[StatusT] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = attendance_filter(emp_code, date_from, date_to, status)
    total = db.attendance_logs.count_documents(q)
    cur = db.attendance_logs.find(q).sort(ATTENDANCE_SORT).skip((page - 1) * page_size).limit(page_size)
    return {"items": [record_out(d) for d in cur], "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: str, body: RegularizeIn):
    day = parse_day(date, "path", "date")
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")
    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": day.isoformat()})
    if not rec:
        raise HTTPException(404, "no record for that date")

    old_status, old_in, old_out = rec["status"], rec.get("punch_in"), rec.get("punch_out")
    new_status = body.status or old_status

    if new_status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            invalid("body", "punch_in", "ABSENT / LEAVE clear the punch times, do not send them")
        new_in = new_out = None
    else:
        new_in = ms_to_dt(body.punch_in) if body.punch_in is not None else old_in
        new_out = ms_to_dt(body.punch_out) if body.punch_out is not None else old_out
        if new_in is None:
            invalid("body", "punch_in", "a presence status needs a punch_in")
        if attendance_date(new_in, emp) != day:
            invalid("body", "punch_in", "punch_in must stay on the record's attendance date")
        if new_out is not None and (new_out <= new_in or new_out - new_in > timedelta(hours=24)):
            invalid("body", "punch_out", "punch_out must be after punch_in and within 24 hours")

    new_vals = {"status": new_status, "punch_in": new_in, "punch_out": new_out,
                **derive(emp, day, new_status, new_in, new_out)}
    old_vals = {
        "status": old_status, "punch_in": old_in, "punch_out": old_out,
        "work_hours": rec.get("work_hours"), "late_minutes": rec.get("late_minutes") or 0,
        "overtime_minutes": rec.get("overtime_minutes") or 0, "half_day": bool(rec.get("half_day", False)),
    }
    changes = {k: {"from": old_vals[k], "to": new_vals[k]} for k in new_vals if old_vals[k] != new_vals[k]}
    if not ({"status", "punch_in", "punch_out"} & changes.keys()):
        invalid("body", "status", "this request does not change anything")

    entry = {"at": now_utc(), "by": body.regularized_by, "reason": body.reason, "changes": changes}

    # Optimistic lock: only write if the record is still exactly as we read it.
    guard = {"_id": rec["_id"], "status": old_status, "punch_in": old_in, "punch_out": old_out}
    n_hist = len(rec.get("history") or [])
    if n_hist:
        guard["history"] = {"$size": n_hist}
    else:
        guard["$or"] = [{"history": None}, {"history": {"$size": 0}}]
    updated = db.attendance_logs.find_one_and_update(
        guard,
        {"$set": new_vals, "$push": {"history": entry}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "record was changed by someone else, please retry")
    return record_out(updated)


# --------------------------------------------------------------------------- #
# Analytics - pipeline builders (the explain endpoint reuses these)
# --------------------------------------------------------------------------- #
def present_x2(weekdays_only):
    """Presence counted in half-days: 2 = full day, 1 = half day, 0 = not present."""
    cond = {"$in": ["$status", PRESENT_STATES]}
    if weekdays_only:
        weekday = {"$isoDayOfWeek": {"$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d"}}}
        cond = {"$and": [cond, {"$lte": [weekday, 5]}]}
    return {"$cond": [cond, {"$cond": [{"$ifNull": ["$half_day", False]}, 1, 2]}, 0]}


def stats_group():
    """$group body shared by the employee monthly and the department summary."""
    late = {"$ifNull": ["$late_minutes", 0]}
    is_late = {"$gt": [late, 0]}
    has_hours = {"$and": [{"$in": ["$status", PRESENT_STATES]}, {"$isNumber": "$work_hours"}]}
    return {
        "_id": None,
        "present2": {"$sum": present_x2(True)},
        # work hours in hundredths, so the average can be rounded exactly in Python
        "hours_cents": {"$sum": {"$cond": [has_hours, {"$round": [{"$multiply": ["$work_hours", 100]}, 0]}, 0]}},
        "hours_n": {"$sum": {"$cond": [has_hours, 1, 0]}},
        "late_count": {"$sum": {"$cond": [is_late, 1, 0]}},
        "late_total": {"$sum": {"$cond": [is_late, late, 0]}},
        "overtime_total": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        "leave_count": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
        "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}},
    }


def monthly_pipeline(emp_code, first, last):
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}},
        {"$group": stats_group()},
    ]


def summary_pipeline(first, last, department):
    match = {"joined_on": {"$lte": last.isoformat()}}  # R9: joined on or before month end
    if department:
        match["department"] = department
    logs_of_month = [
        {"$match": {"date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}},
        {"$group": stats_group()},
    ]
    return [
        {"$match": match},
        # employees drive the pipeline, so people without any log still get counted
        {"$lookup": {"from": "attendance_logs", "localField": "emp_code", "foreignField": "emp_code",
                     "pipeline": logs_of_month, "as": "s"}},
        {"$addFields": {"s": {"$first": "$s"}}},
        {"$group": {
            "_id": "$department",
            "headcount": {"$sum": 1},
            "present2": {"$sum": {"$ifNull": ["$s.present2", 0]}},
            "hours_cents": {"$sum": {"$ifNull": ["$s.hours_cents", 0]}},
            "hours_n": {"$sum": {"$ifNull": ["$s.hours_n", 0]}},
            "late_count": {"$sum": {"$ifNull": ["$s.late_count", 0]}},
            "late_total": {"$sum": {"$ifNull": ["$s.late_total", 0]}},
            "leave_count": {"$sum": {"$ifNull": ["$s.leave_count", 0]}},
            "on_duty_count": {"$sum": {"$ifNull": ["$s.on_duty_count", 0]}},
        }},
        {"$sort": {"_id": 1}},
    ]


def leaderboard_pipeline(first, last, limit, department):
    pipe = [
        {"$match": {"date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}},
        {"$group": {
            "_id": "$emp_code",
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
        }},
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "emp"}},
        {"$unwind": "$emp"},  # logs of unknown employees disappear here
    ]
    if department:
        pipe.append({"$match": {"emp.department": department}})  # rank inside the department only
    pipe += [
        # $rank = standard competition ranking (1, 2, 2, 4)
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},  # limit after ranking, so ties at the cutoff stay
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {"_id": 0, "rank": 1, "emp_code": "$_id", "name": "$emp.name",
                      "department": "$emp.department", "total_late_minutes": 1, "late_count": 1}},
    ]
    return pipe


def trend_pipeline(codes, joined_list, start, end):
    start_dt = datetime(start.year, start.month, start.day, tzinfo=UTC)
    stop_dt = datetime(end.year, end.month, end.day, tzinfo=UTC) + timedelta(days=1)  # bound is exclusive
    day_fmt = "%Y-%m-%d"
    return [
        {"$match": {"emp_code": {"$in": codes}, "date": {"$gte": start.isoformat(), "$lte": end.isoformat()}}},
        # $facet always returns one document, even when nothing matched
        {"$facet": {"rows": [
            {"$group": {"_id": "$date", "present2": {"$sum": present_x2(False)},
                        "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}}}},
        ]}},
        # one dummy zero row on the first day, so $densify always has something to start from
        {"$project": {"rows": {"$concatArrays": [
            "$rows", [{"_id": start.isoformat(), "present2": 0, "late_count": 0}]]}}},
        {"$unwind": "$rows"},
        {"$replaceWith": "$rows"},
        {"$group": {"_id": "$_id", "present2": {"$sum": "$present2"}, "late_count": {"$sum": "$late_count"}}},
        {"$addFields": {"d": {"$dateFromString": {"dateString": "$_id", "format": day_fmt}}}},
        {"$densify": {"field": "d", "range": {"step": 1, "unit": "day", "bounds": [start_dt, stop_dt]}}},
        {"$addFields": {
            "date": {"$dateToString": {"date": "$d", "format": day_fmt}},
            "present2": {"$ifNull": ["$present2", 0]},
            "late_count": {"$ifNull": ["$late_count", 0]},
            "is_working_day": {"$lte": [{"$isoDayOfWeek": "$d"}, 5]},
        }},
        {"$addFields": {
            "headcount": {"$size": {"$filter": {
                "input": {"$literal": joined_list}, "as": "j", "cond": {"$lte": ["$$j", "$date"]}}}},
        }},
        # attendance rate in 1/10000, rounded half-up (null on weekends or when nobody has joined yet)
        {"$addFields": {"rate_int": {"$cond": [
            {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
            {"$floor": {"$add": [
                {"$divide": [{"$multiply": ["$present2", 10000]}, {"$multiply": [2, "$headcount"]}]}, 0.5]}},
            None]}}},
        {"$setWindowFields": {"sortBy": {"d": 1}, "output": {
            "avg_int": {"$avg": "$rate_int", "window": {"documents": [-6, 0]}}}}},
        {"$sort": {"d": 1}},
        {"$project": {
            "_id": 0, "date": 1, "is_working_day": 1, "headcount": 1, "late_count": 1,
            "present_count": {"$divide": ["$present2", 2]},
            "attendance_rate": {"$cond": [{"$isNumber": "$rate_int"}, {"$divide": ["$rate_int", 10000]}, None]},
            "moving_avg_7d": {"$cond": [{"$isNumber": "$avg_int"},
                                        {"$divide": [{"$floor": {"$add": ["$avg_int", 0.5]}}, 10000]}, None]},
        }},
    ]


def trend_inputs(department, start, end):
    """Parse and check the trend arguments, return what the pipeline needs."""
    s = parse_day(start, "query", "from")
    e = parse_day(end, "query", "to")
    if e < s:
        invalid("query", "to", "to must not be before from")
    if (e - s).days + 1 > 92:
        invalid("query", "to", "range must not exceed 92 days")
    people = list(db.employees.find({"department": department}, {"_id": 0, "emp_code": 1, "joined_on": 1}))
    if not people:
        raise HTTPException(404, "department not found")
    return s, e, [p["emp_code"] for p in people], [p["joined_on"] for p in people]


# --------------------------------------------------------------------------- #
# Analytics - endpoints
# --------------------------------------------------------------------------- #
def count_weekdays(first, last):
    if last < first:
        return 0
    return sum(1 for i in range((last - first).days + 1) if (first + timedelta(days=i)).weekday() < 5)


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(pattern=MONTH_RE)):
    first, last = month_bounds(month)
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")

    rows = list(db.attendance_logs.aggregate(monthly_pipeline(emp_code, first, last), hint=IX_EMP_DATE))
    s = rows[0] if rows else {}
    present = Decimal(int(s.get("present2", 0))) / 2

    joined = Date.fromisoformat(emp["joined_on"])
    working_days = count_weekdays(max(first, joined), last)
    pct = half_up(present * 100 / working_days, 2) if working_days else None
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": float(present),
        "leave_days": int(s.get("leave_count", 0)),
        "late_count": int(s.get("late_count", 0)),
        "total_late_minutes": int(s.get("late_total", 0)),
        "total_overtime_minutes": int(s.get("overtime_total", 0)),
        "attendance_pct": pct,
    }


def summary_hint(department):
    return IX_DEPT_JOINED if department else IX_JOINED


@app.get("/analytics/departments/summary")
def department_summary(month: str = Query(pattern=MONTH_RE), department: Optional[str] = None):
    first, last = month_bounds(month)
    rows = db.employees.aggregate(summary_pipeline(first, last, department), hint=summary_hint(department))
    items = []
    for r in rows:
        n = int(r["hours_n"])
        avg = half_up(Decimal(int(r["hours_cents"])) / Decimal(100 * n), 2) if n else None
        items.append({
            "department": r["_id"],
            "headcount": int(r["headcount"]),
            "present_days": float(Decimal(int(r["present2"])) / 2),
            "avg_work_hours": avg,
            "late_count": int(r["late_count"]),
            "total_late_minutes": int(r["late_total"]),
            "leave_count": int(r["leave_count"]),
            "on_duty_count": int(r["on_duty_count"]),
        })
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(pattern=MONTH_RE),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    first, last = month_bounds(month)
    rows = db.attendance_logs.aggregate(leaderboard_pipeline(first, last, limit, department), hint=IX_DATE_EMP)
    items = [{
        "rank": int(r["rank"]),
        "emp_code": r["emp_code"],
        "name": r["name"],
        "department": r["department"],
        "total_late_minutes": int(r["total_late_minutes"]),
        "late_count": int(r["late_count"]),
    } for r in rows]
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_: str = Query(alias="from"),
    to: str = Query(),
):
    start, end, codes, joined = trend_inputs(department, from_, to)
    rows = db.attendance_logs.aggregate(trend_pipeline(codes, joined, start, end), hint=IX_DATE_EMP)
    return {"department": department, "items": list(rows)}


# --------------------------------------------------------------------------- #
# Explain
# --------------------------------------------------------------------------- #
def need(value, name):
    if value is None:
        invalid("query", name, "this parameter is required for this endpoint")
    return value


@app.get("/admin/explain/{endpoint}")
def explain(
    endpoint: Literal["attendance_list", "employee_monthly", "department_summary",
                      "late_leaderboard", "department_trend"],
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=MONTH_RE),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[StatusT] = None,
    from_: Optional[str] = Query(None, alias="from"),
    to: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    hint = None
    if endpoint == "attendance_list":
        coll = "attendance_logs"
        q = attendance_filter(emp_code, date_from, date_to, status)
        command = {"find": coll, "filter": q, "sort": dict(ATTENDANCE_SORT),
                   "skip": (page - 1) * page_size, "limit": page_size}
    else:
        if endpoint == "employee_monthly":
            coll, hint = "attendance_logs", IX_EMP_DATE
            first, last = month_bounds(need(month, "month"))
            pipeline = monthly_pipeline(need(emp_code, "emp_code"), first, last)
        elif endpoint == "department_summary":
            coll, hint = "employees", summary_hint(department)
            first, last = month_bounds(need(month, "month"))
            pipeline = summary_pipeline(first, last, department)
        elif endpoint == "late_leaderboard":
            coll, hint = "attendance_logs", IX_DATE_EMP
            first, last = month_bounds(need(month, "month"))
            pipeline = leaderboard_pipeline(first, last, limit, department)
        else:
            coll, hint = "attendance_logs", IX_DATE_EMP
            start, end, codes, joined = trend_inputs(need(department, "department"),
                                                     need(from_, "from"), need(to, "to"))
            pipeline = trend_pipeline(codes, joined, start, end)
        command = {"aggregate": coll, "pipeline": pipeline, "cursor": {}, "hint": hint}

    result = db.command({"explain": command, "verbosity": "executionStats"})
    return {"endpoint": endpoint, "collection": coll, "explain": json.loads(json_util.dumps(result))}
