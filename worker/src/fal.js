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

// 카메라를 Kling 쪽에서도 묶으려고 넣었다가, 바로 다음 생성이 422 로
// 실패해서 지금은 **보내지 않는다**. 같은 날 캐릭터 크기도 같이 건드려서
// 둘 중 무엇 때문인지 가릴 수 없었다. 오류 본문을 남기도록 고쳤으니,
// 멀쩡한 생성이 한 번 나온 뒤에 이것만 따로 다시 켜서 확인한다.
//
// 카메라 움직임 자체는 생성이 끝난 뒤 ffmpeg 로 입히므로(공짜) 급하지 않다.
const LOCK_CAMERA =
  "LOCKED-OFF STATIC CAMERA: the camera is on a tripod and does not move at all - " +
  "no pan, no tilt, no zoom, no dolly, no orbit, no handheld shake and no push-in. " +
  "The framing, the shot size and the background stay exactly as in the first image for " +
  "the whole clip. Only the character moves. Do not copy any camera movement from the " +
  "reference video - take only the body motion from it.";

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
  if (!r.ok) throw new Error(`영상 맡기기 실패 (${r.status}) ${why(await r.text())}`);
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
      throw new Error(`생성 상태를 확인할 수 없습니다 (${r.status}) ${why(await r.text())}`);
    }
    return "running";            // 잠깐 끊긴 것은 계속 기다린다
  }
  const s = (await r.json()).status || "";
  if (s === "COMPLETED") return "done";
  if (s === "IN_QUEUE") return "queued";
  if (s === "IN_PROGRESS") return "running";
  return "running";
}

/** 다 된 영상의 주소.
 *
 * fal 은 생성이 **실패했을 때도** 상태를 COMPLETED 로 적고, 진짜 이유는 이
 * 결과 주소에 422 와 함께 담아 준다. 전에는 숫자만 던지고 본문을 버려서
 * "결과 받기 실패 (422)" 만 남았다 — 이유를 알 길이 없었다. 이제 다 적는다.
 */
export async function resultUrl(env, req) {
  const r = await fetch(resultOf(req), { headers: auth(env) });
  if (!r.ok) throw new Error(`결과 받기 실패 (${r.status}) ${why(await r.text())}`);
  const out = await r.json();
  const url = out?.video?.url || out?.output?.video?.url;
  if (!url) throw new Error("결과에 영상이 없습니다. " + why(JSON.stringify(out)));
  return url;
}

/** fal 이 돌려준 본문에서 사람이 읽을 이유만 뽑는다. */
function why(text) {
  try {
    const d = JSON.parse(text).detail ?? JSON.parse(text);
    if (typeof d === "string") return d.slice(0, 300);
    if (Array.isArray(d)) {
      return d.map((x) => [].concat(x.loc || []).join(".") + " " + (x.msg || ""))
              .join(" / ").slice(0, 300);
    }
    return JSON.stringify(d).slice(0, 300);
  } catch {
    return String(text).slice(0, 300);
  }
}
