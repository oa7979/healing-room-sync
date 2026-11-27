"""예약 구간을 Google 캘린더(사용자의 Apps Script 웹앱)에 종일 일정으로 반영한다.

  · 직방 contracted 연속 구간   →  `[직방] 용산힐링룸`            · 보라(colorId 3)
  · 삼삼엠투 booking 연속 구간  →  `[삼삼엠투] 용산힐링룸 (입주자)` · 파랑(colorId 9)

실제 캘린더 쓰기는 직접 하지 않고 웹앱(용산힐링룸 캘린더 웹훅)에 JSON 을 POST 한다.
웹앱이 description 의 `[hrsync-key:KEY]` 표식으로 같은 일정을 찾아 갱신하므로
여러 번 보내도 일정이 겹쳐 생기지 않는다. 이미 사람이 손으로 만들어 둔 같은 플랫폼
일정이 있으면 그 일정에 표식만 붙이고 제목·색은 건드리지 않는다.

여기 있는 함수는 값만 만들고 네트워크는 send() 하나만 쓴다(나머지는 순수 함수).
"""
from __future__ import annotations

import hashlib
import json
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta

LOCATION = "서울 용산구 효창원로104길 38 401호 (용산힐링룸)"
TITLE_ZB = "[직방] 용산힐링룸"
TITLE_M33 = "[삼삼엠투] 용산힐링룸"
COLOR_ZB = "3"   # 보라(Grape)
COLOR_M33 = "9"  # 파랑(Blue)
REFRESH_HOURS = 24   # 같은 내용도 하루에 한 번은 다시 보내 캘린더에서 지워진 일정을 되살린다

M33_BOOKED = "booking"
ZB_CONTRACTED = "contracted"

# 삼삼엠투 계약 응답의 필드 이름은 배포에 따라 달라질 수 있어 후보를 넓게 둔다.
NAME_KEYS = ("guestName", "guest_name", "userName", "user_name", "customerName",
             "tenantName", "name")
START_KEYS = ("startDate", "start_date", "checkIn", "checkInDate", "start", "from",
              "moveInDate")
END_KEYS = ("endDate", "end_date", "checkOut", "checkOutDate", "end", "to",
            "moveOutDate")


class GcalError(RuntimeError):
    pass


def _first(row: dict, keys: tuple[str, ...]):
    for k in keys:
        v = row.get(k)
        if v not in (None, ""):
            return v
    return None


def _day(value) -> str | None:
    """'2027-01-26', '2027-01-26T00:00:00.000Z' 같은 값을 YYYY-MM-DD 로."""
    if value in (None, ""):
        return None
    s = str(value)
    if len(s) >= 10 and s[4] == "-" and s[7] == "-":
        return s[:10]
    return None


def guest_name(contracts, start_iso: str, end_iso: str) -> str | None:
    """구간 안에서 시작하는 계약의 입주자 이름. 못 찾으면 None(제목에 이름 없이)."""
    for row in contracts or []:
        if not isinstance(row, dict):
            continue
        name = _first(row, NAME_KEYS)
        s = _day(_first(row, START_KEYS))
        if not name or not s:
            continue
        if start_iso <= s <= end_iso:
            return str(name).strip()
    return None


def segments(dates: dict, statuses: tuple[str, ...], today: date, horizon: date) -> list[tuple[str, str]]:
    """상태가 일치하는 날짜를 연속 구간으로 묶는다. 이미 끝난 구간은 빼고,
    오늘 이후로 이어지는 구간은 실제 숙박 기간 그대로 돌려준다."""
    days = []
    for d, st in (dates or {}).items():
        if st not in statuses:
            continue
        try:
            day = date.fromisoformat(d)
        except ValueError:
            continue
        if day <= horizon:
            days.append(day)
    out: list[list[date]] = []
    for d in sorted(days):
        if out and (d - out[-1][1]).days == 1:
            out[-1][1] = d
        else:
            out.append([d, d])
    return [(a.isoformat(), b.isoformat()) for a, b in out if b >= today]


def _event(prefix: str, platform: str, start_iso: str, last_iso: str,
           title: str, color_id: str) -> dict:
    start = date.fromisoformat(start_iso)
    last = date.fromisoformat(last_iso)
    if last < start:
        raise ValueError(f"bad segment: {start_iso}~{last_iso}")
    end_excl = last + timedelta(days=1)
    ev = {
        "key": f"{prefix}:{start_iso}:{last_iso}",
        "title": title,
        "start_date": start_iso,
        "end_date_exclusive": end_excl.isoformat(),
        "color_id": color_id,
        "location": LOCATION,
        "description": (f"플랫폼: {platform}\n"
                        f"숙박 기간: {start_iso} ~ {last_iso} "
                        f"({end_excl.isoformat()}부터 예약 가능)"),
    }
    ev["sig"] = signature(ev)
    return ev


def signature(ev: dict) -> str:
    raw = "|".join(str(ev.get(k, "")) for k in
                   ("title", "start_date", "end_date_exclusive", "color_id", "location"))
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def build_events(m33_dates: dict | None = None, zb_dates: dict | None = None,
                 contracts=None, today: date | None = None,
                 horizon: date | None = None) -> list[dict]:
    """양쪽 예약 상태에서 캘린더에 있어야 할 일정 목록을 만든다(네트워크 없음)."""
    today = today or date.today()
    horizon = horizon or (today + timedelta(days=400))
    events: list[dict] = []

    for a, b in segments(zb_dates or {}, (ZB_CONTRACTED,), today, horizon):
        events.append(_event("zb", "직방 (item 2649)", a, b, TITLE_ZB, COLOR_ZB))

    for a, b in segments(m33_dates or {}, (M33_BOOKED,), today, horizon):
        name = guest_name(contracts, a, b)
        title = f"{TITLE_M33} ({name} 님)" if name else TITLE_M33
        events.append(_event("m33", "삼삼엠투 (room 93770)", a, b, title, COLOR_M33))

    return events


def _fresh(at: str | None, now: datetime, hours: int) -> bool:
    if not at:
        return False
    try:
        return (now - datetime.fromisoformat(at)) < timedelta(hours=hours)
    except (TypeError, ValueError):
        return False


def pending(events: list[dict], applied: dict | None, now: datetime,
            refresh_hours: int = REFRESH_HOURS) -> list[dict]:
    """이번 회차에 실제로 보낼 것만 고른다(같은 내용을 10분마다 다시 보내지 않는다)."""
    applied = applied or {}
    out = []
    for ev in events:
        prev = applied.get(ev["key"]) or {}
        if prev.get("sig") == ev["sig"] and _fresh(prev.get("at"), now, refresh_hours):
            continue
        out.append(ev)
    return out


def send(url: str, secret: str, event: dict, timeout: int = 30) -> dict:
    """웹앱으로 upsert 1건 전송. 응답 dict 를 돌려주고, 실패하면 GcalError."""
    if not url or not secret:
        raise GcalError("웹훅 URL/시크릿이 없습니다")
    payload = {"secret": secret, "action": "upsert"}
    payload.update({k: event[k] for k in ("key", "title", "start_date",
                                          "end_date_exclusive", "color_id",
                                          "location", "description")})
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(), method="POST",
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read().decode("utf-8", "ignore")
    except urllib.error.HTTPError as e:
        raise GcalError(f"HTTP {e.code}: {e.read().decode('utf-8', 'ignore')[:200]}") from e
    except Exception as e:  # noqa: BLE001
        raise GcalError(f"{type(e).__name__}: {e}") from e
    try:
        data = json.loads(body)
    except json.JSONDecodeError as e:
        raise GcalError(f"응답을 해석하지 못했습니다: {body[:200]}") from e
    if not data.get("ok"):
        raise GcalError(str(data.get("error") or data)[:300])
    return data


def check_result(event: dict, data: dict) -> str | None:
    """웹앱이 돌려준 일정이 우리가 요청한 것과 맞는지. 문제가 있으면 사유 문자열."""
    res = data.get("result") or {}
    if not res.get("event_id"):
        return "응답에 일정 ID 가 없습니다"
    if res.get("start") != event["start_date"]:
        return f"시작일이 다릅니다(요청 {event['start_date']} / 실제 {res.get('start')})"
    if str(res.get("end")) < event["end_date_exclusive"]:
        return (f"종료일이 요청보다 짧습니다"
                f"(요청 {event['end_date_exclusive']} / 실제 {res.get('end')})")
    return None
