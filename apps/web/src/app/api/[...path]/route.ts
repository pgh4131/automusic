import type { NextRequest } from "next/server";

type Context = { params: Promise<{ path: string[] }> };

async function proxy(request: NextRequest, context: Context) {
  const { path } = await context.params;
  if (path[0] !== "v1" || path.some((part) => !/^[a-zA-Z0-9_-]+$/.test(part))) {
    return Response.json({ detail: "알 수 없는 API 경로입니다." }, { status: 404 });
  }
  const headers = new Headers();
  for (const name of ["content-type", "idempotency-key", "range", "if-range", "if-none-match"]) {
    const value = request.headers.get(name);
    if (value) headers.set(name, value);
  }
  let body: string | undefined;
  if (request.method === "POST") {
    if (Number(request.headers.get("content-length")) > 65_536) {
      return Response.json({ detail: "요청 크기가 너무 큽니다." }, { status: 413 });
    }
    body = await request.text();
    if (new TextEncoder().encode(body).byteLength > 65_536) {
      return Response.json({ detail: "요청 크기가 너무 큽니다." }, { status: 413 });
    }
  }
  const origin = process.env.API_INTERNAL_URL ?? "http://127.0.0.1:8000";
  try {
    const upstream = await fetch(`${origin}/api/${path.join("/")}${request.nextUrl.search}`, {
      method: request.method, headers, body, cache: "no-store", signal: AbortSignal.timeout(30_000),
    });
    const responseHeaders = new Headers();
    for (const name of ["content-type", "content-length", "content-range", "accept-ranges", "content-disposition", "cache-control", "etag", "last-modified"]) {
      const value = upstream.headers.get(name);
      if (value) responseHeaders.set(name, value);
    }
    return new Response(upstream.body, { status: upstream.status, headers: responseHeaders });
  } catch {
    return Response.json({ detail: "API 서버에 연결할 수 없습니다. API와 워커 실행 상태를 확인해 주세요." }, { status: 502 });
  }
}

export const GET = proxy;
export const POST = proxy;
