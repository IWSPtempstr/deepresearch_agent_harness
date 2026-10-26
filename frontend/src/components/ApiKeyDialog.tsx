import { useEffect, useState } from "react";
import type { CSSProperties } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { clearApiKey, getApiKey, onUnauthorized, setApiKey } from "../api/client";

const overlayStyle: CSSProperties = {
  position: "fixed", inset: 0, background: "rgba(0,0,0,0.45)", zIndex: 1000,
  display: "flex", alignItems: "center", justifyContent: "center",
};
const panelStyle: CSSProperties = {
  background: "var(--panel, #1b1e27)", color: "inherit", borderRadius: 10,
  padding: "20px 22px", width: 420, maxWidth: "92vw", boxShadow: "0 12px 40px rgba(0,0,0,0.4)",
};
const inputStyle: CSSProperties = {
  width: "100%", boxSizing: "border-box", marginTop: 8, marginBottom: 14,
  padding: "8px 10px", borderRadius: 6, border: "1px solid rgba(127,127,127,0.45)",
  background: "transparent", color: "inherit", fontFamily: "monospace",
};
const triggerStyle: CSSProperties = {
  position: "fixed", top: 12, right: 14, zIndex: 900, borderRadius: 6,
  border: "1px solid rgba(127,127,127,0.4)", background: "transparent",
  color: "inherit", padding: "4px 9px", cursor: "pointer", fontSize: 13,
};

/** X-API-Key 设置入口：手动打开，或任意请求 401 时自动弹出重新采集。 */
export function ApiKeyDialog() {
  const queryClient = useQueryClient();
  const [open, setOpen] = useState(false);
  const [value, setValue] = useState("");
  const [hint, setHint] = useState("");

  useEffect(() => onUnauthorized(() => { setHint("当前请求被拒绝（401）：请粘贴有效的 API Key"); setValue(getApiKey()); setOpen(true); }), []);

  function save() {
    setApiKey(value);
    setOpen(false);
    setHint("");
    // 凭据可能从无到有：刷新全部查询让页面立即用新 key 重取
    void queryClient.invalidateQueries();
  }

  return (
    <>
      <button style={triggerStyle} title="API Key 设置（租户认证）" onClick={() => { setValue(getApiKey()); setHint(""); setOpen(true); }}>
        🔑 Key
      </button>
      {open && (
        <div style={overlayStyle} onClick={() => setOpen(false)}>
          <div style={panelStyle} onClick={(event) => event.stopPropagation()}>
            <div style={{ fontSize: 15, fontWeight: 600, marginBottom: 6 }}>API Key（X-API-Key）</div>
            <div style={{ fontSize: 12, opacity: 0.75, lineHeight: 1.6 }}>
              平台模式启用租户认证后必填；由 <code>scripts/create_tenant.py</code> 签发，仅显示一次。
              留空表示以默认租户访问（后端未签发任何 key 时可用）。
            </div>
            <input
              style={inputStyle}
              value={value}
              placeholder="dra-..."
              onChange={(event) => setValue(event.target.value)}
              onKeyDown={(event) => { if (event.key === "Enter") save(); }}
              autoFocus
            />
            {hint && <div style={{ color: "#e8a33d", fontSize: 12, marginBottom: 10 }}>{hint}</div>}
            <div style={{ display: "flex", gap: 8, justifyContent: "flex-end" }}>
              <button style={{ padding: "6px 12px" }} onClick={() => { clearApiKey(); setValue(""); setOpen(false); setHint(""); void queryClient.invalidateQueries(); }}>
                清除
              </button>
              <button style={{ padding: "6px 12px" }} onClick={() => { setOpen(false); setHint(""); }}>
                取消
              </button>
              <button style={{ padding: "6px 14px" }} onClick={save}>
                保存
              </button>
            </div>
          </div>
        </div>
      )}
    </>
  );
}
