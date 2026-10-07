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
    return {"date": dates[-1], "count": len(items),
            "leaders": items[:top],
            "rising": sorted([x for x in items if x["phase"] == "떠오름"], key=lambda x: x["rank"] - x["rankPrev"])[:12],
            "cooling": [x for x in items if x["phase"] == "식는 중"][:12],
            "hot": [x for x in items if x["phase"] == "과열 주의"][:12]}
