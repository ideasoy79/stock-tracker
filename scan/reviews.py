"""
미뤄 둔 개선 항목을 '때가 됐을 때' 알려주는 서버 쪽 조건 (volume_scan.py 에서 호출)

조건이 처음 맞은 날을 scan/review_state.json 에 기록하고, latest.json 의 "reviews" 로 내보냅니다.
트래커 오늘 탭과 텔레그램 저녁 요약에 표시되고, 앱에서 '완료'나 '나중에'를 누르면 사라집니다.
"""
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(HERE, "review_state.json")
KEEP_DAYS = 45            # 처음 맞은 뒤 이 기간 동안 표시


def _load():
    try:
        with open(STATE, encoding="utf-8") as fp:
            return json.load(fp)
    except Exception:
        return {}


def check(tp, names, today):
    """tp = theme_flow.daily_payload 결과, names = {code: name}, today = 'YYYY-MM-DD'"""
    state = _load()
    pk = (tp or {}).get("picks") or {}
    hist = (pk.get("closed") or []) + (pk.get("open") or [])
    closed = pk.get("closed") or []
    sm = pk.get("summary") or {}
    found = []

    # S5·S4: 실전 기록이 6회 쌓이면 백테스트와 비교할 때
    if sm.get("n", 0) >= 6:
        found.append(("live6", "S5", "실전 기록 6회: 백테스트와 비교할 때",
                      f"실전 {sm['n']}회 회당 {sm['avg']:+.2f}% · 평균 대비 {sm['excess']:+.2f}%p (백테스트 +2.10% · +1.62%p). "
                      "차이가 크면 S5(편향 보정)·S4(시장 국면)를 먼저"))
    # S5: 실전이 백테스트보다 뚜렷이 약할 때 (4회 이상)
    if sm.get("n", 0) >= 4 and sm.get("excess", 0) < 0:
        found.append((f"weak-{sm['n']}", "S5", "실전 성적이 평균보다 약해요",
                      f"실전 {sm['n']}회 평균 대비 {sm['excess']:+.2f}%p · 비중을 줄이고 S5(백테스트 편향 보정) 검토"))
    # S6: 묶음 안에서 -15% 이상 빠진 종목이 나오면 공시 위험 필터 검토
    for h in hist:
        for c, r in (h.get("sr") or {}).items():
            if r is not None and r <= -15:
                found.append((f"loser-{h['date']}-{c}", "S6", "묶음 종목 급락: 공시 위험 필터 검토",
                              f"{names.get(c, c)} {r:+.1f}% ({h['date']} 묶음). 유상증자·CB·보호예수 해제 같은 공시가 있었는지 보고 S6(공시 위험 자동 제외)"))
    # S4: 시장 필터가 공격 ↔ 쉬기로 바뀐 날
    if "restPrev" in pk and pk.get("rest") != pk.get("restPrev"):
        now = "쉬는 구간" if pk.get("rest") else "공격 구간"
        found.append((f"flip-{today}", "S4", f"시장 필터가 {now}으로 바뀜",
                      f"테마 평균 60일 수익률 {pk.get('mkt60', 0):+.1f}%. 지수·신고가 수로 국면을 보강할지(S4) 볼 시점"))

    out, new = [], []
    for key, code, title, why in found:
        if key not in state:
            state[key] = today
            new.append(key)
    for key, code, title, why in found:
        out.append({"key": key, "code": code, "title": title, "why": why, "since": state[key], "new": key in new})
    # 오래된 상태 정리 (조건이 사라진 지 오래된 것)
    live = {k for k, *_ in found}
    for k in list(state):
        if k not in live and state[k] < _days_ago(today, KEEP_DAYS * 2):
            del state[k]
    with open(STATE, "w", encoding="utf-8") as fp:
        json.dump(state, fp, ensure_ascii=False, indent=0)
    return [x for x in out if x["since"] >= _days_ago(today, KEEP_DAYS)]


def _days_ago(today, n):
    from datetime import date, timedelta
    y, m, d = map(int, today.split("-"))
    return (date(y, m, d) - timedelta(days=n)).isoformat()
