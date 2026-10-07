"""
급등 → 되돌림 → 재상승 패턴 (volume_scan.py · theme_bt.py 공용)

1) 급등 구간: 최근 저점 L → 고점 H 가 +20% 이상, 그 사이 거래량이 평소(20일 평균)의 3배 이상 터진 날이 있음
2) 되돌림: 고점 뒤 1~8거래일, 오른 폭의 25~65%를 반납 · 거래량은 급등 때의 절반 이하로 줄어듦 · 시작점(L) 근처까지는 안 무너짐
3) 재상승 신호: 되돌림 중 처음으로 '양봉 + 전날 고가 돌파 종가'가 나온 날
   → 다음날 시가 진입 · 손절 = 되돌림 저점 · 1차 목표 = 이전 고점 H
"""

RISE_MIN = 0.20      # 급등 폭 (저점 → 고점)
DEPTH_MIN = 0.25     # 되돌림 최소 (오른 폭 대비)
DEPTH_MAX = 0.65     # 되돌림 최대
VOL_X = 3.0          # 급등 구간 거래량 배수
DRY = 0.5            # 되돌림 중 평균 거래량 ≤ 급등 최대 거래량 × DRY
PEAK_WIN = 8         # 고점 이후 최대 경과일


def _setup(rows, i):
    """i일 종가 기준 '되돌림 진행 중' 상태면 dict, 아니면 None"""
    if i < 45:
        return None
    hh = max(x["h"] for x in rows[i - PEAK_WIN:i])               # 빠른 거르기: 최근에 +20% 급등 자체가 없으면 끝
    ll = min(x["l"] for x in rows[i - PEAK_WIN - 10:i])
    if ll <= 0 or hh / ll - 1 < RISE_MIN:
        return None
    best = None
    for p in range(i - 1, max(i - PEAK_WIN, 30) - 1, -1):        # 고점 후보 (가까운 날부터)
        H = rows[p]["h"]
        if any(rows[k]["h"] > H for k in range(p + 1, i + 1)):    # 그 뒤에 더 높은 고가가 있으면 고점 아님
            continue
        if any(rows[k]["h"] > H for k in range(max(p - 20, 0), p)):
            continue                                               # 20일 신고가 수준의 고점만
        s = min(range(max(p - 10, 1), p + 1), key=lambda k: rows[k]["l"])
        L = rows[s]["l"]
        if L <= 0 or H / L - 1 < RISE_MIN:
            continue
        base = rows[max(s - 20, 0):s] or rows[:1]
        avg = sum(x["v"] for x in base) / len(base)
        vmax = max(x["v"] for x in rows[s:p + 1])
        if avg <= 0 or vmax < avg * VOL_X:
            continue
        low = min(rows[k]["l"] for k in range(p + 1, i + 1))
        depth = (H - low) / (H - L)
        if not (DEPTH_MIN <= depth <= DEPTH_MAX):
            continue
        dry = sum(rows[k]["v"] for k in range(p + 1, i + 1)) / (i - p)
        if dry > vmax * DRY:
            continue
        best = {"peak": p, "start": s, "H": H, "L": L, "low": low, "depth": depth, "days": i - p,
                "rise": H / L - 1, "dry": dry / vmax}
        break
    return best


def trigger(rows, i):
    """i일에 재상승 신호가 나왔으면 setup dict(+trigger 정보), 아니면 None"""
    if i < 46:
        return None
    r, y = rows[i], rows[i - 1]
    if r["c"] <= r["o"] or r["c"] <= y["h"]:       # 오늘 양봉 + 전날 고가 위 마감
        return None
    st = _setup(rows, i - 1)                       # 어제까지 되돌림 상태였고
    if not st or st["days"] < 1:
        return None
    if r["h"] >= st["H"]:                          # 이미 고점 회복 → 신호 아님 (늦음)
        return None
    # 오늘이 '첫' 재상승인지: 되돌림 기간 중 같은 조건이 이미 있었으면 제외
    for k in range(st["peak"] + 2, i):
        if rows[k]["c"] > rows[k]["o"] and rows[k]["c"] > rows[k - 1]["h"]:
            return None
    st = dict(st)
    st["low"] = min(st["low"], r["l"])
    return st


def state(rows):
    """마지막 날 기준: ('trigger', info) / ('setup', info) / (None, None)"""
    n = len(rows)
    if n < 50:
        return None, None
    t = trigger(rows, n - 1)
    if t:
        return "trigger", t
    s = _setup(rows, n - 1)
    if s:
        return "setup", s
    return None, None


def trade(rows, i, sig, cost_pct, exit_rule="target", max_hold=10):
    """신호일 i → 다음날 시가 진입. 반환 (수익률%, 보유일, 사유, 고점도달 여부) 또는 None
    exit_rule: 'target' = 이전 고점 도달 시 매도 / 'trend' = 고점 도달 후에도 보유, 종가가 5일선 아래면 매도"""
    if i + max_hold >= len(rows):
        return None
    e = rows[i + 1]["o"]
    stop, H = sig["low"], sig["H"]
    if e <= 0 or e <= stop:
        return None
    reached = False
    for j in range(i + 1, i + max_hold + 1):
        d = rows[j]
        if j > i + 1 and d["o"] <= stop:
            return (d["o"] / e - 1) * 100 - cost_pct, j - i, "손절", reached
        if d["l"] <= stop:
            return (stop / e - 1) * 100 - cost_pct, j - i, "손절", reached
        if d["h"] >= H:
            if not reached and exit_rule == "target":
                px = max(H, d["o"])
                return (px / e - 1) * 100 - cost_pct, j - i, "고점 도달", True
            reached = True
        if exit_rule == "trend" and j >= i + 3:
            ma5 = sum(x["c"] for x in rows[j - 4:j + 1]) / 5
            if d["c"] < ma5:
                return (d["c"] / e - 1) * 100 - cost_pct, j - i, "5일선 이탈", reached
    last = rows[i + max_hold]
    return (last["c"] / e - 1) * 100 - cost_pct, max_hold, "기간만료", reached
