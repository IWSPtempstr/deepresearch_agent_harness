"""创建租户与 API key（P2-8 运维入口）。

用法：
    python scripts/create_tenant.py "租户名" [--budget '{"max_llm_tokens": 100000}']
    python scripts/create_tenant.py --key-for <tenant_id> [--label CI]

raw key 只打印一次，请立即保存；库中仅存 sha256 hash。
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.app.tenancy import TenantRegistry
from deepresearch_agent.persistence import Database
from deepresearch_agent.config.settings import APP_DATABASE_URL


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("name", nargs="?", help="新租户名称")
    parser.add_argument("--budget", default="{}", help="租户预算覆盖（JSON，HARNESS_BUDGETS 的键子集）")
    parser.add_argument("--key-for", dest="key_for", help="为既有租户签发 API key")
    parser.add_argument("--label", default="", help="key 标签")
    parser.add_argument("--database-url", default=APP_DATABASE_URL)
    args = parser.parse_args()

    database = Database(args.database_url)
    registry = TenantRegistry(database)
    try:
        if args.key_for:
            raw = await registry.create_api_key(args.key_for, label=args.label)
            print(f"API key（仅此一次显示）: {raw}")
            return
        if not args.name:
            parser.error("需要租户名称或 --key-for")
        budget = json.loads(args.budget) if args.budget else {}
        tenant_id = await registry.create_tenant(args.name, budget=budget)
        raw = await registry.create_api_key(tenant_id, label=args.name)
        print(f"tenant_id: {tenant_id}")
        print(f"API key（仅此一次显示）: {raw}")
    finally:
        await database.close()


if __name__ == "__main__":
    asyncio.run(main())
