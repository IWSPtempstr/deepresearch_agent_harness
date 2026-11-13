import { FormEvent, useState } from "react";

/** BC-01：展示后端给出的**实际问题**；幂等键由调用方生成。 */
export function ClarificationCard({ questions = [], onSubmit }: {
  questions?: string[];
  onSubmit: (content: string) => Promise<void>;
}) {
  const [value, setValue] = useState(""); const [busy, setBusy] = useState(false);
  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!value.trim() || busy) return;
    setBusy(true);
    try { await onSubmit(value.trim()); setValue(""); } finally { setBusy(false); }
  }
  return <form className="clarification-card" onSubmit={submit}>
    <strong>需要你的补充信息</strong>
    {questions.length > 0
      ? <ul className="clarification-questions">{questions.map((question, index) => <li key={index}>{question}</li>)}</ul>
      : <p>系统需要更多信息才能继续当前 Run。</p>}
    <p>回答会写入当前会话并作为后续规划的约束；同一 Run 不会重复询问已回答的问题。</p>
    <div><input value={value} onChange={(event) => setValue(event.target.value)} placeholder="输入澄清信息" /><button disabled={busy}>{busy ? "提交中…" : "继续运行"}</button></div>
  </form>;
}
