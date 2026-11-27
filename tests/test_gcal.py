"""캘린더 동기화 모듈(hrsync/gcal.py) 단위 테스트. 네트워크 없음."""
from __future__ import annotations

import os
import sys
import unittest
from datetime import date, datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hrsync import gcal  # noqa: E402

TODAY = date(2027, 1, 10)


def days(start: str, end: str, status: str) -> dict:
    a, b = date.fromisoformat(start), date.fromisoformat(end)
    out = {}
    d = a
    while d <= b:
        out[d.isoformat()] = status
        d += timedelta(days=1)
    return out


class SegmentsTest(unittest.TestCase):
    def test_groups_consecutive_days(self):
        got = gcal.segments(days("2027-02-01", "2027-02-05", "contracted"), ("contracted",),
                            today=TODAY, horizon=date(2027, 3, 1))
        self.assertEqual(got, [("2027-02-01", "2027-02-05")])

    def test_splits_on_gap(self):
        dates = days("2027-02-01", "2027-02-03", "contracted")
        dates.update(days("2027-02-06", "2027-02-07", "contracted"))
        got = gcal.segments(dates, ("contracted",), today=TODAY, horizon=date(2027, 3, 1))
        self.assertEqual(got, [("2027-02-01", "2027-02-03"), ("2027-02-06", "2027-02-07")])

    def test_drops_finished_segments(self):
        dates = days("2027-01-01", "2027-01-05", "booking")
        self.assertEqual(gcal.segments(dates, ("booking",), today=TODAY,
                                       horizon=date(2027, 3, 1)), [])

    def test_keeps_ongoing_segment_with_real_start(self):
        dates = days("2027-01-08", "2027-01-12", "booking")
        self.assertEqual(gcal.segments(dates, ("booking",), today=TODAY,
                                       horizon=date(2027, 3, 1)),
                         [("2027-01-08", "2027-01-12")])

    def test_ignores_other_statuses_and_bad_keys(self):
        dates = {"2027-02-01": "disable", "nope": "booking",
                 "2027-02-03": "booking"}
        self.assertEqual(gcal.segments(dates, ("booking",), today=TODAY,
                                       horizon=date(2027, 3, 1)),
                         [("2027-02-03", "2027-02-03")])


class GuestNameTest(unittest.TestCase):
    def test_finds_name_in_segment(self):
        contracts = [{"startDate": "2027-01-26T00:00:00.000Z", "endDate": "2027-02-01",
                      "guestName": "송요한"}]
        self.assertEqual(gcal.guest_name(contracts, "2027-01-26", "2027-02-01"), "송요한")

    def test_returns_none_when_unknown(self):
        self.assertIsNone(gcal.guest_name(None, "2027-01-26", "2027-02-01"))
        self.assertIsNone(gcal.guest_name([{"startDate": "2027-03-01", "name": "남남"}],
                                          "2027-01-26", "2027-02-01"))
        self.assertIsNone(gcal.guest_name(["이상한 값"], "2027-01-26", "2027-02-01"))


class BuildEventsTest(unittest.TestCase):
    def test_zb_gets_purple_and_m33_gets_blue(self):
        events = gcal.build_events(
            m33_dates=days("2027-01-26", "2027-02-01", "booking"),
            zb_dates=days("2027-02-10", "2027-02-12", "contracted"),
            today=TODAY, horizon=date(2027, 12, 31))
        by_key = {ev["key"]: ev for ev in events}
        self.assertIn("zb:2027-02-10:2027-02-12", by_key)
        self.assertIn("m33:2027-01-26:2027-02-01", by_key)
        self.assertEqual(by_key["zb:2027-02-10:2027-02-12"]["color_id"], "3")
        self.assertEqual(by_key["zb:2027-02-10:2027-02-12"]["title"], "[직방] 용산힐링룸")
        self.assertEqual(by_key["m33:2027-01-26:2027-02-01"]["color_id"], "9")
        self.assertEqual(by_key["m33:2027-01-26:2027-02-01"]["title"], "[삼삼엠투] 용산힐링룸")

    def test_end_is_exclusive_and_description_holds_platform(self):
        ev = gcal.build_events(zb_dates=days("2027-02-10", "2027-02-12", "contracted"),
                               today=TODAY, horizon=date(2027, 12, 31))[0]
        self.assertEqual(ev["start_date"], "2027-02-10")
        self.assertEqual(ev["end_date_exclusive"], "2027-02-13")
        self.assertIn("직방", ev["description"])
        self.assertIn("2027-02-12", ev["description"])
        self.assertEqual(ev["location"], gcal.LOCATION)

    def test_guest_name_in_title(self):
        contracts = [{"startDate": "2027-01-26", "guestName": "송요한"}]
        events = gcal.build_events(m33_dates=days("2027-01-26", "2027-02-01", "booking"),
                                   contracts=contracts, today=TODAY,
                                   horizon=date(2027, 12, 31))
        self.assertEqual(events[0]["title"], "[삼삼엠투] 용산힐링룸 (송요한 님)")

    def test_finished_booking_makes_no_event(self):
        events = gcal.build_events(m33_dates=days("2026-12-01", "2026-12-05", "booking"),
                                   today=TODAY, horizon=date(2027, 12, 31))
        self.assertEqual(events, [])


class PendingTest(unittest.TestCase):
    def setUp(self):
        self.ev = gcal.build_events(zb_dates=days("2027-02-10", "2027-02-12", "contracted"),
                                    today=TODAY, horizon=date(2027, 12, 31))[0]
        self.now = datetime(2027, 1, 10, 12, 0)

    def test_unchanged_and_fresh_is_skipped(self):
        applied = {self.ev["key"]: {"sig": self.ev["sig"], "at": "2027-01-10T09:00:00"}}
        self.assertEqual(gcal.pending([self.ev], applied, self.now), [])

    def test_unchanged_but_stale_is_resent(self):
        applied = {self.ev["key"]: {"sig": self.ev["sig"], "at": "2027-01-08T09:00:00"}}
        self.assertEqual(gcal.pending([self.ev], applied, self.now), [self.ev])

    def test_changed_content_is_resent(self):
        applied = {self.ev["key"]: {"sig": "old", "at": "2027-01-10T09:00:00"}}
        self.assertEqual(gcal.pending([self.ev], applied, self.now), [self.ev])

    def test_new_key_is_sent(self):
        self.assertEqual(gcal.pending([self.ev], {}, self.now), [self.ev])


class SendTest(unittest.TestCase):
    def test_missing_credentials_raises(self):
        with self.assertRaises(gcal.GcalError):
            gcal.send("", "", {"key": "k"})

    def test_check_result_accepts_matching_response(self):
        ev = {"start_date": "2027-02-10", "end_date_exclusive": "2027-02-13"}
        data = {"ok": True, "result": {"event_id": "x", "start": "2027-02-10",
                                       "end": "2027-02-13"}}
        self.assertIsNone(gcal.check_result(ev, data))

    def test_check_result_accepts_extended_manual_event(self):
        ev = {"start_date": "2027-02-10", "end_date_exclusive": "2027-02-13"}
        data = {"ok": True, "result": {"event_id": "x", "start": "2027-02-10",
                                       "end": "2027-02-15"}}
        self.assertIsNone(gcal.check_result(ev, data))

    def test_check_result_flags_short_end(self):
        ev = {"start_date": "2027-02-10", "end_date_exclusive": "2027-02-13"}
        data = {"ok": True, "result": {"event_id": "x", "start": "2027-02-10",
                                       "end": "2027-02-12"}}
        self.assertTrue(gcal.check_result(ev, data))

    def test_check_result_flags_missing_id(self):
        ev = {"start_date": "2027-02-10", "end_date_exclusive": "2027-02-13"}
        self.assertTrue(gcal.check_result(ev, {"ok": True, "result": {}}))

    def test_check_result_flags_wrong_start(self):
        ev = {"start_date": "2027-02-10", "end_date_exclusive": "2027-02-13"}
        data = {"ok": True, "result": {"event_id": "x", "start": "2027-02-11",
                                       "end": "2027-02-13"}}
        self.assertTrue(gcal.check_result(ev, data))


if __name__ == "__main__":
    unittest.main()
