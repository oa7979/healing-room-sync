"""직방(zigbang) 호스트 화면용 어댑터.

로그인   : POST https://account.zigbang.com/v2/user/token  {"email","password"}
           (직방 계정에 비밀번호가 설정돼 있어야 한다. 구글 로그인만 쓰는 계정이면
            비밀번호를 새로 만들거나 ZB_COOKIE 세션 주입을 써야 한다.)
읽기     : GET  https://stay-apis.zigbang.com/host/items/{id}
           GET  https://stay-apis.zigbang.com/host/items/{id}/calendar?year=&month=
예약불가 : PUT  https://stay-apis.zigbang.com/host/items/{id}/schedules
           {"unavailableDates":[{"startDate","endDate"}]}  ← 전체 목록 교체 방식
요금     : PATCH https://stay-apis.zigbang.com/host/items/{id}  {"pricing":{...}}
"""
from __future__ import annotations

import json
from datetime import date, timedelta

from .httpc import Client

ACC = "https://account.zigbang.com"
STAY = "https://stay-apis.zigbang.com"
ITEM_ID = 2649


class ZBError(RuntimeError):
    pass


class ZBAuthError(ZBError):
    pass


class ZBParseError(ZBError):
    pass


def _dig(d, *keys, default=None):
    if isinstance(d, dict):
        for k in keys:
            if k in d and d[k] is not None:
                return d[k]
    return default


def _unwrap(body: dict) -> dict:
    """{data:{...}} / {item:{...}} 같은 래퍼를 벗긴다."""
    for key in ("data", "item", "result"):
        if isinstance(body.get(key), dict):
            inner = body[key]
            if "weeklyRent" in inner or "id" in inner or "schedules" in inner:
                return inner
    return body


class ZB:
    def __init__(self, client: Client, item_id: int = ITEM_ID, email: str | None = None,
                 password: str | None = None, cookie: str | None = None):
        self.c = client
        self.item_id = item_id
        self.email = email
        self.password = password
        self.cookie = cookie
        self._logged_in = False
        self._token: str | None = None
        self._login_keys: list[str] = []

    def _stay_headers(self) -> dict[str, str]:
        hdrs = {
            "Origin": "https://www.zigbang.com",
            "Referer": "https://www.zigbang.com/host/item",
        }
        if self._token:
            hdrs["Authorization"] = f"Bearer {self._token}"
        return hdrs

    # ------------------------------------------------------------------ login
    def login(self) -> None:
        if self.cookie:
            self.c.set_cookie_header(self.cookie)
            self._logged_in = True
            return
        if not (self.email and self.password):
            raise ZBAuthError("직방 자격증명이 없습니다 (ZB_EMAIL/ZB_PASSWORD 또는 ZB_COOKIE)")
        r = self.c.post(
            ACC + "/v2/user/token",
            json_body={"email": self.email, "password": self.password},
            headers={"Origin": ACC, "Referer": ACC + "/login"},
        )
        if r.status in (400, 401, 403):
            raise ZBAuthError(f"직방 로그인 거부됨 (HTTP {r.status}: {r.body[:200]})")
        if r.status not in (200, 201):
            raise ZBAuthError(f"직방 로그인 실패 (HTTP {r.status})")
        try:
            body = r.json()
            if isinstance(body, dict):
                self._login_keys = list(body.keys())
                for container in (body, body.get("data"), body.get("result")):
                    if isinstance(container, dict):
                        tok = _dig(container, "accessToken", "access_token", "token", "jwt", "secure_token")
                        if tok:
                            self._token = str(tok)
                            break
        except ValueError:
            pass
        self._logged_in = True

    def ensure_login(self) -> None:
        if not self._logged_in:
            self.login()

    # ------------------------------------------------------------------ reads
    def item(self) -> dict:
        self.ensure_login()
        r = self.c.get(f"{STAY}/host/items/{self.item_id}", headers=self._stay_headers())
        if r.status in (401, 403):
            raise ZBAuthError(
                f"직방 세션이 만료됐습니다 (HTTP {r.status}; "
                f"has_token={bool(self._token)}, keys={self._login_keys}, "
                f"cookies={list(self.c.cookies('zigbang.com').keys())})"
            )
        if r.status != 200:
            raise ZBError(f"직방 매물 정보를 읽지 못했습니다 (HTTP {r.status})")
        d = _unwrap(r.json())
        absent_manage = bool(_dig(d, "isWeeklyManageCostAbsent", "managementFeeFree", default=False))
        no_clean = bool(_dig(d, "isCleaningFeeIncluded", "cleaningFeeFree", default=False))
        fees = {
            "weekly_rent": _dig(d, "weeklyRent", "weeklyRate"),
            "maintenance": None if absent_manage else _dig(d, "weeklyManageCost", "managementFee"),
            "cleaning": None if no_clean else _dig(d, "cleaningFee", "cleaningFeeOnce"),
        }
        if fees["weekly_rent"] is None:
            raise ZBParseError("직방 응답에서 주당 요금(weeklyRent)을 찾지 못했습니다: "
                               + json.dumps(d, ensure_ascii=False)[:400])
        return {
            "id": _dig(d, "id", "itemId", default=self.item_id),
            "name": _dig(d, "title", "name"),
            "weekly_rent": int(fees["weekly_rent"]),
            "maintenance": None if fees["maintenance"] is None else int(fees["maintenance"]),
            "cleaning": None if fees["cleaning"] is None else int(fees["cleaning"]),
            "status": _dig(d, "status"),
            "raw": d,
        }

    def calendar(self, first: date, last: date) -> dict:
        """{'dates': {YYYY-MM-DD: 'blocked'|'contracted'}} — first~last 구간.

        직방은 year·month 를 생략하면 '오늘부터 1년'을 한 번에 준다(문서화된 파라미터 설명).
        한 번에 못 받으면 월별로 나눠 조회한다."""
        self.ensure_login()
        hdrs = self._stay_headers()
        dates: dict[str, str] = {}
        parsed_ok = False

        r = self.c.get(f"{STAY}/host/items/{self.item_id}/calendar", headers=hdrs)
        if r.status in (401, 403):
            raise ZBAuthError(f"직방 세션이 만료됐습니다 (HTTP {r.status})")
        if r.status == 200:
            try:
                parsed = self._parse_calendar(r.json())
                if parsed is not None:
                    parsed_ok = True
                    dates = parsed
            except ValueError:
                pass

        if not dates:  # 한 번에 못 받으면 월별 조회
            cur = date(first.year, first.month, 1)
            while cur <= last:
                rr = self.c.get(
                    f"{STAY}/host/items/{self.item_id}/calendar?year={cur.year}&month={cur.month}",
                    headers=hdrs)
                if rr.status in (401, 403):
                    raise ZBAuthError(f"직방 세션이 만료됐습니다 (HTTP {rr.status})")
                if rr.status != 200:
                    raise ZBError(f"직방 달력을 읽지 못했습니다 {cur.year}-{cur.month:02d} (HTTP {rr.status})")
                parsed = self._parse_calendar(rr.json())
                if parsed is not None:
                    parsed_ok = True
                    dates.update(parsed)
                cur = (cur + timedelta(days=32)).replace(day=1)

        if not dates and not parsed_ok:
            raise ZBParseError("직방 달력 응답에서 예약 상태를 찾지 못했습니다")
        return {"dates": {k: v for k, v in dates.items() if first.isoformat() <= k <= last.isoformat()}}

    @staticmethod
    def _parse_calendar(body) -> dict:
        def norm(v: str | None) -> str | None:
            if not v:
                return None
            v = str(v).upper()
            if "BLOCK" in v or "UNAVAILABLE" in v or "CLOSE" in v:
                return "blocked"
            if "CONTRACT" in v or "RESERV" in v or "BOOK" in v or "OCCUP" in v:
                return "contracted"
            return None

        out: dict[str, str] = {}
        root = body
        for key in ("data", "result", "calendar"):
            if isinstance(root, dict) and isinstance(root.get(key), (dict, list)):
                root = root[key]
                break
        rows = None
        default_kind = None
        if isinstance(root, list):
            rows = root
        elif isinstance(root, dict):
            for key in ("schedules", "days", "dates", "items", "blockedDates", "unavailableDates"):
                if isinstance(root.get(key), list):
                    rows = root[key]
                    if key in ("blockedDates", "unavailableDates"):
                        default_kind = "blocked"
                    break
        if rows is None:
            return None
        for row in rows:
            if not isinstance(row, dict):
                continue
            kind = norm(_dig(row, "type", "status", "scheduleType", "state"))
            if not kind and isinstance(row.get("scheduleTypes"), list):
                types = [norm(t) for t in row["scheduleTypes"]]
                if "contracted" in types:
                    kind = "contracted"
                elif "blocked" in types:
                    kind = "blocked"
            kind = kind or default_kind
            s = _dig(row, "startDate", "start", "date")
            e = _dig(row, "endDate", "end", "date") or s
            if not kind or not s:
                continue
            d0, d1 = date.fromisoformat(str(s)[:10]), date.fromisoformat(str(e)[:10])
            cur = d0
            while cur <= d1:
                out[cur.isoformat()] = kind
                cur += timedelta(days=1)
        return out

    # ------------------------------------------------------------------ writes
    def set_unavailable(self, blocked: dict[str, str]) -> None:
        """blocked: {YYYY-MM-DD: 'blocked'|'contracted'} — 이 전체 목록으로 교체한다.
        (직방은 목록 전체 교체 방식이라, 기존 차단을 지우지 않으려면 합집합을 넘겨야 한다.)"""
        self.ensure_login()
        ranges = _to_ranges(sorted(blocked))
        r = self.c.request(
            "PUT",
            f"{STAY}/host/items/{self.item_id}/schedules",
            json_body={"unavailableDates": [{"startDate": a, "endDate": b} for a, b in ranges]},
            headers=self._stay_headers(),
        )
        if r.status in (401, 403):
            raise ZBAuthError(f"직방 세션이 만료됐습니다 (HTTP {r.status})")
        if r.status not in (200, 201, 204):
            raise ZBError(f"직방 예약불가 저장 실패 (HTTP {r.status}: {r.body[:200]})")

    def patch_pricing(self, item: dict, weekly_rent: int | None = None,
                      maintenance: int | None = None, cleaning: int | None = None) -> None:
        raw = item["raw"]
        pricing = {
            "weeklyRent": int(weekly_rent if weekly_rent is not None else item["weekly_rent"]),
            "isWeeklyManageCostAbsent": (_dig(raw, "isWeeklyManageCostAbsent", "managementFeeFree",
                                              default=False)
                                         if maintenance is None else False),
            "weeklyManageCost": int(maintenance if maintenance is not None
                                    else (item["maintenance"] or 0)),
            "isCleaningFeeIncluded": (_dig(raw, "isCleaningFeeIncluded", "cleaningFeeFree", default=False)
                                      if cleaning is None else False),
            "cleaningFee": int(cleaning if cleaning is not None else (item["cleaning"] or 0)),
        }
        r = self.c.request(
            "PATCH",
            f"{STAY}/host/items/{self.item_id}",
            json_body={"pricing": pricing},
            headers=self._stay_headers(),
        )
        if r.status in (401, 403):
            raise ZBAuthError(f"직방 세션이 만료됐습니다 (HTTP {r.status})")
        if r.status not in (200, 201, 204):
            raise ZBError(f"직방 요금 저장 실패 (HTTP {r.status}: {r.body[:200]})")


def _to_ranges(days: list[str]) -> list[tuple[str, str]]:
    out: list[list[str]] = []
    for s in days:
        d = date.fromisoformat(s)
        if out and (d - date.fromisoformat(out[-1][1])).days == 1:
            out[-1][1] = s
        else:
            out.append([s, s])
    return [(a, b) for a, b in out]
