"""데이터 계층: SQLite 저장소, 스키마, 설정, 엑셀 최초 이관.

원본 데이터는 fleet.db(SQLite)에 저장한다. 차량목록/예약목록/운행일지 엑셀은
최초 실행 시 한 번만 읽어서 이관하며, 이후에는 '내보내기' 결과물로만 사용한다.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

BASE_DIR = Path(__file__).resolve().parent
# FLEET_DB 환경변수로 다른 DB 파일을 쓸 수 있다 (테스트·시연용 복사본 등)
DB_FILE = Path(os.environ.get("FLEET_DB") or BASE_DIR / "fleet.db")
UPLOAD_DIR = BASE_DIR / "uploads"
CAR_FILE = BASE_DIR / "차량목록.xlsx"
RESERVATION_FILE = BASE_DIR / "예약목록.xlsx"
RUN_LOG_FILE = BASE_DIR / "운행일지.xlsx"

DT_FMT = "%Y-%m-%d %H:%M"
D_FMT = "%Y-%m-%d"

# ---------------------------------------------------------------------------
# 상태 / 역할 상수
# ---------------------------------------------------------------------------
ST_PENDING = "승인대기"
ST_APPROVED = "승인완료"
ST_OUT = "출차"
ST_OVERDUE = "연체"
ST_DONE = "반납완료"
ST_REJECTED = "반려"
ST_CANCELED = "취소"
ST_EXPIRED = "만료"
ST_NOSHOW = "노쇼"

# 차량을 점유하는 상태 (예약 충돌 검사 대상)
OCCUPYING_STATUSES = (ST_PENDING, ST_APPROVED, ST_OUT, ST_OVERDUE)
# 사용자의 '진행 중/예정' 예약
ACTIVE_STATUSES = OCCUPYING_STATUSES
FINAL_STATUSES = (ST_DONE, ST_REJECTED, ST_CANCELED, ST_EXPIRED, ST_NOSHOW)
ALL_STATUSES = OCCUPYING_STATUSES + FINAL_STATUSES

ROLE_USER = "user"
ROLE_SITE = "site_manager"
ROLE_ADMIN = "admin"
ROLE_LABELS = {ROLE_USER: "일반 사용자", ROLE_SITE: "현장 담당자", ROLE_ADMIN: "총무 관리자"}

KIND_EXEC = "구매"   # 임원용
KIND_WORK = "렌트"   # 업무용

DEFAULT_SETTINGS = {
    "company_name": "(주)회사명",
    "business_no": "",
    "admin_name": "김재열",
    "admin_phone": "010-4714-0595",
    "sites": ["본사", "평택", "동탄", "탕정"],
    "max_days_ahead": 30,
    "max_business_days": 5,
    "time_interval_minutes": 30,
    "default_start_time": "09:00",
    "default_end_time": "18:00",
    "max_future_reservations": 3,
    "auto_approve_enabled": False,
    "auto_approve_time": "17:00",
    "noshow_grace_hours": 2,
    "remind_day_before_time": "15:00",
    "return_remind_hours": 1,
    "tax_period_start": f"{date.today().year}-01-01",
    "tax_period_end": f"{date.today().year}-12-31",
    "oil_interval_km": 10000,
    "tire_interval_km": 40000,
    "replace_years": 4,
    "maint_warn_ratio": 0.9,
    "expiry_warn_days": 30,
    "rent_expiry_warn_days": 60,
    "rent_km_warn_ratio": 0.9,
    "key_pickup_guide": "차키는 각 현장 총무 데스크(보관함)에서 사원증 확인 후 수령합니다. 반납 시 같은 보관함에 넣어 주세요.",
    "calendar_show_names_to_users": False,
    "live_refresh_seconds": 30,
    "ollama_base": "http://127.0.0.1:11434",
    "ollama_model": "qwen2.5vl:3b",
    # 알림 채널
    "notify_webhook_enabled": False,
    "notify_webhook_type": "slack",   # slack | teams | jandi
    "notify_webhook_url": "",
    "notify_email_enabled": False,
    "smtp_host": "",
    "smtp_port": 587,
    "smtp_user": "",
    "smtp_password": "",
    "smtp_from": "",
    "notify_kakao_enabled": False,
    "kakao_api_url": "",
    "kakao_api_key": "",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS employees (
    emp_no TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    dept TEXT DEFAULT '',
    site TEXT DEFAULT '',
    role TEXT NOT NULL DEFAULT 'user',
    phone TEXT DEFAULT '',
    email TEXT DEFAULT '',
    active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS vehicles (
    vehicle_no TEXT PRIMARY KEY,
    model TEXT NOT NULL,
    kind TEXT NOT NULL,              -- 구매(임원용) / 렌트(업무용)
    site TEXT NOT NULL,
    seats INTEGER DEFAULT 5,
    fuel_type TEXT DEFAULT '가솔린',
    body_type TEXT DEFAULT '승용',   -- 승용 / SUV / 승합
    assigned_user TEXT DEFAULT '',
    assigned_user_phone TEXT DEFAULT '',
    manager TEXT DEFAULT '',
    manager_phone TEXT DEFAULT '',
    odometer INTEGER DEFAULT 0,
    purchase_date TEXT,
    purchase_cost INTEGER,
    rent_company TEXT DEFAULT '',
    rent_start TEXT,
    rent_end TEXT,
    rent_monthly_fee INTEGER,
    rent_km_limit INTEGER,
    rent_start_km INTEGER,
    insurance_expiry TEXT,
    inspection_due TEXT,
    car_tax_due TEXT,
    hipass_card TEXT DEFAULT '',
    key_location TEXT DEFAULT '',
    parking_location TEXT DEFAULT '',
    fuel_level TEXT DEFAULT '',
    last_oil_km INTEGER DEFAULT 0,
    last_tire_km INTEGER DEFAULT 0,
    last_maintenance_date TEXT,
    note TEXT DEFAULT '',
    active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS reservations (
    id INTEGER PRIMARY KEY,
    emp_no TEXT NOT NULL,
    user_name TEXT NOT NULL,
    dept TEXT DEFAULT '',
    vehicle_no TEXT NOT NULL,
    start_dt TEXT NOT NULL,
    end_dt TEXT NOT NULL,
    purpose TEXT DEFAULT '',
    destination TEXT DEFAULT '',
    passengers INTEGER DEFAULT 1,
    needs TEXT DEFAULT '',
    status TEXT NOT NULL,
    requested_at TEXT NOT NULL,
    decided_by TEXT DEFAULT '',
    decided_at TEXT,
    auto_approved INTEGER DEFAULT 0,
    reject_reason TEXT DEFAULT '',
    cancel_reason TEXT DEFAULT '',
    checkout_at TEXT,
    checkout_km INTEGER,
    return_at TEXT,
    return_km INTEGER,
    distance INTEGER,
    commute_km INTEGER,      -- ⑧ 출·퇴근용 (세법상 업무용에 포함)
    personal_km INTEGER,     -- 비업무(개인 용도)
    business_km INTEGER,     -- 업무용 사용거리 = 출퇴근 + 일반업무
    use_date TEXT,
    fuel_level TEXT DEFAULT '',
    parking_location TEXT DEFAULT '',
    return_note TEXT DEFAULT '',
    was_overdue INTEGER DEFAULT 0,
    source TEXT DEFAULT 'app',
    updated_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_res_vehicle ON reservations(vehicle_no, start_dt, end_dt);
CREATE INDEX IF NOT EXISTS ix_res_emp ON reservations(emp_no);
CREATE INDEX IF NOT EXISTS ix_res_status ON reservations(status);
CREATE TABLE IF NOT EXISTS change_requests (
    id INTEGER PRIMARY KEY,
    reservation_id INTEGER NOT NULL,
    new_start_dt TEXT NOT NULL,
    new_end_dt TEXT NOT NULL,
    reason TEXT DEFAULT '',
    status TEXT NOT NULL DEFAULT '대기',   -- 대기 / 승인 / 반려 / 취소
    requested_by TEXT,
    requested_at TEXT,
    decided_by TEXT,
    decided_at TEXT,
    decision_note TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS maintenance (
    id INTEGER PRIMARY KEY,
    vehicle_no TEXT NOT NULL,
    start_date TEXT NOT NULL,
    end_date TEXT NOT NULL,
    mtype TEXT DEFAULT '정기점검',
    vendor TEXT DEFAULT '',
    description TEXT DEFAULT '',
    cost INTEGER,
    odometer INTEGER,
    status TEXT NOT NULL DEFAULT '예정',   -- 예정 / 진행 / 완료 / 취소
    created_by TEXT,
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS incidents (
    id INTEGER PRIMARY KEY,
    vehicle_no TEXT NOT NULL,
    reservation_id INTEGER,
    itype TEXT NOT NULL,          -- 손상 / 고장 / 사고 / 기타
    description TEXT DEFAULT '',
    occurred_at TEXT,
    reported_by TEXT,
    status TEXT DEFAULT '접수',    -- 접수 / 처리중 / 완료
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS expenses (
    id INTEGER PRIMARY KEY,
    reservation_id INTEGER,
    vehicle_no TEXT NOT NULL,
    emp_no TEXT,
    etype TEXT NOT NULL,          -- 유류비 / 하이패스 / 주차비 / 세차 / 기타
    amount INTEGER NOT NULL,
    spent_on TEXT,
    note TEXT DEFAULT '',
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS attachments (
    id INTEGER PRIMARY KEY,
    owner_type TEXT NOT NULL,     -- reservation / incident / expense / maintenance
    owner_id INTEGER NOT NULL,
    kind TEXT NOT NULL,           -- 계기판 / 손상 / 영수증 / 기타
    path TEXT NOT NULL,
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY,
    created_at TEXT NOT NULL,
    emp_no TEXT,                  -- 개인 수신자 (NULL이면 역할/현장 대상)
    target_role TEXT,             -- admin / site_manager
    target_site TEXT,
    ntype TEXT NOT NULL,
    title TEXT NOT NULL,
    body TEXT DEFAULT '',
    reservation_id INTEGER,
    vehicle_no TEXT,
    dedupe_key TEXT UNIQUE,
    dispatched INTEGER DEFAULT 0,
    send_log TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS notification_reads (
    notification_id INTEGER NOT NULL,
    emp_no TEXT NOT NULL,
    PRIMARY KEY (notification_id, emp_no)
);
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    entity TEXT,
    entity_id TEXT,
    detail TEXT
);
CREATE TABLE IF NOT EXISTS booking_failures (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    emp_no TEXT,
    site TEXT,
    start_dt TEXT,
    end_dt TEXT,
    passengers INTEGER,
    reason TEXT
);
"""

_init_lock = threading.Lock()
_initialized = False


# ---------------------------------------------------------------------------
# 연결
# ---------------------------------------------------------------------------
def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_FILE, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


@contextmanager
def db():
    """읽기 전용 또는 단일 문장용 연결."""
    ensure_db()
    conn = _connect()
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def tx():
    """쓰기 트랜잭션. BEGIN IMMEDIATE로 쓰기 잠금을 먼저 잡아
    '확인 후 저장' 사이에 다른 세션이 끼어드는 경쟁 상태를 막는다."""
    ensure_db()
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def query_df(sql: str, params: tuple | list = ()) -> pd.DataFrame:
    with db() as conn:
        return pd.read_sql_query(sql, conn, params=params)


def query_all(sql: str, params: tuple | list = ()) -> list[dict]:
    with db() as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def query_one(sql: str, params: tuple | list = ()) -> dict | None:
    with db() as conn:
        row = conn.execute(sql, params).fetchone()
        return dict(row) if row else None


def now() -> datetime:
    return datetime.now().replace(second=0, microsecond=0)


def now_str() -> str:
    return now().strftime(DT_FMT)


def to_dt(value) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day)
    try:
        return datetime.strptime(str(value)[:16], DT_FMT)
    except ValueError:
        try:
            return datetime.strptime(str(value)[:10], D_FMT)
        except ValueError:
            return None


def to_date(value) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return datetime.strptime(str(value)[:10], D_FMT).date()
    except ValueError:
        return None


def dt_str(value: datetime) -> str:
    return value.strftime(DT_FMT)


def d_str(value: date | None) -> str | None:
    return value.strftime(D_FMT) if value else None


# ---------------------------------------------------------------------------
# 설정
# ---------------------------------------------------------------------------
def get_settings() -> dict:
    out = dict(DEFAULT_SETTINGS)
    for row in query_all("SELECT key, value FROM settings"):
        try:
            out[row["key"]] = json.loads(row["value"])
        except json.JSONDecodeError:
            out[row["key"]] = row["value"]
    return out


def get_setting(key: str):
    return get_settings().get(key, DEFAULT_SETTINGS.get(key))


def save_settings(values: dict, conn: sqlite3.Connection | None = None) -> None:
    def _write(c):
        for key, value in values.items():
            c.execute(
                "INSERT INTO settings(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, json.dumps(value, ensure_ascii=False)),
            )
    if conn is not None:
        _write(conn)
    else:
        with tx() as c:
            _write(c)


def audit(conn: sqlite3.Connection, actor: str, action: str, entity: str = "", entity_id="", detail=None):
    conn.execute(
        "INSERT INTO audit_log(ts, actor, action, entity, entity_id, detail) VALUES(?,?,?,?,?,?)",
        (now_str(), actor, action, entity, str(entity_id),
         json.dumps(detail, ensure_ascii=False, default=str) if detail is not None else ""),
    )


# ---------------------------------------------------------------------------
# 초기화 / 엑셀 이관
# ---------------------------------------------------------------------------
def ensure_db() -> None:
    global _initialized
    if _initialized:
        return
    with _init_lock:
        if _initialized:
            return
        fresh = not DB_FILE.exists()
        conn = _connect()
        try:
            conn.executescript(SCHEMA)
            has_vehicles = conn.execute("SELECT COUNT(*) FROM vehicles").fetchone()[0]
        finally:
            conn.close()
        UPLOAD_DIR.mkdir(exist_ok=True)
        _initialized = True
        if fresh or not has_vehicles:
            import_from_excel()


def reset_database() -> dict:
    """프로토타입용: DB를 지우고 엑셀 원본에서 다시 이관한다."""
    global _initialized
    with _init_lock:
        for suffix in ("", "-wal", "-shm"):
            p = Path(str(DB_FILE) + suffix)
            if p.exists():
                p.unlink()
        _initialized = False
    ensure_db()
    return {
        "vehicles": query_one("SELECT COUNT(*) n FROM vehicles")["n"],
        "reservations": query_one("SELECT COUNT(*) n FROM reservations")["n"],
    }


def _s(value) -> str:
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except (TypeError, ValueError):
        pass
    return str(value).strip()


def _int(value, default=None):
    try:
        n = pd.to_numeric(str(value).replace(",", "").replace("원", ""), errors="coerce")
        return default if pd.isna(n) else int(n)
    except Exception:
        return default


def _date(value) -> str | None:
    if _s(value) == "":
        return None
    try:
        d = pd.to_datetime(value, errors="coerce")
        return None if pd.isna(d) else d.strftime(D_FMT)
    except Exception:
        return None


def _hhmm(value, default: str) -> str:
    text = _s(value)
    m = re.match(r"^(\d{1,2}):(\d{2})", text)
    if not m:
        return default
    return f"{int(m.group(1)):02d}:{m.group(2)}"


def infer_seats(model: str) -> int:
    m = model.replace(" ", "")
    if any(k in m for k in ("카니발", "스타리아")):
        return 9
    if "팰리세이드" in m:
        return 7
    return 5


def infer_body(model: str) -> str:
    m = model.replace(" ", "")
    if any(k in m for k in ("카니발", "스타리아")):
        return "승합"
    if any(k in m for k in ("팰리세이드", "싼타페", "쏘렌토", "투싼", "스포티지", "GV80", "GV70")):
        return "SUV"
    return "승용"


def infer_fuel(model: str) -> str:
    m = model.replace(" ", "").upper()
    if any(k in m for k in ("아이오닉", "EV", "IONIQ")):
        return "전기"
    return "가솔린"


def import_from_excel() -> None:
    """엑셀 원본 → SQLite 최초 이관. 엑셀 파일 자체는 수정하지 않는다."""
    settings = dict(DEFAULT_SETTINGS)
    ts = now_str()
    today = date.today()
    with tx() as conn:
        save_settings({k: v for k, v in settings.items()}, conn)

        # --- 차량
        cars = pd.read_excel(CAR_FILE) if CAR_FILE.exists() else pd.DataFrame()
        cars.columns = [str(c).strip() for c in cars.columns]
        maintenance_rows = []
        for _, r in cars.iterrows():
            vno = _s(r.get("차량번호"))
            if not vno:
                continue
            model = _s(r.get("차종"))
            kind = _s(r.get("구분")) or KIND_WORK
            purchase_date = _date(r.get("차량 구매일"))
            cost = _int(r.get("차량 구매 비용"))
            row = {
                "vehicle_no": vno,
                "model": model,
                "kind": kind,
                "site": _s(r.get("현장")) or "본사",
                "seats": infer_seats(model),
                "fuel_type": infer_fuel(model),
                "body_type": infer_body(model),
                "assigned_user": _s(r.get("사용자")),
                "assigned_user_phone": _s(r.get("사용자 연락처")),
                "manager": _s(r.get("관리자")) or settings["admin_name"],
                "manager_phone": _s(r.get("관리자 연락처")) or settings["admin_phone"],
                "odometer": _int(r.get("누적주행거리"), 0),
                "purchase_date": purchase_date if kind == KIND_EXEC else None,
                "purchase_cost": cost if kind == KIND_EXEC else None,
                # 렌트 차량: 기존 '구매일/구매비용'을 계약 시작일/참고값으로 옮긴다.
                "rent_start": purchase_date if kind == KIND_WORK else None,
                "rent_end": (
                    (pd.Timestamp(purchase_date) + pd.DateOffset(years=4)).strftime(D_FMT)
                    if kind == KIND_WORK and purchase_date else None
                ),
                "rent_company": "",
                "rent_monthly_fee": None,
                "rent_km_limit": None,
                "last_oil_km": _int(r.get("엔진오일, 필터 교체 km수"), 0),
                "last_tire_km": _int(r.get("타이어 교체 km수"), 0),
                "last_maintenance_date": _date(r.get("최근정비일")),
                "key_location": f"{_s(r.get('현장')) or '본사'} 총무 데스크 보관함",
                "note": (
                    "이관: 계약 시작일=기존 구매일, 만료일=시작일+4년으로 가정(실제 계약서로 수정 필요)"
                    + (f" / 기존 구매 비용 {cost:,}원" if cost else "")
                    if kind == KIND_WORK else ""
                ),
            }
            cols = ",".join(row.keys())
            conn.execute(
                f"INSERT INTO vehicles({cols}) VALUES({','.join('?' * len(row))})",
                tuple(row.values()),
            )
            if _s(r.get("상태")) == "정비중":
                start = _date(r.get("정비 시작일"))
                end = _date(r.get("정비 예정 완료일"))
                note = ""
                if not start:
                    start = today.strftime(D_FMT)
                    note = " (이관: 원본에 정비 시작일 누락 → 이관일로 설정, 확인 필요)"
                if not end or end < start:
                    end = (to_date(start) + timedelta(days=3)).strftime(D_FMT)
                    note += " (이관: 완료 예정일 누락 → 시작일+3일로 설정)"
                maintenance_rows.append((vno, start, end, "정비", "", "기존 정비중 차량" + note,
                                         "진행" if to_date(start) <= today else "예정", "SYSTEM(이관)", ts))
        conn.executemany(
            "INSERT INTO maintenance(vehicle_no, start_date, end_date, mtype, vendor, description, status, created_by, created_at)"
            " VALUES(?,?,?,?,?,?,?,?,?)",
            maintenance_rows,
        )

        # --- 직원 (프로토타입: 엑셀에 등장한 이름으로 사번 생성)
        site_of_vehicle = {r["vehicle_no"]: r["site"] for r in conn.execute("SELECT vehicle_no, site FROM vehicles")}
        res = pd.read_excel(RESERVATION_FILE) if RESERVATION_FILE.exists() else pd.DataFrame()
        res.columns = [str(c).strip() for c in res.columns]
        employees: dict[str, dict] = {}

        def add_emp(name, site, role=ROLE_USER, dept=None, phone=""):
            if not name or name in employees:
                return
            employees[name] = {
                "emp_no": f"E{1001 + len(employees):04d}",
                "name": name,
                "dept": dept or f"{site}사업팀",
                "site": site,
                "role": role,
                "phone": phone,
            }

        add_emp(settings["admin_name"], "본사", ROLE_ADMIN, "총무팀", settings["admin_phone"])
        for site in settings["sites"]:
            add_emp(f"{site}현장담당", site, ROLE_SITE, f"{site} 현장지원팀")
        for _, r in cars.iterrows():
            add_emp(_s(r.get("사용자")), _s(r.get("현장")) or "본사", ROLE_USER, "임원실", _s(r.get("사용자 연락처")))
        for _, r in res.iterrows():
            add_emp(_s(r.get("예약자")), _s(r.get("현장")) or site_of_vehicle.get(_s(r.get("차량번호")), "본사"))
        conn.executemany(
            "INSERT INTO employees(emp_no, name, dept, site, role, phone) VALUES(?,?,?,?,?,?)",
            [(e["emp_no"], e["name"], e["dept"], e["site"], e["role"], e["phone"]) for e in employees.values()],
        )

        # --- 예약
        now_dt = now()
        for _, r in res.iterrows():
            rid = _int(r.get("예약번호"))
            name = _s(r.get("예약자"))
            if not rid or not name:
                continue
            emp = employees[name]
            start_d = _date(r.get("예약일"))
            end_d = _date(r.get("종료일")) or start_d
            if not start_d:
                continue
            start_dt = f"{start_d} {_hhmm(r.get('시작시간'), settings['default_start_time'])}"
            end_dt = f"{end_d} {_hhmm(r.get('종료시간'), settings['default_end_time'])}"
            status = _s(r.get("상태")) or ST_PENDING
            checkout_at = checkout_km = None
            if status == "운행완료":
                status = ST_DONE
            # 기존에는 '출차' 단계가 없었으므로, 시작 시각이 지난 승인완료 예약은
            # 차량을 가져간 것으로 보고 '출차'로 이관한다. (이후 기간이 지났으면 자동으로 '연체')
            if status == ST_APPROVED and to_dt(start_dt) <= now_dt:
                status = ST_OUT
                checkout_at = start_dt
                checkout_km = _int(r.get("시작주행거리"))
            requested = r.get("신청일시")
            requested_at = (pd.to_datetime(requested).strftime(DT_FMT)
                            if _s(requested) else ts)
            conn.execute(
                """INSERT INTO reservations(id, emp_no, user_name, dept, vehicle_no, start_dt, end_dt, purpose,
                   destination, passengers, status, requested_at, decided_by, decided_at, checkout_at, checkout_km,
                   return_km, distance, return_at, parking_location, source, updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (rid, emp["emp_no"], name, emp["dept"], _s(r.get("차량번호")), start_dt, end_dt,
                 _s(r.get("사용목적")), _s(r.get("목적지")), _int(r.get("인원"), 1), status, requested_at,
                 "SYSTEM(이관)" if status != ST_PENDING else "", ts if status != ST_PENDING else None,
                 checkout_at, checkout_km, _int(r.get("종료주행거리")), _int(r.get("운행거리")),
                 _date(r.get("운행완료일시")), _s(r.get("반납 장소")), "excel", ts),
            )
        audit(conn, "SYSTEM", "엑셀 이관", "database", "",
              {"vehicles": len(cars), "reservations": len(res), "employees": len(employees)})
