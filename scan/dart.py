"""
OpenDART 공시 경고 (무료 · 키 필요)

- 키 발급: https://opendart.fss.or.kr → 인증키 신청 (무료, 하루 2만 회)
- 저장소 Settings › Secrets and variables › Actions › New repository secret
  이름: DART_KEY  값: 발급받은 40자리 키
- 키가 없으면 아무 일도 하지 않고 None 을 돌려줌 (스캔·앱은 그대로 동작)

최근 N일 주요사항보고(B) · 거래소공시(I) 목록을 받아 제목으로 분류:
  risk  : 유상증자, 전환사채·신주인수권부사채·교환사채 발행, 감자, 관리종목, 상장폐지 심사, 거래정지,
          투자경고·위험, 불성실공시, 횡령·배임, 회생, 최대주주 변경, 소송
  good  : 자사주 매입, 공급계약, 무상증자
  info  : 잠정실적, 실적 변동(30% 이상), IR 개최, 배당
risk 는 전 종목, good/info 는 관심 종목(오늘 제안 · 최근 제안 · 신호 종목)만 남김.
분류는 제목 기준이라 내용(조건·규모)까지는 보지 않음 → 앱에서 원문 링크로 확인.
"""
import os
import time
from datetime import datetime, timedelta, timezone

import requests

KST = timezone(timedelta(hours=9))
API = "https://opendart.fss.or.kr/api/list.json"
VIEW = "https://dart.fss.or.kr/dsaf001/main.do?rcpNo="

RULES = [  # (제목에 포함된 말 — 공백 제거 후, 종류, 짧은 이름)
    ("유상증자결정", "risk", "유상증자"),
    ("전환사채권발행결정", "risk", "전환사채 발행"),
    ("신주인수권부사채권발행결정", "risk", "신주인수권부사채 발행"),
    ("교환사채권발행결정", "risk", "교환사채 발행"),
    ("감자결정", "risk", "감자"),
    ("관리종목", "risk", "관리종목"),
    ("상장적격성", "risk", "상장폐지 심사"),
    ("상장폐지", "risk", "상장폐지"),
    ("매매거래정지", "risk", "거래정지"),
    ("투자경고종목", "risk", "투자경고"),
    ("투자위험종목", "risk", "투자위험"),
    ("불성실공시", "risk", "불성실공시"),
    ("횡령", "risk", "횡령·배임"),
    ("배임", "risk", "횡령·배임"),
    ("회생절차", "risk", "회생절차"),
    ("최대주주변경", "risk", "최대주주 변경"),
    ("소송등의제기", "risk", "소송"),
    ("자기주식취득", "good", "자사주 매입"),
    ("단일판매ㆍ공급계약체결", "good", "공급계약"),
    ("단일판매·공급계약체결", "good", "공급계약"),
    ("무상증자결정", "good", "무상증자"),
    ("영업(잠정)실적", "info", "잠정실적"),
    ("매출액또는손익구조", "info", "실적 변동"),
    ("기업설명회(IR)개최", "info", "IR 개최"),
    ("현금ㆍ현물배당결정", "info", "배당"),
    ("현금·현물배당결정", "info", "배당"),
]


def classify(title):
    t = str(title or "").replace(" ", "")
    if "철회" in t or "해제" in t:          # 철회·지정해제 공시는 위험 해소 쪽이라 제외
        return None
    for key, kind, lab in RULES:
        if key in t:
            return kind, lab
    return None


def _get(params, key, tries=3):
    for i in range(tries):
        try:
            r = requests.get(API, params=dict(params, crtfc_key=key), timeout=15)
            js = r.json()
            return js
        except Exception:
            time.sleep(1 + i)
    return {"status": "err", "message": "요청 실패"}


def fetch(interest=(), days=10, key=None, max_pages=60):
    """interest: 관심 종목 코드 집합. 반환 {"asOf","days","items":{code:[{d,k,lab,t,url,fix}]}} 또는 None(키 없음)"""
    key = key or os.environ.get("DART_KEY", "").strip()
    if not key:
        print("[공시] DART_KEY 없음 → 공시 경고 건너뜀", flush=True)
        return None
    interest = set(interest or ())
    now = datetime.now(KST)
    bgn, end = (now - timedelta(days=days)).strftime("%Y%m%d"), now.strftime("%Y%m%d")
    items, seen, calls, err = {}, set(), 0, None
    for ty in ("B", "I"):
        page, total = 1, 1
        while page <= total and page <= max_pages:
            js = _get({"bgn_de": bgn, "end_de": end, "pblntf_ty": ty, "page_no": page, "page_count": 100}, key)
            calls += 1
            st = js.get("status")
            if st == "013":            # 조회된 데이터 없음
                break
            if st != "000":
                err = f"{st} {js.get('message', '')}".strip()
                break
            total = int(js.get("total_page") or 1)
            for x in js.get("list") or []:
                code = (x.get("stock_code") or "").strip()
                if not code or x.get("rcept_no") in seen:
                    continue
                c = classify(x.get("report_nm"))
                if not c:
                    continue
                kind, lab = c
                if kind != "risk" and code not in interest:
                    continue
                seen.add(x.get("rcept_no"))
                d = x.get("rcept_dt") or ""
                items.setdefault(code, []).append({
                    "d": f"{d[:4]}-{d[4:6]}-{d[6:8]}" if len(d) == 8 else d,
                    "k": kind, "lab": lab, "t": str(x.get("report_nm") or "").strip()[:60],
                    "url": VIEW + str(x.get("rcept_no")), "fix": "정정" in str(x.get("report_nm") or "")})
            page += 1
            time.sleep(0.15)
        if err and err.startswith(("010", "011", "012", "020", "021")):   # 키 오류·한도 초과면 중단
            break
    for code in items:
        items[code].sort(key=lambda z: (z["d"], z["k"] == "risk"), reverse=True)
        items[code] = items[code][:6]
    nr = sum(1 for v in items.values() if any(z["k"] == "risk" for z in v))
    print(f"[공시] {calls}회 조회 · {len(items)}종목 (위험 공시 {nr}종목){' · 오류 ' + err if err else ''}", flush=True)
    out = {"asOf": now.strftime("%Y-%m-%d"), "days": days, "items": items}
    if err:
        out["error"] = err
    return out


if __name__ == "__main__":
    import json
    print(json.dumps(fetch(), ensure_ascii=False, indent=1)[:3000])
