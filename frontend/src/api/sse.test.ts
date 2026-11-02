import { describe, expect, it } from "vitest";
import { createSseParser, parseSseBlock } from "./sse";

const event = (name: string, payload: object) =>
  `event: ${name}\ndata: ${JSON.stringify(payload)}`;

describe("parseSseBlock", () => {
  it("parses event and data lines, ignores id lines", () => {
    const parsed = parseSseBlock('event: run.completed\nid: 99\ndata: {"event_id": 99}');
    expect(parsed).toEqual({ name: "run.completed", data: '{"event_id": 99}' });
  });

  it("defaults to message name and joins multi-line data", () => {
    const parsed = parseSseBlock("data: line1\ndata: line2");
    expect(parsed).toEqual({ name: "message", data: "line1\nline2" });
  });

  it("returns null for comment-only or empty blocks", () => {
    expect(parseSseBlock(": keep-alive")).toBeNull();
    expect(parseSseBlock("")).toBeNull();
  });
});

describe("createSseParser (review F1: CRLF handling)", () => {
  const payload = { event_id: 7, event_type: "run.completed" };

  it("parses a full CRLF stream — the sse-starlette default that broke \\n\\n matching", () => {
    const parser = createSseParser();
    const messages = parser.feed(`event: run.started\r\ndata: {"event_id": 5}\r\n\r\nevent: run.completed\r\ndata: ${JSON.stringify(payload)}\r\n\r\n`);
    expect(messages).toHaveLength(2);
    expect(messages[0]).toEqual({ name: "run.started", data: '{"event_id": 5}' });
    expect(messages[1]).toEqual({ name: "run.completed", data: JSON.stringify(payload) });
  });

  it("parses lone-CR separated streams (trailing lone CR held until resolved)", () => {
    const parser = createSseParser();
    const messages = parser.feed(`event: a\rdata: {"x":1}\r\revent: b\rdata: {"y":2}\r\r`);
    // 尾部孤 \r 被暂缓（可能是 \r\n 前半段）：再来任意字节即可消歧放出末块
    messages.push(...parser.feed("\n"));
    expect(messages).toHaveLength(2);
    expect(messages.map((item) => item.name)).toEqual(["a", "b"]);
  });

  it("parses LF streams unchanged", () => {
    const parser = createSseParser();
    const messages = parser.feed(`event: x\ndata: {"k":1}\n\n`);
    expect(messages).toEqual([{ name: "x", data: '{"k":1}' }]);
  });

  it("reassembles an event block split across network chunks (CRLF split mid-block)", () => {
    const parser = createSseParser();
    expect(parser.feed(`event: run.failed\r\nda`)).toEqual([]);
    expect(parser.feed(`ta: {"event_id": 9}\r\n\r\n`)).toEqual([
      { name: "run.failed", data: '{"event_id": 9}' },
    ]);
  });

  it("holds a chunk-trailing lone \\r until its pair \\n arrives — no spurious \\n\\n boundary (review F2 regression)", () => {
    const parser = createSseParser();
    // sse-starlette 的 CRLF 恰被网络在 \r 与 \n 之间拆开：旧实现把 chunk1 尾部
    // 的 \r 改写成 \n，与 chunk2 开头的 \n 拼出伪造 \n\n 边界，把心跳块拦腰
    // 截断——event 名丢失降级为 message，上层按 name 过滤心跳随之失效
    expect(parser.feed(`event: heartbeat\r`)).toEqual([]);
    expect(parser.feed(`\ndata: {"event_id": 1}\r\n\r\n`)).toEqual([
      { name: "heartbeat", data: '{"event_id": 1}' },
    ]);
    // 后续事件不受残块影响，照常解析
    expect(parser.feed(`event: run.completed\r\ndata: {"event_id": 2}\r\n\r\n`)).toEqual([
      { name: "run.completed", data: '{"event_id": 2}' },
    ]);
  });

  it("keeps trailing partial block buffered until completion", () => {
    const parser = createSseParser();
    expect(parser.feed(event("run.queued", { event_id: 1 }) + "\r\n")).toEqual([]);
    expect(parser.feed("\r\n")).toEqual([{ name: "run.queued", data: JSON.stringify({ event_id: 1 }) }]);
  });
});
