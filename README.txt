생각콩 x AI 주식 트래커 — 최종본 (2026-10-07)

[저장소에 넣을 위치]
1. index.html            → 저장소 맨 바깥 (기존 파일 교체)
2. scan/ 폴더 5개 파일    → 저장소 scan/ 폴더 (기존 교체 + 새 파일 추가)
   volume_scan.py, backtest.py, themes.py, theme_flow.py, theme_bt.py
3. workflows/ 폴더 2개    → GitHub 웹에서 .github/workflows/ 의 같은 이름 파일을 열고
   연필 아이콘 → 내용 전체를 이 파일 내용으로 교체 → Commit
   (점(.)으로 시작하는 폴더는 업로드 때 빠지기 쉬워서 이렇게 따로 넣었습니다)
4. worker/stock-proxy.js  → 저장소에 올릴 필요 없음. Cloudflare Worker(stock-tracker) 코드용 보관본

[올린 뒤]
- Actions › volume-scan › Run workflow (테마 구성 파일이 처음 만들어짐)
- 끝나면 Actions › backtest › Run workflow
- 한투 키는 Cloudflare Worker 비밀 변수와 GitHub Secrets 두 곳에만 (KIS_APPKEY, KIS_APPSECRET)
