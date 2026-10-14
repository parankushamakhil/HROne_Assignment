import os
import unittest
from datetime import date, datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

os.environ["MONGO_URI"] = "mongodb://localhost:27017"

from app import main


class AttendanceEdgeCaseTests(unittest.TestCase):
    def test_weekday_count_excludes_weekends_and_empty_ranges(self):
        self.assertEqual(main.weekday_count(date(2026, 7, 4), date(2026, 7, 5)), 0)
        self.assertEqual(main.weekday_count(date(2026, 7, 6), date(2026, 7, 12)), 5)
        self.assertEqual(main.weekday_count(date(2026, 7, 8), date(2026, 7, 7)), 0)

    def test_mid_month_joiner_and_employee_with_no_logs(self):
        employee = SimpleNamespace(find_one=lambda *_args, **_kwargs: {"joined_on": "2026-07-15"})
        observed = {}

        def aggregate(pipeline):
            observed["pipeline"] = pipeline
            return []

        logs = SimpleNamespace(aggregate=aggregate)
        with patch.object(main, "db", SimpleNamespace(employees=employee, attendance_logs=logs)):
            result = main.employee_monthly("EMP0001", "2026-07")

        self.assertEqual(observed["pipeline"][0]["$match"]["date"], {
            "$gte": "2026-07-15", "$lte": "2026-07-31",
        })
        self.assertEqual(result["working_days"], 13)
        self.assertEqual(result["present_days"], 0)
        self.assertEqual(result["attendance_pct"], 0)

    def test_overnight_shift_uses_previous_attendance_date(self):
        punch_in = datetime(2026, 7, 10, 0, 0, tzinfo=timezone.utc)  # 05:30 IST
        punch_out = datetime(2026, 7, 10, 2, 15, tzinfo=timezone.utc)  # 07:45 IST
        attendance_day = main.attendance_date(punch_in, "22:00", "06:00")

        self.assertEqual(attendance_day, date(2026, 7, 9))
        self.assertEqual(main.compute_overtime(punch_out, "06:00", attendance_day, "22:00"), 105)

    def test_equal_lateness_keeps_shared_leaderboard_rank(self):
        pipeline = main.leaderboard_pipeline(date(2026, 7, 1), date(2026, 7, 31), None, 10)
        rank_stage = next(stage["$setWindowFields"] for stage in pipeline if "$setWindowFields" in stage)

        self.assertIn("$rank", rank_stage["output"]["rank"])
        self.assertEqual(pipeline[-2], {"$sort": {"total_late_minutes": -1, "_id": 1}})

    def test_trend_builds_calendar_days_from_a_real_collection(self):
        pipeline = main.trend_pipeline("Engineering", date(2026, 7, 1), date(2026, 7, 3))

        self.assertEqual(pipeline[0], {"$match": {"department": "Engineering"}})
        self.assertEqual(pipeline[1], {"$limit": 1})
        self.assertEqual(pipeline[2]["$project"]["dates"]["$map"]["input"]["$range"], [0, 3])
        self.assertTrue(any(stage.get("$lookup", {}).get("from") == "attendance_logs" for stage in pipeline))

    def test_trend_keeps_weekends_null_and_missing_logs_at_zero(self):
        pipeline = main.trend_pipeline("Engineering", date(2026, 7, 1), date(2026, 7, 5))
        default_counts = next(stage["$set"] for stage in pipeline if "$set" in stage and "headcount" in stage["$set"])
        rate = next(stage["$set"]["attendance_rate"] for stage in pipeline if "$set" in stage and "attendance_rate" in stage["$set"])
        working_day = next(stage["$set"]["is_working_day"] for stage in pipeline if "$set" in stage and "is_working_day" in stage["$set"])

        self.assertEqual(default_counts["present_count"]["$ifNull"][1], 0)
        self.assertIn({"$gt": ["$headcount", 0]}, rate["$cond"][0]["$and"])
        self.assertIsNone(rate["$cond"][2])
        self.assertEqual(working_day["$and"][0]["$gte"][1], 2)
        self.assertEqual(working_day["$and"][1]["$lte"][1], 6)
        self.assertEqual(pipeline[2]["$project"]["dates"]["$map"]["input"]["$range"], [0, 5])

    def test_department_summary_retains_employees_without_attendance_logs(self):
        pipeline = main.department_summary_pipeline(date(2026, 7, 1), date(2026, 7, 31), None)
        unwind = next(stage["$unwind"] for stage in pipeline if "$unwind" in stage)

        self.assertTrue(unwind["preserveNullAndEmptyArrays"])


if __name__ == "__main__":
    unittest.main()
