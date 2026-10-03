#!/usr/bin/env python3
"""
생각콩 × AI 주식 트래커 — 매집 신호 백테스트

질문 하나에 답합니다: "거래량 급증 매집 신호를 규칙대로 사고팔았다면, 과거에 돈이 됐나?"

- 대상: 현재 상장 보통주 전체 (volume_scan.py 와 같은 목록·같은 시세 출처)
- 기간: 최근 약 3년 (BT_DAYS 거래일)
- 신호: 거래량 ≥ 직전 20일 평균 × 3, 거래대금 ≥ 50억
- 진입: 신호 다음날 시가 (신호일 종가에 사는 건 현실에서 어려우므로 보수적으로)
- 청산: 손절 / 목표 / 보유기간 만료 중 먼저 오는 것
    · 같은 날 손절과 목표가 모두 닿으면 손절로 처리 (보수적)
    · 시가가 손절가 아래로 갭하락하면 시가에 손절
- 비용: 왕복 BT_COST% (수수료+세금+슬리피지, 기본 0.35%)
- 한 종목은 보유 중에 다시 사지 않음

결과: scan/backtest.json  (트래커 '급증 점검' 화면에서 표로 보여줌)

주의
- 지금 상장된 종목만 대상이라 상장폐지 종목이 빠짐 → 결과가 실제보다 좋게 나올 수 있음(생존 편향)
- 기관·외국인 수급(6번)과 공시(7번)는 과거 데이터가 없어 검증 대상에서 빠짐
- 과거 성과가 미래 수익을 보장하지 않음
"""
import json
import os
import statistics as st
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import volume_scan as vs  # noqa: E402

BT_DAYS = int(os.environ.get("BT_DAYS", 760))       # 약 3년
COST = float(os.environ.get("BT_COST", 0.35))        # 왕복 비용 %
RATIO_MIN = float(os.environ.get("RATIO_MIN", 3.0))
AMOUNT_MIN = float(os.environ.get("AMOUNT_MIN", 5e9))
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "backtest.json")

# ── 비교할 신호 필터 (점검표 1~4번 조합) ──
FILTERS = {
    "기본(거래량 x3 + 50억)": lambda f: True,
    "+ 등락 0~3%": lambda f: 0 <= f["chg"] <= 3,
    "+ 등락 0~3% + 양봉": lambda f: 0 <= f["chg"] <= 3 and f["bull"],
    "+ 등락 0~3% + 양봉 + 바닥권": lambda f: 0 <= f["chg"] <= 3 and f["bull"] and f["bottom"],
    "1~4번 모두": lambda f: 0 <= f["chg"] <= 3 and f["bull"] and f["bottom"] and f["short_upper"],
    "바닥권만": lambda f: f["bottom"],
    "강한 상승일(+5% 이상)": lambda f: f["chg"] >= 5,
    "하락일(마이너스)": lambda f: f["chg"] < 0,
    "1~4번 + 거래량 x5": lambda f: 0 <= f["chg"] <= 3 and f["bull"] and f["bottom"] and f["short_upper"] and f["ratio"] >= 5,
}

# ── 비교할 매매 규칙 (손절 · 목표 · 최대 보유일) ──
EXITS = {
    "신호일저가 손절 · 목표+10% · 10일": {"stop": "siglow", "target": 10, "hold": 10},
    "신호일저가 손절 · 목표 없음 · 10일": {"stop": "siglow", "target": None, "hold": 10},
    "-7% 손절 · 목표+15% · 10일": {"stop": -7, "target": 15, "hold": 10},
    "-5% 손절 · 목표+10% · 5일": {"stop": -5, "target": 10, "hold": 5},
    "신호일저가 손절 · 목표+20% · 20일": {"stop": "siglow", "target": 20, "hold": 20},
}


def features(rows, i):
    r, pc = rows[i], (rows[i - 1]["c"] or rows[i]["c"])
    w60 = rows[max(0, i - 59):i + 1]
    w120 = rows[max(0, i - 119):i + 1]
    low60 = min(x["l"] for x in w60) or r["l"] or 1
    high120 = max(x["h"] for x in w120) or r["h"] or 1
    rng = r["h"] - r["l"]
    upper = (r["h"] - max(r["o"], r["c"])) / rng if rng else 0
    chg = (r["c"] / pc - 1) * 100
    from_low = (r["c"] / low60 - 1) * 100
    from_high = (r["c"] / high120 - 1) * 100
    return {"chg": chg, "bull": r["c"] >= r["o"], "bottom": from_low <= 20 or from_high <= -30,
            "short_upper": upper <= 0.3}


def simulate(rows, i, rule):
    """신호일 i → 다음날 시가 진입. (수익률%, 보유일, 청산사유) 또는 None"""
    if i + rule["hold"] > len(rows) - 1:   # 보유기간이 아직 안 끝난 최근 신호는 제외 (부분 결과로 왜곡 방지)
        return None
    entry = rows[i + 1]["o"]
    if entry <= 0:
        return None
    stop = rows[i]["l"] if rule["stop"] == "siglow" else entry * (1 + rule["stop"] / 100)
    if stop >= entry:                      # 시가가 이미 신호일 저가 아래 → 진입 안 함
        return None
    target = entry * (1 + rule["target"] / 100) if rule["target"] else None
    last = i + rule["hold"]
    for j in range(i + 1, last + 1):
        d = rows[j]
        if j > i + 1 and d["o"] <= stop:
            return (d["o"] / entry - 1) * 100 - COST, j - i, "갭손절"
        if d["l"] <= stop:
            return (stop / entry - 1) * 100 - COST, j - i, "손절"
        if target and d["h"] >= target:
            return (target / entry - 1) * 100 - COST, j - i, "목표"
    return (rows[last]["c"] / entry - 1) * 100 - COST, last - i, "기간만료"


def signals(rows):
    out = []
    for i in range(120, len(rows) - 1):
        prev = rows[i - 20:i]
        avg = sum(x["v"] for x in prev) / 20
        r = rows[i]
        if avg <= 0 or r["v"] == 0:
            continue
        ratio = r["v"] / avg
        amount = r.get("a") or r["c"] * r["v"]
        if ratio >= RATIO_MIN and amount >= AMOUNT_MIN:
            f = features(rows, i)
            f["ratio"] = ratio
            out.append((i, f))
    return out


def summarize(trades):
    if not trades:
        return None
    rets = [t["ret"] for t in trades]
    wins = [x for x in rets if x > 0]
    losses = [x for x in rets if x <= 0]
    gross_w, gross_l = sum(wins), -sum(losses)
    eq, peak, mdd = 0.0, 0.0, 0.0             # 신호 순서대로 1회씩 동일금액 투자했을 때 누적(%p) 기준 낙폭
    for t in sorted(trades, key=lambda x: x["date"]):
        eq += t["ret"]
        peak = max(peak, eq)
        mdd = min(mdd, eq - peak)
    by_year = {}
    for t in trades:
        by_year.setdefault(t["date"][:4], []).append(t["ret"])
    reasons = {}
    for t in trades:
        reasons[t["why"]] = reasons.get(t["why"], 0) + 1
    return {
        "n": len(trades),
        "win": round(len(wins) / len(trades) * 100, 1),
        "avg": round(st.mean(rets), 2),
        "median": round(st.median(rets), 2),
        "avgWin": round(st.mean(wins), 2) if wins else 0,
        "avgLoss": round(st.mean(losses), 2) if losses else 0,
        "pf": round(gross_w / gross_l, 2) if gross_l else None,
        "worst": round(min(rets), 2),
        "best": round(max(rets), 2),
        "hold": round(st.mean(t["days"] for t in trades), 1),
        "mddPts": round(mdd, 1),
        "byYear": {y: {"n": len(v), "avg": round(st.mean(v), 2), "win": round(sum(1 for x in v if x > 0) / len(v) * 100, 1)}
                   for y, v in sorted(by_year.items())},
        "exits": reasons,
    }


def main():
    t0 = time.time()
    vs.init_kis()
    universe = [s for s in vs.get_universe() if not vs.is_excluded(s["name"], s["code"])]
    print(f"[대상] {len(universe)}종목 · {BT_DAYS}거래일", flush=True)

    def load(s):
        rows = vs.fetch_daily(s["code"], BT_DAYS)          # 네이버: 한 번에 긴 기간
        if len(rows) < 200 and vs.kis:
            try:
                rows = vs.kis.daily(s["code"], BT_DAYS)
            except Exception:
                pass
        rows = [x for x in rows if min(x["o"], x["h"], x["l"], x["c"]) > 0]   # 0으로 찍힌 봉 제거
        return s, rows

    trades = {(fk, ek): [] for fk in FILTERS for ek in EXITS}
    base_fwd = []                                          # 기준선: 아무 날 아무 종목 다음날 시가 매수 → 10일 뒤 종가
    fails, done, first_date, last_date = 0, 0, None, None
    with ThreadPoolExecutor(max_workers=vs.WORKERS) as ex:
        futs = [ex.submit(load, s) for s in universe]
        for k, f in enumerate(as_completed(futs), 1):
            s, rows = f.result()
            if len(rows) < 200:
                fails += 1
                continue
            done += 1
            first_date = min(first_date or rows[0]["d"], rows[0]["d"])
            last_date = max(last_date or rows[-1]["d"], rows[-1]["d"])
            for i in range(120, len(rows) - 11, 23):       # 기준선 표본 (약 한 달 간격)
                e = rows[i + 1]["o"]
                if e > 0:
                    base_fwd.append((rows[i + 11]["c"] / e - 1) * 100 - COST)
            sigs = signals(rows)
            for fk, fn in FILTERS.items():
                picked = [(i, ft) for i, ft in sigs if fn(ft)]
                for ek, rule in EXITS.items():
                    busy_until = -1
                    for i, ft in picked:
                        if i <= busy_until:
                            continue
                        res = simulate(rows, i, rule)
                        if not res:
                            continue
                        ret, days, why = res
                        busy_until = i + days
                        trades[(fk, ek)].append({"code": s["code"], "name": s["name"], "date": rows[i]["d"],
                                                 "ret": round(ret, 2), "days": days, "why": why})
            if k % 300 == 0:
                print(f"  … {k}/{len(universe)}", flush=True)

    print(f"[시세] 성공 {done} · 실패 {fails}", flush=True)
    if done == 0:
        sys.exit("시세를 한 종목도 받지 못했습니다 → 네이버 접속이 막혔거나 주소가 바뀌었을 수 있어요 (KIS 키를 넣으면 KIS로 시도)")
    results = []
    for (fk, ek), tr in trades.items():
        sm = summarize(tr)
        if sm:
            # 표본 외 검증: 앞 2/3 기간 vs 뒤 1/3 기간
            tr_sorted = sorted(tr, key=lambda x: x["date"])
            cut = tr_sorted[int(len(tr_sorted) * 2 / 3)]["date"] if len(tr_sorted) >= 30 else None
            if cut:
                a = [t["ret"] for t in tr_sorted if t["date"] < cut]
                b = [t["ret"] for t in tr_sorted if t["date"] >= cut]
                sm["split"] = {"cut": cut, "before": round(st.mean(a), 2) if a else None, "after": round(st.mean(b), 2) if b else None}
            sm["filter"], sm["exit"] = fk, ek
            sm["recent"] = sorted(tr, key=lambda x: x["date"])[-8:]
            results.append(sm)
    results.sort(key=lambda x: (x["n"] >= 50, x["avg"]), reverse=True)

    base = {"n": len(base_fwd), "avg": round(st.mean(base_fwd), 2) if base_fwd else None,
            "win": round(sum(1 for x in base_fwd if x > 0) / len(base_fwd) * 100, 1) if base_fwd else None}
    out = {
        "kind": "sv4-backtest", "version": 1,
        "generatedAt": datetime.now(vs.KST).isoformat(timespec="seconds"),
        "period": {"from": first_date, "to": last_date, "days": BT_DAYS},
        "universe": done, "fetchFailed": fails,
        "params": {"ratioMin": RATIO_MIN, "amountMin": AMOUNT_MIN, "costPct": COST, "entry": "신호 다음날 시가"},
        "baseline": base,
        "caveats": ["현재 상장 종목만 대상(상장폐지 제외) → 실제보다 좋게 나올 수 있음",
                    "수급(6번)·공시(7번)는 과거 데이터가 없어 검증에서 제외",
                    "같은 날 손절·목표가 모두 닿으면 손절로 처리(보수적)",
                    "과거 성과는 미래 수익을 보장하지 않음"],
        "results": results,
    }
    with open(OUT, "w", encoding="utf-8") as fp:
        json.dump(out, fp, ensure_ascii=False, indent=1)
    print(f"[기준선] 아무 종목 10일 보유 평균 {base['avg']}% · 승률 {base['win']}%")
    for r in results[:8]:
        print(f"  {r['filter']} | {r['exit']} | n={r['n']} 승률 {r['win']}% 평균 {r['avg']}% PF {r['pf']}")
    print(f"[완료] {time.time()-t0:.0f}초 → {OUT}")


if __name__ == "__main__":
    main()
