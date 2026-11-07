# 由 search/tool/reasoning/evidence.py 拆分（T-014），方法体与原位置逐行一致。
"""数值/语义矛盾检测（原 detect_contradictions 等 4 个方法）。"""



class EvidenceContradictionMixin:
    def detect_contradictions(self, evidence_ids):
        """
        检测证据之间的矛盾
        
        参数:
            evidence_ids: 证据ID列表
            
        返回:
            list: 矛盾信息列表
        """
        if len(evidence_ids) < 2:
            return []
            
        contradictions = []
        evidences = [self.evidence_items[eid] for eid in evidence_ids if eid in self.evidence_items]
        
        # 1. 检测数值类矛盾（通过正则表达式）
        import re
        for i in range(len(evidences)):
            for j in range(i+1, len(evidences)):
                # 提取第一个证据中的数值
                content1 = evidences[i]["content"]
                numbers1 = self._extract_numbers_with_context(content1)
                
                # 提取第二个证据中的数值
                content2 = evidences[j]["content"]
                numbers2 = self._extract_numbers_with_context(content2)
                
                # 比较数值
                for num1_info in numbers1:
                    for num2_info in numbers2:
                        # 检查上下文是否相似
                        if self._context_similarity(num1_info["context"], num2_info["context"]) > 0.7:
                            # 检查数值是否不同
                            if abs(num1_info["value"] - num2_info["value"]) > 0.001 * max(num1_info["value"], num2_info["value"]):
                                contradictions.append({
                                    "type": "numerical",
                                    "evidence1": evidence_ids[i],
                                    "evidence2": evidence_ids[j],
                                    "context": num1_info["context"],
                                    "value1": num1_info["value"],
                                    "value2": num2_info["value"]
                                })
        
        # 2. 使用LLM检测语义矛盾
        if hasattr(self, 'llm') and self.llm:
            for i in range(len(evidences)):
                for j in range(i+1, len(evidences)):
                    # 检查是否已经发现数值矛盾
                    if any(c["evidence1"] == evidence_ids[i] and c["evidence2"] == evidence_ids[j] for c in contradictions):
                        continue
                    
                    # 提取内容
                    content1 = evidences[i]["content"]
                    content2 = evidences[j]["content"]
                    
                    # 使用LLM检测矛盾
                    contradiction = self._detect_semantic_contradiction(content1, content2, evidence_ids[i], evidence_ids[j])
                    if contradiction:
                        contradictions.append(contradiction)
        
        # 保存矛盾信息
        for contradiction in contradictions:
            contradiction_id = f"contradiction_{len(self.contradictions)}"
            self.contradictions[contradiction_id] = contradiction
            
        return contradictions

    def _extract_numbers_with_context(self, text):
        """
        提取文本中的数值及其上下文
        
        参数:
            text: 文本内容
            
        返回:
            list: 包含数值和上下文的对象列表
        """
        import re
        
        # 数值模式
        number_pattern = r'(\d+(?:[.,]\d+)?(?:\s*%|\s*元|\s*美元|\s*人民币)?)'
        
        # 查找所有匹配
        matches = list(re.finditer(number_pattern, text))
        results = []
        
        for match in matches:
            # 获取数值
            value_str = match.group(1)
            
            # 清理并转换为浮点数
            clean_value = re.sub(r'[^\d.,]', '', value_str).replace(',', '.')
            try:
                value = float(clean_value)
            except:
                continue
            
            # 获取上下文（数值前后20个字符）
            start = max(0, match.start() - 20)
            end = min(len(text), match.end() + 20)
            context = text[start:end]
            
            results.append({
                "value": value,
                "original": value_str,
                "context": context
            })
            
        return results

    def _context_similarity(self, context1, context2):
        """
        计算两个上下文的相似度
        
        参数:
            context1: 第一个上下文
            context2: 第二个上下文
            
        返回:
            float: 相似度得分(0-1)
        """
        # 实现简单的基于单词重叠的相似度计算
        words1 = set(context1.lower().split())
        words2 = set(context2.lower().split())
        
        if not words1 or not words2:
            return 0
            
        # 计算Jaccard相似度
        intersection = len(words1.intersection(words2))
        union = len(words1.union(words2))
        
        return intersection / union if union > 0 else 0

    def _detect_semantic_contradiction(self, content1, content2, evidence_id1, evidence_id2):
        """
        使用LLM检测语义矛盾
        
        参数:
            content1: 第一个内容
            content2: 第二个内容
            evidence_id1: 第一个证据ID
            evidence_id2: 第二个证据ID
            
        返回:
            Dict或None: 矛盾信息或None（如果没有矛盾）
        """
        prompt = f"""
        分析以下两段内容，判断它们之间是否存在矛盾或不一致：
        
        内容1:
        {content1}
        
        内容2:
        {content2}
        
        如果存在矛盾，请具体说明矛盾点。如果不存在矛盾，请回答"没有矛盾"。
        """
        
        # 调用LLM
        response = self.llm.invoke(prompt)
        analysis = response.content if hasattr(response, 'content') else str(response)
        
        # 判断是否发现矛盾
        if "没有矛盾" in analysis:
            return None
        
        # 提取矛盾点
        contradiction_point = analysis.replace("矛盾点：", "").strip()
        if len(contradiction_point) > 300:
            contradiction_point = contradiction_point[:300] + "..."
            
        return {
            "type": "semantic",
            "evidence1": evidence_id1,
            "evidence2": evidence_id2,
            "analysis": contradiction_point
        }
