/**
 * SSE 字节流解析（评审 F1）。
 *
 * sse-starlette 服务端默认以 \r\n 分隔行/事件块（DEFAULT_SEPARATOR），
 * 解析前必须把 \r\n 与孤 \r 归一化为 \n，否则按 "\n\n" 找块边界会永远
 * 失配，浏览器端收不到任何事件。归一化按"安全前缀"进行：缓冲区尾部的
 * 孤 \r 可能是被网络拆开的 \r\n 前半段，须暂缓到配对的 \n 到达后再处理
 * （评审 F2）。抽为纯函数以便离线单测（CRLF/LF/混合/跨 chunk 残块），
 * hook 只消费语义（event/data 对）。
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
      buffer += chunk;
      // 归一化只处理安全前缀：尾部孤 \r 可能是被网络在中间拆开的 \r\n 前半段，
      // 此刻改写成 \n 会与下一 chunk 开头的 \n 拼出伪造的 \n\n 事件边界，把事件
      // 块拦腰截断（event 名丢失降级为 message）。暂缓到下一轮随新字节一起归一化。
      const holdCr = buffer.endsWith("\r");
      const normalized = (holdCr ? buffer.slice(0, -1) : buffer)
        .replace(/\r\n/g, "\n")
        .replace(/\r/g, "\n");
      const messages: SseMessage[] = [];
      let consumed = 0;
      let boundary = normalized.indexOf("\n\n");
      while (boundary >= 0) {
        const message = parseSseBlock(normalized.slice(consumed, boundary));
        if (message !== null) messages.push(message);
        consumed = boundary + 2;
        boundary = normalized.indexOf("\n\n", consumed);
      }
      // 未完结的残块留回 buffer；被暂缓的 \r 原样接回尾部等待配对
      buffer = normalized.slice(consumed) + (holdCr ? "\r" : "");
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
