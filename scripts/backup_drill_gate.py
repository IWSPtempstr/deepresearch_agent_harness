"""T-004：备份恢复演练报告阈值门禁（CI 步骤）。

校验演练 JSON：passed=true 且 RTO < 目标（60 分钟）。低于阈值以非零退出。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: python scripts/backup_drill_gate.py <report.json>", file=sys.stderr)
        return 2
    report = json.loads(Path(argv[1]).read_text(encoding="utf-8"))
    if report.get("passed") is not True:
        print(f"演练失败: {json.dumps(report, ensure_ascii=False)}", file=sys.stderr)
        return 1
    rto = report.get("rto_seconds")
    target = report.get("rto_target_seconds", 3600)
    if not isinstance(rto, (int, float)) or rto >= target:
        print(f"RTO 超标: {rto}s >= {target}s", file=sys.stderr)
        return 1
    print(f"drill PASS, rto = {rto}s (target {target}s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
