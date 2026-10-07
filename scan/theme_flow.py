"""
테마 흐름 계산 (volume_scan.py · backtest.py 공용)

테마 지수 = 소속 종목 일간 수익률의 단순평균 (매일 재조정한 동일가중 바스켓)
- ret5 / ret20 : 최근 5·20거래일 테마 수익률
- breadth5     : 5일간 오른 소속 종목 비율
- money        : 최근 5일 거래대금 ÷ 직전 60일 평균 거래대금 (1.0 = 평소, 2.0 = 평소의 2배)
- rank / rankPrev : 20일 수익률 순위 백분위(0=1등) — 오늘 vs 5거래일 전
"""
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))


def load_themes():
    try:
        with open(os.path.join(HERE, "themes.json"), encoding="utf-8") as fp:
            return json.load(fp)
    except Exception:
        return {}


def build(rows_by_code, themes, min_members=3):
    """rows_by_code: {code: [{d,c,a|v}, ...]} → (dates, {theme: {"ret": [...], "amt": [...], "n": [...]}}, member_ret)"""
    dates = sorted({r["d"] for rows in rows_by_code.values() for r in rows})
    pos = {d: i for i, d in enumerate(dates)}
    T = len(dates)
    # 종목별 일간 수익률·거래대금 (날짜 정렬)
    dret, damt = {}, {}
    for code, rows in rows_by_code.items():
        rr, aa = [None] * T, [0.0] * T
        prev = None
        for r in rows:
            i = pos[r["d"]]
            if prev and prev > 0:
                ch = r["c"] / prev - 1
                if abs(ch) < 0.35:                      # 액면분할 등 이상치 제외
                    rr[i] = ch
            aa[i] = float(r.get("a") or r["c"] * r.get("v", 0))
            prev = r["c"]
        dret[code], damt[code] = rr, aa
    idx = {}
    for name, codes in themes.items():
        mem = [c for c in codes if c in dret]
        if len(mem) < min_members:
            continue
        ret, amt, cnt = [0.0] * T, [0.0] * T, [0] * T
        for c in mem:
            rr, aa = dret[c], damt[c]
            for i in range(T):
                if rr[i] is not None:
                    ret[i] += rr[i]
                    cnt[i] += 1
                amt[i] += aa[i]
        ret = [ret[i] / cnt[i] if cnt[i] else 0.0 for i in range(T)]
        idx[name] = {"ret": ret, "amt": amt, "n": cnt, "members": mem}
    return dates, idx, dret


def cum(ret, a, b):
    """ret[a+1..b] 누적수익률 (a<b)"""
    x = 1.0
    for i in range(a + 1, b + 1):
        x *= 1 + ret[i]
    return x - 1


def snapshot(dates, idx, dret, at=None):
    """at 시점(기본: 마지막 날) 테마별 지표 + 국면"""
    T = len(dates)
    t = T - 1 if at is None else at
    if t < 66:
        return []
    rows = []
    for name, s in idx.items():
        r5, r20 = cum(s["ret"], t - 5, t), cum(s["ret"], t - 20, t)
        r20p = cum(s["ret"], t - 25, t - 5)
        a5 = sum(s["amt"][t - 4:t + 1]) / 5
        a60 = sum(s["amt"][t - 64:t - 4]) / 60 or 1
        ups = 0
        for c in s["members"]:
            x = 1.0
            for i in range(t - 4, t + 1):
                if dret[c][i] is not None:
                    x *= 1 + dret[c][i]
            ups += x > 1
        rows.append({"theme": name, "n": len(s["members"]), "ret5": r5, "ret20": r20, "ret20prev": r20p,
                     "breadth5": ups / len(s["members"]), "money": a5 / a60})
    if not rows:
        return []
    for key, rk in (("ret20", "rank"), ("ret20prev", "rankPrev")):
        order = sorted(rows, key=lambda x: x[key], reverse=True)
        for i, x in enumerate(order):
            x[rk] = i / max(len(order) - 1, 1)
    r5s = sorted(x["ret5"] for x in rows)
    hot5 = r5s[int(len(r5s) * 0.95)] if r5s else 0
    for x in rows:
        ph = ""
        if x["ret5"] >= hot5 and x["money"] >= 2.0:
            ph = "과열 주의"
        elif x["rank"] <= 0.10 and x["ret5"] > 0:
            ph = "주도"
        elif x["rankPrev"] - x["rank"] >= 0.30 and x["ret5"] > 0:
            ph = "떠오름"
        elif x["rankPrev"] <= 0.20 and x["ret5"] < 0:
            ph = "식는 중"
        x["phase"] = ph
    return rows


def daily_payload(rows_by_code, themes, top=40):
    """latest.json 에 넣을 테마 흐름 요약"""
    if not themes:
        return None
    dates, idx, dret = build(rows_by_code, themes)
    snap = snapshot(dates, idx, dret)
    if not snap:
        return None
    snap.sort(key=lambda x: x["rank"])
    r = lambda v: round(v * 100, 2)
    items = [{"theme": x["theme"], "n": x["n"], "ret5": r(x["ret5"]), "ret20": r(x["ret20"]),
              "breadth5": round(x["breadth5"] * 100), "money": round(x["money"], 2),
              "rank": round(x["rank"] * 100), "rankPrev": round(x["rankPrev"] * 100), "phase": x["phase"]} for x in snap]
    picks = update_picks(dates, idx, dret, snap, rows_by_code)
    return {"date": dates[-1], "count": len(items), "picks": picks,
            "ranks": {x["theme"]: x["rank"] for x in items},
            "leaders": items[:top],
            "rising": sorted([x for x in items if x["phase"] == "떠오름"], key=lambda x: x["rank"] - x["rankPrev"])[:12],
            "cooling": [x for x in items if x["phase"] == "식는 중"][:12],
            "hot": [x for x in items if x["phase"] == "과열 주의"][:12]}


# ───────── 모멘텀 3테마: 오늘의 후보 + 전진 검증(실전 성적) ─────────
HOLD = 10
PICKS_FILE = os.path.join(HERE, "theme_picks.json")


def _mcum(rr, a, b):
    x = 1.0
    for i in range(a + 1, b + 1):
        v = rr[i]
        if v is not None:
            x *= 1 + v
    return x - 1


def update_picks(dates, idx, dret, snap, rows_by_code=None):
    """백테스트에서 가장 나은 규칙(20일 상위 3테마 + 시장 필터 · 10거래일 보유)을 매일 적용하고,
    10거래일마다 새로 고른 묶음의 실제 결과를 theme_picks.json 에 쌓는다."""
    try:
        hist = json.load(open(PICKS_FILE, encoding="utf-8"))
    except Exception:
        hist = []
    pos = {d: i for i, d in enumerate(dates)}
    t = len(dates) - 1
    names = list(idx)
    base_all = lambda a, b: sum(cum(idx[n]["ret"], a, b) for n in names) / len(names)
    # 열린 기록 평가
    for h in hist:
        if h.get("status") == "closed" or h["date"] not in pos:
            continue
        i0 = pos[h["date"]]
        days = t - i0
        end = min(t, i0 + HOLD)
        th = [n for n in h["themes"] if n in idx]
        if not th or days <= 0:
            continue
        h["ret"] = round(sum(cum(idx[n]["ret"], i0, end) for n in th) / len(th) * 100, 2)
        h["base"] = round(base_all(i0, end) * 100, 2)
        st_codes = [c for c in h.get("stocks", []) if c in dret]
        if st_codes:
            h["stocksRet"] = round(sum(_mcum(dret[c], i0, end) for c in st_codes) / len(st_codes) * 100, 2)
        h["days"] = min(days, HOLD)
        if days >= HOLD:
            h["status"], h["closedAt"] = "closed", dates[end]
    # 시장 필터: 전체 테마 평균 60일 수익률이 플러스일 때만 산다 (마이너스면 쉬기)
    mkt60 = base_all(t - 60, t) if t >= 60 else 0
    rest = mkt60 <= 0
    # 오늘의 후보: 20일 수익률 상위 3테마. 종목은 '가장 많이 오른 것'이 아니라 거래대금이 큰 순 5개
    # (백테스트에서 테마별 급등 3종목만 사면 최대 낙폭이 -54%로 커져서, 테마 전체를 고르게 담는 쪽이 낫다)
    def liq(c):
        rows = (rows_by_code or {}).get(c) or []
        return sum(r.get("a", 0) for r in rows[-20:]) / max(len(rows[-20:]), 1)
    cand = [] if rest else sorted(snap, key=lambda x: -x["ret20"])[:3]
    today = []
    for x in cand:
        mem = sorted(idx[x["theme"]]["members"], key=lambda c: -liq(c))[:5]
        today.append({"theme": x["theme"], "ret20": round(x["ret20"] * 100, 2), "ret5": round(x["ret5"] * 100, 2),
                      "phase": x["phase"], "n": len(idx[x["theme"]]["members"]),
                      "top": [chart_note(c, rows_by_code, dret, t) for c in mem]})
    # 새 묶음 기록: 열린 기록이 없을 때만 (10거래일마다 교체)
    if today and not any(h.get("status") == "open" for h in hist):
        hist.append({"date": dates[t], "themes": [x["theme"] for x in today],
                     "stocks": [m["code"] for x in today for m in x["top"]], "status": "open", "days": 0})
    hist = hist[-60:]
    with open(PICKS_FILE, "w", encoding="utf-8") as fp:
        json.dump(hist, fp, ensure_ascii=False, indent=0)
    closed = [h for h in hist if h.get("status") == "closed"]
    summary = None
    if closed:
        ex = [h["ret"] - h["base"] for h in closed]
        summary = {"n": len(closed), "avg": round(sum(h["ret"] for h in closed) / len(closed), 2),
                   "excess": round(sum(ex) / len(ex), 2), "beat": round(sum(1 for e in ex if e > 0) / len(ex) * 100)}
    return {"rule": "20일 수익률 상위 3테마 · 시장 필터 · 10거래일 보유", "rest": rest, "mkt60": round(mkt60 * 100, 2), "today": today,
            "open": [h for h in hist if h.get("status") == "open"], "closed": closed[-10:], "summary": summary}


def chart_note(code, rows_by_code, dret, t):
    """종가 기준 간단한 차트 상태와 매수 검토 코멘트 (규칙 기반 · 판단 보조용)"""
    rows = (rows_by_code or {}).get(code) or []
    cl = [r["c"] for r in rows]
    out = {"code": code, "ret20": round(_mcum(dret[code], t - 20, t) * 100, 1)}
    if len(cl) < 25:
        return out
    c = cl[-1]
    ma5 = sum(cl[-5:]) / 5
    ma20 = sum(cl[-20:]) / 20
    ma20p = sum(cl[-25:-5]) / 20
    gap = (c / ma20 - 1) * 100
    rising = ma20 > ma20p
    stop = round(min(ma20, c * 0.93))
    last, prev = rows[-1], rows[-2]
    o, h, l = last.get("o") or c, last.get("h") or c, last.get("l") or c
    chg = (c / prev["c"] - 1) * 100 if prev["c"] else 0
    rng = h - l
    wick = (h - max(o, c)) / rng if rng > 0 else 0                 # 윗꼬리 비율
    off_high = (1 - c / h) * 100 if h else 0                        # 오늘 고가 대비 밀린 폭
    hh20 = max((r.get("h") or r["c"]) for r in rows[-20:-1])
    new_high = h >= hh20
    if c < ma20:
        state, note = "보류", "20일선 아래로 내려왔어요. 테마가 강해도 이 종목은 추세가 꺾인 상태라 보류."
    elif not rising:
        state, note = "보류", "20일선이 아직 내려가는 중이에요. 다시 올라서는지 확인한 뒤에."
    elif new_high and (off_high >= 5 or (wick >= 0.5 and c < o)):
        state, note = "고점 반락", (f"오늘 20일 최고가({round(h):,}원)를 찍고 {off_high:.0f}% 밀려 마감했어요(윗꼬리). 단기 고점 신호일 수 있어 바로 사지 말고, "
                                 f"며칠 쉬다가 '양봉 + 전날 고가 위 마감'(재상승 신호)이 나올 때. 오늘 저가 {round(l):,}원이 깨지면 보류.")
    elif chg <= -5:
        state, note = "급락 관망", f"하루 {chg:.1f}% 빠졌어요. 5일선 근처라도 '쉬는' 게 아니라 매도가 쏟아진 날이라 1~2일 더 확인한 뒤에."
    elif gap > 15:
        state, note = "눌림 대기", f"20일선보다 {gap:.0f}% 위라 많이 떠 있어요. 추격 대신 5일선({round(ma5):,}원) 근처로 조용히 눌릴 때 분할로."
    elif c <= ma5 * 1.01 and chg > -3 and wick < 0.5:
        state, note = "진입 검토", f"20일선 위 상승 추세에서 5일선 근처까지 조용히 쉬었어요. 분할 진입을 검토할 자리 · 손절 {stop:,}원."
    else:
        state, note = "추세 양호", f"20일선 위 상승 추세예요. 5일선({round(ma5):,}원)까지 조용히 눌릴 때 분할 진입이 유리 · 손절 {stop:,}원."
    out.update({"close": c, "ma5": round(ma5), "ma20": round(ma20), "gap": round(gap, 1),
                "ret5": round((c / cl[-6] - 1) * 100, 1), "chg": round(chg, 1), "offHigh": round(off_high, 1),
                "state": state, "note": note, "stop": stop})
    return out
