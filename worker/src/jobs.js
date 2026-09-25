// 작업 기록을 R2 에 두고 읽는다.
//
// 왜 R2 인가: 람다도 같은 파일을 읽고 쓴다. 한 곳에만 두면 둘이 어긋날 일이 없다.
// 목록은 R2 를 훑으면 느려서, 사람마다 KV 에 따로 적어 둔다.

export const key = (jid, name) => `runs/${jid}/${name}`;

export async function readJob(env, jid) {
  const o = await env.FILES.get(key(jid, "job.json"));
  return o ? await o.json() : null;
}

export async function writeJob(env, jid, job) {
  await env.FILES.put(key(jid, "job.json"), JSON.stringify(job, null, 2), {
    httpMetadata: { contentType: "application/json" },
  });
  return job;
}

export async function readTask(env, jid) {
  const o = await env.FILES.get(key(jid, "task.json"));
  return o ? await o.json() : { state: "idle" };
}

export async function writeTask(env, jid, task) {
  await env.FILES.put(key(jid, "task.json"), JSON.stringify(task));
  return task;
}

// 남의 작업은 없는 것처럼 대한다. 주소를 알아도 못 연다.
export async function ownedJob(env, jid, who) {
  const job = await readJob(env, jid);
  if (!job || job.owner !== who) return null;
  return job;
}

// 사람마다 자기 작업 목록을 KV 에 들고 있는다 (R2 를 매번 훑지 않으려고)
const listKey = (who) => `jobs:${who}`;

export async function indexAdd(env, who, jid) {
  const cur = JSON.parse((await env.COUNTS.get(listKey(who))) || "[]");
  if (!cur.includes(jid)) cur.unshift(jid);
  await env.COUNTS.put(listKey(who), JSON.stringify(cur.slice(0, 50)));
}

export async function indexDrop(env, who, jid) {
  const cur = JSON.parse((await env.COUNTS.get(listKey(who))) || "[]");
  await env.COUNTS.put(listKey(who), JSON.stringify(cur.filter((x) => x !== jid)));
}

export async function listJobs(env, who) {
  const ids = JSON.parse((await env.COUNTS.get(listKey(who))) || "[]");
  const out = [];
  for (const jid of ids) {
    const j = await readJob(env, jid);
    if (!j || j.owner !== who) continue;        // 지워졌거나 남의 것
    out.push({
      id: j.id, title: j.title, created: j.created,
      result: Boolean(j.result), spent: j.spent || 0,
    });
  }
  return out;
}

// 작업 하나를 통째로 지운다 (R2 에 남은 파일까지)
export async function dropJob(env, jid) {
  let cursor;
  do {
    const page = await env.FILES.list({ prefix: `runs/${jid}/`, cursor });
    if (page.objects.length) await env.FILES.delete(page.objects.map((o) => o.key));
    cursor = page.truncated ? page.cursor : undefined;
  } while (cursor);
}

export function blankJob(jid, who) {
  const now = new Date();
  const p = (n) => String(n).padStart(2, "0");
  return {
    id: jid,
    owner: who,
    title: "새 영상",
    created: `${now.getFullYear()}-${p(now.getMonth() + 1)}-${p(now.getDate())} ` +
             `${p(now.getHours())}:${p(now.getMinutes())}`,
    style_mode: "3d",
    character: null, character_raw: null, character_n: 0,
    bg_photo: null, bg_prompt: "", keycut: null, keycut_n: 0, keycut_approved: false,
    reference: null, plate: null, crop: null, clip_start: 0, clip_dur: 0,
    segments: 0, seg_recs: null, clip_score: null, ref_audio: null,
    music: null, music_src: "reference",
    subs: [], subs_on: false, subs_style: "soft", subs_pos: "top",
    poster: ["", "", ""], poster_mode: "bake", poster_bar: true,
    ending_kind: "wall", ending_pose: "point", ending_sec: 4,
    ending_still: null, ending_still_n: 0, ending: null, ending_raw: null,
    camera: "normal", one_shot: true,
    cuts: [], pending: null, result: null, render_n: 0, spent: 0.0,
  };
}
