// 화면이 부르는 API. 무거운 일은 직접 하지 않고 람다에 넘긴다.
//
// 여기가 하는 일
//   · 사람 구분(세션)과 하루 편수 세기
//   · 작업 만들기 / 목록 / 열기 / 지우기
//   · 올라온 파일을 R2 에 넣고, 만든 파일을 내주기
//   · "이 일 해줘" 를 람다에 넘기고, 진행 상황은 R2 의 task.json 으로 주고받기
//
// 무거운 계산이 없으므로 무료 플랜의 짧은 계산 시간 안에 들어간다.
import { invoke } from "./lambda.js";
import {
  blankJob, dropJob, indexAdd, indexDrop, key, listJobs,
  ownedJob, readJob, readTask, writeJob, writeTask,
} from "./jobs.js";

const SID_RE = /^[0-9a-f]{32}$/;
const json = (data, status = 200, extra = {}) =>
  new Response(JSON.stringify(data), {
    status,
    headers: { "content-type": "application/json; charset=utf-8", ...extra },
  });
const oops = (msg, status = 400) => json({ error: msg }, status);

// ── 사람 구분 ────────────────────────────────────────────────────────────
// 쿠키를 막아둔 브라우저(사생활 보호 모드, 편집기 내장 브라우저)가 있어서
// 헤더와 주소(?s=)로도 받는다. 셋 다 없을 때만 새로 만든다.
function whoIs(request) {
  const url = new URL(request.url);
  const cookie = (request.headers.get("cookie") || "")
    .split(";").map((s) => s.trim()).find((s) => s.startsWith("studio_sid="));
  const given = (request.headers.get("x-studio-sid") ||
                 url.searchParams.get("s") ||
                 (cookie ? cookie.slice("studio_sid=".length) : "") || "").trim();
  if (SID_RE.test(given)) return { sid: given, fresh: false };
  const buf = new Uint8Array(16);
  crypto.getRandomValues(buf);
  const sid = [...buf].map((b) => b.toString(16).padStart(2, "0")).join("");
  return { sid, fresh: true };
}

const clientIp = (request) =>
  request.headers.get("cf-connecting-ip") ||
  (request.headers.get("x-forwarded-for") || "").split(",")[0].trim();

// ── 하루 편수 ────────────────────────────────────────────────────────────
const today = () => new Date().toISOString().slice(0, 10);
const usageKey = (who) => `use:${today()}:${who}`;

async function usageOf(env, who) {
  return parseInt((await env.COUNTS.get(usageKey(who))) || "0", 10);
}

async function usageBump(env, who, n = 1) {
  const v = Math.max(0, (await usageOf(env, who)) + n);
  // 이틀치만 남기고 저절로 사라지게 한다 (날짜가 키에 들어 있다)
  await env.COUNTS.put(usageKey(who), String(v), { expirationTtl: 60 * 60 * 48 });
  return v;
}

async function quotaLeft(env, who, ip) {
  const perSid = parseInt(env.DAILY_VIDEOS || "2", 10) - (await usageOf(env, who));
  const perIp = ip
    ? parseInt(env.DAILY_VIDEOS_IP || "4", 10) - (await usageOf(env, `ip:${ip}`))
    : perSid;
  return Math.max(0, Math.min(perSid, perIp));
}

// ── 람다에 일 넘기기 ─────────────────────────────────────────────────────
// 진행 상황은 람다가 R2 의 task.json 에 적는다. 화면은 그 파일을 본다.
async function runOp(env, jid, label, op, params = {}, wait = false) {
  const t = await readTask(env, jid);
  if (t.state === "running" && Date.now() / 1000 - (t.started || 0) < 25 * 60) {
    return oops("이미 다른 작업이 진행 중입니다.", 409);
  }
  await writeTask(env, jid, {
    state: "running", label, msg: null, done: 0, total: 0,
    started: Date.now() / 1000,
  });
  try {
    const out = await invoke(env, { op, jid, params, label }, wait);
    if (wait) {
      await writeTask(env, jid, { state: "done", at: Date.now() / 1000 });
      return json(out.job || {});
    }
    return json(await readTask(env, jid));
  } catch (e) {
    await writeTask(env, jid, { state: "error", msg: String(e.message || e) });
    return oops(String(e.message || e), 500);
  }
}

// ── 길 안내 ──────────────────────────────────────────────────────────────
async function route(request, env, ctx, who, ip) {
  const url = new URL(request.url);
  const path = url.pathname;
  const method = request.method;
  const seg = path.split("/").filter(Boolean);       // ["api", jid, ...]

  if (path === "/api/quota") {
    return json({
      made: await usageOf(env, who),
      limit: parseInt(env.DAILY_VIDEOS || "2", 10),
      left: await quotaLeft(env, who, ip),
      redo: parseInt(env.REDO_LIMIT || "1", 10),
    });
  }

  if (path === "/api/new" && method === "POST") {
    const jid = newId();
    const job = blankJob(jid, who);
    await writeJob(env, jid, job);
    await indexAdd(env, who, jid);
    return json(job);
  }

  if (path === "/api/jobs") return json({ jobs: await listJobs(env, who) });

  if (seg[0] !== "api" || seg.length < 2) return oops("없는 주소입니다.", 404);
  const jid = seg[1];
  const job = await ownedJob(env, jid, who);
  if (!job) return oops("작업을 찾을 수 없습니다.", 404);
  const rest = seg.slice(2).join("/");

  if (!rest) {
    if (method === "GET") return json(job);
    if (method === "DELETE") {
      await dropJob(env, jid);
      await indexDrop(env, who, jid);
      return json({ ok: true });
    }
  }

  if (rest === "task") return json(await readTask(env, jid));

  // 만든 파일 내주기 (영상·그림). R2 에서 곧바로 흘려보낸다.
  if (seg[2] === "file" && seg[3]) {
    const obj = await env.FILES.get(key(jid, seg.slice(3).join("/")));
    if (!obj) return oops("파일이 없습니다.", 404);
    return new Response(obj.body, {
      headers: {
        "content-type": obj.httpMetadata?.contentType || "application/octet-stream",
        "cache-control": "private, max-age=3600",
      },
    });
  }

  // 파일 올리기 → R2 에 넣고, 필요하면 람다에 분석을 맡긴다
  if (method === "POST" && (rest === "character" || rest === "reference" ||
                            rest === "bg_photo" || rest === "music")) {
    const form = await request.formData();
    const file = form.get("file");
    if (!file || typeof file === "string") return oops("파일이 없습니다.");
    const ext = (file.name.match(/\.[a-z0-9]+$/i) || [".bin"])[0].toLowerCase();
    const name = rest + ext;
    await env.FILES.put(key(jid, name), file.stream(), {
      httpMetadata: { contentType: file.type || "application/octet-stream" },
    });
    job[rest] = name;
    await writeJob(env, jid, job);

    if (rest === "reference") {
      return runOp(env, jid, "레퍼런스 분석", "analyze_reference", { name });
    }
    return json(job);
  }

  // 무거운 일들 — 전부 람다로 간다
  if (method === "POST") {
    const form = request.headers.get("content-type")?.includes("form")
      ? await request.formData() : new FormData();
    const s = (k, d = "") => String(form.get(k) ?? d);

    if (rest === "character/cleanup") {
      return runOp(env, jid, "그림 다듬기", "clean_character",
                   { feedback: s("feedback") });
    }
    if (rest === "keycut") {
      return runOp(env, jid, "키컷 만들기", "make_keycut",
                   { bg_prompt: s("bg_prompt"), feedback: s("feedback") });
    }
    if (rest === "reference/scan") {
      return runOp(env, jid, "좋은 구간 찾기", "scan_segments");
    }
    if (rest === "reference/pick") {
      return runOp(env, jid, "구간 적용", "pick_segment", { t0: parseFloat(s("start", "0")) });
    }
    if (rest === "subtitles") {
      return runOp(env, jid, "가사 자막 만들기", "make_lyrics");
    }
    if (rest === "ending") {
      return runOp(env, jid, "엔딩 만들기", "build_ending");
    }
  }

  return oops("없는 주소입니다.", 404);
}

function newId() {
  const n = new Date();
  const p = (x) => String(x).padStart(2, "0");
  const r = new Uint8Array(2);
  crypto.getRandomValues(r);
  return `${p(n.getMonth() + 1)}${p(n.getDate())}-${p(n.getHours())}${p(n.getMinutes())}-` +
         [...r].map((b) => b.toString(16).padStart(2, "0")).join("");
}

export default {
  async fetch(request, env, ctx) {
    const { sid, fresh } = whoIs(request);
    const ip = clientIp(request);
    let resp;
    try {
      resp = await route(request, env, ctx, sid, ip);
    } catch (e) {
      resp = oops(String(e.message || e), 500);
    }
    resp = new Response(resp.body, resp);
    resp.headers.set("x-studio-sid", sid);      // 화면이 받아서 들고 있는다
    if (fresh) {
      resp.headers.append("set-cookie",
        `studio_sid=${sid}; Max-Age=2592000; Path=/; HttpOnly; SameSite=Lax`);
    }
    return resp;
  },

  // 1분마다 — fal 에 맡긴 영상이 다 됐는지 확인하는 자리 (다음 단계에서 채운다)
  async scheduled(event, env, ctx) {
    return;
  },
};
