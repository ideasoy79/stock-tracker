/**
 * 생각콩 × AI 주식 트래커 — 시세 중계 서버 (Cloudflare Worker)
 *
 * 트래커(브라우저)는 증권사 키를 가질 수 없으므로, 이 Worker가 대신 시세를 받아 넘겨줍니다.
 * 키는 Worker의 비밀 변수에만 저장되고 트래커 HTML에는 절대 들어가지 않습니다.
 *
 *   GET /ohlc?code=005930&count=140  → 일봉   { code, src, rows:[{d,o,h,l,c,v,a}] }   (a = 실제 거래대금, KIS일 때)
 *   GET /investor?code=005930        → 수급   { code, rows:[{d,frgn,orgn,prsn}] }     (KIS 전용, 최근 30거래일 순매수 수량)
 *   GET /status?code=005930          → 상태   { code, stat, halt }                    (KIS 전용, 51관리 52위험 53경고 54주의 58정지 59과열)
 *   GET /health                      → { ok, kis }
 *
 * 배포: Cloudflare → Workers & Pages → Create → Worker → 이 코드 붙여넣기 → Deploy
 * 한국투자증권 키 등록: Worker → Settings → Variables and Secrets → Add
 *   KIS_APPKEY    (Type: Secret)
 *   KIS_APPSECRET (Type: Secret)
 * 키가 없거나 KIS 호출이 실패하면 /ohlc 는 네이버 시세로 자동 대체됩니다.
 */
const ALLOW_ORIGIN = "*"; // 트래커 주소만 허용하려면 "https://ideasoy79.github.io" 처럼 바꾸세요
const KIS_BASE = "https://openapi.koreainvestment.com:9443";

const cors = {
  "Access-Control-Allow-Origin": ALLOW_ORIGIN,
  "Access-Control-Allow-Methods": "GET, OPTIONS",
  "Access-Control-Allow-Headers": "Content-Type",
};
const json = (data, status = 200, extra = {}) =>
  new Response(JSON.stringify(data), { status, headers: { "Content-Type": "application/json; charset=utf-8", ...cors, ...extra } });
const ymd = (d) => d.toISOString().slice(0, 10).replace(/-/g, "");
const dash = (s) => `${s.slice(0, 4)}-${s.slice(4, 6)}-${s.slice(6, 8)}`;
const kstToday = () => new Date(Date.now() + 9 * 3600e3);

/* ── KIS 토큰: 하루 1회 발급, Cache에 20시간 보관 (자주 발급하면 KIS가 제한함) ── */
async function kisToken(env, ctx) {
  const cache = caches.default;
  const key = new Request("https://cache.local/kis-token");
  const hit = await cache.match(key);
  if (hit) return (await hit.json()).token;
  const r = await fetch(`${KIS_BASE}/oauth2/tokenP`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ grant_type: "client_credentials", appkey: env.KIS_APPKEY, appsecret: env.KIS_APPSECRET }),
  });
  const js = await r.json();
  if (!js.access_token) throw new Error("KIS 토큰 실패: " + (js.error_description || js.msg1 || r.status));
  ctx.waitUntil(cache.put(key, new Response(JSON.stringify({ token: js.access_token }), { headers: { "Cache-Control": "max-age=72000" } })));
  return js.access_token;
}
async function kisGet(env, ctx, path, trId, params) {
  const token = await kisToken(env, ctx);
  const r = await fetch(`${KIS_BASE}${path}?${new URLSearchParams(params)}`, {
    headers: {
      "content-type": "application/json; charset=utf-8",
      authorization: `Bearer ${token}`,
      appkey: env.KIS_APPKEY, appsecret: env.KIS_APPSECRET,
      tr_id: trId, custtype: "P",
    },
  });
  const js = await r.json();
  if (js.rt_cd !== "0") throw new Error("KIS " + (js.msg_cd || "") + " " + (js.msg1 || r.status));
  return js;
}
async function kisDaily(env, ctx, code, count) {
  const rows = new Map();
  let end = kstToday();
  for (let i = 0; i < 2; i++) {
    const start = new Date(end.getTime() - 170 * 864e5);
    const js = await kisGet(env, ctx, "/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice", "FHKST03010100", {
      FID_COND_MRKT_DIV_CODE: "J", FID_INPUT_ISCD: code, FID_INPUT_DATE_1: ymd(start), FID_INPUT_DATE_2: ymd(end),
      FID_PERIOD_DIV_CODE: "D", FID_ORG_ADJ_PRC: "0",
    });
    const out = (js.output2 || []).filter((x) => x && x.stck_bsop_date);
    for (const x of out) {
      const c = +x.stck_clpr;
      if (!(c > 0)) continue;
      rows.set(x.stck_bsop_date, { d: dash(x.stck_bsop_date), o: +x.stck_oprc, h: +x.stck_hgpr, l: +x.stck_lwpr, c, v: +x.acml_vol, a: +(x.acml_tr_pbmn || 0) });
    }
    if (rows.size >= count || out.length < 100) break;
    const oldest = out.map((x) => x.stck_bsop_date).sort()[0];
    end = new Date(Date.UTC(+oldest.slice(0, 4), +oldest.slice(4, 6) - 1, +oldest.slice(6, 8)) - 864e5);
  }
  return [...rows.keys()].sort().map((k) => rows.get(k)).slice(-count);
}

/* ── 네이버 대체 경로 (비공식 주소) ── */
async function naverDaily(code, count) {
  const res = await fetch(`https://fchart.stock.naver.com/sise.nhn?symbol=${code}&timeframe=day&count=${count}&requestType=0`, { headers: { "User-Agent": "Mozilla/5.0" } });
  if (!res.ok) throw new Error("naver " + res.status);
  const buf = await res.arrayBuffer();
  let text;
  try { text = new TextDecoder("euc-kr").decode(buf); } catch { text = new TextDecoder().decode(buf); }
  const rows = [];
  const re = /data="(\d{8})\|([\d.]+)\|([\d.]+)\|([\d.]+)\|([\d.]+)\|(\d+)"/g;
  let m;
  while ((m = re.exec(text))) {
    if (+m[5] > 0) rows.push({ d: dash(m[1]), o: +m[2], h: +m[3], l: +m[4], c: +m[5], v: +m[6] });
  }
  return rows;
}

async function cached(keyStr, maxAge, ctx, make) {
  const cache = caches.default;
  const key = new Request("https://cache.local/" + keyStr);
  const hit = await cache.match(key);
  if (hit) return hit;
  const data = await make();
  const resp = json(data, 200, { "Cache-Control": `public, max-age=${maxAge}` });
  ctx.waitUntil(cache.put(key, resp.clone()));
  return resp;
}

export default {
  async fetch(request, env, ctx) {
    if (request.method === "OPTIONS") return new Response(null, { headers: cors });
    const url = new URL(request.url);
    const hasKis = !!(env.KIS_APPKEY && env.KIS_APPSECRET);
    const code = (url.searchParams.get("code") || "").toUpperCase();
    const needCode = () => (/^[0-9A-Z]{6}$/.test(code) ? null : json({ error: "code는 6자리 종목코드" }, 400));

    try {
      if (url.pathname === "/health") return json({ ok: true, kis: hasKis });

      if (url.pathname === "/ohlc") {
        const bad = needCode(); if (bad) return bad;
        const count = Math.min(Math.max(parseInt(url.searchParams.get("count") || "60", 10) || 60, 5), 200);
        return await cached(`ohlc/${code}/${count}`, 600, ctx, async () => {
          let kisErr = null;
          if (hasKis) {
            try {
              const rows = await kisDaily(env, ctx, code, count);
              if (rows.length) return { code, src: "kis", rows };
            } catch (e) { kisErr = String(e.message || e); }
          }
          const rows = await naverDaily(code, count);
          return { code, src: "naver", kisErr, rows };
        });
      }

      if (url.pathname === "/investor") {
        const bad = needCode(); if (bad) return bad;
        if (!hasKis) return json({ error: "KIS 키가 등록되지 않았어요", code }, 501);
        return await cached(`inv/${code}`, 1800, ctx, async () => {
          const js = await kisGet(env, ctx, "/uapi/domestic-stock/v1/quotations/inquire-investor", "FHKST01010900", { FID_COND_MRKT_DIV_CODE: "J", FID_INPUT_ISCD: code });
          const rows = (js.output || []).filter((x) => x && x.stck_bsop_date).map((x) => ({
            d: dash(x.stck_bsop_date), frgn: +(x.frgn_ntby_qty || 0), orgn: +(x.orgn_ntby_qty || 0), prsn: +(x.prsn_ntby_qty || 0),
          }));
          return { code, rows };
        });
      }

      if (url.pathname === "/status") {
        const bad = needCode(); if (bad) return bad;
        if (!hasKis) return json({ error: "KIS 키가 등록되지 않았어요", code }, 501);
        return await cached(`stat/${code}`, 3600, ctx, async () => {
          const js = await kisGet(env, ctx, "/uapi/domestic-stock/v1/quotations/inquire-price", "FHKST01010100", { FID_COND_MRKT_DIV_CODE: "J", FID_INPUT_ISCD: code });
          const o = js.output || {};
          return { code, stat: o.iscd_stat_cls_code || "", halt: o.temp_stop_yn || "", name: o.hts_kor_isnm || "" };
        });
      }
    } catch (e) {
      return json({ error: String(e.message || e), code }, 502);
    }
    return json({ error: "not found", usage: ["/ohlc?code=005930", "/investor?code=005930", "/status?code=005930", "/health"] }, 404);
  },
};
