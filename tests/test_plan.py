"""판정 규칙 단위 테스트.  실행: python3 -m unittest discover -s tests -v"""
from __future__ import annotations

import json
import os
import sys
import unittest
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hrsync import notify, plan  # noqa: E402
from hrsync.m33 import M33  # noqa: E402
from hrsync.zb import ZB, _to_ranges  # noqa: E402

TODAY = date(2026, 11, 24)

FIX = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "fixtures")


class DatePlanTest(unittest.TestCase):
    def test_contract_on_m33_blocks_zigbang(self):
        res = plan.plan_dates(
            {"2026-12-01": "booking", "2026-12-02": "booking", "2026-12-03": "booking"},
            {}, TODAY, 180)
        self.assertEqual(res["add_to_zb"], [("2026-12-01", "2026-12-03")])
        self.assertEqual(res["add_to_m33"], [])

    def test_block_on_zigbang_blocks_m33(self):
        res = plan.plan_dates({}, {"2027-03-01": "blocked", "2027-03-02": "blocked"}, TODAY, 180)
        self.assertEqual(res["add_to_m33"], [("2027-03-01", "2027-03-02")])
        self.assertEqual(res["add_to_zb"], [])

    def test_manually_blocked_m33_also_blocks_zigbang(self):
        res = plan.plan_dates({"2026-12-10": "disable"}, {}, TODAY, 180)
        self.assertEqual(res["add_to_zb"], [("2026-12-10", "2026-12-10")])

    def test_never_proposes_an_unblock(self):
        # 양쪽 다 막혀 있으면 아무것도 하지 않는다 (해제 제안이 생기면 안 됨)
        res = plan.plan_dates({"2026-12-05": "disable"}, {"2026-12-05": "blocked"}, TODAY, 180)
        self.assertEqual(res["add_to_zb"], [])
        self.assertEqual(res["add_to_m33"], [])

    def test_partial_overlap_only_fills_gap(self):
        m33 = {f"2026-12-{d:02d}": "booking" for d in range(1, 6)}
        zb = {"2026-12-01": "blocked", "2026-12-02": "blocked"}
        res = plan.plan_dates(m33, zb, TODAY, 180)
        self.assertEqual(res["add_to_zb"], [("2026-12-03", "2026-12-05")])

    def test_past_dates_ignored(self):
        res = plan.plan_dates({"2026-11-20": "booking"}, {}, TODAY, 180)
        self.assertEqual(res["add_to_zb"], [])

    def test_horizon_ignored_beyond(self):
        res = plan.plan_dates({"2027-09-01": "booking"}, {}, TODAY, 30)
        self.assertEqual(res["add_to_zb"], [])

    def test_mass_block_guard(self):
        m33 = {f"2026-12-{d:02d}": "booking" for d in range(1, 32)}
        m33.update({f"2027-01-{d:02d}": "booking" for d in range(1, 32)})
        m33.update({f"2027-02-{d:02d}": "booking" for d in range(1, 32)})
        m33.update({f"2027-03-{d:02d}": "booking" for d in range(1, 10)})
        res = plan.plan_dates(m33, {}, TODAY, 300)
        self.assertTrue(res["unsafe"])
        self.assertEqual(res["add_to_zb"], [])
        self.assertEqual(res["add_to_zb_days"], 99)


class PricePlanTest(unittest.TestCase):
    base_m = {"weekly_rent": 219000, "maintenance": 69000, "cleaning": 39000}
    base_z = {"weekly_rent": 219000, "maintenance": 69000, "cleaning": 39000}

    def test_first_run_is_baseline_only(self):
        res = plan.plan_prices(self.base_m, self.base_z, None, None)
        self.assertTrue(res["baseline"])
        self.assertEqual(res["changes"], [])
        self.assertTrue(res["notes"])

    def test_m33_changed_pushes_to_zigbang(self):
        cur_m = {"weekly_rent": 250000, "maintenance": 69000, "cleaning": 39000}
        res = plan.plan_prices(cur_m, self.base_z, self.base_m, self.base_z)
        self.assertEqual([(c["item"], c["target"], c["value"]) for c in res["changes"]],
                         [("weekly_rent", "zb", 250000)])

    def test_zigbang_changed_pushes_to_m33(self):
        cur_z = {"weekly_rent": 219000, "maintenance": 75000, "cleaning": 39000}
        res = plan.plan_prices(self.base_m, cur_z, self.base_m, self.base_z)
        self.assertEqual([(c["item"], c["target"], c["value"]) for c in res["changes"]],
                         [("maintenance", "m33", 75000)])

    def test_both_changed_is_ambiguous(self):
        cur_m = {"weekly_rent": 250000, "maintenance": 69000, "cleaning": 39000}
        cur_z = {"weekly_rent": 260000, "maintenance": 69000, "cleaning": 39000}
        res = plan.plan_prices(cur_m, cur_z, self.base_m, self.base_z)
        self.assertEqual(res["changes"], [])
        self.assertEqual(len(res["ambiguous"]), 1)

    def test_zero_and_huge_values_rejected(self):
        cur_m = {"weekly_rent": 0, "maintenance": 69000, "cleaning": 39000}
        res = plan.plan_prices(cur_m, self.base_z, self.base_m, self.base_z)
        self.assertEqual(res["changes"], [])
        self.assertTrue(any("0원" in s for s in res["skipped"]))

        cur_m2 = {"weekly_rent": 9000000, "maintenance": 69000, "cleaning": 39000}
        res2 = plan.plan_prices(cur_m2, self.base_z, self.base_m, self.base_z)
        self.assertEqual(res2["changes"], [])
        self.assertTrue(res2["skipped"])

    def test_equal_values_produce_no_action(self):
        res = plan.plan_prices(self.base_m, self.base_z, self.base_m, self.base_z)
        self.assertEqual(res["changes"], [])

    def test_snapshots_drops_extra_keys(self):
        self.assertEqual(plan.snapshots({"weekly_rent": 1, "maintenance": None, "x": 9}),
                         {"weekly_rent": 1, "maintenance": None, "cleaning": None})


class ParserTest(unittest.TestCase):
    def test_to_ranges(self):
        self.assertEqual(_to_ranges(["2026-12-01", "2026-12-02", "2026-12-05"]),
                         [("2026-12-01", "2026-12-02"), ("2026-12-05", "2026-12-05")])

    def test_zigbang_calendar_variants(self):
        a = ZB._parse_calendar({"schedules": [
            {"startDate": "2027-01-26", "endDate": "2027-01-27", "type": "BLOCKED"}]})
        self.assertEqual(a, {"2027-01-26": "blocked", "2027-01-27": "blocked"})
        b = ZB._parse_calendar({"data": [{"date": "2026-12-01", "status": "CONTRACTED"}]})
        self.assertEqual(b, {"2026-12-01": "contracted"})
        c = ZB._parse_calendar({"data": {"unavailableDates": [
            {"startDate": "2026-11-24", "endDate": "2026-11-25"}]}})
        self.assertEqual(c, {"2026-11-24": "blocked", "2026-11-25": "blocked"})
        self.assertEqual(ZB._parse_calendar({"weird": True}), {})

    def test_m33_room_fixture(self):
        path = os.path.join(FIX, "m33_room.json")
        if not os.path.exists(path):
            self.skipTest("fixture 없음")
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)["data"]
        mf = raw["maintenanceFeeInfo"]
        self.assertEqual(raw["usingFee"], 219000)
        self.assertEqual(mf["managementFee"], 69000)
        self.assertEqual(mf["cleanFee"], 39000)

    def test_alert_subject_tag(self):
        msg = notify.build_message("로그인 실패", "본문")
        self.assertTrue(msg["Subject"].startswith("[힐링룸동기화]"))
        self.assertEqual(msg["To"], os.environ.get("ALERT_TO") or os.environ.get("SMTP_USER")
                         or "noreply@example.com")


class RuleDocTest(unittest.TestCase):
    def test_m33_never_uses_delete_in_sync_path(self):
        """동기화 경로에서 해제 함수를 부르지 않는지 소스로 확인한다."""
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "sync.py"), encoding="utf-8") as f:
            src = f.read()
        self.assertNotIn("delete_unavailable", src)
        self.assertNotIn("set_unavailable(blocked=[]", src)


if __name__ == "__main__":
    unittest.main()
