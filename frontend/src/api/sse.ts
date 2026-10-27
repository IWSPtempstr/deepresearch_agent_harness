/**
 * SSE 字节流解析（评审 F1）。
 *
 * sse-starlette 服务端默认以 \r\n 分隔行/事件块（DEFAULT_SEPARATOR），
 * 解析前必须把 \r\n 与孤 \r 归一化为 \n，否则按 "\n\n" 找块边界会永远
 * 失配，浏览器端收不到任何事件。抽为纯函数以便离线单测（CRLF/LF/混合/
 * 跨 chunk 残块），hook 只消费语义（event/data 对）。
 */

export interface SseMessage {
  name: string;
  data: string;
}

export function createSseParser() {
  let buffer = "";
  return {
    /** 喂入一个网络分块，返回其中已完整解析出的消息（按到达顺序）。 */
    feed(chunk: string): SseMessage[] {
      // 归一化换行：CRLF → LF，孤 \r → LF（覆盖 \r 单独作分隔符的实现）
      buffer += chunk.replace(/\r\n/g, "\n").replace(/\r/g, "\n");
      const messages: SseMessage[] = [];
      let boundary = buffer.indexOf("\n\n");
      while (boundary >= 0) {
        const block = buffer.slice(0, boundary);
        buffer = buffer.slice(boundary + 2);
        const message = parseSseBlock(block);
        if (message !== null) messages.push(message);
        boundary = buffer.indexOf("\n\n");
      }
      return messages;
    },
  };
}

/** 解析单个事件块（不含块间空行）；空块或无 data 行返回 null。 */
export function parseSseBlock(block: string): SseMessage | null {
  let name = "message";
  const dataLines: string[] = [];
  for (const line of block.split("\n")) {
    if (line.startsWith("event:")) name = line.slice(6).trim();
    else if (line.startsWith("data:")) dataLines.push(line.slice(5).trimStart());
    // id: 行不消费——游标只按事件 payload 里的 event_id 推进
  }
  if (!dataLines.length) return null;
  return { name, data: dataLines.join("\n") };
}
