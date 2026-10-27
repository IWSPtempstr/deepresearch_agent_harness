import { useEffect, useRef, useState } from "react";
import { API_BASE, api, authHeaders } from "../api/client";
import { createSseParser, SseMessage } from "../api/sse";
import { Run, RunEvent } from "../types/api";

const terminal = new Set(["completed", "failed", "cancelled", "budget_exhausted", "paused"]);

/** 统一的后端状态轮询间隔（毫秒） */
export const RUN_POLL_MS = 2000;

export function useRunEvents(runId?: string | null) {
  const [generation, setGeneration] = useState(0);
  const [events, setEvents] = useState<RunEvent[]>([]);
  const [run, setRun] = useState<Run | null>(null);
  const [connection, setConnection] = useState<"idle" | "connected" | "reconnecting">("idle");
  const cursor = useRef(0);
  const runRef = useRef<Run | null>(null); // 轮询闭包读最新状态，避免过期闭包
  const polling = useRef(false); // 在途请求守卫，防重入

  useEffect(() => {
    if (!runId) { setEvents([]); setRun(null); setConnection("idle"); runRef.current = null; return; }
    let closed = false;
    const controller = new AbortController();
    let reconnectTimer: number | undefined; let pollTimer: number | undefined;
    const sync = () => api.run(runId).then((value) => { runRef.current = value; setRun(value); return value; }).catch(() => undefined);
    const poll = () => {
      if (polling.current) return;
      const current = runRef.current?.status ?? "";
      if (terminal.has(current)) { window.clearInterval(pollTimer); return; }
      polling.current = true;
      api.run(runId).then((value) => { runRef.current = value; setRun(value); }).catch(() => undefined).finally(() => { polling.current = false; });
    };
    // fetch 流式 SSE 替代 EventSource：EventSource 不能携带 X-API-Key 请求头，
    // 且本 hook 的断线重连本就是手写游标续传，原生自动重连并未被依赖。
    const connect = async () => {
      if (closed) return;
      let response: Response;
      try {
        response = await fetch(`${API_BASE}/runs/${runId}/events?after_event_id=${cursor.current}`, {
          headers: authHeaders(),
          signal: controller.signal,
        });
      } catch {
        if (!closed) { setConnection("reconnecting"); reconnectTimer = window.setTimeout(connect, 1_000); }
        return;
      }
      if (closed) return;
      if (response.status === 401 || !response.ok || !response.body) {
        setConnection("reconnecting");
        sync().then((value) => {
          if (terminal.has(value?.status ?? "")) setConnection("idle");
          else if (!closed) reconnectTimer = window.setTimeout(connect, 1_000);
        });
        return;
      }
      setConnection("connected");
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      // 解析在 api/sse.ts 纯函数中（含 CRLF 归一化，评审 F1）；此处只消费语义
      const parser = createSseParser();
      const handleMessages = (messages: SseMessage[]) => {
        for (const parsed of messages) {
          if (parsed.name === "heartbeat") continue;
          try {
            const data = JSON.parse(parsed.data) as RunEvent;
            if (typeof data.event_id === "number") cursor.current = Math.max(cursor.current, data.event_id);
            setEvents((items) => items.some((item) => item.event_id === data.event_id) ? items : [...items, data]);
            if (String(data.event_type).startsWith("run.")) sync();
          } catch { /* 忽略无法解析的残块 */ }
        }
      };
      try {
        for (;;) {
          const { done, value } = await reader.read();
          if (done || closed) break;
          handleMessages(parser.feed(decoder.decode(value, { stream: true })));
        }
      } catch { /* 连接被中断，走下方统一重连 */ }
      if (!closed) {
        // 流结束：服务端终态返回，或连接断开——查状态决定待机还是续传重连
        setConnection("reconnecting");
        sync().then((value) => {
          if (terminal.has(value?.status ?? "")) setConnection("idle");
          else if (!closed) reconnectTimer = window.setTimeout(connect, 1_000);
        });
      }
    };
    setEvents([]); cursor.current = 0; runRef.current = null; sync(); void connect();
    pollTimer = window.setInterval(poll, RUN_POLL_MS);
    return () => { closed = true; controller.abort(); if (reconnectTimer) window.clearTimeout(reconnectTimer); if (pollTimer) window.clearInterval(pollTimer); };
  }, [runId, generation]);

  return {
    events, run, connection,
    refresh: () => runId ? api.run(runId).then((value) => { runRef.current = value; setRun(value); }) : Promise.resolve(),
    restart: () => setGeneration((value) => value + 1),
  };
}
