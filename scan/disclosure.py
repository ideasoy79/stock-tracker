"""
S6 공시 위험 필터 — 묶음 후보 종목에서 '최근 위험 공시'가 있는 종목을 자동으로 뺍니다

데이터 출처: 금융감독원 OpenDART (무료 API 키 → GitHub Secrets 의 OPENDART_API_KEY)
- 키가 없거나 조회에 실패하면 필터를 켜지 않고(fail-open) 기존 방식 그대로 동작합니다.
- 보호예수 해제는 DART 공시가 아니라(예탁결제원 자료) 여기서는 확인하지 못합니다.
- 과거 공시 데이터를 받아 검증할 수 없어서 백테스트(backtest.py · theme_bt.py)에는 반영하지 않았습니다.

위험 공시 두 종류
  DILUTION  지분 희석·오버행 : 유상증자 · 전환사채(CB) · 신주인수권부사채(BW) · 교환사채(EB)  → 최근 30일
  SEVERE    중대 사건       : 횡령·배임 · 상장폐지 · 관리종목 · 불성실공시 · 투자주의환기 · 거래정지 ·
                              회생절차 · 부도 · 감사의견 비적정  → 최근 90일
"""
import io
import os
import re
import time
import zipfile
import xml.etree.ElementTree as ET
from datetime import date, timedelta

import requests

API = "https://opendart.fss.or.kr/api"
DILUTION_DAYS = 30
SEVERE_DAYS = 90

# (분류, 라벨, 제목에 들어 있는 문구)  — 위에서부터 먼저 맞는 것 하나만 적용
RULES = [
    ("DILUTION", "유상증자", ("유상증자결정",)),
    ("DILUTION", "전환사채(CB)", ("전환사채권발행결정",)),
    ("DILUTION", "신주인수권부사채(BW)", ("신주인수권부사채권발행결정",)),
    ("DILUTION", "교환사채(EB)", ("교환사채권발행결정",)),
    ("SEVERE", "횡령·배임", ("횡령", "배임")),
    ("SEVERE", "상장폐지", ("상장폐지",)),
    ("SEVERE", "관리종목", ("관리종목",)),
    ("SEVERE", "불성실공시", ("불성실공시",)),
    ("SEVERE", "투자주의환기", ("투자주의환기",)),
    ("SEVERE", "거래정지", ("거래정지",)),
    ("SEVERE", "회생절차·부도", ("회생절차", "부도발생")),
    ("SEVERE", "감사의견 비적정", ("감사의견", "의견거절", "한정", "부적정")),
]
# 제목에 이런 말이 있으면 '위험 해소'로 보고 건너뜀
CLEARED = ("철회", "취소", "해제", "해소", "종결", "정상")
PREFIX = re.compile(r"^\s*(\[[^\]]*\]\s*)+")        # [기재정정] [첨부정정] 등


def classify(report_nm):
    """공시 제목 → (분류, 라벨) 또는 None"""
    t = PREFIX.sub("", report_nm or "")
    if any(w in t for w in CLEARED):
        return None
    for kind, label, words in RULES:
        if label == "감사의견 비적정":
            # '감사보고서'가 아닌 일반 제목은 제외하고, 비적정 문구가 직접 들어 있을 때만
            if "감사" in t and any(w in t for w in ("의견거절", "부적정", "한정의견", "한정 의견")):
                return kind, label
            continue
        if any(w in t for w in words):
            return kind, label
    return None


def _ymd(d):
    return d.strftime("%Y%m%d")


class Checker:
    def __init__(self, key, session=None, today=None):
        self.key = key
        self.s = session or requests.Session()
        self.today = today or date.today()
        self.corp = None                    # {종목코드: corp_code}

    # ── 종목코드 → corp_code (1회 다운로드) ──
    def _load_corp(self):
        r = self.s.get(f"{API}/corpCode.xml", params={"crtfc_key": self.key}, timeout=60)
        r.raise_for_status()
        z = zipfile.ZipFile(io.BytesIO(r.content))
        root = ET.fromstring(z.read(z.namelist()[0]))
        m = {}
        for it in root.iter("list"):
            sc = (it.findtext("stock_code") or "").strip()
            if sc:
                m[sc] = it.findtext("corp_code")
        if not m:
            raise RuntimeError("corpCode 응답이 비어 있음 (키 확인 필요)")
        self.corp = m

    def _list(self, corp_code, bgn):
        out, page = [], 1
        while True:
            r = self.s.get(f"{API}/list.json", params={
                "crtfc_key": self.key, "corp_code": corp_code, "bgn_de": _ymd(bgn),
                "end_de": _ymd(self.today), "page_no": page, "page_count": 100}, timeout=30)
            r.raise_for_status()
            j = r.json()
            st = j.get("status")
            if st == "013":                 # 조회된 데이터 없음
                return out
            if st != "000":
                raise RuntimeError(f"DART status {st} {j.get('message')}")
            out += j.get("list") or []
            if page >= int(j.get("total_page") or 1):
                return out
            page += 1

    def check_code(self, code):
        """한 종목의 위험 공시 → 가장 최근 것 하나 (없으면 None). 조회 실패는 예외"""
        cc = self.corp.get(code)
        if not cc:
            return None
        bgn = self.today - timedelta(days=max(DILUTION_DAYS, SEVERE_DAYS))
        found = []
        for it in self._list(cc, bgn):
            hit = classify(it.get("report_nm"))
            if not hit:
                continue
            d = it.get("rcept_dt") or ""
            try:
                dd = date(int(d[:4]), int(d[4:6]), int(d[6:8]))
            except Exception:
                continue
            limit = DILUTION_DAYS if hit[0] == "DILUTION" else SEVERE_DAYS
            if (self.today - dd).days <= limit:
                found.append({"kind": hit[0], "label": hit[1], "title": PREFIX.sub("", it.get("report_nm", "")).strip(),
                              "date": dd.isoformat()})
        if not found:
            return None
        # 중대 사건 우선, 그다음 가장 최근 날짜
        sev = [x for x in found if x["kind"] == "SEVERE"]
        pool = sev or found
        return max(pool, key=lambda x: x["date"])

    def __call__(self, codes):
        """codes → {"hits": {code: 공시}, "checked": [code], "failed": [code]}"""
        if self.corp is None:
            self._load_corp()
        hits, checked, failed = {}, [], []
        for c in dict.fromkeys(codes):
            try:
                h = self.check_code(c)
                checked.append(c)
                if h:
                    hits[c] = h
            except Exception as e:
                failed.append(c)
                print(f"[S6] {c} 공시 조회 실패: {e}", flush=True)
            time.sleep(0.05)                # DART 호출 간격(일일 2만 건 · 초당 제한 대비)
        return {"hits": hits, "checked": checked, "failed": failed}


def make_checker():
    """OPENDART_API_KEY 가 있으면 조회 함수, 없으면 None (필터 꺼짐)"""
    key = os.environ.get("OPENDART_API_KEY", "").strip()
    if not key:
        print("[S6] OPENDART_API_KEY 없음 → 공시 위험 필터 꺼짐", flush=True)
        return None
    return Checker(key)
