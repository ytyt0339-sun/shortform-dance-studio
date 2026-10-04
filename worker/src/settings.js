// 화면에서 바꾸는 설정들. 생성이 아니라 기록만 고치는 것이라 값이 싸고 빠르다.
//
// 지금 서버(파이썬)와 **같은 규칙**을 쓴다. 모르는 값이 오면 조용히 무시한다
// (화면이 옛 버전이어도 깨지지 않게).

// 서버(파이썬)가 실제로 아는 값들. 여기 없는 값이 오면 조용히 무시한다.
// 값을 늘릴 때는 파이썬 쪽(pipeline.py)과 **양쪽 다** 고쳐야 한다.
export const CAMERAS = ["none", "push", "sway", "sway_strong", "orbit", "punch", "handheld"];
export const SUB_STYLES = ["soft", "bold", "serif"];
export const STYLE_MODES = ["3d", "doodle"];
export const POSTER_KINDS = ["wall", "board", "banner", "frame"];
export const ENDING_POSES = ["point", "wave", "open", "look"];

const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
const num = (v) => (v === null || v === "" || isNaN(Number(v)) ? null : Number(v));

/** 음악 출처, 생성 방식, 자막 모양 */
export function setOptions(job, f) {
  const s = (k) => (f.has(k) ? String(f.get(k)) : null);

  // 음악 파일 따로 올리기는 없앴다. 남는 길은 춤 영상 소리와 소리 없음뿐이다.
  if (["reference", "none"].includes(s("music_src"))) job.music_src = s("music_src");
  if (["true", "false"].includes(s("one_shot"))) job.one_shot = s("one_shot") === "true";
  if (["top", "bottom"].includes(s("subs_pos"))) job.subs_pos = s("subs_pos");
  if (SUB_STYLES.includes(s("subs_style"))) job.subs_style = s("subs_style");
  if (s("subs_font") !== null) job.subs_font = s("subs_font");

  const size = num(s("subs_size"));
  if (size !== null) job.subs_size = clamp(size, 0, 0.09);
  for (const k of ["subs_color", "subs_outline_color"]) if (s(k)) job[k] = s(k);

  const out = num(s("subs_outline"));
  if (out !== null) job.subs_outline = clamp(out, 0, 0.3);
  if (["true", "false"].includes(s("subs_bold"))) job.subs_bold = s("subs_bold") === "true";
  if (CAMERAS.includes(s("camera"))) job.camera = s("camera");
  if (["true", "false"].includes(s("camera_in_gen"))) {
    job.camera_in_gen = s("camera_in_gen") === "true";
  }
  return job;
}

/** 엔딩 장면과 포스터 디자인 */
export function setEndingOptions(job, f) {
  const s = (k) => (f.has(k) ? String(f.get(k)) : null);

  if (POSTER_KINDS.includes(s("ending_kind"))) job.ending_kind = s("ending_kind");
  if (ENDING_POSES.includes(s("ending_pose"))) job.ending_pose = s("ending_pose");
  if (s("ending_scene") !== null) job.ending_scene = s("ending_scene").trim();
  if (s("ending_prompt") !== null) job.ending_prompt = s("ending_prompt").trim();

  const sec = num(s("ending_sec"));
  if (sec !== null) job.ending_sec = clamp(Math.round(sec), 4, 8);
  for (const k of ["poster_accent", "poster_bg", "poster_ink"]) if (s(k)) job[k] = s(k);
  if (s("poster_font") !== null) job.poster_font = s("poster_font");
  if (["true", "false"].includes(s("poster_bar"))) job.poster_bar = s("poster_bar") === "true";
  if (["bake", "overlay"].includes(s("poster_mode"))) job.poster_mode = s("poster_mode");
  return job;
}

/** 고친 자막 저장 */
export function setSubs(job, f) {
  let items;
  try {
    items = JSON.parse(String(f.get("subs") ?? "[]"));
  } catch {
    return { error: "자막 형식이 잘못됐습니다." };
  }
  job.subs = items.map((x) => ({
    start: Number(x.start || 0), end: Number(x.end || 0), text: String(x.text || ""),
  }));
  job.subs_on = String(f.get("subs_on") ?? "true") === "true";
  return { job };
}


// 레퍼런스 적합도 판정. 지금 서버(파이썬)와 같은 규칙을 쓴다.
const MIN_SCORE = 70, WARN_SCORE = 85;
const TIPS = {
  "전신노출": "머리부터 발끝까지 다 나오는 구간이 부족합니다. 다리가 잘리면 캐릭터 다리도 뭉개집니다.",
  "단독인물": "다른 사람이 함께 잡힙니다.",
  "검출안정성": "동작이 흐릿하거나 빨라서 사람을 놓치는 구간이 많습니다.",
  "화면내유지": "인물이 화면 밖으로 자주 벗어납니다.",
  "동작크기": "동작이 너무 작아서 캐릭터가 거의 안 움직일 수 있습니다.",
  "컷전환없음": "중간에 컷이 바뀝니다. 한 번에 찍은 영상이어야 합니다.",
};

export function scoreCheck(job) {
  const sc = job.clip_score;
  if (!sc) return null;
  const score = sc.score || 0;
  const reasons = [...(sc.fail || [])];
  for (const [k, v] of Object.entries(sc.sub || {})) {
    if (TIPS[k] && v < 0.6 && !reasons.includes(TIPS[k])) reasons.push(TIPS[k]);
  }
  const level = (!sc.ok || score < MIN_SCORE) ? "block" : (score < WARN_SCORE ? "warn" : "ok");
  return { level, score, reasons, min: MIN_SCORE, warn: WARN_SCORE };
}
