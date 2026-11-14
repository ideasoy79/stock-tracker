"""
뉴스 → 테마 → 종목 연결 (매일 저녁 스캔에서 실행)

무엇을 하나
- 오늘 제안(또는 강한) 테마와, 5일 새 크게 오르거나 내린 테마 8개 정도를 골라
  테마 이름으로 최근 3일 뉴스를 찾고, 기사 제목·요약에 나온 '그 테마 소속 종목'을 연결
- 오늘 제안 종목(현행 5 + 후발 3)은 종목 이름으로 최근 3일 뉴스 3건씩

뉴스 출처 (둘 중 되는 쪽)
1) 네이버 검색 API (권장, 무료 하루 25,000회): GitHub Secrets 에 NAVER_CLIENT_ID · NAVER_CLIENT_SECRET
   발급: developers.naver.com › Application › 애플리케이션 등록 › 사용 API '검색' 선택
2) 키가 없으면 구글 뉴스 RSS (키 필요 없음, 비공식이라 막히면 빈 목록)

실패해도 스캔·앱은 그대로 동작 (news = None 또는 빈 목록)
"""
import html
import os
import re
import time
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

import requests

KST = timezone(timedelta(hours=9))
UA = {"User-Agent": "Mozilla/5.0"}
DAYS = 3


def _clean(s):
    s = re.sub(r"<[^>]+>", "", str(s or ""))
    return html.unescape(s).replace("​", "").strip()


def _when(s):
    try:
        return parsedate_to_datetime(s).astimezone(KST)
    except Exception:
        return None


def _naver(q, n, cid, sec):
    r = requests.get("https://openapi.naver.com/v1/search/news.json",
                     params={"query": q, "display": n, "sort": "date"},
                     headers={"X-Naver-Client-Id": cid, "X-Naver-Client-Secret": sec}, timeout=10)
    if r.status_code != 200:
        raise RuntimeError(f"네이버 {r.status_code} {r.text[:80]}")
    out = []
    for x in r.json().get("items") or []:
        link = x.get("originallink") or x.get("link") or ""
        host = urllib.parse.urlparse(link).netloc.replace("www.", "")
        out.append({"t": _clean(x.get("title")), "s": _clean(x.get("description"))[:160], "src": host,
                    "url": x.get("link") or link, "dt": _when(x.get("pubDate"))})
    return out


def _google(q, n):
    url = "https://news.google.com/rss/search?" + urllib.parse.urlencode({"q": f"{q} when:{DAYS}d", "hl": "ko", "gl": "KR", "ceid": "KR:ko"})
    r = requests.get(url, headers=UA, timeout=10)
    if r.status_code != 200:
        raise RuntimeError(f"구글뉴스 {r.status_code}")
    root = ET.fromstring(r.content)
    out = []
    for it in root.iter("item"):
        t = _clean(it.findtext("title"))
        src = _clean(it.findtext("source"))
        if src and t.endswith(" - " + src):
            t = t[: -len(src) - 3]
        out.append({"t": t, "s": "", "src": src, "url": it.findtext("link") or "", "dt": _when(it.findtext("pubDate"))})
        if len(out) >= n:
            break
    return out


class Finder:
    def __init__(self):
        self.cid = os.environ.get("NAVER_CLIENT_ID", "").strip()
        self.sec = os.environ.get("NAVER_CLIENT_SECRET", "").strip()
        self.src = "naver" if self.cid and self.sec else "google"
        self.calls, self.err = 0, None

    def search(self, q, n=20):
        if self.err and self.calls > 3 and self.err.startswith(("네이버 401", "네이버 403", "구글뉴스 4")):
            return []
        self.calls += 1
        try:
            items = _naver(q, n, self.cid, self.sec) if self.src == "naver" else _google(q, n)
        except Exception as e:
            self.err = str(e)[:120]
            if self.src == "naver" and self.calls <= 2:      # 키 문제면 구글로 전환
                self.src = "google"
                try:
                    items = _google(q, n)
                except Exception as e2:
                    self.err = str(e2)[:120]
                    return []
            else:
                return []
        time.sleep(0.12)
        cut = datetime.now(KST) - timedelta(days=DAYS)
        seen, out = set(), []
        for x in items:
            if x["dt"] and x["dt"] < cut:
                continue
            key = re.sub(r"\W", "", x["t"])[:30]
            if not x["t"] or key in seen:
                continue
            seen.add(key)
            out.append(x)
        return out


def theme_query(name):
    """'소캠(SOCAMM)' → '소캠 SOCAMM', '피지컬 AI/휴머노이드 로봇' → '피지컬 AI 휴머노이드 로봇' + 관련주"""
    s = re.sub(r"[()/·,&＆]", " ", str(name))
    s = re.sub(r"\s+", " ", s).strip()
    s = re.sub(r"\b(등|대표주|주요종목)\b", "", s).strip()
    return s + " 관련주"


def _matcher(codes, names):
    pats = []
    for c in codes:
        nm = (names.get(c) or "").strip()
        if len(nm) < 2:
            continue
        if re.fullmatch(r"[A-Za-z0-9&\-\. ]+", nm) and len(nm) <= 3:
            p = re.compile(r"(?<![A-Za-z0-9])" + re.escape(nm) + r"(?![A-Za-z0-9])")
        else:
            p = re.compile(re.escape(nm))
        pats.append((c, nm, p))
    pats.sort(key=lambda x: -len(x[1]))          # 긴 이름부터 (예: 'SK하이닉스'가 'SK'보다 먼저)

    def match(text):
        hit, used = [], text
        for c, nm, p in pats:
            if p.search(used):
                hit.append(c)
                used = p.sub(" ", used)
        return hit
    return match


def _fmt(x, stocks=None):
    d = {"t": x["t"][:90], "src": x["src"][:20], "url": x["url"], "d": x["dt"].strftime("%m/%d %H:%M") if x["dt"] else ""}
    if stocks:
        d["st"] = stocks
    return d


def fetch(tp, names, themes, max_themes=8):
    """tp = theme_flow.daily_payload 결과, names = {code: 종목명}, themes = {테마: [codes]}"""
    if not tp:
        return None
    f = Finder()
    pk = tp.get("picks") or {}
    today = pk.get("today") or []
    leaders = tp.get("leaders") or []
    chosen = [x["theme"] for x in today]
    by5 = sorted([x for x in leaders if x["theme"] not in chosen], key=lambda x: -x.get("ret5", 0))
    for x in by5[:4]:
        chosen.append(x["theme"])
    cool = sorted((tp.get("leaders") or []) + (tp.get("cooling") or []), key=lambda x: x.get("ret5", 0))
    for x in cool:
        if len(chosen) >= max_themes:
            break
        if x["theme"] not in chosen and x.get("ret5", 0) < -3:
            chosen.append(x["theme"])
    info = {x["theme"]: x for x in leaders + (tp.get("cooling") or []) + (tp.get("rising") or [])}
    out_th = []
    for th in chosen[:max_themes]:
        mem = [c for c in themes.get(th, []) if c in names]
        match = _matcher(mem, names)
        items = f.search(theme_query(th), 30)
        cnt, arts = {}, []
        for x in items:
            hit = match(x["t"] + " " + x["s"])
            for c in hit:
                cnt[c] = cnt.get(c, 0) + 1
            arts.append((len(hit), x, hit))
        arts.sort(key=lambda a: (-min(a[0], 2), -(a[1]["dt"].timestamp() if a[1]["dt"] else 0)))
        z = info.get(th, {})
        top = th in [x["theme"] for x in today]
        out_th.append({"theme": th, "q": theme_query(th), "top": top, "pick": top and not pk.get("rest"),
                       "ret5": z.get("ret5"), "ret20": z.get("ret20"), "n": len(items),
                       "items": [_fmt(x, hit) for _, x, hit in arts[:4]],
                       "stocks": sorted(([c, k] for c, k in cnt.items()), key=lambda a: -a[1])[:6]})
    out_st = {}
    codes = list(dict.fromkeys(m["code"] for x in today for m in (x.get("top") or []) + (x.get("lag") or [])))
    for c in codes[:20]:
        nm = names.get(c)
        if not nm:
            continue
        items = f.search(f'"{nm}"' if f.src == "naver" else nm, 10)
        items = [x for x in items if nm in x["t"] or nm in x["s"]] or items[:0]
        if items:
            out_st[c] = [_fmt(x) for x in items[:3]]
    res = {"asOf": datetime.now(KST).strftime("%Y-%m-%d %H:%M"), "rest": bool(pk.get("rest")), "src": f.src, "days": DAYS, "calls": f.calls,
           "themes": out_th, "stocks": out_st}
    if f.err:
        res["error"] = f.err
    print(f"[뉴스] {f.src} {f.calls}회 · 테마 {len(out_th)}개 · 종목 {len(out_st)}개" + (f" · 오류 {f.err}" if f.err else ""), flush=True)
    return res
