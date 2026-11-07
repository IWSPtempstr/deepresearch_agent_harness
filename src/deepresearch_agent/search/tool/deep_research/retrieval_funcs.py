# 由 search/tool/deep_research_tool.py 拆分（T-014），方法体与原位置逐行一致。
"""检索结果解析与 KB/KG 检索函数构建（原 _parse_search_result 等 6 个检索辅助）。"""

from typing import Dict, List, Any, Optional, AsyncGenerator
import re
import logging
import json
import traceback
from deepresearch_agent.retrieval.base import RetrievalProvider, SearchFilters, ToolCallContext, run_async_from_sync


class DeepRetrievalMixin:
    def _parse_search_result(self, result):
        """
        解析搜索结果，支持多种格式
        
        参数:
            result: 搜索返回的原始结果
            
        返回:
            Dict: 解析后的结构化数据
        """
        # 已经是字典，直接返回
        if isinstance(result, dict):
            return result
        
        # 字符串结果需要解析
        if isinstance(result, str):
            # 尝试JSON解析
            try:
                return json.loads(result)
            except json.JSONDecodeError:
                pass
            
            # 使用正则表达式提取JSON对象
            json_patterns = [
                r'{\s*"data"\s*:\s*(\{.*\})\s*}',  # {"data": {...}}
                r'(\{.*\})',                       # {...}
            ]
            
            for pattern in json_patterns:
                matches = re.search(pattern, result, re.DOTALL)
                if matches:
                    try:
                        import ast
                        extracted = matches.group(1)
                        parsed = ast.literal_eval(extracted)
                        return {"data": parsed}
                    except (SyntaxError, ValueError):
                        continue
            
            # 尝试提取Chunk IDs
            chunks_pattern = r'Chunks\s*:\s*\[(.*?)\]'
            chunks_match = re.search(chunks_pattern, result, re.DOTALL)
            if chunks_match:
                try:
                    chunk_text = chunks_match.group(1)
                    # 清理并分割
                    chunks = [c.strip("' \t\n\"") for c in chunk_text.split(",")]
                    chunks = [c for c in chunks if c]  # 移除空字符串
                    return {"data": {"Chunks": chunks}}
                except Exception:
                    pass
        
        # 无法解析，将整个内容作为文本
        return {"data": {"text": str(result)}}

    def _get_chunk_content(self, chunk_id: str) -> Optional[str]:
        """
        根据chunk_id获取真实内容
        
        参数:
            chunk_id: 文本块ID
            
        返回:
            str: 文本块内容，如果找不到则返回None
        """
        try:
            # 使用Neo4j查询获取chunk内容
            query = """
            MATCH (c:__Chunk__ {id: $chunk_id})
            RETURN c.text AS text
            """
            
            result = self.db_query(query, {"chunk_id": chunk_id})
            
            if not result.empty and 'text' in result.columns:
                return result.iloc[0]['text']
            return None
        except Exception as e:
            print(f"[获取Chunk内容] 错误: {str(e)}")
            return None

    def _create_kb_retrieval_func(self):
        """
        创建知识库检索函数
        
        返回:
            function: 知识库检索函数
        """
        def kb_retrieve(question: str, limit: int = 5):
            """基于问题检索知识库内容"""
            try:
                # 记录开始检索
                self._log(f"\n[KB检索] 开始搜索: {question}")

                # 使用本地搜索工具
                result = self.local_tool.search(question)
                self._log(f"\n[KB检索] 原始结果: {result}" if isinstance(result, str) else f"\n[KB检索] 原始结果类型: {type(result)}")
                
                # 检查结果是否为空
                if not result:
                    print("\n[KB检索] 搜索结果为空")
                    return {
                        "chunks": [],
                        "doc_aggs": [],
                        "entities": [],
                        "relationships": [],
                        "Chunks": []
                    }
                    
                # 解析结果
                try:
                    data_dict = self._parse_search_result(result)
                    self._log(f"\n[KB检索] 解析结果: {data_dict.keys()}")
                except Exception as parse_e:
                    print(f"\n[KB检索] 解析结果失败: {parse_e}")
                    # 如果解析失败但结果是字符串，创建一个简单的chunk
                    if isinstance(result, str) and len(result) > 10:
                        return {
                            "chunks": [{
                                "chunk_id": "text_content",
                                "text": result,
                                "content_with_weight": result,
                                "weight": 1.0
                            }],
                            "doc_aggs": [],
                            "entities": [],
                            "relationships": [],
                            "Chunks": ["text_content"]
                        }
                    return {
                        "chunks": [],
                        "doc_aggs": [],
                        "entities": [],
                        "relationships": [],
                        "Chunks": []
                    }
                
                # 标准化数据结构
                if "data" in data_dict:
                    data = data_dict["data"]
                else:
                    data = data_dict
                
                # 提取各类信息
                entities = data.get("Entities", [])
                reports = data.get("Reports", [])
                relationships = data.get("Relationships", [])
                chunk_ids = data.get("Chunks", [])
                
                # 如果data中已经有完整的chunks列表，直接使用
                if "chunks" in data and isinstance(data["chunks"], list) and data["chunks"]:
                    return data
                
                # 否则构建 chunks 列表
                chunks = []
                doc_aggs = []
                
                # 检查是否有真实的chunk_ids
                if chunk_ids:
                    for chunk_id in chunk_ids[:limit]:
                        # 尝试获取真实内容
                        chunk_content = self._get_chunk_content(chunk_id)
                        text = chunk_content or f"Chunk内容: {chunk_id}"
                        
                        chunks.append({
                            "chunk_id": chunk_id,
                            "text": text,
                            "content_with_weight": text,
                            "weight": 1.0,
                            "docnm_kwd": f"Document_{chunk_id}"
                        })
                        
                        # 构造文档聚合
                        doc_id = chunk_id.split("_")[0] if "_" in chunk_id else chunk_id
                        if not any(d.get("doc_id") == doc_id for d in doc_aggs):
                            doc_aggs.append({
                                "doc_id": doc_id,
                                "title": f"Document: {doc_id}"
                            })
                
                # 如果原始结果是字符串且没有找到chunks，将整个文本作为一个chunk
                elif isinstance(result, str) and len(result) > 10 and not chunks:
                    chunks.append({
                        "chunk_id": "text_result",
                        "text": result,
                        "content_with_weight": result,
                        "weight": 1.0,
                        "docnm_kwd": "Document_text"
                    })
                    doc_aggs.append({
                        "doc_id": "text",
                        "title": "Document: text"
                    })
                    chunk_ids = ["text_result"]
                
                # 记录结果统计
                self._log(f"\n[KB检索] 结果: {len(chunks)}个chunks, {len(entities)}个实体, {len(relationships)}个关系")
                
                return {
                    "chunks": chunks,
                    "doc_aggs": doc_aggs,
                    "entities": entities,
                    "reports": reports,
                    "relationships": relationships,
                    "Chunks": [c.get("chunk_id") for c in chunks]
                }
            except Exception as e:
                print(f"\n[KB检索错误] {str(e)}")
                print(traceback.format_exc())
                return {
                    "chunks": [],
                    "doc_aggs": [],
                    "entities": [],
                    "relationships": [],
                    "Chunks": []
                }
        
        return kb_retrieve

    def _create_kg_retrieval_func(self):
        """
        创建知识图谱检索函数
        
        返回:
            function: 知识图谱检索函数
        """
        def kg_retrieve(question: str):
            """基于问题检索知识图谱内容"""
            try:
                # 使用全局搜索工具获取社区信息
                results = self.global_tool.search(question)
                
                # 格式化结果为内容列表
                formatted_results = []
                
                if results and isinstance(results, list):
                    community_content = "## 相关知识社区\n"
                    
                    for i, result in enumerate(results):
                        community_id = f"community_{i}"
                        community_content += f"### 社区 {community_id}\n"
                        community_content += f"内容: {result}\n\n"
                    
                    # 添加社区结果
                    formatted_results.append({
                        "chunk_id": "kg_community_result",
                        "content_with_weight": community_content,
                        "text": community_content,
                        "weight": 0.9,
                        "docnm_kwd": "知识图谱社区"
                    })
                
                return {"content_with_weight": formatted_results}
                
            except Exception as e:
                logging.error(f"知识图谱检索失败: {e}")
                return {"content_with_weight": []}
        
        return kg_retrieve

    def _search_current_provider(self, query: str):
        if self.retrieval_provider is None:
            return self.dual_searcher.search(query)
        return run_async_from_sync(lambda: self._async_search(query))

    @staticmethod
    def _provider_results_to_legacy(results):
        chunks = []
        doc_aggs = []
        seen_docs = set()
        for result in results:
            metadata = result.metadata
            source_id = metadata.source_id
            chunks.append({
                "chunk_id": result.result_id,
                "doc_id": source_id,
                "text": str(result.evidence),
                "content_with_weight": str(result.evidence),
                "url": metadata.url,
                "title": metadata.title,
                "score": result.score,
                "source_mode": result.source_mode,
            })
            if source_id not in seen_docs:
                doc_aggs.append({"doc_id": source_id, "title": metadata.title or source_id, "type": metadata.source_type})
                seen_docs.add(source_id)
        return {"chunks": chunks, "doc_aggs": doc_aggs, "entities": [], "relationships": []}
