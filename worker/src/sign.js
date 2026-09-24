// 잠깐만 열리는 파일 주소를 만든다.
//
// fal 은 "이 그림과 이 영상으로 만들어줘" 할 때 **주소**를 받는다. 우리 파일은
// R2 에 있고 R2 는 바깥에 안 열려 있다. 그래서 우리 주소로 임시 통로를 낸다.
//
//   /f/<작업>/<파일>?e=<끝나는시각>&s=<서명>
//
// 서명이 맞고 시간이 안 지났을 때만 열린다. 남이 주소를 지어내도 안 열린다.

const enc = new TextEncoder();

async function hmac(secret, text) {
  const k = await crypto.subtle.importKey(
    "raw", enc.encode(secret), { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  const sig = await crypto.subtle.sign("HMAC", k, enc.encode(text));
  return [...new Uint8Array(sig)].map((b) => b.toString(16).padStart(2, "0")).join("").slice(0, 32);
}

export async function tempLink(env, base, jid, name, seconds = 3600) {
  const e = Math.floor(Date.now() / 1000) + seconds;
  const s = await hmac(env.LINK_SECRET, `${jid}/${name}:${e}`);
  return `${base}/f/${jid}/${encodeURIComponent(name)}?e=${e}&s=${s}`;
}

export async function linkOk(env, jid, name, e, s) {
  if (!e || !s || Number(e) < Date.now() / 1000) return false;
  const want = await hmac(env.LINK_SECRET, `${jid}/${name}:${e}`);
  // 길이가 같고 글자가 모두 같을 때만 통과 (시간차로 새어 나가지 않게 한 번에 비교)
  if (want.length !== s.length) return false;
  let diff = 0;
  for (let i = 0; i < want.length; i++) diff |= want.charCodeAt(i) ^ s.charCodeAt(i);
  return diff === 0;
}
