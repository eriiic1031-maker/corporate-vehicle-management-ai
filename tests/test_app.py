"""화면 점검: 역할별로 모든 페이지가 오류 없이 열리는지, 주요 버튼 흐름이 동작하는지."""
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

import data
import services

APP = str(Path(__file__).resolve().parents[1] / "app.py")
ADMIN_PAGES = ["dashboard", "approvals", "reservations", "vehicles", "maintenance", "runlog", "fines", "stats", "ask",
               "settings", "employees", "audit"]
USER_PAGES = ["reserve", "my", "calendar", "inbox"]


def run_page(emp_no: str, page: str) -> AppTest:
    at = AppTest.from_file(APP, default_timeout=60)
    at.session_state["emp_no"] = emp_no
    at.session_state["_start_page"] = page
    return at.run()


def emp_no(name):
    return data.query_one("SELECT emp_no FROM employees WHERE name=?", (name,))["emp_no"]


@pytest.mark.parametrize("page", ADMIN_PAGES + USER_PAGES)
def test_admin_pages_render(fresh_db, page):
    at = run_page(emp_no("김재열"), page)
    assert not at.exception, [e.value for e in at.exception]
    assert at.title and at.title[0].value


@pytest.mark.parametrize("page", ADMIN_PAGES[:9] + USER_PAGES)
def test_site_manager_pages_render(fresh_db, page):
    at = run_page(emp_no("평택현장담당"), page)
    assert not at.exception, [e.value for e in at.exception]


@pytest.mark.parametrize("page", USER_PAGES)
def test_user_pages_render(fresh_db, page):
    at = run_page(emp_no("김민수"), page)
    assert not at.exception, [e.value for e in at.exception]


def test_user_cannot_open_admin_page(fresh_db):
    at = run_page(emp_no("김민수"), "settings")
    assert not at.exception
    assert at.title[0].value == "예약하기"   # 권한 없는 페이지는 메뉴에 없어 기본 페이지로 간다


def test_site_manager_scope(fresh_db):
    at = run_page(emp_no("평택현장담당"), "vehicles")
    df = at.dataframe[0].value
    assert set(df["site"]) == {"평택"}


def test_direct_booking_flow(fresh_db):
    """사용자가 직접 예약 → 관리자가 승인함에서 승인."""
    no = emp_no("이준호")
    with data.tx() as conn:
        conn.execute("UPDATE reservations SET status='반납완료' WHERE emp_no=?", (no,))
    at = run_page(no, "reserve")
    at.segmented_control(key="reserve_mode").set_value("직접 예약").run()
    assert not at.exception, [e.value for e in at.exception]
    at.selectbox(key="direct_site").select("평택").run()
    d = services.business_days  # noqa: just to make sure module loaded
    from datetime import date, timedelta
    day = date.today() + timedelta(days=3)
    while not services.is_business_day(day):
        day += timedelta(days=1)
    at.date_input(key="direct_sd").set_value(day).run()
    at.date_input(key="direct_ed").set_value(day).run()
    assert not at.exception, [e.value for e in at.exception]
    at.text_input(key="direct_dest").set_value("평택 공장").run()
    at.button(key="direct_submit").click().run()
    assert not at.exception, [e.value for e in at.exception]
    r = data.query_one("SELECT * FROM reservations WHERE emp_no=? AND status='승인대기'", (no,))
    assert r and r["destination"] == "평택 공장"

    admin = run_page(emp_no("평택현장담당"), "approvals")
    admin.button(key=f"ap_{r['id']}").click().run()
    assert not admin.exception, [e.value for e in admin.exception]
    assert data.query_one("SELECT status FROM reservations WHERE id=?", (r["id"],))["status"] == "승인완료"


def test_ai_chat_fallback(fresh_db, monkeypatch):
    """Ollama가 꺼져 있어도 규칙 기반으로 해석하고 대화로 수정할 수 있다."""
    import ai
    monkeypatch.setattr(ai, "call_ollama", lambda *a, **k: (None, "Ollama에 연결할 수 없습니다."))
    no = emp_no("이준호")
    with data.tx() as conn:
        conn.execute("UPDATE reservations SET status='반납완료' WHERE emp_no=?", (no,))
    at = run_page(no, "reserve")
    at.chat_input(key="ai_chat").set_value("다음주 월요일부터 수요일 퇴근할때까지 3명이서 평택 공장 가요").run()
    assert not at.exception, [e.value for e in at.exception]
    cond = at.session_state["ai_cond"]
    assert cond["passengers"] == 3 and cond["site"] == "평택" and cond["end_time"] == "18:00"
    first_end = cond["end_date"]
    at.chat_input(key="ai_chat").set_value("목요일까지로 바꿔줘").run()
    cond = at.session_state["ai_cond"]
    assert cond["start_date"] < cond["end_date"] and cond["end_date"] > first_end
    assert not at.exception, [e.value for e in at.exception]


def test_return_form_submits(fresh_db):
    """내 예약 화면의 반납 양식으로 실제 반납이 저장된다 (출퇴근/개인 거리 분리 포함)."""
    import services
    r = data.query_one("SELECT * FROM reservations WHERE status='출차' ORDER BY id LIMIT 1")
    start_km = int(r["checkout_km"] or services.get_vehicle(r["vehicle_no"])["odometer"])
    at = run_page(r["emp_no"], "my")
    assert not at.exception, [e.value for e in at.exception]
    form_start = [t for t in at.text_input if t.label.startswith("주행 전")][0]
    assert form_start.value == f"{start_km:,}"          # 소수점 없이 표시
    at.number_input(key=f"ret_km_{r['id']}").set_value(start_km + 150)
    [n for n in at.number_input if n.label.startswith("출·퇴근")][0].set_value(30)
    [n for n in at.number_input if n.label.startswith("개인 용도")][0].set_value(20)
    [t for t in at.text_input if t.label.startswith("주차 위치")][0].set_value("지하 2층 B-12")
    [b for b in at.button if b.label == "반납 완료"][0].click().run()
    assert not at.exception, [e.value for e in at.exception]
    done = services.get_reservation(r["id"])
    assert done["status"] == data.ST_DONE
    assert (done["distance"], done["commute_km"], done["personal_km"], done["business_km"]) == (150, 30, 20, 130)
    assert services.get_vehicle(r["vehicle_no"])["parking_location"] == "지하 2층 B-12"


def test_inbox_open_and_approve(fresh_db):
    """새 알림 → 게시판에서 열기 → 그 자리에서 승인 처리."""
    import notify
    mgr = data.query_one("SELECT * FROM employees WHERE name='평택현장담당'")
    n = [i for i in notify.inbox(mgr) if i["ntype"] == "신청접수"]
    assert not n  # 이관 데이터에는 신청 알림이 없다 → 새 예약을 만든다
    from datetime import date, datetime, time, timedelta
    u = data.query_one("SELECT * FROM employees WHERE name='이준호'")
    with data.tx() as conn:
        conn.execute("UPDATE reservations SET status='반납완료' WHERE emp_no=?", (u["emp_no"],))
    d = date.today() + timedelta(days=3)
    while not services.is_business_day(d):
        d += timedelta(days=1)
    vno = services.recommend("평택", datetime.combine(d, time(9)), datetime.combine(d, time(18)), 1)[0].vehicle["vehicle_no"]
    rid, _ = services.create_reservation(u, vno, datetime.combine(d, time(9)), datetime.combine(d, time(18)),
                                         purpose="회의", destination="평택 공장", passengers=1)
    at = run_page(mgr["emp_no"], "inbox")
    assert not at.exception, [e.value for e in at.exception]
    note = [i for i in notify.inbox(mgr) if i["ntype"] == "신청접수" and i["reservation_id"] == rid][0]
    assert any("처리 필요" in m.value for m in at.markdown)
    at.button(key=f"open_{note['id']}").click().run()
    assert not at.exception, [e.value for e in at.exception]
    # 팝업 안에 승인·반려 버튼이 보인다 (팝업 안 클릭은 AppTest가 재현하지 못해 브라우저로 확인)
    assert at.button(key=f"nap_{note['id']}") and at.button(key=f"nrj_{note['id']}")
    assert notify.unread_count(mgr) < len(notify.inbox(mgr))  # 연 알림은 읽음 처리
