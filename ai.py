"""Ollama 연동: 자연어 예약 해석(대화형 수정), 계기판 사진 판독, 관리자 질의.

AI 결과는 항상 '제안'이다. 날짜·시간·정원·충돌은 services.py가 다시 검증한다.
Ollama가 꺼져 있으면 규칙 기반 해석으로 대체한다.
"""
from __future__ import annotations

import base64
import json
import re
from datetime import date, timedelta

import requests

import data

WEEKDAYS = ["월", "화", "수", "목", "금", "토", "일"]
NEEDS = ["짐 많음", "장거리", "전기차 제외", "전기차 선호"]


def _settings():
    s = data.get_settings()
    return s["ollama_base"].rstrip("/"), s["ollama_model"]


def _extract_json(text: str):
    if not text:
        return None
    try:
        return json.loads(text)
    except Exception:
        m = re.search(r"\{.*\}", text, flags=re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:
                return None
    return None


def call_ollama(prompt: str, images: list[bytes] | None = None, as_json: bool = True, timeout: int = 120):
    base, model = _settings()
    # num_ctx를 줄여 메모리 사용량을 낮춘다 (모델 기본값 128K는 PC 메모리를 크게 차지)
    payload = {"model": model, "prompt": prompt, "stream": False, "options": {"temperature": 0.1, "num_ctx": 4096}}
    if as_json:
        payload["format"] = "json"
    if images:
        payload["images"] = [base64.b64encode(b).decode() for b in images]
    try:
        resp = requests.post(f"{base}/api/generate", json=payload, timeout=timeout)
        if resp.status_code != 200:
            try:
                detail = resp.json().get("error", "")
            except ValueError:
                detail = resp.text
            if any(k in detail for k in ("out-of-memory", "failed to allocate", "out of memory")):
                return None, ("Ollama가 모델을 불러올 메모리가 부족합니다. 다른 프로그램을 닫거나 "
                              "Windows 가상 메모리(페이지 파일)를 늘린 뒤 다시 시도해 주세요.")
            if "not found" in detail:
                return None, f"Ollama에 '{model}' 모델이 없습니다. (ollama pull {model})"
            return None, f"Ollama 오류(HTTP {resp.status_code}): {detail[:200]}"
        text = resp.json().get("response", "")
        if not as_json:
            return text, None
        parsed = _extract_json(text)
        return (parsed, None) if parsed is not None else (None, "AI가 올바른 JSON을 반환하지 않았습니다.")
    except requests.exceptions.ConnectionError:
        return None, "Ollama에 연결할 수 없습니다. (Ollama 실행 여부 확인)"
    except requests.exceptions.Timeout:
        return None, "Ollama 응답 시간이 초과되었습니다."
    except Exception as exc:
        return None, f"Ollama 오류: {exc}"


# ---------------------------------------------------------------------------
# 규칙 기반 보정 (AI가 틀리기 쉬운 상대 날짜·시간을 Python이 확정)
# ---------------------------------------------------------------------------
def _wd(name: str):
    return WEEKDAYS.index(name[0]) if name and name[0] in WEEKDAYS else None


def _resolve_dates(text: str, today: date):
    t = text.replace(" ", "")
    days = re.findall(r"([월화수목금토일])요일", t)
    base_monday = today - timedelta(days=today.weekday())
    if "다다음주" in t:
        base_monday += timedelta(days=14)
    elif "다음주" in t or "담주" in t:
        base_monday += timedelta(days=7)
    if days:
        s_wd, e_wd = _wd(days[0]), _wd(days[-1])
        start = base_monday + timedelta(days=s_wd)
        if "다음주" not in t and "이번주" not in t and start < today:
            start += timedelta(days=7)
        end = start + timedelta(days=(e_wd - s_wd) % 7) if len(days) > 1 else start
        return start, end
    if "오늘" in t:
        return today, today
    if "모레" in t:
        return today + timedelta(days=2), today + timedelta(days=2)
    if "내일" in t:
        return today + timedelta(days=1), today + timedelta(days=1)
    md = re.findall(r"(\d{1,2})월(\d{1,2})일", t)
    if md:
        def mk(m, d):
            y = today.year
            cand = date(y, int(m), int(d))
            return date(y + 1, int(m), int(d)) if cand < today - timedelta(days=30) else cand
        start = mk(*md[0])
        end = mk(*md[-1]) if len(md) > 1 else start
        return start, end
    return None, None


def _resolve_times(text: str):
    t = text.replace(" ", "")
    start = end = None
    if "퇴근" in t:
        end = data.get_setting("default_end_time")
    if "오전" in t and "오후" not in t:
        start, end = start or "09:00", end or "12:00"
    elif "오후" in t and "오전" not in t and not re.search(r"오후\d", t):
        start, end = start or "13:00", end or "18:00"
    hours = re.findall(r"(오전|오후)?(\d{1,2})시(반|30분)?", t)
    conv = []
    for ap, h, half in hours:
        h = int(h)
        if ap == "오후" and h < 12:
            h += 12
        elif not ap and 1 <= h <= 6:
            h += 12  # '3시'는 보통 오후 3시
        conv.append(f"{h:02d}:{'30' if half else '00'}")
    if conv:
        start = conv[0]
        if len(conv) > 1:
            end = conv[1]
    return start, end


def _resolve_needs(text: str) -> list[str]:
    needs = []
    if any(k in text for k in ("짐", "장비", "샘플", "박스", "트렁크")):
        needs.append("짐 많음")
    if any(k in text for k in ("장거리", "부산", "광주", "대구", "강릉", "출장")):
        needs.append("장거리")
    if "전기차" in text and any(k in text for k in ("말고", "제외", "싫")):
        needs.append("전기차 제외")
    elif "전기차" in text:
        needs.append("전기차 선호")
    return needs


def rule_parse(text: str, prev: dict | None = None) -> dict:
    today = date.today()
    sites = data.get_setting("sites")
    out = dict(prev or {})
    s, e = _resolve_dates(text, today)
    if s:
        if prev and prev.get("start_date") and re.search(r"까지", text) and not re.search(r"부터", text):
            # "목요일까지로 바꿔줘" → 시작일은 유지하고 종료일만 변경
            prev_start = date.fromisoformat(prev["start_date"])
            wd = re.findall(r"([월화수목금토일])요일", text.replace(" ", ""))
            if wd:
                e = prev_start + timedelta(days=(_wd(wd[-1]) - prev_start.weekday()) % 7)
            out["end_date"] = e.isoformat()
        else:
            out["start_date"], out["end_date"] = s.isoformat(), e.isoformat()
    st_, et_ = _resolve_times(text)
    if st_:
        out["start_time"] = st_
    if et_:
        out["end_time"] = et_
    m = re.search(r"(\d+)\s*(명|인(?!승))", text)
    if m:
        out["passengers"] = int(m.group(1))
    m = re.search(r"(\d+)\s*인승", text)
    if m:
        out["min_seats"] = int(m.group(1))
    for site in sites:
        if site in text:
            out["site"] = site
            break
    needs = set(out.get("needs") or []) | set(_resolve_needs(text))
    out["needs"] = [n for n in NEEDS if n in needs]
    out.setdefault("start_time", data.get_setting("default_start_time"))
    out.setdefault("end_time", data.get_setting("default_end_time"))
    out.setdefault("passengers", 1)
    if not out.get("destination"):
        m = re.search(r"([가-힣A-Za-z0-9]+ ?(?:공장|사무소|현장|센터|지점|연구소|역|공항|협력사))", text)
        out["destination"] = m.group(1) if m else (out.get("site") or "")
    out.setdefault("purpose", "업무용 차량 이용")
    return out


def parse_request(text: str, prev: dict | None = None) -> tuple[dict, str | None, str]:
    """자연어 예약 요청 해석. prev가 있으면 '이어서 수정'으로 처리한다.
    반환: (조건, 경고메시지, 해석방식)"""
    today = date.today()
    sites = data.get_setting("sites")
    context = ""
    if prev:
        context = f"\n현재까지 확정된 예약 조건(JSON):\n{json.dumps(prev, ensure_ascii=False)}\n" \
                  "사용자의 새 메시지는 위 조건을 '수정'하는 요청이다. 언급되지 않은 항목은 그대로 유지하라.\n"
    prompt = f"""너는 회사 총무팀의 법인차량 예약 요청을 구조화하는 도우미다.
오늘은 {today.isoformat()} ({WEEKDAYS[today.weekday()]}요일)이다. 현장 목록: {', '.join(sites)}.
{context}
사용자 메시지: {text}

JSON 하나만 반환하라. 스키마:
{{"start_date":"YYYY-MM-DD|null","end_date":"YYYY-MM-DD|null","start_time":"HH:MM|null","end_time":"HH:MM|null",
"passengers":숫자|null,"min_seats":숫자|null,"site":"현장명|null","destination":"문자열","purpose":"문자열",
"needs":["짐 많음","장거리","전기차 제외","전기차 선호" 중 해당 항목]}}
규칙: '퇴근할 때까지'는 18:00. 시간은 30분 단위. 'N인승'은 min_seats. 상대 날짜는 오늘 기준으로 계산."""
    result, err = call_ollama(prompt)
    method = "AI(Ollama)"
    if err or not isinstance(result, dict):
        result, method = {}, "규칙 기반(AI 미연결)"
    merged = dict(prev or {})
    for k, v in result.items():
        if v not in (None, "", []):
            merged[k] = v
    # 상대 날짜·시간·인원은 규칙 해석을 우선(소형 모델의 계산 실수 보정)
    ruled = rule_parse(text, merged if prev else None)
    for k in ("start_date", "end_date", "start_time", "end_time", "passengers", "min_seats", "site", "needs"):
        if ruled.get(k) not in (None, "", []):
            if k in ("start_date", "end_date") and not _resolve_dates(text, today)[0]:
                continue
            if k in ("start_time", "end_time") and not any(_resolve_times(text)):
                continue
            if k == "passengers" and not re.search(r"\d+\s*(명|인(?!승))", text):
                continue
            if k == "site" and not any(s in text for s in sites):
                continue
            merged[k] = ruled[k]
    for k in ("start_time", "end_time", "passengers", "destination", "purpose", "needs"):
        merged.setdefault(k, ruled.get(k))
    if merged.get("site") not in sites:
        merged["site"] = None
    return merged, (err if method != "AI(Ollama)" else None), method


# ---------------------------------------------------------------------------
# 계기판 사진 판독
# ---------------------------------------------------------------------------
def read_odometer(image: bytes) -> tuple[int | None, str]:
    prompt = ("이 사진은 자동차 계기판이다. 누적 주행거리(ODO, 총 주행거리) 숫자만 읽어라. "
              "구간 주행거리(TRIP)나 속도, 연비가 아니라 ODO 값이다. "
              'JSON으로만 답하라: {"odometer_km": 정수 또는 null, "confidence": "high|medium|low"}')
    result, err = call_ollama(prompt, images=[image], timeout=180)
    if err:
        return None, err
    try:
        km = result.get("odometer_km")
        km = int(str(km).replace(",", "")) if km not in (None, "") else None
        return km, f"AI 판독 신뢰도: {result.get('confidence', '-')}"
    except Exception:
        return None, "주행거리 숫자를 읽지 못했습니다."


# ---------------------------------------------------------------------------
# 관리자 질의
# ---------------------------------------------------------------------------
def answer_admin_question(question: str, context: dict) -> tuple[str | None, str | None]:
    prompt = f"""너는 회사 법인차량 운영 데이터를 분석하는 총무팀 도우미다.
아래 JSON 데이터만 근거로 한국어로 간결하게 답하라. 데이터에 없는 내용은 추측하지 말고 '데이터로 확인되지 않음'이라고 말하라.
숫자는 데이터 그대로 인용하고, 마지막에 실행할 만한 제안 1~2개를 덧붙여라.

데이터:
{json.dumps(context, ensure_ascii=False, default=str)[:12000]}

질문: {question}"""
    text, err = call_ollama(prompt, as_json=False, timeout=180)
    return (text, None) if not err else (None, err)
