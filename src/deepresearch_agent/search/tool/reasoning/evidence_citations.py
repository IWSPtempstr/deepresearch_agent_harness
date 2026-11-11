# 由 search/tool/reasoning/evidence.py 拆分（T-014），方法体与原位置逐行一致。
"""引用生成与答案标注（原 generate_citations 等 4 个方法）。"""



class EvidenceCitationsMixin:
    def generate_citations(self, answer):
        """
        在答案中生成引用标记
        
        参数:
            answer: 答案文本
            
        返回:
            Dict: 包含带引用的答案和引用信息
        """
        citations = []
        
        # 从答案中提取关键语句
        key_statements = self._extract_key_statements(answer)
        
        # 为每个语句查找最匹配的证据
        for statement in key_statements:
            matching_evidence = self._find_matching_evidence(statement)
            if matching_evidence:
                citation = {
                    "statement": statement,
                    "evidence_id": matching_evidence["evidence_id"],
                    "source_id": matching_evidence["source_id"],
                    "confidence": self.confidence_scores.get(matching_evidence["evidence_id"], 0.5)
                }
                citations.append(citation)
        
        # 生成带引用的答案
        cited_answer = self._add_citations_to_answer(answer, citations)
        
        return {
            "cited_answer": cited_answer,
            "citations": citations
        }

    def _extract_key_statements(self, text):
        """
        从文本中提取关键语句
        
        参数:
            text: 文本内容
            
        返回:
            list: 关键语句列表
        """
        import re
        
        # 按句子划分
        sentences = re.split(r'([.!?。！？]\s*)', text)
        
        # 合并分隔符和句子
        merged_sentences = []
        i = 0
        while i < len(sentences):
            if i + 1 < len(sentences):
                merged_sentences.append(sentences[i] + sentences[i+1])
                i += 2
            else:
                merged_sentences.append(sentences[i])
                i += 1
        
        # 筛选出有意义的句子（长度大于10个字符）
        key_statements = [s.strip() for s in merged_sentences if len(s.strip()) > 10]
        
        return key_statements

    def _find_matching_evidence(self, statement):
        """
        为语句查找最匹配的证据
        
        参数:
            statement: 语句
            
        返回:
            Dict或None: 匹配的证据或None
        """
        # 提取语句中的关键短语
        key_phrases = self._extract_key_phrases(statement)
        
        # 收集可能匹配的证据
        candidate_evidence_ids = []
        for phrase in key_phrases:
            if phrase in self.citation_index:
                candidate_evidence_ids.extend(self.citation_index[phrase])
        
        # 如果没有候选证据，返回None
        if not candidate_evidence_ids:
            return None
            
        # 计算每个证据的匹配得分
        evidence_scores = {}
        for evidence_id in set(candidate_evidence_ids):
            if evidence_id in self.evidence_items:
                evidence = self.evidence_items[evidence_id]
                # 基础得分 - 出现频率
                base_score = candidate_evidence_ids.count(evidence_id)
                # 可信度加权
                confidence = self.confidence_scores.get(evidence_id, 0.5)
                # 最终得分
                evidence_scores[evidence_id] = base_score * confidence
        
        # 找出得分最高的证据
        if evidence_scores:
            best_evidence_id = max(evidence_scores, key=evidence_scores.get)
            return self.evidence_items[best_evidence_id]
        
        return None

    def _add_citations_to_answer(self, answer, citations):
        """
        在答案中添加引用标记
        
        参数:
            answer: 原始答案
            citations: 引用信息列表
            
        返回:
            str: 带引用的答案
        """
        # 为每个引用添加标记
        cited_answer = answer
        
        # 按语句长度从长到短排序，避免替换冲突
        sorted_citations = sorted(citations, key=lambda x: len(x["statement"]), reverse=True)
        
        for i, citation in enumerate(sorted_citations):
            statement = citation["statement"]
            source_id = citation["source_id"]
            citation_mark = f"[{i+1}]"
            
            # 替换语句添加引用标记
            if statement in cited_answer:
                cited_answer = cited_answer.replace(statement, f"{statement}{citation_mark}")
        
        # 添加引用列表
        if citations:
            cited_answer += "\n\n#### 引用\n"
            for i, citation in enumerate(citations):
                cited_answer += f"[{i+1}] {citation['source_id']}\n"
            
        return cited_answer
