"""T-407 旧 checkpoint 快照检查/转换工具（默认 dry-run；不原地破坏原 checkpoint）。

对非终态 Run 的最新 checkpoint 出具映射清单：
- coreact_v1 游标（LoopState v1）：**可映射**——恢复侧代码适配器
  （loop_state_from_workflow_state）已兼容单动作 pending 的读取；
  --apply 时把归一化结果（pending_batch 归一、next_operation 显式化）作为
  **新增 checkpoint 行**写入（保留原行与原 hash）；
- staged_v1 / 旧 staged（无戳记）：**待人工处理**——staged 引擎只读恢复，
  不自动转换、不换引擎重放；
- deep_research：**不转换**（引擎退役；历史已完成 Run 保持可读）。

安全：默认 dry-run 只输出 JSON 清单；--apply 需显式传入（且仍不修改/删除
任何现有行）；不打印消息正文/秘钥。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
TERMINAL_STATUSES = {"completed", "failed", "cancelled", "budget_exhausted"}


def classify(state_json: str | None) -> tuple[str, str | None]:
    """返回 (分类, 目标描述)。分类 ∈ mappable_coreact / manual_staged /
    not_convertible_deep_research / empty / unparsable。"""
    try:
        payload = json.loads(state_json or "{}")
        if not isinstance(payload, dict):
            raise TypeError("not an object")
    except (TypeError, ValueError):
        return "unparsable", None
    workflow_mode = payload.get("workflow_mode")
    workflow_state = payload.get("workflow_state")
    if workflow_mode == "deep_research":
        return "not_convertible_deep_research", None
    if not isinstance(workflow_state, dict) or not workflow_state:
        return "empty", None
    coreact = workflow_state.get("coreact")
    if isinstance(coreact, dict) and coreact.get("engine") == "coreact_v1":
        try:
            version = int(coreact.get("version") or 0)
        except (TypeError, ValueError):
            return "unparsable", None  # 损坏的版本号：人工处理而非工具崩溃
        if version > 1:
            return "manual_coreact_future_version", None  # 高于实现：人工处理
        return "mappable_coreact", "normalized_loop_cursor"
    return "manual_staged", None


def normalize_coreact_state(state_json: str) -> dict | None:
    """用恢复侧同款适配器归一化 coreact 游标（只读；返回**完整**新状态 dict）。

    checkpoint 的 state_json 是整个 RunContext.model_dump()（见
    harness/checkpoints.py）——只替换 workflow_state.coreact 子树，
    顶层字段原样保留（否则新行无法被 RunContext.model_validate 恢复）。
    """
    from deepresearch_agent.harness.coreact_state import loop_state_from_workflow_state

    payload = json.loads(state_json or "{}")
    state = loop_state_from_workflow_state(payload.get("workflow_state") or {})
    if state is None:
        return None
    workflow_state = {
        **(payload.get("workflow_state") or {}),
        "coreact": state.model_dump(mode="json") | {"next_operation": state.next_operation},
    }
    return {**payload, "workflow_state": workflow_state}


def inspect(db_path: Path, *, apply: bool) -> dict:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro" if not apply else str(db_path), uri=not apply)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "select r.run_id, r.workflow_mode, r.status, c.checkpoint_id, c.version, c.stage, c.state_json, c.state_hash "
            "from runs r join checkpoints c on c.run_id = r.run_id "
            "where r.status not in (" + ",".join("?" * len(TERMINAL_STATUSES)) + ") "
            "and c.version = (select max(version) from checkpoints where run_id = r.run_id)",
            tuple(TERMINAL_STATUSES),
        ).fetchall()
        manifest: list[dict] = []
        for row in rows:
            category, target = classify(row["state_json"])
            item = {
                "run_id": row["run_id"],
                "workflow_mode": row["workflow_mode"],
                "status": row["status"],
                "checkpoint_id": row["checkpoint_id"],
                "checkpoint_version": row["version"],
                "stage": row["stage"],
                "original_hash": row["state_hash"],
                "category": category,
                "target": target,
                "applied": False,
            }
            if apply and category == "mappable_coreact":
                # 已应用检测：本工具产出的迁移行存在则跳过（幂等，不重复追加）
                existing = conn.execute(
                    "select count(*) from checkpoints where run_id = ? and checkpoint_id like ?",
                    (row["run_id"], f"ckpt_mig_{row['run_id']}_%"),
                ).fetchone()[0]
                if existing:
                    item["already_applied"] = True
                    manifest.append(item)
                    continue
                normalized = normalize_coreact_state(row["state_json"])
                if normalized is None:
                    # 归一化失败（畸形游标）：不静默跳过——降级为待人工处理
                    item["category"] = "manual_unparsable_cursor"
                    manifest.append(item)
                    continue
                new_json = json.dumps(normalized, ensure_ascii=False)
                new_hash = hashlib.sha256(new_json.encode("utf-8")).hexdigest()
                # 不原地破坏：追加新版本行，保留原行
                conn.execute(
                    "insert into checkpoints (checkpoint_id, run_id, version, stage, state_json, state_hash, schema_version, created_at) "
                    "values (?, ?, ?, ?, ?, ?, 1, strftime('%Y-%m-%dT%H:%M:%SZ','now'))",
                    (f"ckpt_mig_{row['run_id']}_{row['version'] + 1}", row["run_id"], row["version"] + 1, row["stage"], new_json, new_hash),
                )
                item.update({"applied": True, "new_checkpoint_version": row["version"] + 1, "new_hash": new_hash})
            manifest.append(item)
        if apply:
            conn.commit()
        counts: dict[str, int] = {}
        for item in manifest:
            counts[item["category"]] = counts.get(item["category"], 0) + 1
        return {"mode": "apply" if apply else "dry-run", "non_terminal_runs": len(manifest), "categories": counts, "items": manifest}
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(REPO_ROOT / "data" / "app.db"))
    parser.add_argument("--output", default=None, help="清单 JSON 输出路径；缺省打印 stdout")
    parser.add_argument("--apply", action="store_true", help="对可映射 coreact 游标追加归一化 checkpoint 行（默认 dry-run 不写）")
    args = parser.parse_args()
    report = inspect(Path(args.db), apply=args.apply)
    rendered = json.dumps(report, ensure_ascii=False, indent=2, default=str)
    if args.output:
        Path(args.output).write_text(rendered, encoding="utf-8")
        print(f"manifest written to {args.output}")
    else:
        print(rendered)
    return 0


if __name__ == "__main__":
    sys.exit(main())
