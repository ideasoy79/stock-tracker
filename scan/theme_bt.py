"""
테마 순환 백테스트 — backtest.py 에서 호출

질문 두 가지
 1) 어떤 테마를 사야 했나?  (모멘텀 / 반전 / 순위 급상승 / 자금 유입 / 과열)  — 매주 3개 테마를 골라 5·10일 보유
 2) 순환은 예측되나?        A테마가 강했던 다음 주에 B테마가 오르는 '선후 관계'를
                             앞 2/3 기간에서 찾고 → 뒤 1/3 기간에서도 맞는지 확인 (표본 외 검증)
"""
import statistics as st

import theme_flow as tf


def _rank(vals):
    order = sorted(vals, key=lambda kv: kv[1], reverse=True)
    n = max(len(order) - 1, 1)
    return {k: i / n for i, (k, _) in enumerate(order)}


def mcum(rr, a, b):
    x = 1.0
    for i in range(a + 1, b + 1):
        v = rr[i]
        if v is not None:
            x *= 1 + v
    return x - 1


def strategies(dates, idx, cost_pct, dret=None):
    names = list(idx)
    T = len(dates)
    out = []
    for hold in (5, 10):
        recs = {k: [] for k in ["모멘텀: 20일 상위 3테마", "단기 모멘텀: 5일 상위 3테마", "반전: 20일 하위 3테마",
                                "순위 급상승 3테마", "자금 유입 3테마", "과열 테마(5일 급등+자금 2배)", "기준선: 전체 테마 평균",
                                "모멘텀 3테마(과열 제외)", "모멘텀 3테마 · 테마별 강한 3종목", "모멘텀 3테마 + 시장 필터"]}
        prev_pick = {k: set() for k in recs}
        t = 70
        while t + hold < T:
            r20 = {n: tf.cum(idx[n]["ret"], t - 20, t) for n in names}
            r20p = {n: tf.cum(idx[n]["ret"], t - 25, t - 5) for n in names}
            r5 = {n: tf.cum(idx[n]["ret"], t - 5, t) for n in names}
            money = {}
            for n in names:
                a = idx[n]["amt"]
                money[n] = (sum(a[t - 4:t + 1]) / 5) / ((sum(a[t - 64:t - 4]) / 60) or 1)
            fwd = {n: tf.cum(idx[n]["ret"], t, t + hold) for n in names}
            rk, rkp = _rank(r20.items()), _rank(r20p.items())
            hot5 = sorted(r5.values())[int(len(r5) * 0.95)]
            picks = {
                "모멘텀: 20일 상위 3테마": sorted(names, key=lambda n: -r20[n])[:3],
                "단기 모멘텀: 5일 상위 3테마": sorted(names, key=lambda n: -r5[n])[:3],
                "반전: 20일 하위 3테마": sorted(names, key=lambda n: r20[n])[:3],
                "순위 급상승 3테마": sorted([n for n in names if r5[n] > 0], key=lambda n: rk[n] - rkp[n])[:3],
                "자금 유입 3테마": sorted([n for n in names if r5[n] > 0], key=lambda n: -money[n])[:3],
                "과열 테마(5일 급등+자금 2배)": [n for n in names if r5[n] >= hot5 and money[n] >= 2][:5],
            }
            hot = {n for n in names if r5[n] >= hot5 and money[n] >= 2}
            picks["모멘텀 3테마(과열 제외)"] = sorted([n for n in names if n not in hot], key=lambda n: -r20[n])[:3]
            mkt60 = st.mean(tf.cum(idx[n]["ret"], t - 60, t) for n in names)
            picks["모멘텀 3테마 + 시장 필터"] = picks["모멘텀: 20일 상위 3테마"] if mkt60 > 0 else []
            base = st.mean(fwd.values())
            # 테마별 강한 3종목 (실제로 살 수 있는 형태)
            if dret is not None:
                k = "모멘텀 3테마 · 테마별 강한 3종목"
                codes = []
                for n in picks["모멘텀: 20일 상위 3테마"]:
                    mem = sorted(idx[n]["members"], key=lambda c: -mcum(dret[c], t - 20, t))[:3]
                    codes += [c for c in mem if c not in codes]
                if codes:
                    turn = 1 - len(set(codes) & prev_pick[k]) / len(codes)
                    ret = st.mean(mcum(dret[c], t, t + hold) for c in codes) * 100 - cost_pct * turn
                    recs[k].append({"date": dates[t], "ret": ret, "ex": ret - base * 100, "picks": codes})
                    prev_pick[k] = set(codes)
            if not picks["모멘텀 3테마 + 시장 필터"]:
                recs["모멘텀 3테마 + 시장 필터"].append({"date": dates[t], "ret": 0.0, "ex": -base * 100, "picks": ["현금"]})
                prev_pick["모멘텀 3테마 + 시장 필터"] = set()
            for k, ps in picks.items():
                if not ps:
                    continue
                turn = 1 - len(set(ps) & prev_pick[k]) / len(ps)
                ret = st.mean(fwd[n] for n in ps) * 100 - cost_pct * turn
                recs[k].append({"date": dates[t], "ret": ret, "ex": ret - base * 100, "picks": ps})
                prev_pick[k] = set(ps)
            recs["기준선: 전체 테마 평균"].append({"date": dates[t], "ret": base * 100, "ex": 0.0, "picks": []})
            t += hold
        for k, rr in recs.items():
            if len(rr) < 10:
                continue
            rets = [x["ret"] for x in rr]
            cut = int(len(rr) * 2 / 3)
            eq, peak, mdd = 1.0, 1.0, 0.0
            for x in rets:
                eq *= 1 + x / 100
                peak = max(peak, eq)
                mdd = min(mdd, eq / peak - 1)
            out.append({
                "strategy": k, "hold": hold, "n": len(rr),
                "avg": round(st.mean(rets), 2), "win": round(sum(1 for x in rets if x > 0) / len(rets) * 100, 1),
                "excess": round(st.mean(x["ex"] for x in rr), 2),
                "beat": round(sum(1 for x in rr if x["ex"] > 0) / len(rr) * 100, 1),
                "total": round((eq - 1) * 100, 1), "mdd": round(mdd * 100, 1),
                "split": {"cut": rr[cut]["date"], "before": round(st.mean(x["ex"] for x in rr[:cut]), 2),
                          "after": round(st.mean(x["ex"] for x in rr[cut:]), 2)},
                "last": [{"date": x["date"], "ret": round(x["ret"], 2), "picks": x["picks"]} for x in rr[-4:]],
            })
    return out


def lead_lag(dates, idx, top_pairs=15):
    """주간 수익률로 'A가 강한 다음 주에 B가 오른다'를 찾고 표본 외 검증"""
    names = [n for n in idx]
    T = len(dates)
    weeks = list(range(70, T - 5, 5))
    W = {n: [tf.cum(idx[n]["ret"], t, t + 5) for t in weeks] for n in names}
    K = len(weeks)
    mkt = [st.mean(W[n][k] for n in names) for k in range(K)]
    X = {n: [W[n][k] - mkt[k] for k in range(K)] for n in names}       # 시장 대비 초과수익
    cut = int(K * 2 / 3)
    if cut < 20 or K - cut < 8:
        return None
    mem = {n: set(idx[n]["members"]) for n in names}

    def corr(a, b):
        ma, mb = st.mean(a), st.mean(b)
        va = sum((x - ma) ** 2 for x in a) ** .5
        vb = sum((y - mb) ** 2 for y in b) ** .5
        return sum((x - ma) * (y - mb) for x, y in zip(a, b)) / (va * vb) if va and vb else 0

    cands = []
    for a in names:
        xa = X[a][:cut - 1]
        for b in names:
            if a == b:
                continue
            inter = len(mem[a] & mem[b])
            if inter / max(min(len(mem[a]), len(mem[b])), 1) > 0.2:   # 종목이 겹치는 테마끼리는 제외
                continue
            c = corr(xa, X[b][1:cut])
            cands.append((c, a, b))
    cands.sort(reverse=True)
    pairs = cands[:top_pairs]
    # 표본 외: A가 앞 기간 기준 상위 20% 주간이면 다음 주 B를 산다
    hits, outs, ins, per = 0, [], [], []
    for c, a, b in pairs:
        thr = sorted(X[a][:cut])[int(cut * 0.8)]
        po = [X[b][k + 1] for k in range(cut, K - 1) if X[a][k] >= thr]
        per.append({"lead": a, "follow": b, "corr": round(c, 3), "oosN": len(po),
                    "oosAvg": round(st.mean(po) * 100, 2) if po else None})
        for k in range(cut - 1):
            if X[a][k] >= thr:
                ins.append(X[b][k + 1])
        for k in range(cut, K - 1):
            if X[a][k] >= thr:
                outs.append(X[b][k + 1])
                hits += X[b][k + 1] > 0
    return {
        "weeks": K, "cut": dates[weeks[cut]],
        "pairs": per,
        "inSample": {"n": len(ins), "avgExcess": round(st.mean(ins) * 100, 2) if ins else None},
        "outSample": {"n": len(outs), "avgExcess": round(st.mean(outs) * 100, 2) if outs else None,
                      "hit": round(hits / len(outs) * 100, 1) if outs else None},
    }


def run(keep, themes, cost_pct):
    if not themes:
        return None
    dates, idx, dret = tf.build(keep, themes)
    if len(dates) < 150 or len(idx) < 10:
        return None
    return {"themes": len(idx), "strategies": strategies(dates, idx, cost_pct, dret), "leadLag": lead_lag(dates, idx)}
