#!/usr/bin/env python3
"""
테마 분류 → scan/themes.json  { "테마명": ["005930", ...], ... }

순서대로 시도하고, 처음 성공한 것을 씁니다.
 1) 네이버 증권 테마 페이지 (약 260개 테마)
 2) 네이버 모바일 증권 테마 API
 3) KRX KIND 업종 분류 (테마 대신 업종 순환 — 해외 서버에서도 비교적 안정적)
volume-scan 워크플로가 월요일(또는 파일이 없을 때) 실행합니다.
"""
import io
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import requests

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "themes.json")
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
      "Referer": "https://finance.naver.com/"}
S = requests.Session()
S.headers.update(UA)
CODE_RE = re.compile(r'code=([0-9A-Z]{6})')


def get(url, enc="euc-kr"):
    for a in range(3):
        try:
            r = S.get(url, timeout=15)
            if enc:
                r.encoding = enc
            return r
        except Exception as e:
            last = e
            time.sleep(1 + a)
    print(f"  요청 실패: {url} ({last})", flush=True)
    return None


def from_naver_pc():
    themes = {}
    for page in range(1, 15):
        r = get(f"https://finance.naver.com/sise/sise_group.naver?type=theme&page={page}")
        if r is None:
            break
        if page == 1:
            print(f"  [PC] 상태 {r.status_code} · {len(r.text)}자", flush=True)
        found = re.findall(r'type=theme&(?:amp;)?no=(\d+)[^>]*>\s*([^<]+?)\s*</a>', r.text)
        new = [(no, nm) for no, nm in found if no not in themes]
        if not new:
            break
        themes.update(new)
    print(f"  [PC] 테마 {len(themes)}개", flush=True)
    if len(themes) < 50:
        return {}

    def members(item):
        no, name = item
        r = get(f"https://finance.naver.com/sise/sise_group_detail.naver?type=theme&no={no}")
        return name, list(dict.fromkeys(CODE_RE.findall(r.text if r is not None else "")))

    with ThreadPoolExecutor(max_workers=6) as ex:
        return {n: c for n, c in ex.map(members, themes.items()) if len(c) >= 3}


def from_naver_mobile():
    themes = {}
    for page in range(1, 20):
        r = get(f"https://m.stock.naver.com/api/stocks/theme?page={page}&pageSize=100", enc=None)
        if r is None:
            break
        try:
            js = r.json()
        except Exception:
            print(f"  [모바일] JSON 아님 (상태 {r.status_code})", flush=True)
            break
        groups = js.get("groups") or js.get("themes") or js.get("result") or []
        if not groups:
            break
        for g in groups:
            no = g.get("no") or g.get("themeNo") or g.get("id")
            nm = g.get("name") or g.get("themeName")
            if no and nm:
                themes[str(no)] = nm
    print(f"  [모바일] 테마 {len(themes)}개", flush=True)
    if len(themes) < 50:
        return {}

    def members(item):
        no, name = item
        codes = []
        r = get(f"https://m.stock.naver.com/api/stocks/theme/{no}?page=1&pageSize=100", enc=None)
        try:
            js = r.json()
            for x in js.get("stocks") or js.get("result") or []:
                c = x.get("itemCode") or x.get("code")
                if c:
                    codes.append(c)
        except Exception:
            pass
        return name, codes

    with ThreadPoolExecutor(max_workers=6) as ex:
        return {n: c for n, c in ex.map(members, themes.items()) if len(c) >= 3}


def from_kind_industry():
    import pandas as pd
    out = {}
    for mkt in ("stockMkt", "kosdaqMkt"):
        r = get(f"https://kind.krx.co.kr/corpgeneral/corpList.do?method=download&searchType=13&marketType={mkt}")
        if r is None:
            continue
        df = pd.read_html(io.StringIO(r.text), converters={"종목코드": str})[0]
        for _, row in df.iterrows():
            ind = str(row.get("업종", "")).strip()
            if ind and ind != "nan":
                out.setdefault(ind, []).append(str(row["종목코드"]).strip().zfill(6))
    out = {k: v for k, v in out.items() if len(v) >= 5}
    print(f"  [KIND 업종] {len(out)}개", flush=True)
    return out


def main():
    for name, fn in (("네이버 테마", from_naver_pc), ("네이버 모바일 테마", from_naver_mobile), ("KRX 업종", from_kind_industry)):
        print(f"[테마] {name} 시도", flush=True)
        try:
            th = fn()
        except Exception as e:
            print(f"  실패: {type(e).__name__}: {e}", flush=True)
            th = {}
        if len(th) >= 20:
            with open(OUT, "w", encoding="utf-8") as fp:
                json.dump(th, fp, ensure_ascii=False, indent=0)
            print(f"[완료] {name} {len(th)}개 · 평균 {sum(map(len, th.values())) / len(th):.0f}종목 → {OUT}")
            return
    sys.exit("테마 분류를 하나도 만들지 못했습니다")


if __name__ == "__main__":
    main()
