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


def strategies(dates, idx, cost_pct, dret=None, damt=None):
    names = list(idx)
    T = len(dates)
    out = []
    _px = {}

    def PX(c):   # 수익률을 누적한 상대 가격 (이동평균 계산용)
        if c not in _px:
            arr, p = [], None
            for v in dret[c]:
                if v is not None:
                    p = (p or 1.0) * (1 + v)
                arr.append(p)
            _px[c] = arr
        return _px[c]
    for hold in (5, 10):
        recs = {k: [] for k in ["모멘텀: 20일 상위 3테마", "단기 모멘텀: 5일 상위 3테마", "반전: 20일 하위 3테마",
                                "순위 급상승 3테마", "자금 유입 3테마", "과열 테마(5일 급등+자금 2배)", "기준선: 전체 테마 평균",
                                "모멘텀 3테마(과열 제외)", "모멘텀 3테마 · 테마별 강한 3종목", "모멘텀 3테마 + 시장 필터",
                                "시장 필터 + 20일선 위·이격 15% 이내 종목",
                                "실제 매수형: 시장 필터 + 테마별 거래대금 상위 5종목",
                                "실제 매수형 + 종목별 -15% 비상 손절"]}
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
            if dret is not None:
                k = "시장 필터 + 20일선 위·이격 15% 이내 종목"
                if mkt60 > 0:
                    codes = []
                    for n in picks["모멘텀: 20일 상위 3테마"]:
                        for c in idx[n]["members"]:
                            px = PX(c)
                            if px[t] is None or px[t - 25] is None:
                                continue
                            ma20 = sum(px[t - 19:t + 1]) / 20
                            ma20p = sum(px[t - 24:t - 4]) / 20
                            if px[t] >= ma20 and ma20 > ma20p and px[t] / ma20 - 1 <= 0.15 and c not in codes:
                                codes.append(c)
                    if codes:
                        turn = 1 - len(set(codes) & prev_pick[k]) / len(codes)
                        ret = st.mean(mcum(dret[c], t, t + hold) for c in codes) * 100 - cost_pct * turn
                        recs[k].append({"date": dates[t], "ret": ret, "ex": ret - base * 100, "picks": codes[:12]})
                        prev_pick[k] = set(codes)
                    else:
                        recs[k].append({"date": dates[t], "ret": 0.0, "ex": -base * 100, "picks": ["현금"]})
                        prev_pick[k] = set()
                else:
                    recs[k].append({"date": dates[t], "ret": 0.0, "ex": -base * 100, "picks": ["현금"]})
                    prev_pick[k] = set()
            # 트래커가 실제로 보여주는 형태: 테마별 최근 20일 거래대금 상위 5종목을 고르게
            if dret is not None and damt is not None:
                k1, k2 = "실제 매수형: 시장 필터 + 테마별 거래대금 상위 5종목", "실제 매수형 + 종목별 -15% 비상 손절"
                if mkt60 > 0:
                    codes = []
                    for n in picks["모멘텀: 20일 상위 3테마"]:
                        liq = lambda c: sum(damt[c][t - 19:t + 1])
                        for c in sorted(idx[n]["members"], key=lambda c: -liq(c))[:5]:
                            if c not in codes:
                                codes.append(c)
                    turn = 1 - len(set(codes) & prev_pick[k1]) / len(codes)

                    def stopped(c):
                        x = 1.0
                        for i in range(t + 1, t + hold + 1):
                            v = dret[c][i]
                            if v is not None:
                                x *= 1 + v
                            if x <= 0.85:          # 종가 기준 -15% 닿은 날 정리 (갭 하락이면 그보다 더 빠질 수 있음)
                                break
                        return x - 1
                    r1 = st.mean(mcum(dret[c], t, t + hold) for c in codes) * 100 - cost_pct * turn
                    r2 = st.mean(stopped(c) for c in codes) * 100 - cost_pct * turn
                    recs[k1].append({"date": dates[t], "ret": r1, "ex": r1 - base * 100, "picks": codes[:15]})
                    recs[k2].append({"date": dates[t], "ret": r2, "ex": r2 - base * 100, "picks": codes[:15]})
                    prev_pick[k1] = prev_pick[k2] = set(codes)
                else:
                    for k in (k1, k2):
                        recs[k].append({"date": dates[t], "ret": 0.0, "ex": -base * 100, "picks": ["현금"]})
                        prev_pick[k] = set()
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


SURGE_GROUPS = ["급증 전체", "급증 + 상위 10% 테마 소속", "급증 + 상위 10% 테마 + 20일선 위·상승",
                "급증 + 상위 10% 테마 + 20일선 위·상승 + 시장 필터", "급증 + 테마 하위 50%"]


def surge_test(keep, dates, idx, cost_pct, ratio_min=3.0, amount_min=5e9, hold=10):
    """거래량 급증 신호를 '그날 강한 테마에 속했는지'로 나눠서 10거래일 보유 성과 비교
    (진입: 다음날 시가 · 청산: 10거래일째 종가 · 비용 반영 · 같은 종목 보유 중 신호는 건너뜀)"""
    names = list(idx)
    T = len(dates)
    P = {}
    for n in names:
        x, arr = 1.0, []
        for v in idx[n]["ret"]:
            x *= 1 + v
            arr.append(x)
        P[n] = arr
    rank_cache, mkt_cache = {}, {}

    def ranks(t):
        if t not in rank_cache:
            rank_cache[t] = _rank([(n, P[n][t] / P[n][t - 20]) for n in names])
        return rank_cache[t]

    def mkt(t):
        if t not in mkt_cache:
            mkt_cache[t] = st.mean(P[n][t] / P[n][t - 60] - 1 for n in names)
        return mkt_cache[t]
    by_code = {}
    for n in names:
        for c in idx[n]["members"]:
            by_code.setdefault(c, []).append(n)
    pos = {d: i for i, d in enumerate(dates)}
    cut = dates[int(T * 2 / 3)]
    recs = {g: [] for g in SURGE_GROUPS}
    for code, rows in keep.items():
        if "v" not in rows[0]:
            return None
        busy = {g: -1 for g in SURGE_GROUPS}
        for i in range(60, len(rows) - hold - 1):
            r = rows[i]
            avg = sum(x["v"] for x in rows[i - 20:i]) / 20
            if avg <= 0 or r["v"] < avg * ratio_min or (r.get("a") or 0) < amount_min:
                continue
            t = pos.get(r["d"])
            if t is None or t < 70:
                continue
            e = rows[i + 1]["o"]
            if not e or e <= 0:
                continue
            ret = (rows[i + hold]["c"] / e - 1) * 100 - cost_pct
            if abs(ret) > 80:
                continue
            rk = ranks(t)
            best = min((rk[n] for n in by_code.get(code, [])), default=None)
            cl = [x["c"] for x in rows[i - 24:i + 1]]
            ma20, ma20p = sum(cl[-20:]) / 20, sum(cl[:20]) / 20
            up = cl[-1] > ma20 and ma20 > ma20p
            hit = {"급증 전체": True,
                   "급증 + 상위 10% 테마 소속": best is not None and best <= 0.10,
                   "급증 + 테마 하위 50%": best is not None and best >= 0.50}
            hit["급증 + 상위 10% 테마 + 20일선 위·상승"] = hit["급증 + 상위 10% 테마 소속"] and up
            hit["급증 + 상위 10% 테마 + 20일선 위·상승 + 시장 필터"] = hit["급증 + 상위 10% 테마 + 20일선 위·상승"] and mkt(t) > 0
            for g, ok in hit.items():
                if ok and i > busy[g]:
                    recs[g].append((r["d"], ret))
                    busy[g] = i + hold
    out = []
    for g in SURGE_GROUPS:
        v = recs[g]
        if not v:
            continue
        rets = [x for _, x in v]
        a = [x for d, x in v if d < cut]
        b = [x for d, x in v if d >= cut]
        eq, peak, mdd = 0.0, 0.0, 0.0
        for _, x in sorted(v):
            eq += x
            peak = max(peak, eq)
            mdd = min(mdd, eq - peak)
        out.append({"group": g, "n": len(v), "avg": round(st.mean(rets), 2), "median": round(st.median(rets), 2),
                    "win": round(sum(1 for x in rets if x > 0) / len(rets) * 100, 1),
                    "split": {"cut": cut, "before": round(st.mean(a), 2) if a else None, "after": round(st.mean(b), 2) if b else None}})
    return {"hold": hold, "groups": out}


def _ctx(dates, idx):
    """테마 순위(20일 수익률 백분위)·시장 필터를 날짜별로 꺼내 쓰는 도우미"""
    names = list(idx)
    P = {}
    for n in names:
        x, arr = 1.0, []
        for v in idx[n]["ret"]:
            x *= 1 + v
            arr.append(x)
        P[n] = arr
    rc, mc = {}, {}

    def ranks(t):
        if t not in rc:
            rc[t] = _rank([(n, P[n][t] / P[n][t - 20]) for n in names])
        return rc[t]

    def mkt(t):
        if t not in mc:
            mc[t] = st.mean(P[n][t] / P[n][t - 60] - 1 for n in names)
        return mc[t]
    by_code = {}
    for n in names:
        for c in idx[n]["members"]:
            by_code.setdefault(c, []).append(n)
    return ranks, mkt, by_code, {d: i for i, d in enumerate(dates)}


PB_GROUPS = ["재상승 신호 전체", "재상승 + 상위 10% 테마", "재상승 + 상위 10% 테마 + 시장 필터", "재상승 + 되돌림 38~62%"]


def pullback_test(keep, dates, idx, cost_pct):
    """급등 → 되돌림 → 재상승 신호의 과거 성과. 두 가지 청산을 비교:
    목표형 = 이전 고점 닿으면 매도 / 추세형 = 고점 넘어서도 들고 가다 종가가 5일선 아래면 매도 (둘 다 손절은 되돌림 저점, 최대 10일)"""
    import pullback as pb
    ranks, mkt, by_code, pos = _ctx(dates, idx)
    cut = dates[int(len(dates) * 2 / 3)]
    recs = {(g, ex): [] for g in PB_GROUPS for ex in ("target", "trend")}
    reach7 = {g: [0, 0] for g in PB_GROUPS}
    base = [0, 0]                                   # 되돌림만 보고(신호 없이) 7일 안에 고점 회복한 비율
    for code, rows in keep.items():
        if "h" not in rows[0]:
            return None
        busy = {g: -1 for g in PB_GROUPS}
        seen_peak = set()
        for i in range(46, len(rows) - 11):
            stp = pb._setup(rows, i)
            if stp and stp["peak"] not in seen_peak:
                seen_peak.add(stp["peak"])
                base[1] += 1
                base[0] += any(rows[k]["h"] >= stp["H"] for k in range(i + 1, i + 8))
            sig = pb.trigger(rows, i)
            if not sig:
                continue
            t = pos.get(rows[i]["d"])
            if t is None or t < 70:
                continue
            best = min((ranks(t)[n] for n in by_code.get(code, [])), default=None)
            strong = best is not None and best <= 0.10
            hit = {"재상승 신호 전체": True, "재상승 + 상위 10% 테마": strong,
                   "재상승 + 상위 10% 테마 + 시장 필터": strong and mkt(t) > 0,
                   "재상승 + 되돌림 38~62%": 0.38 <= sig["depth"] <= 0.62}
            r7 = any(rows[k]["h"] >= sig["H"] for k in range(i + 1, i + 8))
            for g, ok in hit.items():
                if not ok or i <= busy[g]:
                    continue
                reach7[g][1] += 1
                reach7[g][0] += r7
                hold_until = i
                for ex in ("target", "trend"):
                    res = pb.trade(rows, i, sig, cost_pct, ex)
                    if res and abs(res[0]) < 80:
                        recs[(g, ex)].append((rows[i]["d"], res[0], res[1], res[2]))
                        hold_until = max(hold_until, i + res[1])
                busy[g] = hold_until
    out = []
    for g in PB_GROUPS:
        for ex in ("target", "trend"):
            v = recs[(g, ex)]
            if not v:
                continue
            rets = [x[1] for x in v]
            a = [x[1] for x in v if x[0] < cut]
            b = [x[1] for x in v if x[0] >= cut]
            wins, losses = [x for x in rets if x > 0], [x for x in rets if x <= 0]
            why = {}
            for x in v:
                why[x[3]] = why.get(x[3], 0) + 1
            out.append({"group": g, "exit": "이전 고점에서 매도" if ex == "target" else "5일선 깨질 때까지 보유",
                        "n": len(v), "avg": round(st.mean(rets), 2), "win": round(len(wins) / len(v) * 100, 1),
                        "pf": round(sum(wins) / -sum(losses), 2) if losses and sum(losses) < 0 else None,
                        "hold": round(st.mean(x[2] for x in v), 1), "why": why,
                        "reach7": round(reach7[g][0] / reach7[g][1] * 100, 1) if reach7[g][1] else None,
                        "split": {"cut": cut, "before": round(st.mean(a), 2) if a else None, "after": round(st.mean(b), 2) if b else None}})
    return {"groups": out, "baseReach7": round(base[0] / base[1] * 100, 1) if base[1] else None, "baseN": base[1],
            "params": {"rise": pb.RISE_MIN, "depth": [pb.DEPTH_MIN, pb.DEPTH_MAX], "volX": pb.VOL_X}}


def run(keep, themes, cost_pct, ratio_min=3.0, amount_min=5e9):
    if not themes:
        return None
    dates, idx, dret = tf.build(keep, themes)
    if len(dates) < 150 or len(idx) < 10:
        return None
    pos = {d: i for i, d in enumerate(dates)}
    damt = {}
    for c, rows in keep.items():
        arr = [0.0] * len(dates)
        for r in rows:
            if r["d"] in pos:
                arr[pos[r["d"]]] = float(r.get("a") or 0)
        damt[c] = arr
    res = {"themes": len(idx), "strategies": strategies(dates, idx, cost_pct, dret, damt), "leadLag": lead_lag(dates, idx)}
    try:
        res["surge"] = surge_test(keep, dates, idx, cost_pct, ratio_min, amount_min)
    except Exception as e:
        print(f"[테마] 급증×테마 검증 실패: {e}", flush=True)
    try:
        res["pullback"] = pullback_test(keep, dates, idx, cost_pct)
    except Exception as e:
        print(f"[테마] 눌림 재상승 검증 실패: {e}", flush=True)
    return res
