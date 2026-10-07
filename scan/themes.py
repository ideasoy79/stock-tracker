#!/usr/bin/env python3
"""
네이버 증권 테마 분류(약 260개) → scan/themes.json  { "테마명": ["005930", ...], ... }

- volume_scan.py(매일)와 backtest.py(매월)가 이 파일로 테마별 흐름을 계산합니다.
- 테마 구성은 자주 안 바뀌므로 주 1회 갱신이면 충분합니다 (volume-scan 워크플로가 월요일에 실행).
- 네이버 비공식 페이지를 읽으므로 화면 구조가 바뀌면 수정이 필요할 수 있습니다.
"""
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import requests

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "themes.json")
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"}
S = requests.Session()
S.headers.update(UA)

LIST_URL = "https://finance.naver.com/sise/sise_group.naver?type=theme&page={}"
DETAIL_URL = "https://finance.naver.com/sise/sise_group_detail.naver?type=theme&no={}"
THEME_RE = re.compile(r'sise_group_detail\.naver\?type=theme&(?:amp;)?no=(\d+)"[^>]*>([^<]+)</a>')
CODE_RE = re.compile(r'/item/main\.naver\?code=([0-9A-Z]{6})')


def get(url):
    for a in range(3):
        try:
            r = S.get(url, timeout=15)
            r.encoding = "euc-kr"
            return r.text
        except Exception:
            time.sleep(1 + a)
    return ""


def main():
    themes = {}
    for page in range(1, 15):
        html = get(LIST_URL.format(page))
        found = THEME_RE.findall(html)
        new = [(no, name.strip()) for no, name in found if no not in themes]
        if not new:
            break
        for no, name in new:
            themes[no] = name
    print(f"[테마] 목록 {len(themes)}개", flush=True)
    if len(themes) < 50:
        sys.exit("테마 목록을 충분히 못 읽었습니다 (네이버 화면 구조 변경 가능)")

    def members(item):
        no, name = item
        codes = list(dict.fromkeys(CODE_RE.findall(get(DETAIL_URL.format(no)))))
        return name, codes

    out = {}
    with ThreadPoolExecutor(max_workers=6) as ex:
        for name, codes in ex.map(members, themes.items()):
            if len(codes) >= 3:
                out[name] = codes
    with open(OUT, "w", encoding="utf-8") as fp:
        json.dump(out, fp, ensure_ascii=False, indent=0)
    print(f"[완료] 테마 {len(out)}개 · 평균 {sum(map(len, out.values())) / max(len(out), 1):.0f}종목 → {OUT}")


if __name__ == "__main__":
    main()
