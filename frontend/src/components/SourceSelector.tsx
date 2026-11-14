import { Capabilities, SourceMode } from "../types/api";

const CHANNEL_LABEL: Record<SourceMode, string> = {
  graphrag: "私有数据库",
  web: "联网搜索",
};

/** S5：多选来源（≥1）。交互语义：
 * - 点击未选通道 → 追加为次通道；
 * - 点击已选次通道 → 提升为主通道（首位）；
 * - 点击主通道 → 移除该通道（集合为 1 时无操作，保证 ≥1）。
 */
export function SourceSelector({ values, onChange, capabilities }: {
  values: SourceMode[];
  onChange: (modes: SourceMode[]) => void;
  capabilities?: Capabilities;
}) {
  function toggle(mode: SourceMode) {
    if (!values.includes(mode)) {
      onChange([...values, mode]);
      return;
    }
    if (values[0] === mode) {
      if (values.length > 1) onChange(values.filter((item) => item !== mode));
      return;
    }
    onChange([mode, ...values.filter((item) => item !== mode)]);
  }
  return <div className="source-selector" aria-label="信息源（可多选）">
    {(["graphrag", "web"] as SourceMode[]).map((mode) => {
      const available = capabilities?.sources[mode]?.available ?? true;
      const selected = values.includes(mode);
      const primary = values[0] === mode;
      const webProviders = capabilities?.web_providers;
      const tooltip = mode === "web" && webProviders
        ? (webProviders.tavily ? "Tavily + 学术 API（OpenAlex/arXiv）可用" : "Tavily 未配置：本通道将使用学术 API（OpenAlex/arXiv）")
        : capabilities?.sources[mode]?.reason ?? undefined;
      return <label
        key={mode}
        className={`${selected ? "selected" : ""} ${!available ? "disabled" : ""}`}
        title={tooltip}
      >
        <input
          type="checkbox"
          value={mode}
          checked={selected}
          disabled={!available}
          onChange={() => toggle(mode)}
        />
        <span>{CHANNEL_LABEL[mode]}{selected && primary ? "（主）" : ""}</span>
      </label>;
    })}
  </div>;
}
