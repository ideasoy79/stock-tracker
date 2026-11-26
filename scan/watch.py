"""
관심 테마 워치리스트 (로봇 · 전력망·전력기기 · 고령화 헬스케어)

종목 목록과 성장성·연결도 점수는 scan/watchlist.json (Claude 정성 판단, 분기마다 재채점)
매일 저녁 스캔에서 자동으로 채우는 것
- 시장 연결도: 최근 120거래일 종목 수익률과 그 관심 테마 평균의 상관계수(0~1)
- ① 고객·수주: 최근 90일 DART 공급계약(단일판매·공급계약) 공시
- ② 실적 개선: DART 재무 API — 최근 분기 매출이 전년 같은 분기보다 늘고, 영업이익이 늘었거나 흑자
- ③ 수급: 외국인 또는 기관이 2거래일 연속 순매수 (한국투자증권 API)
- 테마 시점: 관심 테마(연결된 트래커 테마 종목 평균)가 60일·20일 모두 플러스 = 주도 칸
- 진입 검토 = 체크 2개 이상 + 테마 주도 칸. 켜진 날을 기록해 10·20거래일 뒤 결과를 지수(KODEX 200)와 비교
상태는 scan/watch_state.json 에 누적 (공시 90일, 재무 캐시, 신호 기록)
"""
import io
import json
import os
import time
import zipfile
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
KST = timezone(timedelta(hours=9))
WL_FILE = os.path.join(HERE, "watchlist.json")
ST_FILE = os.path.join(HERE, "watch_state.json")
DART = "https://opendart.fss.or.kr/api"


def load_list():
    try:
        return json.load(open(WL_FILE, encoding="utf-8"))
    except Exception:
        return None


def codes():
    w = load_list() or {}
    return {s["code"] for t in w.get("themes", []) for s in t.get("stocks", [])}


def _state():
    try:
        return json.load(open(ST_FILE, encoding="utf-8"))
    except Exception:
        return {}


def grade(g, l):
    if g <= 3:
        return "편승·보류"
    if g >= 6:
        return "관찰 우선" if l >= 5 else "간접 수혜"
    return "조건부"


def _corr(a, b):
    n = len(a)
    if n < 30:
        return None
    ma, mb = sum(a) / n, sum(b) / n
    va = sum((x - ma) ** 2 for x in a)
    vb = sum((y - mb) ** 2 for y in b)
    if va <= 0 or vb <= 0:
        return None
    return sum((x - ma) * (y - mb) for x, y in zip(a, b)) / (va * vb) ** 0.5


def _rets(rows):
    out, prev = {}, None
    for r in rows:
        if prev and prev > 0:
            x = r["c"] / prev - 1
            if abs(x) < 0.35:
                out[r["d"]] = x
        prev = r["c"]
    return out


# ───── DART: 공급계약 90일 누적 ─────
def dart_merge(st, dt, wcodes, key):
    """오늘 dart.fetch 결과(dt)에서 관심 종목의 공급계약·실적 공시를 90일 누적. 처음 한 번은 90일치를 받아 채움"""
    hist = st.setdefault("dart", {})
    if key and not st.get("dartBackfill"):
        try:
            import dart
            bf = dart.fetch(wcodes, days=90, key=key, max_pages=400)
            if bf and not bf.get("error"):
                dt = {"items": {**(bf.get("items") or {}), **((dt or {}).get("items") or {})}}
                st["dartBackfill"] = datetime.now(KST).strftime("%Y-%m-%d")
        except Exception as e:
            print(f"[관심] 공시 90일 채우기 실패: {e}", flush=True)
    for c, items in ((dt or {}).get("items") or {}).items():
        if c not in wcodes:
            continue
        cur = hist.setdefault(c, [])
        seen = {x["url"] for x in cur}
        for z in items:
            if z["url"] not in seen:
                cur.append({k: z[k] for k in ("d", "k", "lab", "t", "url")})
    cut = (datetime.now(KST) - timedelta(days=90)).strftime("%Y-%m-%d")
    for c in list(hist):
        hist[c] = sorted([z for z in hist[c] if z["d"] >= cut], key=lambda z: z["d"], reverse=True)[:12]
        if not hist[c]:
            del hist[c]


# ───── DART: 재무 (② 실적) ─────
def _num(v):
    try:
        return float(str(v).replace(",", "").strip() or "nan")
    except Exception:
        return None


def corp_codes(st, wcodes, key):
    cc = st.setdefault("corp", {})
    need = [c for c in wcodes if c not in cc]
    if not need or not key:
        return cc
    if st.get("corpAt") == datetime.now(KST).strftime("%Y-%m-%d"):
        return cc
    try:
        r = requests.get(f"{DART}/corpCode.xml", params={"crtfc_key": key}, timeout=60)
        z = zipfile.ZipFile(io.BytesIO(r.content))
        root = ET.fromstring(z.read(z.namelist()[0]))
        for it in root.iter("list"):
            sc = (it.findtext("stock_code") or "").strip()
            if sc in wcodes:
                cc[sc] = it.findtext("corp_code")
        st["corpAt"] = datetime.now(KST).strftime("%Y-%m-%d")
    except Exception as e:
        print(f"[관심] 회사 고유번호 받기 실패: {e}", flush=True)
    return cc


REPORTS = [("11014", "3분기"), ("11012", "반기"), ("11013", "1분기"), ("11011", "연간")]


def fin_check(st, wcodes, key, max_calls=150):
    """분기 실적: 7일에 한 번 종목별로 최신 보고서를 찾아 매출·영업이익을 전년 같은 기간과 비교"""
    fin = st.setdefault("fin", {})
    if not key:
        return fin
    cc = corp_codes(st, wcodes, key)
    today = datetime.now(KST)
    calls = 0
    for c in sorted(wcodes):
        f = fin.get(c) or {}
        if f.get("at") and (today - datetime.strptime(f["at"], "%Y-%m-%d").replace(tzinfo=KST)).days < 7:
            continue
        corp = cc.get(c)
        if not corp:
            continue
        got = None
        for yr in (today.year, today.year - 1):
            for code, lab in REPORTS:
                if calls >= max_calls:
                    break
                calls += 1
                try:
                    js = requests.get(f"{DART}/fnlttSinglAcnt.json", timeout=15, params={
                        "crtfc_key": key, "corp_code": corp, "bsns_year": str(yr), "reprt_code": code}).json()
                except Exception:
                    js = {}
                time.sleep(0.12)
                if js.get("status") != "000":
                    continue
                rows = js.get("list") or []
                for div in ("CFS", "OFS"):
                    pick = {}
                    for x in rows:
                        if x.get("fs_div") != div:
                            continue
                        nm = (x.get("account_nm") or "").replace(" ", "")
                        if nm in ("매출액", "수익(매출액)", "영업수익") and "sales" not in pick:
                            pick["sales"] = x
                        if nm in ("영업이익", "영업이익(손실)") and "op" not in pick:
                            pick["op"] = x
                    if "sales" in pick and "op" in pick:
                        s1, s0 = _num(pick["sales"].get("thstrm_amount")), _num(pick["sales"].get("frmtrm_amount"))
                        o1, o0 = _num(pick["op"].get("thstrm_amount")), _num(pick["op"].get("frmtrm_amount"))
                        if None not in (s1, s0, o1, o0):
                            got = {"period": f"{yr} {lab}", "sales": round(s1 / 1e8), "sales0": round(s0 / 1e8),
                                   "op": round(o1 / 1e8), "op0": round(o0 / 1e8),
                                   "ok": bool(s1 > s0 and (o1 > o0 or o1 > 0))}
                        break
                if got:
                    break
            if got or calls >= max_calls:
                break
        fin[c] = dict(got or {"none": True}, at=today.strftime("%Y-%m-%d"))
    print(f"[관심] 재무 조회 {calls}회", flush=True)
    return fin


# ───── 본체 ─────
def run(keep, names, kis=None, dt=None, tp=None, bench=None, inv_cache=None):
    w = load_list()
    if not w:
        return None
    st = _state()
    wcodes = codes()
    key = os.environ.get("DART_KEY", "").strip()
    try:
        dart_merge(st, dt, wcodes, key)
    except Exception as e:
        print(f"[관심] 공시 누적 실패: {e}", flush=True)
    try:
        fin = fin_check(st, wcodes, key)
    except Exception as e:
        fin = st.get("fin", {})
        print(f"[관심] 재무 실패: {e}", flush=True)
    import theme_flow as _tf
    th = _tf.load_themes()
    rets = {c: _rets(keep[c]) for c in wcodes if c in keep}
    dates = sorted({d for c in keep for d in (r["d"] for r in keep[c][-130:])})[-121:]
    asof = dates[-1] if dates else None
    inv_cache = inv_cache if inv_cache is not None else {}
    out_th = []
    sigs_today = []
    for t in w["themes"]:
        mem = sorted({c for n in t.get("timing", []) for c in th.get(n, []) if c in keep} | {s["code"] for s in t["stocks"] if s["code"] in keep})
        mr = {c: _rets(keep[c]) if c not in rets else rets[c] for c in mem}
        idx = []
        for d in dates[1:]:
            v = [mr[c][d] for c in mem if d in mr[c]]
            idx.append(sum(v) / len(v) if v else 0.0)
        def cum(k):
            if len(idx) < k:
                return None
            v = 1.0
            for x in idx[-k:]:
                v *= 1 + x
            return v - 1
        x60, x20, x5 = cum(60), cum(20), cum(5)
        q = None if x60 is None else ("주도" if x60 > 0 and x20 > 0 else "과열·눌림" if x60 > 0 else "부상" if x20 > 0 else "소외")
        timing = q == "주도"
        rows = []
        for s in t["stocks"]:
            c = s["code"]
            r = {"code": c, "name": s["name"], "cat": s["cat"], "g": s["g"], "l": s["l"], "grade": grade(s["g"], s["l"]),
                 "why": s.get("why", ""), "risk": s.get("risk", "")}
            if c not in keep:
                r["nodata"] = True
                rows.append(r)
                continue
            a = [rets[c].get(d) for d in dates[1:]]
            pairs = [(x, y) for x, y in zip(a, idx) if x is not None]
            cr = _corr([p[0] for p in pairs], [p[1] for p in pairs])
            r["corr"] = round(cr, 2) if cr is not None else None
            last = keep[c][-1]
            r["close"] = last["c"]
            r["r5"] = round((last["c"] / keep[c][-6]["c"] - 1) * 100, 1) if len(keep[c]) > 6 else None
            # ① 공급계약 90일
            ds = [z for z in st.get("dart", {}).get(c, []) if z.get("lab") == "공급계약"]
            r["c1"] = bool(ds)
            if ds:
                r["c1t"] = f"{ds[0]['d'][5:].replace('-', '/')} {ds[0]['t'][:40]}"
                r["c1u"] = ds[0]["url"]
            risk = [z for z in st.get("dart", {}).get(c, []) if z.get("k") == "risk"]
            if risk:
                r["warn"] = f"{risk[0]['d'][5:].replace('-', '/')} {risk[0]['lab']}"
            # ② 실적
            f = fin.get(c) or {}
            if f and not f.get("none"):
                r["c2"] = bool(f.get("ok"))
                r["c2t"] = f"{f['period']} 매출 {f['sales']:,}억(전년 {f['sales0']:,}) · 영업이익 {f['op']:,}억(전년 {f['op0']:,})"
            else:
                r["c2"] = None
            # ③ 수급
            iv = inv_cache.get(c)
            if iv is None and kis:
                try:
                    iv = kis.investor(c)
                except Exception:
                    iv = {}
                inv_cache[c] = iv
            iv = iv or {}
            dd = sorted(iv)[-2:]
            if len(dd) == 2:
                fr = all(iv[d]["frgn"] > 0 for d in dd)
                og = all(iv[d]["orgn"] > 0 for d in dd)
                r["c3"] = fr or og
                r["c3t"] = ("외국인" if fr else "") + ("·" if fr and og else "") + ("기관" if og else "") + (" 2일 연속 순매수" if fr or og else "")
            else:
                r["c3"] = None
            n = sum(1 for k in ("c1", "c2", "c3") if r.get(k))
            r["n"] = n
            r["sig"] = "진입 검토" if n >= 2 and timing else ("체크 충족 · 시점 대기" if n >= 2 else "대기")
            if r["sig"] == "진입 검토":
                sigs_today.append({"d": asof, "theme": t["id"], "code": c, "name": s["name"], "px": last["c"], "n": n, "grade": r["grade"]})
            rows.append(r)
        out_th.append({"id": t["id"], "name": t["name"], "desc": t.get("desc", ""), "q": q,
                       "x": round(x60 * 100, 1) if x60 is not None else None, "y": round(x20 * 100, 1) if x20 is not None else None,
                       "r5": round(x5 * 100, 1) if x5 is not None else None, "timing": timing, "nmem": len(mem), "stocks": rows})
    # 신호 기록과 채점 (10·20거래일 뒤, 같은 기간 지수 ETF와 비교)
    sig = st.setdefault("signals", [])
    prev = {(x["code"], x["theme"]) for x in sig if x.get("d") and asof and x["d"] >= (dates[-6] if len(dates) > 6 else "")}
    for x in sigs_today:
        if (x["code"], x["theme"]) not in prev and not any(y["d"] == x["d"] and y["code"] == x["code"] for y in sig):
            sig.append(x)
    bmap = {r["d"]: r["c"] for r in (bench or [])}
    for x in sig:
        rows = keep.get(x["code"]) or []
        pos = {r["d"]: i for i, r in enumerate(rows)}
        if x["d"] not in pos:
            continue
        i0 = pos[x["d"]]
        for k in (10, 20):
            if f"r{k}" in x or i0 + k >= len(rows):
                continue
            d1 = rows[i0 + k]["d"]
            x[f"r{k}"] = round((rows[i0 + k]["c"] / x["px"] - 1) * 100, 2)
            if x["d"] in bmap and d1 in bmap:
                x[f"i{k}"] = round((bmap[d1] / bmap[x["d"]] - 1) * 100, 2)
    st["signals"] = sig[-300:]
    done = [x for x in sig if "r10" in x]
    score = None
    if done:
        wi = [x for x in done if "i10" in x]
        score = {"n": len(done), "avg10": round(sum(x["r10"] for x in done) / len(done), 2),
                 "win10": round(sum(1 for x in done if x["r10"] > 0) / len(done) * 100)}
        if wi:
            score.update({"idx10": round(sum(x["i10"] for x in wi) / len(wi), 2),
                          "beat10": round(sum(1 for x in wi if x["r10"] > x["i10"]) / len(wi) * 100)})
    with open(ST_FILE, "w", encoding="utf-8") as fp:
        json.dump(st, fp, ensure_ascii=False, indent=0)
    on = sum(1 for t in out_th for r in t["stocks"] if r.get("sig") == "진입 검토")
    print(f"[관심] 테마 {len(out_th)}개 · 종목 {sum(len(t['stocks']) for t in out_th)}개 · 진입 검토 {on}개", flush=True)
    return {"asOf": asof, "scoredAt": w.get("scoredAt"), "by": w.get("by"), "rules": w.get("rules"),
            "dart": bool(key), "kis": bool(kis), "themes": out_th, "score": score, "recent": sig[-15:][::-1]}
