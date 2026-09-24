// fal.ai 에 춤 영상 생성을 맡기고, 다 됐는지 확인한다.
//
// 8~12분 걸리는 일이라 "맡겨놓고 나중에 확인"하는 방식(큐)을 쓴다.
// 기다리는 동안 우리 쪽은 아무것도 붙잡고 있지 않는다.
const BASE = "https://queue.fal.run";
const KLING = "fal-ai/kling-video/v2.6/standard/motion-control";
export const PRICE_PER_SEC = 0.07;

const auth = (env) => ({ authorization: `Key ${env.FAL_KEY}`, "content-type": "application/json" });

/** 생성을 맡기고 접수 번호만 받는다. */
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
  return out.request_id;
}

/** "queued" / "running" / "done" / "failed" */
export async function poll(env, id) {
  const r = await fetch(`${BASE}/${KLING}/requests/${id}/status`, { headers: auth(env) });
  if (r.status === 404) return "failed";
  if (!r.ok) return "running";            // 잠깐 끊긴 것과 진짜 실패를 구분한다
  const s = (await r.json()).status || "";
  if (s === "COMPLETED") return "done";
  if (s === "IN_QUEUE") return "queued";
  return "running";
}

/** 다 된 영상의 주소. */
export async function resultUrl(env, id) {
  const r = await fetch(`${BASE}/${KLING}/requests/${id}`, { headers: auth(env) });
  if (!r.ok) throw new Error(`결과 받기 실패 (${r.status})`);
  const out = await r.json();
  const url = out?.video?.url || out?.output?.video?.url;
  if (!url) throw new Error("결과에 영상이 없습니다.");
  return url;
}
