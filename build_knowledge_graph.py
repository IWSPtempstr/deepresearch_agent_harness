"""知识图谱构建入口：本地语料 GraphRAG（原行为）或 OpenAlex 学术图谱（P2-11）。

用法：
    python build_knowledge_graph.py --source scholar --query "transformer attention" --max-works 200
    python build_knowledge_graph.py                    # 原行为：本地语料 GraphRAG

scholar 图谱节点/边：(:Paper)-[:AUTHORED]-(:Author)-[:AFFILIATED_WITH]-(:Institution)、
(:Paper)-[:PUBLISHED_IN]->(:Venue)、(:Paper)-[:ABOUT]->(:Concept)、(:Paper)-[:CITES]->(:Paper)。
Neo4j 写入为幂等 MERGE，可重复执行增量扩展。
"""

import argparse
import asyncio
import sys
from pathlib import Path

# src 布局：脚本从项目根目录直接运行时，将 src 加入 sys.path 以定位 deepresearch_agent 包
_SRC = Path(__file__).resolve().parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


async def build_scholar_graph(query: str, max_works: int, mailto: str, neo4j_uri: str, neo4j_user: str, neo4j_password: str) -> int:
    """抓取 OpenAlex works 并幂等写入 Neo4j；返回写入论文数。"""
    import httpx
    from neo4j import AsyncGraphDatabase

    from deepresearch_agent.retrieval.scholar_provider import _inverted_abstract_to_text

    params = {"search": query, "per-page": min(max_works, 50),
              "select": "id,doi,display_name,publication_date,abstract_inverted_index,authorships,primary_location,concepts,referenced_works"}
    if mailto:
        params["mailto"] = mailto
    async with httpx.AsyncClient(timeout=60.0) as client:
        response = await client.get("https://api.openalex.org/works", params=params)
        response.raise_for_status()
        works = (response.json() or {}).get("results", [])[:max_works]

    driver = AsyncGraphDatabase.driver(neo4j_uri, auth=(neo4j_user, neo4j_password))
    upsert_work = """
    MERGE (p:Paper {openalex_id: $id})
    SET p.title = $title, p.doi = $doi, p.published = $published, p.abstract = $abstract
    """
    upsert_person = """
    MATCH (p:Paper {openalex_id: $work_id})
    MERGE (a:Author {openalex_id: $author_id})
    SET a.name = $author_name
    MERGE (i:Institution {openalex_id: $inst_id})
    SET i.name = $inst_name
    MERGE (a)-[:AFFILIATED_WITH]->(i)
    MERGE (a)-[:AUTHORED]->(p)
    """
    upsert_venue = """
    MATCH (p:Paper {openalex_id: $work_id})
    MERGE (v:Venue {openalex_id: $venue_id})
    SET v.name = $venue_name
    MERGE (p)-[:PUBLISHED_IN]->(v)
    """
    upsert_concept = """
    MATCH (p:Paper {openalex_id: $work_id})
    MERGE (c:Concept {openalex_id: $concept_id})
    SET c.name = $concept_name
    MERGE (p)-[:ABOUT]->(c)
    """
    upsert_reference = """
    MATCH (p:Paper {openalex_id: $work_id})
    MERGE (r:Paper {openalex_id: $ref_id})
    MERGE (p)-[:CITES]->(r)
    """
    try:
        async with driver.session() as session:
            written = 0
            for work in works:
                work_id = str(work.get("id") or "")
                if not work_id:
                    continue
                await session.run(upsert_work, {
                    "id": work_id,
                    "title": work.get("display_name"),
                    "doi": work.get("doi"),
                    "published": work.get("publication_date"),
                    "abstract": _inverted_abstract_to_text(work.get("abstract_inverted_index"))[:4000],
                })
                for authorship in (work.get("authorships") or []):
                    author = authorship.get("author") or {}
                    institutions = authorship.get("institutions") or []
                    inst = institutions[0] if institutions else {}
                    await session.run(upsert_person, {
                        "work_id": work_id,
                        "author_id": str(author.get("id") or author.get("display_name") or "unknown"),
                        "author_name": author.get("display_name"),
                        "inst_id": str(inst.get("id") or inst.get("display_name") or "unknown"),
                        "inst_name": inst.get("display_name"),
                    })
                venue = ((work.get("primary_location") or {}).get("source") or {})
                if venue.get("id"):
                    await session.run(upsert_venue, {
                        "work_id": work_id, "venue_id": str(venue["id"]), "venue_name": venue.get("display_name"),
                    })
                for concept in (work.get("concepts") or [])[:5]:
                    if concept.get("id"):
                        await session.run(upsert_concept, {
                            "work_id": work_id, "concept_id": str(concept["id"]), "concept_name": concept.get("display_name"),
                        })
                for ref in (work.get("referenced_works") or []):
                    if ref:
                        await session.run(upsert_reference, {"work_id": work_id, "ref_id": str(ref)})
                written += 1
        return written
    finally:
        await driver.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Build knowledge graph (local corpus or OpenAlex scholar graph)")
    parser.add_argument("--source", choices=("local", "scholar"), default="local")
    parser.add_argument("--query", default="", help="scholar 源的检索词")
    parser.add_argument("--max-works", type=int, default=200)
    parser.add_argument("--mailto", default="")
    parser.add_argument("--neo4j-uri", default="")
    parser.add_argument("--neo4j-user", default="")
    parser.add_argument("--neo4j-password", default="")
    args = parser.parse_args()

    if args.source == "scholar":
        if not args.query:
            parser.error("--source scholar 需要 --query")
        from deepresearch_agent.config.settings import NEO4J_PASSWORD, NEO4J_URI, NEO4J_USERNAME

        written = asyncio.run(build_scholar_graph(
            args.query, args.max_works, args.mailto,
            args.neo4j_uri or NEO4J_URI, args.neo4j_user or NEO4J_USERNAME, args.neo4j_password or NEO4J_PASSWORD,
        ))
        print(f"scholar graph written: {written} works")
        return

    from deepresearch_agent.integrations.build.main import KnowledgeGraphProcessor

    processor = KnowledgeGraphProcessor()
    processor.process_all()


if __name__ == "__main__":
    main()
