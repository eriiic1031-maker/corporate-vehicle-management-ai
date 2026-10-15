"""사용자 화면: 예약하기(AI 대화/직접), 내 예약(출차·반납·변경·취소), 알림함, 예약 현황."""
from __future__ import annotations

from datetime import date, datetime, timedelta

import pandas as pd
import streamlit as st

import ai
import data
import notify
import services
import ui
from data import ST_APPROVED, ST_OUT, ST_OVERDUE, ST_PENDING


# =========================================================================
# 예약하기
# =========================================================================
def _booking_conditions(prefix: str, cond: dict) -> dict | None:
    """조건 입력 위젯. cond 값으로 초기화하고 수정된 조건을 돌려준다."""
    settings = data.get_settings()
    sites = list(settings["sites"])
    opts = ui.time_options()
    today = date.today()
    max_day = today + timedelta(days=int(settings["max_days_ahead"]))

    def _d(key, fallback):
        try:
            v = date.fromisoformat(cond.get(key)) if cond.get(key) else fallback
        except ValueError:
            v = fallback
        return min(max(v, today), max_day)

    c1, c2, c3 = st.columns([1.2, 1, 1])
    site_idx = sites.index(cond["site"]) if cond.get("site") in sites else 0
    site = c1.selectbox("현장", sites, index=site_idx, key=f"{prefix}_site")
    start_d = c2.date_input("시작일", _d("start_date", today), min_value=today, max_value=max_day, key=f"{prefix}_sd")
    end_d = c3.date_input("종료일", max(_d("end_date", start_d), start_d), min_value=start_d, max_value=max_day,
                          key=f"{prefix}_ed")
    c1, c2, c3 = st.columns([1.2, 1, 1])
    st_default = cond.get("start_time") if cond.get("start_time") in opts else settings["default_start_time"]
    et_default = cond.get("end_time") if cond.get("end_time") in opts else settings["default_end_time"]
    passengers = c1.number_input("인원", 1, 15, int(cond.get("passengers") or 1), key=f"{prefix}_pax")
    start_t = c2.selectbox("시작 시간", opts, index=opts.index(st_default), key=f"{prefix}_st")
    end_t = c3.selectbox("종료 시간", opts, index=opts.index(et_default), key=f"{prefix}_et")
    needs = st.pills("추가 조건", ai.NEEDS, selection_mode="multi",
                     default=[n for n in (cond.get("needs") or []) if n in ai.NEEDS], key=f"{prefix}_needs",
                     help=f"시간 단위 예약 · 영업일 최대 {settings['max_business_days']}일 · {max_day:%m/%d}까지")
    return {"site": site, "start_date": start_d.isoformat(), "end_date": end_d.isoformat(), "start_time": start_t,
            "end_time": end_t, "passengers": int(passengers), "needs": needs or [],
            "destination": cond.get("destination", ""), "purpose": cond.get("purpose", ""),
            "min_seats": cond.get("min_seats")}


def _recommend_and_book(me: dict, cond: dict, prefix: str) -> None:
    start_dt = ui.combine(date.fromisoformat(cond["start_date"]), cond["start_time"])
    end_dt = ui.combine(date.fromisoformat(cond["end_date"]), cond["end_time"])
    settings = data.get_settings()
    try:
        services.validate_period(start_dt, end_dt, settings)
    except services.ServiceError as exc:
        st.warning(str(exc), icon=":material/schedule:")
        return
    with data.db() as conn:
        block = services.user_block_reason(conn, me["emp_no"], start_dt, end_dt, settings)
    if block:
        st.error(block, icon=":material/block:")
        return
    pax = max(int(cond["passengers"]), int(cond.get("min_seats") or 0))
    recs = services.recommend(cond["site"], start_dt, end_dt, pax, cond["needs"])
    if cond.get("min_seats"):
        recs = [r for r in recs if services.capacity(r.vehicle) >= int(cond["min_seats"])]
    if not recs:
        st.warning(f"{cond['site']} 현장에 이 시간대 예약 가능한 업무용 차량이 없습니다. 시간이나 현장을 바꿔 보세요.",
                   icon=":material/no_crash:")
        key = (cond["site"], cond["start_date"], cond["start_time"], cond["end_date"], cond["end_time"], pax)
        logged = st.session_state.setdefault("_fail_logged", set())
        if key not in logged:
            services.log_failure(me, cond["site"], start_dt, end_dt, pax, "가용 차량 없음")
            logged.add(key)
        return

    st.markdown(f"**:material/recommend: 추천 차량** :gray[{ui.period_text(data.dt_str(start_dt), data.dt_str(end_dt))}]")
    labels = []
    for r in recs:
        v = r.vehicle
        labels.append(f"{v['vehicle_no']} · {v['model']} ({v['body_type']}, {v['fuel_type']})")
    choice = st.radio("차량 선택", range(len(recs)), format_func=lambda i: labels[i], key=f"{prefix}_car",
                      label_visibility="collapsed")
    for i, r in enumerate(recs):
        if i == choice:
            with st.container(border=True):
                st.markdown(f"**{labels[i]}**  {':blue-badge[추천 1순위]' if i == 0 else ''}")
                st.markdown(" ".join(f":green-badge[{x}]" for x in r.reasons))
                st.caption(f":material/key: {r.vehicle['key_location'] or '-'}  ·  "
                           f":material/local_parking: {r.vehicle['parking_location'] or '-'}")
    c1, c2 = st.columns(2)
    destination = c1.text_input("목적지", value=cond.get("destination") or "", key=f"{prefix}_dest", placeholder="예: 평택 공장")
    purpose = c2.text_input("사용 목적", value=cond.get("purpose") or "", key=f"{prefix}_purpose", placeholder="예: 생산회의 참석")
    auto = settings.get("auto_approve_enabled")
    if st.button("예약 신청", type="primary", icon=":material/send:", key=f"{prefix}_submit", width="stretch",
                 help="현장 담당자가 승인합니다." + (f" 시작일 전날 {settings['auto_approve_time']} 이후엔 자동 승인" if auto else "")):
        if not destination.strip():
            st.error("목적지를 입력해 주세요.")
            return
        try:
            rid, msg = services.create_reservation(
                me, recs[choice].vehicle["vehicle_no"], start_dt, end_dt, purpose=purpose or "업무용 차량 이용",
                destination=destination, passengers=int(cond["passengers"]), needs=cond["needs"])
            for k in ("ai_cond", "ai_history"):
                st.session_state.pop(k, None)
            ui.flash(msg)
            st.switch_page(st.session_state["_pages"]["my"])
        except Exception as exc:
            ui.show_error(exc)


def page_reserve():
    me = ui.current_emp()
    ui.show_flash()
    with data.db() as conn:
        overdue = conn.execute("SELECT COUNT(*) FROM reservations WHERE emp_no=? AND status=?", (me["emp_no"], ST_OVERDUE)).fetchone()[0]
    if overdue:
        st.error("반납 기한이 지난 예약이 있어 새 예약을 할 수 없습니다. '내 예약'에서 먼저 반납해 주세요.", icon=":material/warning:")
        return
    mode = st.segmented_control("예약 방식", ["AI 대화로 예약", "직접 예약"], default="AI 대화로 예약", key="reserve_mode")
    if mode == "직접 예약":
        cond = _booking_conditions("direct", st.session_state.get("direct_cond", {}))
        st.divider()
        _recommend_and_book(me, cond, "direct")
        return

    history = st.session_state.setdefault("ai_history", [])
    if not history:
        with st.chat_message("assistant"):
            st.markdown("언제, 몇 명이, 어디로 가시나요?  \n"
                        ":gray[예) 다음주 월~수 퇴근 때까지 3명 평택 공장, 짐 많음 → 이어서 “목요일까지로 바꿔줘”]")
    for msg in history[-8:]:
        with st.chat_message(msg["role"]):
            st.markdown(msg["text"])
    prompt = st.chat_input("예: 내일 오후 2명 동탄 현장", key="ai_chat")
    if prompt:
        prev = st.session_state.get("ai_cond")
        with st.spinner("요청을 해석하는 중…"):
            cond, warn, method = ai.parse_request(prompt, prev)
        st.session_state["ai_cond"] = cond
        # 수정된 조건이 위젯에 반영되도록 이전 위젯 값을 지운다.
        for k in [k for k in st.session_state if str(k).startswith("ai_w_")]:
            del st.session_state[k]
        summary = (f"{cond.get('start_date')} {cond.get('start_time')} ~ {cond.get('end_date')} {cond.get('end_time')} · "
                   f"{cond.get('passengers')}명 · 현장 {cond.get('site') or '미지정'}"
                   + (f" · {', '.join(cond['needs'])}" if cond.get("needs") else "")
                   + (f" · {cond['min_seats']}인승 이상" if cond.get("min_seats") else ""))
        history += [{"role": "user", "text": prompt},
                    {"role": "assistant", "text": f"이렇게 이해했어요: {summary}\n\n_해석 방식: {method}_"
                     + (f"\n\n{warn}" if warn else "")}]
        st.rerun()
    cond = st.session_state.get("ai_cond")
    if not cond:
        return
    with st.container(border=True):
        st.markdown("**:material/tune: 예약 조건**")
        edited = _booking_conditions("ai_w", cond)
    if st.button("대화 새로 시작", icon=":material/restart_alt:", key="ai_reset"):
        for k in ["ai_cond", "ai_history"] + [k for k in st.session_state if str(k).startswith("ai_w_")]:
            st.session_state.pop(k, None)
        st.rerun()
    _recommend_and_book(me, edited, "ai_w")


# =========================================================================
# 내 예약
# =========================================================================
@st.dialog("예약 취소")
def _cancel_dialog(rid: int):
    reason = st.text_input("취소 사유 (선택)", key=f"cancel_reason_{rid}")
    if st.button("취소하기", type="primary", width="stretch"):
        try:
            services.cancel(ui.current_emp(), rid, reason)
            ui.flash(f"예약 #{rid}을(를) 취소했습니다.")
            st.rerun()
        except Exception as exc:
            ui.show_error(exc)


@st.dialog("기간 변경")
def _change_dialog(r: dict):
    opts = ui.time_options()
    s, e = data.to_dt(r["start_dt"]), data.to_dt(r["end_dt"])
    today = date.today()
    c1, c2 = st.columns(2)
    sd = c1.date_input("시작일", max(s.date(), today), min_value=today)
    stt = c2.selectbox("시작 시간", opts, index=opts.index(s.strftime("%H:%M")) if s.strftime("%H:%M") in opts else 18)
    ed = c1.date_input("종료일", max(e.date(), sd), min_value=sd)
    ett = c2.selectbox("종료 시간", opts, index=opts.index(e.strftime("%H:%M")) if e.strftime("%H:%M") in opts else 36)
    reason = st.text_input("변경 사유")
    st.caption("승인 전 예약은 바로 바뀌고, 승인된 예약은 관리자 승인 후 반영됩니다.")
    if st.button("변경 요청", type="primary", width="stretch"):
        try:
            msg = services.request_change(ui.current_emp(), r["id"], ui.combine(sd, stt), ui.combine(ed, ett), reason)
            ui.flash(msg)
            st.rerun()
        except Exception as exc:
            ui.show_error(exc)


def _return_form(me: dict, r: dict):
    rid = r["id"]
    v = services.get_vehicle(r["vehicle_no"])
    ck = r.get("checkout_km")
    start_km = int(ck) if ck is not None and not pd.isna(ck) else int(v["odometer"] or 0)
    photo = st.file_uploader("계기판 사진 (AI 판독)", type=["jpg", "jpeg", "png"], key=f"odo_photo_{rid}")
    km_key = f"ret_km_{rid}"
    st.session_state.setdefault(km_key, int(start_km))
    if photo is not None:
        st.image(photo, width=260, alt="반납 계기판 사진")
        if st.button("사진에서 주행거리 읽기", icon=":material/document_scanner:", key=f"ocr_{rid}"):
            with st.spinner("계기판을 읽는 중…"):
                value, info = ai.read_odometer(photo.getvalue())
            if value is None:
                st.warning(f"자동 판독 실패: {info} 직접 입력해 주세요.")
            elif value < start_km or value > start_km + 3000:
                st.warning(f"판독값 {value:,} km가 출발 시 거리({start_km:,} km)와 맞지 않습니다. 사진을 확인하고 직접 입력해 주세요.")
            else:
                st.session_state[km_key] = value
                st.success(f"판독값 {value:,} km를 입력했습니다. ({info}) 계기판과 같은지 확인해 주세요.")
    with st.form(f"return_form_{rid}"):
        c1, c2 = st.columns(2)
        ret_km = c1.number_input("주행 후 계기판 거리 (km)", min_value=int(start_km), step=1, key=km_key)
        c2.text_input("주행 전 계기판 거리 (km)", f"{start_km:,}", disabled=True)
        c1, c2, c3 = st.columns(3)
        commute = c1.number_input("출·퇴근용 거리 (km)", min_value=0, step=1, value=0,
                                  help="집↔회사 이동 거리. 세법상 업무용 사용거리에 포함됩니다.")
        personal = c2.number_input("개인 용도 거리 (km)", min_value=0, step=1, value=0,
                                   help="업무와 무관한 사적 사용 거리 (업무사용비율에서 제외)")
        s_d, e_d = data.to_date(r["start_dt"]), data.to_date(r["end_dt"])
        use_date = c3.date_input("사용일자", min(max(date.today(), s_d), max(e_d, date.today())), min_value=s_d,
                                 max_value=max(e_d, date.today()))
        c1, c2 = st.columns(2)
        fuel_opts = ["가득", "3/4", "1/2", "1/4", "경고등"] if v["fuel_type"] != "전기" else ["80% 이상", "50~80%", "20~50%", "20% 미만"]
        fuel = c1.selectbox("주유량" if v["fuel_type"] != "전기" else "충전량", fuel_opts, index=1)
        parking = c2.text_input("주차 위치 (층/구역)", placeholder="예: 지하 2층 B-12")
        note = st.text_input("메모 (선택)", placeholder="예: 와이퍼 교체 필요")
        st.markdown("**:material/receipt_long: 비용 · 영수증** :gray[(선택)]")
        expenses = []
        for etype in ("유류비", "하이패스", "주차비"):
            c1, c2 = st.columns([1, 2])
            amount = c1.number_input(f"{etype} (원)", min_value=0, step=1000, key=f"exp_{etype}_{rid}")
            receipt = c2.file_uploader(f"{etype} 영수증", type=["jpg", "jpeg", "png", "pdf"], key=f"rcpt_{etype}_{rid}")
            expenses.append({"etype": etype, "amount": amount,
                             "receipt": (receipt.name, receipt.getvalue()) if receipt else None})
        damage_on = st.checkbox("차량 손상·이상이 있어요", key=f"dmg_on_{rid}")
        c1, c2 = st.columns([1, 2])
        dtype = c1.selectbox("유형", ["손상", "고장", "사고", "기타"], key=f"dmg_type_{rid}")
        ddesc = c2.text_input("내용", key=f"dmg_desc_{rid}", placeholder="예: 뒷범퍼 긁힘")
        dphotos = st.file_uploader("손상 사진", type=["jpg", "jpeg", "png"], accept_multiple_files=True, key=f"dmg_ph_{rid}")
        submitted = st.form_submit_button("반납 완료", type="primary", icon=":material/assignment_turned_in:", width="stretch")
    if submitted:
        try:
            msg = services.return_vehicle(
                me, rid, return_km=int(ret_km), commute_km=int(commute), personal_km=int(personal), use_date=use_date,
                fuel_level=fuel, parking_location=parking, note=note,
                odometer_photo=(photo.name, photo.getvalue()) if photo else None,
                damage={"itype": dtype, "description": ddesc,
                        "photos": [(p.name, p.getvalue()) for p in dphotos or []]} if damage_on else None,
                expenses=expenses)
            ui.flash(msg)
            st.rerun()
        except Exception as exc:
            ui.show_error(exc)


def _reservation_card(me: dict, r: dict):
    with st.container(border=True):
        st.markdown(f"{ui.badge(r['status'])} **{r['vehicle_no']} {r['model'] or ''}** :gray[#{r['id']}]")
        st.caption(f":material/schedule: {ui.period_text(r['start_dt'], r['end_dt'])}  ·  :material/place: {r['destination'] or '-'}"
                   f"  ·  :material/group: {r['passengers']}명")
        status = r["status"]
        now = data.now()
        if status in (ST_APPROVED, ST_OUT, ST_OVERDUE):
            contact = services.site_contact(r["site"])
            st.markdown(f":material/key: {r['key_location'] or '-'}  ·  :material/local_parking: {r['vehicle_parking'] or '확인 필요'}"
                        f"  ·  :material/local_gas_station: {r['vehicle_fuel'] or '-'}  ·  :material/call: {contact['name']} {contact.get('phone') or ''}")
        if status == ST_OVERDUE:
            st.error("반납 기한이 지났습니다. 바로 반납해 주세요.", icon=":material/alarm:")
        btns = st.container(horizontal=True)
        if status in (ST_APPROVED, ST_OUT, ST_OVERDUE):
            btns.popover("수령·반납 안내", icon=":material/info:").markdown(data.get_setting("key_pickup_guide"))
        if status in (ST_PENDING, ST_APPROVED) and data.to_dt(r["start_dt"]) > now:
            if btns.button("기간 변경", icon=":material/edit_calendar:", key=f"chg_{r['id']}"):
                _change_dialog(r)
            if btns.button("취소", icon=":material/cancel:", key=f"cxl_{r['id']}"):
                _cancel_dialog(r["id"])
        if status == ST_APPROVED:
            can_out = data.to_dt(r["start_dt"]) - timedelta(hours=1) <= now < data.to_dt(r["end_dt"])
            if btns.button("출차(키 수령)", type="primary", icon=":material/key:", key=f"out_{r['id']}",
                           disabled=not can_out, help=None if can_out else "예약 시작 1시간 전부터 누를 수 있어요."):
                try:
                    services.checkout(me, r["id"])
                    ui.flash(f"#{r['id']} 출차 처리했습니다. 안전 운전하세요!")
                    st.rerun()
                except Exception as exc:
                    ui.show_error(exc)
        if status in (ST_OUT, ST_OVERDUE):
            with st.expander("차량 반납하기", expanded=status == ST_OVERDUE, icon=":material/assignment_return:"):
                _return_form(me, r)
        crs = data.query_all("SELECT * FROM change_requests WHERE reservation_id=? AND status='대기'", (r["id"],))
        for c in crs:
            st.caption(f":material/hourglass_top: 변경 요청 대기 → {ui.period_text(c['new_start_dt'], c['new_end_dt'])}")


def page_my():
    me = ui.current_emp()
    ui.show_flash()
    df = services.my_reservations(me)
    active = df[df["status"].isin(data.ACTIVE_STATUSES)] if not df.empty else df
    st.subheader(":material/directions_car: 진행 중 · 예정")
    if active.empty:
        st.info("진행 중이거나 예정된 예약이 없습니다.", icon=":material/event_available:")
    else:
        order = {ST_OVERDUE: 0, ST_OUT: 1, ST_APPROVED: 2, ST_PENDING: 3}
        rows = sorted(active.to_dict("records"), key=lambda r: (order.get(r["status"], 9), r["start_dt"]))
        for r in rows:
            _reservation_card(me, r)
    st.subheader(":material/history: 지난 예약")
    past = df[~df["status"].isin(data.ACTIVE_STATUSES)] if not df.empty else df
    if past.empty:
        st.caption("지난 예약이 없습니다.")
    else:
        view = past.assign(기간=past.apply(lambda r: ui.period_text(r["start_dt"], r["end_dt"]), axis=1))
        st.dataframe(
            view[["id", "vehicle_no", "model", "기간", "destination", "status", "distance", "business_km",
                  "reject_reason", "cancel_reason"]],
            hide_index=True,
            column_config={"id": "예약번호", "vehicle_no": "차량", "model": "차종", "destination": "목적지",
                           "status": "상태", "distance": st.column_config.NumberColumn("주행(km)", format="%d"),
                           "business_km": st.column_config.NumberColumn("업무용(km)", format="%d"),
                           "reject_reason": "반려 사유", "cancel_reason": "취소 사유"},
            alt="지난 예약 목록",
        )


# =========================================================================
# 알림함 / 예약 현황
# =========================================================================
def _related(n: dict) -> tuple[dict | None, dict | None]:
    r = services.get_reservation(n["reservation_id"]) if n.get("reservation_id") else None
    cr = None
    if r and n["ntype"] == "변경요청":
        cr = data.query_one("SELECT * FROM change_requests WHERE reservation_id=? ORDER BY id DESC LIMIT 1", (r["id"],))
    return r, cr


def _action_state(me: dict, n: dict, r: dict | None, cr: dict | None) -> str:
    """게시판 목록의 처리 상태 표시: '처리 필요' / '처리 완료' / ''."""
    manager = r is not None and services.can_manage_site(me, r["site"])
    if n["ntype"] == "변경요청" and cr:
        return "처리 필요" if (cr["status"] == "대기" and manager) else "처리 완료"
    if n["ntype"] == "신청접수" and manager:
        return "처리 필요" if r["status"] == ST_PENDING else "처리 완료"
    if n["ntype"] in ("연체", "반납리마인드") and r:
        return "처리 필요" if r["status"] in (ST_OUT, ST_OVERDUE) else "처리 완료"
    if n["ntype"] == "출발리마인드" and r and r["emp_no"] == me["emp_no"]:
        return "처리 필요" if r["status"] == ST_APPROVED else "처리 완료"
    if n["ntype"] in ("정비만기", "손상신고") and services.is_manager(me):
        return "확인 필요"
    return ""


def _go(page_key: str, **state):
    for k, v in state.items():
        st.session_state[k] = v
    st.switch_page(st.session_state["_pages"][page_key])


@st.dialog("알림 상세", width="large")
def _notification_dialog(n: dict):
    me = ui.current_emp()
    notify.mark_read(me, n["id"])
    icon, color, _ = notify.NTYPE_STYLE.get(n["ntype"], ("notifications", "gray", ""))
    st.markdown(f":{color}[:material/{icon}:] **{n['title']}**")
    st.caption(f"{n['created_at']} · {notify.NTYPE_LABELS.get(n['ntype'], n['ntype'])}")
    r, cr = _related(n)
    if n["body"] and not r:
        st.markdown(n["body"].replace("\n", "  \n"))
    if r:
        with st.container(border=True):
            st.markdown(f"**#{r['id']} {r['vehicle_no']} {r['model'] or ''}** {ui.badge(r['status'])}")
            st.caption(f":material/person: {r['user_name']} ({r['dept'] or '-'}) · :material/schedule: "
                       f"{ui.period_text(r['start_dt'], r['end_dt'])} · :material/place: {r['destination'] or '-'}")
            if r.get("decided_by"):
                st.caption(f"처리: {r['decided_by']} {r.get('decided_at') or ''}"
                           + (f" · 반려 사유: {r['reject_reason']}" if r.get("reject_reason") else ""))
    manager = r is not None and services.can_manage_site(me, r["site"])
    try:
        if n["ntype"] == "변경요청" and cr and cr["status"] == "대기" and manager:
            st.markdown(f"요청 기간: **{ui.period_text(cr['new_start_dt'], cr['new_end_dt'])}** · 사유: {cr['reason'] or '-'}")
            c = st.container(horizontal=True)
            if c.button("변경 승인", type="primary", icon=":material/check:"):
                services.decide_change(me, cr["id"], True)
                ui.flash("기간 변경을 승인했습니다.")
                st.rerun()
            if c.button("변경 반려", icon=":material/close:"):
                services.decide_change(me, cr["id"], False, "관리자 반려")
                ui.flash("기간 변경을 반려했습니다.")
                st.rerun()
        elif r and manager and r["status"] == ST_PENDING:
            reason = st.text_input("반려 사유 (반려할 때만)", key=f"nrej_{n['id']}")
            c = st.container(horizontal=True)
            if c.button("승인", type="primary", icon=":material/check:", key=f"nap_{n['id']}"):
                services.approve(me, r["id"])
                ui.flash(f"#{r['id']} 승인했습니다.")
                st.rerun()
            if c.button("반려", icon=":material/close:", key=f"nrj_{n['id']}"):
                services.reject(me, r["id"], reason)
                ui.flash(f"#{r['id']} 반려했습니다.")
                st.rerun()
        elif r and r["status"] in (ST_OUT, ST_OVERDUE, ST_APPROVED, ST_PENDING):
            if r["emp_no"] == me["emp_no"]:
                label = {ST_OUT: "반납하러 가기", ST_OVERDUE: "반납하러 가기", ST_APPROVED: "출차·변경하러 가기",
                         ST_PENDING: "내 예약 보기"}[r["status"]]
                if st.button(label, type="primary", icon=":material/arrow_forward:"):
                    _go("my")
            elif manager and "reservations" in st.session_state["_pages"]:
                if st.button("예약 관리에서 처리", type="primary", icon=":material/arrow_forward:"):
                    _go("reservations", manual_rid=int(r["id"]))
        elif n["ntype"] == "정비만기" and services.is_manager(me):
            if st.button("정비·만기 화면으로", type="primary", icon=":material/build:"):
                _go("maintenance")
        elif n["ntype"] == "손상신고" and services.is_manager(me):
            if st.button("사고·손상 신고 보기", type="primary", icon=":material/car_crash:"):
                _go("runlog")
        elif r:
            st.caption("이미 처리가 끝난 건입니다.")
    except Exception as exc:
        ui.show_error(exc)


def page_inbox():
    me = ui.current_emp()
    ui.show_flash()
    items = notify.inbox(me, limit=300)
    unread = sum(1 for i in items if not i["read"])
    bar = st.container(horizontal=True, vertical_alignment="bottom")
    view = bar.segmented_control("보기", [f"안 읽음 {unread}", "전체"], default=f"안 읽음 {unread}" if unread else "전체",
                                 key="inbox_view", label_visibility="collapsed")
    cats = bar.pills("분류", notify.CATEGORIES, selection_mode="multi", key="inbox_cats", label_visibility="collapsed")
    if bar.button("모두 읽음", icon=":material/done_all:", disabled=not unread):
        notify.mark_all_read(me)
        st.rerun()
    if view and view.startswith("안 읽음"):
        items = [i for i in items if not i["read"]]
    if cats:
        items = [i for i in items if notify.NTYPE_STYLE.get(i["ntype"], ("", "", ""))[2] in cats]
    if not items:
        st.info("표시할 알림이 없습니다.", icon=":material/notifications_off:")
        return
    limit = st.session_state.get("inbox_limit", 30)
    for n in items[:limit]:
        r, cr = _related(n)
        state = _action_state(me, n, r, cr)
        icon, color, _ = notify.NTYPE_STYLE.get(n["ntype"], ("notifications", "gray", ""))
        with st.container(border=True, horizontal=True, vertical_alignment="center"):
            st.markdown(f"### :{color}[:material/{icon}:]", width="content")
            with st.container(gap="small"):
                new = "" if n["read"] else ":red-badge[NEW] "
                tag = {"처리 필요": " :orange-badge[처리 필요]", "확인 필요": " :orange-badge[확인 필요]",
                       "처리 완료": " :gray-badge[처리 완료]"}.get(state, "")
                st.markdown(f"{new}**{n['title']}**{tag}")
                st.caption(f"{n['created_at'][5:]} · {notify.NTYPE_LABELS.get(n['ntype'], n['ntype'])}")
            if st.button("열기", key=f"open_{n['id']}", type="primary" if state in ("처리 필요", "확인 필요") else "secondary"):
                _notification_dialog(n)
    if len(items) > limit:
        if st.button(f"더 보기 ({len(items) - limit}건 남음)", icon=":material/expand_more:", width="stretch"):
            st.session_state["inbox_limit"] = limit + 30
            st.rerun()


def page_calendar():
    me = ui.current_emp()
    settings = data.get_settings()
    sites = list(settings["sites"])
    c1, c2 = st.columns([1, 2])
    site = c1.selectbox("현장", sites, index=sites.index(me["site"]) if me["site"] in sites else 0, key="cal_site")
    today = date.today()
    rng = c2.date_input("기간", (today, today + timedelta(days=6)), key="cal_range")
    if not isinstance(rng, (list, tuple)) or len(rng) != 2:
        st.caption("시작일과 종료일을 선택해 주세요.")
        return
    start, end = rng
    show_names = services.is_manager(me) or bool(settings.get("calendar_show_names_to_users"))
    rows = services.reservations_df("v.site=? AND r.status IN (?,?,?,?,?) AND r.start_dt<=? AND r.end_dt>=? AND v.kind=?",
                                    (site, ST_PENDING, ST_APPROVED, ST_OUT, ST_OVERDUE, data.ST_DONE,
                                     f"{end} 23:59", f"{start} 00:00", data.KIND_WORK))
    maint = data.query_df("SELECT m.*, v.model FROM maintenance m JOIN vehicles v USING(vehicle_no) WHERE v.site=? AND v.kind=?"
                          " AND m.status IN ('예정','진행') AND m.start_date<=? AND m.end_date>=?",
                          (site, data.KIND_WORK, end.isoformat(), start.isoformat()))
    chart = ui.timeline_chart(rows, start, end, show_names=show_names, me=me["emp_no"], maintenance=maint)
    if not show_names:
        st.caption(":material/lock: 다른 사람 예약은 '예약됨'으로 표시")
    if chart is None:
        st.info("이 기간에는 예약이 없습니다. 모든 업무용 차량을 쓸 수 있어요.")
    else:
        st.altair_chart(chart, alt="차량별 예약 타임라인")
