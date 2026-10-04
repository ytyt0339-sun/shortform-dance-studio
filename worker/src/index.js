// 화면이 부르는 API. 무거운 일은 직접 하지 않고 람다에 넘긴다.
//
// 여기가 하는 일
//   · 사람 구분(세션)과 하루 편수 세기
//   · 작업 만들기 / 목록 / 열기 / 지우기
//   · 올라온 파일을 R2 에 넣고, 만든 파일을 내주기
//   · "이 일 해줘" 를 람다에 넘기고, 진행 상황은 R2 의 task.json 으로 주고받기
//
// 무거운 계산이 없으므로 무료 플랜의 짧은 계산 시간 안에 들어간다.
import * as fal from "./fal.js";
import { invoke } from "./lambda.js";
import { linkOk, tempLink } from "./sign.js";
import { scoreCheck, setEndingOptions, setOptions, setSubs, STYLE_MODES } from "./settings.js";
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

// ── 전체 한도 ────────────────────────────────────────────────────────────
// AWS·Cloudflare 는 "여기서 멈춤" 을 안 준다. 넘으면 그냥 요금이 붙는다.
// 그래서 우리가 직접 센다. 이 숫자를 넘으면 아무도 새로 못 만든다.
//
// 진짜 돈이 드는 건 fal 이다(1편당 2~3천 원). 클라우드 무료 한도는
// 월 1,000편쯤에서 걸리므로, 아래 숫자를 그보다 낮게 두면 둘 다 막힌다.
const monthKey = () => `all:${new Date().toISOString().slice(0, 7)}`;   // all:2026-09
const dayKey = () => `all:${today()}`;                                 // all:2026-09-25

async function totalUsed(env) {
  const [m, d] = await Promise.all([
    env.COUNTS.get(monthKey()), env.COUNTS.get(dayKey()),
  ]);
  return { month: parseInt(m || "0", 10), day: parseInt(d || "0", 10) };
}

async function totalBump(env, n = 1) {
  const u = await totalUsed(env);
  await Promise.all([
    // 두 달치만 남기고 저절로 사라진다 (달이 키에 들어 있다)
    env.COUNTS.put(monthKey(), String(Math.max(0, u.month + n)), { expirationTtl: 60 * 86400 }),
    env.COUNTS.put(dayKey(), String(Math.max(0, u.day + n)), { expirationTtl: 3 * 86400 }),
  ]);
}

/** 전체 한도가 남았는가. 다 썼으면 이유를 돌려준다. */
async function totalLeft(env) {
  const maxM = parseInt(env.MAX_MONTH || "150", 10);
  const maxD = parseInt(env.MAX_DAY || "0", 10);          // 0 이면 하루 한도는 안 본다
  const u = await totalUsed(env);
  if (maxD > 0 && u.day >= maxD) {
    return { ok: false, why: "오늘 이 서비스 전체 한도를 다 썼습니다. 내일 다시 이용해 주세요." };
  }
  if (u.month >= maxM) return { ok: false, why: "이번 달 이 서비스 전체 한도를 다 썼습니다. 다음 달에 다시 이용해 주세요." };
  return { ok: true, month: maxM - u.month, day: maxD - u.day };
}

// 사람별 하루 한도. 0 으로 두면 안 막는다 (지금은 전체 월 한도로만 잠근다).
async function quotaLeft(env, who, ip) {
  const maxSid = parseInt(env.DAILY_VIDEOS || "0", 10);
  const maxIp = parseInt(env.DAILY_VIDEOS_IP || "0", 10);
  if (maxSid <= 0 && maxIp <= 0) return Infinity;
  const perSid = maxSid > 0 ? maxSid - (await usageOf(env, who)) : Infinity;
  const perIp = (maxIp > 0 && ip) ? maxIp - (await usageOf(env, `ip:${ip}`)) : Infinity;
  return Math.max(0, Math.min(perSid, perIp));
}

// ── 람다에 일 넘기기 ─────────────────────────────────────────────────────
// 진행 상황은 람다가 R2 의 task.json 에 적는다. 화면은 그 파일을 본다.
async function runOp(env, jid, label, op, params = {}, wait = false) {
  const t = await readTask(env, jid);
  if (t.state === "running" && Date.now() / 1000 - (t.started || 0) < 25 * 60) {
    return oops("이미 다른 작업이 진행 중입니다.", 409);
  }
  const started = Date.now() / 1000;
  await writeTask(env, jid, {
    state: "running", label, msg: null, done: 0, total: 0, started,
  });
  try {
    // 시작 시각을 같이 보낸다. 람다가 진행 상황을 적을 때 이 값을 그대로 달아야
    // 화면의 "몇 초 지남" 이 0 으로 되돌아가지 않는다.
    const out = await invoke(env, { op, jid, params, label, started }, wait);
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

  // 잠시 멈춤. 한 편 만들 때마다 fal 에 실제로 돈이 나가므로, 구경만 할 수
  // 있게 열어 두고 새로 만드는 길만 막는다. 읽기(GET)는 그대로 통과시켜
  // 화면과 이미 만든 결과물, /how 문서는 계속 보인다.
  // 다시 열 때: wrangler.toml 의 PAUSED 를 "0" 으로 바꾸고 올린다.
  if (env.PAUSED === "1" && method !== "GET" && method !== "HEAD") {
    return oops("지금은 새로 만들기를 멈춰 두었습니다. 화면과 이미 만든 결과물은 그대로 보실 수 있습니다.", 503);
  }

  if (path === "/api/quota") {
    // 사람별 하루 한도는 꺼져 있고, 서비스 전체의 한 달 한도로만 잠근다.
    // 화면은 "남은 편수" 하나만 보므로 둘 중 빡빡한 쪽을 준다.
    const all = await totalLeft(env);
    const mine = await quotaLeft(env, who, ip);
    const left = Math.min(mine, all.ok ? (all.month ?? 0) : 0);
    return json({
      made: await usageOf(env, who),
      limit: parseInt(env.MAX_MONTH || "150", 10),
      left: Number.isFinite(left) ? left : 9999,
      redo: parseInt(env.REDO_LIMIT || "1", 10),
      scope: "month",                     // 이 숫자는 '이번 달 전체' 기준이다
      paused: env.PAUSED === "1",         // 멈춘 동안에는 화면이 안내만 띄운다
      all,
    });
  }

  if (path === "/api/new" && method === "POST") {
    const jid = newId();
    const job = blankJob(jid, who);
    await writeJob(env, jid, job);
    await indexAdd(env, who, jid);
    return json(job);
  }

  // 화면이 배열 그대로 받기를 기대한다 (감싸면 목록이 안 그려진다)
  if (path === "/api/jobs") return json(await listJobs(env, who));

  // 화면이 "내가 최신인가" 물어보는 자리. 올릴 때마다 값이 바뀐다.
  if (path === "/api/version") return json({ page_mtime: Number(env.BUILT_AT || 0) });

  // 고를 수 있는 글꼴과 선택지. 잘 안 바뀌므로 하루 동안 기억해 둔다.
  if (path === "/api/fonts") {
    const got = await env.COUNTS.get("lists", "json");
    if (got) return json(got);
    const out = await invoke(env, { op: "font_list", jid: "_lists" }, true);
    const lists = out.result || out.job || {};
    await env.COUNTS.put("lists", JSON.stringify(lists), { expirationTtl: 86400 });
    return json(lists);
  }

  if (seg[0] !== "api" || seg.length < 2) return oops("없는 주소입니다.", 404);
  const jid = seg[1];
  const job = await ownedJob(env, jid, who);
  if (!job) return oops("작업을 찾을 수 없습니다.", 404);
  const rest = seg.slice(2).join("/");

  if (!rest) {
    if (method === "GET") return json({ ...job, score_check: scoreCheck(job) });
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

  // ── 큰 파일 나눠 올리기 ──
  // 한 번에 받을 수 있는 크기에 한계가 있어서(무료는 100MB), 브라우저가 파일을
  // 조각내 보내면 여기서 다시 하나로 잇는다. 폰으로 찍은 긴 영상도 올라간다.
  if (seg[2] === "upload" && method === "POST" && seg[3] === "start") {
    const kind = url.searchParams.get("kind") || "reference";
    const ext = (url.searchParams.get("ext") || ".mp4").toLowerCase();
    if (!["character", "reference", "bg_photo", "music", "person_photo"].includes(kind)) {
      return oops("올릴 수 없는 종류입니다.");
    }
    const name = kind + ext;
    const up = await env.FILES.createMultipartUpload(key(jid, name));
    return json({ name, uploadId: up.uploadId });
  }
  if (seg[2] === "upload" && method === "PUT" && seg[3] === "part") {
    const name = url.searchParams.get("name");
    const uploadId = url.searchParams.get("uploadId");
    const n = parseInt(url.searchParams.get("n") || "0", 10);
    if (!name || !uploadId || !n) return oops("조각 정보가 빠졌습니다.");
    const up = env.FILES.resumeMultipartUpload(key(jid, name), uploadId);
    const part = await up.uploadPart(n, request.body);
    return json({ partNumber: part.partNumber, etag: part.etag });
  }
  if (seg[2] === "upload" && method === "POST" && seg[3] === "finish") {
    const form = await request.formData();
    const name = String(form.get("name") || "");
    const uploadId = String(form.get("uploadId") || "");
    let parts;
    try {
      parts = JSON.parse(String(form.get("parts") || "[]"));
    } catch {
      return oops("조각 목록이 잘못됐습니다.");
    }
    const up = env.FILES.resumeMultipartUpload(key(jid, name), uploadId);
    await up.complete(parts);
    const kind = name.split(".")[0];
    job[kind] = name;
    await writeJob(env, jid, job);
    if (kind === "reference") {
      return runOp(env, jid, "레퍼런스 분석", "analyze_reference", { name });
    }
    return json(job);
  }

  // 파일 올리기 → R2 에 넣고, 필요하면 람다에 분석을 맡긴다
  // 배경·엔딩 사진은 "지우기"도 같은 자리로 온다 (화면이 clear=true 를 보낸다).
  if (method === "POST" && (rest === "character" || rest === "reference" ||
                            rest === "bg_photo" || rest === "music" ||
                            rest === "person_photo" || rest === "ending/photo")) {
    const field = rest === "ending/photo" ? "ending_photo" : rest;
    const form = await request.formData();
    const file = form.get("file");

    if (String(form.get("clear") || "") === "true" || !file || typeof file === "string") {
      if (job[field]) await env.FILES.delete(key(jid, job[field]));
      job[field] = null;
      return json(await writeJob(env, jid, job));
    }
    const ext = (file.name.match(/\.[a-z0-9]+$/i) || [".bin"])[0].toLowerCase();
    const name = field + ext;
    await env.FILES.put(key(jid, name), file.stream(), {
      httpMetadata: { contentType: file.type || "application/octet-stream" },
    });
    job[field] = name;
    if (rest === "character") {
      // 새 그림을 올렸으면 앞 그림의 흔적을 지운다. 안 지우면 다듬기·세우기가
      // 예전 원본(character_raw)에서 다시 그려서, 방금 올린 그림이 무시된다.
      job.character_raw = null;
      job.character_stood = false;
      job.keycut = null;
      job.keycut_approved = false;
    }
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
    if (rest === "character/stand") {
      return runOp(env, jid, "전신으로 세우기", "stand_character",
                   { feedback: s("feedback") });
    }
    if (rest === "character/make") {
      return runOp(env, jid, "캐릭터 만들기", "make_character",
                   { prompt: s("prompt"), use_photo: s("use_photo", "true") === "true" });
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
    if (rest === "render") {
      return startRender(env, url, jid, job, who, ip, s("with_ending", "true") === "true");
    }

    // ── 설정만 고치는 것들 (생성 아님, 빠르고 공짜) ──
    if (rest === "style") {
      if (!STYLE_MODES.includes(s("style_mode"))) return oops("없는 화풍입니다.");
      job.style_mode = s("style_mode");
      return json(await writeJob(env, jid, job));
    }
    if (rest === "keycut/approve") {
      if (!job.keycut) return oops("키컷이 아직 없습니다.");
      job.keycut_approved = s("approved", "true") === "true";
      return json(await writeJob(env, jid, job));
    }
    if (rest === "options") return json(await writeJob(env, jid, setOptions(job, form)));
    if (rest === "ending/options") {
      return json(await writeJob(env, jid, setEndingOptions(job, form)));
    }
    if (rest === "poster") {
      job.poster = [s("l1"), s("l2"), s("l3")].map((x) => x.trim());
      await writeJob(env, jid, job);
      // 이미 만들어둔 엔딩이 있으면 글자만 다시 얹는다 (무료)
      if (job.ending_raw || job.ending_still) {
        return runOp(env, jid, "포스터 문구 반영", "apply_poster_text");
      }
      return json(job);
    }
    if (rest === "subtitles/save") {
      const out = setSubs(job, form);
      if (out.error) return oops(out.error);
      await writeJob(env, jid, out.job);
      // 이미 만든 영상이 있으면 자막만 다시 굽는다 (무료)
      if (out.job.result) return runOp(env, jid, "자막 반영", "apply_subs");
      return json(out.job);
    }
    if (rest === "subtitles/restyle") {
      if (!job.result) return oops("먼저 영상을 만들어주세요.");
      return runOp(env, jid, "모양 반영", "apply_subs");
    }
    if (rest === "ending/still") return runOp(env, jid, "엔딩 장면 만들기", "make_still");
    if (rest === "ending/video") return runOp(env, jid, "엔딩 영상 만들기", "build_ending");
  }

  // 얼마나 드는지 미리 보여주기 (화면의 '만들기' 옆 안내)
  if (rest === "estimate") {
    const CUT_SEC = 6, NB = 0.08, POSTER_VID = 0.54;
    const n = job.segments || 0;
    const secs = job.one_shot === false ? n * CUT_SEC : (job.clip_dur || n * CUT_SEC);
    const dance = Math.round(secs * fal.PRICE_PER_SEC * 100) / 100;
    const ending = Math.round((NB + POSTER_VID) * 100) / 100;
    return json({ segments: n, seconds: Math.round(secs), one_shot: job.one_shot !== false,
                  dance, ending, total: Math.round((dance + ending) * 100) / 100 });
  }

  // 포스터 미리보기 — 설정이 같으면 만들어둔 것을 그대로 준다 (빠르고 공짜)
  if (rest === "poster_preview") {
    const tag = await stamp(env, job);
    const name = `poster_art_${tag}.png`;
    let obj = await env.FILES.get(key(jid, name));
    if (!obj) {
      await invoke(env, { op: "poster_preview", jid, params: {} }, true);
      const made = await env.FILES.get(key(jid, "poster_art.png"));
      if (!made) return oops("미리보기를 만들지 못했습니다.", 500);
      const body = await made.arrayBuffer();
      await env.FILES.put(key(jid, name), body,
                          { httpMetadata: { contentType: "image/png" } });
      return new Response(body, { headers: { "content-type": "image/png",
                                             "cache-control": "no-store" } });
    }
    return new Response(obj.body, { headers: { "content-type": "image/png",
                                               "cache-control": "no-store" } });
  }

  // 완성본 내려받기 — 파일 이름을 작업 제목으로 준다
  if (rest === "download") {
    if (!job.result) return oops("아직 완성된 영상이 없습니다.");
    const obj = await env.FILES.get(key(jid, job.result));
    if (!obj) return oops("파일이 없습니다.", 404);
    const name = encodeURIComponent(`${job.title || "video"}.mp4`);
    return new Response(obj.body, {
      headers: {
        "content-type": "video/mp4",
        "content-disposition": `attachment; filename*=UTF-8''${name}`,
      },
    });
  }

  return oops("없는 주소입니다.", 404);
}

// ── 영상 만들기 ──────────────────────────────────────────────────────────
// fal 에 맡기기만 하고 바로 돌아온다. 다 됐는지는 1분마다 도는 확인이 본다.
// 8~12분 걸리는 일을 붙잡고 기다리지 않으므로, 사람이 몰려도 밀리지 않는다.
async function startRender(env, url, jid, job, who, ip, wantEnding) {
  if (!job.keycut_approved) return oops("키컷을 먼저 확인하고 승인해주세요.");
  if (!job.plate) return oops("레퍼런스 춤 영상을 먼저 올려주세요.");
  if ((job.render_n || 0) > parseInt(env.REDO_LIMIT || "1", 10)) {
    return oops("이 영상은 다시 만들기 횟수를 다 썼습니다. 자막·글꼴·카메라는 " +
                "다시 만들지 않아도 바꿀 수 있습니다.", 429);
  }
  if ((await quotaLeft(env, who, ip)) <= 0) {
    return oops("오늘 만들 수 있는 편수를 다 쓰셨습니다. 내일 다시 이용해 주세요.", 429);
  }      // 사람별 한도가 꺼져 있으면 이 검사는 그냥 지나간다
  const all = await totalLeft(env);
  if (!all.ok) return oops(all.why, 429);

  await usageBump(env, who, 1);
  if (ip) await usageBump(env, `ip:${ip}`, 1);
  await totalBump(env, 1);
  const base = url.origin;
  try {
    const [img, vid] = await Promise.all([
      tempLink(env, base, jid, job.keycut, 6 * 3600),
      tempLink(env, base, jid, job.plate, 6 * 3600),
    ]);
    const got = await fal.submit(env, img, vid);
    job.render_n = (job.render_n || 0) + 1;
    job.pending = {
      stage: "cuts", want_ending: wantEnding,
      // 확인 주소도 같이 적어 둔다. 주소를 지어내면 확인이 안 된다.
      reqs: [{ ...got, out: "cuts/full.mp4", done: false }],
    };
    await writeJob(env, jid, job);
    await env.COUNTS.put(`pend:${jid}`, String(Date.now()), { expirationTtl: 60 * 60 * 6 });
    await writeTask(env, jid, {
      state: "waiting", label: "영상 만들기", done: 0, total: 1,
      msg: "춤 영상 만드는 중 (8~12분) — 창을 닫아도 계속됩니다",
      started: Date.now() / 1000,
    });
    return json(await readTask(env, jid));
  } catch (e) {
    // 실패한 것은 쓴 것으로 치지 않는다 (안 그러면 실패만으로 막힌다)
    await usageBump(env, who, -1);
    if (ip) await usageBump(env, `ip:${ip}`, -1);
    await totalBump(env, -1);
    await writeTask(env, jid, { state: "error", msg: String(e.message || e) });
    return oops(String(e.message || e), 500);
  }
}

// 1분마다 — 맡겨둔 영상이 다 됐는지 보고, 됐으면 마무리를 람다에 넘긴다
async function checkPending(env) {
  const list = await env.COUNTS.list({ prefix: "pend:" });
  for (const k of list.keys) {
    const jid = k.name.slice("pend:".length);
    try {
      const job = await readJob(env, jid);
      const p = job?.pending;
      if (!p || p.stage !== "cuts") { await env.COUNTS.delete(k.name); continue; }

      let allDone = true;
      for (const r of p.reqs) {
        if (r.done) continue;
        const st = await fal.poll(env, r);
        if (st === "failed") throw new Error("생성에 실패했습니다. 다시 시도해주세요.");
        if (st !== "done") { allDone = false; continue; }
        // 다 된 영상을 우리 저장소로 옮긴다
        const src = await fetch(await fal.resultUrl(env, r));
        await env.FILES.put(key(jid, r.out), src.body,
                            { httpMetadata: { contentType: "video/mp4" } });
        r.done = true;
      }
      job.cuts = p.reqs.map((r) => r.out);
      await writeJob(env, jid, job);
      const done = p.reqs.filter((r) => r.done).length;
      await writeTask(env, jid, { state: "waiting", label: "영상 만들기",
                                  done, total: p.reqs.length, started: Date.now() / 1000 });
      if (!allDone) continue;

      // 남은 일(엔딩·합치기·자막)은 람다가 한다. 몇 십 초 걸린다.
      job.pending = { stage: "finishing", want_ending: p.want_ending };
      await writeJob(env, jid, job);
      const startedAt = Date.now() / 1000;
      await writeTask(env, jid, { state: "running", label: "마무리",
                                  msg: "영상 합치는 중", started: startedAt });
      await invoke(env, { op: "finish_up", jid, label: "마무리", started: startedAt,
                          params: { want_ending: p.want_ending } }, false);
      await env.COUNTS.delete(k.name);
    } catch (e) {
      await env.COUNTS.delete(k.name);
      const job = await readJob(env, jid);
      if (job) {                       // 실패는 쓴 것으로 치지 않는다
        job.pending = null;
        job.render_n = Math.max(0, (job.render_n || 1) - 1);
        await writeJob(env, jid, job);
        if (job.owner) await usageBump(env, job.owner, -1);
        await totalBump(env, -1);
      }
      await writeTask(env, jid, { state: "error", msg: String(e.message || e) });
    }
  }
}

// 포스터 설정으로 짧은 지문을 만든다. 같은 설정이면 같은 값이라 다시 안 그린다.
async function stamp(env, job) {
  const src = JSON.stringify([job.poster, job.poster_accent, job.poster_bg,
                              job.poster_ink, job.poster_font, job.poster_bar]);
  const buf = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(src));
  return [...new Uint8Array(buf)].slice(0, 5)
    .map((b) => b.toString(16).padStart(2, "0")).join("");
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
    // fal 이 우리 파일을 가져갈 때 쓰는 임시 주소. 세션과 무관하게 열린다.
    const u = new URL(request.url);
    if (u.pathname.startsWith("/f/")) {
      const [, , jid, ...rest] = u.pathname.split("/");
      const name = decodeURIComponent(rest.join("/"));
      const ok = await linkOk(env, jid, name, u.searchParams.get("e"), u.searchParams.get("s"));
      if (!ok) return oops("주소가 만료됐거나 잘못됐습니다.", 403);
      const obj = await env.FILES.get(key(jid, name));
      if (!obj) return oops("파일이 없습니다.", 404);
      return new Response(obj.body, {
        headers: { "content-type": obj.httpMetadata?.contentType || "application/octet-stream" },
      });
    }

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

  // 1분마다 — 맡겨둔 영상이 다 됐는지 확인하고, 람다가 잠들지 않게 깨워 둔다.
  //
  // 람다는 한동안 안 쓰면 잠들고, 다시 깨우는 데 몇 분이 걸린다(이미지가 2.5GB).
  // 가벼운 인사만 보내 깨워 두면 사용자가 그 몇 분을 안 기다려도 된다.
  // 인사 한 번은 0.1초짜리라 무료 한도에 견줘 없는 셈이다.
  async scheduled(event, env, ctx) {
    ctx.waitUntil(checkPending(env));
    ctx.waitUntil(invoke(env, { op: "ping", jid: "_warm" }, false).catch(() => {}));
  },
};
