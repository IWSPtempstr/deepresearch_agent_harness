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

  it("parses lone-CR separated streams", () => {
    const parser = createSseParser();
    const messages = parser.feed(`event: a\rdata: {"x":1}\r\revent: b\rdata: {"y":2}\r\r`);
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
    expect(parser.feed(`ta: {"event_id": 9}\r\r`)).toEqual([
      { name: "run.failed", data: '{"event_id": 9}' },
    ]);
  });

  it("keeps trailing partial block buffered until completion", () => {
    const parser = createSseParser();
    expect(parser.feed(event("run.queued", { event_id: 1 }) + "\r\n")).toEqual([]);
    expect(parser.feed("\r\n")).toEqual([{ name: "run.queued", data: JSON.stringify({ event_id: 1 }) }]);
  });
});
