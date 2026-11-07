# 由 search/tool/reasoning/thinking.py 拆分（T-014），方法体与原位置逐行一致。
"""初始思考、假设生成与验证（原 generate_initial_thinking 等 8 个方法）。"""

import re
import json
from typing import List, Dict, Any
from deepresearch_agent.config.prompts import (
    BEGIN_SEARCH_QUERY,
    BEGIN_SEARCH_RESULT,
    END_SEARCH_RESULT,
    REASON_PROMPT,
    END_SEARCH_QUERY,
    INITIAL_THINKING_PROMPT,
    HYPOTHESIS_GENERATION_PROMPT,
    HYPOTHESIS_VERIFICATION_PROMPT,
    VERIFICATION_STATUS_PROMPT,
    UPDATE_THINKING_PROMPT,
    COUNTERFACTUAL_ANALYSIS_PROMPT,
    COUNTERFACTUAL_COMPARISON_PROMPT,
)


class ThinkingHypothesesMixin:
    def generate_initial_thinking(self):
        """生成初步思考过程"""
        prompt = INITIAL_THINKING_PROMPT
        response = self.llm.invoke([
            {"role": "system", "content": prompt},
            {"role": "user", "content": self.msg_history[0]["content"]}
        ])
        
        content = response.content if hasattr(response, 'content') else str(response)
        self.add_reasoning_step(content)
        
        return content

    def generate_hypotheses(self, initial_thinking):
        """
        生成多个可能的假设
        
        参数:
            initial_thinking: 初步思考内容
            
        返回:
            List[Dict]: 假设列表
        """
        prompt = HYPOTHESIS_GENERATION_PROMPT.format(initial_thinking=initial_thinking)
        
        response = self.llm.invoke(prompt)
        content = response.content if hasattr(response, 'content') else str(response)
        
        # 解析假设
        try:            
            # 寻找JSON部分
            json_match = re.search(r'\[.*\]', content, re.DOTALL)
            if json_match:
                hypotheses = json.loads(json_match.group(0))
                self.hypotheses = hypotheses
                
                # 添加假设到推理步骤
                hypothesis_step = "生成的假设：\n"
                for i, hyp in enumerate(hypotheses):
                    hypothesis_step += f"假设 {i+1}: {hyp['hypothesis']}\n"
                    hypothesis_step += f"理由: {hyp['reasoning']}\n\n"
                
                self.add_reasoning_step(hypothesis_step)
                return hypotheses
            else:
                # 使用正则表达式提取假设
                return self._extract_hypotheses_fallback(content)
        except Exception as e:
            print(f"解析假设失败: {e}")
            return self._extract_hypotheses_fallback(content)

    def _extract_hypotheses_fallback(self, content):
        """
        当JSON解析失败时，使用正则表达式提取假设
        
        参数:
            content: 包含假设的文本
            
        返回:
            List[Dict]: 假设列表
        """        
        hypotheses = []
        
        # 查找假设模式
        hypothesis_pattern = re.compile(r'假设\s*\d+[:：]?\s*(.*?)(?=假设\s*\d+[:：]?|$)', re.DOTALL)
        matches = hypothesis_pattern.findall(content)
        
        for i, match in enumerate(matches):
            # 尝试分离假设和理由
            parts = re.split(r'理由[:：]', match, 1)
            
            if len(parts) == 2:
                hypothesis, reasoning = parts
            else:
                hypothesis = parts[0]
                reasoning = ""
                
            hypotheses.append({
                "hypothesis": hypothesis.strip(),
                "reasoning": reasoning.strip()
            })
        
        # 如果没有找到假设，创建一个默认假设
        if not hypotheses:
            hypotheses = [{
                "hypothesis": "问题可能需要更多背景信息",
                "reasoning": "初步思考中没有明确的答案方向"
            }]
            
        # 添加假设到思考引擎状态
        self.hypotheses = hypotheses
        
        # 添加假设到推理步骤
        hypothesis_step = "生成的假设：\n"
        for i, hyp in enumerate(hypotheses):
            hypothesis_step += f"假设 {i+1}: {hyp['hypothesis']}\n"
            hypothesis_step += f"理由: {hyp['reasoning']}\n\n"
        
        self.add_reasoning_step(hypothesis_step)
        
        return hypotheses

    def verify_hypothesis(self, hypothesis):
        """
        验证假设
        
        参数:
            hypothesis: 要验证的假设
            
        返回:
            Dict: 验证结果
        """
        prompt = HYPOTHESIS_VERIFICATION_PROMPT.format(
            hypothesis_text=hypothesis["hypothesis"],
            reasoning_text=hypothesis["reasoning"],
        )
        
        response = self.llm.invoke(prompt)
        verification = response.content if hasattr(response, 'content') else str(response)
        
        # 创建验证结果
        verification_result = {
            "hypothesis": hypothesis['hypothesis'],
            "verification": verification,
            "status": self._assess_verification_status(verification)
        }
        
        # 添加到验证链
        self.verification_chain.append(verification_result)
        
        # 添加到推理步骤
        self.add_reasoning_step(f"验证假设: {hypothesis['hypothesis']}\n\n{verification}")
        
        return verification_result

    def _assess_verification_status(self, verification):
        """
        评估验证状态
        
        参数:
            verification: 验证文本
            
        返回:
            str: 验证状态 (supported/rejected/uncertain)
        """
        # 分析验证文本，确定假设状态
        prompt = VERIFICATION_STATUS_PROMPT.format(verification=verification)
        
        try:
            response = self.llm.invoke(prompt)
            status = response.content if hasattr(response, 'content') else str(response)
            
            # 清理并标准化状态
            status = status.strip().lower()
            
            if "support" in status:
                return "supported"
            elif "reject" in status:
                return "rejected"
            else:
                return "uncertain"
        except:
            # 默认不确定
            return "uncertain"

    def think_deeper(self, query, context=None):
        """
        启动深度思考模式
        
        参数:
            query: 用户问题
            context: 上下文信息
            
        返回:
            str: 深度思考结果
        """
        # 初始化思考历史
        self.initialize_with_query(query)
        
        # 添加上下文信息（如果有）
        if context:
            self.add_reasoning_step(f"考虑以下背景信息：\n{context}")
        
        # 生成初步思考
        initial_thinking = self.generate_initial_thinking()
        
        # 提出假设
        hypotheses = self.generate_hypotheses(initial_thinking)
        
        # 对每个假设进行验证
        verifications = []
        for hypothesis in hypotheses:
            verification = self.verify_hypothesis(hypothesis)
            verifications.append(verification)
            
        # 基于验证结果更新思考
        updated_thinking = self.update_thinking_based_on_verification(verifications)
        
        # 整合所有思考过程
        final_thinking = self.integrate_thinking_process(
            initial_thinking,
            hypotheses,
            verifications,
            updated_thinking
        )
        
        return final_thinking

    def update_thinking_based_on_verification(self, verifications):
        """
        基于验证结果更新思考
        
        参数:
            verifications: 验证结果列表
            
        返回:
            str: 更新后的思考
        """
        # 汇总验证结果
        verification_summary = "验证结果汇总:\n"
        
        supported = []
        rejected = []
        uncertain = []
        
        for v in verifications:
            if v["status"] == "supported":
                supported.append(v["hypothesis"])
            elif v["status"] == "rejected":
                rejected.append(v["hypothesis"])
            else:
                uncertain.append(v["hypothesis"])
                
        verification_summary += f"- 被支持的假设: {len(supported)}\n"
        if supported:
            verification_summary += "  " + "\n  ".join(supported) + "\n"
            
        verification_summary += f"- 被拒绝的假设: {len(rejected)}\n"
        if rejected:
            verification_summary += "  " + "\n  ".join(rejected) + "\n"
            
        verification_summary += f"- 不确定的假设: {len(uncertain)}\n"
        if uncertain:
            verification_summary += "  " + "\n  ".join(uncertain) + "\n"
        
        # 添加汇总到推理步骤
        self.add_reasoning_step(verification_summary)
        
        # 基于验证结果更新思考
        prompt = UPDATE_THINKING_PROMPT.format(
            verification_summary=verification_summary
        )
        
        response = self.llm.invoke(prompt)
        updated_thinking = response.content if hasattr(response, 'content') else str(response)
        
        # 添加到推理步骤
        self.add_reasoning_step(f"更新后的思考:\n\n{updated_thinking}")
        
        return updated_thinking

    def integrate_thinking_process(self, initial_thinking, hypotheses, verifications, updated_thinking):
        """
        整合所有思考过程
        
        参数:
            initial_thinking: 初步思考
            hypotheses: 假设列表
            verifications: 验证结果
            updated_thinking: 更新后的思考
            
        返回:
            str: 整合后的思考过程
        """
        # 构建完整的思考过程
        integrated_thinking = "# 思考过程\n\n"
        integrated_thinking += "## 初步分析\n\n"
        integrated_thinking += initial_thinking + "\n\n"
        
        integrated_thinking += "## 假设生成\n\n"
        for i, hyp in enumerate(hypotheses):
            integrated_thinking += f"### 假设 {i+1}: {hyp['hypothesis']}\n"
            integrated_thinking += f"{hyp['reasoning']}\n\n"
        
        integrated_thinking += "## 假设验证\n\n"
        for i, ver in enumerate(verifications):
            status_map = {
                "supported": "✅ 支持",
                "rejected": "❌ 拒绝", 
                "uncertain": "❓ 不确定"
            }
            status = status_map.get(ver["status"], "未知")
            
            integrated_thinking += f"### 验证 {i+1}: {ver['hypothesis']} [{status}]\n"
            integrated_thinking += f"{ver['verification']}\n\n"
        
        integrated_thinking += "## 最终思考\n\n"
        integrated_thinking += updated_thinking
        
        return integrated_thinking
