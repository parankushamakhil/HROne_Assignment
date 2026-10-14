"""Employee attendance API. All business rules and HTTP handlers live here."""
import json
import os
import re
from calendar import monthrange
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Literal

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import ConnectionFailure, DuplicateKeyError, PyMongoError

load_dotenv()
UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"), tz_aware=True, tzinfo=UTC, serverSelectionTimeoutMS=5000)
db = client[os.getenv("MONGO_DB", "attendance_db")]
app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")
PRESENT = ("PRESENT", "WFH", "ON_DUTY")
EPOCH_MIN, EPOCH_MAX = 100_000_000_000, 4_102_444_800_000


@app.on_event("startup")
def create_indexes():
    try:
        db.employees.create_index("emp_code", unique=True, name="uq_employee_code")
        db.employees.create_index([("department", 1), ("joined_on", 1)], name="department_joined")
        db.employees.create_index([("department", 1), ("emp_code", 1)], name="department_employee_code")
        db.employees.create_index("joined_on", name="joined_on")
        db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True, name="uq_attendance_day")
        db.attendance_logs.create_index([("date", -1), ("emp_code", 1)], name="date_employee")
        db.attendance_logs.create_index([("status", 1), ("date", -1), ("emp_code", 1)], name="status_date_employee")
        db.attendance_logs.create_index([("date", 1), ("late_minutes", 1), ("emp_code", 1)], name="date_late_employee")
    except ConnectionFailure:
        # Let the app start so /health can report a temporary MongoDB outage.
        pass


def fail(detail: str, code: int = 422):
    raise HTTPException(code, detail)


def parse_date(value: str) -> date:
    try:
        result = date.fromisoformat(value)
    except (TypeError, ValueError):
        fail("date must be YYYY-MM-DD")
    if result.isoformat() != value:
        fail("date must be YYYY-MM-DD")
    return result


def parse_month(value: str) -> tuple[date, date]:
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", value or ""):
        fail("month must be YYYY-MM")
    year, month = map(int, value.split("-"))
    try:
        return date(year, month, 1), date(year, month, monthrange(year, month)[1])
    except ValueError:
        fail("month must be a valid calendar month")


def millis_to_datetime(value: int) -> datetime:
    if type(value) is not int or not EPOCH_MIN <= value <= EPOCH_MAX:
        fail("timestamp must be an epoch-millisecond integer in the supported range")
    return (datetime(1970, 1, 1, tzinfo=UTC) + timedelta(milliseconds=value)).replace(microsecond=0)


def as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def to_millis(value: datetime | None) -> int | None:
    value = as_utc(value)
    return int(value.timestamp() * 1000) if value else None


def rounded(value: float | Decimal, places: int = 2) -> float:
    return float(Decimal(str(value)).quantize(Decimal("1").scaleb(-places), rounding=ROUND_HALF_UP))


def shift_parts(value: str) -> tuple[int, int]:
    h, m = map(int, value.split(":"))
    return h, m


def attendance_date(punched_at: datetime, shift_start: str, shift_end: str) -> date:
    local = as_utc(punched_at).astimezone(IST)
    if shift_end <= shift_start and local.strftime("%H:%M") < shift_end:
        return local.date() - timedelta(days=1)
    return local.date()


def compute_late_minutes(punch_in: datetime, shift_start: str, day: date) -> int:
    h, m = shift_parts(shift_start)
    start = datetime.combine(day, time(h, m), IST)
    elapsed = int((as_utc(punch_in).astimezone(IST) - start).total_seconds())
    return elapsed // 60 if elapsed > 600 else 0


def compute_work_hours(punch_in: datetime | None, punch_out: datetime | None) -> float | None:
    if punch_in is None or punch_out is None:
        return None
    seconds = int((as_utc(punch_out) - as_utc(punch_in)).total_seconds())
    return rounded(Decimal(seconds) / Decimal(3600))


def compute_overtime(punch_out: datetime | None, shift_end: str, day: date, shift_start: str) -> int:
    if punch_out is None:
        return 0
    h, m = shift_parts(shift_end)
    end_day = day + timedelta(days=1) if shift_end <= shift_start else day
    end = datetime.combine(end_day, time(h, m), IST)
    elapsed = int((as_utc(punch_out).astimezone(IST) - end).total_seconds())
    return elapsed // 60 if elapsed >= 1800 else 0


def serialize_attendance(doc: dict) -> dict:
    doc = dict(doc)
    doc.pop("_id", None)
    doc.setdefault("punch_in", None)
    doc.setdefault("punch_out", None)
    doc.setdefault("work_hours", None)
    doc.setdefault("late_minutes", 0)
    doc.setdefault("overtime_minutes", 0)
    doc.setdefault("half_day", False)
    history = doc.setdefault("history", [])
    doc["punch_in"] = to_millis(doc.get("punch_in"))
    doc["punch_out"] = to_millis(doc.get("punch_out"))
    doc["history"] = [{
        "at": to_millis(item.get("at")), "by": item["by"], "reason": item["reason"],
        "changes": {field: {key: to_millis(value) if field in ("punch_in", "punch_out") else value
                             for key, value in change.items()} for field, change in item.get("changes", {}).items()},
    } for item in history]
    return doc


def serialize_employee(doc: dict) -> dict:
    doc = dict(doc)
    doc.pop("_id", None)
    if isinstance(doc.get("created_at"), datetime):
        doc["created_at"] = to_millis(doc["created_at"])
    return doc


def weekday_count(start: date, end: date) -> int:
    if start > end:
        return 0
    days = (end - start).days + 1
    weeks, remainder = divmod(days, 7)
    weekday = start.weekday()
    return weeks * 5 + min(remainder, max(0, 5 - weekday)) + min(5, max(0, remainder - (7 - weekday)))


class EmployeeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def valid_joined_on(cls, value):
        parse_date(value)
        return value

    @model_validator(mode="after")
    def distinct_shift_times(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start and shift_end must differ")
        return self


class PunchInIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str
    punched_at: StrictInt | None = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"

    @field_validator("punched_at")
    @classmethod
    def valid_punched_at(cls, value):
        if value is not None:
            millis_to_datetime(value)
        return value


class PunchOutIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str
    punched_at: StrictInt | None = None

    @field_validator("punched_at")
    @classmethod
    def valid_punched_at(cls, value):
        if value is not None:
            millis_to_datetime(value)
        return value


class RegularizeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    status: Literal["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"] | None = None
    punch_in: StrictInt | None = None
    punch_out: StrictInt | None = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @field_validator("status", mode="before")
    @classmethod
    def status_must_not_be_null(cls, value):
        if value is None:
            raise ValueError("status cannot be null")
        return value

    @field_validator("punch_in", "punch_out")
    @classmethod
    def valid_punch_time(cls, value):
        if value is not None:
            millis_to_datetime(value)
        return value


def monthly_group_pipeline(code: str, start: date, end: date) -> list[dict]:
    return [
        {"$match": {"emp_code": code, "date": {"$gte": start.isoformat(), "$lte": end.isoformat()}}},
        {"$set": {"_weekday": {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}}},
        {"$group": {"_id": None,
                    "present_days": {"$sum": {"$cond": [{"$and": [{"$in": ["$status", list(PRESENT)]}, {"$gte": ["$_weekday", 2]}, {"$lte": ["$_weekday", 6]}]}, {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1]}, 0]}},
                    "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                    "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
                    "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                    "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}}}},
    ]


def department_summary_pipeline(start: date, end: date, department: str | None) -> list[dict]:
    match = {"joined_on": {"$lte": end.isoformat()}}
    if department is not None:
        match["department"] = department
    return [
        {"$match": match},
        {"$lookup": {"from": "attendance_logs", "let": {"code": "$emp_code", "joined_on": "$joined_on"}, "pipeline": [
            {"$match": {"$expr": {"$and": [{"$eq": ["$emp_code", "$$code"]}, {"$gte": ["$date", start.isoformat()]}, {"$gte": ["$date", "$$joined_on"]}, {"$lte": ["$date", end.isoformat()]}]}}},
        ], "as": "logs"}},
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {"$set": {"_weekday": {"$cond": [{"$ne": [{"$type": "$logs.date"}, "missing"]}, {"$dayOfWeek": {"$dateFromString": {"dateString": "$logs.date"}}}, 0]}}},
        {"$group": {"_id": {"emp_code": "$emp_code", "department": "$department"}, "department": {"$first": "$department"},
                    "headcount": {"$first": 1},
                    "present_days": {"$sum": {"$cond": [{"$and": [{"$in": ["$logs.status", list(PRESENT)]}, {"$gte": ["$_weekday", 2]}, {"$lte": ["$_weekday", 6]}]}, {"$cond": [{"$ifNull": ["$logs.half_day", False]}, 0.5, 1]}, 0]}},
                    "work_hours_sum": {"$sum": {"$cond": [{"$and": [{"$in": ["$logs.status", list(PRESENT)]}, {"$isNumber": "$logs.work_hours"}]}, "$logs.work_hours", 0]}},
                    "work_hours_count": {"$sum": {"$cond": [{"$and": [{"$in": ["$logs.status", list(PRESENT)]}, {"$isNumber": "$logs.work_hours"}]}, 1, 0]}},
                    "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]}, 1, 0]}},
                    "total_late_minutes": {"$sum": {"$ifNull": ["$logs.late_minutes", 0]}},
                    "leave_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}},
                    "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]}}}},
        {"$group": {"_id": "$department", "headcount": {"$sum": "$headcount"}, "present_days": {"$sum": "$present_days"},
                    "work_hours_sum": {"$sum": "$work_hours_sum"}, "work_hours_count": {"$sum": "$work_hours_count"},
                    "late_count": {"$sum": "$late_count"}, "total_late_minutes": {"$sum": "$total_late_minutes"},
                    "leave_count": {"$sum": "$leave_count"}, "on_duty_count": {"$sum": "$on_duty_count"}}},
        {"$project": {"_id": 0, "department": "$_id", "headcount": 1, "present_days": 1,
                       "avg_work_hours": {"$cond": [{"$gt": ["$work_hours_count", 0]}, {"$divide": ["$work_hours_sum", "$work_hours_count"]}, None]},
                       "late_count": 1, "total_late_minutes": 1, "leave_count": 1, "on_duty_count": 1}},
        {"$sort": {"department": 1}},
    ]


def leaderboard_pipeline(start: date, end: date, department: str | None, limit: int) -> list[dict]:
    pipeline = [
        {"$match": {"date": {"$gte": start.isoformat(), "$lte": end.isoformat()}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "employee"}},
        {"$unwind": "$employee"},
    ]
    if department is not None:
        pipeline.append({"$match": {"employee.department": department}})
    pipeline.extend([
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {"_id": 0, "rank": 1, "emp_code": "$_id", "name": "$employee.name", "department": "$employee.department", "total_late_minutes": 1, "late_count": 1}},
    ])
    return pipeline


def trend_pipeline(department: str, start: date, end: date) -> list[dict]:
    count = (end - start).days + 1
    return [
        {"$match": {"department": department}},
        {"$limit": 1},
        {"$project": {"dates": {"$map": {"input": {"$range": [0, count]}, "as": "i", "in": {"$dateAdd": {"startDate": {"$dateFromString": {"dateString": start.isoformat()}}, "unit": "day", "amount": "$$i"}}}}}},
        {"$unwind": "$dates"},
        {"$project": {"_id": 0, "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$dates"}}}},
        {"$lookup": {"from": "employees", "let": {"day": "$date"}, "pipeline": [
            {"$match": {"department": department, "$expr": {"$lte": ["$joined_on", "$$day"]}}}, {"$count": "count"},
        ], "as": "eligible"}},
        {"$lookup": {"from": "attendance_logs", "let": {"day": "$date"}, "pipeline": [
            {"$match": {"$expr": {"$eq": ["$date", "$$day"]}}},
            {"$lookup": {"from": "employees", "localField": "emp_code", "foreignField": "emp_code", "as": "employee"}},
            {"$unwind": "$employee"}, {"$match": {"employee.department": department}},
            {"$group": {"_id": None,
                         "present_count": {"$sum": {"$cond": [{"$in": ["$status", list(PRESENT)]}, {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1]}, 0]}},
                         "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}}}},
        ], "as": "stats"}},
        {"$set": {"_day": {"$dateFromString": {"dateString": "$date"}}, "headcount": {"$ifNull": [{"$arrayElemAt": ["$eligible.count", 0]}, 0]},
                  "present_count": {"$ifNull": [{"$arrayElemAt": ["$stats.present_count", 0]}, 0]}, "late_count": {"$ifNull": [{"$arrayElemAt": ["$stats.late_count", 0]}, 0]}}},
        {"$set": {"is_working_day": {"$and": [{"$gte": [{"$dayOfWeek": "$_day"}, 2]}, {"$lte": [{"$dayOfWeek": "$_day"}, 6]}]}}},
        {"$set": {"attendance_rate": {"$cond": [{"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]}, {"$divide": ["$present_count", "$headcount"]}, None]}}},
        {"$setWindowFields": {"sortBy": {"date": 1}, "output": {"moving_avg_7d": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}}}},
        {"$project": {"_id": 0, "date": 1, "is_working_day": 1, "headcount": 1, "present_count": 1, "late_count": 1, "attendance_rate": 1, "moving_avg_7d": 1}},
    ]


def explain(collection, pipeline: list[dict]) -> dict:
    command = {"aggregate": collection.name, "pipeline": pipeline, "cursor": {}}
    return db.command("explain", command, verbosity="executionStats")


@app.get("/health")
def health():
    try:
        client.admin.command("ping")
    except PyMongoError:
        raise HTTPException(503, "MongoDB unavailable")
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = datetime.now(UTC).replace(microsecond=0)
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return serialize_employee(doc)


@app.get("/employees")
def list_employees(department: str | None = None, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    query = {"department": department} if department is not None else {}
    total = db.employees.count_documents(query)
    items = [serialize_employee(doc) for doc in db.employees.find(query, {"_id": 0}).sort("emp_code", 1).skip((page - 1) * page_size).limit(page_size)]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    employee = db.employees.find_one({"emp_code": body.emp_code})
    if not employee:
        raise HTTPException(404, "employee not found")
    ts = millis_to_datetime(body.punched_at) if body.punched_at is not None else datetime.now(UTC).replace(microsecond=0)
    day = attendance_date(ts, employee["shift_start"], employee["shift_end"])
    doc = {"emp_code": body.emp_code, "date": day.isoformat(), "status": body.status, "punch_in": ts, "punch_out": None,
           "work_hours": None, "late_minutes": compute_late_minutes(ts, employee["shift_start"], day), "overtime_minutes": 0, "half_day": False, "history": []}
    try:
        result = db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return serialize_attendance(doc)


@app.get("/attendance")
def list_attendance(emp_code: str | None = None, date_from: str | None = None, date_to: str | None = None,
                    status: Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"] | None = None,
                    page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    query = {}
    date_query = {}
    if emp_code:
        query["emp_code"] = emp_code
    if date_from:
        parse_date(date_from)
        date_query["$gte"] = date_from
    if date_to:
        parse_date(date_to)
        date_query["$lte"] = date_to
    if date_from and date_to and date_from > date_to:
        fail("date_from must not be after date_to")
    if date_query:
        query["date"] = date_query
    if status:
        query["status"] = status
    total = db.attendance_logs.count_documents(query)
    docs = db.attendance_logs.find(query).sort([("date", -1), ("emp_code", 1)]).skip((page - 1) * page_size).limit(page_size)
    return {"items": [serialize_attendance(doc) for doc in docs], "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    employee = db.employees.find_one({"emp_code": body.emp_code})
    if not employee:
        raise HTTPException(404, "employee not found")
    ts = millis_to_datetime(body.punched_at) if body.punched_at is not None else datetime.now(UTC).replace(microsecond=0)
    record = db.attendance_logs.find_one({"emp_code": body.emp_code, "punch_in": {"$lte": ts}}, sort=[("punch_in", -1), ("date", -1)])
    if record is None:
        raise HTTPException(404, "no punch-in found")
    if record.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")
    punch_in_time = as_utc(record["punch_in"])
    duration = int((ts - punch_in_time).total_seconds())
    if duration <= 0 or duration > 24 * 3600:
        fail("punched_at must be after punch_in and within 24 hours")
    day = parse_date(record["date"])
    hours = compute_work_hours(punch_in_time, ts)
    values = {"punch_out": ts, "work_hours": hours,
              "overtime_minutes": compute_overtime(ts, employee["shift_end"], day, employee["shift_start"]),
              "half_day": hours < 4.5}
    updated = db.attendance_logs.find_one_and_update({"_id": record["_id"], "punch_out": None}, {"$set": values}, return_document=ReturnDocument.AFTER)
    if updated is None:
        raise HTTPException(409, "already punched out")
    return serialize_attendance(updated)


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: RegularizeIn):
    day = parse_date(date)
    employee = db.employees.find_one({"emp_code": emp_code})
    if not employee:
        raise HTTPException(404, "employee not found")
    old = db.attendance_logs.find_one({"emp_code": emp_code, "date": day.isoformat()})
    if old is None:
        raise HTTPException(404, "attendance record not found")
    old.setdefault("history", [])
    old.setdefault("late_minutes", 0)
    old.setdefault("overtime_minutes", 0)
    old.setdefault("half_day", False)
    old.setdefault("work_hours", None)
    old.setdefault("punch_in", None)
    old.setdefault("punch_out", None)
    new = {key: old.get(key) for key in ("status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day")}
    patch = body.model_dump(exclude_unset=True, exclude={"reason", "regularized_by"})
    for key, value in patch.items():
        new[key] = millis_to_datetime(value) if key in ("punch_in", "punch_out") and value is not None else value
    status = new["status"]
    if status in ("ABSENT", "LEAVE"):
        if any(k in patch for k in ("punch_in", "punch_out")):
            fail("ABSENT and LEAVE cannot be supplied with punch times")
        new["punch_in"] = new["punch_out"] = None
    elif new["punch_in"] is None:
        fail("presence status requires punch_in")
    if new["punch_in"] is not None:
        if attendance_date(new["punch_in"], employee["shift_start"], employee["shift_end"]) != day:
            fail("punch_in must belong to the attendance date")
        if new["punch_out"] is not None:
            seconds = int((as_utc(new["punch_out"]) - as_utc(new["punch_in"])).total_seconds())
            if seconds <= 0 or seconds > 24 * 3600:
                fail("punch_out must be after punch_in and within 24 hours")
    if new["punch_in"] is None:
        new.update(work_hours=None, late_minutes=0, overtime_minutes=0, half_day=False)
    else:
        new["late_minutes"] = compute_late_minutes(new["punch_in"], employee["shift_start"], day)
        new["work_hours"] = compute_work_hours(new["punch_in"], new["punch_out"])
        new["overtime_minutes"] = compute_overtime(new["punch_out"], employee["shift_end"], day, employee["shift_start"])
        new["half_day"] = new["work_hours"] is not None and new["work_hours"] < 4.5
    changes = {}
    for key in ("status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day"):
        before, after = old.get(key), new.get(key)
        if before != after:
            changes[key] = {"from": before, "to": after}
    if not changes:
        fail("request does not change the record")
    entry = {"at": datetime.now(UTC).replace(microsecond=0), "by": body.regularized_by, "reason": body.reason, "changes": changes}
    history_filter = {"history": old["history"]} if "history" in old else {"history": {"$exists": False}}
    updated = db.attendance_logs.find_one_and_update({"_id": old["_id"], **history_filter}, {"$set": {key: new[key] for key in new}, "$push": {"history": entry}}, return_document=ReturnDocument.AFTER)
    if updated is None:
        raise HTTPException(409, "record changed concurrently; retry regularization")
    return serialize_attendance(updated)


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str):
    start, end = parse_month(month)
    employee = db.employees.find_one({"emp_code": emp_code})
    if not employee:
        raise HTTPException(404, "employee not found")
    effective_start = max(start, parse_date(employee["joined_on"]))
    working_days = weekday_count(effective_start, end)
    rows = list(db.attendance_logs.aggregate(monthly_group_pipeline(emp_code, effective_start, end))) if effective_start <= end else []
    result = rows[0] if rows else {"present_days": 0, "leave_days": 0, "late_count": 0, "total_late_minutes": 0, "total_overtime_minutes": 0}
    pct = rounded(Decimal(str(result["present_days"])) / Decimal(working_days) * 100, 2) if working_days else None
    return {"emp_code": emp_code, "month": month, "working_days": working_days, "present_days": rounded(result["present_days"]),
            "leave_days": result["leave_days"], "late_count": result["late_count"], "total_late_minutes": result["total_late_minutes"],
            "total_overtime_minutes": result["total_overtime_minutes"], "attendance_pct": pct}


@app.get("/analytics/departments/summary")
def department_summary(month: str, department: str | None = None):
    start, end = parse_month(month)
    items = list(db.employees.aggregate(department_summary_pipeline(start, end, department)))
    for item in items:
        item["present_days"] = rounded(item["present_days"])
        if item["avg_work_hours"] is not None:
            item["avg_work_hours"] = rounded(item["avg_work_hours"])
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: str, limit: int = Query(10, ge=1, le=50), department: str | None = None):
    start, end = parse_month(month)
    items = list(db.attendance_logs.aggregate(leaderboard_pipeline(start, end, department, limit)))
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_date: str = Query(..., alias="from"), to: str = Query(...)):
    start, end = parse_date(from_date), parse_date(to)
    if end < start or (end - start).days + 1 > 92:
        fail("date range must be ordered and no longer than 92 days")
    if not db.employees.find_one({"department": department}, {"_id": 1}):
        raise HTTPException(404, "department not found")
    items = list(db.employees.aggregate(trend_pipeline(department, start, end)))
    for item in items:
        for key in ("attendance_rate", "moving_avg_7d"):
            if item[key] is not None:
                item[key] = rounded(item[key], 4)
        item["present_count"] = rounded(item["present_count"])
    return {"department": department, "items": items}


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(endpoint: Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"],
                     emp_code: str | None = None, month: str | None = None, department: str | None = None,
                     limit: int = Query(10, ge=1, le=50), date_from: str | None = None, date_to: str | None = None,
                     status: Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"] | None = None,
                     page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100),
                     from_date: str | None = Query(None, alias="from"), to: str | None = None):
    if endpoint == "attendance_list":
        q = {}
        if emp_code:
            q["emp_code"] = emp_code
        if status:
            q["status"] = status
        dates = {}
        if date_from:
            parse_date(date_from); dates["$gte"] = date_from
        if date_to:
            parse_date(date_to); dates["$lte"] = date_to
        if date_from and date_to and date_from > date_to:
            fail("date_from must not be after date_to")
        if dates:
            q["date"] = dates
        pipe = [{"$match": q}, {"$sort": {"date": -1, "emp_code": 1}}, {"$skip": (page - 1) * page_size}, {"$limit": page_size}]
        collection = db.attendance_logs
    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            fail("emp_code and month are required for employee_monthly")
        start, end = parse_month(month)
        employee = db.employees.find_one({"emp_code": emp_code}, {"joined_on": 1})
        if not employee:
            raise HTTPException(404, "employee not found")
        pipe, collection = monthly_group_pipeline(emp_code, max(start, parse_date(employee["joined_on"])), end), db.attendance_logs
    elif endpoint == "department_summary":
        if not month:
            fail("month is required for department_summary")
        start, end = parse_month(month)
        pipe, collection = department_summary_pipeline(start, end, department), db.employees
    elif endpoint == "late_leaderboard":
        if not month:
            fail("month is required for late_leaderboard")
        start, end = parse_month(month)
        pipe, collection = leaderboard_pipeline(start, end, department, limit), db.attendance_logs
    else:
        if not department or not from_date or not to:
            fail("department, from and to are required for department_trend")
        start, end = parse_date(from_date), parse_date(to)
        if end < start or (end - start).days + 1 > 92:
            fail("date range must be ordered and no longer than 92 days")
        pipe, collection = trend_pipeline(department, start, end), db.employees
    try:
        result = explain(collection, pipe)
    except PyMongoError as exc:
        raise HTTPException(422, f"unable to explain query: {exc}")
    return {"endpoint": endpoint, "collection": collection.name,
            "explain": json.loads(json_util.dumps(result))}
