"""
Employee Attendance & Analytics API - STARTER

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)

This file was written quickly by a colleague who has left the company. The happy path
works, but nobody has reviewed it. Read PROBLEM_STATEMENT.docx for what is expected of you,
openapi.yaml for the contract and DATA_MODEL.md for what is stored in MongoDB.
"""
import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Literal, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field, model_validator
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError

load_dotenv()

client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"))
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))


def init_indexes():
    # employees collection indexes
    db.employees.create_index([("emp_code", 1)], unique=True)
    db.employees.create_index([("department", 1)])
    db.employees.create_index([("department", 1), ("joined_on", 1)])

    # attendance_logs collection indexes
    db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True)
    db.attendance_logs.create_index([("date", -1), ("emp_code", 1)])
    # leaderboard: filter by date range + late_minutes > 0
    db.attendance_logs.create_index([("date", 1), ("late_minutes", -1), ("emp_code", 1)])
    # trend: filter by date range for aggregation
    db.attendance_logs.create_index([("date", 1), ("emp_code", 1)])


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_indexes()
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def to_ist(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(IST)


def to_epoch_ms(dt: Optional[datetime]) -> Optional[int]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    # Truncate to whole seconds per Rule R1
    return int(dt.timestamp() // 1) * 1000


def from_epoch_ms(ms: Optional[int]) -> Optional[datetime]:
    if ms is None:
        return None
    # Truncate to whole seconds per Rule R1
    sec = ms // 1000
    return datetime.fromtimestamp(sec, tz=timezone.utc)


def get_attendance_date(punch_in_ist: datetime, shift_start: str, shift_end: str) -> str:
    """R1: A record's date is the IST calendar date of punch_in.
    Overnight shifts: punch-in earlier than shift_end belongs to the shift that started the previous day.
    """
    if shift_end <= shift_start:
        end_h, end_m = map(int, shift_end.split(":"))
        if (punch_in_ist.hour, punch_in_ist.minute, punch_in_ist.second) < (end_h, end_m, 0):
            return (punch_in_ist.date() - timedelta(days=1)).isoformat()
    return punch_in_ist.date().isoformat()


def compute_late_minutes(punch_in: datetime, shift_start: str, date_str: Optional[str] = None) -> int:
    """R2: Grace period 10 minutes. Late only if strictly more than 10:00 after shift_start.
    Then late_minutes = whole minutes (floor) since shift_start.
    """
    punch_in_ist = to_ist(punch_in)
    if not date_str:
        date_str = punch_in_ist.date().isoformat()
    h, m = map(int, shift_start.split(":"))
    start_dt = datetime.fromisoformat(date_str).replace(hour=h, minute=m, second=0, microsecond=0, tzinfo=IST)
    diff_sec = (punch_in_ist - start_dt).total_seconds()
    if diff_sec > 600:
        return int(diff_sec // 60)
    return 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """R4: (punch_out - punch_in) in seconds / 3600, rounded to 2 decimals half-up."""
    sec = int((punch_out - punch_in).total_seconds())
    hours = Decimal(sec) / Decimal(3600)
    return float(hours.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_overtime(punch_out: datetime, shift_end: str, date_str: str, shift_start: Optional[str] = None) -> int:
    """R3: Whole minutes (floor) between shift_end and punch_out, counted only when >= 30; otherwise 0.
    Overnight shift ends on next calendar day.
    """
    punch_out_ist = to_ist(punch_out)
    h_end, m_end = map(int, shift_end.split(":"))
    end_date = datetime.fromisoformat(date_str)
    if shift_start and shift_end <= shift_start:
        end_date += timedelta(days=1)
    shift_end_dt = end_date.replace(hour=h_end, minute=m_end, second=0, microsecond=0, tzinfo=IST)
    diff_sec = (punch_out_ist - shift_end_dt).total_seconds()
    minutes = int(diff_sec // 60)
    return minutes if minutes >= 30 else 0


def compute_half_day(work_hours: Optional[float]) -> bool:
    """R5: After punch-out, half_day is true when the rounded work_hours < 4.50."""
    if work_hours is None:
        return False
    return work_hours < 4.50


def format_attendance_doc(doc: dict) -> dict:
    history = []
    for h in doc.get("history") or []:
        changes = {}
        for k, v in (h.get("changes") or {}).items():
            c_from = v.get("from")
            c_to = v.get("to")
            if k in ("punch_in", "punch_out"):
                if isinstance(c_from, datetime):
                    c_from = to_epoch_ms(c_from)
                if isinstance(c_to, datetime):
                    c_to = to_epoch_ms(c_to)
            changes[k] = {"from": c_from, "to": c_to}
        at_val = h.get("at")
        if isinstance(at_val, datetime):
            at_val = to_epoch_ms(at_val)
        history.append({
            "at": at_val,
            "by": h.get("by"),
            "reason": h.get("reason"),
            "changes": changes,
        })

    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": to_epoch_ms(doc.get("punch_in")),
        "punch_out": to_epoch_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": history,
    }


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
class EmployeeIn(BaseModel):
    emp_code: str = Field(..., pattern=r"^EMP\d{4,6}$")
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str = Field(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$")

    @model_validator(mode="after")
    def validate_shifts(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(default=None, ge=100000000000, le=4102444800000)
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(default=None, ge=100000000000, le=4102444800000)


class RegularizeIn(BaseModel):
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None
    punch_in: Optional[int] = Field(default=None, ge=100000000000, le=4102444800000)
    punch_out: Optional[int] = Field(default=None, ge=100000000000, le=4102444800000)
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)


# --------------------------------------------------------------------------- #
# Endpoints provided
# --------------------------------------------------------------------------- #
@app.get("/health", status_code=200)
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(status_code=503, detail="Database unavailable")


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    now_utc = datetime.now(timezone.utc).replace(microsecond=0)
    doc["created_at"] = now_utc
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")

    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": to_epoch_ms(now_utc),
    }


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    page = int(getattr(page, "default", page))
    page_size = int(getattr(page_size, "default", page_size))
    q = {}
    if department:
        q["department"] = department
    skip = (page - 1) * page_size
    total = db.employees.count_documents(q)
    cursor = db.employees.find(q, {"_id": 0}).sort("emp_code", 1).skip(skip).limit(page_size)
    items = []
    for emp in cursor:
        items.append({
            "emp_code": emp["emp_code"],
            "name": emp["name"],
            "email": emp["email"],
            "department": emp["department"],
            "shift_start": emp.get("shift_start", "09:30"),
            "shift_end": emp.get("shift_end", "18:30"),
            "joined_on": emp["joined_on"],
            "created_at": to_epoch_ms(emp.get("created_at")),
        })
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "unknown employee")

    if body.punched_at is not None:
        ts_utc = from_epoch_ms(body.punched_at)
    else:
        ts_utc = datetime.now(timezone.utc).replace(microsecond=0)

    punch_in_ist = to_ist(ts_utc)
    shift_start = emp.get("shift_start", "09:30")
    shift_end = emp.get("shift_end", "18:30")
    d = get_attendance_date(punch_in_ist, shift_start, shift_end)
    late = compute_late_minutes(punch_in_ist, shift_start, d)

    doc = {
        "emp_code": body.emp_code,
        "date": d,
        "status": body.status,
        "punch_in": ts_utc,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")

    return format_attendance_doc(doc)


@app.post("/attendance/punch-out", status_code=200)
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "unknown employee")

    if body.punched_at is not None:
        punch_out_utc = from_epoch_ms(body.punched_at)
    else:
        punch_out_utc = datetime.now(timezone.utc).replace(microsecond=0)

    # Closes the employee's most recent record whose punch_in is at or before punched_at
    rec = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": punch_out_utc}},
        sort=[("punch_in", -1)],
    )
    if not rec or rec.get("punch_in") is None:
        raise HTTPException(404, "no punch-in found")

    punch_in_utc = rec["punch_in"]
    if punch_in_utc.tzinfo is None:
        punch_in_utc = punch_in_utc.replace(tzinfo=timezone.utc)

    diff_seconds = (punch_out_utc - punch_in_utc).total_seconds()
    if diff_seconds <= 0:
        raise HTTPException(422, "punch_out must be strictly after punch_in")
    if diff_seconds > 86400:
        raise HTTPException(422, "punch_out must be within 24 hours of punch_in")

    if rec.get("punch_out") is not None:
        raise HTTPException(409, "record is already punched out")

    shift_start = emp.get("shift_start", "09:30")
    shift_end = emp.get("shift_end", "18:30")
    work_hours = compute_work_hours(punch_in_utc, punch_out_utc)
    overtime = compute_overtime(punch_out_utc, shift_end, rec["date"], shift_start)
    half_day = compute_half_day(work_hours)

    updated = db.attendance_logs.find_one_and_update(
        {"_id": rec["_id"], "punch_out": None},
        {
            "$set": {
                "punch_out": punch_out_utc,
                "work_hours": work_hours,
                "overtime_minutes": overtime,
                "half_day": half_day,
            }
        },
        return_document=ReturnDocument.AFTER,
    )
    if not updated:
        raise HTTPException(409, "record is already punched out")

    return format_attendance_doc(updated)


@app.patch("/attendance/{emp_code}/{date}", status_code=200)
def regularize_attendance(emp_code: str, date: str, body: RegularizeIn):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "unknown employee")

    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not rec:
        raise HTTPException(404, "record not found for date")

    new_status = body.status if body.status is not None else rec["status"]

    # Setting status ABSENT or LEAVE clears both punch times; supplying punch_in or punch_out with it is 422
    if new_status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(422, "cannot supply punch times with ABSENT or LEAVE status")
        final_punch_in = None
        final_punch_out = None
        final_work_hours = None
        final_late_minutes = 0
        final_overtime_minutes = 0
        final_half_day = False
    else:
        # Presence status requires punch_in
        if body.punch_in is not None:
            final_punch_in = from_epoch_ms(body.punch_in)
        else:
            final_punch_in = rec.get("punch_in")

        if final_punch_in is None:
            raise HTTPException(422, "presence status requires punch_in")

        # punch_in must stay on record's attendance date (R1)
        shift_start = emp.get("shift_start", "09:30")
        shift_end = emp.get("shift_end", "18:30")
        calc_date = get_attendance_date(to_ist(final_punch_in), shift_start, shift_end)
        if calc_date != date:
            raise HTTPException(422, "punch_in does not belong to attendance date")

        if body.punch_out is not None:
            final_punch_out = from_epoch_ms(body.punch_out)
        else:
            final_punch_out = rec.get("punch_out")

        if final_punch_out is not None:
            p_in = final_punch_in.replace(tzinfo=timezone.utc) if final_punch_in.tzinfo is None else final_punch_in
            p_out = final_punch_out.replace(tzinfo=timezone.utc) if final_punch_out.tzinfo is None else final_punch_out
            diff_sec = (p_out - p_in).total_seconds()
            if diff_sec <= 0:
                raise HTTPException(422, "punch_out must be strictly after punch_in")
            if diff_sec > 86400:
                raise HTTPException(422, "punch_out must be within 24 hours of punch_in")

            final_work_hours = compute_work_hours(p_in, p_out)
            final_overtime_minutes = compute_overtime(p_out, shift_end, date, shift_start)
            final_half_day = compute_half_day(final_work_hours)
        else:
            final_work_hours = None
            final_overtime_minutes = 0
            final_half_day = False

        final_late_minutes = compute_late_minutes(final_punch_in, shift_start, date)

    # Compute exact changes
    changes = {}

    def normalize_dt(d):
        if d is None:
            return None
        return d.replace(tzinfo=timezone.utc) if d.tzinfo is None else d

    if rec.get("status") != new_status:
        changes["status"] = {"from": rec.get("status"), "to": new_status}

    old_pin = normalize_dt(rec.get("punch_in"))
    new_pin = normalize_dt(final_punch_in)
    if old_pin != new_pin:
        changes["punch_in"] = {"from": old_pin, "to": new_pin}

    old_pout = normalize_dt(rec.get("punch_out"))
    new_pout = normalize_dt(final_punch_out)
    if old_pout != new_pout:
        changes["punch_out"] = {"from": old_pout, "to": new_pout}

    if rec.get("work_hours") != final_work_hours:
        changes["work_hours"] = {"from": rec.get("work_hours"), "to": final_work_hours}

    if rec.get("late_minutes", 0) != final_late_minutes:
        changes["late_minutes"] = {"from": rec.get("late_minutes", 0), "to": final_late_minutes}

    if rec.get("overtime_minutes", 0) != final_overtime_minutes:
        changes["overtime_minutes"] = {"from": rec.get("overtime_minutes", 0), "to": final_overtime_minutes}

    if rec.get("half_day", False) != final_half_day:
        changes["half_day"] = {"from": rec.get("half_day", False), "to": final_half_day}

    if not changes:
        raise HTTPException(422, "request changes nothing")

    now_utc = datetime.now(timezone.utc).replace(microsecond=0)
    history_entry = {
        "at": now_utc,
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    current_history_len = len(rec.get("history") or [])
    updated = db.attendance_logs.find_one_and_update(
        {
            "_id": rec["_id"],
            "$expr": {"$eq": [{"$size": {"$ifNull": ["$history", []]}}, current_history_len]},
        },
        {
            "$set": {
                "status": new_status,
                "punch_in": final_punch_in,
                "punch_out": final_punch_out,
                "work_hours": final_work_hours,
                "late_minutes": final_late_minutes,
                "overtime_minutes": final_overtime_minutes,
                "half_day": final_half_day,
            },
            "$push": {"history": history_entry},
        },
        return_document=ReturnDocument.AFTER,
    )
    if not updated:
        raise HTTPException(409, "concurrent modification conflict")

    return format_attendance_doc(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    page = int(getattr(page, "default", page))
    page_size = int(getattr(page_size, "default", page_size))

    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from must not be greater than date_to")

    valid_statuses = {"PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"}
    if status and status not in valid_statuses:
        raise HTTPException(422, f"status must be one of {valid_statuses}")

    q = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        q["status"] = status

    skip = (page - 1) * page_size
    total = db.attendance_logs.count_documents(q)
    cursor = (
        db.attendance_logs.find(q)
        .sort([("date", -1), ("emp_code", 1)])
        .skip(skip)
        .limit(page_size)
    )
    items = [format_attendance_doc(d) for d in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


# --------------------------------------------------------------------------- #
# Analytics helpers
# --------------------------------------------------------------------------- #
def round_half_up(value: float, decimals: int = 2) -> float:
    """Rounds a float to the given number of decimal places using half-up rounding (R8)."""
    factor = Decimal(10) ** -decimals
    return float(Decimal(str(value)).quantize(factor, rounding=ROUND_HALF_UP))


def count_working_days(start_date: str, end_date: str, joined_on: str) -> int:
    """Count Mon-Fri calendar days from max(joined_on, start_date) to end_date inclusive (R7, R9)."""
    from datetime import date
    s = max(
        date.fromisoformat(start_date),
        date.fromisoformat(joined_on),
    )
    e = date.fromisoformat(end_date)
    if s > e:
        return 0
    count = 0
    cur = s
    while cur <= e:
        if cur.weekday() < 5:  # Mon=0, Fri=4
            count += 1
        cur += timedelta(days=1)
    return count


# --------------------------------------------------------------------------- #
# Analytics endpoints
# --------------------------------------------------------------------------- #
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$")):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "unknown employee")

    year, mon = int(month[:4]), int(month[5:])
    # First and last day of the month as YYYY-MM-DD strings
    from calendar import monthrange
    last_day = monthrange(year, mon)[1]
    month_start = f"{month}-01"
    month_end = f"{month}-{last_day:02d}"

    pipeline = [
        {"$match": {"emp_code": emp_code, "date": {"$gte": month_start, "$lte": month_end}}},
        {
            "$group": {
                "_id": None,
                # present_days: PRESENT/WFH/ON_DUTY on Mon-Fri; half_day=True counts as 0.5 (R5-R7)
                # MongoDB $dayOfWeek: 1=Sun,2=Mon,...,6=Fri,7=Sat; so Mon-Fri is [2,3,4,5,6]
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {
                                        "$in": [
                                            {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}},
                                            [2, 3, 4, 5, 6],
                                        ]
                                    },
                                ]
                            },
                            {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                            0,
                        ]
                    }
                },
                "leave_days": {
                    "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}
                },
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
            }
        },
    ]

    result = list(db.attendance_logs.aggregate(pipeline))
    agg = result[0] if result else {}

    present_days = agg.get("present_days", 0)
    leave_days = agg.get("leave_days", 0)
    late_count = agg.get("late_count", 0)
    total_late_minutes = agg.get("total_late_minutes", 0)
    total_overtime_minutes = agg.get("total_overtime_minutes", 0)

    working_days = count_working_days(month_start, month_end, emp["joined_on"])
    attendance_pct = (
        round_half_up((present_days / working_days) * 100, 2)
        if working_days > 0
        else None
    )

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": int(total_late_minutes),
        "total_overtime_minutes": int(total_overtime_minutes),
        "attendance_pct": attendance_pct,
    }


@app.get("/analytics/departments/summary")
def departments_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    from calendar import monthrange
    year, mon = int(month[:4]), int(month[5:])
    last_day = monthrange(year, mon)[1]
    month_start = f"{month}-01"
    month_end = f"{month}-{last_day:02d}"

    pipeline = [
        # Step 1: employees who had joined by end of this month
        {"$match": {"joined_on": {"$lte": month_end}}},

        # Step 2: fetch this employee's attendance logs for the month
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"ec": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {"$eq": ["$emp_code", "$$ec"]},
                            "date": {"$gte": month_start, "$lte": month_end},
                        }
                    }
                ],
                "as": "logs",
            }
        },

        # Step 3: one row per (employee, log); keep employees with zero logs
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},

        # Step 4: group by department, accumulate stats
        {
            "$group": {
                "_id": "$department",
                # collect distinct emp_codes so we can count headcount
                "emp_codes": {"$addToSet": "$emp_code"},

                # present_days: Mon-Fri PRESENT/WFH/ON_DUTY; half_day=True → 0.5
                # Guard: only call $dateFromString when logs.date is not null
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$ne": [{"$ifNull": ["$logs.date", None]}, None]},
                                    {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {
                                        "$in": [
                                            {
                                                "$dayOfWeek": {
                                                    "$dateFromString": {"dateString": "$logs.date"}
                                                }
                                            },
                                            [2, 3, 4, 5, 6],  # Mon–Fri
                                        ]
                                    },
                                ]
                            },
                            {"$cond": [{"$eq": ["$logs.half_day", True]}, 0.5, 1.0]},
                            0,
                        ]
                    }
                },

                # work_hours sum + count (for avg, presence-status records with non-null work_hours only)
                "work_hours_sum": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$ne": [{"$ifNull": ["$logs.work_hours", None]}, None]},
                                ]
                            },
                            "$logs.work_hours",
                            0,
                        ]
                    }
                },
                "work_hours_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$ne": [{"$ifNull": ["$logs.work_hours", None]}, None]},
                                ]
                            },
                            1,
                            0,
                        ]
                    }
                },

                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]},
                            1,
                            0,
                        ]
                    }
                },
                "total_late_minutes": {"$sum": {"$ifNull": ["$logs.late_minutes", 0]}},
                "leave_count": {
                    "$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}
                },
                "on_duty_count": {
                    "$sum": {"$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]}
                },
            }
        },

        # Step 5: project clean output
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": {"$size": "$emp_codes"},
                "present_days": 1,
                "work_hours_sum": 1,
                "work_hours_count": 1,
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1,
            }
        },

        # Step 6: only include departments with at least one employee
        {"$match": {"headcount": {"$gt": 0}}},

        # Step 7: sort alphabetically by department name
        {"$sort": {"department": 1}},
    ]

    # Optional: filter to a single department
    if department:
        pipeline.insert(0, {"$match": {"department": department}})

    rows = list(db.employees.aggregate(pipeline))

    items = []
    for row in rows:
        wh_count = row["work_hours_count"]
        wh_sum = row["work_hours_sum"]
        avg_work_hours = round_half_up(wh_sum / wh_count, 2) if wh_count > 0 else None

        items.append({
            "department": row["department"],
            "headcount": row["headcount"],
            "present_days": row["present_days"],
            "avg_work_hours": avg_work_hours,
            "late_count": row["late_count"],
            "total_late_minutes": int(row["total_late_minutes"]),
            "leave_count": row["leave_count"],
            "on_duty_count": row["on_duty_count"],
        })

    return {"month": month, "items": items}


# --------------------------------------------------------------------------- #
# GET /analytics/leaderboard/late
# --------------------------------------------------------------------------- #
@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(default=10, ge=1, le=50),
    department: Optional[str] = None,
):
    from calendar import monthrange
    year, mon = int(month[:4]), int(month[5:])
    last_day = monthrange(year, mon)[1]
    month_start = f"{month}-01"
    month_end = f"{month}-{last_day:02d}"

    # Build the aggregation on attendance_logs joined to employees
    match_logs: dict = {
        "date": {"$gte": month_start, "$lte": month_end},
        "late_minutes": {"$gt": 0},
    }

    pipeline = [
        {"$match": match_logs},

        # Sum late_minutes per employee in the month
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            }
        },
        # Only employees with total > 0
        {"$match": {"total_late_minutes": {"$gt": 0}}},

        # Join employees collection to get name and department
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        # Drop logs whose employee no longer exists
        {"$match": {"emp": {"$ne": []}}},
        {"$unwind": "$emp"},

        # Optional department filter (ranking inside that department only)
        *(
            [{"$match": {"emp.department": department}}]
            if department
            else []
        ),

        # Sort: total_late_minutes desc, emp_code asc — stable order before ranking
        {"$sort": {"total_late_minutes": -1, "_id": 1}},

        # Project fields we need
        {
            "$project": {
                "_id": 0,
                "emp_code": "$_id",
                "name": "$emp.name",
                "department": "$emp.department",
                "total_late_minutes": 1,
            }
        },
    ]

    rows = list(db.attendance_logs.aggregate(pipeline))

    # Standard competition ranking: assign ranks, keep rows where rank <= limit
    result = []
    rank = 0
    prev_minutes = None
    for i, row in enumerate(rows):
        if row["total_late_minutes"] != prev_minutes:
            rank = i + 1  # next rank = 1-based position after ties
            prev_minutes = row["total_late_minutes"]
        if rank > limit:
            break
        result.append({
            "rank": rank,
            "emp_code": row["emp_code"],
            "name": row["name"],
            "department": row["department"],
            "total_late_minutes": row["total_late_minutes"],
        })

    return {"month": month, "items": result}


# --------------------------------------------------------------------------- #
# GET /analytics/departments/{department}/trend
# --------------------------------------------------------------------------- #
@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_date: str = Query(..., alias="from"),
    to_date: str = Query(..., alias="to"),
):
    from datetime import date as date_type

    # --- validation ---
    try:
        d_from = date_type.fromisoformat(from_date)
        d_to = date_type.fromisoformat(to_date)
    except ValueError:
        raise HTTPException(422, "invalid date format")

    if d_to < d_from:
        raise HTTPException(422, "to must not be before from")
    if (d_to - d_from).days > 91:
        raise HTTPException(422, "date range must not exceed 92 days")

    # 404 if department has no employees
    dept_count = db.employees.count_documents({"department": department})
    if dept_count == 0:
        raise HTTPException(404, "unknown department")

    # --- aggregation ---
    pipeline = [
        # Step 1: attendance logs in date range for this department's employees
        {
            "$match": {
                "date": {"$gte": from_date, "$lte": to_date},
            }
        },
        # Step 2: join employees to filter by department
        {
            "$lookup": {
                "from": "employees",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},
        {"$match": {"emp.department": department}},

        # Step 3: group by date
        {
            "$group": {
                "_id": "$date",
                "present_count": {
                    "$sum": {
                        "$cond": [
                            {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                            0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}
                },
            }
        },

        # Step 4: densify to fill gaps (requires MongoDB 5.1+)
        {
            "$densify": {
                "field": "_id",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [
                        datetime.fromisoformat(from_date).replace(tzinfo=timezone.utc),
                        datetime.fromisoformat(to_date).replace(tzinfo=timezone.utc) + timedelta(days=1),
                    ],
                },
            }
        },

        # Step 5: convert densified _id (datetime) back to YYYY-MM-DD string & fill nulls
        {
            "$addFields": {
                "_id": {
                    "$cond": [
                        {"$eq": [{"$type": "$_id"}, "date"]},
                        {"$dateToString": {"format": "%Y-%m-%d", "date": "$_id"}},
                        "$_id",
                    ]
                },
                "present_count": {"$ifNull": ["$present_count", 0]},
                "late_count": {"$ifNull": ["$late_count", 0]},
            }
        },

        {"$sort": {"_id": 1}},
    ]

    rows = list(db.attendance_logs.aggregate(pipeline))

    # --- headcount per day (R9: employees with joined_on <= that day) ---
    # Fetch all employees in the department once
    dept_employees = list(
        db.employees.find(
            {"department": department, "joined_on": {"$lte": to_date}},
            {"emp_code": 1, "joined_on": 1, "_id": 0},
        )
    )

    def headcount_on(day_str: str) -> int:
        return sum(1 for e in dept_employees if e["joined_on"] <= day_str)

    # Build rows with headcount and rates, then compute 7-day moving avg
    from decimal import Decimal as D, ROUND_HALF_UP as RHU

    def r4(val) -> Optional[float]:
        if val is None:
            return None
        return float(D(str(val)).quantize(D("0.0001"), rounding=RHU))

    trend_rows = []
    # For moving average we track non-null rates in a window of up to 7
    rate_window: list = []

    for row in rows:
        day_str = row["_id"]
        d = date_type.fromisoformat(day_str)
        is_working = d.weekday() < 5  # Mon-Fri

        hc = headcount_on(day_str)
        present_count = row.get("present_count", 0) or 0
        late_count = row.get("late_count", 0) or 0

        if is_working and hc > 0:
            rate = r4(present_count / hc)
        else:
            rate = None

        # 7-day moving average of non-null rates within the range
        rate_window.append(rate)
        if len(rate_window) > 7:
            rate_window.pop(0)
        non_null = [r for r in rate_window if r is not None]
        moving_avg = r4(sum(non_null) / len(non_null)) if non_null else None

        trend_rows.append({
            "date": day_str,
            "is_working_day": is_working,
            "headcount": hc,
            "present_count": present_count,
            "late_count": late_count,
            "attendance_rate": rate,
            "moving_avg_7d": moving_avg,
        })

    return {"department": department, "from": from_date, "to": to_date, "items": trend_rows}


# --------------------------------------------------------------------------- #
# GET /admin/explain/{endpoint}
# --------------------------------------------------------------------------- #
@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str,
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(default=None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
    limit: int = Query(default=10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    from_date: Optional[str] = Query(default=None, alias="from"),
    to_date: Optional[str] = Query(default=None, alias="to"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    from calendar import monthrange
    from datetime import date as date_type

    VALID = {"attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"}
    if endpoint not in VALID:
        raise HTTPException(422, f"endpoint must be one of {sorted(VALID)}")

    # ------------------------------------------------------------------ #
    if endpoint == "attendance_list":
        if date_from and date_to and date_from > date_to:
            raise HTTPException(422, "date_from must not be greater than date_to")
        q: dict = {}
        if emp_code:
            q["emp_code"] = emp_code
        if date_from or date_to:
            q["date"] = {}
            if date_from:
                q["date"]["$gte"] = date_from
            if date_to:
                q["date"]["$lte"] = date_to
        if status:
            q["status"] = status
        skip = (page - 1) * page_size
        raw = db.attendance_logs.find(q).sort([("date", -1), ("emp_code", 1)]).skip(skip).limit(page_size)
        explain_doc = raw.explain("executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain_doc}

    # ------------------------------------------------------------------ #
    if endpoint == "employee_monthly":
        if not emp_code:
            raise HTTPException(422, "emp_code is required for employee_monthly")
        if not month:
            raise HTTPException(422, "month is required for employee_monthly")
        year, mon = int(month[:4]), int(month[5:])
        last_day = monthrange(year, mon)[1]
        month_start = f"{month}-01"
        month_end = f"{month}-{last_day:02d}"
        pipeline = [
            {"$match": {"emp_code": emp_code, "date": {"$gte": month_start, "$lte": month_end}}},
            {
                "$group": {
                    "_id": None,
                    "present_days": {
                        "$sum": {
                            "$cond": [
                                {
                                    "$and": [
                                        {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                        {"$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, [2, 3, 4, 5, 6]]},
                                    ]
                                },
                                {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                                0,
                            ]
                        }
                    },
                    "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                }
            },
        ]
        explain_doc = db.attendance_logs.aggregate(pipeline, explain=True)
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain_doc}

    # ------------------------------------------------------------------ #
    if endpoint == "department_summary":
        if not month:
            raise HTTPException(422, "month is required for department_summary")
        year, mon = int(month[:4]), int(month[5:])
        last_day = monthrange(year, mon)[1]
        month_end = f"{month}-{last_day:02d}"
        emp_match: dict = {"joined_on": {"$lte": month_end}}
        if department:
            emp_match["department"] = department
        pipeline = [
            {"$match": emp_match},
            {"$lookup": {"from": "attendance_logs", "localField": "emp_code", "foreignField": "emp_code", "as": "logs"}},
            {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
            {"$group": {"_id": "$department", "headcount": {"$addToSet": "$emp_code"}}},
        ]
        explain_doc = db.employees.aggregate(pipeline, explain=True)
        return {"endpoint": endpoint, "collection": "employees", "explain": explain_doc}

    # ------------------------------------------------------------------ #
    if endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(422, "month is required for late_leaderboard")
        year, mon = int(month[:4]), int(month[5:])
        last_day = monthrange(year, mon)[1]
        month_start = f"{month}-01"
        month_end = f"{month}-{last_day:02d}"
        match_logs: dict = {"date": {"$gte": month_start, "$lte": month_end}, "late_minutes": {"$gt": 0}}
        pipeline = [
            {"$match": match_logs},
            {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}}},
            {"$match": {"total_late_minutes": {"$gt": 0}}},
            {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "emp"}},
            {"$match": {"emp": {"$ne": []}}},
            {"$unwind": "$emp"},
            *(
                [{"$match": {"emp.department": department}}]
                if department else []
            ),
            {"$sort": {"total_late_minutes": -1, "_id": 1}},
        ]
        explain_doc = db.attendance_logs.aggregate(pipeline, explain=True)
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain_doc}

    # ------------------------------------------------------------------ #
    if endpoint == "department_trend":
        if not department:
            raise HTTPException(422, "department is required for department_trend")
        if not from_date:
            raise HTTPException(422, "from is required for department_trend")
        if not to_date:
            raise HTTPException(422, "to is required for department_trend")
        try:
            d_from = date_type.fromisoformat(from_date)
            d_to = date_type.fromisoformat(to_date)
        except ValueError:
            raise HTTPException(422, "invalid date format")
        if d_to < d_from:
            raise HTTPException(422, "to must not be before from")
        if (d_to - d_from).days > 91:
            raise HTTPException(422, "date range must not exceed 92 days")
        pipeline = [
            {"$match": {"date": {"$gte": from_date, "$lte": to_date}}},
            {"$lookup": {"from": "employees", "localField": "emp_code", "foreignField": "emp_code", "as": "emp"}},
            {"$unwind": "$emp"},
            {"$match": {"emp.department": department}},
            {"$group": {"_id": "$date", "present_count": {"$sum": 1}}},
            {"$sort": {"_id": 1}},
        ]
        explain_doc = db.attendance_logs.aggregate(pipeline, explain=True)
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain_doc}
