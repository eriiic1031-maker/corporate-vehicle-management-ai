import calendar
import html
import json
import re
from datetime import date, datetime, timedelta
from pathlib import Path

import requests
import streamlit as st

try:
    from openpyxl import Workbook as XLWorkbook, load_workbook
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
except ImportError:
    XLWorkbook = None
    load_workbook = None
    Font = PatternFill = Alignment = Border = Side = None

try:
    import pandas as pd
except ImportError:
    st.error('pandas가 설치되어 있지 않습니다. 터미널에서 pip install pandas를 실행하세요.')
    st.stop()

try:
    import holidays
except ImportError:
    holidays = None


# =========================================================
# 기본 설정
# =========================================================
st.set_page_config(
    page_title="법인차량 관리 AI",
    page_icon="🚗",
    layout="wide",
)

BASE_DIR = Path(__file__).resolve().parent
CAR_FILE = BASE_DIR / "차량목록.xlsx"
RESERVATION_FILE = BASE_DIR / "예약목록.xlsx"
RUN_LOG_FILE = BASE_DIR / "운행일지.xlsx"

OLLAMA_BASE = "http://127.0.0.1:11434"
OLLAMA_MODEL = "qwen2.5vl:3b"

DEFAULT_START_TIME = "09:00"
DEFAULT_END_TIME = "18:00"
MAX_RESERVATION_DAYS = 30
MAX_BUSINESS_DAYS = 5
TIME_INTERVAL_MINUTES = 30
ACTIVE_STATUSES = ["승인대기", "승인완료"]
SITES = ["본사", "평택", "동탄", "탕정"]

# 관리자/사용자 화면 자동 동기화 주기(초). 엑셀 파일을 주기적으로 다시 읽어
# 다른 브라우저 세션에서 생긴 예약/승인/정비/차량정보 변경도 빠르게 반영한다.
LIVE_REFRESH_SECONDS = 5

def live_refresh_fragment(func):
    """지원되는 Streamlit에서는 해당 화면 영역만 주기적으로 다시 실행한다."""
    fragment = getattr(st, "fragment", None)
    if fragment is None:
        return func
    return fragment(run_every=f"{LIVE_REFRESH_SECONDS}s")(func)

RESERVATION_COLUMNS = [
    "예약번호",
    "예약자",
    "예약일",
    "종료일",
    "차량번호",
    "차종",
    "현장",
    "차량용도",
    "시작시간",
    "종료시간",
    "사용목적",
    "목적지",
    "인원",
    "상태",
    "신청일시",
    "시작주행거리",
    "종료주행거리",
    "운행거리",
    "운행완료일시",
    "사용자 부서",
    "반납 장소",
    "사용일자",
    "업무용 사용거리",
    "일반업무용 사용거리",
]

RUN_LOG_CORE_COLUMNS = [
    "운행일", "예약번호", "차량번호", "차종", "현장", "사용자",
    "목적지", "사용목적", "예약기간", "시작시간", "종료시간",
    "시작주행거리", "종료주행거리", "운행거리", "운행완료일시", "상태"
]
RUN_LOG_EXTRA_COLUMNS = ["반납 장소", "사용자 부서", "업무용 사용거리", "일반업무용 사용거리"]
RUN_LOG_COLUMNS = RUN_LOG_CORE_COLUMNS + RUN_LOG_EXTRA_COLUMNS
# Existing workbook keeps column 17 as 월; new fields are appended after it.
RUN_LOG_COLUMNS_WITH_MONTH = RUN_LOG_CORE_COLUMNS + ["월"] + RUN_LOG_EXTRA_COLUMNS


def normalize_columns(df):
    df = df.copy()
    df.columns = [str(c).strip() for c in df.columns]
    return df


def clean_str(value):
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except Exception:
        pass
    return str(value).strip()


def load_cars():
    if not CAR_FILE.exists():
        st.error(f"차량목록.xlsx 파일을 찾을 수 없습니다.\n\n{CAR_FILE}")
        st.stop()

    df = pd.read_excel(CAR_FILE)
    df = normalize_columns(df)

    required = [
        "차량번호", "차종", "구분", "현장", "사용자", "사용자 연락처",
        "관리자", "관리자 연락처", "상태", "누적주행거리", "차량 구매일",
        "최근정비일", "정비 시작일", "정비 예정 완료일", "타이어 교체 km수",
        "엔진오일, 필터 교체 km수", "타이어 교체(4만 km 마다)",
        "엔진오일, 필터 교환(1만 km 마다)", "차량 교체 4년 마다", "차량 구매 비용"
    ]
    for col in required:
        if col not in df.columns:
            df[col] = ""

    for col in ["차량 구매일", "최근정비일", "정비 시작일", "정비 예정 완료일"]:
        df[col] = pd.to_datetime(df[col], errors="coerce").dt.date

    for col in ["누적주행거리", "타이어 교체 km수", "엔진오일, 필터 교체 km수"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    df["차량용도"] = df["구분"].apply(
        lambda x: "임원용차량" if clean_str(x) == "구매" else "업무용차량" if clean_str(x) == "렌트" else ""
    )
    return df


def load_reservations():
    if not RESERVATION_FILE.exists():
        return pd.DataFrame(columns=RESERVATION_COLUMNS)

    df = pd.read_excel(RESERVATION_FILE)
    df = normalize_columns(df)

    # 기존/이전 버전 엑셀의 열 이름 차이를 자동 보정
    aliases = {
        "사용 목적": "사용목적",
        "사용목적/내용": "사용목적",
        "목적": "사용목적",
        "예약 시작일": "예약일",
        "예약 종료일": "종료일",
    }
    for old_name, new_name in aliases.items():
        if old_name in df.columns and new_name not in df.columns:
            df = df.rename(columns={old_name: new_name})

    for col in RESERVATION_COLUMNS:
        if col not in df.columns:
            df[col] = ""

    df = df[RESERVATION_COLUMNS].copy()
    df["예약일"] = pd.to_datetime(df["예약일"], errors="coerce").dt.date
    df["종료일"] = pd.to_datetime(df["종료일"], errors="coerce").dt.date
    df["사용일자"] = pd.to_datetime(df["사용일자"], errors="coerce").dt.date
    df["인원"] = pd.to_numeric(df["인원"], errors="coerce").fillna(0).astype(int)
    df["예약번호"] = pd.to_numeric(df["예약번호"], errors="coerce").fillna(0).astype(int)
    for col in ["시작주행거리", "종료주행거리", "운행거리", "업무용 사용거리", "일반업무용 사용거리"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["운행완료일시"] = pd.to_datetime(df["운행완료일시"], errors="coerce")
    return df


def save_reservations(df):
    out = df.copy()
    for col in RESERVATION_COLUMNS:
        if col not in out.columns:
            out[col] = ""
    out = out[RESERVATION_COLUMNS]
    out.to_excel(RESERVATION_FILE, index=False)


def save_run_log_entry(entry):
    """운행 완료 1건을 운행일지.xlsx의 운행일 기준 월 시트에 누적한다."""
    run_date = entry.get("운행일")
    if not isinstance(run_date, date):
        run_date = _date_value(run_date) or today_kst()

    month_sheet = run_date.strftime("%Y-%m")
    row = {col: entry.get(col, "") for col in RUN_LOG_COLUMNS}

    if RUN_LOG_FILE.exists():
        try:
            existing = pd.read_excel(RUN_LOG_FILE, sheet_name=month_sheet)
            existing = normalize_columns(existing)
        except Exception:
            existing = pd.DataFrame(columns=RUN_LOG_COLUMNS)
    else:
        existing = pd.DataFrame(columns=RUN_LOG_COLUMNS)

    for col in RUN_LOG_COLUMNS:
        if col not in existing.columns:
            existing[col] = ""
    existing = existing[RUN_LOG_COLUMNS]
    existing = pd.concat([existing, pd.DataFrame([row])], ignore_index=True)

    write_mode = "a" if RUN_LOG_FILE.exists() else "w"
    if write_mode == "a":
        with pd.ExcelWriter(RUN_LOG_FILE, engine="openpyxl", mode="a", if_sheet_exists="replace") as writer:
            existing.to_excel(writer, sheet_name=month_sheet, index=False)
    else:
        with pd.ExcelWriter(RUN_LOG_FILE, engine="openpyxl", mode="w") as writer:
            existing.to_excel(writer, sheet_name=month_sheet, index=False)


def save_cars(df):
    out = df.copy()
    # 화면 계산용 파생열은 원본 차량목록.xlsx 레이아웃에 쓰지 않는다.
    out = out.drop(columns=["차량용도"], errors="ignore")
    # 차량 원본 열 순서 유지: 엑셀에 있던 열을 우선 유지하고 새 열만 뒤에 추가
    try:
        existing = list(pd.read_excel(CAR_FILE, nrows=0).columns)
    except Exception:
        existing = []
    ordered = [c for c in existing if c in out.columns] + [c for c in out.columns if c not in existing]
    out = out[ordered]
    out.to_excel(CAR_FILE, index=False)


def today_kst():
    return date.today()


def get_korean_holidays(year):
    if holidays is None:
        return set()
    return holidays.KR(years=year)


def is_business_day(target_date):
    if target_date.weekday() >= 5:
        return False
    return target_date not in get_korean_holidays(target_date.year)


def get_business_days(start_date, end_date):
    if start_date > end_date:
        return []
    result = []
    cursor = start_date
    while cursor <= end_date:
        if is_business_day(cursor):
            result.append(cursor)
        cursor += timedelta(days=1)
    return result


def get_business_day_count(start_date, end_date):
    return len(get_business_days(start_date, end_date))


def date_ranges_overlap(start1, end1, start2, end2):
    if any(x is None for x in [start1, end1, start2, end2]):
        return False
    return start1 <= end2 and start2 <= end1


def get_next_reservation_number(reservations):
    if reservations.empty:
        return 1001
    nums = pd.to_numeric(reservations["예약번호"], errors="coerce").dropna()
    return int(nums.max()) + 1 if not nums.empty else 1001


def active_reservations(reservations):
    if reservations.empty:
        return reservations.copy()
    return reservations[reservations["상태"].isin(ACTIVE_STATUSES)].copy()


def check_vehicle_conflict(reservations, vehicle_number, start_date, end_date, exclude_reservation_number=None):
    active = active_reservations(reservations)
    if active.empty:
        return None

    for _, row in active.iterrows():
        if clean_str(row["차량번호"]) != clean_str(vehicle_number):
            continue
        if exclude_reservation_number is not None and int(row["예약번호"]) == int(exclude_reservation_number):
            continue
        if date_ranges_overlap(start_date, end_date, row["예약일"], row["종료일"]):
            return row
    return None


def check_user_reservation_block(reservations, user_name, exclude_reservation_number=None):
    name = clean_str(user_name)
    if not name:
        return None
    active = active_reservations(reservations)
    if active.empty:
        return None

    today = today_kst()
    candidates = active[active["예약자"].astype(str).str.strip() == name]
    for _, row in candidates.iterrows():
        if exclude_reservation_number is not None and int(row["예약번호"]) == int(exclude_reservation_number):
            continue
        if row["종료일"] is not None and row["종료일"] >= today:
            return row
    return None


def get_reserved_vehicle_numbers_today(reservations):
    today = today_kst()
    active = active_reservations(reservations)
    if active.empty:
        return set()
    result = set()
    for _, row in active.iterrows():
        if row["예약일"] and row["종료일"] and row["예약일"] <= today <= row["종료일"]:
            result.add(clean_str(row["차량번호"]))
    return result


def get_running_vehicle_numbers_today(reservations):
    today = today_kst()
    approved = reservations[reservations["상태"] == "승인완료"].copy() if not reservations.empty else reservations
    result = set()
    for _, row in approved.iterrows():
        if row["예약일"] and row["종료일"] and row["예약일"] <= today <= row["종료일"]:
            result.add(clean_str(row["차량번호"]))
    return result


def get_reservations_for_date(reservations, target_date):
    active = active_reservations(reservations)
    if active.empty:
        return active
    return active[
        active["예약일"].notna()
        & active["종료일"].notna()
        & (active["예약일"] <= target_date)
        & (active["종료일"] >= target_date)
    ].copy()


def infer_capacity(model):
    model = clean_str(model).replace(" ", "")
    if "카니발" in model:
        return 9
    if "스타리아" in model:
        return 9
    if "팰리세이드" in model:
        return 7
    if "싼타페" in model:
        return 5
    if "쏘렌토" in model:
        return 5
    return 5


def get_capacity(row):
    if "승차정원" in row.index:
        try:
            value = int(float(row["승차정원"]))
            if value > 0:
                return value
        except Exception:
            pass
    return infer_capacity(row.get("차종", ""))


def vehicle_display_type(row):
    return "임원용차량" if clean_str(row.get("구분")) == "구매" else "업무용차량" if clean_str(row.get("구분")) == "렌트" else "-"


def fmt_date(d):
    if not d:
        return "-"
    return d.strftime("%Y-%m-%d") if hasattr(d, "strftime") else str(d)


def fmt_number(value):
    try:
        return f"{int(float(value)):,}"
    except Exception:
        return "-"


def fmt_money(value):
    try:
        return f"{int(float(value)):,}원"
    except Exception:
        return clean_str(value) or "-"


def fmt_period(row):
    start = fmt_date(row.get("예약일"))
    end = fmt_date(row.get("종료일"))
    stime = clean_str(row.get("시작시간")) or DEFAULT_START_TIME
    etime = clean_str(row.get("종료시간")) or DEFAULT_END_TIME
    if start == end:
        return f"{start} {stime} ~ {etime}"
    return f"{start} ~ {end} / {stime} ~ {etime}"


def reservation_runtime_label(row):
    today = today_kst()
    if clean_str(row.get("상태")) == "승인완료" and row.get("예약일") and row.get("종료일"):
        if row["예약일"] <= today <= row["종료일"]:
            return "운행중"
    return clean_str(row.get("상태")) or "-"


def status_badge(status):
    status = clean_str(status)
    cls = {
        "승인완료": "approved",
        "승인대기": "pending",
        "반려": "rejected",
    }.get(status, "pending")
    return f'<span class="badge {cls}">{html.escape(status)}</span>'


def render_kpi(title, value, caption=""):
    st.markdown(
        f"""
        <div class="kpi-card">
            <div class="kpi-title">{html.escape(title)}</div>
            <div class="kpi-value">{html.escape(str(value))}</div>
            <div class="kpi-caption">{html.escape(caption)}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def inject_css():
    st.markdown(
        """
        <style>
        .block-container {padding-top: 1.2rem; padding-bottom: 2rem;}
        .kpi-card {border:1px solid #e6e8eb;border-radius:16px;padding:16px 18px;background:#fff;box-shadow:0 2px 10px rgba(0,0,0,.04);min-height:115px;}
        .kpi-title {font-size:14px;color:#64748b;font-weight:700;}
        .kpi-value {font-size:28px;font-weight:800;margin-top:6px;color:#111827;}
        .kpi-caption {font-size:12px;color:#94a3b8;margin-top:4px;}
        .badge {display:inline-block;padding:4px 8px;border-radius:999px;font-size:12px;font-weight:700;}
        .approved {background:#dcfce7;color:#166534;}
        .pending {background:#fef3c7;color:#92400e;}
        .rejected {background:#fee2e2;color:#991b1b;}
        .section-title {font-size:19px;font-weight:800;margin:12px 0 8px;}
        .small-muted {color:#64748b;font-size:12px;}
        .calendar-week {position:relative;display:grid;grid-template-columns:repeat(7,minmax(0,1fr));border:1px solid #dbe1e8;border-radius:14px;overflow:hidden;margin-bottom:12px;background:#fff;}
        .calendar-day {grid-row:1 / -1;border-right:1px solid #edf0f3;min-height:92px;padding:7px;background:#fff;}
        .calendar-day:last-child {border-right:none;}
        .day-head {font-weight:800;font-size:12px;color:#334155;}
        .day-number {font-size:15px;font-weight:800;margin-top:2px;}
        .sunday {color:#dc2626;}
        .saturday {color:#2563eb;}
        .holiday {background:#fff7f7;}
        .calendar-bar {align-self:start;margin:3px 4px 0 4px;padding:5px 7px;border-radius:8px;font-size:11px;line-height:1.25;font-weight:700;z-index:3;overflow:hidden;white-space:nowrap;text-overflow:ellipsis;min-width:0;box-shadow:0 1px 3px rgba(0,0,0,.10);}
        .bar-approved {background:#dcfce7;color:#166534;border:1px solid #86efac;}
        .bar-pending {background:#fef3c7;color:#92400e;border:1px solid #fcd34d;}
        .bar-start {border-top-left-radius:10px;border-bottom-left-radius:10px;}
        .bar-end {border-top-right-radius:10px;border-bottom-right-radius:10px;}
        .bar-mid {border-radius:3px;}
        .month-title {font-size:20px;font-weight:800;margin:4px 0 10px;}
        .car-mini {padding:12px;border:1px solid #e5e7eb;border-radius:12px;background:#fff;margin-bottom:8px;}
        .car-number {font-size:16px;font-weight:800;}
        .car-meta {font-size:12px;color:#64748b;margin-top:2px;}
        </style>
        """,
        unsafe_allow_html=True,
    )


# =========================================================
# 차량 상세 팝업 / 편집
# =========================================================
def _date_value(value):
    """Streamlit date_input에 안전하게 넣을 수 있는 datetime.date/None으로 변환한다.

    Excel에서 비어 있는 날짜는 pandas의 NaT로 들어오며, NaT는 datetime/date
    타입으로도 인식될 수 있어 기존 코드처럼 타입 검사를 먼저 하면
    Streamlit 내부에서 float/날짜 포맷 오류가 발생할 수 있다.
    따라서 결측값을 가장 먼저 처리한다.
    """
    if value is None:
        return None

    # pandas NaT / NaN / pd.NA를 가장 먼저 제거
    try:
        missing = pd.isna(value)
        if isinstance(missing, bool) and missing:
            return None
        if not isinstance(missing, (bool,)) and bool(missing):
            return None
    except Exception:
        pass

    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value

    # Excel serial number가 들어오는 경우도 방어
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            if float(value) <= 0:
                return None
            parsed = pd.Timestamp("1899-12-30") + pd.to_timedelta(float(value), unit="D")
            return parsed.date()
        except Exception:
            return None

    try:
        parsed = pd.to_datetime(value, errors="coerce")
        if pd.isna(parsed):
            return None
        return parsed.date()
    except Exception:
        return None


def safe_int(value, default=0):
    try:
        number = pd.to_numeric(value, errors="coerce")
        if pd.isna(number):
            return default
        return int(number)
    except Exception:
        return default


def _show_vehicle_edit_form(cars_df, car, title_prefix):
    vehicle_no = clean_str(car.get("차량번호"))
    st.markdown(f"### {title_prefix} {vehicle_no}")

    with st.form(f"vehicle-edit-form-{vehicle_no}"):
        c1, c2, c3, c4 = st.columns(4)
        c1.text_input("차량번호", value=vehicle_no, disabled=True)
        c2.text_input("차종", value=clean_str(car.get("차종")), key=f"edit-model-{vehicle_no}")
        c3.selectbox("구분", ["구매", "렌트"], index=0 if clean_str(car.get("구분")) == "구매" else 1,
                      key=f"edit-type-{vehicle_no}")
        c4.selectbox("상태", ["운행가능", "정비중"], index=0 if clean_str(car.get("상태")) == "운행가능" else 1,
                      key=f"edit-status-{vehicle_no}")

        c1, c2, c3 = st.columns(3)
        c1.selectbox("현장", SITES, index=SITES.index(clean_str(car.get("현장"))) if clean_str(car.get("현장")) in SITES else 0,
                      key=f"edit-site-{vehicle_no}")
        c2.text_input("사용자", value=clean_str(car.get("사용자")), key=f"edit-user-{vehicle_no}")
        c3.text_input("사용자 연락처", value=clean_str(car.get("사용자 연락처")), key=f"edit-user-phone-{vehicle_no}")

        c1, c2 = st.columns(2)
        c1.text_input("관리자", value=clean_str(car.get("관리자")), key=f"edit-manager-{vehicle_no}")
        c2.text_input("관리자 연락처", value=clean_str(car.get("관리자 연락처")), key=f"edit-manager-phone-{vehicle_no}")

        c1, c2, c3 = st.columns(3)
        current_mileage = c1.number_input("누적주행거리 (km)", min_value=0, value=safe_int(car.get("누적주행거리")), step=100,
                                          key=f"edit-mileage-{vehicle_no}")
        tire_km = c2.number_input("타이어 교체 기준 (km)", min_value=0, value=safe_int(car.get("타이어 교체 km수")), step=100,
                                   key=f"edit-tire-km-{vehicle_no}")
        oil_km = c3.number_input("엔진오일·필터 교체 기준 (km)", min_value=0, value=safe_int(car.get("엔진오일, 필터 교체 km수")), step=100,
                                 key=f"edit-oil-km-{vehicle_no}")

        st.markdown("#### 📅 정비 / 구매일")
        c1, c2, c3 = st.columns(3)
        purchase_date = c1.date_input("차량 구매일", value=_date_value(car.get("차량 구매일")), key=f"edit-purchase-date-{vehicle_no}")
        recent_date = c2.date_input("최근정비일", value=_date_value(car.get("최근정비일")), key=f"edit-recent-date-{vehicle_no}")
        repair_start = c3.date_input("정비 시작일", value=_date_value(car.get("정비 시작일")), key=f"edit-repair-start-{vehicle_no}")

        c1, c2, c3 = st.columns(3)
        repair_end = c1.date_input("정비 예정 완료일", value=_date_value(car.get("정비 예정 완료일")), key=f"edit-repair-end-{vehicle_no}")
        tire_status = c2.selectbox("타이어 교체", ["정상", "교체 대상"],
                                   index=0 if clean_str(car.get("타이어 교체(4만 km 마다)")) != "교체 대상" else 1,
                                   key=f"edit-tire-status-{vehicle_no}")
        oil_status = c3.selectbox("엔진오일·필터 교환", ["정상", "교체 대상"],
                                  index=0 if clean_str(car.get("엔진오일, 필터 교환(1만 km 마다)")) != "교체 대상" else 1,
                                  key=f"edit-oil-status-{vehicle_no}")

        c1, c2 = st.columns(2)
        replace_4y = c1.selectbox("차량 교체 4년", ["정상", "교체 대상"],
                                  index=0 if clean_str(car.get("차량 교체 4년 마다")) != "교체 대상" else 1,
                                  key=f"edit-4y-{vehicle_no}")
        purchase_cost = c2.text_input("차량 구매 비용", value=clean_str(car.get("차량 구매 비용")), key=f"edit-cost-{vehicle_no}")

        st.caption("※ 차량번호는 고유 식별값이므로 수정할 수 없습니다. 저장 시 차량목록.xlsx에 즉시 반영됩니다.")
        submitted = st.form_submit_button("💾 저장", type="primary", use_container_width=True)

    if submitted:
        idx_list = cars_df.index[cars_df["차량번호"].astype(str).str.strip() == vehicle_no].tolist()
        if not idx_list:
            st.error("차량번호를 찾을 수 없습니다.")
            return
        idx = idx_list[0]
        update_values = {
            "차종": st.session_state[f"edit-model-{vehicle_no}"],
            "구분": st.session_state[f"edit-type-{vehicle_no}"],
            "현장": st.session_state[f"edit-site-{vehicle_no}"],
            "사용자": st.session_state[f"edit-user-{vehicle_no}"],
            "사용자 연락처": st.session_state[f"edit-user-phone-{vehicle_no}"],
            "관리자": st.session_state[f"edit-manager-{vehicle_no}"],
            "관리자 연락처": st.session_state[f"edit-manager-phone-{vehicle_no}"],
            "상태": st.session_state[f"edit-status-{vehicle_no}"],
            "누적주행거리": current_mileage,
            "차량 구매일": purchase_date,
            "최근정비일": recent_date,
            "정비 시작일": repair_start,
            "정비 예정 완료일": repair_end,
            "타이어 교체 km수": tire_km,
            "엔진오일, 필터 교체 km수": oil_km,
            "타이어 교체(4만 km 마다)": tire_status,
            "엔진오일, 필터 교환(1만 km 마다)": oil_status,
            "차량 교체 4년 마다": replace_4y,
            "차량 구매 비용": purchase_cost,
        }
        for col, value in update_values.items():
            if col in cars_df.columns:
                cars_df.loc[idx, col] = value
        # 구분 변경 시 차량용도는 다시 계산
        st.success(f"{vehicle_no} 차량 정보가 저장되었습니다.")
        save_cars(cars_df)
        st.rerun()


def _render_maintenance_detail(car):
    c1, c2, c3 = st.columns(3)
    c1.metric("차종", clean_str(car.get("차종")) or "-")
    c2.metric("현장", clean_str(car.get("현장")) or "-")
    c3.metric("상태", clean_str(car.get("상태")) or "-")

    rows = [
        ["차량번호", clean_str(car.get("차량번호"))],
        ["정비 시작일", fmt_date(_date_value(car.get("정비 시작일")))],
        ["정비 예정 완료일", fmt_date(_date_value(car.get("정비 예정 완료일")))],
        ["최근 정비일", fmt_date(_date_value(car.get("최근정비일")))],
        ["누적주행거리", f"{fmt_number(car.get('누적주행거리'))} km"],
        ["타이어 교체 기준", f"{fmt_number(car.get('타이어 교체 km수'))} km"],
        ["엔진오일·필터 교체 기준", f"{fmt_number(car.get('엔진오일, 필터 교체 km수'))} km"],
        ["타이어 교체 상태", clean_str(car.get("타이어 교체(4만 km 마다)")) or "-"],
        ["엔진오일·필터 상태", clean_str(car.get("엔진오일, 필터 교환(1만 km 마다)")) or "-"],
        ["차량 4년 교체 상태", clean_str(car.get("차량 교체 4년 마다")) or "-"],
    ]
    st.dataframe(pd.DataFrame(rows, columns=["항목", "내용"]), use_container_width=True, hide_index=True)

    st.markdown("#### ✏️ 정비정보 편집")


if hasattr(st, "dialog"):
    @st.dialog("🔧 정비차량 상세 / 편집")
    def maintenance_dialog(car_dict, cars_snapshot):
        _render_maintenance_detail(pd.Series(car_dict))
        _show_vehicle_edit_form(cars_snapshot.copy(), pd.Series(car_dict), "🔧")
else:
    def maintenance_dialog(car_dict, cars_snapshot):
        with st.expander("🔧 정비차량 상세 / 편집", expanded=True):
            _render_maintenance_detail(pd.Series(car_dict))
            _show_vehicle_edit_form(cars_snapshot.copy(), pd.Series(car_dict), "🔧")


def _render_replacement_detail(car):
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("차종", clean_str(car.get("차종")) or "-")
    c2.metric("구분", clean_str(car.get("구분")) or "-")
    c3.metric("현장", clean_str(car.get("현장")) or "-")
    c4.metric("사용자", clean_str(car.get("사용자")) or "-")

    rows = [
        ["차량번호", clean_str(car.get("차량번호"))],
        ["차종", clean_str(car.get("차종"))],
        ["차량용도", vehicle_display_type(car)],
        ["구분", clean_str(car.get("구분"))],
        ["현장", clean_str(car.get("현장"))],
        ["사용자", clean_str(car.get("사용자")) or "-"],
        ["사용자 연락처", clean_str(car.get("사용자 연락처")) or "-"],
        ["관리자", clean_str(car.get("관리자")) or "-"],
        ["차량 상태", clean_str(car.get("상태")) or "-"],
        ["누적주행거리", f"{fmt_number(car.get('누적주행거리'))} km"],
        ["차량 구매일", fmt_date(_date_value(car.get("차량 구매일")))],
        ["차량 구매 비용", fmt_money(car.get("차량 구매 비용"))],
        ["4년 교체 상태", clean_str(car.get("차량 교체 4년 마다"))],
    ]
    st.dataframe(pd.DataFrame(rows, columns=["항목", "내용"]), use_container_width=True, hide_index=True)

    st.markdown("#### ✏️ 교체대상 차량 정보 편집")


if hasattr(st, "dialog"):
    @st.dialog("♻️ 교체대상 차량 상세 / 편집")
    def replacement_dialog(car_dict, cars_snapshot):
        _render_replacement_detail(pd.Series(car_dict))
        _show_vehicle_edit_form(cars_snapshot.copy(), pd.Series(car_dict), "♻️")
else:
    def replacement_dialog(car_dict, cars_snapshot):
        with st.expander("♻️ 교체대상 차량 상세 / 편집", expanded=True):
            _render_replacement_detail(pd.Series(car_dict))
            _show_vehicle_edit_form(cars_snapshot.copy(), pd.Series(car_dict), "♻️")


# =========================================================
# 캘린더
# =========================================================
WEEKDAYS = ["월", "화", "수", "목", "금", "토", "일"]


def calendar_weeks(year, month):
    cal = calendar.Calendar(firstweekday=0)
    return cal.monthdatescalendar(year, month)


def lane_assignment(rows, week_start, week_end):
    visible = []
    for idx, row in rows.iterrows():
        rs = row["예약일"]
        re_ = row["종료일"]
        if not rs or not re_:
            continue
        if rs <= week_end and re_ >= week_start:
            visible.append((int(row["예약번호"]), row))

    visible.sort(key=lambda x: (x[1]["예약일"], x[1]["종료일"], x[0]))
    lanes = []
    result = []
    for rid, row in visible:
        start = max(row["예약일"], week_start)
        end = min(row["종료일"], week_end)
        lane = None
        for i, last_end in enumerate(lanes):
            if start > last_end:
                lane = i
                lanes[i] = end
                break
        if lane is None:
            lane = len(lanes)
            lanes.append(end)
        result.append((lane, row, start, end))
    return result, max(1, len(lanes))


def render_calendar(reservations, year, month):
    st.markdown(f'<div class="month-title">{year}년 {month}월 예약 캘린더</div>', unsafe_allow_html=True)

    weeks = calendar_weeks(year, month)
    for week in weeks:
        week_start = week[0]
        week_end = week[-1]
        lanes, lane_count = lane_assignment(active_reservations(reservations), week_start, week_end)
        rows = 1 + lane_count
        height_hint = 46 + lane_count * 31

        # CSS Grid: 각 날짜 셀은 전체 영역에 깔고, 예약 막대는 2번째 행부터 가로로 이어 붙인다.
        parts = [f'<div class="calendar-week" style="grid-template-rows:34px repeat({lane_count},31px);min-height:{height_hint}px;">']
        for col, d in enumerate(week, start=1):
            classes = ["calendar-day"]
            if d.month != month:
                classes.append("holiday")
            if d.weekday() == 5:
                classes.append("saturday")
            if d.weekday() == 6:
                classes.append("sunday")
            if d in get_korean_holidays(d.year):
                classes.append("holiday")
            label = WEEKDAYS[d.weekday()]
            number = d.day
            parts.append(
                f'<div class="{" ".join(classes)}" style="grid-column:{col};">'
                f'<div class="day-head">{label}</div><div class="day-number">{number}</div></div>'
            )

        for lane, row, start, end in lanes:
            start_col = (start - week_start).days + 1
            end_col = (end - week_start).days + 2
            status = clean_str(row["상태"])
            bar_cls = "bar-approved" if status == "승인완료" else "bar-pending"
            corner = []
            if row["예약일"] == start:
                corner.append("bar-start")
            if row["종료일"] == end:
                corner.append("bar-end")
            if not corner:
                corner = ["bar-mid"]
            title = f"{clean_str(row['차량번호'])} · {clean_str(row['예약자'])} · {clean_str(row['현장'])}"
            parts.append(
                f'<div class="calendar-bar {bar_cls} {" ".join(corner)}" '
                f'style="grid-column:{start_col}/{end_col};grid-row:{lane + 2};">'
                f'{html.escape(title)}</div>'
            )
        parts.append("</div>")
        st.markdown("".join(parts), unsafe_allow_html=True)

    st.caption("🟢 승인완료 · 🟡 승인대기 · 여러 날짜 예약은 같은 주 안에서 하나의 연속 막대로 표시됩니다.")


# =========================================================
# 예약 가능 차량 / 예약 생성
# =========================================================
def get_rent_cars(cars):
    return cars[cars["구분"].astype(str).str.strip() == "렌트"].copy()


def get_executive_cars(cars):
    return cars[cars["구분"].astype(str).str.strip() == "구매"].copy()


def get_maintenance_cars(cars):
    return cars[cars["상태"].astype(str).str.strip() == "정비중"].copy()


def get_replacement_cars(cars):
    return cars[cars["차량 교체 4년 마다"].astype(str).str.strip() == "교체 대상"].copy()


def is_vehicle_available_for_period(cars, reservations, vehicle_number, start_date, end_date):
    matches = cars[cars["차량번호"].astype(str).str.strip() == clean_str(vehicle_number)]
    if matches.empty:
        return False, "차량 정보를 찾을 수 없습니다."
    car = matches.iloc[0]
    if clean_str(car["구분"]) != "렌트":
        return False, "구매 차량은 임원용차량으로 예약할 수 없습니다."
    if clean_str(car["상태"]) != "운행가능":
        return False, "정비 중인 차량은 예약할 수 없습니다."
    conflict = check_vehicle_conflict(reservations, vehicle_number, start_date, end_date)
    if conflict is not None:
        return False, f"예약 충돌: {fmt_period(conflict)}"
    return True, "예약 가능"


def make_reservation(cars, reservations, user_name, vehicle_number, start_date, end_date,
                     start_time, end_time, purpose, destination, passengers, status="승인대기"):
    user_block = check_user_reservation_block(reservations, user_name)
    if user_block is not None:
        return False, f"현재 사용자는 이미 {clean_str(user_block['상태'])} 예약이 있습니다. ({fmt_period(user_block)})", reservations

    today = today_kst()
    max_date = today + timedelta(days=MAX_RESERVATION_DAYS)
    if start_date < today:
        return False, "과거 날짜는 예약할 수 없습니다.", reservations
    if end_date < start_date:
        return False, "종료일은 시작일보다 빠를 수 없습니다.", reservations
    if end_date > max_date:
        return False, f"예약은 {fmt_date(max_date)}까지 가능합니다.", reservations

    business_days = get_business_day_count(start_date, end_date)
    if business_days > MAX_BUSINESS_DAYS:
        return False, f"1회 예약은 최대 {MAX_BUSINESS_DAYS}영업일까지 가능합니다. 현재 {business_days}영업일입니다.", reservations
    if business_days == 0:
        return False, "선택한 기간에 영업일이 없습니다.", reservations

    try:
        sh = int(start_time.split(":")[0]) * 60 + int(start_time.split(":")[1])
        eh = int(end_time.split(":")[0]) * 60 + int(end_time.split(":")[1])
        if sh % TIME_INTERVAL_MINUTES != 0 or eh % TIME_INTERVAL_MINUTES != 0:
            return False, "시간은 30분 단위로 입력해야 합니다.", reservations
        if eh <= sh:
            return False, "종료시간은 시작시간보다 늦어야 합니다.", reservations
    except Exception:
        return False, "시간 형식이 올바르지 않습니다.", reservations

    match = cars[cars["차량번호"].astype(str).str.strip() == clean_str(vehicle_number)]
    if match.empty:
        return False, "차량을 찾을 수 없습니다.", reservations
    car = match.iloc[0]

    if clean_str(car["구분"]) != "렌트":
        return False, "구매 차량은 예약할 수 없습니다. 구매 차량은 임원용차량입니다.", reservations
    if clean_str(car["상태"]) != "운행가능":
        return False, "현재 정비 중인 차량은 예약할 수 없습니다.", reservations

    conflict = check_vehicle_conflict(reservations, vehicle_number, start_date, end_date)
    if conflict is not None:
        return False, f"차량 예약이 이미 존재합니다. {fmt_period(conflict)}", reservations

    next_no = get_next_reservation_number(reservations)
    new_row = {
        "예약번호": next_no,
        "예약자": clean_str(user_name),
        "예약일": start_date,
        "종료일": end_date,
        "차량번호": clean_str(car["차량번호"]),
        "차종": clean_str(car["차종"]),
        "현장": clean_str(car["현장"]),
        "차량용도": "업무용차량",
        "시작시간": start_time,
        "종료시간": end_time,
        "사용목적": clean_str(purpose),
        "목적지": clean_str(destination),
        "인원": int(passengers or 1),
        "상태": status,
        "신청일시": datetime.now().replace(microsecond=0),
        "시작주행거리": safe_int(car.get("누적주행거리")),
        "종료주행거리": None,
        "운행거리": None,
        "운행완료일시": None,
        "사용자 부서": clean_str(car.get("사용자 부서")) or clean_str(car.get("부서")) or clean_str(car.get("현장")),
        "반납 장소": "",
        "사용일자": None,
        "업무용 사용거리": None,
        "일반업무용 사용거리": None,
    }

    out = pd.concat([reservations, pd.DataFrame([new_row])], ignore_index=True)
    save_reservations(out)
    return True, f"예약번호 {next_no}로 예약 신청했습니다.", out


def time_options():
    values = []
    for hour in range(0, 24):
        for minute in [0, 30]:
            values.append(f"{hour:02d}:{minute:02d}")
    return values


# =========================================================
# Ollama AI
# =========================================================
def extract_json(text):
    text = clean_str(text)
    if not text:
        return None
    try:
        return json.loads(text)
    except Exception:
        pass

    match = re.search(r"\{.*\}", text, flags=re.S)
    if match:
        try:
            return json.loads(match.group(0))
        except Exception:
            return None
    return None


def call_ollama_json(prompt):
    url = f"{OLLAMA_BASE}/api/generate"
    payload = {
        "model": OLLAMA_MODEL,
        "prompt": prompt,
        "stream": False,
        "format": "json",
        "options": {"temperature": 0.1},
    }
    try:
        response = requests.post(url, json=payload, timeout=120)
        response.raise_for_status()
        data = response.json()
        result = extract_json(data.get("response", ""))
        if result is not None:
            return result, None
        return None, "Ollama가 JSON을 반환하지 않았습니다."
    except requests.exceptions.ConnectionError:
        return None, "Ollama에 연결할 수 없습니다. Ollama가 실행 중인지 확인하세요."
    except requests.exceptions.Timeout:
        return None, "Ollama 응답 시간이 초과되었습니다."
    except requests.exceptions.HTTPError as exc:
        return None, f"Ollama HTTP 오류: {exc}"
    except Exception as exc:
        return None, f"Ollama 오류: {exc}"


def weekday_number(name):
    mapping = {"월요일": 0, "화요일": 1, "수요일": 2, "목요일": 3, "금요일": 4, "토요일": 5, "일요일": 6,
               "월": 0, "화": 1, "수": 2, "목": 3, "금": 4, "토": 5, "일": 6}
    return mapping.get(clean_str(name))


def resolve_relative_dates_from_text(user_text, today):
    text = clean_str(user_text)
    found = re.findall(r"(월요일|화요일|수요일|목요일|금요일|토요일|일요일)", text)
    if not found:
        found_short = re.findall(r"([월화수목금토일])요일", text)
        found = [f + "요일" for f in found_short]

    if "다음주" in text or "다음 주" in text:
        monday = today - timedelta(days=today.weekday()) + timedelta(days=7)
        if found:
            start_wd = weekday_number(found[0])
            end_wd = weekday_number(found[1]) if len(found) >= 2 else start_wd
            if start_wd is not None and end_wd is not None:
                start = monday + timedelta(days=start_wd)
                end = monday + timedelta(days=end_wd)
                if end < start:
                    end += timedelta(days=7)
                return start, end

    if "내일" in text:
        start = today + timedelta(days=1)
        return start, start
    if "모레" in text:
        start = today + timedelta(days=2)
        return start, start

    date_matches = re.findall(r"(\d{1,2})월\s*(\d{1,2})일", text)
    if len(date_matches) >= 2:
        y = today.year
        start_m, start_d = map(int, date_matches[0])
        end_m, end_d = map(int, date_matches[1])
        return date(y, start_m, start_d), date(y, end_m, end_d)
    if len(date_matches) == 1:
        y = today.year
        m, d = map(int, date_matches[0])
        one = date(y, m, d)
        return one, one

    return None, None


def parse_ai_request(user_text):
    today = today_kst()
    prompt = f"""
너는 기업 총무팀의 법인차량 예약 요청을 구조화하는 AI다.
오늘 날짜는 {today.isoformat()} ({['월','화','수','목','금','토','일'][today.weekday()]}요일)이다.
예약 가능한 현장은 본사, 평택, 동탄, 탕정이다.
구매 차량은 임원용차량이며 사용자가 예약할 수 없다. 렌트 차량만 업무용차량 예약 대상이다.

사용자 요청:
{user_text}

반드시 JSON 하나만 반환해라. 설명이나 마크다운은 쓰지 마라.
스키마:
{{
  "intent": "reserve_vehicle",
  "start_date": "YYYY-MM-DD 또는 null",
  "end_date": "YYYY-MM-DD 또는 null",
  "start_time": "HH:MM 또는 null",
  "end_time": "HH:MM 또는 null",
  "passengers": 숫자 또는 null,
  "site": "본사/평택/동탄/탕정 또는 null",
  "destination": "문자열",
  "purpose": "문자열",
  "needs_recommendation": true
}}

규칙:
- '퇴근할 때까지'는 종료시간 18:00으로 해석한다.
- 시작시간이 없으면 09:00을 제안한다.
- 인원이 없으면 null로 둔다.
- 목적지에 평택/동탄/탕정/본사가 명확하면 site를 그 값으로 둔다.
- 날짜가 상대 표현이면 오늘 날짜를 기준으로 계산한 ISO 날짜를 반환한다.
"""

    result, error = call_ollama_json(prompt)
    if error:
        return None, error

    # AI가 실수한 상대날짜는 Python이 보정
    start_rel, end_rel = resolve_relative_dates_from_text(user_text, today)
    if start_rel and end_rel:
        result["start_date"] = start_rel.isoformat()
        result["end_date"] = end_rel.isoformat()

    # 자연어 보정
    if result.get("passengers") in [None, "", 0]:
        m = re.search(r"(\d+)\s*명", user_text)
        if m:
            result["passengers"] = int(m.group(1))

    if "퇴근" in user_text:
        result["end_time"] = DEFAULT_END_TIME
    if not result.get("start_time"):
        result["start_time"] = DEFAULT_START_TIME
    if not result.get("end_time"):
        result["end_time"] = DEFAULT_END_TIME

    if not result.get("site"):
        for site in SITES:
            if site in user_text:
                result["site"] = site
                break

    if not result.get("destination"):
        result["destination"] = result.get("site") or ""

    if not result.get("purpose"):
        result["purpose"] = "업무용 차량 예약"

    return result, None


def get_ai_recommendations(cars, reservations, ai_result):
    site = clean_str(ai_result.get("site"))
    passengers = int(ai_result.get("passengers") or 1)
    start_date = datetime.strptime(ai_result["start_date"], "%Y-%m-%d").date()
    end_date = datetime.strptime(ai_result["end_date"], "%Y-%m-%d").date()

    work_cars = get_rent_cars(cars)
    if site in SITES:
        work_cars = work_cars[work_cars["현장"].astype(str).str.strip() == site]

    candidates = []
    for _, car in work_cars.iterrows():
        if clean_str(car["상태"]) != "운행가능":
            continue
        capacity = get_capacity(car)
        if capacity < passengers:
            continue
        conflict = check_vehicle_conflict(reservations, car["차량번호"], start_date, end_date)
        if conflict is not None:
            continue
        candidates.append((capacity, float(car["누적주행거리"] or 0), car))

    # 인원에 딱 맞는 차량을 우선하고, 같은 정원에서는 주행거리가 낮은 차량 우선
    candidates.sort(key=lambda item: (item[0], item[1]))
    return [item[2] for item in candidates[:5]]




# =========================================================
# 운행일지 v2: 차량별 시트 + 월별 KM
# =========================================================
RUNLOG_GUIDE_SHEET_V2 = "사용안내"
RUNLOG_DATA_SHEET_V2 = "운행데이터"
RUNLOG_SUMMARY_SHEET_V2 = "월별요약"
RUNLOG_HEADER_ROW_V2 = 8
RUNLOG_DATA_START_ROW_V2 = 9
RUNLOG_DATA_END_ROW_V2 = 48
RUNLOG_TOTAL_ROW_V2 = 49


def _runlog_sheet_name_v2(vehicle_number):
    name = re.sub(r'[:\\/?*\[\]]', '_', clean_str(vehicle_number))
    return name[:31] or "차량"


def _style_runlog_header_v2(ws, row, end_col):
    if Font is None:
        return
    fill = PatternFill("solid", fgColor="5B9BD5")
    font = Font(color="FFFFFF", bold=True, size=9)
    align = Alignment(horizontal="center", vertical="center", wrap_text=True)
    side = Side(style="thin", color="B7C9D6")
    border = Border(left=side, right=side, top=side, bottom=side)
    for col in range(1, end_col + 1):
        cell = ws.cell(row=row, column=col)
        cell.fill = fill
        cell.font = font
        cell.alignment = align
        cell.border = border


def _style_runlog_label_v2(cell):
    if Font is None:
        return
    cell.fill = PatternFill("solid", fgColor="D9EAF7")
    cell.font = Font(color="17365D", bold=True, size=9)
    cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)


def _build_vehicle_runlog_sheet_v2(ws, car):
    vno = clean_str(car.get("차량번호"))
    model = clean_str(car.get("차종"))
    use_type = "임원용차량" if clean_str(car.get("구분")) == "구매" else "업무용차량"
    site = clean_str(car.get("현장"))
    current_km = safe_int(car.get("누적주행거리"))
    state = clean_str(car.get("상태"))
    user = clean_str(car.get("사용자"))
    admin = clean_str(car.get("관리자")) or "김재열"

    # Rebuild only when a brand-new sheet is created.
    ws["A1"] = "【업무용승용차 운행기록부】<2016.4.1. 제정>"
    if not any(str(r) == "A1:J1" for r in ws.merged_cells.ranges):
        ws.merge_cells("A1:J1")
    ws["A2"] = "과 세 기 간"
    ws["B2"] = "2026.01.01 ～ 2026.12.31"
    ws["C2"] = "업무용승용차 운행기록부"
    ws["D2"] = "상 호 명"
    ws["A3"] = "사업자등록번호"
    ws["A5"] = "1. 기본정보"
    if not any(str(r) == "A5:J5" for r in ws.merged_cells.ranges):
        ws.merge_cells("A5:J5")
    ws["A6"] = "①차 종"
    ws["B6"] = model
    ws["D6"] = "②자동차등록번호"
    ws["E6"] = vno
    ws["G6"] = "구분"
    ws["H6"] = use_type
    ws["I6"] = "현장"
    ws["J6"] = site
    ws["A7"] = "현재 누적주행거리"
    ws["B7"] = current_km
    ws["C7"] = "상태"
    ws["D7"] = state
    ws["E7"] = "현재 사용자"
    ws["F7"] = user
    ws["G7"] = "관리자"
    ws["H7"] = admin
    ws["A7"] = "2. 업무용 사용비율 계산"
    if not any(str(r) == "A7:J7" for r in ws.merged_cells.ranges):
        ws.merge_cells("A7:J7")

    # Header row 9 for the detailed log in v2. Existing template has header at 8, so move section down safely.
    header_row = 8
    data_start = 9
    data_end = 48
    total_row = 49
    if clean_str(ws["A9"].value) != "③사용일자(요일)":
        ws.cell(header_row,1,"③사용일자(요일)")
        ws.cell(header_row,2,"④사용자-부서")
        ws.cell(header_row,3,"④사용자-성명")
        ws.cell(header_row,4,"⑤주행 전 계기판의 거리(㎞)")
        ws.cell(header_row,5,"⑥주행 후 계기판의 거리(㎞)")
        ws.cell(header_row,6,"⑦주행거리(㎞)")
        ws.cell(header_row,7,"⑧출·퇴근용(㎞)")
        ws.cell(header_row,8,"⑨일반 업무용(㎞)")
        ws.cell(header_row,9,"업무용 사용거리(㎞)")
        ws.cell(header_row,10,"⑩비고")
        _style_runlog_header_v2(ws, header_row, 10)
        for r in range(data_start, data_end+1):
            for c in range(1,11):
                ws.cell(r,c,None)
        ws["A49"] = "⑪과세기간 총주행 거리(㎞)"
        ws["F49"] = 0
        ws["G49"] = "⑫과세기간 업무용 사용거리(㎞)"
        ws["I49"] = 0
        ws["J49"] = "⑬업무사용비율(⑫/⑪)"
        for c in ["A49","G49","J49"]:
            _style_runlog_label_v2(ws[c])

    # Monthly summary block.
    ws["L5"] = "2026년 월별 KM 요약"
    if not any(str(r) == "L5:O5" for r in ws.merged_cells.ranges):
        ws.merge_cells("L5:O5")
    _style_runlog_label_v2(ws["L5"])
    headers = ["월","총주행거리(km)","업무용 사용거리(km)","업무사용비율"]
    for c,v in enumerate(headers,12):
        ws.cell(6,c,v)
    _style_runlog_header_v2(ws,6,15)
    for m in range(1,13):
        rr=6+m
        ws.cell(rr,12,f"{m:02d}월")
        ws.cell(rr,13,0)
        ws.cell(rr,14,0)
        ws.cell(rr,15,0)
        ws.cell(rr,15).number_format="0.0%"
    ws["L19"]="합계"; ws["M19"]=0; ws["N19"]=0; ws["O19"]=0; ws["O19"].number_format="0.0%"
    _style_runlog_label_v2(ws["L19"])
    widths={"A":18,"B":14,"C":14,"D":17,"E":17,"F":14,"G":14,"H":14,"I":16,"J":30,"K":3,"L":10,"M":17,"N":19,"O":15}
    for col,w in widths.items(): ws.column_dimensions[col].width=w
    ws.freeze_panes="A10"


def _ensure_runlog_workbook_v2():
    if load_workbook is None:
        raise RuntimeError("openpyxl이 설치되어 있지 않습니다. pip install openpyxl")
    if RUN_LOG_FILE.exists():
        wb = load_workbook(RUN_LOG_FILE)
    else:
        wb = XLWorkbook()
        if wb.sheetnames:
            wb.remove(wb[wb.sheetnames[0]])

    if RUNLOG_GUIDE_SHEET_V2 not in wb.sheetnames:
        g=wb.create_sheet(RUNLOG_GUIDE_SHEET_V2,0)
        g["A1"]="【업무용승용차 운행기록부】 · 법인차량 관리 AI"
        g["A2"]="차량별 관리"; g["B2"]="차량등록번호별 개별 시트"
        g["A3"]="월별 KM"; g["B3"]="각 차량 시트 오른쪽 1~12월 KM 요약"
        g["A4"]="운행일"; g["B4"]="예약 시작일 기준. 10/31~11/1은 10/31자로 입력"
        g["A5"]="운행거리"; g["B5"]="주행 후 계기판 - 주행 전 계기판"
        g["A6"]="지원 시트"; g["B6"]="운행데이터 / 월별요약"
        _style_runlog_label_v2(g["A2"]); _style_runlog_label_v2(g["A3"]); _style_runlog_label_v2(g["A4"]); _style_runlog_label_v2(g["A5"]); _style_runlog_label_v2(g["A6"])
        g.column_dimensions["A"].width=18; g.column_dimensions["B"].width=65
        g["A1"].fill=PatternFill("solid",fgColor="1F4E78") if Font else g["A1"].fill
        if Font:
            g["A1"].font=Font(color="FFFFFF",bold=True,size=14); g["A1"].alignment=Alignment(horizontal="center")
    if RUNLOG_DATA_SHEET_V2 not in wb.sheetnames:
        raw=wb.create_sheet(RUNLOG_DATA_SHEET_V2)
        for c,h in enumerate(RUN_LOG_COLUMNS_WITH_MONTH,1): raw.cell(1,c,h)
        _style_runlog_header_v2(raw,1,len(RUN_LOG_COLUMNS_WITH_MONTH))
    else:
        raw=wb[RUNLOG_DATA_SHEET_V2]
        existing_headers={clean_str(raw.cell(1,c).value) for c in range(1,raw.max_column+1)}
        for header in RUN_LOG_COLUMNS_WITH_MONTH:
            if header not in existing_headers:
                raw.cell(1,raw.max_column+1,header)
                existing_headers.add(header)
        _style_runlog_header_v2(raw,1,raw.max_column)
    if RUNLOG_SUMMARY_SHEET_V2 not in wb.sheetnames:
        sm=wb.create_sheet(RUNLOG_SUMMARY_SHEET_V2)
        headers=["차량번호","차종","차량용도","현장"]+[f"{m:02d}월 KM" for m in range(1,13)]+["연간 KM"]
        for c,h in enumerate(headers,1): sm.cell(1,c,h)
        _style_runlog_header_v2(sm,1,len(headers))
    return wb


def _ensure_vehicle_runlog_sheet_v2(wb, car):
    vno=clean_str(car.get("차량번호"))
    sn=_runlog_sheet_name_v2(vno)
    if sn in wb.sheetnames:
        ws=wb[sn]
        # Only synchronize the top vehicle info; keep existing log rows.
        ws["B6"]=clean_str(car.get("차종")); ws["E6"]=vno
        ws["H6"]="임원용차량" if clean_str(car.get("구분"))=="구매" else "업무용차량"
        ws["J6"]=clean_str(car.get("현장")); ws["B7"]=safe_int(car.get("누적주행거리"))
        ws["D7"]=clean_str(car.get("상태")); ws["F7"]=clean_str(car.get("사용자")); ws["H7"]=clean_str(car.get("관리자")) or "김재열"
        return ws
    ws=wb.create_sheet(sn)
    _build_vehicle_runlog_sheet_v2(ws, car)
    return ws


def _find_runlog_row_v2(ws, reservation_no, total_row=49):
    needle=f"예약번호:{int(reservation_no)}"
    for r in range(RUNLOG_DATA_START_ROW_V2, max(RUNLOG_DATA_START_ROW_V2,total_row)):
        if needle in clean_str(ws.cell(r,10).value) or clean_str(ws.cell(r,2).value)==str(reservation_no):
            return r
    return None


def _get_log_rows_v2(ws):
    out=[]
    for r in range(RUNLOG_DATA_START_ROW_V2, ws.max_row+1):
        val=ws.cell(r,1).value
        if isinstance(val, datetime): val=val.date()
        if isinstance(val, date):
            out.append(r)
    return out


def _update_vehicle_log_sheet_v2(ws, entry):
    run_date=_date_value(entry.get("운행일")) or today_kst()
    reservation_no=int(entry.get("예약번호"))
    total_row=49
    for r in range(RUNLOG_DATA_START_ROW_V2, ws.max_row+2):
        if clean_str(ws.cell(r,1).value).startswith("⑪"):
            total_row=r; break
    row=_find_runlog_row_v2(ws,reservation_no,total_row)
    if row is None:
        row=None
        for r in range(RUNLOG_DATA_START_ROW_V2,total_row):
            if not any(ws.cell(r,c).value not in (None,"") for c in range(1,11)):
                row=r; break
        if row is None:
            ws.insert_rows(total_row,1)
            row=total_row; total_row+=1

    km_start=safe_int(entry.get("시작주행거리")); km_end=safe_int(entry.get("종료주행거리")); km_dist=safe_int(entry.get("운행거리"))
    department=clean_str(entry.get("사용자 부서")) or clean_str(entry.get("현장"))
    remark=(f"예약번호:{reservation_no} / 목적지:{clean_str(entry.get('목적지')) or '-'} / "
            f"{clean_str(entry.get('사용목적')) or '-'} / 반납장소:{clean_str(entry.get('반납 장소')) or '-'}")
    # ⑧ 출퇴근용은 0, ⑨ 일반 업무용과 업무용 사용거리는 계산된 주행거리와 동일하게 기록한다.
    values=[run_date,department,clean_str(entry.get("사용자")),km_start,km_end,km_dist,0,km_dist,km_dist,remark]
    for c,v in enumerate(values,1): ws.cell(row,c,v)
    ws.cell(row,1).number_format="yyyy-mm-dd"
    for c in range(4,10): ws.cell(row,c).number_format='#,##0'

    # Calculate total and monthly figures from all saved records.
    records=_get_log_rows_v2(ws)
    total_distance=sum(safe_int(ws.cell(r,6).value) for r in records)
    total_business=sum(safe_int(ws.cell(r,9).value) for r in records)
    ws.cell(total_row,6,total_distance); ws.cell(total_row,9,total_business); ws.cell(total_row,10,(total_business/total_distance) if total_distance else 0); ws.cell(total_row,10).number_format="0.0%"
    monthly={m:[0,0] for m in range(1,13)}
    for r in records:
        d=ws.cell(r,1).value
        if isinstance(d,datetime): d=d.date()
        if isinstance(d,date):
            monthly[d.month][0]+=safe_int(ws.cell(r,6).value); monthly[d.month][1]+=safe_int(ws.cell(r,9).value)
    for m in range(1,13):
        rr=6+m; ws.cell(rr,13,monthly[m][0]); ws.cell(rr,14,monthly[m][1]); ws.cell(rr,15,(monthly[m][1]/monthly[m][0]) if monthly[m][0] else 0); ws.cell(rr,15).number_format="0.0%"
    annual_distance=sum(v[0] for v in monthly.values()); annual_business=sum(v[1] for v in monthly.values())
    ws["M19"]=annual_distance; ws["N19"]=annual_business; ws["O19"]=(annual_business/annual_distance) if annual_distance else 0; ws["O19"].number_format="0.0%"
    return monthly


def _update_runlog_summary_v2(wb, car, monthly):
    ws=wb[RUNLOG_SUMMARY_SHEET_V2]
    vno=clean_str(car.get("차량번호")); target=None
    for r in range(2,ws.max_row+1):
        if clean_str(ws.cell(r,1).value)==vno: target=r; break
    if target is None: target=ws.max_row+1
    ws.cell(target,1,vno); ws.cell(target,2,clean_str(car.get("차종"))); ws.cell(target,3,"임원용차량" if clean_str(car.get("구분"))=="구매" else "업무용차량"); ws.cell(target,4,clean_str(car.get("현장")))
    for m in range(1,13): ws.cell(target,4+m,monthly[m][0])
    ws.cell(target,17,sum(monthly[m][0] for m in range(1,13)))


def save_run_log_entry_v2(entry):
    """차량별 공식 서식과 원본 운행데이터 시트를 함께 업데이트한다."""
    run_date=_date_value(entry.get("운행일")) or today_kst()
    wb=_ensure_runlog_workbook_v2()
    vehicle_no=clean_str(entry.get("차량번호"))
    cars_now=load_cars()
    match=cars_now[cars_now["차량번호"].astype(str).str.strip()==vehicle_no]
    car=match.iloc[0].to_dict() if not match.empty else {"차량번호":vehicle_no,"차종":clean_str(entry.get("차종")),"구분":"렌트","현장":clean_str(entry.get("현장")),"누적주행거리":safe_int(entry.get("종료주행거리")),"상태":"운행가능","사용자":"","관리자":"김재열"}
    vws=_ensure_vehicle_runlog_sheet_v2(wb,car)
    monthly=_update_vehicle_log_sheet_v2(vws,entry)
    _update_runlog_summary_v2(wb,car,monthly)

    raw=wb[RUNLOG_DATA_SHEET_V2]
    header_map={clean_str(raw.cell(1,c).value):c for c in range(1,raw.max_column+1) if clean_str(raw.cell(1,c).value)}
    reservation_no=int(entry.get("예약번호"))
    reservation_col=header_map.get("예약번호",2)
    target=None
    for r in range(2,raw.max_row+1):
        if safe_int(raw.cell(r,reservation_col).value,default=-1)==reservation_no:
            target=r
            break
    if target is None:
        target=raw.max_row+1

    distance=safe_int(entry.get("운행거리"))
    values={
        "운행일":run_date,
        "예약번호":reservation_no,
        "차량번호":vehicle_no,
        "차종":clean_str(entry.get("차종")),
        "현장":clean_str(entry.get("현장")),
        "사용자":clean_str(entry.get("사용자")),
        "목적지":clean_str(entry.get("목적지")),
        "사용목적":clean_str(entry.get("사용목적")),
        "예약기간":clean_str(entry.get("예약기간")),
        "시작시간":clean_str(entry.get("시작시간")),
        "종료시간":clean_str(entry.get("종료시간")),
        "시작주행거리":safe_int(entry.get("시작주행거리")),
        "종료주행거리":safe_int(entry.get("종료주행거리")),
        "운행거리":distance,
        "운행완료일시":entry.get("운행완료일시"),
        "상태":clean_str(entry.get("상태")),
        "월":run_date.strftime("%Y-%m"),
        "반납 장소":clean_str(entry.get("반납 장소")),
        "사용자 부서":clean_str(entry.get("사용자 부서")),
        "업무용 사용거리":safe_int(entry.get("업무용 사용거리"), default=distance),
        "일반업무용 사용거리":safe_int(entry.get("일반업무용 사용거리"), default=distance),
    }
    for name,value in values.items():
        col=header_map.get(name)
        if col:
            raw.cell(target,col,value)
    if header_map.get("운행일"):
        raw.cell(target,header_map["운행일"]).number_format="yyyy-mm-dd"
    if header_map.get("운행완료일시"):
        raw.cell(target,header_map["운행완료일시"]).number_format="yyyy-mm-dd hh:mm"
    for name in ["시작주행거리","종료주행거리","운행거리","업무용 사용거리","일반업무용 사용거리"]:
        if header_map.get(name):
            raw.cell(target,header_map[name]).number_format="#,##0"
    wb.save(RUN_LOG_FILE)


def load_run_log_all_v2():
    if not RUN_LOG_FILE.exists(): return pd.DataFrame(columns=RUN_LOG_COLUMNS)
    try:
        xls=pd.ExcelFile(RUN_LOG_FILE)
        if RUNLOG_DATA_SHEET_V2 not in xls.sheet_names: return pd.DataFrame(columns=RUN_LOG_COLUMNS)
        df=pd.read_excel(RUN_LOG_FILE,sheet_name=RUNLOG_DATA_SHEET_V2)
    except Exception:
        return pd.DataFrame(columns=RUN_LOG_COLUMNS)
    df=normalize_columns(df)
    for col in RUN_LOG_COLUMNS:
        if col not in df.columns: df[col]=""
    return df[RUN_LOG_COLUMNS]

# =========================================================
# 차량 추가 / 삭제 / 운행현황 / 운행 종료
# =========================================================
def _base_vehicle_row():
    return {
        "차량번호": "",
        "차종": "",
        "구분": "렌트",
        "현장": "본사",
        "사용자": "",
        "사용자 연락처": "",
        "관리자": "김재열",
        "관리자 연락처": "010-4714-0595",
        "상태": "운행가능",
        "누적주행거리": 0,
        "차량 구매일": None,
        "최근정비일": None,
        "정비 시작일": None,
        "정비 예정 완료일": None,
        "타이어 교체 km수": 0,
        "엔진오일, 필터 교체 km수": 0,
        "타이어 교체(4만 km 마다)": "정상",
        "엔진오일, 필터 교환(1만 km 마다)": "정상",
        "차량 교체 4년 마다": "정상",
        "차량 구매 비용": "",
    }


def add_vehicle(cars_df):
    st.markdown("### ➕ 차량 추가")
    st.caption("저장하면 차량목록.xlsx에 즉시 추가됩니다. 구매=임원용차량, 렌트=업무용차량으로 자동 구분됩니다.")
    with st.form("vehicle-add-form", clear_on_submit=False):
        c1, c2, c3, c4 = st.columns(4)
        vehicle_no = c1.text_input("차량번호", placeholder="예: 12가3456")
        model = c2.text_input("차종", placeholder="예: 쏘렌토")
        kind = c3.selectbox("구분", ["렌트", "구매"])
        site = c4.selectbox("현장", SITES)

        c1, c2, c3, c4 = st.columns(4)
        user = c1.text_input("사용자", value="" if kind == "렌트" else "")
        user_phone = c2.text_input("사용자 연락처")
        status = c3.selectbox("상태", ["운행가능", "정비중"])
        mileage = c4.number_input("누적주행거리 (km)", min_value=0, value=0, step=100)

        c1, c2, c3 = st.columns(3)
        purchase_date = c1.date_input("차량 구매일", value=None)
        recent_date = c2.date_input("최근정비일", value=None)
        purchase_cost = c3.text_input("차량 구매 비용", placeholder="예: 40,000,000원")

        submitted = st.form_submit_button("💾 차량 추가", type="primary", use_container_width=True)

    if submitted:
        vehicle_no = clean_str(vehicle_no)
        model = clean_str(model)
        if not vehicle_no or not model:
            st.error("차량번호와 차종은 필수입니다.")
            return
        if vehicle_no in set(cars_df["차량번호"].astype(str).str.strip()):
            st.error(f"이미 등록된 차량번호입니다: {vehicle_no}")
            return

        row = _base_vehicle_row()
        row.update({
            "차량번호": vehicle_no,
            "차종": model,
            "구분": kind,
            "현장": site,
            "사용자": clean_str(user),
            "사용자 연락처": clean_str(user_phone),
            "상태": status,
            "누적주행거리": int(mileage),
            "차량 구매일": purchase_date,
            "최근정비일": recent_date,
            "차량 구매 비용": clean_str(purchase_cost),
        })
        if kind == "렌트":
            row["사용자"] = ""
            row["사용자 연락처"] = ""

        out = pd.concat([cars_df, pd.DataFrame([row])], ignore_index=True)
        save_cars(out)
        try:
            wb = _ensure_runlog_workbook_v2()
            _ensure_vehicle_runlog_sheet_v2(wb, row)
            wb.save(RUN_LOG_FILE)
        except Exception as e:
            st.warning(f"차량목록은 저장됐지만 운행일지 차량 시트 생성에 실패했습니다: {e}")
        st.success(f"{vehicle_no} 차량이 추가되었습니다.")
        st.rerun()


def delete_vehicle(cars_df, reservations):
    st.markdown("### 🗑 차량 삭제")
    if cars_df.empty:
        st.info("등록된 차량이 없습니다.")
        return

    numbers = [clean_str(v) for v in cars_df["차량번호"].tolist()]
    selected = st.selectbox("삭제할 차량", numbers, key="delete_vehicle_select")
    row = cars_df[cars_df["차량번호"].astype(str).str.strip() == selected]
    if row.empty:
        return
    row = row.iloc[0]
    st.warning(
        f"삭제 대상: **{selected} · {clean_str(row['차종'])} · {vehicle_display_type(row)} · {clean_str(row['현장'])}**"
    )

    conflict = None
    for _, r in active_reservations(reservations).iterrows():
        if clean_str(r["차량번호"]) == selected:
            conflict = r
            break
    if conflict is not None:
        st.error("활성 예약이 있는 차량은 삭제할 수 없습니다.")
        st.info(f"기존 예약: {fmt_period(conflict)} · {clean_str(conflict['예약자'])}")
        return

    confirm = st.checkbox("삭제할 차량임을 확인했습니다.", key="delete_vehicle_confirm")
    if st.button("🗑 차량 삭제", type="primary", disabled=not confirm, use_container_width=True):
        out = cars_df[cars_df["차량번호"].astype(str).str.strip() != selected].copy()
        save_cars(out)
        st.success(f"{selected} 차량이 삭제되었습니다.")
        st.rerun()


def get_today_vehicle_status_rows(cars, reservations):
    today = today_kst()
    running = get_running_vehicle_numbers_today(reservations)
    pending_today = set()
    active = active_reservations(reservations)
    for _, r in active.iterrows():
        if clean_str(r["상태"]) != "승인대기":
            continue
        if r["예약일"] and r["종료일"] and r["예약일"] <= today <= r["종료일"]:
            pending_today.add(clean_str(r["차량번호"]))

    rows = []
    for _, car in cars.iterrows():
        no = clean_str(car["차량번호"])
        state = clean_str(car["상태"])
        if state == "정비중":
            op = "정비중"
            code = 1
        elif clean_str(car["차량 교체 4년 마다"]) == "교체 대상":
            op = "교체대상"
            code = 2
        elif no in running:
            op = "운행중"
            code = 5
        elif no in pending_today:
            op = "예약대기"
            code = 4
        elif clean_str(car["구분"]) == "구매":
            op = "임원용"
            code = 3
        else:
            op = "운행가능"
            code = 6
        rows.append({
            "차량번호": no,
            "차종": clean_str(car["차종"]),
            "차량용도": vehicle_display_type(car),
            "현장": clean_str(car["현장"]),
            "오늘 운영상태": op,
            "운영상태코드": code,
            "누적주행거리": safe_int(car["누적주행거리"]),
        })
    return pd.DataFrame(rows)


def render_vehicle_operations_chart(cars, reservations):
    st.markdown("### 📊 차량별 운영현황")
    st.caption("오늘 기준 상태입니다. 업무용차량의 승인완료 예약은 '운행중', 승인대기 예약은 '예약대기'로 표시합니다.")
    status_df = get_today_vehicle_status_rows(cars, reservations)
    if status_df.empty:
        st.info("차량 데이터가 없습니다.")
        return

    work_only = status_df[status_df["차량용도"] == "업무용차량"].copy()
    executive_count = int((status_df["차량용도"] == "임원용차량").sum())
    work_count = len(work_only)
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("전체", len(status_df))
    c2.metric("임원", executive_count)
    c3.metric("업무용", work_count)
    c4.metric("오늘 운행중", int((status_df["오늘 운영상태"] == "운행중").sum()))

    chart_df = work_only[["차량번호", "운영상태코드"]].sort_values("차량번호").set_index("차량번호")
    st.bar_chart(chart_df, height=360)
    st.caption("운영상태코드: 6 운행가능 · 5 운행중 · 4 예약대기 · 2 교체대상 · 1 정비중")

    st.dataframe(
        status_df[["차량번호", "차종", "차량용도", "현장", "오늘 운영상태", "누적주행거리"]]
        .sort_values(["차량용도", "현장", "차량번호"])
        .assign(누적주행거리=lambda d: d["누적주행거리"].apply(lambda x: f"{fmt_number(x)} km")),
        use_container_width=True,
        hide_index=True,
    )


def complete_reservation_with_km(
    cars, reservations, reservation_no, end_km,
    return_location="", use_date=None, department="", user_name="",
):
    """반납 정보를 예약목록과 차량별 운행일지에 저장한다."""
    reservation_no = int(reservation_no)
    idx_list = reservations.index[
        pd.to_numeric(reservations["예약번호"], errors="coerce") == reservation_no
    ].tolist()
    if not idx_list:
        return False, "예약번호를 찾을 수 없습니다."

    idx = idx_list[0]
    row = reservations.loc[idx].copy()
    if clean_str(row.get("상태")) != "승인완료":
        return False, "승인완료된 예약만 반납할 수 있습니다."

    return_location = clean_str(return_location)
    if not return_location:
        return False, "반납 장소를 입력해 주세요."

    vehicle_no = clean_str(row.get("차량번호"))
    car_idx_list = cars.index[cars["차량번호"].astype(str).str.strip() == vehicle_no].tolist()
    if not car_idx_list:
        return False, "차량 정보를 찾을 수 없습니다."
    car_idx = car_idx_list[0]
    car = cars.loc[car_idx]

    start_km = safe_int(row.get("시작주행거리"), default=safe_int(car.get("누적주행거리")))
    current_km = safe_int(car.get("누적주행거리"))
    end_km = int(end_km)
    if end_km < start_km:
        return False, f"주행 후 계기판 거리는 주행 전 계기판 거리({start_km:,} km) 이상이어야 합니다."
    if end_km < current_km:
        return False, f"주행 후 계기판 거리는 현재 차량 누적주행거리({current_km:,} km)보다 작을 수 없습니다."

    distance = end_km - start_km
    use_date = _date_value(use_date) or _date_value(row.get("예약일")) or today_kst()
    reservation_start = _date_value(row.get("예약일"))
    reservation_end = _date_value(row.get("종료일"))
    if reservation_start and use_date < reservation_start:
        return False, f"사용일자는 예약 시작일({reservation_start:%Y-%m-%d}) 이후로 선택해 주세요."
    if reservation_end and use_date > reservation_end:
        return False, f"사용일자는 예약 종료일({reservation_end:%Y-%m-%d}) 이전으로 선택해 주세요."
    if use_date > today_kst():
        return False, "사용일자는 오늘 날짜 또는 이전 날짜로 선택해 주세요."

    now = datetime.now().replace(microsecond=0)
    department = clean_str(department) or clean_str(row.get("현장"))
    user_name = clean_str(user_name) or clean_str(row.get("예약자"))

    reservations.loc[idx, "시작주행거리"] = start_km
    reservations.loc[idx, "종료주행거리"] = end_km
    reservations.loc[idx, "운행거리"] = distance
    reservations.loc[idx, "운행완료일시"] = now
    reservations.loc[idx, "상태"] = "운행완료"
    reservations.loc[idx, "반납 장소"] = return_location
    reservations.loc[idx, "사용일자"] = pd.Timestamp(use_date)
    reservations.loc[idx, "사용자 부서"] = department
    reservations.loc[idx, "업무용 사용거리"] = distance
    reservations.loc[idx, "일반업무용 사용거리"] = distance

    cars.loc[car_idx, "누적주행거리"] = end_km
    save_reservations(reservations)
    save_cars(cars)

    log_entry = {
        "운행일": use_date,
        "예약번호": reservation_no,
        "차량번호": vehicle_no,
        "차종": clean_str(row.get("차종")),
        "현장": department,
        "사용자": user_name,
        "목적지": clean_str(row.get("목적지")),
        "사용목적": clean_str(row.get("사용목적")),
        "예약기간": fmt_period(row),
        "시작시간": clean_str(row.get("시작시간")),
        "종료시간": clean_str(row.get("종료시간")),
        "시작주행거리": start_km,
        "종료주행거리": end_km,
        "운행거리": distance,
        "운행완료일시": now,
        "상태": "운행완료",
        "반납 장소": return_location,
        "사용자 부서": department,
        "업무용 사용거리": distance,
        "일반업무용 사용거리": distance,
    }
    save_run_log_entry_v2(log_entry)
    return True, f"예약 #{reservation_no} 차량 반납이 완료되었습니다. 주행거리 {distance:,} km가 운행일지에 저장되었습니다."


def load_run_log_all():
    if not RUN_LOG_FILE.exists():
        return pd.DataFrame(columns=RUN_LOG_COLUMNS)
    all_rows = []
    try:
        xls = pd.ExcelFile(RUN_LOG_FILE)
        for sheet in xls.sheet_names:
            if sheet == "사용안내":
                continue
            try:
                df = pd.read_excel(RUN_LOG_FILE, sheet_name=sheet)
                if not df.empty:
                    all_rows.append(df)
            except Exception:
                pass
    except Exception:
        return pd.DataFrame(columns=RUN_LOG_COLUMNS)
    if not all_rows:
        return pd.DataFrame(columns=RUN_LOG_COLUMNS)
    out = pd.concat(all_rows, ignore_index=True)
    for col in RUN_LOG_COLUMNS:
        if col not in out.columns:
            out[col] = ""
    return out[RUN_LOG_COLUMNS]


# =========================================================
# 관리자 페이지
# =========================================================
@live_refresh_fragment
def admin_dashboard(cars, reservations):
    # 자동 갱신마다 엑셀 원본을 새로 읽어 KPI/표/승인 목록을 최신 상태로 유지한다.
    cars = load_cars()
    reservations = load_reservations()
    today = today_kst()
    work = get_rent_cars(cars)
    executive = get_executive_cars(cars)
    maintenance = get_maintenance_cars(work)
    replacement = get_replacement_cars(cars)
    operational_work = work[work["상태"].astype(str).str.strip() == "운행가능"]
    reserved_today = get_reserved_vehicle_numbers_today(reservations) & set(work["차량번호"].astype(str).str.strip())
    running_today = get_running_vehicle_numbers_today(reservations) & set(work["차량번호"].astype(str).str.strip())
    available_today = max(0, len(operational_work) - len(reserved_today))
    pending = reservations[reservations["상태"] == "승인대기"].copy() if not reservations.empty else reservations

    st.title("🚗 법인차량 관리 AI")
    st.caption(f"기준일: {fmt_date(today)} · 구매=임원용차량 · 렌트=업무용차량")

    k = st.columns(7)
    with k[0]: render_kpi("전체 차량", len(cars), "전체 48대 기준")
    with k[1]: render_kpi("임원용 차량", len(executive), "구매 차량")
    with k[2]: render_kpi("업무용 차량", len(work), "렌트 차량")
    with k[3]: render_kpi("오늘 정비중", len(maintenance), "업무용 정비차량")
    with k[4]: render_kpi("오늘 운행중", len(running_today), "승인완료 + 오늘 기간 포함")
    with k[5]: render_kpi("오늘 예약", len(reserved_today), "승인대기/승인완료 포함")
    with k[6]: render_kpi("오늘 운행가능", available_today, f"정상 {len(operational_work)} - 오늘 예약 {len(reserved_today)}")

    st.markdown("## 🚙 전체 차량 현황")
    tab_all, tab_exec, tab_work = st.tabs([
        f"전체 ({len(cars)}대)",
        f"임원 ({len(executive)}대)",
        f"업무용 ({len(work)}대)",
    ])

    with tab_all:
        st.dataframe(
            cars[["차량번호", "차종", "차량용도", "구분", "현장", "사용자", "상태", "누적주행거리"]]
            .assign(누적주행거리=lambda d: d["누적주행거리"].apply(lambda x: f"{fmt_number(x)} km")),
            use_container_width=True,
            hide_index=True,
        )

    with tab_exec:
        st.info("구매 차량은 임원용차량으로 관리되며 사용자 예약 화면에서는 선택할 수 없습니다.")
        st.dataframe(
            executive[["차량번호", "차종", "현장", "사용자", "상태", "누적주행거리", "차량 교체 4년 마다"]]
            .assign(누적주행거리=lambda d: d["누적주행거리"].apply(lambda x: f"{fmt_number(x)} km")),
            use_container_width=True,
            hide_index=True,
        )

    with tab_work:
        st.caption("렌트 차량만 업무용차량입니다. 정비중 차량은 아래 관리 탭에서 별도 확인합니다.")
        site_filter = st.radio("현장", ["전체"] + SITES, horizontal=True, key="admin_work_site")
        work_view = work.copy()
        if site_filter != "전체":
            work_view = work_view[work_view["현장"].astype(str).str.strip() == site_filter]
        operational_view = work_view[work_view["상태"].astype(str).str.strip() != "정비중"].copy()
        st.dataframe(
            operational_view[["차량번호", "차종", "현장", "상태", "누적주행거리", "차량 교체 4년 마다"]]
            .assign(누적주행거리=lambda d: d["누적주행거리"].apply(lambda x: f"{fmt_number(x)} km")),
            use_container_width=True,
            hide_index=True,
        )

    st.divider()
    st.markdown("## 🛠 차량 관리")
    manage_tabs = st.tabs([
        f"♻️ 교체 대상 차량 ({len(replacement)}대)",
        f"🔧 정비 차량 ({len(maintenance)}대)",
        "➕ 차량 추가 / 삭제",
    ])

    # 교체 대상
    with manage_tabs[0]:
        if replacement.empty:
            st.success("현재 교체 대상 차량이 없습니다.")
        else:
            replacement_table = replacement[[
                "차량번호", "차종", "구분", "현장", "사용자", "사용자 연락처",
                "상태", "누적주행거리", "차량 구매일", "차량 구매 비용", "차량 교체 4년 마다"
            ]].copy()
            replacement_table["누적주행거리"] = replacement_table["누적주행거리"].apply(lambda x: f"{fmt_number(x)} km")
            replacement_table["차량 구매 비용"] = replacement_table["차량 구매 비용"].apply(fmt_money)
            st.dataframe(replacement_table, use_container_width=True, hide_index=True)
            nums = [clean_str(x) for x in replacement["차량번호"].tolist()]
            selected = st.selectbox("교체대상 차량번호", nums, key="replacement_select_v2")
            if st.button("♻️ 상세 / 편집", key="replacement_open_v2", use_container_width=True):
                selected_row = replacement[replacement["차량번호"].astype(str).str.strip() == selected].iloc[0]
                replacement_dialog(selected_row.to_dict(), cars)

    # 정비
    with manage_tabs[1]:
        if maintenance.empty:
            st.success("현재 정비 차량이 없습니다.")
        else:
            maintenance_table = maintenance[[
                "차량번호", "차종", "구분", "현장", "상태", "누적주행거리",
                "최근정비일", "정비 시작일", "정비 예정 완료일",
                "타이어 교체 km수", "엔진오일, 필터 교체 km수",
                "타이어 교체(4만 km 마다)", "엔진오일, 필터 교환(1만 km 마다)"
            ]].copy()
            for col in ["누적주행거리", "타이어 교체 km수", "엔진오일, 필터 교체 km수"]:
                maintenance_table[col] = maintenance_table[col].apply(lambda x: f"{fmt_number(x)} km")
            for col in ["최근정비일", "정비 시작일", "정비 예정 완료일"]:
                maintenance_table[col] = maintenance_table[col].apply(lambda x: fmt_date(_date_value(x)))
            st.dataframe(maintenance_table, use_container_width=True, hide_index=True)
            nums = [clean_str(x) for x in maintenance["차량번호"].tolist()]
            selected = st.selectbox("정비차량 번호", nums, key="maintenance_select_v2")
            if st.button("🔧 상세 / 편집", key="maintenance_open_v2", use_container_width=True):
                selected_row = maintenance[maintenance["차량번호"].astype(str).str.strip() == selected].iloc[0]
                maintenance_dialog(selected_row.to_dict(), cars)

    # 차량 추가 삭제
    with manage_tabs[2]:
        add_col, del_col = st.columns(2)
        with add_col:
            add_vehicle(cars)
        with del_col:
            delete_vehicle(cars, reservations)

    st.divider()
    render_vehicle_operations_chart(cars, reservations)

    st.divider()
    st.markdown('### 📅 월간 예약 현황')
    cal_col1, cal_col2 = st.columns(2)
    with cal_col1:
        year = st.selectbox("연도", list(range(today.year - 1, today.year + 2)), index=1, key="admin_year_v2")
    with cal_col2:
        month = st.selectbox("월", list(range(1, 13)), index=today.month - 1, key="admin_month_v2")
    render_calendar(reservations, year, month)

    st.divider()
    st.markdown("### 🟡 예약 승인 / 반려")
    if pending.empty:
        st.success("현재 승인대기 예약이 없습니다.")
    else:
        for _, row in pending.sort_values(["예약일", "예약번호"]).iterrows():
            with st.container(border=True):
                a, b, c, d = st.columns([1.0, 1.7, 3.2, 1.5])
                with a:
                    st.markdown(f"**#{int(row['예약번호'])}**")
                    st.markdown(status_badge("승인대기"), unsafe_allow_html=True)
                with b:
                    st.markdown(f"**{clean_str(row['차량번호'])} · {clean_str(row['차종'])}**")
                    st.caption(f"{clean_str(row['현장'])} · {clean_str(row['차량용도'])}")
                with c:
                    st.markdown(f"**예약기간**  {fmt_period(row)}")
                    st.caption(
                        f"예약자: {clean_str(row['예약자'])} · {clean_str(row['인원'])}명 · "
                        f"목적지: {clean_str(row['목적지']) or '-'} · 사용목적: {clean_str(row['사용목적']) or '-'}"
                    )
                with d:
                    b1, b2 = st.columns(2)
                    if b1.button("✅ 승인", key=f"approve-{int(row['예약번호'])}", use_container_width=True):
                        conflict = check_vehicle_conflict(
                            reservations, row["차량번호"], row["예약일"], row["종료일"],
                            exclude_reservation_number=int(row["예약번호"]),
                        )
                        if conflict is not None:
                            st.error(f"다른 활성예약과 충돌합니다: {fmt_period(conflict)}")
                        else:
                            reservations.loc[reservations["예약번호"] == int(row["예약번호"]), "상태"] = "승인완료"
                            save_reservations(reservations)
                            st.rerun()
                    if b2.button("❌ 반려", key=f"reject-{int(row['예약번호'])}", use_container_width=True):
                        reservations.loc[reservations["예약번호"] == int(row["예약번호"]), "상태"] = "반려"
                        save_reservations(reservations)
                        st.rerun()

    st.divider()
    st.markdown("### ✅ 승인완료 / 현재 예약")
    active_display = active_reservations(reservations)
    if active_display.empty:
        st.info("현재 활성 예약이 없습니다.")
    else:
        active_display = active_display.sort_values(["예약일", "예약번호"], ascending=[True, True]).copy()
        view = active_display[[
            "예약번호", "현장", "차량용도", "차량번호", "차종", "예약자",
            "예약일", "종료일", "시작시간", "종료시간", "인원", "목적지", "사용목적", "상태"
        ]].copy()
        view["예약기간"] = view.apply(fmt_period, axis=1)
        view["표시상태"] = view.apply(reservation_runtime_label, axis=1)
        # 필요한 열을 view에 생성한 뒤 선택한다. 사용목적 누락 KeyError를 방지한다.
        view = view[[
            "예약번호", "현장", "차량용도", "차량번호", "차종", "예약자",
            "예약기간", "인원", "목적지", "사용목적", "표시상태"
        ]]
        st.dataframe(view, use_container_width=True, hide_index=True)

    st.divider()
    st.markdown("### 📒 운행일지")
    logs = load_run_log_all_v2()
    if logs.empty:
        st.info("아직 완료된 운행일지가 없습니다. 사용자가 차량 사용 후 종료주행거리를 입력하면 월별 운행일지에 자동 기록됩니다.")
    else:
        display_logs = logs.copy()
        for col in ["시작주행거리", "종료주행거리", "운행거리"]:
            display_logs[col] = pd.to_numeric(display_logs[col], errors="coerce").apply(lambda x: f"{fmt_number(x)} km")
        st.dataframe(
            display_logs[["운행일", "예약번호", "차량번호", "차종", "현장", "사용자", "예약기간", "시작주행거리", "종료주행거리", "운행거리", "운행완료일시"]]
            .sort_values(["운행일", "예약번호"], ascending=[False, False]),
            use_container_width=True,
            hide_index=True,
        )


# =========================================================
# 사용자 페이지
# =========================================================
def reservation_summary(ai_result, car, user_name):
    start = datetime.strptime(ai_result["start_date"], "%Y-%m-%d").date()
    end = datetime.strptime(ai_result["end_date"], "%Y-%m-%d").date()
    business = get_business_day_count(start, end)
    st.success("예약 조건을 확인했습니다.")
    st.markdown(
        f"""
        **예약자:** {html.escape(user_name)}  
        **차량:** {html.escape(clean_str(car['차량번호']))} · {html.escape(clean_str(car['차종']))}  
        **현장:** {html.escape(clean_str(car['현장']))} · 업무용차량  
        **기간:** {fmt_date(start)} ~ {fmt_date(end)}  
        **시간:** {html.escape(ai_result.get('start_time') or DEFAULT_START_TIME)} ~ {html.escape(ai_result.get('end_time') or DEFAULT_END_TIME)}  
        **인원:** {int(ai_result.get('passengers') or 1)}명  
        **목적지:** {html.escape(clean_str(ai_result.get('destination')) or '-')}  
        **사용목적:** {html.escape(clean_str(ai_result.get('purpose')) or '-')}  
        **영업일수:** {business}일  
        **상태:** 🟡 승인대기
        """,
        unsafe_allow_html=True,
    )


def ai_reservation_tab(cars, reservations, user_name):
    st.markdown("### 🤖 AI 자연어 차량 예약")
    st.caption("Ollama qwen2.5vl:3b가 자연어 요청을 분석하고, Python이 실제 예약 가능 여부를 다시 검증합니다.")

    if not user_name:
        st.warning("먼저 예약자 이름을 입력하세요.")
        return

    block = check_user_reservation_block(reservations, user_name)
    if block is not None:
        st.error(f"현재 {user_name}님은 추가 예약이 불가합니다.")
        st.info(f"기존 예약: {fmt_period(block)} · {clean_str(block['차량번호'])} · {clean_str(block['상태'])}")
        return

    example = "내가 다음주에 월요일부터 수요일 퇴근할때까지 차량을 예약하려고 하는데 3명이서 평택 공장으로 가려고 해 어떤 차량을 예약하면 좋을지 확인하고 예약해줘"
    request = st.text_area(
        "예약 요청을 자연어로 입력하세요",
        height=110,
        placeholder=example,
        key="ai_request_text",
    )

    if st.button("🤖 AI 분석하기", type="primary", use_container_width=True):
        if not request.strip():
            st.warning("예약 요청 내용을 입력하세요.")
        else:
            with st.spinner("Ollama qwen2.5vl:3b가 예약 조건을 분석하고 있습니다..."):
                result, error = parse_ai_request(request)
            if error:
                st.error(error)
            else:
                st.session_state["ai_result"] = result
                st.session_state.pop("ai_selected_car", None)
                st.rerun()

    ai_result = st.session_state.get("ai_result")
    if not ai_result:
        return

    st.divider()
    st.markdown("### ① AI가 해석한 예약 조건")
    rcols = st.columns(6)
    rcols[0].metric("시작일", clean_str(ai_result.get("start_date")) or "미확인")
    rcols[1].metric("종료일", clean_str(ai_result.get("end_date")) or "미확인")
    rcols[2].metric("시작시간", clean_str(ai_result.get("start_time")) or DEFAULT_START_TIME)
    rcols[3].metric("종료시간", clean_str(ai_result.get("end_time")) or DEFAULT_END_TIME)
    rcols[4].metric("인원", f"{int(ai_result.get('passengers') or 1)}명")
    rcols[5].metric("현장", clean_str(ai_result.get("site")) or "미지정")

    if not ai_result.get("start_date") or not ai_result.get("end_date"):
        st.error("AI가 예약 날짜를 확실하게 해석하지 못했습니다. 자연어에 날짜를 조금 더 명확하게 적어주세요.")
        return

    try:
        start_date = datetime.strptime(ai_result["start_date"], "%Y-%m-%d").date()
        end_date = datetime.strptime(ai_result["end_date"], "%Y-%m-%d").date()
    except Exception:
        st.error("AI가 반환한 날짜 형식이 올바르지 않습니다.")
        return

    today = today_kst()
    if start_date < today:
        st.error("과거 날짜는 예약할 수 없습니다.")
        return
    if end_date < start_date:
        st.error("종료일이 시작일보다 빠릅니다.")
        return
    if end_date > today + timedelta(days=MAX_RESERVATION_DAYS):
        st.error(f"예약은 {fmt_date(today + timedelta(days=MAX_RESERVATION_DAYS))}까지 가능합니다.")
        return

    business_days = get_business_day_count(start_date, end_date)
    if business_days == 0:
        st.error("선택한 기간에 영업일이 없습니다.")
        return
    if business_days > MAX_BUSINESS_DAYS:
        st.error(f"최대 {MAX_BUSINESS_DAYS}영업일까지만 예약 가능합니다. 현재 {business_days}영업일입니다.")
        return

    try:
        recommendations = get_ai_recommendations(cars, reservations, ai_result)
    except Exception as exc:
        st.error(f"예약 가능 차량을 계산하는 중 오류가 발생했습니다: {exc}")
        return

    st.markdown("### ② AI 추천 차량")
    if not recommendations:
        site_text = clean_str(ai_result.get("site"))
        st.warning(f"{site_text or '선택한 조건'}에서 조건에 맞는 업무용 차량을 찾지 못했습니다.")
        st.caption("구매 차량(임원용)은 제외되며, 정비중 차량과 기존 예약 차량도 자동 제외됩니다.")
        return

    option_labels = [
        f"{clean_str(car['차량번호'])} · {clean_str(car['차종'])} · {clean_str(car['현장'])} · 정원 {get_capacity(car)}명 · 주행 {fmt_number(car['누적주행거리'])}km"
        for car in recommendations
    ]
    selected_label = st.radio("추천 차량 중 선택", option_labels, key="ai_car_choice")
    selected_index = option_labels.index(selected_label)
    selected_car = recommendations[selected_index]
    st.session_state["ai_selected_car"] = clean_str(selected_car["차량번호"])

    reservation_summary(ai_result, selected_car, user_name)

    st.divider()
    st.markdown("### ③ 예약 신청")
    if st.button("🚗 이 조건으로 예약 신청", type="primary", use_container_width=True):
        # 최종 저장 직전 모든 조건을 다시 검증
        current_reservations = load_reservations()
        ok, msg, _ = make_reservation(
            cars,
            current_reservations,
            user_name=user_name,
            vehicle_number=clean_str(selected_car["차량번호"]),
            start_date=start_date,
            end_date=end_date,
            start_time=clean_str(ai_result.get("start_time")) or DEFAULT_START_TIME,
            end_time=clean_str(ai_result.get("end_time")) or DEFAULT_END_TIME,
            purpose=clean_str(ai_result.get("purpose")) or "업무용 차량 예약",
            destination=clean_str(ai_result.get("destination")),
            passengers=int(ai_result.get("passengers") or 1),
            status="승인대기",
        )
        if ok:
            st.success(msg)
            st.session_state.pop("ai_result", None)
            st.session_state.pop("ai_selected_car", None)
            st.rerun()
        else:
            st.error(msg)


def direct_reservation_tab(cars, reservations, user_name):
    st.markdown("### 📝 직접 예약")
    if not user_name:
        st.warning("예약자 이름을 먼저 입력하세요.")
        return

    block = check_user_reservation_block(reservations, user_name)
    if block is not None:
        st.error(f"현재 {user_name}님은 추가 예약이 불가합니다.")
        st.info(f"기존 예약: {fmt_period(block)} · {clean_str(block['차량번호'])} · {clean_str(block['상태'])}")
        return

    today = today_kst()
    min_date = today
    max_date = today + timedelta(days=MAX_RESERVATION_DAYS)

    site = st.selectbox("현장", SITES, key="direct_site")
    date_value = st.date_input(
        "예약 기간",
        value=(today, today),
        min_value=min_date,
        max_value=max_date,
        format="YYYY-MM-DD",
        key="direct_dates",
    )
    if isinstance(date_value, tuple) or isinstance(date_value, list):
        start_date, end_date = date_value[0], date_value[-1]
    else:
        start_date = end_date = date_value

    business_days = get_business_day_count(start_date, end_date)
    c1, c2, c3 = st.columns(3)
    c1.metric("영업일수", f"{business_days}일")
    c2.metric("최대 영업일", f"{MAX_BUSINESS_DAYS}일")
    c3.metric("예약 가능 종료일", fmt_date(max_date))

    if business_days == 0:
        st.error("선택한 기간에 영업일이 없습니다.")
        return
    if business_days > MAX_BUSINESS_DAYS:
        st.error(f"최대 {MAX_BUSINESS_DAYS}영업일까지 예약할 수 있습니다.")
        return

    available_cars = []
    work_site = get_rent_cars(cars)
    work_site = work_site[work_site["현장"].astype(str).str.strip() == site]
    for _, car in work_site.iterrows():
        if clean_str(car["상태"]) != "운행가능":
            continue
        conflict = check_vehicle_conflict(reservations, car["차량번호"], start_date, end_date)
        if conflict is None:
            available_cars.append(car)

    if not available_cars:
        st.warning(f"{site} 현장에 해당 기간 예약 가능한 업무용차량이 없습니다.")
        return

    labels = [
        f"{clean_str(car['차량번호'])} · {clean_str(car['차종'])} · 정원 {get_capacity(car)}명 · 주행 {fmt_number(car['누적주행거리'])}km"
        for car in available_cars
    ]
    selected_label = st.selectbox("예약 차량", labels)
    selected_car = available_cars[labels.index(selected_label)]

    options = time_options()
    c1, c2 = st.columns(2)
    start_time = c1.selectbox("시작시간", options, index=options.index(DEFAULT_START_TIME), key="direct_start")
    end_time = c2.selectbox("종료시간", options, index=options.index(DEFAULT_END_TIME), key="direct_end")

    passengers = st.number_input("인원", min_value=1, max_value=max(1, get_capacity(selected_car)), value=min(3, get_capacity(selected_car)), step=1)
    destination = st.text_input("목적지", placeholder="예: 평택 공장")
    purpose = st.text_input("사용목적", placeholder="예: 생산회의 참석")

    st.markdown("#### 예약 확인")
    st.info(
        f"{user_name} · {clean_str(selected_car['차량번호'])} ({clean_str(selected_car['차종'])}) · {site} · "
        f"{fmt_date(start_date)} ~ {fmt_date(end_date)} · {start_time} ~ {end_time} · {int(passengers)}명 · 승인대기"
    )

    if st.button("🚗 예약 신청", type="primary", use_container_width=True):
        ok, msg, _ = make_reservation(
            cars,
            reservations,
            user_name=user_name,
            vehicle_number=clean_str(selected_car["차량번호"]),
            start_date=start_date,
            end_date=end_date,
            start_time=start_time,
            end_time=end_time,
            purpose=purpose or "업무용 차량 예약",
            destination=destination,
            passengers=int(passengers),
            status="승인대기",
        )
        if ok:
            st.success(msg)
            st.rerun()
        else:
            st.error(msg)


def trip_completion_tab(cars, reservations, user_name, reservation_no=None):
    """승인된 차량의 반납 장소와 운행기록을 입력받는다."""
    if not user_name:
        st.warning("먼저 예약자 이름을 입력하세요.")
        return

    mine = reservations[
        (reservations["예약자"].astype(str).str.strip() == user_name.strip())
        & (reservations["상태"].astype(str).str.strip() == "승인완료")
    ].copy() if not reservations.empty else pd.DataFrame()

    if reservation_no is not None:
        mine = mine[pd.to_numeric(mine["예약번호"], errors="coerce") == int(reservation_no)]
    if mine.empty:
        st.info("현재 반납할 승인완료 예약이 없습니다.")
        return

    mine = mine.sort_values(["예약일", "예약번호"], ascending=[True, True])
    row = mine.iloc[0]
    rno = int(row["예약번호"])
    vehicle_no = clean_str(row.get("차량번호"))
    car_match = cars[cars["차량번호"].astype(str).str.strip() == vehicle_no]
    if car_match.empty:
        st.error("차량 정보를 찾을 수 없습니다.")
        return
    car = car_match.iloc[0]

    start_km = safe_int(row.get("시작주행거리"), default=safe_int(car.get("누적주행거리")))
    current_km = safe_int(car.get("누적주행거리"))
    default_use_date = _date_value(row.get("사용일자")) or _date_value(row.get("예약일")) or today_kst()
    reservation_start = _date_value(row.get("예약일"))
    reservation_end = _date_value(row.get("종료일"))

    st.markdown("### 🚘 차량 반납 정보")
    st.caption("반납 정보를 작성하고 저장해야 예약이 종료되며, 이후 새 차량을 예약할 수 있습니다.")
    c1, c2, c3 = st.columns(3)
    c1.metric("예약 차량", f"{vehicle_no} · {clean_str(row.get('차종'))}")
    c2.metric("예약 기간", fmt_period(row))
    c3.metric("현재 누적주행거리", f"{current_km:,} km")

    return_location = st.text_input(
        "반납 장소",
        placeholder="예: 지하 2층 B2",
        key=f"return-location-{rno}",
    )
    d1, d2 = st.columns(2)
    with d1:
        use_date = st.date_input(
            "사용일자",
            value=default_use_date,
            min_value=reservation_start,
            max_value=reservation_end,
            key=f"return-use-date-{rno}",
            help="예약 기간에서 자동으로 불러오며, 조기 반납 시 실제 사용일자로 수정할 수 있습니다.",
        )
        department = st.text_input(
            "사용자 부서",
            value=clean_str(row.get("사용자 부서")) or clean_str(row.get("현장")) or "미등록",
            disabled=True,
            key=f"return-department-{rno}",
        )
        display_name = st.text_input(
            "사용자",
            value=clean_str(row.get("예약자")),
            disabled=True,
            key=f"return-user-{rno}",
        )
    with d2:
        st.text_input(
            "주행 전 계기판의 거리 (km)",
            value=f"{start_km:,}",
            disabled=True,
            key=f"return-start-km-display-{rno}",
        )
        end_km = st.number_input(
            "주행 후 계기판의 거리 (km)",
            min_value=max(0, start_km),
            value=max(0, start_km),
            step=1,
            format="%d",
            key=f"return-end-km-{rno}",
            help="숫자(정수)만 입력할 수 있습니다.",
        )
        distance = int(end_km) - start_km
        st.text_input(
            "주행거리 (km)",
            value=f"{distance:,}",
            disabled=True,
            key=f"return-distance-display-{rno}",
        )
        st.text_input(
            "업무용 사용거리 (km)",
            value=f"{distance:,}",
            disabled=True,
            key=f"return-business-km-display-{rno}",
        )
        st.text_input(
            "일반 업무용 사용거리 (km)",
            value=f"{distance:,}",
            disabled=True,
            key=f"return-general-business-km-display-{rno}",
        )

    if distance < 0 or end_km < current_km:
        st.error(f"주행 후 계기판 거리는 {max(start_km, current_km):,} km 이상이어야 합니다.")

    save_col, cancel_col = st.columns([3, 1])
    with save_col:
        save_clicked = st.button(
            "✅ 차량 반납 및 운행일지 저장",
            type="primary",
            use_container_width=True,
            key=f"save-vehicle-return-{rno}",
            disabled=(distance < 0 or int(end_km) < current_km),
        )
    with cancel_col:
        cancel_clicked = st.button(
            "닫기",
            use_container_width=True,
            key=f"cancel-vehicle-return-{rno}",
        )

    if cancel_clicked:
        st.session_state["vehicle_return_form_reservation"] = None
        st.rerun()

    if save_clicked:
        if not clean_str(return_location):
            st.error("반납 장소를 입력해 주세요. 예: 지하 2층 B2")
            return
        current_reservations = load_reservations()
        current_cars = load_cars()
        ok, msg = complete_reservation_with_km(
            current_cars,
            current_reservations,
            rno,
            int(end_km),
            return_location=return_location,
            use_date=use_date,
            department=department,
            user_name=display_name,
        )
        if ok:
            st.success(msg)
            st.session_state["vehicle_return_form_reservation"] = None
            st.rerun()
        else:
            st.error(msg)

    st.divider()
    st.markdown("#### 📒 내 운행 완료 이력")
    logs = load_run_log_all_v2()
    if logs.empty:
        st.info("운행 완료 이력이 없습니다.")
    else:
        mine_logs = logs[logs["사용자"].astype(str).str.strip() == user_name.strip()].copy()
        if mine_logs.empty:
            st.info("내 운행 완료 이력이 없습니다.")
        else:
            for col in ["시작주행거리", "종료주행거리", "운행거리"]:
                mine_logs[col] = pd.to_numeric(mine_logs[col], errors="coerce").apply(lambda x: f"{fmt_number(x)} km")
            st.dataframe(
                mine_logs[["운행일", "예약번호", "차량번호", "차종", "예약기간", "시작주행거리", "종료주행거리", "운행거리", "목적지"]]
                .sort_values(["운행일", "예약번호"], ascending=[False, False]),
                use_container_width=True,
                hide_index=True,
            )


@live_refresh_fragment
def user_page(cars, reservations):
    # 예약/반납 화면에서는 매 실행마다 엑셀을 다시 읽어 최신 상태로 처리한다.
    cars = load_cars()
    reservations = load_reservations()
    st.title("👤 차량 예약")
    st.caption("사용자 예약 대상은 렌트(업무용차량)입니다. 구매 차량은 임원용차량으로 예약할 수 없습니다.")

    user_name = st.text_input("예약자 이름", placeholder="예: 홍길동", key="user_name")
    own = reservations[reservations["예약자"].astype(str).str.strip() == user_name.strip()].copy() if user_name and not reservations.empty else pd.DataFrame()
    approved_mine = own[own["상태"].astype(str).str.strip() == "승인완료"].copy() if not own.empty else pd.DataFrame()
    pending_mine = own[own["상태"].astype(str).str.strip() == "승인대기"].copy() if not own.empty else pd.DataFrame()

    if not approved_mine.empty:
        approved_mine = approved_mine.sort_values(["예약일", "예약번호"], ascending=[True, True])
        return_row = approved_mine.iloc[0]
        reservation_no = int(return_row["예약번호"])
        st.success("차량 예약이 승인되었습니다. 사용을 마치면 아래 ‘차량 반납’ 버튼을 눌러 반납 정보를 작성해 주세요.")
        st.info(
            f"예약 차량: {clean_str(return_row.get('차량번호'))} · {clean_str(return_row.get('차종'))}\n\n"
            f"예약 기간: {fmt_period(return_row)} · 현장: {clean_str(return_row.get('현장'))}"
        )
        end_date = _date_value(return_row.get("종료일"))
        if end_date and today_kst() > end_date:
            st.warning("차량 반납기간이 하루가 지났습니다. 차량 반납을 해주세요.")

        current_form_reservation = st.session_state.get("vehicle_return_form_reservation")
        if st.button("🚘 차량 반납", type="primary", use_container_width=True, key=f"vehicle-return-button-{reservation_no}"):
            st.session_state["vehicle_return_form_reservation"] = reservation_no
            st.rerun()

        if current_form_reservation == reservation_no:
            st.divider()
            trip_completion_tab(cars, reservations, user_name, reservation_no=reservation_no)
        elif current_form_reservation is not None:
            # 로그인한 사용자의 현재 예약과 다른 폼 상태가 남아 있으면 새 예약에 맞춰 초기화한다.
            st.session_state["vehicle_return_form_reservation"] = None
    elif not pending_mine.empty:
        pending_row = pending_mine.sort_values(["예약일", "예약번호"], ascending=[True, True]).iloc[0]
        st.warning("차량 예약이 승인 대기 중입니다. 승인 또는 반려 결과가 나올 때까지 추가 예약할 수 없습니다.")
        st.info(f"대기 예약: {clean_str(pending_row.get('차량번호'))} · {fmt_period(pending_row)} · 상태: 승인대기")
        st.session_state["vehicle_return_form_reservation"] = None
    else:
        st.session_state["vehicle_return_form_reservation"] = None
        if user_name:
            block = check_user_reservation_block(reservations, user_name)
            if block is not None:
                st.warning(f"{user_name}님은 현재 활성 예약이 있어 추가 예약이 차단됩니다.")
                st.info(f"기존 예약: {fmt_period(block)} · {clean_str(block['차량번호'])} · {clean_str(block['현장'])} · {clean_str(block['상태'])}")
            else:
                tabs = st.tabs(["🤖 AI 예약", "📝 직접 예약"])
                with tabs[0]:
                    ai_reservation_tab(cars, reservations, user_name)
                with tabs[1]:
                    direct_reservation_tab(cars, reservations, user_name)
        else:
            st.info("예약을 신청하려면 먼저 예약자 이름을 입력하세요.")

    st.divider()
    st.markdown("### 📅 월간 예약 현황")
    today = today_kst()
    c1, c2 = st.columns(2)
    with c1:
        year = st.selectbox("연도", list(range(today.year - 1, today.year + 2)), index=1, key="user_year_v2")
    with c2:
        month = st.selectbox("월", list(range(1, 13)), index=today.month - 1, key="user_month_v2")
    render_calendar(reservations, year, month)


def main():
    inject_css()
    cars = load_cars()
    reservations = load_reservations()

    with st.sidebar:
        st.header("🚗 법인차량 관리 AI")
        page = st.radio("메뉴", ["관리자 페이지", "사용자 페이지"], key="main_page")
        st.divider()
        st.markdown("**차량 구분**")
        st.write(f"전체 {len(cars)}대")
        st.write(f"임원용 {len(get_executive_cars(cars))}대")
        st.write(f"업무용 {len(get_rent_cars(cars))}대")
        st.caption(f"Ollama: {OLLAMA_MODEL}")
        st.caption(f"예약범위: 오늘 ~ {fmt_date(today_kst() + timedelta(days=MAX_RESERVATION_DAYS))}")
        st.caption(f"운행일지: {RUN_LOG_FILE.name}")

    if page == "관리자 페이지":
        admin_dashboard(cars, reservations)
    else:
        user_page(cars, reservations)


if __name__ == "__main__":
    main()
