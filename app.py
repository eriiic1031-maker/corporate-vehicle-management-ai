"""법인차량 관리 AI — 진입점.

실행:  streamlit run app.py
프로토타입이라 로그인 대신 사이드바에서 사용자(사번)를 고르며, 고른 사람의 권한에 따라
메뉴와 처리 가능한 범위가 달라진다. (일반 사용자 / 현장 담당자 / 총무 관리자)
"""
import streamlit as st

import admin
import data
import notify
import services
import user
from data import ROLE_LABELS

st.set_page_config(page_title="법인차량 관리 AI", page_icon=":material/directions_car:", layout="wide")

data.ensure_db()
services.run_scheduled_jobs()

# --- 현재 사용자 (로그인 대체) ---------------------------------------------------
employees = services.list_employees()
by_no = {e["emp_no"]: e for e in employees}
# 주소창(?emp_no=)에 사용자가 있으면 그 사람으로 시작한다 (새로고침·링크 이동에도 유지)
if st.query_params.get("emp_no") in by_no and "emp_no" not in st.session_state:
    st.session_state["emp_no"] = st.query_params["emp_no"]
if st.session_state.get("emp_no") not in by_no:
    admins = [e for e in employees if e["role"] == data.ROLE_ADMIN]
    st.session_state["emp_no"] = (admins or employees)[0]["emp_no"]
me = by_no[st.session_state["emp_no"]]
st.session_state["me"] = me


# --- 메뉴 (권한별) --------------------------------------------------------------
def _page(fn, *, title, icon, url_path):
    # 시작 페이지 지정(자동 테스트·바로가기용). 지정이 없으면 메뉴의 첫 페이지가 기본.
    return st.Page(fn, title=title, icon=icon, url_path=url_path,
                   default=st.session_state.get("_start_page") == url_path)


pages = {
    "reserve": _page(user.page_reserve, title="예약하기", icon=":material/add_circle:", url_path="reserve"),
    "my": _page(user.page_my, title="내 예약", icon=":material/event_note:", url_path="my"),
    "calendar": _page(user.page_calendar, title="예약 현황", icon=":material/calendar_month:", url_path="calendar"),
    "inbox": _page(user.page_inbox, title="알림 게시판", icon=":material/notifications:", url_path="inbox"),
}
nav = {"내 업무": [pages["reserve"], pages["my"], pages["calendar"], pages["inbox"]]}

if services.is_manager(me):
    pages.update({
        "dashboard": _page(admin.page_dashboard, title="대시보드", icon=":material/dashboard:", url_path="dashboard"),
        "approvals": _page(admin.page_approvals, title="승인함", icon=":material/approval:", url_path="approvals"),
        "reservations": _page(admin.page_reservations, title="예약 관리", icon=":material/view_timeline:", url_path="reservations"),
        "vehicles": _page(admin.page_vehicles, title="차량", icon=":material/directions_car:", url_path="vehicles"),
        "maintenance": _page(admin.page_maintenance, title="정비 · 만기", icon=":material/build:", url_path="maintenance"),
        "runlog": _page(admin.page_runlog, title="운행일지 · 내보내기", icon=":material/description:", url_path="runlog"),
        "fines": _page(admin.page_fines, title="과태료 운전자 조회", icon=":material/gavel:", url_path="fines"),
        "stats": _page(admin.page_stats, title="통계", icon=":material/insights:", url_path="stats"),
        "ai": _page(admin.page_ai, title="AI 질의", icon=":material/smart_toy:", url_path="ask"),
    })
    nav = {"관리": [pages[k] for k in ("dashboard", "approvals", "reservations", "vehicles", "maintenance",
                                     "runlog", "fines", "stats", "ai")], **nav}
if services.is_admin(me):
    pages.update({
        "settings": _page(admin.page_settings, title="설정", icon=":material/settings:", url_path="settings"),
        "employees": _page(admin.page_employees, title="직원 · 권한", icon=":material/group:", url_path="employees"),
        "audit": _page(admin.page_audit, title="감사 로그", icon=":material/history:", url_path="audit"),
    })
    nav["시스템"] = [pages["settings"], pages["employees"], pages["audit"]]

st.session_state["_pages"] = pages
# 기본 메뉴는 숨기고 사이드바에 직접 그린다 → 사용자·알림 영역을 맨 위에 둘 수 있다.
page = st.navigation(nav, position="hidden")
st.query_params["emp_no"] = me["emp_no"]


@st.fragment(run_every=int(data.get_setting("live_refresh_seconds") or 30))
def _sidebar_status():
    """화면 전체를 다시 그리지 않고, 자동 작업과 알림 수만 주기적으로 갱신한다."""
    services.run_scheduled_jobs()
    unread = notify.unread_count(st.session_state["me"])
    st.page_link(st.session_state["_pages"]["inbox"],
                 label=f"새 알림 {unread}건" if unread else "새 알림 없음",
                 icon=":material/notifications_active:" if unread else ":material/notifications:")


with st.sidebar:
    home = pages["dashboard"] if services.is_manager(me) else pages["reserve"]
    st.page_link(home, label="**법인차량 관리 AI**", icon=":material/directions_car:")
    st.selectbox(
        "사용자 (로그인 대체)",
        list(by_no),
        format_func=lambda no: f"{by_no[no]['name']} · {ROLE_LABELS[by_no[no]['role']]}"
        + (f"({by_no[no]['site']})" if by_no[no]["role"] == data.ROLE_SITE else "") + f" · {no}",
        key="emp_no",
    )
    settings = data.get_settings()
    st.caption(f"{me['dept']} · {me['site']}"
               + (f"  ·  자동 승인 {'ON' if settings['auto_approve_enabled'] else 'OFF'}" if services.is_manager(me) else ""))
    _sidebar_status()
    st.divider()
    for section, items in nav.items():
        st.caption(section)
        for p in items:
            st.page_link(p, label=p.title, icon=p.icon)

st.title(page.title)
page.run()
