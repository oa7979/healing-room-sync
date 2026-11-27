#!/usr/bin/env python3
"""Apps Script 캘린더 웹훅 연결 점검 (수동 실행용).

  python3 tools/webhook_smoke_test.py            # ping + list 만 (읽기)
  python3 tools/webhook_smoke_test.py --write    # 테스트 일정 생성/갱신까지 (뒤에서 삭제 필요)

웹훅 URL·시크릿은 환경변수 GCAL_WEBHOOK_URL / GCAL_WEBHOOK_SECRET 로 받는다.
저장소에는 어떤 값도 남기지 않는다.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

URL = os.environ.get("GCAL_WEBHOOK_URL", "")
SECRET = os.environ.get("GCAL_WEBHOOK_SECRET", "")


def post(payload: dict) -> dict:
    if not URL:
        raise SystemExit("GCAL_WEBHOOK_URL 이 없습니다.")
    body = json.dumps(payload).encode()
    req = urllib.request.Request(URL, data=body, method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read().decode("utf-8", "ignore"))
    except urllib.error.HTTPError as e:
        return {"ok": False, "http": e.code, "body": e.read().decode("utf-8", "ignore")[:300]}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--start", default="2026-11-29")
    ap.add_argument("--end", default="2027-02-03")
    args = ap.parse_args()

    bad = post({"action": "ping", "secret": "wrong-secret"})
    print("1) 잘못된 시크릿:", json.dumps(bad, ensure_ascii=False)[:200])

    ok = post({"action": "ping", "secret": SECRET})
    print("2) 정상 ping:", json.dumps(ok, ensure_ascii=False)[:200])

    listed = post({"action": "list", "secret": SECRET,
                   "start_date": args.start, "end_date_exclusive": args.end})
    print(f"3) 기간 조회 ({args.start}~{args.end}):", json.dumps(listed, ensure_ascii=False)[:600])

    if args.write:
        payload = {"secret": SECRET, "action": "upsert", "key": "zz-test:creates-once",
                   "title": "[직방] 용산힐링룸 (웹훅 테스트)",
                   "start_date": "2030-03-01", "end_date_exclusive": "2030-03-03",
                   "color_id": "3", "location": "테스트",
                   "description": "웹훅 동작 확인용. 자동으로 지워집니다."}
        first = post(payload)
        print("4) 첫 upsert:", json.dumps(first, ensure_ascii=False)[:400])
        second = post(payload)
        print("5) 같은 키 다시 upsert(중복 생성 없어야 함):", json.dumps(second, ensure_ascii=False)[:400])
        listed2 = post({"action": "list", "secret": SECRET,
                        "start_date": "2030-03-01", "end_date_exclusive": "2030-03-03"})
        events = [e for e in (listed2.get("events") or [])
                  if "[hrsync-key:zz-test:creates-once]" in str(e.get("description") or "")]
        print(f"6) 같은 키 일정 개수: {len(events)}")
        for e in events:
            print("   ", json.dumps(e, ensure_ascii=False)[:300])
    return 0


if __name__ == "__main__":
    sys.exit(main())
