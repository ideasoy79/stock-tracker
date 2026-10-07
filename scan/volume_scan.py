#!/usr/bin/env python3
"""
생각콩 × AI 주식 트래커 — 거래량 급증 스캐너

전 종목(코스피·코스닥)을 훑어서 최근 20거래일 안에
  · 거래량이 직전 20일 평균의 3배 이상
  · 그날 거래대금(추정: 종가×거래량) 50억 원 이상
인 날이 있었던 종목을 찾아 scan/latest.json 으로 저장합니다.

- GitHub Actions 에서 평일 장 마감 후 자동 실행됩니다 (.github/workflows/volume-scan.yml)
- PC에서도 실행 가능:  pip install requests pandas lxml  →  python scan/volume_scan.py
  만들어진 scan/latest.json 을 트래커 [급증 점검 → 파일 가져오기] 로 올리면 됩니다.

데이터 출처: 종목 목록 = KRX KIND 상장법인목록(실패 시 네이버 증권)
             일봉·수급·종목상태 = 한국투자증권 KIS Open API (환경변수 KIS_APPKEY / KIS_APPSECRET 이 있을 때)
             KIS 키가 없거나 실패하면 네이버 증권 차트 데이터로 자동 대체 (비공식 주소)
"""
import io
import json
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

import requests

RATIO_MIN = float(os.environ.get("RATIO_MIN", 3.0))          # 평균 대비 배수
AMOUNT_MIN = float(os.environ.get("AMOUNT_MIN", 5e9))         # 거래대금 하한 (원)
LOOKBACK = int(os.environ.get("LOOKBACK", 20))                # 최근 며칠 안에 발생
AVG_WIN = 20                                                  # 평균 기간
COUNT = 140                                                   # 받아올 일봉 개수 (120일 고점 계산용)
WORKERS = int(os.environ.get("WORKERS", 8))

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"}
KST = timezone(timedelta(hours=9))
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "latest.json")

session = requests.Session()
session.headers.update(UA)


# ───────────────────────── 종목 목록 ─────────────────────────
def is_excluded(name: str, code: str) -> bool:
    """스팩·우선주·리츠·ETF류 제외 (보통주만 남김)"""
    if "스팩" in name or "SPAC" in name.upper():
        return True
    if code[-1] != "0":                       # 우선주는 끝자리가 0이 아님 (005935 등)
        return True
    if re.search(r"(우|우B|우C|\(전환\))$", name):
        return True
    if re.search(r"리츠|REIT|ETN|ETF|KODEX|TIGER|KBSTAR|ACE |SOL |HANARO|ARIRANG", name, re.I):
        return True
    return False


def list_from_kind():
    import pandas as pd
    out = []
    for mkt, label in (("stockMkt", "KOSPI"), ("kosdaqMkt", "KOSDAQ")):
        url = ("https://kind.krx.co.kr/corpgeneral/corpList.do"
               f"?method=download&searchType=13&marketType={mkt}")
        r = session.get(url, timeout=30)
        r.encoding = "euc-kr"
        df = pd.read_html(io.StringIO(r.text), converters={"종목코드": str})[0]
        for _, row in df.iterrows():
            code = str(row["종목코드"]).strip().zfill(6)
            out.append({"code": code, "name": str(row["회사명"]).strip(), "market": label})
    return out


def list_from_naver():
    out = []
    for mkt in ("KOSPI", "KOSDAQ"):
        page = 1
        while True:
            url = f"https://m.stock.naver.com/api/stocks/marketValue/{mkt}?page={page}&pageSize=100"
            js = session.get(url, timeout=20).json()
            stocks = js.get("stocks", [])
            if not stocks:
                break
            for s in stocks:
                if s.get("stockEndType", "stock") != "stock":
                    continue
                out.append({"code": s["itemCode"], "name": s["stockName"], "market": mkt})
            page += 1
            if page > 40:
                break
    return out


def list_from_tracker():
    """최후 수단: 저장소의 index.html 안 KRX_STOCKS 목록(종목명·코드)을 사용"""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    html = open(os.path.join(root, "index.html"), encoding="utf-8").read()
    m = re.search(r"KRX_STOCKS\s*=\s*(\[.*?\]);?\s*\n", html, re.S)
    data = json.loads(m.group(1))
    return [{"code": x["code"], "name": x["name"], "market": ""} for x in data if x.get("code")]


def get_universe():
    for fn in (list_from_kind, list_from_naver, list_from_tracker):
        try:
            lst = fn()
            if len(lst) > 1000:
                print(f"[목록] {fn.__name__}: {len(lst)}개", flush=True)
                return lst
        except Exception as e:  # noqa
            print(f"[목록] {fn.__name__} 실패: {type(e).__name__}: {e}", flush=True)
    sys.exit("종목 목록을 가져오지 못했습니다.")


# ───────────────────────── 일봉 ─────────────────────────
ITEM_RE = re.compile(r'data="(\d{8})\|([\d.]+)\|([\d.]+)\|([\d.]+)\|([\d.]+)\|(\d+)"')


def fetch_daily(code: str, count: int = COUNT):
    url = ("https://fchart.stock.naver.com/sise.nhn"
           f"?symbol={code}&timeframe=day&count={count}&requestType=0")
    for attempt in range(3):
        try:
            r = session.get(url, timeout=15)
            r.encoding = "euc-kr"
            rows = []
            for d, o, h, l, c, v in ITEM_RE.findall(r.text):
                o, h, l, c, v = float(o), float(h), float(l), float(c), int(v)
                if min(o, h, l, c) <= 0:          # 거래정지일 등 0으로 찍힌 봉은 제외
                    continue
                rows.append({"d": f"{d[:4]}-{d[4:6]}-{d[6:]}", "o": o, "h": h, "l": l, "c": c, "v": v})
            return rows
        except Exception:
            time.sleep(1 + attempt)
    return []



# ───────────────────────── 한국투자증권 KIS ─────────────────────────
KIS_BASE = os.environ.get("KIS_BASE", "https://openapi.koreainvestment.com:9443")
KIS_RPS = float(os.environ.get("KIS_RPS", 15))   # 초당 호출 수 (실전 계좌 한도보다 조금 낮게)


class KIS:
    def __init__(self, appkey, appsecret):
        self.appkey, self.appsecret = appkey, appsecret
        self.token = None
        self.lock = threading.Lock()
        self.next_t = 0.0
        self.errors = 0

    def auth(self):
        r = requests.post(f"{KIS_BASE}/oauth2/tokenP", timeout=20, json={
            "grant_type": "client_credentials", "appkey": self.appkey, "appsecret": self.appsecret})
        js = r.json()
        if "access_token" not in js:
            raise RuntimeError(f"KIS 토큰 발급 실패: {js}")
        self.token = js["access_token"]

    def _throttle(self):
        with self.lock:
            now = time.time()
            wait = self.next_t - now
            self.next_t = max(now, self.next_t) + 1.0 / KIS_RPS
        if wait > 0:
            time.sleep(wait)

    def get(self, path, tr_id, params):
        for attempt in range(3):
            self._throttle()
            r = requests.get(f"{KIS_BASE}{path}", params=params, timeout=15, headers={
                "content-type": "application/json; charset=utf-8",
                "authorization": f"Bearer {self.token}",
                "appkey": self.appkey, "appsecret": self.appsecret,
                "tr_id": tr_id, "custtype": "P"})
            try:
                js = r.json()
            except Exception:
                js = {}
            if js.get("rt_cd") == "0":
                return js
            msg = js.get("msg1", "") + js.get("msg_cd", "")
            if "초당" in msg or "EGW00201" in msg:      # 호출 한도 초과 → 잠시 쉬고 재시도
                time.sleep(1.0)
                continue
            break
        self.errors += 1
        return None

    def daily(self, code, count=COUNT):
        """일봉 (최대 100개씩 → 두 번 호출해서 count개)"""
        rows, end = {}, datetime.now(KST).date()
        for _ in range(count // 100 + 1):
            js = self.get("/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice", "FHKST03010100", {
                "FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code,
                "FID_INPUT_DATE_1": (end - timedelta(days=170)).strftime("%Y%m%d"),
                "FID_INPUT_DATE_2": end.strftime("%Y%m%d"),
                "FID_PERIOD_DIV_CODE": "D", "FID_ORG_ADJ_PRC": "0"})
            if not js:
                break
            out = [x for x in js.get("output2", []) if x and x.get("stck_bsop_date")]
            for x in out:
                d = x["stck_bsop_date"]
                c = float(x.get("stck_clpr") or 0)
                if c <= 0 or min(float(x.get(k) or 0) for k in ("stck_oprc", "stck_hgpr", "stck_lwpr")) <= 0:
                    continue
                rows[d] = {"d": f"{d[:4]}-{d[4:6]}-{d[6:]}", "o": float(x["stck_oprc"]), "h": float(x["stck_hgpr"]),
                           "l": float(x["stck_lwpr"]), "c": c, "v": int(float(x["acml_vol"])),
                           "a": int(float(x.get("acml_tr_pbmn") or 0))}
            if len(rows) >= count or len(out) < 100:
                break
            oldest = min(x["stck_bsop_date"] for x in out)
            end = datetime.strptime(oldest, "%Y%m%d").date() - timedelta(days=1)
        return [rows[k] for k in sorted(rows)][-count:]

    def investor(self, code):
        """최근 30거래일 투자자별 순매수 (수량)"""
        js = self.get("/uapi/domestic-stock/v1/quotations/inquire-investor", "FHKST01010900",
                      {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code})
        out = {}
        for x in (js or {}).get("output", []) or []:
            d = x.get("stck_bsop_date")
            if not d:
                continue
            num = lambda k: int(float(x.get(k) or 0))
            out[f"{d[:4]}-{d[4:6]}-{d[6:]}"] = {"frgn": num("frgn_ntby_qty"), "orgn": num("orgn_ntby_qty"), "prsn": num("prsn_ntby_qty")}
        return out

    def status(self, code):
        """종목상태구분코드 (51 관리, 52 투자위험, 53 투자경고, 54 투자주의, 58 거래정지, 59 단기과열)"""
        js = self.get("/uapi/domestic-stock/v1/quotations/inquire-price", "FHKST01010100",
                      {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code})
        o = (js or {}).get("output") or {}
        return {"stat": o.get("iscd_stat_cls_code", ""), "halt": o.get("temp_stop_yn", "")}


kis = None


def init_kis():
    global kis
    k, sct = os.environ.get("KIS_APPKEY"), os.environ.get("KIS_APPSECRET")
    if not (k and sct):
        print("[KIS] 키 없음 → 네이버 시세 사용", flush=True)
        return
    try:
        kis = KIS(k, sct)
        kis.auth()
        print("[KIS] 토큰 발급 완료 → 한국투자증권 시세 사용", flush=True)
    except Exception as e:
        kis = None
        print(f"[KIS] 사용 불가({e}) → 네이버 시세로 대체", flush=True)


def fetch_rows(code):
    """KIS 우선, 실패하면 네이버"""
    if kis:
        try:
            rows = kis.daily(code)
            if len(rows) > AVG_WIN + 2:
                return rows, "kis"
        except Exception:
            pass
    return fetch_daily(code), "naver"

# ───────────────────────── 신호 계산 ─────────────────────────
def analyze(stock, rows):
    """최근 LOOKBACK 거래일 중 가장 최근 신호일 1건을 돌려줌 (없으면 None)"""
    n = len(rows)
    if n < AVG_WIN + 2:
        return None
    hit = None
    start = max(AVG_WIN, n - LOOKBACK)
    for i in range(n - 1, start - 1, -1):
        prev = rows[i - AVG_WIN:i]
        avg = sum(r["v"] for r in prev) / AVG_WIN
        if avg <= 0:
            continue
        r = rows[i]
        ratio = r["v"] / avg
        amount = r.get("a") or r["c"] * r["v"]
        if r["v"] == 0:
            continue
        if ratio >= RATIO_MIN and amount >= AMOUNT_MIN:
            hit = (i, ratio, avg, amount)
            break
    if not hit:
        return None

    i, ratio, avg, amount = hit
    r = rows[i]
    pc = rows[i - 1]["c"] or r["c"]
    chg = (r["c"] / pc - 1) * 100
    win60 = rows[max(0, i - 59):i + 1]
    win120 = rows[max(0, i - 119):i + 1]
    low60 = min(x["l"] for x in win60) or r["l"] or 1
    high120 = max(x["h"] for x in win120) or r["h"] or 1
    rng = r["h"] - r["l"]
    upper = (r["h"] - max(r["o"], r["c"])) / rng if rng else 0
    lower = (min(r["o"], r["c"]) - r["l"]) / rng if rng else 0

    after = rows[i + 1:]
    track = {}
    for k in (5, 10):
        if len(after) >= k:
            seg = after[:k]
            track[f"d{k}"] = {
                "date": seg[-1]["d"],
                "close": seg[-1]["c"],
                "ret": round((seg[-1]["c"] / r["c"] - 1) * 100, 2),
                "held": min(x["l"] for x in seg) >= r["l"],
            }

    return {
        "code": stock["code"],
        "name": stock["name"],
        "market": stock["market"],
        "sigDate": r["d"],
        "daysAgo": n - 1 - i,
        "bar": {"o": r["o"], "h": r["h"], "l": r["l"], "c": r["c"], "v": r["v"]},
        "prevClose": pc,
        "chg": round(chg, 2),
        "ratio": round(ratio, 2),
        "avg20": int(avg),
        "amountEst": int(amount),
        "amountReal": bool(r.get("a")),
        "fromLow60": round((r["c"] / low60 - 1) * 100, 2),
        "fromHigh120": round((r["c"] / high120 - 1) * 100, 2),
        "upperTail": round(upper, 3),
        "lowerTail": round(lower, 3),
        "lastDate": rows[-1]["d"],
        "lastClose": rows[-1]["c"],
        "track": track,
    }


def theme_payload(keep):
    try:
        import theme_flow
        th = theme_flow.load_themes()
        if not th:
            print("[테마] themes.json 없음 → 테마 흐름 생략", flush=True)
            return None
        p = theme_flow.daily_payload(keep, th)
        print(f"[테마] {p['count'] if p else 0}개 테마 흐름 계산", flush=True)
        return p
    except Exception as e:
        print(f"[테마] 계산 실패: {e}", flush=True)
        return None


def main():
    t0 = time.time()
    init_kis()
    universe = [s for s in get_universe() if not is_excluded(s["name"], s["code"])]
    print(f"[대상] 보통주 {len(universe)}개", flush=True)

    hits, base_dates, fails, srcs = [], {}, 0, {}
    keep = {}                                   # 테마 흐름 계산용 (종목별 최근 일봉)
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = {ex.submit(fetch_rows, s["code"]): s for s in universe}
        for k, f in enumerate(as_completed(futs), 1):
            s = futs[f]
            rows, src = f.result()
            srcs[src] = srcs.get(src, 0) + 1
            if not rows:
                fails += 1
                continue
            base_dates[rows[-1]["d"]] = base_dates.get(rows[-1]["d"], 0) + 1
            keep[s["code"]] = [{"d": x["d"], "c": x["c"], "a": x.get("a") or x["c"] * x["v"]} for x in rows]
            res = analyze(s, rows)
            if res:
                hits.append(res)
            if k % 300 == 0:
                print(f"  … {k}/{len(universe)}  (신호 {len(hits)})", flush=True)

    base = max(base_dates, key=base_dates.get) if base_dates else None

    # 신호 종목만 수급(기관·외국인)과 종목상태(관리·경고·정지) 추가 조회
    if kis and hits:
        def enrich(h):
            try:
                inv = kis.investor(h["code"])
                if h["sigDate"] in inv:
                    h["inv"] = inv[h["sigDate"]]
                h.update(kis.status(h["code"]))
            except Exception:
                pass
        with ThreadPoolExecutor(max_workers=4) as ex:
            list(ex.map(enrich, hits))
        print(f"[KIS] 수급·상태 조회 {len(hits)}종목 (오류 {kis.errors})", flush=True)
    hits.sort(key=lambda x: (x["sigDate"], x["ratio"]), reverse=True)
    out = {
        "kind": "sv4-volume-scan",
        "version": 1,
        "generatedAt": datetime.now(KST).isoformat(timespec="seconds"),
        "baseDate": base,
        "params": {"ratioMin": RATIO_MIN, "amountMin": AMOUNT_MIN, "lookback": LOOKBACK, "avgWin": AVG_WIN},
        "universe": len(universe),
        "fetchFailed": fails,
        "sources": srcs,
        "note": "amountReal=false 이면 거래대금은 종가×거래량 추정치. stat: 51관리 52투자위험 53투자경고 54투자주의 58거래정지 59단기과열.",
        "hits": hits,
        "themes": theme_payload(keep),
    }
    with open(OUT, "w", encoding="utf-8") as fp:
        json.dump(out, fp, ensure_ascii=False, indent=1)
    print(f"[완료] 신호 {len(hits)}종목 · 기준일 {base} · 출처 {srcs} · 실패 {fails} · {time.time()-t0:.0f}초 → {OUT}")
    if len(universe) and fails / len(universe) > 0.5:
        sys.exit("시세를 절반 이상 못 받았습니다. 네이버 주소가 바뀌었는지 확인이 필요합니다.")


if __name__ == "__main__":
    main()
