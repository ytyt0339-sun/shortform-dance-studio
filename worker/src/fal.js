// fal.ai 에 춤 영상 생성을 맡기고, 다 됐는지 확인한다.
//
// 8~12분 걸리는 일이라 "맡겨놓고 나중에 확인"하는 방식(큐)을 쓴다.
// 기다리는 동안 우리 쪽은 아무것도 붙잡고 있지 않는다.
//
// 주의 — 맡길 때와 확인할 때 주소 모양이 다르다.
//   맡기기: https://queue.fal.run/fal-ai/kling-video/v2.6/standard/motion-control
//   확인  : https://queue.fal.run/fal-ai/kling-video/requests/<번호>/status
// 확인 주소를 지어내면 405 가 나고, 그걸 "아직 만드는 중" 으로 오해하면
// 영원히 기다리게 된다 (실제로 그랬다). 그래서 맡길 때 받은 주소를 그대로 쓴다.
const BASE = "https://queue.fal.run";
const KLING = "fal-ai/kling-video/v2.6/standard/motion-control";
const KLING_ROOT = "fal-ai/kling-video";      // 확인용 짧은 주소
export const PRICE_PER_SEC = 0.07;

const auth = (env) => ({ authorization: `Key ${env.FAL_KEY}`, "content-type": "application/json" });

/** 생성을 맡기고 접수 번호와 확인용 주소를 받는다. */
export async function submit(env, imageUrl, videoUrl) {
  const r = await fetch(`${BASE}/${KLING}`, {
    method: "POST",
    headers: auth(env),
    body: JSON.stringify({
      image_url: imageUrl,
      video_url: videoUrl,
      character_orientation: "video",
      keep_original_sound: false,
    }),
  });
  if (!r.ok) throw new Error(`영상 맡기기 실패 (${r.status}) ${(await r.text()).slice(0, 200)}`);
  const out = await r.json();
  return {
    id: out.request_id,
    statusUrl: out.status_url || `${BASE}/${KLING_ROOT}/requests/${out.request_id}/status`,
    resultUrl: out.response_url || `${BASE}/${KLING_ROOT}/requests/${out.request_id}`,
  };
}

const statusOf = (req) =>
  req.statusUrl || `${BASE}/${KLING_ROOT}/requests/${req.id}/status`;
const resultOf = (req) =>
  req.resultUrl || `${BASE}/${KLING_ROOT}/requests/${req.id}`;

/** "queued" / "running" / "done" / "failed" / "unknown" */
export async function poll(env, req) {
  const r = await fetch(statusOf(req), { headers: auth(env) });
  if (r.status === 404) return "failed";
  if (!r.ok) {
    // 4xx 는 우리가 잘못 물어본 것이다. 조용히 기다리면 영영 안 끝난다.
    if (r.status >= 400 && r.status < 500) {
      throw new Error(`생성 상태를 확인할 수 없습니다 (${r.status})`);
    }
    return "running";            // 잠깐 끊긴 것은 계속 기다린다
  }
  const s = (await r.json()).status || "";
  if (s === "COMPLETED") return "done";
  if (s === "IN_QUEUE") return "queued";
  if (s === "IN_PROGRESS") return "running";
  return "running";
}

/** 다 된 영상의 주소. */
export async function resultUrl(env, req) {
  const r = await fetch(resultOf(req), { headers: auth(env) });
  if (!r.ok) throw new Error(`결과 받기 실패 (${r.status})`);
  const out = await r.json();
  const url = out?.video?.url || out?.output?.video?.url;
  if (!url) throw new Error("결과에 영상이 없습니다.");
  return url;
}
