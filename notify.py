"""알림: 앱 내 알림함 + 외부 채널(사내 메신저 웹훅, 메일, 카카오 알림톡).

알림은 먼저 DB에 쌓고(트랜잭션 안), 외부 채널 발송은 트랜잭션이 끝난 뒤
dispatch_pending()에서 한다. 발송 실패는 send_log에 남기고 앱 동작은 막지 않는다.
"""
from __future__ import annotations

import smtplib
import sqlite3
from email.mime.text import MIMEText

import requests

import data

NTYPE_LABELS = {
    "신청접수": "신청 접수",
    "승인": "승인",
    "자동승인": "자동 승인",
    "반려": "반려",
    "취소": "취소",
    "만료": "자동 만료",
    "변경요청": "기간 변경 요청",
    "변경결과": "기간 변경 결과",
    "출발리마인드": "출발 전날 안내",
    "반납리마인드": "반납 안내",
    "연체": "연체",
    "노쇼": "노쇼",
    "정비만기": "정비·만기 알림",
    "손상신고": "손상·이상 신고",
}


def notify(conn: sqlite3.Connection, ntype: str, title: str, body: str = "", *,
           emp_no: str | None = None, role: str | None = None, site: str | None = None,
           reservation_id: int | None = None, vehicle_no: str | None = None,
           dedupe_key: str | None = None) -> int | None:
    """알림 1건 등록. dedupe_key가 같으면 다시 만들지 않는다(자동 작업의 중복 방지)."""
    cur = conn.execute(
        """INSERT OR IGNORE INTO notifications(created_at, emp_no, target_role, target_site, ntype, title, body,
           reservation_id, vehicle_no, dedupe_key) VALUES(?,?,?,?,?,?,?,?,?,?)""",
        (data.now_str(), emp_no, role, site, ntype, title, body, reservation_id, vehicle_no, dedupe_key),
    )
    return cur.lastrowid if cur.rowcount else None


def notify_managers(conn, ntype, title, body="", *, site=None, reservation_id=None, vehicle_no=None, dedupe_key=None):
    """총무 관리자 + 해당 현장 담당자에게 알림."""
    notify(conn, ntype, title, body, role=data.ROLE_ADMIN, reservation_id=reservation_id,
           vehicle_no=vehicle_no, dedupe_key=f"{dedupe_key}:admin" if dedupe_key else None)
    if site:
        notify(conn, ntype, title, body, role=data.ROLE_SITE, site=site, reservation_id=reservation_id,
               vehicle_no=vehicle_no, dedupe_key=f"{dedupe_key}:site" if dedupe_key else None)


# ---------------------------------------------------------------------------
# 알림함 조회
# ---------------------------------------------------------------------------
def _inbox_where(emp: dict) -> tuple[str, list]:
    clauses = ["n.emp_no = ?"]
    params: list = [emp["emp_no"]]
    if emp["role"] == data.ROLE_ADMIN:
        clauses.append("n.target_role = 'admin'")
    if emp["role"] == data.ROLE_SITE:
        clauses.append("(n.target_role = 'site_manager' AND n.target_site = ?)")
        params.append(emp["site"])
    return "(" + " OR ".join(clauses) + ")", params


def inbox(emp: dict, limit: int = 200) -> list[dict]:
    where, params = _inbox_where(emp)
    return data.query_all(
        f"""SELECT n.*, (r.emp_no IS NOT NULL) AS read
            FROM notifications n
            LEFT JOIN notification_reads r ON r.notification_id = n.id AND r.emp_no = ?
            WHERE {where} ORDER BY n.id DESC LIMIT ?""",
        [emp["emp_no"], *params, limit],
    )


def unread_count(emp: dict) -> int:
    where, params = _inbox_where(emp)
    row = data.query_one(
        f"""SELECT COUNT(*) AS n FROM notifications n
            LEFT JOIN notification_reads r ON r.notification_id = n.id AND r.emp_no = ?
            WHERE {where} AND r.emp_no IS NULL""",
        [emp["emp_no"], *params],
    )
    return int(row["n"]) if row else 0


def mark_all_read(emp: dict) -> None:
    where, params = _inbox_where(emp)
    with data.tx() as conn:
        conn.execute(
            f"""INSERT OR IGNORE INTO notification_reads(notification_id, emp_no)
                SELECT n.id, ? FROM notifications n WHERE {where}""",
            [emp["emp_no"], *params],
        )


# ---------------------------------------------------------------------------
# 외부 채널 발송
# ---------------------------------------------------------------------------
def _recipients(conn, n: dict) -> list[dict]:
    if n["emp_no"]:
        rows = conn.execute("SELECT * FROM employees WHERE emp_no=?", (n["emp_no"],)).fetchall()
    elif n["target_role"] == data.ROLE_SITE:
        rows = conn.execute("SELECT * FROM employees WHERE role=? AND site=? AND active=1",
                            (data.ROLE_SITE, n["target_site"])).fetchall()
    else:
        rows = conn.execute("SELECT * FROM employees WHERE role=? AND active=1", (data.ROLE_ADMIN,)).fetchall()
    return [dict(r) for r in rows]


def _send_webhook(settings: dict, n: dict, names: str) -> str:
    url = settings.get("notify_webhook_url")
    if not url:
        return "웹훅: URL 미설정"
    kind = settings.get("notify_webhook_type", "slack")
    text = f"[법인차량] {n['title']}\n{n['body']}\n수신: {names}"
    headers = {}
    if kind == "jandi":
        payload = {"body": f"[법인차량] {n['title']}", "connectInfo": [{"title": f"수신: {names}", "description": n["body"]}]}
        headers = {"Accept": "application/vnd.tosslab.jandi-v2+json", "Content-Type": "application/json"}
    else:  # slack / teams(incoming webhook) 모두 text 필드를 받는다.
        payload = {"text": text}
    resp = requests.post(url, json=payload, headers=headers, timeout=5)
    return f"웹훅({kind}): HTTP {resp.status_code}"


def _send_email(settings: dict, n: dict, recipients: list[dict]) -> str:
    to = [r["email"] for r in recipients if r.get("email")]
    if not to:
        return "메일: 수신자 이메일 없음"
    if not settings.get("smtp_host"):
        return "메일: SMTP 미설정"
    msg = MIMEText(n["body"] or n["title"], _charset="utf-8")
    msg["Subject"] = f"[법인차량] {n['title']}"
    msg["From"] = settings.get("smtp_from") or settings.get("smtp_user")
    msg["To"] = ", ".join(to)
    with smtplib.SMTP(settings["smtp_host"], int(settings.get("smtp_port") or 587), timeout=10) as s:
        s.starttls()
        if settings.get("smtp_user"):
            s.login(settings["smtp_user"], settings.get("smtp_password", ""))
        s.sendmail(msg["From"], to, msg.as_string())
    return f"메일: {len(to)}명 발송"


def _send_kakao(settings: dict, n: dict, recipients: list[dict]) -> str:
    # 카카오 알림톡은 공식 딜러사(비즈메시지 대행사) API 계약이 필요하다.
    # 대행사 API 주소/키를 설정하면 아래 형식으로 전달한다(대행사 규격에 맞게 조정 필요).
    url = settings.get("kakao_api_url")
    if not url:
        return "카카오: 대행사 API 미설정"
    phones = [r["phone"] for r in recipients if r.get("phone")]
    if not phones:
        return "카카오: 수신자 연락처 없음"
    resp = requests.post(url, json={"to": phones, "title": n["title"], "message": n["body"]},
                         headers={"Authorization": f"Bearer {settings.get('kakao_api_key', '')}"}, timeout=5)
    return f"카카오: HTTP {resp.status_code}"


def dispatch_pending(limit: int = 50) -> int:
    """아직 외부 채널로 보내지 않은 알림을 발송한다. 채널이 모두 꺼져 있으면 처리 표시만 한다."""
    settings = data.get_settings()
    channels = []
    if settings.get("notify_webhook_enabled"):
        channels.append("webhook")
    if settings.get("notify_email_enabled"):
        channels.append("email")
    if settings.get("notify_kakao_enabled"):
        channels.append("kakao")

    with data.db() as conn:
        pending = [dict(r) for r in conn.execute(
            "SELECT * FROM notifications WHERE dispatched=0 ORDER BY id LIMIT ?", (limit,)).fetchall()]
        results = {}
        for n in pending:
            logs = []
            if channels:
                recipients = _recipients(conn, n)
                names = ", ".join(r["name"] for r in recipients) or "-"
                for ch in channels:
                    try:
                        if ch == "webhook":
                            logs.append(_send_webhook(settings, n, names))
                        elif ch == "email":
                            logs.append(_send_email(settings, n, recipients))
                        elif ch == "kakao":
                            logs.append(_send_kakao(settings, n, recipients))
                    except Exception as exc:  # 발송 실패가 업무를 막으면 안 된다
                        logs.append(f"{ch}: 실패 {exc}")
            else:
                logs.append("외부 채널 꺼짐(앱 알림함만)")
            results[n["id"]] = " / ".join(logs)
    if results:
        with data.tx() as conn:
            conn.executemany("UPDATE notifications SET dispatched=1, send_log=? WHERE id=?",
                             [(log, nid) for nid, log in results.items()])
    return len(results)


def send_test(settings: dict) -> list[str]:
    """설정 화면의 '테스트 발송' 버튼용."""
    n = {"title": "테스트 알림", "body": "법인차량 관리 시스템 알림 채널 테스트입니다.", "emp_no": None,
         "target_role": data.ROLE_ADMIN, "target_site": None}
    out = []
    with data.db() as conn:
        recipients = _recipients(conn, n)
    for name, fn in (("웹훅", lambda: _send_webhook(settings, n, "관리자")),
                     ("메일", lambda: _send_email(settings, n, recipients)),
                     ("카카오", lambda: _send_kakao(settings, n, recipients))):
        try:
            out.append(fn())
        except Exception as exc:
            out.append(f"{name}: 실패 {exc}")
    return out


def mark_read(emp: dict, notification_id: int) -> None:
    with data.tx() as conn:
        conn.execute("INSERT OR IGNORE INTO notification_reads(notification_id, emp_no) VALUES(?, ?)",
                     (int(notification_id), emp["emp_no"]))


# 게시판 표시용: 유형별 아이콘·색·분류
NTYPE_STYLE = {
    "신청접수": ("post_add", "blue", "예약"), "승인": ("check_circle", "green", "예약"),
    "자동승인": ("check_circle", "green", "예약"), "반려": ("cancel", "red", "예약"),
    "취소": ("event_busy", "gray", "예약"), "만료": ("timer_off", "gray", "예약"),
    "변경요청": ("edit_calendar", "orange", "예약"), "변경결과": ("event_available", "blue", "예약"),
    "출발리마인드": ("schedule", "blue", "안내"), "반납리마인드": ("assignment_return", "orange", "안내"),
    "연체": ("alarm", "red", "연체·노쇼"), "노쇼": ("person_off", "red", "연체·노쇼"),
    "정비만기": ("build", "orange", "정비·신고"), "손상신고": ("car_crash", "red", "정비·신고"),
}
CATEGORIES = ["예약", "안내", "연체·노쇼", "정비·신고"]
