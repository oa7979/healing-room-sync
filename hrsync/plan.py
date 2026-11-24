"""동기화 판정 규칙 (순수 함수, 네트워크 없음).

확정된 규칙
  ● 날짜 : 결제·계약이 잡힌 쪽(삼삼엠투 booking/disable, 직방 blocked/contracted)을 기준으로
           반대쪽을 '막는다'. 막는 것만 자동. 푸는 것(해제)은 절대 자동으로 하지 않는다.
  ● 가격 : 항목별 1:1 (주 요금=임대료, 관리비, 청소비).
           직전 실행 때 값과 비교해 '최근에 바뀐 쪽'을 기준으로 반대쪽을 맞춘다.
           같은 주기에 양쪽이 모두 바뀌어 판정이 안 되면 아무것도 쓰지 않고 보고한다.
           0원이거나 비정상적으로 큰 값은 적용하지 않고 보고한다.
"""
from __future__ import annotations

from datetime import date, timedelta

ITEMS = (
    ("weekly_rent", "주 요금(임대료)"),
    ("maintenance", "관리비"),
    ("cleaning", "청소비"),
)

M33_BOOKED = "booking"
M33_BLOCKED = "disable"

MAX_PRICE = 5_000_000      # 이보다 크면 비정상으로 본다
PRICE_RATIO_LIMIT = 3      # 반대쪽 값의 3배를 넘으면 비정상으로 본다
MAX_ADD_DAYS = 90          # 한 회차에 새로 막는 날짜가 이보다 많으면 사람 확인


def _days(today: date, horizon_days: int) -> list[date]:
    return [today + timedelta(days=i) for i in range(horizon_days + 1)]


def _ranges(days: list[date]) -> list[tuple[str, str]]:
    out: list[list[date]] = []
    for d in days:
        if out and (d - out[-1][1]).days == 1:
            out[-1][1] = d
        else:
            out.append([d, d])
    return [(a.isoformat(), b.isoformat()) for a, b in out]


def plan_dates(m33_dates: dict, zb_dates: dict, today: date, horizon_days: int = 180) -> dict:
    """양쪽 달력을 비교해 '추가로 막아야 할 날짜'만 계산한다."""
    add_zb: list[date] = []
    add_m33: list[date] = []
    notes: list[str] = []

    for d in _days(today, horizon_days):
        iso = d.isoformat()
        m = m33_dates.get(iso)
        z = zb_dates.get(iso)
        m_blocked = m in (M33_BOOKED, M33_BLOCKED)
        z_blocked = z is not None

        if m_blocked and not z_blocked:
            add_zb.append(d)
        elif z_blocked and not m_blocked:
            add_m33.append(d)

    total = len(add_zb) + len(add_m33)
    unsafe = total > MAX_ADD_DAYS
    if unsafe:
        notes.append(
            f"한 회차에 막아야 할 날짜가 {total}일로 너무 많아 자동 적용을 멈췄습니다 "
            f"(삼삼엠투→직방 {len(add_zb)}일, 직방→삼삼엠투 {len(add_m33)}일)."
        )
    return {
        "add_to_zb": [] if unsafe else _ranges(add_zb),
        "add_to_m33": [] if unsafe else _ranges(add_m33),
        "add_to_zb_days": len(add_zb),
        "add_to_m33_days": len(add_m33),
        "unsafe": unsafe,
        "notes": notes,
    }


def _as_int(v):
    if v is None or v == "":
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def plan_prices(cur_m33: dict, cur_zb: dict, prev_m33: dict | None, prev_zb: dict | None) -> dict:
    """항목별로 어느 쪽이 최근에 바뀌었는지 판정하고, 반대쪽에 넣을 값을 정한다."""
    baseline = prev_m33 is None or prev_zb is None
    changes: list[dict] = []
    ambiguous: list[str] = []
    skipped: list[str] = []
    notes: list[str] = []

    if baseline:
        notes.append("첫 실행이라 양쪽 금액을 기준선으로 저장만 했습니다(가격 변경 없음).")

    for key, label in ITEMS:
        cm, cz = _as_int(cur_m33.get(key)), _as_int(cur_zb.get(key))
        if cm is None and cz is None:
            continue
        if not baseline and cm == cz:
            continue

        if baseline:
            # 기준선만 저장. 값이 다르면 다음 회차에 판정한다.
            if cm != cz:
                notes.append(f"{label}: 삼삼엠투 {cm:,}원 / 직방 {cz if cz is None else format(cz, ',')}원 (기준선 저장)")
            continue

        pm, pz = _as_int(prev_m33.get(key)), _as_int(prev_zb.get(key))
        m_changed = pm != cm
        z_changed = pz != cz

        if m_changed and z_changed:
            ambiguous.append(f"{label}: 양쪽이 같은 주기에 바뀜 (삼삼엠투 {pm:,}→{cm:,} / "
                             f"직방 {pz if pz is None else format(pz, ',')}→{cz if cz is None else format(cz, ',')})")
            continue
        if not m_changed and not z_changed:
            notes.append(f"{label}: 양쪽 다 그대로인데 금액이 다름 "
                         f"(삼삼엠투 {cm:,} / 직방 {cz if cz is None else format(cz, ',')}) — 확인 필요")
            continue

        if m_changed:
            target, value, from_side, to_side = "zb", cm, "삼삼엠투", "직방"
            base_value = cz
        else:
            target, value, from_side, to_side = "m33", cz, "직방", "삼삼엠투"
            base_value = cm

        reason = _reject(value, base_value)
        if reason:
            skipped.append(f"{label}: {from_side} 값 {value:,}원이 {reason} — 적용하지 않음")
            continue

        changes.append({"item": key, "label": label, "target": target, "value": value,
                        "source": from_side, "dest": to_side})

    return {"changes": changes, "ambiguous": ambiguous, "skipped": skipped,
            "baseline": baseline, "notes": notes}


def _reject(value: int, other: int | None) -> str | None:
    if value <= 0:
        return "0원 이하"
    if value > MAX_PRICE:
        return f"{MAX_PRICE:,}원 초과"
    if other and other > 0 and value > other * PRICE_RATIO_LIMIT:
        return f"반대쪽({other:,}원)의 {PRICE_RATIO_LIMIT}배 초과"
    return None


def snapshots(fees: dict) -> dict:
    """상태 파일에 저장할 항목별 금액만 뽑는다."""
    return {k: _as_int(fees.get(k)) for k, _ in ITEMS}
