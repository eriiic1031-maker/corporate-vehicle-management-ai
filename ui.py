"""화면 공통 요소: 현재 사용자, 상태 배지, 시간 선택, 차트(타임라인·가동률)."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta

import altair as alt
import pandas as pd
import streamlit as st

import data
import services

# 차트 색: 카테고리 슬롯을 고정 순서로 배정(데이터 시각화 기본 팔레트). 연체는 위험 상태색.
SERIES = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#4a3aa7", "#e34948"]
CRITICAL = "#d03b3b"
FLEET_STATUS_ORDER = ["대기(운행가능)", "운행중", "오늘 예약", "정비중", "임원 배정", "연체(미반납)"]
FLEET_STATUS_COLORS = [SERIES[0], SERIES[1], SERIES[2], SERIES[3], SERIES[6], CRITICAL]
RES_STATUS_ORDER = [data.ST_PENDING, data.ST_APPROVED, data.ST_OUT, data.ST_DONE, data.ST_OVERDUE, "정비"]
RES_STATUS_COLORS = [SERIES[3], SERIES[0], SERIES[1], SERIES[5], CRITICAL, "#898781"]

BADGE_COLORS = {
    data.ST_PENDING: "orange", data.ST_APPROVED: "blue", data.ST_OUT: "violet", data.ST_OVERDUE: "red",
    data.ST_DONE: "green", data.ST_REJECTED: "gray", data.ST_CANCELED: "gray", data.ST_EXPIRED: "gray",
    data.ST_NOSHOW: "red",
}


def current_emp() -> dict:
    return st.session_state["me"]


def badge(status: str) -> str:
    return f":{BADGE_COLORS.get(status, 'gray')}-badge[{status}]"


def show_error(exc: Exception) -> None:
    if isinstance(exc, services.ServiceError):
        st.error(str(exc), icon=":material/error:")
    else:
        st.exception(exc)


def flash(msg: str, kind: str = "success") -> None:
    """rerun 뒤에도 보이도록 메시지를 남긴다."""
    st.session_state["_flash"] = (kind, msg)


def show_flash() -> None:
    item = st.session_state.pop("_flash", None)
    if item:
        kind, msg = item
        getattr(st, kind)(msg, icon=":material/check_circle:" if kind == "success" else None)


def time_options() -> list[str]:
    step = int(data.get_setting("time_interval_minutes") or 30)
    return [f"{m // 60:02d}:{m % 60:02d}" for m in range(0, 24 * 60, step)]


def combine(d: date, hhmm: str) -> datetime:
    h, m = (int(x) for x in hhmm.split(":"))
    return datetime.combine(d, time(h, m))


def period_text(start_dt: str, end_dt: str) -> str:
    s, e = data.to_dt(start_dt), data.to_dt(end_dt)
    if not s or not e:
        return f"{start_dt} ~ {end_dt}"
    wk = "월화수목금토일"
    if s.date() == e.date():
        return f"{s:%m/%d}({wk[s.weekday()]}) {s:%H:%M}~{e:%H:%M}"
    return f"{s:%m/%d}({wk[s.weekday()]}) {s:%H:%M} ~ {e:%m/%d}({wk[e.weekday()]}) {e:%H:%M}"


def km(value) -> str:
    try:
        return f"{int(value):,} km"
    except (TypeError, ValueError):
        return "-"


def won(value) -> str:
    try:
        return f"{int(value):,}원"
    except (TypeError, ValueError):
        return "-"


# ---------------------------------------------------------------------------
# 차트
# ---------------------------------------------------------------------------
def timeline_chart(rows: pd.DataFrame, start: date, end: date, *, show_names: bool, me: str | None = None,
                   maintenance: pd.DataFrame | None = None) -> alt.Chart | None:
    """차량별 타임라인(간트). show_names=False면 다른 사람 예약은 '예약됨'으로만 표시한다."""
    items = []
    for _, r in rows.iterrows():
        mine = me is not None and r["emp_no"] == me
        label = r["user_name"] if (show_names or mine) else "예약됨"
        items.append({"차량": f"{r['vehicle_no']} · {r['model']}", "시작": data.to_dt(r["start_dt"]),
                      "종료": data.to_dt(r["end_dt"]), "상태": r["status"], "표시": label,
                      "기간": period_text(r["start_dt"], r["end_dt"]),
                      "목적지": (r["destination"] if (show_names or mine) else "-") or "-"})
    if maintenance is not None:
        for _, m in maintenance.iterrows():
            items.append({"차량": f"{m['vehicle_no']} · {m['model']}",
                          "시작": datetime.combine(data.to_date(m["start_date"]), time(0, 0)),
                          "종료": datetime.combine(data.to_date(m["end_date"]), time(23, 59)),
                          "상태": "정비", "표시": m["mtype"], "기간": f"{m['start_date']} ~ {m['end_date']}", "목적지": "-"})
    if not items:
        return None
    df = pd.DataFrame(items)
    lo, hi = datetime.combine(start, time(0, 0)), datetime.combine(end, time(23, 59))
    df = df[(df["종료"] >= lo) & (df["시작"] <= hi)].copy()
    if df.empty:
        return None
    df["시작"] = df["시작"].clip(lower=lo)
    df["종료"] = df["종료"].clip(upper=hi)
    n = df["차량"].nunique()
    return (
        alt.Chart(df)
        .mark_bar(cornerRadius=4, height={"band": 0.7})
        .encode(
            x=alt.X("시작:T", title=None, scale=alt.Scale(domain=[lo, hi]),
                    axis=alt.Axis(format="%m/%d", tickCount="day", grid=True)),
            x2="종료:T",
            y=alt.Y("차량:N", title=None, sort="ascending"),
            color=alt.Color("상태:N", scale=alt.Scale(domain=RES_STATUS_ORDER, range=RES_STATUS_COLORS),
                            legend=alt.Legend(orient="top", title=None)),
            tooltip=["차량", "표시", "상태", "기간", "목적지"],
        )
        .properties(height=max(120, 26 * n))
    )


def fleet_status_chart(counts: pd.DataFrame) -> alt.LayerChart:
    """현장별 누적 막대. 모든 현장 이름을 표시하고, 막대 안에 대수, 오른쪽 끝에 합계를 적는다."""
    order = list(data.get_setting("sites"))
    sites = [s for s in order if s in set(counts["현장"])] + sorted(set(counts["현장"]) - set(order))
    totals = counts.groupby("현장", as_index=False)["대수"].sum()
    totals["라벨"] = totals["대수"].map(lambda n: f"{n}대")
    max_total = int(totals["대수"].max()) if not totals.empty else 1
    y = alt.Y("현장:N", sort=sites, title=None,
              axis=alt.Axis(labelOverlap=False, labelFontSize=14, labelFontWeight="bold", labelPadding=10,
                            ticks=False, domain=False),
              scale=alt.Scale(paddingInner=0.35))
    stacked = (
        alt.Chart(counts)
        .transform_calculate(order=f"indexof({FLEET_STATUS_ORDER}, datum['상태'])")
        .transform_stack(stack="대수", as_=["x0", "x1"], groupby=["현장"], sort=[alt.SortField("order")])
        .transform_calculate(mid="(datum.x0 + datum.x1) / 2")
    )
    bars = stacked.mark_bar(cornerRadius=4, stroke="white", strokeWidth=2).encode(
        y=y,
        x=alt.X("x0:Q", title="차량 수(대)", scale=alt.Scale(domain=[0, max_total * 1.08]),
                axis=alt.Axis(format="d", tickMinStep=1, grid=True, gridOpacity=0.4)),
        x2="x1:Q",
        color=alt.Color("상태:N", scale=alt.Scale(domain=FLEET_STATUS_ORDER, range=FLEET_STATUS_COLORS),
                        legend=alt.Legend(orient="top", title=None, labelFontSize=13, symbolType="square",
                                          symbolSize=180, columns=6)),
        tooltip=["현장", "상태", alt.Tooltip("대수:Q", title="대수")],
    )
    labels = stacked.mark_text(fontSize=13, fontWeight="bold").encode(
        y=y, x="mid:Q", text="대수:Q",
        color=alt.condition(alt.datum["상태"] == "정비중", alt.value("#0b0b0b"), alt.value("white")),
    ).transform_filter("datum['대수'] > 0")
    total = alt.Chart(totals).mark_text(align="left", dx=6, fontSize=13, color="#52514e").encode(
        y=y, x="대수:Q", text="라벨:N")
    return (bars + labels + total).properties(height=70 * len(sites) + 30)


def trend_chart(trend: pd.DataFrame) -> alt.Chart:
    sites = list(data.get_setting("sites"))
    base = alt.Chart(trend).encode(
        x=alt.X("월:O", title=None),
        y=alt.Y("가동률:Q", title="가동률", axis=alt.Axis(format="%")),
        color=alt.Color("현장:N", scale=alt.Scale(domain=sites, range=SERIES[:len(sites)]),
                        legend=alt.Legend(orient="top", title=None)),
        tooltip=["월", "현장", alt.Tooltip("가동률:Q", format=".1%")],
    )
    return (base.mark_line(strokeWidth=2) + base.mark_point(size=60, filled=True)).properties(height=260)


def utilization_bar(util: pd.DataFrame) -> alt.Chart:
    df = util.sort_values("가동률", ascending=False)
    return (
        alt.Chart(df)
        .mark_bar(cornerRadiusEnd=4, color=SERIES[0])
        .encode(
            y=alt.Y("차량번호:N", sort="-x", title=None, axis=alt.Axis(labelOverlap=False)),
            x=alt.X("가동률:Q", axis=alt.Axis(format="%"), title="가동률(근무시간 기준)"),
            tooltip=["차량번호", "차종", "현장", "예약 건수", alt.Tooltip("가동률:Q", format=".1%"),
                     "사용 시간(h)", "가용 시간(h)", "주행거리(km)"],
        )
        .properties(height=max(160, 24 * len(df)))
    )


def link_card(key: str, page, title: str, value, sub: str, icon: str) -> None:
    """카드 전체를 누르면 해당 페이지로 이동하는 지표 카드.
    카드 안에 투명한 페이지 링크를 깔아 카드 영역 전체를 덮는다(앱 안 이동이라 화면 상태가 유지된다)."""
    css_key = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in key)
    st.html(f"""<style>
    .st-key-{css_key} {{ position: relative; transition: border-color .15s, box-shadow .15s; }}
    .st-key-{css_key}:hover {{ border-color: var(--st-primary-color, #ff4b4b);
        box-shadow: 0 2px 10px rgba(0,0,0,.08); }}
    .st-key-{css_key} [data-testid="stPageLink"],
    .st-key-{css_key} [data-testid="stElementContainer"]:has([data-testid="stPageLink"]),
    .st-key-{css_key} [data-testid="stPageLink"] > div {{ position: static !important; }}
    .st-key-{css_key} [data-testid="stPageLink"] a {{ position: absolute; inset: 0; z-index: 2;
        opacity: 0; border-radius: inherit; }}
    </style>""")
    with st.container(border=True, key=css_key):
        st.metric(f"{title}", value, border=False)
        st.caption(f"{icon} {sub}  ·  눌러서 이동")
        st.page_link(page, label=title)
