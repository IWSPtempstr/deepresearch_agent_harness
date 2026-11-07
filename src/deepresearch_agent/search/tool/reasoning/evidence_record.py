# 由 search/tool/reasoning/evidence.py 拆分（T-014），方法体与原位置逐行一致。
"""证据入账与引用索引（原 add_evidence 等 4 个方法）。"""

import time
import hashlib


class EvidenceRecordMixin:
    def add_evidence(self, 
                   step_id: str, 
                   source_id: str, 
                   content: str, 
                   source_type: str) -> str:
        """
        添加证据项
        
        参数:
            step_id: 步骤ID
            source_id: 来源ID（如块ID）
            content: 证据内容
            source_type: 来源类型
            
        返回:
            str: 证据ID
        """
        # 生成证据ID
        evidence_id = hashlib.md5(f"{source_id}:{content[:50]}".encode()).hexdigest()[:10]
        
        # 创建证据记录
        evidence = {
            "evidence_id": evidence_id,
            "source_id": source_id,
            "content": content,
            "source_type": source_type,
            "timestamp": time.time()
        }
        
        # 存储证据并关联到步骤
        self.evidence_items[evidence_id] = evidence
        
        # 查找步骤并添加证据ID
        for step in self.reasoning_steps:
            if step["step_id"] == step_id:
                if evidence_id not in step["evidence_ids"]:
                    step["evidence_ids"].append(evidence_id)
                break
        
        return evidence_id

    def add_evidence_with_confidence(
        self, 
        step_id: str, 
        source_id: str, 
        content: str, 
        source_type: str, 
        confidence=0.5,
        metadata=None
    ):
        """
        添加带可信度得分的证据
        
        参数:
            step_id: 步骤ID
            source_id: 来源ID
            content: 证据内容
            source_type: 来源类型
            confidence: 可信度评分(0-1)
            metadata: 元数据字典
            
        返回:
            str: 证据ID
        """
        # 添加基础证据
        evidence_id = self.add_evidence(step_id, source_id, content, source_type)
        
        # 保存可信度评分
        self.confidence_scores[evidence_id] = confidence
        
        # 添加元数据（如果有）
        if metadata:
            if evidence_id in self.evidence_items:
                self.evidence_items[evidence_id]["metadata"] = metadata
        
        # 更新引用索引
        self._update_citation_index(evidence_id, content)
        
        return evidence_id

    def _update_citation_index(self, evidence_id, content):
        """
        更新引用索引，提取关键短语作为索引
        
        参数:
            evidence_id: 证据ID
            content: 证据内容
        """
        # 分析内容，提取关键短语
        key_phrases = self._extract_key_phrases(content)
        
        # 将关键短语添加到引用索引
        for phrase in key_phrases:
            if phrase not in self.citation_index:
                self.citation_index[phrase] = []
            
            if evidence_id not in self.citation_index[phrase]:
                self.citation_index[phrase].append(evidence_id)

    def _extract_key_phrases(self, content):
        """
        从文本中提取关键短语
        
        参数:
            content: 文本内容
            
        返回:
            list: 关键短语列表
        """
        # 使用简单的启发式方法提取关键短语
        # 1. 划分为句子
        import re
        sentences = re.split(r'[.!?。！？]', content)
        
        # 2. 从每个句子中提取名词短语和数值
        key_phrases = []
        
        # 数值模式
        number_pattern = r'\d+(?:[.,]\d+)?(?:\s*%|\s*元|\s*美元|\s*人民币)?'
        
        # 名词短语模式（简化版）
        noun_phrase_pattern = r'[A-Z][a-z]+\s+(?:[a-z]+\s+){0,2}[a-z]+'
        
        for sentence in sentences:
            # 提取数值
            numbers = re.findall(number_pattern, sentence)
            key_phrases.extend(numbers)
            
            # 提取英文名词短语
            noun_phrases = re.findall(noun_phrase_pattern, sentence)
            key_phrases.extend(noun_phrases)
            
            # 提取中文名词短语（简化）
            if len(sentence) > 3:
                # 使用滑动窗口提取短语
                for i in range(len(sentence) - 3):
                    phrase = sentence[i:i+4]
                    if len(phrase.strip()) >= 2:
                        key_phrases.append(phrase.strip())
        
        # 去重并保留最有意义的短语
        return list(set([p for p in key_phrases if len(p) > 1]))
