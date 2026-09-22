// 무거운 일을 AWS Lambda 에 넘긴다.
//
// 두 가지 방식이 있다.
//   기다림(sync)  자세 인식처럼 1분 안에 끝나는 일. 결과를 바로 받는다.
//   맡김(async)   합치기처럼 오래 걸리는 일. 람다가 R2 에 결과를 적고,
//                 화면은 task.json 을 보며 기다린다.
//
// 서명(SigV4)은 aws4fetch 가 해준다. Worker 안에서도 도는 작은 라이브러리다.
import { AwsClient } from "aws4fetch";

let client = null;

function aws(env) {
  if (!client) {
    client = new AwsClient({
      accessKeyId: env.AWS_ACCESS_KEY_ID,
      secretAccessKey: env.AWS_SECRET_ACCESS_KEY,
      region: env.AWS_REGION || "ap-northeast-2",
      service: "lambda",
    });
  }
  return client;
}

/**
 * 람다를 부른다.
 * @param wait true 면 끝날 때까지 기다렸다 결과를 돌려준다.
 */
export async function invoke(env, payload, wait = true) {
  const region = env.AWS_REGION || "ap-northeast-2";
  const name = env.LAMBDA_NAME || "dance-studio";
  const url = `https://lambda.${region}.amazonaws.com/2015-03-31/functions/${name}/invocations`;

  const r = await aws(env).fetch(url, {
    method: "POST",
    headers: {
      "content-type": "application/json",
      // Event = 맡기고 바로 돌아온다. RequestResponse = 끝날 때까지 기다린다.
      "x-amz-invocation-type": wait ? "RequestResponse" : "Event",
    },
    body: JSON.stringify(payload),
  });

  if (!wait) {
    if (r.status !== 202) throw new Error(`람다 맡기기 실패 (${r.status})`);
    return { ok: true, queued: true };
  }
  if (!r.ok) throw new Error(`람다 호출 실패 (${r.status}) ${(await r.text()).slice(0, 200)}`);
  const out = await r.json();
  if (out && out.ok === false) throw new Error(out.error || "람다에서 실패했습니다.");
  return out;
}
