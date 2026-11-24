#!/usr/bin/env python3
"""용산힐링룸 삼삼엠투 ↔ 직방 자동 동기화.

  python3 sync.py --mode dry-run     # 읽기만. 비교 결과만 출력
  python3 sync.py --mode apply       # 실제로 막고/값을 맞춘다 (해제는 절대 안 함)

동작 원칙
  · 결제·계약이 잡힌 쪽을 기준으로 반대쪽을 '막는다'. 해제는 자동으로 하지 않는다.
  · 가격은 항목별 1:1. 직전 실행 대비 최근에 바뀐 쪽을 기준으로 반대쪽을 맞춘다.
  · 한 단계라도 실패하면 이번 회차는 아무것도 바꾸지 않고 메일로 남긴다.
  · 정상 동작이면 아무 알림도 보내지 않는다.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import date, datetime, timedelta, timezone

from hrsync import notify, plan
from hrsync import state as statemod
from hrsync.httpc import Client
from hrsync.m33 import ROOM_ID, M33, M33Error
from hrsync.zb import ITEM_ID, ZB, ZBError

KST = timezone(timedelta(hours=9))
DAYS = ["월", "화", "수", "목", "금", "토", "일"]


def now_kst() -> datetime:
    return datetime.now(KST)


def load_session(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_session(path: str, data: dict) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def won(v) -> str:
    return "없음" if v is None else f"{int(v):,}원"


def fmt_range(a: str, b: str) -> str:
    d = date.fromisoformat(a)
    if a == b:
        return f"{a}({DAYS[d.weekday()]})"
    return f"{a}~{b}"


def group_ranges(dates: dict, today: date, horizon: date, kinds: tuple[str, ...]) -> list[str]:
    days = sorted(d for d in dates if kinds is None or dates[d] in kinds)
    days = [date.fromisoformat(d) for d in days if today <= date.fromisoformat(d) <= horizon]
    out: list[list[date]] = []
    for d in days:
        if out and (d - out[-1][1]).days == 1:
            out[-1][1] = d
        else:
            out.append([d, d])
    return [fmt_range(a.isoformat(), b.isoformat()) for a, b in out]


def read_m33(email: str, password: str, room_id: int, action_ids: dict,
             session: dict | None = None, session_sink=None):
    c = Client()
    api = M33(c, email, password, room_id=room_id, action_ids=action_ids,
              session=(session or {}).get("value"), on_session=session_sink)
    room = api.room()          # 저장된 세션으로 먼저 시도(401이면 내부에서 로그인)
    sched = api.schedules()
    return api, room, sched


def read_zb(email, password, cookie, item_id, fixture=None):
    if fixture:
        import json as _json
        with open(fixture, encoding="utf-8") as f:
            data = _json.load(f)
        return _FixtureZB(data), data["item"]
    c = Client()
    api = ZB(c, item_id=item_id, email=email, password=password, cookie=cookie)
    api.login()
    item = api.item()
    return api, item


class _FixtureZB:
    """직방 자격증명 없이 판정 로직을 확인할 때 쓰는 읽기 전용 대역."""

    def __init__(self, data: dict):
        self.data = data

    def calendar(self, first, last):
        cal = self.data.get("calendar") or {}
        return {"dates": {k: v for k, v in cal.items() if first.isoformat() <= k <= last.isoformat()}}

    def set_unavailable(self, *a, **k):
        raise ZBError("픽스처 모드에서는 직방에 쓸 수 없습니다")

    def patch_pricing(self, *a, **k):
        raise ZBError("픽스처 모드에서는 직방에 쓸 수 없습니다")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["dry-run", "apply"], default=os.environ.get("MODE", "dry-run"))
    ap.add_argument("--site", choices=["all", "m33", "zb"], default="all")
    ap.add_argument("--horizon", type=int, default=int(os.environ.get("HORIZON_DAYS", "180")))
    ap.add_argument("--state", default=os.environ.get("STATE_PATH", "state/state.json"))
    ap.add_argument("--session", default=os.environ.get("SESSION_PATH", "state/session.json"))
    ap.add_argument("--room-id", type=int, default=int(os.environ.get("M33_ROOM_ID", ROOM_ID)))
    ap.add_argument("--item-id", type=int, default=int(os.environ.get("ZB_ITEM_ID", ITEM_ID)))
    ap.add_argument("--zb-fixture", default=os.environ.get("ZB_FIXTURE"),
                    help="직방 응답 대신 쓸 JSON 파일(테스트 전용, apply 모드에서는 거부)")
    args = ap.parse_args()

    if args.zb_fixture and args.mode == "apply":
        print("--zb-fixture 는 테스트 전용입니다. apply 모드에서는 쓸 수 없습니다.", file=sys.stderr)
        return 2

    t0 = now_kst()
    today = t0.date()
    horizon = today + timedelta(days=args.horizon)
    st = statemod.load(args.state)
    prev = {k: (st.get(k) or {}) for k in ("m33", "zb")}

    lines: list[str] = []
    problems: list[str] = []

    def log(s: str = "") -> None:
        lines.append(s)
        print(s, flush=True)

    log(f"=== 힐링룸 동기화 {t0:%Y-%m-%d %H:%M} KST · 모드 {args.mode} · 대상 {args.site} ===")
    log(f"기간: {today} ~ {horizon}")

    # ---------------------------------------------------------------- 읽기
    m33_api = m33_room = m33_sched = None
    zb_api = zb_item = zb_cal = None

    if args.site in ("all", "m33"):
        try:
            sess = load_session(args.session)
            m33_api, m33_room, m33_sched = read_m33(
                os.environ.get("M33_EMAIL", ""), os.environ.get("M33_PASSWORD", ""),
                args.room_id, st.get("m33", {}).get("action_ids") or {},
                session=sess.get("m33"),
                session_sink=lambda v: (sess.__setitem__("m33", {"value": v, "saved_at": now_kst().isoformat(timespec="seconds")}),
                                        save_session(args.session, sess)))
            st.setdefault("m33", {})["action_ids"] = m33_api.action_ids
            log(f"[삼삼엠투] {m33_room['name']} (room {args.room_id}, {m33_room['status']})")
            log(f"  요금: 주 {won(m33_room['weekly_rent'])} / 관리비 {won(m33_room['maintenance'])}"
                f" / 청소비 {won(m33_room['cleaning'])}")
            bk = group_ranges(m33_sched["dates"], today, horizon, ("booking",))
            bl = group_ranges(m33_sched["dates"], today, horizon, ("disable",))
            log(f"  계약(booking): {', '.join(bk) or '없음'}")
            log(f"  수동차단(disable): {', '.join(bl) or '없음'}")
        except Exception as e:  # noqa: BLE001
            problems.append(f"삼삼엠투 읽기 실패: {type(e).__name__}: {e}")
            log(f"[삼삼엠투] 읽기 실패 — {type(e).__name__}: {e}")

    zb_configured = bool(os.environ.get("ZB_COOKIE") or
                         (os.environ.get("ZB_EMAIL") and os.environ.get("ZB_PASSWORD")))
    if args.site in ("all", "zb") and not zb_configured and not args.zb_fixture:
        skip = ("직방 자격증명이 없어 직방 동기화를 건너뜁니다 "
                "(ZB_EMAIL/ZB_PASSWORD 또는 ZB_COOKIE 를 Secrets 에 넣어 주세요). "
                "삼삼엠투만 읽었습니다 — 비교·자동수정은 직방을 읽을 수 있어야 합니다.")
        log(f"[직방] 건너뜀 — {skip}")
        problems.append(skip)
    elif args.site in ("all", "zb"):
        try:
            zb_api, zb_item = read_zb(os.environ.get("ZB_EMAIL"), os.environ.get("ZB_PASSWORD"),
                                      os.environ.get("ZB_COOKIE"), args.item_id, args.zb_fixture)
            zb_cal = zb_api.calendar(today, horizon)
            log(f"[직방] {zb_item['name']} (item {args.item_id}, {zb_item['status']})"
                + ("   ※ 픽스처(테스트) 데이터" if args.zb_fixture else ""))
            log(f"  요금: 주 {won(zb_item['weekly_rent'])} / 관리비 {won(zb_item['maintenance'])}"
                f" / 청소비 {won(zb_item['cleaning'])}")
            log(f"  예약불가: {', '.join(group_ranges(zb_cal['dates'], today, horizon, ('blocked',))) or '없음'}")
            log(f"  계약완료: {', '.join(group_ranges(zb_cal['dates'], today, horizon, ('contracted',))) or '없음'}")
        except Exception as e:  # noqa: BLE001
            problems.append(f"직방 읽기 실패: {type(e).__name__}: {e}")
            log(f"[직방] 읽기 실패 — {type(e).__name__}: {e}")

    # ---------------------------------------------------------------- 판정
    date_plan = price_plan = None
    if m33_sched and zb_cal:
        date_plan = plan.plan_dates(m33_sched["dates"], zb_cal["dates"], today, args.horizon)
        log("")
        log("[날짜 판정]")
        if date_plan["add_to_zb"]:
            log(f"  → 직방에 예약불가로 막을 기간: {', '.join(fmt_range(a, b) for a, b in date_plan['add_to_zb'])}")
        if date_plan["add_to_m33"]:
            log(f"  → 삼삼엠투에 차단할 기간: {', '.join(fmt_range(a, b) for a, b in date_plan['add_to_m33'])}")
        if not (date_plan["add_to_zb"] or date_plan["add_to_m33"]):
            log("  차이 없음 — 막을 날짜 없음")
        for n in date_plan["notes"]:
            log(f"  ! {n}")

    if m33_room and zb_item:
        price_plan = plan.plan_prices(
            plan.snapshots(m33_room), plan.snapshots(zb_item),
            (prev["m33"].get("fees") if prev["m33"] else None),
            (prev["zb"].get("fees") if prev["zb"] else None))
        log("")
        log("[가격 판정]")
        if price_plan["changes"]:
            for c in price_plan["changes"]:
                log(f"  → {c['source']} 기준 {c['label']} {c['value']:,}원을 {c['dest']}에 반영")
        if not price_plan["changes"]:
            log("  반영할 변경 없음")
        for a in price_plan["ambiguous"]:
            log(f"  ! 판정 불가 — {a}")
        for s in price_plan["skipped"]:
            log(f"  ! 적용 보류 — {s}")
        for n in price_plan["notes"]:
            log(f"  · {n}")

    # 이전에 있던 차단이 사라진 경우(사람이 지웠을 수 있음) — 손대지 않고 알리기만
    removed = []
    for key, label, statuses in (("m33", "삼삼엠투", ("booking", "disable")),
                                 ("zb", "직방", ("blocked", "contracted"))):
        old = (prev[key] or {}).get("dates") or {}
        cur = (m33_sched["dates"] if key == "m33" and m33_sched else
               zb_cal["dates"] if key == "zb" and zb_cal else None)
        if not cur:
            continue
        gone = sorted(set(old) - set(cur))
        gone = [g for g in gone if date.fromisoformat(g) >= today]
        if gone:
            removed.append(f"{label}: {', '.join(group_ranges({g: 'x' for g in gone}, today, horizon, ('x',)))}")

    # ---------------------------------------------------------------- 적용
    applied: list[str] = []
    if args.mode == "apply" and not problems:
        if date_plan:
            for a, b in date_plan["add_to_zb"]:
                try:
                    blocked = dict(zb_cal["dates"])
                    d = date.fromisoformat(a)
                    while d <= date.fromisoformat(b):
                        blocked[d.isoformat()] = "blocked"
                        d += timedelta(days=1)
                    zb_api.set_unavailable(blocked)
                    zb_cal = zb_api.calendar(today, horizon)
                    ok = all(zb_cal["dates"].get((date.fromisoformat(a) + timedelta(days=i)).isoformat())
                             for i in range((date.fromisoformat(b) - date.fromisoformat(a)).days + 1))
                    applied.append(f"직방 {fmt_range(a, b)} 예약불가 {'완료' if ok else '확인 실패'}")
                    if not ok:
                        problems.append(f"직방 {fmt_range(a,b)} 차단이 반영되지 않았습니다")
                except Exception as e:  # noqa: BLE001
                    problems.append(f"직방 차단 실패 {fmt_range(a,b)}: {type(e).__name__}: {e}")
                    break
            for a, b in date_plan["add_to_m33"]:
                try:
                    m33_api.set_unavailable(date.fromisoformat(a), date.fromisoformat(b))
                    m33_sched = m33_api.schedules()
                    got = m33_sched["dates"]
                    ok = all(got.get((date.fromisoformat(a) + timedelta(days=i)).isoformat())
                             for i in range((date.fromisoformat(b) - date.fromisoformat(a)).days + 1))
                    applied.append(f"삼삼엠투 {fmt_range(a, b)} 차단 {'완료' if ok else '확인 실패'}")
                    if not ok:
                        problems.append(f"삼삼엠투 {fmt_range(a,b)} 차단이 반영되지 않았습니다")
                except Exception as e:  # noqa: BLE001
                    problems.append(f"삼삼엠투 차단 실패 {fmt_range(a,b)}: {type(e).__name__}: {e}")
                    break

        if price_plan:
            by_target: dict[str, list[dict]] = {}
            for c in price_plan["changes"]:
                by_target.setdefault(c["target"], []).append(c)
            for target, items in by_target.items():
                kw = {c["item"]: c["value"] for c in items}
                try:
                    if target == "zb":
                        zb_api.patch_pricing(zb_item, **kw)
                        zb_item = zb_api.item()
                        after = plan.snapshots(zb_item)
                    else:
                        m33_api.set_fees(**kw)
                        m33_room = m33_api.room()
                        after = plan.snapshots(m33_room)
                    for c in items:
                        ok = after.get(c["item"]) == c["value"]
                        applied.append(f"{c['dest']} {c['label']} {c['value']:,}원 "
                                       f"{'반영 완료' if ok else '확인 실패'}")
                        if not ok:
                            problems.append(f"{c['dest']} {c['label']} 반영 확인 실패 "
                                            f"(기대 {c['value']:,} / 실제 {after.get(c['item'])})")
                except Exception as e:  # noqa: BLE001
                    problems.append(f"{target} 요금 저장 실패: {type(e).__name__}: {e}")

    if args.mode == "apply":
        log("")
        log("[조치]")
        if not applied and not problems:
            log("  바꾼 것 없음")
        for a in applied:
            log(f"  · {a}")

    if removed:
        log("")
        log("[참고] 이전에 있던 차단이 사라졌습니다(자동으로 되돌리지 않음):")
        for r in removed:
            log(f"  · {r}")

    # ---------------------------------------------------------------- 상태 저장
    if m33_sched:
        st.setdefault("m33", {})["dates"] = m33_sched["dates"]
    if zb_cal:
        st.setdefault("zb", {})["dates"] = zb_cal["dates"]

    # 가격 기준선은 '양쪽이 같아진 항목'만 갱신한다(어긋난 항목은 다음 회차에 다시 판정).
    if m33_room and zb_item:
        cur_m, cur_z = plan.snapshots(m33_room), plan.snapshots(zb_item)
        for key, _ in plan.ITEMS:
            if cur_m.get(key) is not None and cur_m.get(key) == cur_z.get(key):
                st.setdefault("m33", {}).setdefault("fees", {})[key] = cur_m[key]
                st.setdefault("zb", {}).setdefault("fees", {})[key] = cur_z[key]

    st["last_run"] = {
        "at": t0.isoformat(timespec="seconds"),
        "mode": args.mode,
        "problems": problems,
        "applied": applied,
        "add_to_zb": date_plan["add_to_zb"] if date_plan else None,
        "add_to_m33": date_plan["add_to_m33"] if date_plan else None,
        "price_changes": [f"{c['dest']} {c['label']} {c['value']}" for c in
                          (price_plan["changes"] if price_plan else [])],
    }

    # ---------------------------------------------------------------- 알림
    alert_bits = list(problems)
    if price_plan:
        alert_bits += [f"판정 불가 — {a}" for a in price_plan["ambiguous"]]
        alert_bits += [f"적용 보류 — {s}" for s in price_plan["skipped"]]
    if date_plan:
        alert_bits += date_plan["notes"]
    if removed:
        alert_bits += [f"차단이 사라짐 — {r}" for r in removed]

    alert_state = st.get("alert") or {}
    alert_ok = False
    body = "\n".join(lines) + "\n\n■ 요약\n" + "\n".join(f"- {b}" for b in alert_bits)

    if alert_bits and args.mode == "apply":
        h = hashlib.sha256("\n".join(alert_bits).encode()).hexdigest()[:16]
        last = alert_state.get("at")
        recent = False
        if last:
            try:
                recent = (t0 - datetime.fromisoformat(last)) < timedelta(hours=24)
            except ValueError:
                recent = False
        if recent and alert_state.get("hash") == h:
            log("\n[알림] 같은 내용을 24시간 안에 이미 남겼습니다 — 이슈 새로 만들지 않음")
            alert_ok = True
        else:
            ok, detail = notify.open_alert("확인이 필요합니다", body, key=h)
            alert_ok = ok
            if os.environ.get("SMTP_USER") and os.environ.get("SMTP_PASS"):
                mok, mdetail = notify.send_mail("확인이 필요합니다", body)
                detail += f" / 메일 {'발송' if mok else '미발송'}({mdetail})"
            log(f"\n[알림] {'기록 완료' if ok else '기록 실패'} — {detail}")
            st["alert"] = {"hash": h, "at": t0.isoformat(timespec="seconds"), "detail": detail}
    elif args.mode == "apply" and not alert_bits:
        n, detail = notify.close_alerts()
        if n:
            log(f"\n[알림] 열려 있던 이슈 {n}건을 정상 확인으로 닫았습니다")
        if alert_state:
            st.pop("alert", None)
    elif args.mode == "dry-run" and alert_bits:
        log("\n[알림] 드라이런이라 이슈를 만들지 않습니다 (apply 모드에서 아래 내용으로 기록):")
        for b in alert_bits:
            log(f"  - {b}")

    if args.mode == "apply":
        statemod.save(args.state, st)
    else:
        print("\n(드라이런이라 상태 파일은 저장하지 않았습니다)")
    # 예외가 '이슈로 기록'됐으면 실행 자체는 성공으로 끝낸다.
    # (실패로 끝내면 GitHub 이 매 회차 실패 메일을 보내 사용자 메일함이 지저분해진다.
    #  이슈 기록조차 실패했을 때만 비정상 종료해 GitHub 실패 알림을 마지막 수단으로 쓴다.)
    return 0 if (not problems or alert_ok) else 1


if __name__ == "__main__":
    sys.exit(main())
