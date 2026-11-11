"""T-401 存量数据与旧路径盘点（只读；默认 dry-run）。

用途：唯一 coreact 入口切换前的实施 gate 证据。
- 只读连接 SQLite 数据库（mode=ro），不执行任何 UPDATE/DELETE/DDL；
  生产 PostgreSQL 用只读账号执行同款 SQL（或将库镜像为 SQLite 后重跑本脚本）；
- 统计旧 workflow_mode 的 Run 终态/未终态数量；
- 统计 checkpoint 版本分布与编排实现标记（coreact_v1 / staged_v1 / deep_research）；
- 列出未终态 Run 的最新 checkpoint 阶段（判定"可映射 / 待人工处理"）；
- 静态扫描旧引擎入口的直接文本引用（启发式清单，非编译级分析）。

安全说明：
- 不打印任何秘钥、用户消息正文或报告正文；仅输出计数、ID 与阶段名；
- 脚本自身无写库路径（SQLite 可能生成空的 -wal/-shm sidecar，不写库内容）。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# 终态集合与 harness/contracts.RunStatus 对齐（含历史值）。
TERMINAL_STATUSES = {"completed", "failed", "cancelled", "budget_exhausted"}
# 可安全转换的未终态（有 checkpoint 且状态可映射）；其余进人工处理清单。
# interrupted 且无 checkpoint 的旧 Run 不自动续跑，保持原状。
OLD_WORKFLOW_MODES = ("deep_research", "plan_execute_report")


def inspect_database(db_path: Path) -> dict:
    """只读盘点 SQLite 数据库（生产 PostgreSQL 用只读账号执行同款 SQL）。"""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        result: dict = {"database": str(db_path), "runs": {}, "checkpoints": {}, "non_terminal": []}
        modes = conn.execute(
            "select workflow_mode, status, count(*) as n from runs group by workflow_mode, status"
        ).fetchall()
        for row in modes:
            result["runs"].setdefault(row["workflow_mode"], {})[row["status"]] = row["n"]

        ck_cols = [r["name"] for r in conn.execute("PRAGMA table_info(checkpoints)").fetchall()]
        version_col = "checkpoint_version" if "checkpoint_version" in ck_cols else "schema_version"
        result["checkpoints"]["total"] = conn.execute("select count(*) from checkpoints").fetchone()[0]
        result["checkpoints"]["by_" + version_col] = {
            str(r[0]): r[1]
            for r in conn.execute(
                f"select {version_col}, count(*) from checkpoints group by {version_col}"
            ).fetchall()
        }

        # checkpoint 编排实现标记（分批扫描 state_json；只读键存在性，不载入正文）。
        # 位置与实现对齐：payload["workflow_mode"] 是 Run 的模式（权威）；
        # plan_execute_report 的引擎标记在 workflow_state["orchestration"]
        # （harness/driver_adapters.py snapshot()；缺标记=旧 staged 实现）。
        markers = {
            "coreact_v1": 0, "staged_v1": 0, "staged_legacy": 0, "deep_research": 0,
            "empty_workflow_state": 0, "unparsable": 0,
        }
        for (state_json,) in conn.execute("select state_json from checkpoints"):
            try:
                payload = json.loads(state_json or "{}")
                if not isinstance(payload, dict):
                    raise TypeError("state_json is not an object")
                workflow_mode = payload.get("workflow_mode")
                workflow_state = payload.get("workflow_state")
                if workflow_mode == "deep_research":
                    markers["deep_research"] += 1
                elif not isinstance(workflow_state, dict) or not workflow_state:
                    markers["empty_workflow_state"] += 1
                elif workflow_state.get("orchestration") == "coreact_v1":
                    markers["coreact_v1"] += 1
                elif workflow_state.get("orchestration") == "staged_v1":
                    markers["staged_v1"] += 1
                elif workflow_mode == "plan_execute_report":
                    # 快照无 orchestration 标记 = 旧 staged 实现（drivers_plan_execute._restore 同规则）
                    markers["staged_legacy"] += 1
                else:
                    markers["unparsable"] += 1
            except (TypeError, ValueError, AttributeError):
                markers["unparsable"] += 1
        result["checkpoints"]["markers"] = markers

        # 未终态 Run：最新 checkpoint 阶段 + 是否可映射
        non_terminal = conn.execute(
            "select run_id, workflow_mode, status, current_stage from runs "
            f"where workflow_mode in ({','.join('?' * len(OLD_WORKFLOW_MODES))}) "
            "and status not in (" + ",".join("?" * len(TERMINAL_STATUSES)) + ")",
            (*OLD_WORKFLOW_MODES, *TERMINAL_STATUSES),
        ).fetchall()
        for row in non_terminal:
            latest = conn.execute(
                "select stage, " + version_col + " from checkpoints where run_id = ? "
                "order by version desc limit 1",
                (row["run_id"],),
            ).fetchone()
            # 无 checkpoint 的未终态 Run：没有可恢复的执行现场，禁止盲目续跑。
            mapping = "manual_review" if latest is None else f"checkpoint_stage:{latest['stage']}"
            result["non_terminal"].append(
                {
                    "run_id": row["run_id"],
                    "workflow_mode": row["workflow_mode"],
                    "status": row["status"],
                    "current_stage": row["current_stage"],
                    "latest_checkpoint_stage": latest["stage"] if latest else None,
                    "latest_checkpoint_version": latest[1] if latest else None,
                    "handling": mapping,
                }
            )
        return result
    finally:
        conn.close()


def inspect_references() -> dict:
    """静态引用清单：旧引擎入口模块在 src/backend 中的 importer。"""
    targets = {
        "drivers_deep_research": "harness/drivers_deep_research.py",
        "deep_research_agent": "agents/deep_research_agent.py",
        "base_planner": "agents/multi_agent/planner/base_planner.py",
        "task_decomposer": "agents/multi_agent/planner/task_decomposer.py",
        "plan_reviewer": "agents/multi_agent/planner/plan_reviewer.py",
        "clarifier": "agents/multi_agent/planner/clarifier.py",
        "orchestrator": "agents/multi_agent/orchestrator.py",
        "multi_agent_factory": "agents/multi_agent/integration/multi_agent_factory.py",
    }
    search_roots = [
        REPO_ROOT / "src" / "deepresearch_agent",
        REPO_ROOT / "backend",
        REPO_ROOT / "scripts",
        REPO_ROOT / "evals",
        REPO_ROOT / "tests",
        REPO_ROOT,  # 根目录散落的 CLI 脚本（如 search_without_stream.py）
    ]
    report: dict = {"note": "direct textual importers only（启发式；registry/字符串动态调用需人工复核）"}
    for name, rel in targets.items():
        module_path = REPO_ROOT / "src" / "deepresearch_agent" / rel
        refs: list[str] = []
        if not module_path.exists():
            report[name] = {"module": rel, "exists": False, "references": []}
            continue
        for root in search_roots:
            if not root.exists():
                continue
            for py in root.rglob("*.py"):
                if "__pycache__" in py.parts or py.resolve() == Path(__file__).resolve():
                    continue
                if any(part in py.parts for part in ("node_modules", ".venv", ".zcode-site", "vendor")):
                    continue
                try:
                    text = py.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                if name in text and ("deepresearch_agent" in text or "backend" in str(py) or "tests" in str(py) or root == REPO_ROOT):
                    for lineno, line in enumerate(text.splitlines(), 1):
                        if f"{name}" in line and (
                            "import" in line or "from " in line or f'"{name}"' in line or f"'{name}'" in line
                        ):
                            refs.append(f"{py.relative_to(REPO_ROOT)}:{lineno}")
        report[name] = {"module": rel, "exists": True, "references": sorted(set(refs))}
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(REPO_ROOT / "data" / "app.db"), help="SQLite 数据库路径（只读打开）")
    parser.add_argument("--output", default=None, help="清单 JSON 输出路径；缺省打印 stdout")
    parser.add_argument("--skip-references", action="store_true", help="跳过静态引用扫描")
    args = parser.parse_args()

    db_path = Path(args.db)
    report: dict = {"mode": "dry-run (read-only)", "database_exists": db_path.exists()}
    if db_path.exists():
        report["database"] = inspect_database(db_path)
    if not args.skip_references:
        report["references"] = inspect_references()
    rendered = json.dumps(report, ensure_ascii=False, indent=2, default=str)
    if args.output:
        Path(args.output).write_text(rendered, encoding="utf-8")
        print(f"inventory written to {args.output}")
    else:
        print(rendered)
    return 0


if __name__ == "__main__":
    sys.exit(main())
