"""관리자 화면 (총무 관리자 · 현장 담당자).

현장 담당자는 자기 현장 차량·예약만 보이고 처리할 수 있다. 설정·직원·감사로그는 총무 관리자 전용.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta

import pandas as pd
import streamlit as st

import ai
import data
import export
import notify
import services
import ui
from data import ST_APPROVED, ST_DONE, ST_OUT, ST_OVERDUE, ST_PENDING


def _scope_caption(me: dict):
    if services.is_admin(me):
        st.markdown(":blue-badge[:material/public: 전체 현장]")
    else:
        st.markdown(f":violet-badge[:material/location_on: {me['site']} 현장만]")


def _goto(key: str, label: str, icon: str):
    st.page_link(st.session_state["_pages"][key], label=label, icon=icon)


# =========================================================================
# 대시보드
# =========================================================================
def page_dashboard():
    me = ui.current_emp()
    _scope_caption(me)
    ui.show_flash()
    tasks = services.today_tasks(me)
    st.subheader("오늘 할 일")
    cols = st.columns(4)
    cards = [
        ("승인 대기", tasks["pending"] + tasks["changes"], f"변경 요청 {tasks['changes']}", "approvals", ":material/approval:"),
        ("연체", tasks["overdue"], "미반납", "reservations", ":material/alarm:"),
        ("오늘 출차", tasks["today_out"], f"운행중 {tasks['out_now']}", "reservations", ":material/directions_car:"),
        ("정비·만기", tasks["alerts"], f"손상 신고 {tasks['incidents']}", "maintenance", ":material/build:"),
    ]
    for i, (col, (title, value, sub, page, icon)) in enumerate(zip(cols, cards)):
        with col:
            ui.link_card(f"task_card_{i}", st.session_state["_pages"][page], title, value, sub, icon)

    sites = services.manageable_sites(me)
    st.subheader("오늘 차량 현황")
    counts = services.status_counts(sites)
    if counts.empty:
        st.info("차량이 없습니다.")
    else:
        totals = counts.groupby("상태")["대수"].sum()
        tiles = st.columns(len(ui.FLEET_STATUS_ORDER))
        for t, name in zip(tiles, ui.FLEET_STATUS_ORDER):
            t.metric(name, f"{int(totals.get(name, 0))}대")
        st.altair_chart(ui.fleet_status_chart(counts), alt="현장별 오늘 차량 상태 누적 막대")
        with st.expander("표로 보기", icon=":material/table:"):
            st.dataframe(services.fleet_status_today(sites), hide_index=True, alt="오늘 차량 상태 표")

    st.subheader("월별 가동률 추이", help="업무용 차량 · 예약·운행 시간 ÷ 가용 시간 (영업일 09~18시, 정비 기간 제외)")
    trend = services.monthly_trend(6, sites)
    if trend.empty:
        st.caption("데이터가 없습니다.")
    else:
        st.altair_chart(ui.trend_chart(trend), alt="현장별 월별 가동률 추이")

    overdue = services.scoped_reservations(me, "r.status=?", [ST_OVERDUE])
    if not overdue.empty:
        st.subheader("연체 차량")
        st.dataframe(overdue.assign(기간=overdue.apply(lambda r: ui.period_text(r["start_dt"], r["end_dt"]), axis=1))
                     [["id", "user_name", "dept", "vehicle_no", "model", "site", "기간"]],
                     hide_index=True, column_config={"id": "예약번호", "user_name": "사용자", "dept": "부서",
                                                     "vehicle_no": "차량", "model": "차종", "site": "현장"},
                     alt="연체 예약 목록")


# =========================================================================
# 승인함
# =========================================================================
@st.dialog("예약 반려")
def _reject_dialog(rid: int):
    reason = st.text_area("반려 사유 (사용자에게 알림으로 전달됩니다)", key=f"rej_{rid}",
                          placeholder="예: 해당 일자 임원 행사로 차량 회수 예정입니다. 본사 차량으로 다시 신청해 주세요.")
    if st.button("반려", type="primary", width="stretch"):
        try:
            services.reject(ui.current_emp(), rid, reason)
            ui.flash(f"#{rid} 반려했습니다.")
            st.rerun()
        except Exception as exc:
            ui.show_error(exc)


def page_approvals():
    me = ui.current_emp()
    _scope_caption(me)
    ui.show_flash()
    settings = data.get_settings()
    with st.container(border=True):
        c1, c2 = st.columns([2, 1], vertical_alignment="center")
        enabled = bool(settings.get("auto_approve_enabled"))
        c1.markdown(f":material/bolt: **자동 승인** {':green-badge[ON]' if enabled else ':gray-badge[OFF]'} "
                    f":gray[전날 {settings.get('auto_approve_time')} 이후]",
                    help="시작일 하루 전날 설정 시각 이후 승인대기 예약을 자동 승인합니다. 정비 일정과 겹치면 자동 승인하지 않고 알려 드립니다.")
        if services.is_admin(me):
            new = c2.toggle("자동 승인 사용", value=enabled, key="auto_toggle")
            if new != enabled:
                services.save_settings(me, {"auto_approve_enabled": new})
                if new:
                    services.run_scheduled_jobs(force=True)
                ui.flash(f"자동 승인을 {'켰' if new else '껐'}습니다.")
                st.rerun()
        else:
            c2.caption(":material/lock: 총무 관리자만 변경")

    pending = services.scoped_reservations(me, "r.status=?", [ST_PENDING])
    st.subheader(f":material/pending_actions: 승인 대기 {len(pending)}")
    if pending.empty:
        st.success("처리할 승인 요청이 없습니다.", icon=":material/task_alt:")
    for r in pending.to_dict("records"):
        with st.container(border=True):
            c1, c2 = st.columns([4, 1.4], vertical_alignment="center")
            cutoff = services.auto_approve_cutoff(data.to_dt(r["start_dt"]), settings)
            c1.markdown(f"**{r['user_name']}** :gray[{r['dept'] or ''}] · **{r['vehicle_no']}** {r['model']} :gray[#{r['id']} · {r['site']}]"
                        + (f" :blue-badge[자동승인 {cutoff:%m/%d %H:%M}]" if settings.get("auto_approve_enabled") else ""))
            c1.caption(f":material/schedule: {ui.period_text(r['start_dt'], r['end_dt'])}  ·  :material/group: {r['passengers']}명"
                       f"  ·  :material/place: {r['destination'] or '-'}  ·  {r['purpose'] or ''}")
            b = c2.container(horizontal=True)
            if b.button("승인", type="primary", key=f"ap_{r['id']}", icon=":material/check:"):
                try:
                    services.approve(me, r["id"])
                    ui.flash(f"#{r['id']} 승인했습니다.")
                    st.rerun()
                except Exception as exc:
                    ui.show_error(exc)
            if b.button("반려", key=f"rj_{r['id']}", icon=":material/close:"):
                _reject_dialog(r["id"])

    crs = services.pending_change_requests(me)
    st.subheader(f":material/edit_calendar: 기간 변경 요청 {len(crs)}")
    if crs.empty:
        st.caption("대기 중인 변경 요청이 없습니다.")
    for c in crs.to_dict("records"):
        with st.container(border=True):
            c1, c2 = st.columns([4, 1.4], vertical_alignment="center")
            c1.markdown(f"**{c['user_name']}** · **{c['vehicle_no']}** :gray[#{c['reservation_id']} · {c['site']}]")
            c1.caption(f"{ui.period_text(c['start_dt'], c['end_dt'])} :material/arrow_forward: **{ui.period_text(c['new_start_dt'], c['new_end_dt'])}**"
                       f"  ·  {c['reason'] or ''}")
            b = c2.container(horizontal=True)
            if b.button("승인", type="primary", key=f"cra_{c['id']}"):
                try:
                    services.decide_change(me, c["id"], True)
                    ui.flash("변경 요청을 승인했습니다.")
                    st.rerun()
                except Exception as exc:
                    ui.show_error(exc)
            if b.button("반려", key=f"crr_{c['id']}"):
                try:
                    services.decide_change(me, c["id"], False, "관리자 반려")
                    ui.flash("변경 요청을 반려했습니다.")
                    st.rerun()
                except Exception as exc:
                    ui.show_error(exc)

    upcoming = services.scoped_reservations(me, "r.status=? AND r.start_dt>?", [ST_APPROVED, data.now_str()])
    with st.expander(f"승인된 예정 예약 {len(upcoming)}건 (필요 시 반려)", icon=":material/event:"):
        for r in upcoming.to_dict("records"):
            c1, c2 = st.columns([5, 1], vertical_alignment="center")
            c1.markdown(f"#{r['id']} {r['user_name']} · {r['vehicle_no']} · {ui.period_text(r['start_dt'], r['end_dt'])}"
                        + (" :blue-badge[자동승인]" if r["auto_approved"] else ""))
            if c2.button("반려", key=f"rj2_{r['id']}"):
                _reject_dialog(r["id"])


# =========================================================================
# 예약 관리 (타임라인)
# =========================================================================
def page_reservations():
    me = ui.current_emp()
    _scope_caption(me)
    ui.show_flash()
    sites = services.manageable_sites(me)
    c1, c2, c3 = st.columns([1, 1.4, 2])
    site = c1.selectbox("현장", ["전체"] + sites if len(sites) > 1 else sites, key="res_site")
    today = date.today()
    rng = c2.date_input("기간", (today - timedelta(days=3), today + timedelta(days=10)), key="res_range")
    statuses = c3.multiselect("상태", list(data.ALL_STATUSES),
                              default=[ST_PENDING, ST_APPROVED, ST_OUT, ST_OVERDUE, ST_DONE], key="res_status")
    if not isinstance(rng, (list, tuple)) or len(rng) != 2:
        return
    start, end = rng
    where = f"r.start_dt<=? AND r.end_dt>=? AND r.status IN ({','.join('?' * len(statuses or ['-']))})"
    params = [f"{end} 23:59", f"{start} 00:00", *(statuses or ["-"])]
    if site != "전체":
        where += " AND v.site=?"
        params.append(site)
    rows = services.scoped_reservations(me, where, params)
    view_sites = sites if site == "전체" else [site]
    maint = data.query_df(f"SELECT m.*, v.model FROM maintenance m JOIN vehicles v USING(vehicle_no) WHERE v.site IN "
                          f"({','.join('?' * len(view_sites))}) AND m.status IN ('예정','진행') AND m.start_date<=? AND m.end_date>=?",
                          [*view_sites, end.isoformat(), start.isoformat()])
    tab1, tab2 = st.tabs(["차량별 타임라인", "목록"])
    with tab1:
        chart = ui.timeline_chart(rows, start, end, show_names=True, maintenance=maint)
        if chart is None:
            st.info("조건에 맞는 예약이 없습니다.")
        else:
            st.altair_chart(chart, alt="차량별 예약·정비 타임라인")
    with tab2:
        if rows.empty:
            st.info("조건에 맞는 예약이 없습니다.")
        else:
            view = rows.assign(기간=rows.apply(lambda r: ui.period_text(r["start_dt"], r["end_dt"]), axis=1))
            st.dataframe(view[["id", "status", "site", "vehicle_no", "model", "user_name", "dept", "기간", "destination",
                               "purpose", "passengers", "decided_by", "reject_reason"]],
                         hide_index=True, alt="예약 목록",
                         column_config={"id": "예약번호", "status": "상태", "site": "현장", "vehicle_no": "차량", "model": "차종",
                                        "user_name": "사용자", "dept": "부서", "destination": "목적지", "purpose": "목적",
                                        "passengers": "인원", "decided_by": "처리자", "reject_reason": "반려 사유"})
    st.subheader("수동 처리")
    c1, c2 = st.columns([2, 3])
    rid = c1.number_input("예약번호", min_value=0, step=1, key="manual_rid")
    if rid:
        r = services.get_reservation(int(rid))
        if not r or r["site"] not in sites:
            c2.warning("권한 범위 안에서 찾을 수 없는 예약입니다.")
        else:
            c2.markdown(f"#{r['id']} {r['user_name']} · {r['vehicle_no']} · {ui.period_text(r['start_dt'], r['end_dt'])} {ui.badge(r['status'])}")
            b = c2.container(horizontal=True)
            if r["status"] == ST_APPROVED and b.button("노쇼 처리", key="m_noshow"):
                try:
                    services.mark_noshow(me, r["id"])
                    ui.flash("노쇼로 기록했습니다.")
                    st.rerun()
                except Exception as exc:
                    ui.show_error(exc)
            if r["status"] in (ST_PENDING, ST_APPROVED) and b.button("관리자 취소", key="m_cancel"):
                try:
                    services.cancel(me, r["id"], "관리자 취소")
                    ui.flash("취소했습니다.")
                    st.rerun()
                except Exception as exc:
                    ui.show_error(exc)
            if r["status"] in (ST_OUT, ST_OVERDUE):
                c2.caption("사용자가 반납하지 못하는 경우 아래에서 대신 반납 처리할 수 있습니다.")
                with st.expander("대신 반납 처리", icon=":material/assignment_return:"):
                    import user
                    user._return_form(me, r)


# =========================================================================
# 차량
# =========================================================================
def _vehicle_form(me: dict, v: dict | None):
    is_new = v is None
    v = v or {"kind": data.KIND_WORK, "site": services.manageable_sites(me)[0], "seats": 5, "fuel_type": "가솔린",
              "body_type": "승용", "odometer": 0, "last_oil_km": 0, "last_tire_km": 0,
              "manager": data.get_setting("admin_name"), "manager_phone": data.get_setting("admin_phone")}
    sites = services.manageable_sites(me)
    key = "new" if is_new else v["vehicle_no"]

    def d(field):
        return data.to_date(v.get(field))

    with st.form(f"veh_form_{key}"):
        st.markdown("**기본 정보**")
        c = st.columns(4)
        vno = c[0].text_input("차량번호", v.get("vehicle_no", ""), disabled=not is_new)
        model = c[1].text_input("차종", v.get("model", ""))
        kind = c[2].selectbox("구분", [data.KIND_WORK, data.KIND_EXEC], index=0 if v["kind"] == data.KIND_WORK else 1,
                              format_func=lambda k: f"{k} ({services.vehicle_use_label(k)})")
        site = c[3].selectbox("현장", sites, index=sites.index(v["site"]) if v["site"] in sites else 0)
        c = st.columns(4)
        seats = c[0].number_input("승차정원", 2, 15, int(v.get("seats") or 5))
        fuel = c[1].selectbox("연료", ["가솔린", "디젤", "하이브리드", "전기", "LPG"],
                              index=["가솔린", "디젤", "하이브리드", "전기", "LPG"].index(v.get("fuel_type") or "가솔린"))
        body = c[2].selectbox("차체", ["승용", "SUV", "승합"], index=["승용", "SUV", "승합"].index(v.get("body_type") or "승용"))
        odo = c[3].number_input("누적주행거리(km)", 0, value=int(v.get("odometer") or 0), step=100)
        c = st.columns(4)
        user_ = c[0].text_input("배정 사용자(임원)", v.get("assigned_user") or "")
        user_phone = c[1].text_input("사용자 연락처", v.get("assigned_user_phone") or "")
        manager = c[2].text_input("관리자", v.get("manager") or "")
        manager_phone = c[3].text_input("관리자 연락처", v.get("manager_phone") or "")

        st.markdown("**구매 / 렌트 계약** · 구매 차량은 구매일, 렌트 차량은 계약 기간이 필요합니다")
        c = st.columns(4)
        purchase_date = c[0].date_input("구매일(구매 차량)", d("purchase_date"))
        purchase_cost = c[1].number_input("구매 비용(원)", 0, value=int(v.get("purchase_cost") or 0), step=1000000)
        rent_company = c[2].text_input("렌트사", v.get("rent_company") or "")
        rent_fee = c[3].number_input("월 렌트료(원)", 0, value=int(v.get("rent_monthly_fee") or 0), step=10000)
        c = st.columns(4)
        rent_start = c[0].date_input("계약 시작일", d("rent_start"))
        rent_end = c[1].date_input("계약 만료일", d("rent_end"))
        rent_km_limit = c[2].number_input("계약 주행한도(km)", 0, value=int(v.get("rent_km_limit") or 0), step=1000)
        rent_start_km = c[3].number_input("계약 시작 시 주행거리(km)", 0, value=int(v.get("rent_start_km") or 0), step=100)

        st.markdown("**만기 · 정비 기준**")
        c = st.columns(4)
        insurance = c[0].date_input("보험 만기", d("insurance_expiry"))
        inspection = c[1].date_input("정기검사 만기", d("inspection_due"))
        tax = c[2].date_input("자동차세 납부일", d("car_tax_due"))
        last_maint = c[3].date_input("최근 정비일", d("last_maintenance_date"))
        c = st.columns(4)
        last_oil = c[0].number_input("마지막 오일 교체 시 km", 0, value=int(v.get("last_oil_km") or 0), step=100)
        last_tire = c[1].number_input("마지막 타이어 교체 시 km", 0, value=int(v.get("last_tire_km") or 0), step=100)
        hipass = c[2].text_input("하이패스 카드번호", v.get("hipass_card") or "")
        key_loc = c[3].text_input("차키 보관 위치", v.get("key_location") or "")
        c = st.columns([1, 3])
        parking = c[0].text_input("현재 주차 위치", v.get("parking_location") or "")
        note = c[1].text_input("비고", v.get("note") or "")
        ok = st.form_submit_button("차량 추가" if is_new else "저장", type="primary", icon=":material/save:", width="stretch")
    if ok:
        values = {"vehicle_no": vno if is_new else v["vehicle_no"], "model": model, "kind": kind, "site": site,
                  "seats": int(seats), "fuel_type": fuel, "body_type": body, "odometer": int(odo), "assigned_user": user_,
                  "assigned_user_phone": user_phone, "manager": manager, "manager_phone": manager_phone,
                  "purchase_date": purchase_date, "purchase_cost": int(purchase_cost) or None, "rent_company": rent_company,
                  "rent_monthly_fee": int(rent_fee) or None, "rent_start": rent_start, "rent_end": rent_end,
                  "rent_km_limit": int(rent_km_limit) or None, "rent_start_km": int(rent_start_km) if rent_km_limit else None,
                  "insurance_expiry": insurance, "inspection_due": inspection, "car_tax_due": tax,
                  "last_maintenance_date": last_maint, "last_oil_km": int(last_oil), "last_tire_km": int(last_tire),
                  "hipass_card": hipass, "key_location": key_loc, "parking_location": parking, "note": note}
        try:
            services.save_vehicle(me, values, is_new)
            ui.flash(f"{values['vehicle_no']} 차량을 {'추가' if is_new else '저장'}했습니다.")
            st.rerun()
        except Exception as exc:
            ui.show_error(exc)


def page_vehicles():
    me = ui.current_emp()
    _scope_caption(me)
    ui.show_flash()
    sites = services.manageable_sites(me)
    df = services.list_vehicles()
    df = df[df["site"].isin(sites)]
    status = services.fleet_status_today(sites).set_index("차량번호")["상태"] if not df.empty else pd.Series(dtype=str)
    kind = st.segmented_control("구분", ["전체", "업무용(렌트)", "임원용(구매)"], default="전체", key="veh_kind")
    view = df if kind in (None, "전체") else df[df["kind"] == (data.KIND_WORK if kind.startswith("업무") else data.KIND_EXEC)]
    view = view.assign(오늘상태=view["vehicle_no"].map(status), 구분표시=view["kind"].map(services.vehicle_use_label))
    st.dataframe(view[["vehicle_no", "model", "구분표시", "site", "오늘상태", "seats", "fuel_type", "odometer",
                       "rent_end", "insurance_expiry", "key_location", "parking_location"]],
                 hide_index=True, alt="차량 목록",
                 column_config={"vehicle_no": "차량번호", "model": "차종", "구분표시": "구분", "site": "현장", "오늘상태": "오늘 상태",
                                "seats": "정원", "fuel_type": "연료",
                                "odometer": st.column_config.NumberColumn("누적주행(km)", format="localized"),
                                "rent_end": "렌트 만료", "insurance_expiry": "보험 만기", "key_location": "차키 위치",
                                "parking_location": "주차 위치"})
    tab_edit, tab_add, tab_del = st.tabs(["상세 · 수정", "차량 추가", "차량 삭제"])
    with tab_edit:
        if df.empty:
            st.info("차량이 없습니다.")
        else:
            vno = st.selectbox("차량 선택", df["vehicle_no"].tolist(),
                               format_func=lambda x: f"{x} · {df.set_index('vehicle_no').loc[x, 'model']}", key="veh_sel")
            v = services.get_vehicle(vno)
            alerts = services.vehicle_alerts(v, data.get_settings())
            for a in alerts:
                st.warning(f"[{a['level']}] {a['message']}", icon=":material/warning:")
            _vehicle_form(me, v)
            h1, h2, h3, h4 = st.tabs(["예약·운행 이력", "정비 이력", "사고·손상", "비용"])
            with h1:
                st.dataframe(data.query_df("SELECT id, user_name, dept, start_dt, end_dt, status, checkout_km, return_km, distance, "
                                           "business_km, personal_km, parking_location FROM reservations WHERE vehicle_no=? "
                                           "ORDER BY start_dt DESC", (vno,)), hide_index=True, alt="예약 운행 이력")
            with h2:
                st.dataframe(data.query_df("SELECT start_date, end_date, mtype, vendor, description, cost, odometer, status "
                                           "FROM maintenance WHERE vehicle_no=? ORDER BY start_date DESC", (vno,)),
                             hide_index=True, alt="정비 이력")
            with h3:
                st.dataframe(data.query_df("SELECT id, itype, description, occurred_at, reported_by, status FROM incidents "
                                           "WHERE vehicle_no=? ORDER BY id DESC", (vno,)), hide_index=True, alt="사고 손상 이력")
            with h4:
                st.dataframe(data.query_df("SELECT spent_on, etype, amount, emp_no, note FROM expenses WHERE vehicle_no=? "
                                           "ORDER BY spent_on DESC", (vno,)), hide_index=True, alt="비용 이력")
    with tab_add:
        _vehicle_form(me, None)
    with tab_del:
        if not services.is_admin(me):
            st.info("차량 삭제는 총무 관리자만 할 수 있습니다.")
        elif not df.empty:
            vno = st.selectbox("삭제할 차량", df["vehicle_no"].tolist(), key="veh_del")
            st.caption("삭제하면 목록에서 숨겨지고(비활성), 과거 운행기록은 그대로 남습니다. 진행 중·예정 예약이 있으면 삭제할 수 없습니다.")
            confirm = st.checkbox("삭제할 차량이 맞습니다", key="veh_del_ok")
            if st.button("차량 삭제", disabled=not confirm, icon=":material/delete:"):
                try:
                    services.delete_vehicle(me, vno)
                    ui.flash(f"{vno} 차량을 삭제했습니다.")
                    st.rerun()
                except Exception as exc:
                    ui.show_error(exc)


# =========================================================================
# 정비 · 만기
# =========================================================================
def page_maintenance():
    me = ui.current_emp()
    _scope_caption(me)
    ui.show_flash()
    sites = services.manageable_sites(me)
    s = data.get_settings()
    st.subheader("자동 판정 알림")
    st.caption(f"오일 {s['oil_interval_km']:,} km · 타이어 {s['tire_interval_km']:,} km · 임원 차량 {s['replace_years']}년 교체 기준. "
               f"기준의 {int(float(s['maint_warn_ratio']) * 100)}%에 도달하거나 만기 {s['expiry_warn_days']}일 전부터 알립니다.")
    alerts = services.all_vehicle_alerts(me)
    if alerts.empty:
        st.success("처리할 알림이 없습니다.")
    else:
        lv = st.pills("수준", sorted(alerts["수준"].unique()), selection_mode="multi", key="alert_lv")
        st.dataframe(alerts if not lv else alerts[alerts["수준"].isin(lv)], hide_index=True, alt="정비 만기 알림")

    st.subheader("정비 일정 (예정 · 진행)")
    sched = services.maintenance_df(me)
    if sched.empty:
        st.caption("등록된 정비 일정이 없습니다.")
    else:
        today = date.today().isoformat()
        st.dataframe(sched.assign(지연=sched["end_date"] < today)[["id", "vehicle_no", "model", "site", "start_date", "end_date",
                                                                   "mtype", "vendor", "description", "status", "지연"]],
                     hide_index=True, alt="정비 일정",
                     column_config={"id": "번호", "vehicle_no": "차량", "model": "차종", "site": "현장", "start_date": "시작일",
                                    "end_date": "예정 완료일", "mtype": "유형", "vendor": "업체", "description": "내용",
                                    "status": "상태", "지연": st.column_config.CheckboxColumn("완료일 지남")})
        mid = st.selectbox("처리할 정비", sched["id"].tolist(),
                           format_func=lambda i: f"#{i} {sched.set_index('id').loc[i, 'vehicle_no']} {sched.set_index('id').loc[i, 'mtype']}",
                           key="maint_sel")
        m = sched.set_index("id").loc[mid]
        t1, t2, t3 = st.tabs(["완료 처리", "기간 변경", "취소"])
        with t1:
            with st.form("maint_done"):
                c = st.columns(3)
                end_d = c[0].date_input("완료일", date.today())
                cost = c[1].number_input("비용(원)", 0, step=10000)
                v = services.get_vehicle(m["vehicle_no"])
                odo = c[2].number_input("정비 시 주행거리(km)", 0, value=int(v["odometer"] or 0), step=100)
                desc = st.text_input("정비 내역", m["description"])
                c = st.columns(2)
                oil = c[0].checkbox("엔진오일·필터 교체함")
                tire = c[1].checkbox("타이어 교체함")
                if st.form_submit_button("완료 처리", type="primary"):
                    try:
                        services.complete_maintenance(me, int(mid), end_date=end_d, cost=int(cost) or None, odometer=int(odo),
                                                      description=desc, reset_oil=oil, reset_tire=tire)
                        ui.flash("정비를 완료 처리했습니다. 차량이 다시 예약 가능해집니다.")
                        st.rerun()
                    except Exception as exc:
                        ui.show_error(exc)
        with t2:
            c = st.columns(3)
            ns = c[0].date_input("시작일", data.to_date(m["start_date"]), key="mp_s")
            ne = c[1].date_input("예정 완료일", data.to_date(m["end_date"]), key="mp_e")
            if c[2].button("기간 저장", key="mp_save"):
                try:
                    services.update_maintenance_period(me, int(mid), ns, ne)
                    ui.flash("정비 기간을 변경했습니다.")
                    st.rerun()
                except Exception as exc:
                    ui.show_error(exc)
        with t3:
            if st.button("이 정비 일정 취소", key="mp_cancel"):
                try:
                    services.cancel_maintenance(me, int(mid))
                    ui.flash("정비 일정을 취소했습니다.")
                    st.rerun()
                except Exception as exc:
                    ui.show_error(exc)

    st.subheader("정비 일정 등록")
    st.caption("정비 기간 동안은 예약이 막히고, 기간이 끝나면 다시 예약할 수 있습니다. 겹치는 예약이 있으면 알려 드립니다.")
    vdf = services.list_vehicles()
    vdf = vdf[vdf["site"].isin(sites)]
    with st.form("maint_add"):
        c = st.columns(4)
        vno = c[0].selectbox("차량", vdf["vehicle_no"].tolist(),
                             format_func=lambda x: f"{x} · {vdf.set_index('vehicle_no').loc[x, 'model']}")
        ms = c[1].date_input("시작일", date.today())
        me_ = c[2].date_input("예정 완료일", date.today() + timedelta(days=1))
        mtype = c[3].selectbox("유형", ["정기점검", "엔진오일", "타이어", "사고수리", "기타"])
        c = st.columns([1, 2, 1])
        vendor = c[0].text_input("정비업체")
        desc = c[1].text_input("내용")
        cost = c[2].number_input("예상 비용(원)", 0, step=10000)
        if st.form_submit_button("등록", type="primary"):
            try:
                affected = services.add_maintenance(me, vno, ms, me_, mtype, vendor, desc, int(cost) or None)
                msg = "정비 일정을 등록했습니다."
                if affected:
                    msg += f" 겹치는 예약 {len(affected)}건(#{', #'.join(str(a['id']) for a in affected)})이 있어 담당자에게 알렸습니다."
                ui.flash(msg, "warning" if affected else "success")
                st.rerun()
            except Exception as exc:
                ui.show_error(exc)

    with st.expander("완료된 정비 이력", icon=":material/history:"):
        st.dataframe(services.maintenance_df(me, ("완료", "취소")), hide_index=True, alt="완료된 정비 이력")


# =========================================================================
# 운행일지 · 내보내기 · 사고/비용
# =========================================================================
def page_runlog():
    me = ui.current_emp()
    _scope_caption(me)
    ui.show_flash()
    s = data.get_settings()
    sites = services.manageable_sites(me)
    c1, c2 = st.columns(2)
    start = c1.date_input("시작일", data.to_date(s["tax_period_start"]), key="rl_s")
    end = c2.date_input("종료일", min(data.to_date(s["tax_period_end"]), date.today()), key="rl_e")
    st.caption(f"기본값은 설정의 과세기간({s['tax_period_start']} ~ {s['tax_period_end']})입니다.")
    logs = services.reservations_df(
        f"r.status=? AND v.site IN ({','.join('?' * len(sites))}) AND COALESCE(r.use_date, substr(r.start_dt,1,10)) BETWEEN ? AND ?",
        [ST_DONE, *sites, start.isoformat(), end.isoformat()])
    t1, t2, t3, t4 = st.tabs(["운행기록", "운행기록부 내보내기", "사고·손상 신고", "비용"])
    with t1:
        if logs.empty:
            st.info("기간 안에 반납 완료된 운행이 없습니다.")
        else:
            tot, bus = logs["distance"].fillna(0).sum(), logs["business_km"].fillna(0).sum()
            c = st.columns(3)
            c[0].metric("총주행거리", ui.km(tot))
            c[1].metric("업무용 사용거리", ui.km(bus))
            c[2].metric("업무사용비율", f"{(bus / tot):.1%}" if tot else "-")
            st.dataframe(logs[["use_date", "id", "vehicle_no", "model", "site", "user_name", "dept", "checkout_km", "return_km",
                               "distance", "commute_km", "personal_km", "business_km", "parking_location", "fuel_level"]],
                         hide_index=True, alt="운행기록",
                         column_config={"use_date": "사용일자", "id": "예약번호", "vehicle_no": "차량", "model": "차종", "site": "현장",
                                        "user_name": "사용자", "dept": "부서", "checkout_km": "주행 전", "return_km": "주행 후",
                                        "distance": "주행", "commute_km": "출퇴근", "personal_km": "개인", "business_km": "업무용",
                                        "parking_location": "주차 위치", "fuel_level": "연료"})
    with t2:
        vdf = services.list_vehicles(active_only=False)
        vdf = vdf[vdf["site"].isin(sites)]
        kind = st.segmented_control("대상", ["업무용(렌트)", "임원용(구매)", "전체"], default="전체", key="rl_kind")
        if kind == "업무용(렌트)":
            vdf = vdf[vdf["kind"] == data.KIND_WORK]
        elif kind == "임원용(구매)":
            vdf = vdf[vdf["kind"] == data.KIND_EXEC]
        chosen = st.multiselect("차량 (비우면 위 대상 전체)", vdf["vehicle_no"].tolist(), key="rl_vehicles")
        targets = chosen or vdf["vehicle_no"].tolist()
        st.caption(f"{len(targets)}대 · 차량별 시트(국세청 업무용승용차 운행기록부 서식) + 요약 시트. "
                   "업무용 사용거리 = 출·퇴근용 + 일반 업무용, 개인 용도 거리는 제외됩니다.")
        c1, c2 = st.columns(2)
        if c1.button("엑셀 만들기", icon=":material/table_view:", key="mk_xlsx"):
            st.session_state["rl_xlsx"] = export.runlog_excel(targets, start, end)
        if c2.button("PDF 만들기", icon=":material/picture_as_pdf:", key="mk_pdf"):
            pdf = export.runlog_pdf(targets, start, end)
            if pdf is None:
                st.error("PDF용 한글 글꼴(맑은 고딕 등) 또는 reportlab이 없어 만들 수 없습니다.")
            st.session_state["rl_pdf"] = pdf
        if st.session_state.get("rl_xlsx"):
            c1.download_button("운행기록부.xlsx 받기", st.session_state["rl_xlsx"], f"운행기록부_{start}_{end}.xlsx",
                               icon=":material/download:", type="primary")
        if st.session_state.get("rl_pdf"):
            c2.download_button("운행기록부.pdf 받기", st.session_state["rl_pdf"], f"운행기록부_{start}_{end}.pdf",
                               mime="application/pdf", icon=":material/download:", type="primary")
        st.divider()
        if st.button("원본 데이터 백업(엑셀) 만들기", icon=":material/backup:", key="mk_backup"):
            st.session_state["backup_xlsx"] = export.data_backup_excel(me)
        if st.session_state.get("backup_xlsx"):
            st.download_button("데이터 백업.xlsx 받기", st.session_state["backup_xlsx"], f"법인차량_데이터_{date.today()}.xlsx",
                               icon=":material/download:")
    with t3:
        inc = services.incidents_df(me)
        if inc.empty:
            st.caption("신고된 사고·손상이 없습니다.")
        else:
            st.dataframe(inc[["id", "vehicle_no", "model", "site", "itype", "description", "user_name", "occurred_at", "status"]],
                         hide_index=True, alt="사고 손상 신고")
            c = st.columns([1, 1, 1], vertical_alignment="bottom")
            iid = c[0].selectbox("신고 번호", inc["id"].tolist(), key="inc_sel")
            new = c[1].selectbox("상태", ["접수", "처리중", "완료"], key="inc_status")
            if c[2].button("상태 변경", key="inc_save"):
                services.update_incident_status(me, int(iid), new)
                ui.flash("상태를 변경했습니다.")
                st.rerun()
            for a in services.attachments_for("incident", int(iid)):
                path = data.BASE_DIR / a["path"]
                if path.exists():
                    st.image(str(path), width=240, alt="손상 사진")
    with t4:
        exp = services.expenses_df(me, start, end)
        if exp.empty:
            st.caption("기간 안에 등록된 비용이 없습니다.")
        else:
            by = exp.groupby("etype")["amount"].sum()
            cols = st.columns(len(by) + 1)
            cols[0].metric("합계", ui.won(exp["amount"].sum()))
            for c, (k, val) in zip(cols[1:], by.items()):
                c.metric(k, ui.won(val))
            st.dataframe(exp[["spent_on", "vehicle_no", "site", "user_name", "dept", "etype", "amount", "note"]],
                         hide_index=True, alt="비용 목록",
                         column_config={"amount": st.column_config.NumberColumn("금액(원)", format="localized")})


# =========================================================================
# 과태료 운전자 조회
# =========================================================================
def page_fines():
    me = ui.current_emp()
    _scope_caption(me)
    st.caption("주정차·속도위반 고지서의 차량번호와 위반 일시를 넣으면 그 시각의 운전자를 찾습니다.")
    vdf = services.list_vehicles(active_only=False)
    vdf = vdf[vdf["site"].isin(services.manageable_sites(me))]
    c = st.columns([2, 1, 1], vertical_alignment="bottom")
    vno = c[0].selectbox("차량번호", vdf["vehicle_no"].tolist(), key="fine_vno")
    d = c[1].date_input("위반 날짜", date.today(), key="fine_d")
    t = c[2].time_input("위반 시각", time(10, 0), step=600, key="fine_t")
    when = datetime.combine(d, t)
    found = services.find_driver(vno, when)
    if not found:
        st.warning("해당 시각에 기록된 예약·운행이 없습니다. 무단 사용 가능성이 있으니 차키 대장과 주차 기록을 확인해 주세요.",
                   icon=":material/help:")
        return
    for r in found:
        with st.container(border=True):
            emp = services.get_employee(r["emp_no"]) if r.get("emp_no") else None
            st.markdown(f"### {r['user_name']}  :gray[{r.get('dept') or ''}]")
            st.markdown(f"연락처 {emp['phone'] if emp and emp['phone'] else '-'} · 예약 #{r['id'] or '-'} · {ui.badge(r['status'])} · 근거: {r['근거']}")
            if r.get("start_dt") not in (None, "-"):
                st.caption(f"예약 {ui.period_text(r['start_dt'], r['end_dt'])} · 출차 {r.get('checkout_at') or '-'} · "
                           f"반납 {r.get('return_at') or '-'} · 목적지 {r.get('destination') or '-'}")
    if len(found) > 1:
        st.info("두 건 이상이 겹칩니다. 출차·반납 시각을 확인해 주세요.")


# =========================================================================
# 통계
# =========================================================================
def page_stats():
    me = ui.current_emp()
    _scope_caption(me)
    sites = services.manageable_sites(me)
    today = date.today()
    first = today.replace(day=1)
    period = st.segmented_control("기간", ["이번 달", "지난 달", "최근 90일", "직접 선택"], default="이번 달", key="stat_period")
    if period == "지난 달":
        end = first - timedelta(days=1)
        start = end.replace(day=1)
    elif period == "최근 90일":
        start, end = today - timedelta(days=89), today
    elif period == "직접 선택":
        rng = st.date_input("기간", (first, today), key="stat_rng")
        if not isinstance(rng, (list, tuple)) or len(rng) != 2:
            return
        start, end = rng
    else:
        start, end = first, today
    util = services.utilization(start, end, sites)
    fails = services.failure_stats(start, end, sites)
    if util.empty:
        st.info("업무용 차량이 없습니다.")
        return
    avail = util["가용 시간(h)"].sum()
    rate = util["사용 시간(h)"].sum() / avail if avail else 0
    idle = util[util["가동률"] < 0.1]
    c = st.columns(4)
    c[0].metric("업무용 차량 가동률", f"{rate:.1%}")
    c[1].metric("유휴 차량 (가동률 10% 미만)", f"{len(idle)}대")
    c[2].metric("예약 실패율", f"{fails['rate']:.1%}", help="원하는 조건에 차량이 없어 예약하지 못한 비율")
    c[3].metric("예약 실패", f"{fails['failures']}건 / 신청 {fails['requests']}건")

    st.subheader("현장별 가동률")
    g = util.groupby("현장").agg(차량=("차량번호", "count"), 사용=("사용 시간(h)", "sum"), 가용=("가용 시간(h)", "sum"),
                                주행=("주행거리(km)", "sum")).reset_index()
    g["가동률"] = g["사용"] / g["가용"].where(g["가용"] > 0, 1)
    g["예약 실패"] = g["현장"].map(fails["by_site"]).fillna(0).astype(int)
    g["판단"] = g.apply(lambda r: "증차·재배치 검토" if r["가동률"] >= 0.7 or r["예약 실패"] >= 3
                       else ("감차 검토" if r["가동률"] < 0.2 and r["차량"] > 1 else "적정"), axis=1)
    st.dataframe(g, hide_index=True, alt="현장별 가동률",
                 column_config={"가동률": st.column_config.ProgressColumn("가동률", format="percent", min_value=0, max_value=1),
                                "사용": "사용 시간(h)", "가용": "가용 시간(h)", "주행": "주행거리(km)"})
    st.caption("판단 기준(참고): 가동률 70% 이상 또는 예약 실패 3건 이상 → 증차·재배치 검토 / 가동률 20% 미만 → 감차 검토")

    st.subheader("차량별 가동률")
    st.altair_chart(ui.utilization_bar(util), alt="차량별 가동률 막대")
    with st.expander(f"유휴 차량 {len(idle)}대", icon=":material/hourglass_empty:"):
        st.dataframe(idle, hide_index=True, alt="유휴 차량 목록",
                     column_config={"가동률": st.column_config.NumberColumn(format="percent")})

    st.subheader("월별 추이")
    trend = services.monthly_trend(6, sites)
    if not trend.empty:
        st.altair_chart(ui.trend_chart(trend), alt="현장별 월별 가동률 추이")

    st.subheader("부서별 사용량 · 비용 배분")
    dept = services.dept_usage(start, end, sites)
    if dept.empty:
        st.caption("기간 안에 운행 기록이 없습니다.")
    else:
        st.dataframe(dept, hide_index=True, alt="부서별 사용량")

    if not fails["rows"].empty:
        with st.expander("예약 실패 기록", icon=":material/report:"):
            st.dataframe(fails["rows"], hide_index=True, alt="예약 실패 기록")


# =========================================================================
# 관리자 AI 질의
# =========================================================================
def page_ai():
    me = ui.current_emp()
    _scope_caption(me)
    st.caption("예: “이번 달 가장 적게 쓴 차량은?”, “탕정 현장 차량이 모자라?”, “다음 달 만기 챙길 차량 알려줘” "
               "— 권한 범위 안의 집계 데이터만 근거로 답합니다.")
    history = st.session_state.setdefault("admin_ai_history", [])
    for m in history[-10:]:
        with st.chat_message(m["role"]):
            st.markdown(m["text"])
    q = st.chat_input("운영 데이터에 대해 물어보세요", key="admin_ai_q")
    if q:
        with st.spinner("데이터를 집계하고 답변을 만드는 중…"):
            ctx = services.ai_context(me)
            answer, err = ai.answer_admin_question(q, ctx)
        if err:
            answer = (f"AI에 연결하지 못했습니다 ({err}). 대신 집계 데이터를 보여 드립니다.\n\n"
                      + _fallback_answer(q, ctx))
        history += [{"role": "user", "text": q}, {"role": "assistant", "text": answer}]
        st.rerun()


def _fallback_answer(q: str, ctx: dict) -> str:
    """AI가 꺼져 있을 때 자주 묻는 질문은 규칙으로 답한다."""
    util = pd.DataFrame(ctx["이번달 차량별 가동률"])
    lines = []
    if util.empty:
        return "집계할 데이터가 없습니다."
    if any(k in q for k in ("적게", "유휴", "안 쓰", "놀")):
        low = util.nsmallest(5, "가동률")
        lines.append("**이번 달 가동률 하위 5대**")
        lines += [f"- {r['차량번호']} {r['차종']} ({r['현장']}) {r['가동률']:.1%}, 예약 {r['예약 건수']}건" for r in low.to_dict("records")]
    if any(k in q for k in ("모자", "부족", "많이", "증차")) or not lines:
        lines.append("**현장별 이번 달 가동률 · 예약 실패**")
        fails = ctx["이번달 예약 실패"]["현장별 실패"]
        for site, v in ctx["현장별 가동률"].get("이번달", {}).items():
            lines.append(f"- {site}: 차량 {v['차량수']}대, 가동률 {v['가동률']:.1%}, 예약 실패 {fails.get(site, 0)}건")
    if any(k in q for k in ("만기", "정비", "보험", "검사")):
        lines.append("**정비·만기 알림**")
        lines += [f"- {a['차량번호']} {a['항목']}: {a['내용']}" for a in ctx["정비·만기 알림"][:10]]
    return "\n".join(lines)


# =========================================================================
# 설정 · 직원 · 감사로그 (총무 관리자 전용)
# =========================================================================
def page_settings():
    me = ui.current_emp()
    ui.show_flash()
    s = data.get_settings()
    with st.form("settings_form"):
        st.markdown("**회사 · 담당자**")
        c = st.columns(4)
        company = c[0].text_input("상호명", s["company_name"])
        biz = c[1].text_input("사업자등록번호", s["business_no"])
        admin_name = c[2].text_input("총무 담당자", s["admin_name"])
        admin_phone = c[3].text_input("담당자 연락처", s["admin_phone"])
        c = st.columns(2)
        tps = c[0].date_input("과세기간 시작", data.to_date(s["tax_period_start"]))
        tpe = c[1].date_input("과세기간 종료", data.to_date(s["tax_period_end"]))
        sites_text = st.text_input("현장 목록 (쉼표로 구분)", ", ".join(s["sites"]))

        st.markdown("**예약 정책**")
        c = st.columns(4)
        max_days = c[0].number_input("최대 며칠 앞까지 예약", 1, 180, int(s["max_days_ahead"]))
        max_bdays = c[1].number_input("1회 최대 영업일", 1, 30, int(s["max_business_days"]))
        max_future = c[2].number_input("1인 미래 예약 최대 건수", 1, 20, int(s["max_future_reservations"]))
        interval = c[3].selectbox("시간 단위(분)", [30, 60], index=0 if int(s["time_interval_minutes"]) == 30 else 1)
        c = st.columns(4)
        opts = [f"{h:02d}:{m:02d}" for h in range(24) for m in (0, 30)]
        d_start = c[0].selectbox("기본 시작 시간", opts, index=opts.index(s["default_start_time"]))
        d_end = c[1].selectbox("기본 종료 시간", opts, index=opts.index(s["default_end_time"]))
        grace = c[2].number_input("노쇼 판정(시작 후 몇 시간)", 0.5, 24.0, float(s["noshow_grace_hours"]), step=0.5)
        show_names = c[3].checkbox("일반 사용자 캘린더에 이름 공개", bool(s["calendar_show_names_to_users"]))

        st.markdown("**자동 승인**")
        c = st.columns(2)
        auto = c[0].toggle("자동 승인 사용", bool(s["auto_approve_enabled"]))
        auto_time = c[1].selectbox("시작일 전날 몇 시부터 자동 승인", opts, index=opts.index(s["auto_approve_time"]))

        st.markdown("**알림 시점**")
        c = st.columns(2)
        remind_time = c[0].selectbox("출발 전날 안내 시각", opts, index=opts.index(s["remind_day_before_time"]))
        ret_hours = c[1].number_input("반납 안내 (종료 몇 시간 전)", 0.5, 24.0, float(s["return_remind_hours"]), step=0.5)

        st.markdown("**정비 · 만기 기준**")
        c = st.columns(4)
        oil = c[0].number_input("오일 교체 주기(km)", 1000, 50000, int(s["oil_interval_km"]), step=1000)
        tire = c[1].number_input("타이어 교체 주기(km)", 10000, 150000, int(s["tire_interval_km"]), step=5000)
        years = c[2].number_input("임원 차량 교체 주기(년)", 1, 15, int(s["replace_years"]))
        warn_ratio = c[3].slider("미리 알림 시점(기준의 %)", 50, 100, int(float(s["maint_warn_ratio"]) * 100))
        c = st.columns(2)
        exp_days = c[0].number_input("보험·검사·세금 만기 며칠 전 알림", 1, 120, int(s["expiry_warn_days"]))
        rent_days = c[1].number_input("렌트 계약 만료 며칠 전 알림", 1, 180, int(s["rent_expiry_warn_days"]))

        st.markdown("**안내 문구**")
        guide = st.text_area("차키 수령 안내 (승인 알림·내 예약에 표시)", s["key_pickup_guide"])

        st.markdown("**알림 채널** · 앱 알림함은 항상 사용됩니다")
        c = st.columns([1, 1, 3])
        wh_on = c[0].toggle("사내 메신저", bool(s["notify_webhook_enabled"]))
        wh_type = c[1].selectbox("종류", ["slack", "teams", "jandi"], index=["slack", "teams", "jandi"].index(s["notify_webhook_type"]),
                                 format_func={"slack": "Slack", "teams": "Teams", "jandi": "잔디"}.get)
        wh_url = c[2].text_input("Incoming Webhook URL", s["notify_webhook_url"], type="password")
        c = st.columns([1, 2, 1, 2])
        mail_on = c[0].toggle("메일", bool(s["notify_email_enabled"]))
        smtp_host = c[1].text_input("SMTP 서버", s["smtp_host"])
        smtp_port = c[2].number_input("포트", 1, 65535, int(s["smtp_port"]))
        smtp_from = c[3].text_input("보내는 주소", s["smtp_from"])
        c = st.columns(2)
        smtp_user = c[0].text_input("SMTP 계정", s["smtp_user"])
        smtp_pw = c[1].text_input("SMTP 비밀번호", s["smtp_password"], type="password")
        c = st.columns([1, 2, 2])
        kakao_on = c[0].toggle("카카오 알림톡", bool(s["notify_kakao_enabled"]))
        kakao_url = c[1].text_input("알림톡 대행사 API URL", s["kakao_api_url"])
        kakao_key = c[2].text_input("API 키", s["kakao_api_key"], type="password")
        st.caption("카카오 알림톡은 비즈메시지 대행사 계약과 템플릿 승인이 필요합니다. 대행사 API 규격에 맞게 notify.py를 조정하세요.")

        st.markdown("**AI (Ollama)**")
        c = st.columns(2)
        ollama_base = c[0].text_input("Ollama 주소", s["ollama_base"])
        ollama_model = c[1].text_input("모델", s["ollama_model"])
        saved = st.form_submit_button("설정 저장", type="primary", icon=":material/save:", width="stretch")
    if saved:
        sites = [x.strip() for x in sites_text.split(",") if x.strip()]
        if not sites:
            st.error("현장을 하나 이상 입력해 주세요.")
            return
        values = {
            "company_name": company, "business_no": biz, "admin_name": admin_name, "admin_phone": admin_phone,
            "tax_period_start": tps.isoformat(), "tax_period_end": tpe.isoformat(), "sites": sites,
            "max_days_ahead": int(max_days), "max_business_days": int(max_bdays), "max_future_reservations": int(max_future),
            "time_interval_minutes": int(interval), "default_start_time": d_start, "default_end_time": d_end,
            "noshow_grace_hours": float(grace), "calendar_show_names_to_users": bool(show_names),
            "auto_approve_enabled": bool(auto), "auto_approve_time": auto_time,
            "remind_day_before_time": remind_time, "return_remind_hours": float(ret_hours),
            "oil_interval_km": int(oil), "tire_interval_km": int(tire), "replace_years": int(years),
            "maint_warn_ratio": warn_ratio / 100, "expiry_warn_days": int(exp_days), "rent_expiry_warn_days": int(rent_days),
            "key_pickup_guide": guide, "notify_webhook_enabled": bool(wh_on), "notify_webhook_type": wh_type,
            "notify_webhook_url": wh_url, "notify_email_enabled": bool(mail_on), "smtp_host": smtp_host,
            "smtp_port": int(smtp_port), "smtp_from": smtp_from, "smtp_user": smtp_user, "smtp_password": smtp_pw,
            "notify_kakao_enabled": bool(kakao_on), "kakao_api_url": kakao_url, "kakao_api_key": kakao_key,
            "ollama_base": ollama_base, "ollama_model": ollama_model,
        }
        if tpe < tps:
            st.error("과세기간 종료일이 시작일보다 빠릅니다.")
            return
        try:
            services.save_settings(me, values)
            services.run_scheduled_jobs(force=True)
            ui.flash("설정을 저장했습니다.")
            st.rerun()
        except Exception as exc:
            ui.show_error(exc)

    c = st.columns(2)
    if c[0].button("알림 채널 테스트 발송", icon=":material/send:"):
        for line in notify.send_test(data.get_settings()):
            st.write(line)
    with c[1].popover("데이터 초기화 (프로토타입)", icon=":material/restart_alt:"):
        st.warning("DB를 지우고 차량목록·예약목록 엑셀에서 다시 가져옵니다. 앱에서 입력한 내용은 모두 사라집니다.")
        ok = st.text_input("확인을 위해 '초기화'를 입력하세요", key="reset_confirm")
        if st.button("초기화 실행", disabled=ok != "초기화", type="primary"):
            result = data.reset_database()
            services.run_scheduled_jobs(force=True)
            ui.flash(f"초기화했습니다. 차량 {result['vehicles']}대, 예약 {result['reservations']}건")
            st.rerun()


def page_employees():
    me = ui.current_emp()
    ui.show_flash()
    st.caption("프로토타입에서는 로그인 대신 사이드바에서 사용자를 고릅니다. 실제 운영 시 사번은 사내 계정(SSO)과 연결합니다.")
    sites = list(data.get_setting("sites"))
    emps = pd.DataFrame(services.list_employees(active_only=False))
    edited = st.data_editor(
        emps, hide_index=True, num_rows="dynamic", key="emp_editor",
        column_config={
            "emp_no": st.column_config.TextColumn("사번", required=True),
            "name": st.column_config.TextColumn("이름", required=True),
            "dept": "부서",
            "site": st.column_config.SelectboxColumn("소속 현장", options=sites),
            "role": st.column_config.SelectboxColumn("권한", options=list(data.ROLE_LABELS), required=True,
                                                     help="user=일반 사용자, site_manager=현장 담당자, admin=총무 관리자"),
            "phone": "연락처", "email": "이메일",
            "active": st.column_config.CheckboxColumn("재직"),
        },
    )
    st.caption("권한: user = 일반 사용자 · site_manager = 현장 담당자(소속 현장만 승인·관리) · admin = 총무 관리자")
    if st.button("변경 저장", type="primary", icon=":material/save:"):
        before = emps.set_index("emp_no").to_dict("index")
        count = 0
        try:
            for row in edited.to_dict("records"):
                if not row.get("emp_no"):
                    continue
                row["active"] = 1 if row.get("active") in (1, True, None) or pd.isna(row.get("active")) else 0
                row = {k: ("" if (v is None or (isinstance(v, float) and pd.isna(v))) else v) for k, v in row.items()}
                old = before.get(row["emp_no"])
                if old is None:
                    services.save_employee(me, row, is_new=True)
                    count += 1
                elif any(str(old.get(k, "")) != str(row.get(k, "")) for k in ("name", "dept", "site", "role", "phone", "email", "active")):
                    services.save_employee(me, row, is_new=False)
                    count += 1
            ui.flash(f"{count}명 저장했습니다.")
            st.rerun()
        except Exception as exc:
            ui.show_error(exc)


def page_audit():
    st.caption("누가 언제 무엇을 바꿨는지 기록합니다. (승인·반려·취소·반납·차량/설정 변경 등)")
    c = st.columns(2)
    q = c[0].text_input("검색 (사용자·작업·대상)", key="audit_q")
    limit = c[1].selectbox("표시 건수", [200, 500, 2000], key="audit_limit")
    df = data.query_df("SELECT ts, actor, action, entity, entity_id, detail FROM audit_log ORDER BY id DESC LIMIT ?", (limit,))
    if q:
        mask = df.apply(lambda r: q in " ".join(str(x) for x in r.values), axis=1)
        df = df[mask]
    st.dataframe(df, hide_index=True, alt="감사 로그",
                 column_config={"ts": "일시", "actor": "사용자", "action": "작업", "entity": "대상", "entity_id": "대상 ID",
                                "detail": "상세"})
