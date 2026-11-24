"""어댑터가 실제로 어떤 요청을 만드는지 확인하는 테스트 (네트워크 없음).

두 사이트의 요청 형식은 각 사이트 웹앱 코드에서 확인한 값이다.
  · 삼삼엠투: Next 서버 액션 (Next-Action 헤더 + RSC 본문)
  · 직방    : stay-apis REST (PUT /host/items/{id}/schedules, PATCH /host/items/{id})
"""
from __future__ import annotations

import json
import os
import sys
import unittest
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hrsync.httpc import Response  # noqa: E402
from hrsync.m33 import M33  # noqa: E402
from hrsync.zb import ZB  # noqa: E402


class FakeClient:
    def __init__(self, responses: dict | None = None):
        self.calls = []
        self.responses = responses or {}
        self._fixed_cookie = None

    def set_cookie_header(self, v):
        self._fixed_cookie = v

    def cookies(self, domain):
        return {"__Secure-session-token": "fake-token"} if "33m2" in domain else {}

    def get(self, url, **kw):
        self.calls.append(("GET", url, None, kw.get("headers")))
        for pat, resp in self.responses.items():
            if pat in url:
                return resp
        return Response(200, "{}", {}, url)

    def post(self, url, **kw):
        self.calls.append(("POST", url, kw.get("data") or kw.get("json_body"), kw.get("headers")))
        for pat, resp in self.responses.items():
            if pat in url:
                return resp
        return Response(200, "{}", {}, url)

    def request(self, method, url, **kw):
        self.calls.append((method, url, kw.get("json_body"), kw.get("headers")))
        for pat, resp in self.responses.items():
            if pat in url:
                return resp
        return Response(200, "{}", {}, url)


M33_ROOM = {"data": {
    "usingFee": 219000,
    "maintenanceFeeInfo": {"managementFee": 69000, "cleanFee": 39000,
                           "includeElectricity": True, "includeWater": True, "includeGas": True,
                           "managementFeeDescription": "전기/가스/수도 포함"},
    "discountInfo": {"longTermDiscounts": [{"weeks": 8, "discountRate": 10}],
                     "earlyCheckinDiscounts": []},
    "refundPolicy": "MIDDLE", "propertyType": "STUDIO", "roomName": "테스트"}}

ZB_ITEM = {"data": {"id": 2649, "title": "테스트", "weeklyRent": 219000,
                    "weeklyManageCost": 69000, "cleaningFee": 39000,
                    "isWeeklyManageCostAbsent": False, "isCleaningFeeIncluded": False,
                    "status": "PUBLISHED"}}


class M33RequestTest(unittest.TestCase):
    def make(self):
        c = FakeClient({"/v1/use-auth/host/rooms/93770/schedules": Response(
            200, json.dumps({"data": {"schedules": [], "contracts": []}}), {}, "")})
        api = M33(c, "a@b.c", "pw")
        api._logged_in = True
        return c, api

    def test_block_posts_server_action(self):
        c, api = self.make()
        api.set_unavailable(date(2027, 6, 15), date(2027, 6, 16))
        method, url, data, headers = c.calls[-1]
        self.assertEqual(method, "POST")
        self.assertTrue(url.endswith("/host/room/93770/schedule"))
        self.assertEqual(headers["Next-Action"], api.action_ids["registerUnavailableSchedule"])
        body = json.loads(data)
        self.assertEqual(body[0]["roomId"], 93770)
        self.assertEqual(body[0]["body"], {"startDate": "2027-06-15", "endDate": "2027-06-16"})

    def test_fee_update_sends_full_payload(self):
        c = FakeClient({"/v1/use-auth/host/rooms/93770": Response(200, json.dumps(M33_ROOM), {}, "")})
        api = M33(c, "a@b.c", "pw")
        api._logged_in = True
        api.set_fees(weekly_rent=239000)
        method, url, data, headers = c.calls[-1]
        self.assertEqual(headers["Next-Action"], api.action_ids["updateRoomFeeInfo"])
        payload = json.loads(data)[0]["body"]
        self.assertEqual(payload["usingFee"], 239000)
        self.assertEqual(payload["managementFee"], 69000)   # 건드리지 않은 값 유지
        self.assertEqual(payload["cleanFee"], 39000)
        self.assertEqual(payload["longTermDiscounts"], [{"weeks": 8, "discountRate": 10}])
        self.assertEqual(payload["refundPolicy"], "MIDDLE")
        self.assertEqual(payload["propertyType"], "STUDIO")


class ZBRequestTest(unittest.TestCase):
    def test_item_parsing(self):
        c = FakeClient({"/host/items/2649": Response(200, json.dumps(ZB_ITEM), {}, "")})
        api = ZB(c, 2649, cookie="a=b")
        api.login()
        item = api.item()
        self.assertEqual((item["weekly_rent"], item["maintenance"], item["cleaning"]),
                         (219000, 69000, 39000))

    def test_schedules_put_uses_union_ranges(self):
        c = FakeClient()
        api = ZB(c, 2649, cookie="a=b")
        api.login()
        api.set_unavailable({"2027-01-26": "blocked", "2027-01-27": "blocked",
                             "2027-02-02": "blocked"})
        method, url, payload, _ = c.calls[-1]
        self.assertEqual(method, "PUT")
        self.assertTrue(url.endswith("/host/items/2649/schedules"))
        self.assertEqual(payload["unavailableDates"],
                         [{"startDate": "2027-01-26", "endDate": "2027-01-27"},
                          {"startDate": "2027-02-02", "endDate": "2027-02-02"}])

    def test_pricing_patch(self):
        c = FakeClient({"/host/items/2649": Response(200, json.dumps(ZB_ITEM), {}, "")})
        api = ZB(c, 2649, cookie="a=b")
        api.login()
        item = api.item()
        api.patch_pricing(item, weekly_rent=239000)
        method, url, payload, _ = c.calls[-1]
        self.assertEqual(method, "PATCH")
        self.assertEqual(payload["pricing"]["weeklyRent"], 239000)
        self.assertEqual(payload["pricing"]["weeklyManageCost"], 69000)
        self.assertEqual(payload["pricing"]["cleaningFee"], 39000)

    def test_calendar_two_months(self):
        cal = {"data": {"schedules": [
            {"startDate": "2027-01-26", "endDate": "2027-01-28", "type": "BLOCKED"}]}}
        c = FakeClient({"/calendar?year=2027&month=1": Response(200, json.dumps(cal), {}, "")})
        api = ZB(c, 2649, cookie="a=b")
        api.login()
        got = api.calendar(date(2027, 1, 1), date(2027, 2, 5))["dates"]
        self.assertEqual(got["2027-01-27"], "blocked")
        self.assertTrue(any("month=2" in u for _, u, _, _ in c.calls))

    def test_403_is_reported(self):
        c = FakeClient({"/host/items/2649": Response(403, "{}", {}, "")})
        api = ZB(c, 2649, cookie="a=b")
        api.login()
        with self.assertRaises(Exception) as ctx:
            api.item()
        self.assertIn("세션", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
