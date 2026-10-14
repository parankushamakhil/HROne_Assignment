"""Run API and aggregation checks against an isolated, temporary MongoDB database."""
import os
import json
import socket
import sys
import subprocess
import time
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from threading import Barrier
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from dotenv import load_dotenv
from pymongo import MongoClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

load_dotenv()
MONGO_URI = os.getenv("MONGO_TEST_URI") or os.getenv("MONGO_URI")
if not MONGO_URI:
    raise SystemExit("Set MONGO_URI in .env or MONGO_TEST_URI before running this integration suite.")

TEST_DB = f"asmt_{uuid.uuid4().hex}"
os.environ["MONGO_URI"] = MONGO_URI
os.environ["MONGO_DB"] = TEST_DB
admin_client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=10000)
admin_client.admin.command("ping")

from app import main  # noqa: E402  (environment must be set before importing the application)


UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
SERVER = None
BASE_URL = None


def request(method, path, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = Request(BASE_URL + path, data=data, method=method, headers={"Content-Type": "application/json"})
    try:
        with urlopen(req, timeout=10) as response:
            return response.status, json.loads(response.read())
    except HTTPError as response:
        return response.code, json.loads(response.read())


def get(path, **params):
    query = urlencode(params)
    return request("GET", path + (f"?{query}" if query else ""))


def plan_stages(value):
    if isinstance(value, dict):
        if value.get("stage") in ("IXSCAN", "COLLSCAN"):
            yield value["stage"]
        for child in value.values():
            yield from plan_stages(child)
    elif isinstance(value, list):
        for child in value:
            yield from plan_stages(child)


def epoch_ms(local: datetime) -> int:
    return int(local.astimezone(UTC).timestamp() * 1000)


def stored_log(code, day, status, *, late=0, overtime=0, half_day=False, work_hours=None, hour=9, minute=30):
    punch_in = datetime.fromisoformat(f"{day}T{hour:02}:{minute:02}:00").replace(tzinfo=IST)
    return {
        "emp_code": code,
        "date": day,
        "status": status,
        "punch_in": punch_in if status in ("PRESENT", "WFH", "ON_DUTY") else None,
        "punch_out": punch_in + timedelta(hours=8) if work_hours is not None else None,
        "work_hours": work_hours,
        "late_minutes": late,
        "overtime_minutes": overtime,
        "half_day": half_day,
        "history": [],
    }


def seed_data():
    main.db.employees.insert_many([
        {"emp_code": "EMP1001", "name": "Asha Rao", "email": "asha@example.com", "department": "Engineering", "shift_start": "09:30", "shift_end": "18:30", "joined_on": "2026-07-01", "created_at": datetime(2026, 7, 1, tzinfo=UTC)},
        {"emp_code": "EMP1002", "name": "Ben Shah", "email": "ben@example.com", "department": "Engineering", "shift_start": "09:30", "shift_end": "18:30", "joined_on": "2026-07-15", "created_at": datetime(2026, 7, 15, tzinfo=UTC)},
        {"emp_code": "EMP1003", "name": "Cara Iyer", "email": "cara@example.com", "department": "Engineering", "shift_start": "09:30", "shift_end": "18:30", "joined_on": "2026-07-01", "created_at": datetime(2026, 7, 1, tzinfo=UTC)},
        {"emp_code": "EMP1004", "name": "Dev Mehta", "email": "dev@example.com", "department": "Engineering", "shift_start": "09:30", "shift_end": "18:30", "joined_on": "2026-07-01", "created_at": datetime(2026, 7, 1, tzinfo=UTC)},
        {"emp_code": "EMP1005", "name": "Esha Das", "email": "esha@example.com", "department": "Engineering", "shift_start": "22:00", "shift_end": "06:00", "joined_on": "2026-07-01", "created_at": datetime(2026, 7, 1, tzinfo=UTC)},
        {"emp_code": "EMP2001", "name": "Finn Roy", "email": "finn@example.com", "department": "Sales", "shift_start": "09:30", "shift_end": "18:30", "joined_on": "2026-07-01", "created_at": datetime(2026, 7, 1, tzinfo=UTC)},
    ])
    legacy = stored_log("EMP1004", "2026-07-01", "PRESENT", late=60, work_hours=8.0)
    legacy.pop("half_day")
    legacy.pop("history")
    main.db.attendance_logs.insert_many([
        stored_log("EMP1001", "2026-07-01", "PRESENT", half_day=True, work_hours=4.0),
        stored_log("EMP1001", "2026-07-02", "PRESENT", late=15, work_hours=7.5),
        stored_log("EMP1001", "2026-07-04", "WFH", late=15, overtime=35, work_hours=8.0),
        stored_log("EMP1001", "2026-07-07", "LEAVE"),
        stored_log("EMP1002", "2026-07-15", "WFH", late=30, work_hours=8.0),
        stored_log("EMP1002", "2026-07-16", "ON_DUTY", work_hours=7.0),
        legacy,
        stored_log("EMP2001", "2026-07-01", "PRESENT", work_hours=8.0),
    ])


class AssessmentIntegrationTests(unittest.TestCase):
    def test_readiness_and_employee_crud_validation(self):
        self.assertEqual(get("/health")[0], 200)
        payload = {"emp_code": "EMP9001", "name": "New Hire", "email": "new@example.com", "department": "QA", "joined_on": "2026-08-01"}
        code, created = request("POST", "/employees", payload)
        self.assertEqual(code, 201)
        self.assertEqual(created["created_at"] % 1000, 0)
        self.assertEqual(request("POST", "/employees", payload)[0], 409)
        self.assertEqual(request("POST", "/employees", {**payload, "emp_code": "bad"})[0], 422)

    def test_pagination_filters_and_monthly_analytics(self):
        page = get("/attendance", emp_code="EMP1001", page=1, page_size=2)[1]
        self.assertEqual(page["total"], 4)
        self.assertEqual(len(page["items"]), 2)
        self.assertEqual([row["date"] for row in page["items"]], ["2026-07-07", "2026-07-04"])
        legacy_page = get("/attendance", emp_code="EMP1004")[1]
        self.assertEqual((legacy_page["items"][0]["half_day"], legacy_page["items"][0]["history"]), (False, []))

        monthly = get("/analytics/employees/EMP1001/monthly", month="2026-07")[1]
        self.assertEqual((monthly["working_days"], monthly["present_days"], monthly["leave_days"]), (23, 1.5, 1))
        self.assertEqual((monthly["late_count"], monthly["total_late_minutes"], monthly["total_overtime_minutes"]), (2, 30, 35))
        self.assertEqual(monthly["attendance_pct"], 6.52)

        joiner = get("/analytics/employees/EMP1002/monthly", month="2026-07")[1]
        self.assertEqual((joiner["working_days"], joiner["present_days"]), (13, 2))
        empty = get("/analytics/employees/EMP1003/monthly", month="2026-07")[1]
        self.assertEqual((empty["present_days"], empty["attendance_pct"]), (0, 0))

    def test_department_summary_counts_joiners_and_employees_without_logs(self):
        items = get("/analytics/departments/summary", month="2026-07")[1]["items"]
        engineering = next(item for item in items if item["department"] == "Engineering")
        self.assertEqual(engineering["headcount"], 5)
        self.assertEqual(engineering["present_days"], 4.5)
        self.assertEqual(engineering["avg_work_hours"], 7.08)
        self.assertEqual(engineering["leave_count"], 1)
        self.assertEqual(engineering["on_duty_count"], 1)

    def test_leaderboard_ties_at_cutoff_and_department_filter(self):
        rows = get("/analytics/leaderboard/late", month="2026-07", limit=2, department="Engineering")[1]["items"]
        self.assertEqual([(r["emp_code"], r["rank"]) for r in rows], [("EMP1004", 1), ("EMP1001", 2), ("EMP1002", 2)])

    def test_trend_gap_fill_weekends_headcount_and_moving_average(self):
        status, result = get("/analytics/departments/Engineering/trend", **{"from": "2026-07-01", "to": "2026-07-05"})
        self.assertEqual(status, 200)
        rows = result["items"]
        self.assertEqual(len(rows), 5)
        self.assertEqual([row["headcount"] for row in rows], [4, 4, 4, 4, 4])
        self.assertEqual([row["present_count"] for row in rows], [1.5, 1, 0, 1, 0])
        self.assertEqual([row["attendance_rate"] for row in rows], [0.375, 0.25, 0, None, None])
        self.assertEqual([row["moving_avg_7d"] for row in rows], [0.375, 0.3125, 0.2083, 0.2083, 0.2083])

    def test_every_explain_endpoint_runs_over_its_matching_query(self):
        checks = [
            ("attendance_list", {}),
            ("employee_monthly", {"emp_code": "EMP1001", "month": "2026-07"}),
            ("department_summary", {"month": "2026-07"}),
            ("late_leaderboard", {"month": "2026-07"}),
            ("department_trend", {"department": "Engineering", "from": "2026-07-01", "to": "2026-07-03"}),
        ]
        for endpoint, params in checks:
            with self.subTest(endpoint=endpoint):
                code, result = get(f"/admin/explain/{endpoint}", **params)
                self.assertEqual(code, 200, result)
                self.assertEqual(result["endpoint"], endpoint)
                self.assertTrue("queryPlanner" in result["explain"] or "stages" in result["explain"])
                stages = list(plan_stages(result["explain"]))
                self.assertIn("IXSCAN", stages)
                self.assertNotIn("COLLSCAN", stages)

    def test_timestamp_boundaries_and_punch_out_rounding(self):
        employee = {"emp_code": "EMP3001", "name": "Shift Tester", "email": "shift@example.com", "department": "QA", "joined_on": "2026-08-01", "shift_start": "09:30", "shift_end": "18:30"}
        self.assertEqual(request("POST", "/employees", employee)[0], 201)

        grace = epoch_ms(datetime(2026, 8, 3, 9, 40, 0, 999000, tzinfo=IST))
        on_time = request("POST", "/attendance/punch-in", {"emp_code": "EMP3001", "punched_at": grace, "late_minutes": 999})[1]
        self.assertEqual((on_time["late_minutes"], on_time["punch_in"] % 1000), (0, 0))
        late = epoch_ms(datetime(2026, 8, 4, 9, 40, 1, tzinfo=IST))
        late_record = request("POST", "/attendance/punch-in", {"emp_code": "EMP3001", "punched_at": late})[1]
        self.assertEqual(late_record["late_minutes"], 10)

        punch_in = epoch_ms(datetime(2026, 8, 5, 9, 30, tzinfo=IST))
        request("POST", "/attendance/punch-in", {"emp_code": "EMP3001", "punched_at": punch_in})
        rounded_boundary = epoch_ms(datetime(2026, 8, 5, 13, 59, 42, tzinfo=IST))
        closed = request("POST", "/attendance/punch-out", {"emp_code": "EMP3001", "punched_at": rounded_boundary})[1]
        self.assertEqual((closed["work_hours"], closed["half_day"]), (4.5, False))

        for day, end_time, expected_overtime in ((6, (18, 59, 59), 0), (7, (19, 0, 0), 30)):
            start = epoch_ms(datetime(2026, 8, day, 9, 30, tzinfo=IST))
            self.assertEqual(request("POST", "/attendance/punch-in", {"emp_code": "EMP3001", "punched_at": start})[0], 201)
            end = epoch_ms(datetime(2026, 8, day, *end_time, tzinfo=IST))
            result = request("POST", "/attendance/punch-out", {"emp_code": "EMP3001", "punched_at": end})
            self.assertEqual((result[0], result[1]["overtime_minutes"]), (200, expected_overtime))

        early_overnight = epoch_ms(datetime(2026, 8, 14, 5, 30, tzinfo=IST))
        overnight = request("POST", "/attendance/punch-in", {"emp_code": "EMP1005", "punched_at": early_overnight})
        self.assertEqual((overnight[0], overnight[1]["date"]), (201, "2026-08-13"))

    def test_missing_resources_and_invalid_timestamp(self):
        self.assertEqual(request("POST", "/attendance/punch-in", {"emp_code": "EMP9999"})[0], 404)
        self.assertEqual(request("POST", "/attendance/punch-in", {"emp_code": "EMP1001", "punched_at": 1})[0], 422)
        self.assertEqual(request("POST", "/attendance/punch-in", {"emp_code": "EMP1001", "status": "ABSENT"})[0], 422)
        self.assertEqual(get("/analytics/departments/Unknown/trend", **{"from": "2026-07-01", "to": "2026-07-02"})[0], 404)

    def test_parallel_punch_ins_and_punch_outs_are_race_safe(self):
        base = datetime(2026, 8, 10, 22, 0, tzinfo=IST)
        barrier = Barrier(2)

        def punch_in():
            barrier.wait()
            return request("POST", "/attendance/punch-in", {"emp_code": "EMP1005", "punched_at": epoch_ms(base)})[0]

        with ThreadPoolExecutor(max_workers=2) as pool:
            statuses = sorted(pool.map(lambda _n: punch_in(), range(2)))
        self.assertEqual(statuses, [201, 409])

        punchin_next_day = datetime(2026, 8, 11, 22, 0, tzinfo=IST)
        self.assertEqual(request("POST", "/attendance/punch-in", {"emp_code": "EMP1005", "punched_at": epoch_ms(punchin_next_day)})[0], 201)
        punchout = epoch_ms(datetime(2026, 8, 12, 6, 45, tzinfo=IST))
        barrier = Barrier(2)

        def close_shift():
            barrier.wait()
            return request("POST", "/attendance/punch-out", {"emp_code": "EMP1005", "punched_at": punchout})[0]

        with ThreadPoolExecutor(max_workers=2) as pool:
            statuses = sorted(pool.map(lambda _n: close_shift(), range(2)))
        self.assertEqual(statuses, [200, 409])

    def test_parallel_regularizations_never_drop_history(self):
        employee = {"emp_code": "EMP3002", "name": "Correction Tester", "email": "correction@example.com", "department": "QA", "joined_on": "2026-08-01"}
        self.assertEqual(request("POST", "/employees", employee)[0], 201)
        day = "2026-08-13"
        self.assertEqual(request("POST", "/attendance/punch-in", {"emp_code": "EMP3002", "punched_at": epoch_ms(datetime(2026, 8, 13, 9, 30, tzinfo=IST))})[0], 201)
        barrier = Barrier(2)
        times = [datetime(2026, 8, 13, 9, 45, tzinfo=IST), datetime(2026, 8, 13, 10, 0, tzinfo=IST)]

        def correct(index):
            barrier.wait()
            return request("PATCH", f"/attendance/EMP3002/{day}", {"punch_in": epoch_ms(times[index]), "reason": f"Correction {index}", "regularized_by": f"reviewer{index}"})[0]

        with ThreadPoolExecutor(max_workers=2) as pool:
            statuses = list(pool.map(correct, range(2)))
        self.assertTrue(all(status in (200, 409) for status in statuses), statuses)
        record = main.db.attendance_logs.find_one({"emp_code": "EMP3002", "date": day})
        self.assertEqual(len(record["history"]), statuses.count(200))


def run():
    global SERVER, BASE_URL
    try:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        BASE_URL = f"http://127.0.0.1:{port}"
        SERVER = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port)],
            cwd=Path(__file__).resolve().parents[1], env=os.environ.copy(),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        for _ in range(100):
            if SERVER.poll() is not None:
                output, _ = SERVER.communicate()
                raise RuntimeError(f"The temporary API server exited before becoming ready: {output[-2000:]}")
            try:
                if get("/health")[0] == 200:
                    break
            except Exception:
                time.sleep(0.2)
        else:
            raise RuntimeError("The temporary API server did not become ready.")
        seed_data()
        suite = unittest.defaultTestLoader.loadTestsFromTestCase(AssessmentIntegrationTests)
        result = unittest.TextTestRunner(verbosity=2).run(suite)
        return result.wasSuccessful()
    finally:
        if SERVER and SERVER.poll() is None:
            SERVER.terminate()
            SERVER.wait(timeout=10)
        main.client.close()
        admin_client.drop_database(TEST_DB)
        admin_client.close()


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
