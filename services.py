"""업무 규칙: 권한, 예약 상태 흐름, 충돌 검사, 자동 작업(만료·자동승인·노쇼·연체·리마인드),
정비·만기 판정, 반납/운행기록, 통계.

화면(UI) 코드는 이 모듈의 함수만 호출한다. 권한 검사도 여기서 다시 하므로
화면에서 버튼을 숨기는 것과 별개로 서버 쪽에서 막힌다.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from functools import lru_cache
from pathlib import Path

import pandas as pd

import data
import notify
from data import (ST_APPROVED, ST_CANCELED, ST_DONE, ST_EXPIRED, ST_NOSHOW, ST_OUT, ST_OVERDUE,
                  ST_PENDING, ST_REJECTED, OCCUPYING_STATUSES, ROLE_ADMIN, ROLE_SITE, KIND_WORK,
                  dt_str, to_date, to_dt)

try:
    import holidays as _holidays
except ImportError:  # pragma: no cover
    _holidays = None


class ServiceError(Exception):
    """사용자에게 그대로 보여줄 수 있는 업무 규칙 위반."""


SYSTEM = "SYSTEM"


# =========================================================================
# 직원 / 권한
# =========================================================================
def list_employees(active_only: bool = True) -> list[dict]:
    sql = "SELECT * FROM employees" + (" WHERE active=1" if active_only else "") + " ORDER BY role DESC, site, name"
    return data.query_all(sql)


def get_employee(emp_no: str) -> dict | None:
    return data.query_one("SELECT * FROM employees WHERE emp_no=?", (emp_no,))


def is_admin(emp: dict) -> bool:
    return emp["role"] == ROLE_ADMIN


def is_manager(emp: dict) -> bool:
    return emp["role"] in (ROLE_ADMIN, ROLE_SITE)


def manageable_sites(emp: dict) -> list[str]:
    if is_admin(emp):
        return list(data.get_setting("sites"))
    if emp["role"] == ROLE_SITE:
        return [emp["site"]]
    return []


def can_manage_site(emp: dict, site: str) -> bool:
    return is_admin(emp) or (emp["role"] == ROLE_SITE and emp["site"] == site)


def _require(cond: bool, msg: str = "권한이 없습니다."):
    if not cond:
        raise ServiceError(msg)


def actor_label(emp: dict | None) -> str:
    if not emp:
        return SYSTEM
    return f"{emp['name']}({emp['emp_no']})"


def save_employee(actor: dict, emp: dict, is_new: bool) -> None:
    _require(is_admin(actor), "직원/권한 관리는 총무 관리자만 할 수 있습니다.")
    if not emp.get("emp_no") or not emp.get("name"):
        raise ServiceError("사번과 이름은 필수입니다.")
    if emp["role"] == ROLE_SITE and not emp.get("site"):
        raise ServiceError("현장 담당자는 담당 현장을 지정해야 합니다.")
    with data.tx() as conn:
        exists = conn.execute("SELECT 1 FROM employees WHERE emp_no=?", (emp["emp_no"],)).fetchone()
        if is_new and exists:
            raise ServiceError(f"이미 있는 사번입니다: {emp['emp_no']}")
        if is_new:
            conn.execute("INSERT INTO employees(emp_no,name,dept,site,role,phone,email,active) VALUES(?,?,?,?,?,?,?,1)",
                         (emp["emp_no"], emp["name"], emp.get("dept", ""), emp.get("site", ""), emp["role"],
                          emp.get("phone", ""), emp.get("email", "")))
        else:
            conn.execute("UPDATE employees SET name=?,dept=?,site=?,role=?,phone=?,email=?,active=? WHERE emp_no=?",
                         (emp["name"], emp.get("dept", ""), emp.get("site", ""), emp["role"], emp.get("phone", ""),
                          emp.get("email", ""), int(emp.get("active", 1)), emp["emp_no"]))
        data.audit(conn, actor_label(actor), "직원 추가" if is_new else "직원 수정", "employee", emp["emp_no"], emp)


# =========================================================================
# 날짜 / 정책
# =========================================================================
@lru_cache(maxsize=16)
def _kr_holidays(year: int):
    return _holidays.KR(years=year) if _holidays else {}


def is_business_day(d: date) -> bool:
    return d.weekday() < 5 and d not in _kr_holidays(d.year)


def business_days(start: date, end: date) -> int:
    n, cur = 0, start
    while cur <= end:
        n += is_business_day(cur)
        cur += timedelta(days=1)
    return n


def holiday_name(d: date) -> str | None:
    return _kr_holidays(d.year).get(d) if _holidays else None


def auto_approve_cutoff(start_dt: datetime, settings: dict) -> datetime:
    """시작일 전날 설정 시각(기본 17:00). 이 시각이 지나면 승인대기 예약을 자동 승인한다."""
    hh, mm = (int(x) for x in str(settings.get("auto_approve_time", "17:00")).split(":"))
    prev = start_dt.date() - timedelta(days=1)
    return datetime(prev.year, prev.month, prev.day, hh, mm)


def validate_period(start_dt: datetime, end_dt: datetime, settings: dict, *, allow_past_start=False) -> None:
    now = data.now()
    interval = int(settings.get("time_interval_minutes", 30))
    if end_dt <= start_dt:
        raise ServiceError("종료 일시는 시작 일시보다 늦어야 합니다.")
    for t in (start_dt, end_dt):
        if (t.hour * 60 + t.minute) % interval:
            raise ServiceError(f"시간은 {interval}분 단위로 선택해 주세요.")
    if not allow_past_start and start_dt < now - timedelta(minutes=interval):
        raise ServiceError("이미 지난 시간은 예약할 수 없습니다.")
    max_day = now.date() + timedelta(days=int(settings["max_days_ahead"]))
    if end_dt.date() > max_day:
        raise ServiceError(f"예약은 {max_day:%Y-%m-%d}까지 가능합니다.")
    bdays = business_days(start_dt.date(), end_dt.date())
    if bdays == 0:
        raise ServiceError("선택한 기간에 영업일이 없습니다. (주말·공휴일만 포함)")
    if bdays > int(settings["max_business_days"]):
        raise ServiceError(f"1회 예약은 최대 {settings['max_business_days']}영업일입니다. (현재 {bdays}영업일)")


# =========================================================================
# 차량
# =========================================================================
def list_vehicles(active_only: bool = True) -> pd.DataFrame:
    return data.query_df("SELECT * FROM vehicles" + (" WHERE active=1" if active_only else "") + " ORDER BY kind, site, vehicle_no")


def get_vehicle(vno: str) -> dict | None:
    return data.query_one("SELECT * FROM vehicles WHERE vehicle_no=?", (vno,))


def vehicle_use_label(kind: str) -> str:
    return "임원용차량" if kind == data.KIND_EXEC else "업무용차량"


def effective_interval(r: dict, now: datetime | None = None) -> tuple[datetime, datetime]:
    """예약이 실제로 차량을 점유하는 구간."""
    now = now or data.now()
    start, end = to_dt(r["start_dt"]), to_dt(r["end_dt"])
    if r["status"] == ST_OUT and r.get("checkout_at"):
        start = min(start, to_dt(r["checkout_at"]))
    if r["status"] == ST_OVERDUE:
        # 반납 전까지는 오늘 하루 전체를 점유한 것으로 본다.
        end = max(end, datetime(now.year, now.month, now.day, 23, 59))
    if r["status"] == ST_DONE:
        start = to_dt(r.get("checkout_at")) or start
        ret = to_dt(r.get("return_at"))
        if ret and ret.hour + ret.minute > 0:
            end = ret
    return start, end


def find_reservation_conflict(conn, vno: str, start_dt: datetime, end_dt: datetime, exclude_id=None) -> dict | None:
    rows = conn.execute(
        f"SELECT * FROM reservations WHERE vehicle_no=? AND status IN ({','.join('?' * len(OCCUPYING_STATUSES))})"
        " AND id IS NOT ? AND start_dt < ?",
        (vno, *OCCUPYING_STATUSES, exclude_id, dt_str(end_dt)),
    ).fetchall()
    for row in rows:
        r = dict(row)
        s, e = effective_interval(r)
        if s < end_dt and start_dt < e:
            return r
    return None


def find_maintenance_conflict(conn, vno: str, start_dt: datetime, end_dt: datetime) -> dict | None:
    row = conn.execute(
        "SELECT * FROM maintenance WHERE vehicle_no=? AND status IN ('예정','진행') AND start_date<=? AND end_date>=?",
        (vno, end_dt.strftime(data.D_FMT), start_dt.strftime(data.D_FMT)),
    ).fetchone()
    return dict(row) if row else None


def vehicle_block_reason(conn, vehicle: dict, start_dt, end_dt, exclude_id=None) -> str | None:
    if not vehicle or not vehicle["active"]:
        return "등록되지 않았거나 삭제된 차량입니다."
    if vehicle["kind"] != KIND_WORK:
        return "구매 차량은 임원용차량이라 예약할 수 없습니다."
    m = find_maintenance_conflict(conn, vehicle["vehicle_no"], start_dt, end_dt)
    if m:
        return f"정비 일정과 겹칩니다 ({m['start_date']} ~ {m['end_date']}, {m['mtype']})"
    c = find_reservation_conflict(conn, vehicle["vehicle_no"], start_dt, end_dt, exclude_id)
    if c:
        return f"다른 예약과 겹칩니다 ({c['start_dt']} ~ {c['end_dt']}, {c['status']})"
    return None


def user_block_reason(conn, emp_no: str, start_dt: datetime, end_dt: datetime, settings: dict, exclude_id=None) -> str | None:
    """완화된 정책: 동시에 진행 중인 예약 1건(시간 겹침 금지) + 미래 예약 최대 N건, 연체 중이면 신규 불가."""
    rows = [dict(r) for r in conn.execute(
        f"SELECT * FROM reservations WHERE emp_no=? AND status IN ({','.join('?' * len(OCCUPYING_STATUSES))}) AND id IS NOT ?",
        (emp_no, *OCCUPYING_STATUSES, exclude_id)).fetchall()]
    if any(r["status"] == ST_OVERDUE for r in rows):
        return "반납 기한이 지난(연체) 예약이 있어 새 예약을 할 수 없습니다. 먼저 차량을 반납해 주세요."
    for r in rows:
        s, e = effective_interval(r)
        if s < end_dt and start_dt < e:
            return f"같은 시간대에 이미 본인 예약이 있습니다 (#{r['id']} {r['start_dt']} ~ {r['end_dt']})"
    now = data.now()
    future = [r for r in rows if to_dt(r["start_dt"]) > now]
    limit = int(settings.get("max_future_reservations", 3))
    if to_dt(dt_str(start_dt)) > now and len(future) >= limit:
        return f"앞으로 예정된 예약은 최대 {limit}건까지 가능합니다. (현재 {len(future)}건)"
    return None


def capacity(v: dict) -> int:
    return int(v.get("seats") or data.infer_seats(v.get("model", "")))


@dataclass
class Recommendation:
    vehicle: dict
    score: float
    reasons: list[str]


def recommend(site: str | None, start_dt: datetime, end_dt: datetime, passengers: int,
              needs: list[str] | None = None, limit: int = 5) -> list[Recommendation]:
    """예약 가능한 업무용 차량을 점수로 정렬하고 추천 이유를 붙인다. (점수가 낮을수록 우선)"""
    needs = set(needs or [])
    passengers = max(1, int(passengers or 1))
    out: list[Recommendation] = []
    with data.db() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM vehicles WHERE active=1 AND kind=?" + (" AND site=?" if site else ""),
            (KIND_WORK, site) if site else (KIND_WORK,)).fetchall()]
        for v in rows:
            if capacity(v) < passengers:
                continue
            if "전기차 제외" in needs and v["fuel_type"] == "전기":
                continue
            if vehicle_block_reason(conn, v, start_dt, end_dt):
                continue
            spare = capacity(v) - passengers
            score = spare * 10 + (v["odometer"] or 0) / 10000
            reasons = [f"{passengers}명 탑승 · 정원 {capacity(v)}명" + (" (딱 맞는 크기)" if spare <= 1 else f" (여유 {spare}석)")]
            if site:
                reasons.append(f"요청 현장 '{site}' 소속 차량")
            if "짐 많음" in needs:
                if v["body_type"] in ("SUV", "승합"):
                    score -= 25
                    reasons.append(f"짐이 많아 {v['body_type']} 우선")
                else:
                    score += 15
            if "장거리" in needs:
                if v["fuel_type"] == "전기":
                    score += 20
                else:
                    reasons.append("장거리라 충전 부담 없는 내연기관 우선")
            if "전기차 선호" in needs and v["fuel_type"] == "전기":
                score -= 20
                reasons.append("전기차 선호")
            out.append(Recommendation(v, score, reasons))
    out.sort(key=lambda r: r.score)
    if out:
        min_km = min(r.vehicle["odometer"] or 0 for r in out)
        for r in out:
            if (r.vehicle["odometer"] or 0) == min_km:
                r.reasons.append(f"후보 중 주행거리가 가장 적음 ({min_km:,} km)")
            else:
                r.reasons.append(f"주행거리 {r.vehicle['odometer'] or 0:,} km")
    return out[:limit]


def log_failure(emp: dict, site: str | None, start_dt, end_dt, passengers, reason: str) -> None:
    with data.tx() as conn:
        conn.execute("INSERT INTO booking_failures(ts, emp_no, site, start_dt, end_dt, passengers, reason) VALUES(?,?,?,?,?,?,?)",
                     (data.now_str(), emp["emp_no"], site or "", dt_str(start_dt), dt_str(end_dt), passengers, reason))


# =========================================================================
# 예약 조회
# =========================================================================
RES_SELECT = """SELECT r.*, v.model, v.site, v.kind, v.key_location, v.parking_location AS vehicle_parking,
                       v.fuel_level AS vehicle_fuel, v.manager, v.manager_phone
                FROM reservations r LEFT JOIN vehicles v ON v.vehicle_no = r.vehicle_no"""


def get_reservation(rid: int) -> dict | None:
    return data.query_one(RES_SELECT + " WHERE r.id=?", (int(rid),))


def reservations_df(where: str = "1=1", params: tuple | list = ()) -> pd.DataFrame:
    return data.query_df(RES_SELECT + f" WHERE {where} ORDER BY r.start_dt, r.id", params)


def scoped_reservations(emp: dict, where: str = "1=1", params: list | tuple = ()) -> pd.DataFrame:
    sites = manageable_sites(emp)
    if not sites:
        return reservations_df("r.emp_no=? AND " + where, [emp["emp_no"], *params])
    return reservations_df(f"v.site IN ({','.join('?' * len(sites))}) AND " + where, [*sites, *params])


def my_reservations(emp: dict) -> pd.DataFrame:
    return reservations_df("r.emp_no=?", (emp["emp_no"],)).sort_values(["start_dt", "id"], ascending=[False, False])


def site_contact(site: str) -> dict:
    row = data.query_one("SELECT name, phone FROM employees WHERE role=? AND site=? AND active=1", (ROLE_SITE, site))
    s = data.get_settings()
    return row or {"name": s["admin_name"], "phone": s["admin_phone"]}


# =========================================================================
# 예약 상태 흐름
# =========================================================================
def _load_res(conn, rid: int) -> dict:
    row = conn.execute("SELECT r.*, v.site FROM reservations r LEFT JOIN vehicles v USING(vehicle_no) WHERE r.id=?",
                       (int(rid),)).fetchone()
    if not row:
        raise ServiceError("예약을 찾을 수 없습니다.")
    return dict(row)


def _set_status(conn, rid: int, status: str, **fields) -> None:
    fields["status"] = status
    fields["updated_at"] = data.now_str()
    cols = ", ".join(f"{k}=?" for k in fields)
    conn.execute(f"UPDATE reservations SET {cols} WHERE id=?", (*fields.values(), int(rid)))


def _res_text(r: dict) -> str:
    return f"#{r['id']} {r['vehicle_no']} · {r['start_dt']} ~ {r['end_dt']}"


def _approve_in_tx(conn, r: dict, actor_name: str, auto: bool) -> None:
    _set_status(conn, r["id"], ST_APPROVED, decided_by=actor_name, decided_at=data.now_str(), auto_approved=int(auto))
    data.audit(conn, actor_name, "자동 승인" if auto else "승인", "reservation", r["id"])
    v = conn.execute("SELECT key_location, site FROM vehicles WHERE vehicle_no=?", (r["vehicle_no"],)).fetchone()
    guide = data.get_setting("key_pickup_guide")
    body = (f"{_res_text(r)} 예약이 {'자동 ' if auto else ''}승인되었습니다.\n"
            f"차키 위치: {v['key_location'] if v else '-'}\n{guide}")
    notify.notify(conn, "자동승인" if auto else "승인", f"예약 승인: {r['vehicle_no']}", body,
                  emp_no=r["emp_no"], reservation_id=r["id"])


def create_reservation(actor: dict, vehicle_no: str, start_dt: datetime, end_dt: datetime, *, purpose: str,
                       destination: str, passengers: int, needs: list[str] | None = None) -> tuple[int, str]:
    settings = data.get_settings()
    validate_period(start_dt, end_dt, settings)
    with data.tx() as conn:
        v = conn.execute("SELECT * FROM vehicles WHERE vehicle_no=?", (vehicle_no,)).fetchone()
        v = dict(v) if v else None
        reason = vehicle_block_reason(conn, v, start_dt, end_dt)
        if reason:
            raise ServiceError(reason)
        if capacity(v) < int(passengers or 1):
            raise ServiceError(f"정원({capacity(v)}명)보다 인원이 많습니다.")
        reason = user_block_reason(conn, actor["emp_no"], start_dt, end_dt, settings)
        if reason:
            raise ServiceError(reason)
        next_id = (conn.execute("SELECT MAX(id) FROM reservations").fetchone()[0] or 1000) + 1
        conn.execute(
            """INSERT INTO reservations(id, emp_no, user_name, dept, vehicle_no, start_dt, end_dt, purpose, destination,
               passengers, needs, status, requested_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (next_id, actor["emp_no"], actor["name"], actor.get("dept", ""), vehicle_no, dt_str(start_dt), dt_str(end_dt),
             purpose.strip(), destination.strip(), int(passengers or 1), ",".join(needs or []), ST_PENDING,
             data.now_str(), data.now_str()))
        data.audit(conn, actor_label(actor), "예약 신청", "reservation", next_id,
                   {"vehicle": vehicle_no, "start": dt_str(start_dt), "end": dt_str(end_dt)})
        r = _load_res(conn, next_id)
        notify.notify(conn, "신청접수", f"예약 신청 접수: {vehicle_no}", f"{_res_text(r)} 신청이 접수되었습니다. 승인 결과를 알려 드립니다.",
                      emp_no=actor["emp_no"], reservation_id=next_id)
        notify.notify_managers(conn, "신청접수", f"새 예약 신청: {actor['name']} · {vehicle_no}",
                               f"{_res_text(r)} / 목적지 {destination or '-'} / {purpose or '-'}",
                               site=v["site"], reservation_id=next_id)
        msg = f"예약번호 {next_id}로 신청했습니다. 승인을 기다려 주세요."
        if settings.get("auto_approve_enabled") and data.now() >= auto_approve_cutoff(start_dt, settings):
            _approve_in_tx(conn, r, "SYSTEM(자동승인)", auto=True)
            msg = f"예약번호 {next_id}로 신청했고, 자동 승인 시각이 지나 바로 승인되었습니다."
    _after()
    return next_id, msg


def approve(actor: dict, rid: int) -> None:
    with data.tx() as conn:
        r = _load_res(conn, rid)
        _require(can_manage_site(actor, r["site"]), "이 현장 차량의 예약을 승인할 권한이 없습니다.")
        if r["status"] != ST_PENDING:
            raise ServiceError(f"승인대기 상태가 아닙니다. (현재: {r['status']})")
        start, end = to_dt(r["start_dt"]), to_dt(r["end_dt"])
        if end <= data.now():
            raise ServiceError("이미 끝난 기간의 예약은 승인할 수 없습니다.")
        m = find_maintenance_conflict(conn, r["vehicle_no"], start, end)
        if m:
            raise ServiceError(f"신청 후 정비 일정이 잡혔습니다 ({m['start_date']}~{m['end_date']}). 반려 후 다른 차량을 안내해 주세요.")
        _approve_in_tx(conn, r, actor_label(actor), auto=False)
    _after()


def reject(actor: dict, rid: int, reason: str) -> None:
    if not (reason or "").strip():
        raise ServiceError("반려 사유를 입력해 주세요.")
    with data.tx() as conn:
        r = _load_res(conn, rid)
        _require(can_manage_site(actor, r["site"]), "이 현장 차량의 예약을 반려할 권한이 없습니다.")
        if r["status"] not in (ST_PENDING, ST_APPROVED):
            raise ServiceError(f"승인대기/승인완료 예약만 반려할 수 있습니다. (현재: {r['status']})")
        _set_status(conn, rid, ST_REJECTED, decided_by=actor_label(actor), decided_at=data.now_str(),
                    reject_reason=reason.strip())
        data.audit(conn, actor_label(actor), "반려", "reservation", rid, {"reason": reason})
        notify.notify(conn, "반려", f"예약 반려: {r['vehicle_no']}", f"{_res_text(r)}\n반려 사유: {reason.strip()}",
                      emp_no=r["emp_no"], reservation_id=rid)
    _after()


def cancel(actor: dict, rid: int, reason: str = "") -> None:
    with data.tx() as conn:
        r = _load_res(conn, rid)
        _require(r["emp_no"] == actor["emp_no"] or can_manage_site(actor, r["site"]), "본인 예약만 취소할 수 있습니다.")
        if r["status"] not in (ST_PENDING, ST_APPROVED):
            raise ServiceError(f"승인대기/승인완료 예약만 취소할 수 있습니다. (현재: {r['status']})")
        _set_status(conn, rid, ST_CANCELED, cancel_reason=(reason or "").strip())
        data.audit(conn, actor_label(actor), "취소", "reservation", rid, {"reason": reason})
        conn.execute("UPDATE change_requests SET status='취소' WHERE reservation_id=? AND status='대기'", (rid,))
        if r["emp_no"] != actor["emp_no"]:
            notify.notify(conn, "취소", f"예약 취소: {r['vehicle_no']}", f"{_res_text(r)} 예약이 관리자에 의해 취소되었습니다. {reason}",
                          emp_no=r["emp_no"], reservation_id=rid)
        else:
            notify.notify_managers(conn, "취소", f"예약 취소: {r['user_name']} · {r['vehicle_no']}",
                                   f"{_res_text(r)} / 사유: {reason or '-'}", site=r["site"], reservation_id=rid)
    _after()


def request_change(actor: dict, rid: int, new_start: datetime, new_end: datetime, reason: str = "") -> str:
    """출발 전 기간 변경. 승인대기면 바로 바꾸고, 승인완료면 관리자 승인을 받는다."""
    settings = data.get_settings()
    validate_period(new_start, new_end, settings)
    with data.tx() as conn:
        r = _load_res(conn, rid)
        _require(r["emp_no"] == actor["emp_no"], "본인 예약만 변경할 수 있습니다.")
        if r["status"] not in (ST_PENDING, ST_APPROVED):
            raise ServiceError("출발 전(승인대기/승인완료) 예약만 변경할 수 있습니다.")
        if to_dt(r["start_dt"]) <= data.now():
            raise ServiceError("이미 시작 시간이 지난 예약은 변경할 수 없습니다.")
        v = dict(conn.execute("SELECT * FROM vehicles WHERE vehicle_no=?", (r["vehicle_no"],)).fetchone())
        block = vehicle_block_reason(conn, v, new_start, new_end, exclude_id=rid) or \
            user_block_reason(conn, actor["emp_no"], new_start, new_end, settings, exclude_id=rid)
        if block:
            raise ServiceError(block)
        if r["status"] == ST_PENDING:
            conn.execute("UPDATE reservations SET start_dt=?, end_dt=?, updated_at=? WHERE id=?",
                         (dt_str(new_start), dt_str(new_end), data.now_str(), rid))
            data.audit(conn, actor_label(actor), "기간 변경(승인 전)", "reservation", rid,
                       {"from": [r["start_dt"], r["end_dt"]], "to": [dt_str(new_start), dt_str(new_end)]})
            msg = "승인 전 예약이라 기간을 바로 변경했습니다."
        else:
            if conn.execute("SELECT 1 FROM change_requests WHERE reservation_id=? AND status='대기'", (rid,)).fetchone():
                raise ServiceError("이미 처리 대기 중인 변경 요청이 있습니다.")
            conn.execute("""INSERT INTO change_requests(reservation_id,new_start_dt,new_end_dt,reason,status,requested_by,requested_at)
                            VALUES(?,?,?,?,'대기',?,?)""",
                         (rid, dt_str(new_start), dt_str(new_end), reason.strip(), actor["emp_no"], data.now_str()))
            data.audit(conn, actor_label(actor), "기간 변경 요청", "reservation", rid,
                       {"to": [dt_str(new_start), dt_str(new_end)], "reason": reason})
            notify.notify_managers(conn, "변경요청", f"기간 변경 요청: {r['user_name']} · {r['vehicle_no']}",
                                   f"{_res_text(r)} → {dt_str(new_start)} ~ {dt_str(new_end)} / 사유: {reason or '-'}",
                                   site=r["site"], reservation_id=rid)
            msg = "승인된 예약이라 변경 요청을 보냈습니다. 관리자가 승인하면 반영됩니다."
    _after()
    return msg


def decide_change(actor: dict, cr_id: int, approve_it: bool, note: str = "") -> None:
    settings = data.get_settings()
    with data.tx() as conn:
        cr = conn.execute("SELECT * FROM change_requests WHERE id=?", (cr_id,)).fetchone()
        if not cr or cr["status"] != "대기":
            raise ServiceError("처리할 수 없는 변경 요청입니다.")
        r = _load_res(conn, cr["reservation_id"])
        _require(can_manage_site(actor, r["site"]), "이 현장 예약의 변경을 처리할 권한이 없습니다.")
        if approve_it:
            if r["status"] not in (ST_PENDING, ST_APPROVED):
                raise ServiceError("예약 상태가 바뀌어 변경을 적용할 수 없습니다.")
            ns, ne = to_dt(cr["new_start_dt"]), to_dt(cr["new_end_dt"])
            v = dict(conn.execute("SELECT * FROM vehicles WHERE vehicle_no=?", (r["vehicle_no"],)).fetchone())
            block = vehicle_block_reason(conn, v, ns, ne, exclude_id=r["id"]) or \
                user_block_reason(conn, r["emp_no"], ns, ne, settings, exclude_id=r["id"])
            if block:
                raise ServiceError(f"지금은 적용할 수 없습니다: {block}")
            conn.execute("UPDATE reservations SET start_dt=?, end_dt=?, updated_at=? WHERE id=?",
                         (cr["new_start_dt"], cr["new_end_dt"], data.now_str(), r["id"]))
        conn.execute("UPDATE change_requests SET status=?, decided_by=?, decided_at=?, decision_note=? WHERE id=?",
                     ("승인" if approve_it else "반려", actor_label(actor), data.now_str(), note, cr_id))
        data.audit(conn, actor_label(actor), "변경 요청 " + ("승인" if approve_it else "반려"), "reservation", r["id"], {"note": note})
        notify.notify(conn, "변경결과", f"기간 변경 {'승인' if approve_it else '반려'}: {r['vehicle_no']}",
                      f"요청 기간 {cr['new_start_dt']} ~ {cr['new_end_dt']} / {note or ''}",
                      emp_no=r["emp_no"], reservation_id=r["id"])
    _after()


def pending_change_requests(emp: dict) -> pd.DataFrame:
    sites = manageable_sites(emp)
    if not sites:
        return pd.DataFrame()
    return data.query_df(
        f"""SELECT c.*, r.user_name, r.vehicle_no, r.start_dt, r.end_dt, r.status AS res_status, v.site
            FROM change_requests c JOIN reservations r ON r.id=c.reservation_id
            LEFT JOIN vehicles v ON v.vehicle_no=r.vehicle_no
            WHERE c.status='대기' AND v.site IN ({','.join('?' * len(sites))}) ORDER BY c.id""", sites)


def checkout(actor: dict, rid: int, km: int | None = None) -> None:
    """출차(키 수령). 시작 1시간 전부터 종료 전까지 가능."""
    with data.tx() as conn:
        r = _load_res(conn, rid)
        _require(r["emp_no"] == actor["emp_no"] or can_manage_site(actor, r["site"]), "본인 예약만 출차 처리할 수 있습니다.")
        if r["status"] != ST_APPROVED:
            raise ServiceError(f"승인완료 예약만 출차할 수 있습니다. (현재: {r['status']})")
        now = data.now()
        if now < to_dt(r["start_dt"]) - timedelta(hours=1):
            raise ServiceError("출차는 예약 시작 1시간 전부터 가능합니다.")
        if now >= to_dt(r["end_dt"]):
            raise ServiceError("예약 기간이 이미 끝났습니다.")
        odo = conn.execute("SELECT odometer FROM vehicles WHERE vehicle_no=?", (r["vehicle_no"],)).fetchone()[0] or 0
        km = int(km) if km else odo
        if km < odo:
            raise ServiceError(f"계기판 거리는 차량 누적주행거리({odo:,} km)보다 작을 수 없습니다.")
        _set_status(conn, rid, ST_OUT, checkout_at=data.now_str(), checkout_km=km)
        data.audit(conn, actor_label(actor), "출차", "reservation", rid, {"km": km})
    _after()


def save_upload(owner_type: str, owner_id: int, kind: str, filename: str, content: bytes) -> str:
    folder = data.UPLOAD_DIR / owner_type / str(owner_id)
    folder.mkdir(parents=True, exist_ok=True)
    ext = Path(filename).suffix.lower() or ".bin"
    path = folder / f"{kind}_{int(time.time() * 1000)}{ext}"
    path.write_bytes(content)
    try:
        return str(path.relative_to(data.BASE_DIR))
    except ValueError:
        return str(path)


def return_vehicle(actor: dict, rid: int, *, return_km: int, commute_km: int, personal_km: int = 0, use_date: date, fuel_level: str,
                   parking_location: str, note: str = "", odometer_photo: tuple[str, bytes] | None = None,
                   damage: dict | None = None, expenses: list[dict] | None = None) -> str:
    """반납. 출퇴근 거리를 분리해 업무용 사용거리를 계산하고 운행기록으로 남긴다.
    damage = {"itype","description","photos":[(name, bytes)]}, expenses = [{"etype","amount","note","receipt":(name,bytes)}]"""
    if not (parking_location or "").strip():
        raise ServiceError("반납(주차) 위치를 입력해 주세요. 예: 지하 2층 B-12")
    with data.tx() as conn:
        r = _load_res(conn, rid)
        _require(r["emp_no"] == actor["emp_no"] or can_manage_site(actor, r["site"]), "본인 예약만 반납할 수 있습니다.")
        if r["status"] not in (ST_OUT, ST_OVERDUE):
            raise ServiceError(f"출차/연체 상태의 예약만 반납할 수 있습니다. (현재: {r['status']})")
        odo = conn.execute("SELECT odometer FROM vehicles WHERE vehicle_no=?", (r["vehicle_no"],)).fetchone()[0] or 0
        start_km = r["checkout_km"] if r["checkout_km"] is not None else odo
        return_km = int(return_km)
        if return_km < start_km:
            raise ServiceError(f"주행 후 계기판 거리는 출발 시 거리({start_km:,} km) 이상이어야 합니다.")
        distance = return_km - start_km
        commute_km, personal_km = int(commute_km or 0), int(personal_km or 0)
        if commute_km < 0 or personal_km < 0 or commute_km + personal_km > distance:
            raise ServiceError(f"출퇴근 거리 + 개인 용도 거리는 0 ~ 주행거리({distance:,} km) 사이여야 합니다.")
        general_km = distance - commute_km - personal_km   # ⑨ 일반 업무용
        business_km = commute_km + general_km              # 업무용 사용거리(⑧+⑨)
        start_d, end_d = to_date(r["start_dt"]), to_date(r["end_dt"])
        if not (start_d <= use_date <= max(end_d, date.today())):
            raise ServiceError(f"사용일자는 {start_d} ~ {max(end_d, date.today())} 사이로 선택해 주세요.")
        was_overdue = r["status"] == ST_OVERDUE
        _set_status(conn, rid, ST_DONE, return_at=data.now_str(), return_km=return_km, distance=distance,
                    commute_km=commute_km, personal_km=personal_km, business_km=business_km, use_date=use_date.strftime(data.D_FMT),
                    fuel_level=fuel_level, parking_location=parking_location.strip(), return_note=note.strip(),
                    was_overdue=int(was_overdue), checkout_km=start_km)
        conn.execute("UPDATE vehicles SET odometer=MAX(odometer, ?), parking_location=?, fuel_level=? WHERE vehicle_no=?",
                     (return_km, parking_location.strip(), fuel_level, r["vehicle_no"]))
        if odometer_photo:
            p = save_upload("reservation", rid, "계기판", *odometer_photo)
            conn.execute("INSERT INTO attachments(owner_type,owner_id,kind,path,created_at) VALUES('reservation',?,?,?,?)",
                         (rid, "계기판", p, data.now_str()))
        if damage and (damage.get("description") or "").strip():
            cur = conn.execute("""INSERT INTO incidents(vehicle_no,reservation_id,itype,description,occurred_at,reported_by,status,created_at)
                                  VALUES(?,?,?,?,?,?,'접수',?)""",
                               (r["vehicle_no"], rid, damage.get("itype", "손상"), damage["description"].strip(),
                                data.now_str(), actor_label(actor), data.now_str()))
            for name, content in damage.get("photos") or []:
                p = save_upload("incident", cur.lastrowid, "손상", name, content)
                conn.execute("INSERT INTO attachments(owner_type,owner_id,kind,path,created_at) VALUES('incident',?,?,?,?)",
                             (cur.lastrowid, "손상", p, data.now_str()))
            notify.notify_managers(conn, "손상신고", f"{damage.get('itype', '손상')} 신고: {r['vehicle_no']}",
                                   f"{_res_text(r)} 반납 시 신고: {damage['description'].strip()}",
                                   site=r["site"], reservation_id=rid, vehicle_no=r["vehicle_no"])
        for e in expenses or []:
            if not e.get("amount"):
                continue
            cur = conn.execute("""INSERT INTO expenses(reservation_id,vehicle_no,emp_no,etype,amount,spent_on,note,created_at)
                                  VALUES(?,?,?,?,?,?,?,?)""",
                               (rid, r["vehicle_no"], r["emp_no"], e["etype"], int(e["amount"]),
                                use_date.strftime(data.D_FMT), e.get("note", ""), data.now_str()))
            if e.get("receipt"):
                p = save_upload("expense", cur.lastrowid, "영수증", *e["receipt"])
                conn.execute("INSERT INTO attachments(owner_type,owner_id,kind,path,created_at) VALUES('expense',?,?,?,?)",
                             (cur.lastrowid, "영수증", p, data.now_str()))
        data.audit(conn, actor_label(actor), "반납", "reservation", rid,
                   {"return_km": return_km, "distance": distance, "commute_km": commute_km,
                    "personal_km": personal_km, "overdue": was_overdue})
    _after()
    return (f"반납 완료: 주행 {distance:,} km · 업무용 {business_km:,} km "
            f"(출퇴근 {commute_km:,} + 일반업무 {general_km:,}) · 개인 용도 {personal_km:,} km")


def mark_noshow(actor: dict, rid: int) -> None:
    with data.tx() as conn:
        r = _load_res(conn, rid)
        _require(can_manage_site(actor, r["site"]))
        if r["status"] != ST_APPROVED or to_dt(r["start_dt"]) > data.now():
            raise ServiceError("시작 시간이 지났는데 출차하지 않은 승인완료 예약만 노쇼 처리할 수 있습니다.")
        _set_status(conn, rid, ST_NOSHOW)
        data.audit(conn, actor_label(actor), "노쇼 처리", "reservation", rid)
        notify.notify(conn, "노쇼", f"노쇼 처리: {r['vehicle_no']}", f"{_res_text(r)} 예약을 사용하지 않아 노쇼로 기록되었습니다.",
                      emp_no=r["emp_no"], reservation_id=rid)
    _after()


# =========================================================================
# 자동 작업 (스케줄러)
# =========================================================================
_last_tick = 0.0


def run_scheduled_jobs(force: bool = False, now: datetime | None = None) -> dict:
    """앱이 열려 있을 때 주기적으로 호출. 같은 작업을 여러 번 실행해도 결과가 같다(멱등)."""
    global _last_tick
    if not force and time.time() - _last_tick < 30:
        return {}
    _last_tick = time.time()
    now = now or data.now()
    settings = data.get_settings()
    today = now.date()
    summary = {"auto_approved": 0, "expired": 0, "noshow": 0, "overdue": 0, "reminders": 0, "alerts": 0}
    with data.tx() as conn:
        # 1) 자동 승인: 시작일 전날 설정 시각이 지난 승인대기 (아직 시작 전인 건)
        if settings.get("auto_approve_enabled"):
            for row in conn.execute("SELECT * FROM reservations WHERE status=? AND start_dt>?", (ST_PENDING, dt_str(now))).fetchall():
                r = dict(row)
                start, end = to_dt(r["start_dt"]), to_dt(r["end_dt"])
                if now < auto_approve_cutoff(start, settings):
                    continue
                m = find_maintenance_conflict(conn, r["vehicle_no"], start, end)
                if m:
                    site = conn.execute("SELECT site FROM vehicles WHERE vehicle_no=?", (r["vehicle_no"],)).fetchone()
                    notify.notify_managers(conn, "정비만기", f"자동 승인 보류: #{r['id']} 정비 일정 충돌",
                                           f"{_res_text(r)} — 정비 {m['start_date']}~{m['end_date']}. 직접 처리해 주세요.",
                                           site=site[0] if site else None, reservation_id=r["id"],
                                           dedupe_key=f"autohold:{r['id']}")
                    continue
                _approve_in_tx(conn, r, "SYSTEM(자동승인)", auto=True)
                summary["auto_approved"] += 1

        # 2) 자동 만료: 시작 시간이 지났는데 승인대기
        for row in conn.execute("SELECT * FROM reservations WHERE status=? AND start_dt<=?", (ST_PENDING, dt_str(now))).fetchall():
            r = dict(row)
            _set_status(conn, r["id"], ST_EXPIRED)
            data.audit(conn, SYSTEM, "자동 만료", "reservation", r["id"])
            notify.notify(conn, "만료", f"예약 만료: {r['vehicle_no']}",
                          f"{_res_text(r)} 예약이 시작 시간까지 승인되지 않아 자동 만료되었습니다. 필요하면 다시 신청해 주세요.",
                          emp_no=r["emp_no"], reservation_id=r["id"])
            summary["expired"] += 1

        # 3) 노쇼: 승인완료인데 시작 + 유예시간까지 출차하지 않음
        grace = timedelta(hours=float(settings.get("noshow_grace_hours", 2)))
        for row in conn.execute("SELECT r.*, v.site FROM reservations r LEFT JOIN vehicles v USING(vehicle_no) WHERE r.status=?",
                                (ST_APPROVED,)).fetchall():
            r = dict(row)
            start, end = to_dt(r["start_dt"]), to_dt(r["end_dt"])
            if now >= min(start + grace, end):
                _set_status(conn, r["id"], ST_NOSHOW)
                data.audit(conn, SYSTEM, "자동 노쇼", "reservation", r["id"])
                notify.notify(conn, "노쇼", f"노쇼 기록: {r['vehicle_no']}",
                              f"{_res_text(r)} 예약 후 출차 기록이 없어 노쇼로 기록되었습니다. 사용하지 않을 때는 미리 취소해 주세요.",
                              emp_no=r["emp_no"], reservation_id=r["id"])
                notify.notify_managers(conn, "노쇼", f"노쇼: {r['user_name']} · {r['vehicle_no']}", _res_text(r),
                                       site=r["site"], reservation_id=r["id"], dedupe_key=f"noshow:{r['id']}")
                summary["noshow"] += 1

        # 4) 연체: 출차 후 종료 시간이 지났는데 반납하지 않음
        for row in conn.execute("SELECT r.*, v.site FROM reservations r LEFT JOIN vehicles v USING(vehicle_no) WHERE r.status IN (?,?)",
                                (ST_OUT, ST_OVERDUE)).fetchall():
            r = dict(row)
            if to_dt(r["end_dt"]) >= now:
                continue
            if r["status"] == ST_OUT:
                _set_status(conn, r["id"], ST_OVERDUE)
                data.audit(conn, SYSTEM, "연체 전환", "reservation", r["id"])
                summary["overdue"] += 1
            days = (today - to_date(r["end_dt"])).days
            notify.notify(conn, "연체", f"반납 기한 경과: {r['vehicle_no']}",
                          f"{_res_text(r)} 반납 기한이 지났습니다({days}일). 바로 반납 처리해 주세요.",
                          emp_no=r["emp_no"], reservation_id=r["id"], dedupe_key=f"overdue:{r['id']}:{today}")
            notify.notify_managers(conn, "연체", f"연체: {r['user_name']} · {r['vehicle_no']}",
                                   f"{_res_text(r)} / 기한 경과 {days}일", site=r["site"], reservation_id=r["id"],
                                   dedupe_key=f"overdue:{r['id']}:{today}")

        # 5) 출발 전날 리마인드
        hh, mm = (int(x) for x in str(settings.get("remind_day_before_time", "15:00")).split(":"))
        if (now.hour, now.minute) >= (hh, mm):
            tomorrow = (today + timedelta(days=1)).strftime(data.D_FMT)
            for row in conn.execute("SELECT r.*, v.key_location FROM reservations r LEFT JOIN vehicles v USING(vehicle_no)"
                                    " WHERE r.status=? AND substr(r.start_dt,1,10)=?", (ST_APPROVED, tomorrow)).fetchall():
                r = dict(row)
                if notify.notify(conn, "출발리마인드", f"내일 출발: {r['vehicle_no']}",
                                 f"{_res_text(r)}\n차키 위치: {r['key_location'] or '-'}\n{settings.get('key_pickup_guide', '')}",
                                 emp_no=r["emp_no"], reservation_id=r["id"], dedupe_key=f"remind_start:{r['id']}"):
                    summary["reminders"] += 1

        # 6) 반납 리마인드: 종료 N시간 전
        hours = float(settings.get("return_remind_hours", 1))
        for row in conn.execute("SELECT * FROM reservations WHERE status=?", (ST_OUT,)).fetchall():
            r = dict(row)
            end = to_dt(r["end_dt"])
            if now < end <= now + timedelta(hours=hours):
                if notify.notify(conn, "반납리마인드", f"반납 시간 안내: {r['vehicle_no']}",
                                 f"{_res_text(r)} 반납 시간이 다가옵니다. 반납 시 계기판 사진과 주차 위치를 입력해 주세요.",
                                 emp_no=r["emp_no"], reservation_id=r["id"], dedupe_key=f"remind_return:{r['id']}"):
                    summary["reminders"] += 1

        # 7) 정비 일정 상태 갱신
        conn.execute("UPDATE maintenance SET status='진행' WHERE status='예정' AND start_date<=?", (today.strftime(data.D_FMT),))

        # 8) 차량 정비·만기 알림 (월 1회 반복)
        month = today.strftime("%Y-%m")
        vehicles = [dict(v) for v in conn.execute("SELECT * FROM vehicles WHERE active=1").fetchall()]
        overdue_maint = {m["vehicle_no"]: dict(m) for m in conn.execute(
            "SELECT * FROM maintenance WHERE status='진행' AND end_date<?", (today.strftime(data.D_FMT),)).fetchall()}
        for v in vehicles:
            for a in vehicle_alerts(v, settings, today, overdue_maint.get(v["vehicle_no"])):
                if notify.notify(conn, "정비만기", f"[{a['level']}] {v['vehicle_no']} {a['type']}", a["message"],
                                 role=ROLE_ADMIN, vehicle_no=v["vehicle_no"],
                                 dedupe_key=f"valert:{v['vehicle_no']}:{a['type']}:{a['level']}:{month}:admin"):
                    summary["alerts"] += 1
                notify.notify(conn, "정비만기", f"[{a['level']}] {v['vehicle_no']} {a['type']}", a["message"],
                              role=ROLE_SITE, site=v["site"], vehicle_no=v["vehicle_no"],
                              dedupe_key=f"valert:{v['vehicle_no']}:{a['type']}:{a['level']}:{month}:site")
    _after()
    return summary


def _after() -> None:
    """트랜잭션 후 외부 채널 발송 (실패해도 업무 흐름은 계속)."""
    try:
        notify.dispatch_pending()
    except Exception:
        pass


# =========================================================================
# 정비 · 만기 판정
# =========================================================================
def vehicle_alerts(v: dict, settings: dict, today: date | None = None, overdue_maint: dict | None = None) -> list[dict]:
    """주행거리·날짜 기준 자동 판정. level: '교체 필요'(기준 초과) / '임박'(기준의 N% 도달 또는 만기 D-N)."""
    today = today or date.today()
    out = []
    odo = int(v.get("odometer") or 0)
    ratio = float(settings.get("maint_warn_ratio", 0.9))

    def km_check(label, last, interval):
        if not interval:
            return
        used = odo - int(last or 0)
        if used >= interval:
            out.append({"level": "교체 필요", "type": label, "message": f"{label}: 마지막 교체 후 {used:,} km 주행 (기준 {interval:,} km)"})
        elif used >= interval * ratio:
            out.append({"level": "임박", "type": label, "message": f"{label}: {interval - used:,} km 남음 (기준 {interval:,} km)"})

    km_check("엔진오일·필터", v.get("last_oil_km"), int(settings.get("oil_interval_km", 10000)))
    km_check("타이어", v.get("last_tire_km"), int(settings.get("tire_interval_km", 40000)))

    def date_check(label, due, warn_days, overdue_level="만료"):
        d = to_date(due)
        if not d:
            return
        left = (d - today).days
        if left < 0:
            out.append({"level": overdue_level, "type": label, "message": f"{label}: {d} ({-left}일 지남)"})
        elif left <= warn_days:
            out.append({"level": "임박", "type": label, "message": f"{label}: {d} (D-{left})"})

    warn = int(settings.get("expiry_warn_days", 30))
    if v.get("kind") == data.KIND_EXEC and v.get("purchase_date"):
        pd_ = to_date(v["purchase_date"])
        years = int(settings.get("replace_years", 4))
        try:
            due = pd_.replace(year=pd_.year + years)
        except ValueError:
            due = pd_ + timedelta(days=365 * years)
        date_check(f"차량 교체({years}년)", due, 90, overdue_level="교체 필요")
    if v.get("kind") == KIND_WORK:
        date_check("렌트 계약 만료", v.get("rent_end"), int(settings.get("rent_expiry_warn_days", 60)))
        limit, start_km = v.get("rent_km_limit"), v.get("rent_start_km")
        if limit and start_km is not None:
            used = odo - int(start_km)
            if used >= limit:
                out.append({"level": "초과", "type": "렌트 주행한도", "message": f"계약 주행한도 초과: {used:,}/{int(limit):,} km"})
            elif used >= limit * float(settings.get("rent_km_warn_ratio", 0.9)):
                out.append({"level": "임박", "type": "렌트 주행한도", "message": f"계약 주행한도 {used:,}/{int(limit):,} km"})
    date_check("보험 만기", v.get("insurance_expiry"), warn)
    date_check("정기검사", v.get("inspection_due"), warn)
    date_check("자동차세 납부", v.get("car_tax_due"), warn)
    if overdue_maint:
        out.append({"level": "확인 필요", "type": "정비 완료 처리",
                    "message": f"정비 예정 완료일({overdue_maint['end_date']})이 지났습니다. 완료 처리하거나 기간을 연장해 주세요."})
    return out


def all_vehicle_alerts(emp: dict) -> pd.DataFrame:
    settings = data.get_settings()
    sites = manageable_sites(emp)
    today = date.today()
    overdue = {m["vehicle_no"]: m for m in data.query_all(
        "SELECT * FROM maintenance WHERE status='진행' AND end_date<?", (today.strftime(data.D_FMT),))}
    rows = []
    for v in data.query_all("SELECT * FROM vehicles WHERE active=1"):
        if v["site"] not in sites:
            continue
        for a in vehicle_alerts(v, settings, today, overdue.get(v["vehicle_no"])):
            rows.append({"차량번호": v["vehicle_no"], "차종": v["model"], "현장": v["site"],
                         "구분": vehicle_use_label(v["kind"]), "수준": a["level"], "항목": a["type"], "내용": a["message"]})
    return pd.DataFrame(rows)


def maintenance_df(emp: dict, statuses=("예정", "진행")) -> pd.DataFrame:
    sites = manageable_sites(emp)
    if not sites:
        return pd.DataFrame()
    return data.query_df(
        f"""SELECT m.*, v.model, v.site FROM maintenance m JOIN vehicles v USING(vehicle_no)
            WHERE m.status IN ({','.join('?' * len(statuses))}) AND v.site IN ({','.join('?' * len(sites))})
            ORDER BY m.start_date""", [*statuses, *sites])


def add_maintenance(actor: dict, vno: str, start: date, end: date, mtype: str, vendor: str = "",
                    description: str = "", cost: int | None = None) -> list[dict]:
    """정비 일정 등록. 겹치는 예약 목록을 돌려주고 담당자에게 알린다."""
    if not start or not end:
        raise ServiceError("정비 시작일과 예정 완료일은 필수입니다.")
    if end < start:
        raise ServiceError("예정 완료일은 시작일보다 빠를 수 없습니다.")
    with data.tx() as conn:
        v = conn.execute("SELECT * FROM vehicles WHERE vehicle_no=?", (vno,)).fetchone()
        if not v:
            raise ServiceError("차량을 찾을 수 없습니다.")
        _require(can_manage_site(actor, v["site"]), "이 현장 차량의 정비를 등록할 권한이 없습니다.")
        status = "진행" if start <= date.today() else "예정"
        cur = conn.execute("""INSERT INTO maintenance(vehicle_no,start_date,end_date,mtype,vendor,description,cost,status,created_by,created_at)
                              VALUES(?,?,?,?,?,?,?,?,?,?)""",
                           (vno, start.strftime(data.D_FMT), end.strftime(data.D_FMT), mtype, vendor, description, cost,
                            status, actor_label(actor), data.now_str()))
        data.audit(conn, actor_label(actor), "정비 등록", "maintenance", cur.lastrowid,
                   {"vehicle": vno, "start": str(start), "end": str(end), "type": mtype})
        affected = [dict(r) for r in conn.execute(
            f"""SELECT * FROM reservations WHERE vehicle_no=? AND status IN ({','.join('?' * len(OCCUPYING_STATUSES))})
                AND substr(start_dt,1,10)<=? AND substr(end_dt,1,10)>=?""",
            (vno, *OCCUPYING_STATUSES, end.strftime(data.D_FMT), start.strftime(data.D_FMT))).fetchall()]
        for r in affected:
            notify.notify_managers(conn, "정비만기", f"정비 일정과 겹치는 예약: #{r['id']}",
                                   f"{_res_text(r)} ({r['user_name']}) — 차량 변경이나 일정 조정이 필요합니다.",
                                   site=v["site"], reservation_id=r["id"], dedupe_key=f"maintclash:{cur.lastrowid}:{r['id']}")
    _after()
    return affected


def complete_maintenance(actor: dict, mid: int, *, end_date: date, cost: int | None, odometer: int | None,
                         description: str = "", reset_oil: bool = False, reset_tire: bool = False) -> None:
    with data.tx() as conn:
        m = conn.execute("SELECT m.*, v.site, v.odometer AS v_odo FROM maintenance m JOIN vehicles v USING(vehicle_no) WHERE m.id=?",
                         (mid,)).fetchone()
        if not m:
            raise ServiceError("정비 일정을 찾을 수 없습니다.")
        _require(can_manage_site(actor, m["site"]))
        if m["status"] in ("완료", "취소"):
            raise ServiceError("이미 종료된 정비입니다.")
        odo = int(odometer) if odometer else int(m["v_odo"] or 0)
        conn.execute("UPDATE maintenance SET status='완료', end_date=?, cost=?, odometer=?, description=? WHERE id=?",
                     (end_date.strftime(data.D_FMT), cost, odo, description or m["description"], mid))
        sets, vals = ["last_maintenance_date=?", "odometer=MAX(odometer, ?)"], [end_date.strftime(data.D_FMT), odo]
        if reset_oil:
            sets.append("last_oil_km=?")
            vals.append(odo)
        if reset_tire:
            sets.append("last_tire_km=?")
            vals.append(odo)
        conn.execute(f"UPDATE vehicles SET {', '.join(sets)} WHERE vehicle_no=?", (*vals, m["vehicle_no"]))
        data.audit(conn, actor_label(actor), "정비 완료", "maintenance", mid,
                   {"cost": cost, "odometer": odo, "oil": reset_oil, "tire": reset_tire})


def update_maintenance_period(actor: dict, mid: int, start: date, end: date) -> None:
    if end < start:
        raise ServiceError("예정 완료일은 시작일보다 빠를 수 없습니다.")
    with data.tx() as conn:
        m = conn.execute("SELECT m.*, v.site FROM maintenance m JOIN vehicles v USING(vehicle_no) WHERE m.id=?", (mid,)).fetchone()
        if not m:
            raise ServiceError("정비 일정을 찾을 수 없습니다.")
        _require(can_manage_site(actor, m["site"]))
        conn.execute("UPDATE maintenance SET start_date=?, end_date=? WHERE id=?",
                     (start.strftime(data.D_FMT), end.strftime(data.D_FMT), mid))
        data.audit(conn, actor_label(actor), "정비 기간 변경", "maintenance", mid, {"start": str(start), "end": str(end)})


def cancel_maintenance(actor: dict, mid: int) -> None:
    with data.tx() as conn:
        m = conn.execute("SELECT m.*, v.site FROM maintenance m JOIN vehicles v USING(vehicle_no) WHERE m.id=?", (mid,)).fetchone()
        if not m:
            raise ServiceError("정비 일정을 찾을 수 없습니다.")
        _require(can_manage_site(actor, m["site"]))
        conn.execute("UPDATE maintenance SET status='취소' WHERE id=?", (mid,))
        data.audit(conn, actor_label(actor), "정비 취소", "maintenance", mid)


# =========================================================================
# 차량 관리
# =========================================================================
VEHICLE_FIELDS = ["model", "kind", "site", "seats", "fuel_type", "body_type", "assigned_user", "assigned_user_phone",
                  "manager", "manager_phone", "odometer", "purchase_date", "purchase_cost", "rent_company", "rent_start",
                  "rent_end", "rent_monthly_fee", "rent_km_limit", "rent_start_km", "insurance_expiry", "inspection_due",
                  "car_tax_due", "hipass_card", "key_location", "parking_location", "last_oil_km", "last_tire_km",
                  "last_maintenance_date", "note"]


def save_vehicle(actor: dict, values: dict, is_new: bool) -> None:
    vno = (values.get("vehicle_no") or "").strip()
    if not vno or not (values.get("model") or "").strip():
        raise ServiceError("차량번호와 차종은 필수입니다.")
    if values.get("kind") == data.KIND_EXEC and not values.get("purchase_date"):
        raise ServiceError("구매(임원용) 차량은 구매일이 필요합니다. (교체 시기 자동 판정에 사용)")
    if values.get("kind") == KIND_WORK and not (values.get("rent_start") and values.get("rent_end")):
        raise ServiceError("렌트(업무용) 차량은 계약 시작일과 만료일이 필요합니다.")
    if values.get("rent_start") and values.get("rent_end") and values["rent_end"] < values["rent_start"]:
        raise ServiceError("렌트 만료일은 시작일보다 늦어야 합니다.")
    for k, v in list(values.items()):
        if isinstance(v, date):
            values[k] = v.strftime(data.D_FMT)
    with data.tx() as conn:
        old = conn.execute("SELECT * FROM vehicles WHERE vehicle_no=?", (vno,)).fetchone()
        _require(can_manage_site(actor, values["site"]), "다른 현장 차량은 등록·수정할 수 없습니다.")
        if is_new:
            if old and old["active"]:
                raise ServiceError(f"이미 등록된 차량번호입니다: {vno}")
            if old:  # 삭제됐던 차량 재등록
                conn.execute("DELETE FROM vehicles WHERE vehicle_no=?", (vno,))
            cols = ["vehicle_no"] + VEHICLE_FIELDS
            conn.execute(f"INSERT INTO vehicles({','.join(cols)}) VALUES({','.join('?' * len(cols))})",
                         [vno] + [values.get(c) for c in VEHICLE_FIELDS])
            data.audit(conn, actor_label(actor), "차량 추가", "vehicle", vno, values)
        else:
            if not old:
                raise ServiceError("차량을 찾을 수 없습니다.")
            _require(can_manage_site(actor, old["site"]), "다른 현장 차량은 수정할 수 없습니다.")
            changed = {c: values.get(c) for c in VEHICLE_FIELDS if c in values and str(values.get(c)) != str(old[c])}
            if changed:
                conn.execute(f"UPDATE vehicles SET {', '.join(f'{c}=?' for c in changed)} WHERE vehicle_no=?",
                             (*changed.values(), vno))
                data.audit(conn, actor_label(actor), "차량 수정", "vehicle", vno,
                           {c: [old[c], changed[c]] for c in changed})


def delete_vehicle(actor: dict, vno: str) -> None:
    _require(is_admin(actor), "차량 삭제는 총무 관리자만 할 수 있습니다.")
    with data.tx() as conn:
        active = conn.execute(f"SELECT COUNT(*) FROM reservations WHERE vehicle_no=? AND status IN ({','.join('?' * len(OCCUPYING_STATUSES))})",
                              (vno, *OCCUPYING_STATUSES)).fetchone()[0]
        if active:
            raise ServiceError(f"진행 중이거나 예정된 예약이 {active}건 있어 삭제할 수 없습니다.")
        conn.execute("UPDATE vehicles SET active=0 WHERE vehicle_no=?", (vno,))
        data.audit(conn, actor_label(actor), "차량 삭제(비활성)", "vehicle", vno)


def vehicle_status_now(v: dict, res_by_vehicle: dict, maint_today: set) -> str:
    if v["vehicle_no"] in maint_today:
        return "정비중"
    statuses = res_by_vehicle.get(v["vehicle_no"], set())
    if ST_OVERDUE in statuses:
        return "연체(미반납)"
    if ST_OUT in statuses:
        return "운행중"
    if ST_APPROVED in statuses or ST_PENDING in statuses:
        return "오늘 예약"
    if v["kind"] == data.KIND_EXEC:
        return "임원 배정"
    return "대기(운행가능)"


def fleet_status_today(sites: list[str] | None = None) -> pd.DataFrame:
    now = data.now()
    today = now.date().strftime(data.D_FMT)
    vehicles = data.query_all("SELECT * FROM vehicles WHERE active=1")
    maint = {m["vehicle_no"] for m in data.query_all(
        "SELECT vehicle_no FROM maintenance WHERE status IN ('예정','진행') AND start_date<=? AND end_date>=?", (today, today))}
    res_by = {}
    for r in data.query_all(
            f"SELECT * FROM reservations WHERE status IN ({','.join('?' * len(OCCUPYING_STATUSES))})", OCCUPYING_STATUSES):
        s, e = effective_interval(r, now)
        if s.date() <= now.date() <= e.date():
            res_by.setdefault(r["vehicle_no"], set()).add(r["status"])
    rows = []
    for v in vehicles:
        if sites and v["site"] not in sites:
            continue
        rows.append({"차량번호": v["vehicle_no"], "차종": v["model"], "구분": vehicle_use_label(v["kind"]),
                     "현장": v["site"], "상태": vehicle_status_now(v, res_by, maint),
                     "누적주행거리": v["odometer"], "주차 위치": v["parking_location"] or "", "차키 위치": v["key_location"] or ""})
    return pd.DataFrame(rows)


# =========================================================================
# 과태료 운전자 조회
# =========================================================================
def find_driver(vno: str, when: datetime) -> list[dict]:
    rows = data.query_all(RES_SELECT + " WHERE r.vehicle_no=? AND r.status IN (?,?,?,?)",
                          (vno, ST_OUT, ST_OVERDUE, ST_DONE, ST_APPROVED))
    out = []
    for r in rows:
        s, e = effective_interval(r)
        if r["status"] == ST_DONE and r.get("return_at") and len(str(r["return_at"])) <= 10:
            e = max(e, to_dt(r["end_dt"]))  # 반납 시각이 날짜만 있는 이관 데이터
        if s <= when <= e:
            basis = "출차·반납 기록" if r.get("checkout_at") and r["status"] == ST_DONE else "예약 기간"
            out.append({**r, "근거": basis})
    v = get_vehicle(vno)
    if not out and v and v["kind"] == data.KIND_EXEC and v["assigned_user"]:
        out.append({"id": None, "user_name": v["assigned_user"], "dept": "임원 배정 차량", "start_dt": "-", "end_dt": "-",
                    "status": "배정", "근거": "임원 전담 배정 차량"})
    return out


# =========================================================================
# 통계
# =========================================================================
WORK_START, WORK_END = 9, 18


def _business_hours_between(s: datetime, e: datetime) -> float:
    total, cur = 0.0, s.date()
    while cur <= e.date():
        if is_business_day(cur):
            ws, we = datetime.combine(cur, datetime.min.time()).replace(hour=WORK_START), \
                datetime.combine(cur, datetime.min.time()).replace(hour=WORK_END)
            a, b = max(s, ws), min(e, we)
            if b > a:
                total += (b - a).total_seconds() / 3600
        cur += timedelta(days=1)
    return total


def utilization(start: date, end: date, sites: list[str] | None = None) -> pd.DataFrame:
    """업무용 차량 가동률 = 사용 시간(근무시간 09~18, 영업일 기준) / 가용 시간."""
    vehicles = [v for v in data.query_all("SELECT * FROM vehicles WHERE active=1 AND kind=?", (KIND_WORK,))
                if not sites or v["site"] in sites]
    period_s = datetime.combine(start, datetime.min.time())
    period_e = datetime.combine(end, datetime.max.time()).replace(microsecond=0)
    capacity_h = _business_hours_between(period_s, period_e)
    res = data.query_all("SELECT * FROM reservations WHERE status IN (?,?,?,?) AND start_dt<=? AND end_dt>=?",
                         (ST_APPROVED, ST_OUT, ST_OVERDUE, ST_DONE, dt_str(period_e), dt_str(period_s)))
    maint = data.query_all("SELECT * FROM maintenance WHERE status!='취소' AND start_date<=? AND end_date>=?",
                           (end.strftime(data.D_FMT), start.strftime(data.D_FMT)))
    used, trips, km, maint_h = {}, {}, {}, {}
    for r in res:
        s, e = effective_interval(r)
        h = _business_hours_between(max(s, period_s), min(e, period_e))
        used[r["vehicle_no"]] = used.get(r["vehicle_no"], 0) + h
        trips[r["vehicle_no"]] = trips.get(r["vehicle_no"], 0) + 1
        km[r["vehicle_no"]] = km.get(r["vehicle_no"], 0) + int(r["distance"] or 0)
    for m in maint:
        s = max(datetime.combine(to_date(m["start_date"]), datetime.min.time()), period_s)
        e = min(datetime.combine(to_date(m["end_date"]), datetime.max.time()).replace(microsecond=0), period_e)
        maint_h[m["vehicle_no"]] = maint_h.get(m["vehicle_no"], 0) + _business_hours_between(s, e)
    rows = []
    for v in vehicles:
        avail = max(capacity_h - maint_h.get(v["vehicle_no"], 0), 0)
        u = min(used.get(v["vehicle_no"], 0), avail) if avail else 0
        rows.append({"차량번호": v["vehicle_no"], "차종": v["model"], "현장": v["site"], "예약 건수": trips.get(v["vehicle_no"], 0),
                     "사용 시간(h)": round(u, 1), "가용 시간(h)": round(avail, 1),
                     "가동률": (u / avail) if avail else 0.0, "주행거리(km)": km.get(v["vehicle_no"], 0)})
    return pd.DataFrame(rows)


def monthly_trend(months: int = 6, sites: list[str] | None = None) -> pd.DataFrame:
    today = date.today()
    rows = []
    for i in range(months - 1, -1, -1):
        first = (today.replace(day=1) - pd.DateOffset(months=i)).date()
        last = (pd.Timestamp(first) + pd.offsets.MonthEnd(0)).date()
        u = utilization(first, min(last, today) if first <= today else last, sites)
        if u.empty:
            continue
        for site, g in u.groupby("현장"):
            avail = g["가용 시간(h)"].sum()
            rows.append({"월": first.strftime("%Y-%m"), "현장": site,
                         "가동률": (g["사용 시간(h)"].sum() / avail) if avail else 0})
    return pd.DataFrame(rows)


def dept_usage(start: date, end: date, sites: list[str] | None = None) -> pd.DataFrame:
    res = data.query_df(RES_SELECT + " WHERE r.status IN (?,?,?) AND substr(r.start_dt,1,10)<=? AND substr(r.end_dt,1,10)>=?",
                        (ST_DONE, ST_OUT, ST_OVERDUE, end.strftime(data.D_FMT), start.strftime(data.D_FMT)))
    if sites and not res.empty:
        res = res[res["site"].isin(sites)]
    exp = data.query_df("SELECT e.*, r.dept FROM expenses e LEFT JOIN reservations r ON r.id=e.reservation_id "
                        "WHERE e.spent_on BETWEEN ? AND ?", (start.strftime(data.D_FMT), end.strftime(data.D_FMT)))
    if res.empty:
        return pd.DataFrame(columns=["부서", "운행 건수", "주행거리(km)", "업무용(km)", "출퇴근(km)", "개인 용도(km)", "비용(원)"])
    res["dept"] = res["dept"].replace("", "미지정").fillna("미지정")
    g = res.groupby("dept").agg(운행건수=("id", "count"), 주행거리=("distance", "sum"), 업무용=("business_km", "sum"),
                                출퇴근=("commute_km", "sum"), 개인=("personal_km", "sum")).reset_index()
    cost = exp.groupby("dept")["amount"].sum() if not exp.empty else pd.Series(dtype=float)
    g["비용"] = g["dept"].map(cost).fillna(0).astype(int)
    g.columns = ["부서", "운행 건수", "주행거리(km)", "업무용(km)", "출퇴근(km)", "개인 용도(km)", "비용(원)"]
    return g.sort_values("주행거리(km)", ascending=False)


def failure_stats(start: date, end: date, sites: list[str] | None = None) -> dict:
    s, e = start.strftime(data.D_FMT), (end + timedelta(days=1)).strftime(data.D_FMT)
    fails = data.query_df("SELECT * FROM booking_failures WHERE ts>=? AND ts<?", (s, e))
    reqs = data.query_df(RES_SELECT + " WHERE r.requested_at>=? AND r.requested_at<?", (s, e))
    if sites:
        fails = fails[fails["site"].isin(sites) | (fails["site"] == "")] if not fails.empty else fails
        reqs = reqs[reqs["site"].isin(sites)] if not reqs.empty else reqs
    total = len(fails) + len(reqs)
    return {"failures": len(fails), "requests": len(reqs), "rate": (len(fails) / total) if total else 0.0,
            "by_site": fails.groupby("site").size().to_dict() if not fails.empty else {}, "rows": fails}


def status_counts(sites: list[str] | None = None) -> pd.DataFrame:
    df = fleet_status_today(sites)
    if df.empty:
        return df
    return df.groupby(["현장", "상태"]).size().reset_index(name="대수")


def today_tasks(emp: dict) -> dict:
    sites = manageable_sites(emp)
    today = date.today().strftime(data.D_FMT)
    res = scoped_reservations(emp)
    alerts = all_vehicle_alerts(emp)
    return {
        "pending": int((res["status"] == ST_PENDING).sum()) if not res.empty else 0,
        "overdue": int((res["status"] == ST_OVERDUE).sum()) if not res.empty else 0,
        "today_out": int(((res["status"] == ST_APPROVED) & (res["start_dt"].str[:10] == today)).sum()) if not res.empty else 0,
        "out_now": int((res["status"] == ST_OUT).sum()) if not res.empty else 0,
        "changes": len(pending_change_requests(emp)),
        "alerts": len(alerts),
        "incidents": len(data.query_all(
            f"SELECT i.id FROM incidents i JOIN vehicles v USING(vehicle_no) WHERE i.status!='완료' AND v.site IN ({','.join('?' * len(sites))})",
            sites)) if sites else 0,
    }


def runlog_rows(vno: str, start: date, end: date) -> list[dict]:
    """운행기록부용: 반납완료 건 (사용일자 기준)."""
    return data.query_all(
        "SELECT * FROM reservations WHERE vehicle_no=? AND status=? AND COALESCE(use_date, substr(start_dt,1,10)) BETWEEN ? AND ?"
        " ORDER BY COALESCE(use_date, start_dt), id",
        (vno, ST_DONE, start.strftime(data.D_FMT), end.strftime(data.D_FMT)))


def save_settings(actor: dict, values: dict) -> None:
    _require(is_admin(actor), "설정은 총무 관리자만 바꿀 수 있습니다.")
    old = data.get_settings()
    changed = {k: v for k, v in values.items() if old.get(k) != v}
    if not changed:
        return
    with data.tx() as conn:
        data.save_settings(changed, conn)
        safe = {k: ("***" if "password" in k or "key" in k.split("_")[-1] else v) for k, v in changed.items()}
        data.audit(conn, actor_label(actor), "설정 변경", "settings", "", safe)


def update_incident_status(actor: dict, iid: int, status: str) -> None:
    with data.tx() as conn:
        i = conn.execute("SELECT i.*, v.site FROM incidents i JOIN vehicles v USING(vehicle_no) WHERE i.id=?", (iid,)).fetchone()
        if not i:
            raise ServiceError("신고 내역을 찾을 수 없습니다.")
        _require(can_manage_site(actor, i["site"]))
        conn.execute("UPDATE incidents SET status=? WHERE id=?", (status, iid))
        data.audit(conn, actor_label(actor), "사고·손상 상태 변경", "incident", iid, {"status": status})


def incidents_df(emp: dict) -> pd.DataFrame:
    sites = manageable_sites(emp)
    if not sites:
        return pd.DataFrame()
    return data.query_df(
        f"""SELECT i.*, v.model, v.site, r.user_name FROM incidents i JOIN vehicles v USING(vehicle_no)
            LEFT JOIN reservations r ON r.id=i.reservation_id
            WHERE v.site IN ({','.join('?' * len(sites))}) ORDER BY i.id DESC""", sites)


def expenses_df(emp: dict, start: date, end: date) -> pd.DataFrame:
    sites = manageable_sites(emp)
    if not sites:
        return pd.DataFrame()
    return data.query_df(
        f"""SELECT e.*, v.site, r.user_name, r.dept FROM expenses e JOIN vehicles v USING(vehicle_no)
            LEFT JOIN reservations r ON r.id=e.reservation_id
            WHERE v.site IN ({','.join('?' * len(sites))}) AND e.spent_on BETWEEN ? AND ? ORDER BY e.spent_on DESC""",
        [*sites, start.strftime(data.D_FMT), end.strftime(data.D_FMT)])


def attachments_for(owner_type: str, owner_id: int) -> list[dict]:
    return data.query_all("SELECT * FROM attachments WHERE owner_type=? AND owner_id=?", (owner_type, owner_id))


def ai_context(emp: dict) -> dict:
    """관리자 AI 질의에 넘길 집계 데이터 (권한 범위 안)."""
    sites = manageable_sites(emp)
    today = date.today()
    first = today.replace(day=1)
    prev_last = first - timedelta(days=1)
    prev_first = prev_last.replace(day=1)
    util = utilization(first, today, sites)
    util_prev = utilization(prev_first, prev_last, sites)
    site_util = {}
    for name, frame in (("이번달", util), ("지난달", util_prev)):
        if not frame.empty:
            g = frame.groupby("현장").agg(차량수=("차량번호", "count"), 사용=("사용 시간(h)", "sum"), 가용=("가용 시간(h)", "sum"))
            site_util[name] = {s: {"차량수": int(r["차량수"]), "가동률": round(r["사용"] / r["가용"], 3) if r["가용"] else 0}
                               for s, r in g.iterrows()}
    fails = failure_stats(first, today, sites)
    return {
        "기준일": today.isoformat(),
        "조회 범위 현장": sites,
        "현장별 가동률": site_util,
        "이번달 차량별 가동률": util.sort_values("가동률")[["차량번호", "차종", "현장", "예약 건수", "가동률", "주행거리(km)"]]
        .round(3).to_dict("records") if not util.empty else [],
        "이번달 예약 실패": {"실패": fails["failures"], "신청": fails["requests"], "실패율": round(fails["rate"], 3),
                        "현장별 실패": fails["by_site"]},
        "오늘 할 일": today_tasks(emp),
        "정비·만기 알림": all_vehicle_alerts(emp).to_dict("records")[:40],
        "부서별 사용(이번달)": dept_usage(first, today, sites).to_dict("records"),
    }
