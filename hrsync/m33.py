"""삼삼엠투(33m2) 호스트 화면용 어댑터.

공개 API가 없어서 웹앱이 실제로 쓰는 두 가지 통로를 그대로 쓴다.
  1) 읽기 : Next.js 서버가 노출하는 /v1/use-auth/... REST 엔드포인트
            (세션 쿠키 __Secure-session-token 필요)
  2) 쓰기 : Next.js 서버 액션(POST + Next-Action 헤더 + RSC 본문)

로그인은 로그인 화면 자체가 쓰는 서버 액션(signIn)에 이메일/비밀번호를 보내면
__Secure-session-token 쿠키가 발급되는 방식이다. 별도 인증 서버 호출이 없다.
서버 액션 ID는 배포 때마다 바뀌므로, 호출이 실패하면 JS 번들에서 ID를 다시
찾아낸다(_discover).
"""
from __future__ import annotations

import json
import re
import time
from datetime import date, datetime, timedelta, timezone

from .httpc import Client

BASE = "https://web.33m2.co.kr"
ROOM_ID = 93770
KST = timezone(timedelta(hours=9))

# 2026-11-20 배포 기준 ID. 값이 틀리면 _discover 가 새로 찾는다.
DEFAULT_ACTION_IDS = {
    "signIn": "7f57b50f3cdf6f3d306c1dede3730e44df51d5d1e2",
    "registerUnavailableSchedule": "7f5fd4e93f10202c1074bc5ae010c6439628cdb8fc",
    "deleteUnavailableSchedule": "7f9c371d9aae08f5f07c907d603cd350effcf80a61",
    "updateRoomFeeInfo": "7fde127908d9046aea8fba903d385114a7bd2026b1",
}

ACTION_PAGES = {
    "signIn": "/sign-in",
    "registerUnavailableSchedule": "/host/room/%d/schedule" % ROOM_ID,
    "deleteUnavailableSchedule": "/host/room/%d/schedule" % ROOM_ID,
    "updateRoomFeeInfo": "/host/room/%d/update?step=2" % ROOM_ID,
}

STATUS_BOOKED = "booking"   # 계약완료
STATUS_BLOCKED = "disable"  # 임대인이 막은 날(계약 불가능)

_FEE_KEYS = ("usingFee", "maintenanceFeeInfo", "discountInfo", "refundPolicy", "propertyType")


class M33Error(RuntimeError):
    pass


class AuthError(M33Error):
    pass


class M33:
    def __init__(self, client: Client, email: str, password: str, room_id: int = ROOM_ID,
                 action_ids: dict | None = None, session: str | None = None,
                 on_session=None):
        self.c = client
        self.email = email
        self.password = password
        self.room_id = room_id
        self.action_ids = dict(action_ids or DEFAULT_ACTION_IDS)
        self._logged_in = False
        self.on_session = on_session
        if session:
            # 이전 실행에서 저장해 둔 세션 쿠키 재사용 (로그인 횟수를 줄인다)
            self.c.set_cookie_header("__Secure-session-token=" + session)
            self._logged_in = True

    # ------------------------------------------------------------------ login
    def login(self) -> None:
        if not self.email or not self.password:
            raise AuthError("삼삼엠투 아이디/비밀번호가 설정되지 않았습니다 (M33_EMAIL/M33_PASSWORD)")
        page = self.c.get(BASE + "/sign-in")
        if page.status != 200:
            raise AuthError(f"로그인 화면을 열지 못했습니다 (HTTP {page.status})")
        body = [{"username": self.email, "password": self.password, "rememberMe": True}]
        resp = None
        for attempt in range(3):
            resp = self._action("signIn", body, page_path="/sign-in")
            token = self.c.cookies("33m2.co.kr").get("__Secure-session-token")
            if token:
                self._logged_in = True
                if self.on_session:
                    self.on_session(token)
                return
            # COMN_003 = '처리중입니다' (바로 앞 로그인과 겹칠 때). 잠깐 기다렸다 다시.
            if "COMN_003" in (resp.body or "") and attempt < 2:
                time.sleep(15)
                continue
            break
        raise AuthError("로그인 응답에 세션 쿠키가 없습니다 (아이디/비밀번호 확인 필요"
                         + (", 서버: 처리중(COMN_003)" if resp and "COMN_003" in resp.body else "") + ")")

    def ensure_login(self) -> None:
        if not self._logged_in:
            self.login()

    # ------------------------------------------------------------------ reads
    def room(self) -> dict:
        self.ensure_login()
        r = self.c.get(f"{BASE}/v1/use-auth/host/rooms/{self.room_id}")
        if r.status == 401:
            self.login()
            r = self.c.get(f"{BASE}/v1/use-auth/host/rooms/{self.room_id}")
        if r.status != 200:
            raise M33Error(f"매물 정보를 읽지 못했습니다 (HTTP {r.status})")
        d = r.json()["data"]
        mf = d.get("maintenanceFeeInfo") or {}
        return {
            "name": d.get("roomName"),
            "weekly_rent": d.get("usingFee"),
            "maintenance": mf.get("managementFee"),
            "cleaning": mf.get("cleanFee"),
            "deposit_included": None,
            "status": d.get("roomStatus"),
            "raw": d,
        }

    def schedules(self) -> dict:
        """{'dates': {YYYY-MM-DD: 'booking'|'disable'}, 'contracts': [...]}"""
        self.ensure_login()
        r = self.c.get(f"{BASE}/v1/use-auth/host/rooms/{self.room_id}/schedules")
        if r.status == 401:
            self.login()
            r = self.c.get(f"{BASE}/v1/use-auth/host/rooms/{self.room_id}/schedules")
        if r.status != 200:
            raise M33Error(f"일정을 읽지 못했습니다 (HTTP {r.status})")
        data = r.json()["data"]
        dates = {}
        for row in data.get("schedules") or []:
            st = row.get("status")
            if st in (STATUS_BOOKED, STATUS_BLOCKED):
                dates[row["date"]] = st
        return {"dates": dates, "contracts": data.get("contracts") or []}

    # ----------------------------------------------------------------- writes
    def set_unavailable(self, start: date, end: date) -> None:
        """start~end(포함)를 '계약 불가능'으로 막는다."""
        self.ensure_login()
        body = [{"roomId": self.room_id,
                 "body": {"startDate": start.isoformat(), "endDate": end.isoformat()}}]
        resp = self._action("registerUnavailableSchedule", body,
                            page_path=ACTION_PAGES["registerUnavailableSchedule"])
        if resp.status != 200:
            raise M33Error(f"차단 요청 실패 (HTTP {resp.status})")

    def delete_unavailable(self, start: date, end: date) -> None:
        """수동 차단을 푼다.  ※ 동기화 프로그램은 이 함수를 자동으로 쓰지 않는다.
        사용자가 직접 지시했을 때만 쓰는 수동 도구다."""
        self.ensure_login()
        body = [{"roomId": self.room_id,
                 "body": {"startDate": start.isoformat(), "endDate": end.isoformat()}}]
        resp = self._action("deleteUnavailableSchedule", body,
                            page_path=ACTION_PAGES["deleteUnavailableSchedule"])
        if resp.status != 200:
            raise M33Error(f"차단 해제 요청 실패 (HTTP {resp.status})")

    def set_fees(self, weekly_rent: int | None = None, maintenance: int | None = None,
                 cleaning: int | None = None) -> None:
        """변경할 항목만 넘긴다. 나머지는 현재 값 그대로 다시 보낸다."""
        self.ensure_login()
        room = self.room()["raw"]
        mf = room.get("maintenanceFeeInfo") or {}
        disc = room.get("discountInfo") or {}
        payload = {
            "usingFee": int(weekly_rent if weekly_rent is not None else room["usingFee"]),
            "longTermDiscounts": [
                {"weeks": d["weeks"], "discountRate": d["discountRate"]}
                for d in disc.get("longTermDiscounts") or []
            ],
            "earlyCheckinDiscounts": [
                {"days": d.get("days"), "discountAmount": d.get("discountAmount")}
                for d in disc.get("earlyCheckinDiscounts") or []
            ],
            "managementFee": int(maintenance if maintenance is not None else mf.get("managementFee") or 0),
            "includeElectricity": bool(mf.get("includeElectricity")),
            "includeWater": bool(mf.get("includeWater")),
            "includeGas": bool(mf.get("includeGas")),
            "managementFeeDescription": mf.get("managementFeeDescription") or "",
            "cleanFee": int(cleaning if cleaning is not None else mf.get("cleanFee") or 0),
            "refundPolicy": room.get("refundPolicy"),
            "propertyType": room.get("propertyType"),
        }
        body = [{"roomId": self.room_id, "body": payload}]
        resp = self._action("updateRoomFeeInfo", body, page_path=ACTION_PAGES["updateRoomFeeInfo"])
        if resp.status != 200:
            raise M33Error(f"요금 저장 실패 (HTTP {resp.status})")

    # ----------------------------------------------------------------- server actions
    def _action(self, name: str, args, page_path: str):
        aid = self.action_ids[name]
        resp = self._post_action(aid, args, page_path)
        if resp.status in (200, 201):
            return resp
        # 배포로 액션 ID가 바뀐 경우 다시 찾아서 한 번만 재시도
        fresh = self._discover(name, page_path)
        if fresh and fresh != aid:
            self.action_ids[name] = fresh
            return self._post_action(fresh, args, page_path)
        return resp

    def _post_action(self, action_id: str, args, page_path: str):
        return self.c.post(
            BASE + page_path,
            data=json.dumps(args, ensure_ascii=False).encode("utf-8"),
            headers={
                "Next-Action": action_id,
                "Content-Type": "text/plain;charset=UTF-8",
                "Accept": "text/x-component",
                "Origin": BASE,
                "Referer": BASE + page_path,
            },
        )

    def _discover(self, name: str, page_path: str) -> str | None:
        """페이지가 불러오는 JS 번들에서 createServerReference("<id>",...,"<name>") 를 찾는다."""
        page = self.c.get(BASE + page_path)
        if page.status != 200:
            return None
        chunks = set(re.findall(r'https://[a-z0-9.]*cloudfront\.net[^"\']+\.js', page.body))
        pat = re.compile(r'"([0-9a-f]{30,60})"[^)]{0,300}?"' + re.escape(name) + r'"')
        for url in sorted(chunks):
            js = self.c.get(url)
            if js.status != 200:
                continue
            m = pat.search(js.body)
            if m:
                return m.group(1)
        return None
