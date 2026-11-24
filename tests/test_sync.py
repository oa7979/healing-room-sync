"""실행기(sync.py) 조립 검증: 가짜 API 로 apply 경로를 돌려 본다. 네트워크 없음."""
from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sync as runner  # noqa: E402
from hrsync import notify  # noqa: E402

TODAY = date.today()
D1 = (TODAY + timedelta(days=10)).isoformat()


def fees(w=219000, m=69000, c=39000):
    return {"weekly_rent": w, "maintenance": m, "cleaning": c}


class FakeM33:
    def __init__(self, room=None, dates=None):
        self.room_data = fees() if room is None else room
        self.dates = dict(dates or {})
        self.action_ids = {}
        self.blocks = []
        self.fee_calls = []

    def room(self):
        return {"name": "가짜 삼삼엠투", "status": "REGISTERED", **self.room_data}

    def schedules(self):
        return {"dates": dict(self.dates), "contracts": []}

    def set_unavailable(self, start, end):
        d = start
        while d <= end:
            self.dates[d.isoformat()] = "disable"
            d += timedelta(days=1)
        self.blocks.append((start.isoformat(), end.isoformat()))

    def set_fees(self, **kw):
        self.fee_calls.append(kw)
        self.room_data.update({k: v for k, v in kw.items() if k in self.room_data})


class FakeZB:
    def __init__(self, item=None, cal=None, break_writes=False):
        self.item_data = fees() if item is None else item
        self.cal = dict(cal or {})
        self.break_writes = break_writes
        self.puts = []
        self.patches = []

    def item(self):
        return {"id": 2649, "name": "가짜 직방", "status": "PUBLISHED", **self.item_data}

    def calendar(self, first, last):
        return {"dates": dict(self.cal)}

    def set_unavailable(self, blocked):
        self.puts.append(dict(blocked))
        if not self.break_writes:
            self.cal = dict(blocked)

    def patch_pricing(self, item, **kw):
        self.patches.append(kw)
        if not self.break_writes:
            self.item_data.update({k: v for k, v in kw.items() if k in self.item_data})


class RunnerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.state = os.path.join(self.tmp, "state.json")
        self.session = os.path.join(self.tmp, "session.json")
        self.alerts = []
        self.closed = []
        self._orig = (runner.read_m33, runner.read_zb, notify.open_alert, notify.close_alerts)
        self._env = {k: os.environ.get(k) for k in ("ZB_COOKIE", "ZB_EMAIL", "ZB_PASSWORD")}
        os.environ["ZB_COOKIE"] = "dummy=1"
        notify.open_alert = lambda title, body, key=None: (self.alerts.append((title, body, key)) or (True, "test"))
        notify.close_alerts = lambda note="": (self.closed.append(note) or (0, "ok"))

    def tearDown(self):
        runner.read_m33, runner.read_zb, notify.open_alert, notify.close_alerts = self._orig
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def run_sync(self, m33, zb, mode="apply"):
        runner.read_m33 = lambda *a, **k: (m33, m33.room(), m33.schedules())
        runner.read_zb = lambda *a, **k: (zb, zb.item())
        argv = ["sync.py", "--mode", mode, "--state", self.state, "--session", self.session]
        old = sys.argv
        sys.argv = argv
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                return runner.main()
        finally:
            sys.argv = old

    def test_apply_blocks_missing_side(self):
        m33 = FakeM33(dates={D1: "booking"})
        zb = FakeZB()
        self.assertEqual(self.run_sync(m33, zb), 0)
        self.assertTrue(zb.puts and D1 in zb.puts[0], zb.puts)
        st = json.load(open(self.state, encoding="utf-8"))
        self.assertIn(D1, st["m33"]["dates"])

    def test_apply_pushes_price_change(self):
        m33 = FakeM33(room=fees(w=239000))
        zb = FakeZB()
        # 기준선 먼저
        self.run_sync(FakeM33(), FakeZB())
        self.assertEqual(self.run_sync(m33, zb), 0)
        self.assertEqual(zb.patches[-1]["weekly_rent"], 239000)

    def test_verification_failure_raises_alert(self):
        m33 = FakeM33(dates={D1: "booking"})
        zb = FakeZB(break_writes=True)   # 쓰기를 해도 달력이 안 바뀌는 상황
        # 예외가 이슈로 기록되면 실행은 성공(0)으로 끝난다 — 실패 메일 폭탄 방지
        self.assertEqual(self.run_sync(m33, zb), 0)
        self.assertTrue(self.alerts)

    def test_failure_to_record_alert_returns_error(self):
        notify.open_alert = lambda title, body, key=None: (False, "API 실패")
        m33 = FakeM33(dates={D1: "booking"})
        zb = FakeZB(break_writes=True)
        self.assertEqual(self.run_sync(m33, zb), 1)

    def test_clean_run_closes_open_alerts(self):
        m33 = FakeM33(dates={D1: "booking"})
        zb = FakeZB(cal={D1: "blocked"})
        self.run_sync(FakeM33(), FakeZB())      # 기준선
        self.assertEqual(self.run_sync(m33, zb), 0)
        self.assertEqual(self.alerts, [])

    def test_never_calls_unblock(self):
        # 직방에만 막힌 날: 삼삼엠투에 '막기'만 하고, 반대 방향 해제는 없다
        m33 = FakeM33()
        zb = FakeZB(cal={D1: "blocked"})
        self.run_sync(FakeM33(), FakeZB())
        self.assertEqual(self.run_sync(m33, zb), 0)
        self.assertEqual(m33.blocks, [(D1, D1)])
        self.assertEqual(zb.puts, [])

    def test_skips_zigbang_when_not_configured(self):
        os.environ.pop("ZB_COOKIE", None)
        os.environ.pop("ZB_EMAIL", None)
        os.environ.pop("ZB_PASSWORD", None)
        m33 = FakeM33()
        zb = FakeZB()
        self.assertEqual(self.run_sync(m33, zb), 0)
        self.assertTrue(any("직방 자격증명이 없어" in a[0] + a[1] for a in self.alerts))
        self.assertEqual(zb.puts, [])

    def test_dry_run_writes_nothing(self):
        m33 = FakeM33(dates={D1: "booking"})
        zb = FakeZB()
        self.run_sync(m33, zb, mode="dry-run")
        self.assertEqual(zb.puts, [])
        self.assertFalse(os.path.exists(self.state))


if __name__ == "__main__":
    unittest.main()
