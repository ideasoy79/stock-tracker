#!/usr/bin/env python3
"""
텔레그램 알림 — python scan/notify.py evening | intraday | test

evening  : 저녁 스캔 직후 3~6줄 요약 (시장 상태 · 테마 묶음 · 할 일 · 손절 근접 · 새 개선 알림)
intraday : 장중 30분마다 보유 종목 손절선 이탈 점검 + 테마 묶음 정리일 아침 알림 (같은 건 하루 1번만)
test     : 연결 확인 메시지

필요한 GitHub Secrets
  TELEGRAM_BOT_TOKEN  (필수) BotFather 에서 받은 봇 토큰
  FIREBASE_SA         (선택) Firebase 서비스 계정 JSON 전체. 있으면 앱 설정에 텔레그램 ID를 넣은 사람 모두에게 보내고,
                      보유 종목 손절 점검도 합니다. 없으면 TELEGRAM_CHAT_ID 한 명에게 저녁 요약만 보냅니다.
  TELEGRAM_CHAT_ID    (선택) FIREBASE_SA 없이 쓸 때 받을 사람
"""
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
KST = timezone(timedelta(hours=9))
APP_URL = "https://ideasoy79.github.io/stock-tracker/"
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
CODE_RE = re.compile(r"\b(\d{6})\b")


def send(chat, text):
    if not TOKEN or not chat:
        return False
    try:
        r = requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage", timeout=15,
                          json={"chat_id": chat, "text": text, "disable_web_page_preview": True})
        ok = r.ok and r.json().get("ok")
        if not ok:
            print(f"  전송 실패 {chat}: {r.text[:120]}", flush=True)
        return ok
    except Exception as e:
        print(f"  전송 오류 {chat}: {e}", flush=True)
        return False


def latest():
    try:
        with open(os.path.join(HERE, "latest.json"), encoding="utf-8") as fp:
            return json.load(fp)
    except Exception:
        return {}


def price_now(code):
    """네이버 일봉 마지막 봉 (장중이면 현재가). (날짜, 가격) 또는 (None, None)"""
    try:
        r = requests.get(f"https://fchart.stock.naver.com/sise.nhn?symbol={code}&timeframe=day&count=2&requestType=0",
                         timeout=10, headers={"User-Agent": "Mozilla/5.0"})
        m = re.findall(r'data="(\d{8})\|[\d.]+\|[\d.]+\|[\d.]+\|([\d.]+)\|\d+"', r.text)
        if m:
            d, c = m[-1]
            return f"{d[:4]}-{d[4:6]}-{d[6:]}", float(c)
    except Exception:
        pass
    return None, None


# ───────── 받는 사람 ─────────
db = None


def users():
    global db
    sa = os.environ.get("FIREBASE_SA", "").strip()
    if sa:
        try:
            import firebase_admin
            from firebase_admin import credentials, firestore
            if not firebase_admin._apps:
                firebase_admin.initialize_app(credentials.Certificate(json.loads(sa)))
            db = firestore.client()
            out = []
            for doc in db.collection("users").stream():
                d = doc.to_dict() or {}
                nt = ((d.get("app") or {}).get("notify") or {})
                if nt.get("chatId") and nt.get("on", True):
                    out.append({"uid": doc.id, "chat": str(nt["chatId"]).strip(), "records": d.get("records") or [],
                                "name": d.get("userName") or ""})
            print(f"[알림] 받는 사람 {len(out)}명 (Firebase)", flush=True)
            return out
        except Exception as e:
            print(f"[알림] Firebase 읽기 실패: {e}", flush=True)
    chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    return [{"uid": "owner", "chat": chat, "records": [], "name": ""}] if chat else []


def state_get(uid):
    if db is None:
        return {}
    try:
        s = db.collection("notify_state").document(uid).get()
        return s.to_dict() or {} if s.exists else {}
    except Exception:
        return {}


def state_set(uid, st):
    if db is None:
        return
    try:
        db.collection("notify_state").document(uid).set(st)
    except Exception as e:
        print(f"  상태 저장 실패: {e}", flush=True)


def active(records):
    out = []
    for r in records:
        if r.get("status") not in ("buy", "hold"):
            continue
        m = CODE_RE.search(str(r.get("stock", "")))
        if m:
            out.append((r, m.group(1)))
    return out


# ───────── 테마 묶음: 매일 제안 + 각자 산 묶음(산 날부터 10거래일) ─────────
KRX_HOLI = {"2026-10-09", "2026-12-25", "2026-12-31", "2027-01-01", "2027-02-08", "2027-02-09", "2027-03-01", "2027-05-05",
            "2027-05-13", "2027-08-16", "2027-09-14", "2027-09-15", "2027-09-16", "2027-10-04", "2027-10-11", "2027-12-27", "2027-12-31"}


def td_between(a, b):
    """a 다음 날부터 b까지 거래일 수"""
    from datetime import date as _d
    if not a or not b or b <= a:
        return 0
    x = _d.fromisoformat(a)
    end = _d.fromisoformat(b)
    k = 0
    while x < end:
        x += timedelta(days=1)
        if x.weekday() < 5 and x.isoformat() not in KRX_HOLI:
            k += 1
    return k


def proposal_lines(js):
    pk = ((js.get("themes") or {}).get("picks")) or {}
    as_of = pk.get("asOf") or js.get("baseDate") or ""
    hold = pk.get("hold", 10)
    lines = []
    if pk.get("mkt60") is not None:
        lines.append(("쉬는 구간" if pk.get("rest") else "공격 구간") + f" · 테마 평균 60일 {pk['mkt60']:+.1f}%"
                     + (f" · 20일 {pk['mkt20']:+.1f}%" if pk.get("mkt20") is not None else ""))
    if not pk.get("rest") and pk.get("today"):
        th = " · ".join(re.sub(r"\s*\(.*\)\s*$", "", x["theme"]) for x in pk["today"])
        n = len({m["code"] for x in pk["today"] for m in x.get("top", [])})
        lines.append(f"오늘의 묶음 제안: {th} ({n}종목) → 앱에서 [묶음 사기] 또는 [패스]")
    elif pk.get("rest"):
        lines.append(f"오늘은 제안 없음 ({pk.get('restWhy') or '시장 약세'})")
    us = js.get("us") or []
    if us:
        lines.append("어젯밤 미국: " + " · ".join(f"{u['name']} {u['chg']:+.1f}%" for u in us[:3]) + " (참고)")
    return lines, as_of, hold


def my_baskets(records, as_of, hold):
    out = {}
    for r, code in active(records):
        b = r.get("basket")
        if b:
            out.setdefault(b, 0)
            out[b] += 1
    return [(b, n, td_between(b, as_of)) for b, n in sorted(out.items())]


def evening(js):
    head = f"[생각콩 트래커] {js.get('baseDate', '')} 종가 기준" + (" (장중 실행)" if js.get("intraday") else "")
    pl, as_of, hold = proposal_lines(js)
    news = [x for x in js.get("reviews") or [] if x.get("new")]
    for u in users():
        lines = [head] + pl
        for b, n, d in my_baskets(u["records"], as_of, hold):
            if d >= hold:
                lines.append(f"정리일 지남: {b[5:].replace('-', '/')} 묶음 {n}종목 → 다음 거래일 매도")
            elif d == hold - 1:
                lines.append(f"내일 정리: {b[5:].replace('-', '/')} 묶음 {n}종목 (장 마감 전 예약 매도)")
        near, broke = [], []
        for r, code in active(u["records"])[:30]:
            stop = float(r.get("stoploss") or 0)
            if stop <= 0:
                continue
            _, px = price_now(code)
            if not px:
                continue
            nm = str(r.get("stock", "")).split(" ")[0]
            if px < stop:
                broke.append(f"{nm} {px:,.0f}원(손절 {stop:,.0f})")
            elif (px - stop) / px < 0.02:
                near.append(nm)
        if broke:
            lines.append("손절선 이탈: " + ", ".join(broke))
        if near:
            lines.append("손절선 2% 이내: " + ", ".join(near))
        for x in news:
            lines.append(f"개선 알림 {x['code']}: {x['title']}")
        lines.append(APP_URL)
        send(u["chat"], "\n".join(lines))


def intraday(js):
    now = datetime.now(KST)
    today = now.strftime("%Y-%m-%d")
    pk = ((js.get("themes") or {}).get("picks")) or {}
    hold = pk.get("hold", 10)
    for u in users():
        st = state_get(u["uid"])
        sent = st.get("stop") or {}
        sent = {k: v for k, v in sent.items() if v == today}
        msgs = []
        if st.get("basketDay") != today and now.hour < 12 and now.weekday() < 5 and today not in KRX_HOLI:
            due = [b for b, n, d in my_baskets(u["records"], today, hold) if d >= hold]
            if due:
                msgs.append("[오늘 묶음 정리일] " + ", ".join(b[5:].replace("-", "/") + " 묶음" for b in due) + " — 장 마감 전에 정리하세요.")
                st["basketDay"] = today
        for r, code in active(u["records"])[:30]:
            stop = float(r.get("stoploss") or 0)
            rid = str(r.get("id"))
            if stop <= 0 or rid in sent:
                continue
            d, px = price_now(code)
            if d != today or not px:          # 오늘 장이 안 열렸으면 건너뜀
                continue
            if px < stop:
                entry = float(r.get("entry") or 0)
                nm = str(r.get("stock", "")).split(" ")[0]
                msgs.append(f"[손절선 이탈] {nm} 현재 {px:,.0f}원 · 손절 {stop:,.0f}원"
                            + (f" · 진입 대비 {(px / entry - 1) * 100:+.1f}%" if entry else ""))
                sent[rid] = today
        if msgs:
            send(u["chat"], "\n".join(msgs + [APP_URL]))
        st["stop"] = sent
        state_set(u["uid"], st)


def test():
    us = users()
    for u in us:
        send(u["chat"], "[생각콩 트래커] 텔레그램 연결 확인 — 저녁 요약과 손절 알림이 여기로 와요.\n" + APP_URL)
    print(f"[알림] 테스트 {len(us)}명", flush=True)


if __name__ == "__main__":
    mode = (sys.argv[1] if len(sys.argv) > 1 else "evening").strip()
    if not TOKEN:
        print("[알림] TELEGRAM_BOT_TOKEN 없음 → 건너뜀")
        sys.exit(0)
    js = latest()
    {"evening": lambda: evening(js), "intraday": lambda: intraday(js), "test": test}.get(mode, lambda: evening(js))()
