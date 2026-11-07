# 由 search/tool/reasoning/evidence.py 拆分（T-014），方法体与原位置逐行一致。
"""推理链读取与摘要/来源统计（原 get_reasoning_chain 等 4 个方法）。"""

from typing import Dict, List
import time


class EvidenceSummaryMixin:
    def get_reasoning_chain(self, query_id: str) -> Dict:
        """
        获取完整的推理链
        
        参数:
            query_id: 查询ID
            
        返回:
            Dict: 推理链，包含步骤和证据
        """
        if query_id not in self.query_contexts:
            return {}
        
        # 获取查询相关的步骤ID
        step_ids = self.query_contexts[query_id]["step_ids"]
        
        # 按时间顺序收集步骤
        steps = []
        for step_id in step_ids:
            for step in self.reasoning_steps:
                if step["step_id"] == step_id:
                    # 复制步骤并添加完整证据
                    step_copy = step.copy()
                    step_copy["evidence"] = []
                    
                    # 添加证据详情
                    for evidence_id in step["evidence_ids"]:
                        if evidence_id in self.evidence_items:
                            evidence_copy = self.evidence_items[evidence_id].copy()
                            # 添加可信度评分
                            evidence_copy["confidence"] = self.confidence_scores.get(evidence_id, 0.5)
                            step_copy["evidence"].append(evidence_copy)
                    
                    steps.append(step_copy)
                    break
        
        # 按时间戳排序
        steps.sort(key=lambda x: x["timestamp"])
        
        # 构建完整推理链
        reasoning_chain = {
            "query": self.query_contexts[query_id]["query"],
            "keywords": self.query_contexts[query_id]["keywords"],
            "start_time": self.query_contexts[query_id]["start_time"],
            "end_time": time.time(),
            "steps": steps,
            "contradiction_count": len([c for c in self.contradictions.values() 
                                     if any(c.get("evidence1", "") == e_id or c.get("evidence2", "") == e_id 
                                         for e_id in 
                                         [e for s in steps for e in s["evidence_ids"]])
                                    ])
        }
        
        return reasoning_chain

    def get_step_evidence(self, step_id: str) -> List[Dict]:
        """
        获取特定步骤的证据
        
        Args:
            step_id: 步骤ID
            
        Returns:
            List[Dict]: 证据列表
        """
        # 查找步骤
        for step in self.reasoning_steps:
            if step["step_id"] == step_id:
                # 收集证据
                evidence_list = []
                for evidence_id in step["evidence_ids"]:
                    if evidence_id in self.evidence_items:
                        evidence_list.append(
                            self.evidence_items[evidence_id]
                        )
                return evidence_list
        
        return []

    def summarize_reasoning(self, query_id: str) -> Dict:
        """
        总结推理过程
        
        参数:
            query_id: 查询ID
            
        返回:
            Dict: 推理摘要
        """
        chain = self.get_reasoning_chain(query_id)
        if not chain:
            return {"summary": "没有找到相关推理链"}
        
        # 计算统计信息
        steps_count = len(chain.get("steps", []))
        evidence_count = sum(len(step.get("evidence", [])) 
                           for step in chain.get("steps", []))
        
        # 识别关键步骤（有最多证据的步骤）
        key_steps = []
        if steps_count > 0:
            # 按证据数量排序
            sorted_steps = sorted(
                chain.get("steps", []),
                key=lambda x: len(x.get("evidence", [])),
                reverse=True
            )
            
            # 取前3个关键步骤
            key_steps = sorted_steps[:min(3, len(sorted_steps))]
        
        # 计算处理时间
        duration = chain.get("end_time", time.time()) - chain.get("start_time", time.time())
        
        # 生成摘要
        summary = {
            "query": chain.get("query", ""),
            "steps_count": steps_count,
            "evidence_count": evidence_count,
            "duration_seconds": duration,
            "contradiction_count": chain.get("contradiction_count", 0),
            "key_steps": [
                {
                    "step_id": step.get("step_id"),
                    "search_query": step.get("search_query"),
                    "evidence_count": len(step.get("evidence", []))
                }
                for step in key_steps
            ]
        }
        
        return summary

    def get_evidence_source_stats(self, query_id: str) -> Dict:
        """
        获取证据来源统计
        
        参数:
            query_id: 查询ID
            
        返回:
            Dict: 证据来源统计
        """
        chain = self.get_reasoning_chain(query_id)
        if not chain:
            return {"sources": {}}
        
        # 收集所有证据
        all_evidence = []
        for step in chain.get("steps", []):
            all_evidence.extend(step.get("evidence", []))
        
        # 按来源类型分组
        sources = {}
        for evidence in all_evidence:
            source_type = evidence.get("source_type", "unknown")
            if source_type not in sources:
                sources[source_type] = 0
            sources[source_type] += 1
        
        return {"sources": sources}
