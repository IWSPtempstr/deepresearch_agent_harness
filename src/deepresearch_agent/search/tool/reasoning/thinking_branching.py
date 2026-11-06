# 由 search/tool/reasoning/thinking.py 拆分（T-014），方法体与原位置逐行一致。
"""推理步骤记录与分支推理/反事实分析（原 branch_reasoning 等 6 个方法）。"""

import time
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


class ThinkingBranchingMixin:
    def add_reasoning_step(self, content: str):
        """
        添加推理步骤
        
        参数:
            content: 步骤内容
        """
        self.all_reasoning_steps.append(content)
        
        # 更新推理树
        if self.current_branch not in self.reasoning_tree:
            self.reasoning_tree[self.current_branch] = []
            
        self.reasoning_tree[self.current_branch].append({
            "content": content,
            "timestamp": time.time()
        })

    def branch_reasoning(self, branch_name: str, base_branch: str = "main"):
        """
        创建推理分支
        
        参数:
            branch_name: 分支名称
            base_branch: 基础分支
        """
        # 确保基础分支存在
        if base_branch not in self.reasoning_tree:
            base_branch = "main"
            
        # 创建新分支
        self.reasoning_tree[branch_name] = []
        
        # 复制基础分支内容
        for step in self.reasoning_tree[base_branch]:
            self.reasoning_tree[branch_name].append(step.copy())
            
        # 切换到新分支
        self.current_branch = branch_name
        
        # 添加分支创建记录
        self.add_reasoning_step(f"创建推理分支: {branch_name}，基于: {base_branch}")

    def switch_branch(self, branch_name: str):
        """
        切换推理分支
        
        参数:
            branch_name: 分支名称
        """
        # 确保分支存在
        if branch_name not in self.reasoning_tree:
            return False
            
        # 切换分支
        self.current_branch = branch_name
        return True

    def merge_branches(self, source_branch: str, target_branch: str = "main"):
        """
        合并推理分支
        
        参数:
            source_branch: 源分支
            target_branch: 目标分支
            
        返回:
            bool: 是否成功合并
        """
        # 确保分支存在
        if source_branch not in self.reasoning_tree or target_branch not in self.reasoning_tree:
            return False
            
        # 获取源分支独有的步骤
        source_steps = self.reasoning_tree[source_branch]
        target_steps = self.reasoning_tree[target_branch]
        
        # 找出源分支中独有的步骤
        source_unique_steps = []
        target_step_contents = [step["content"] for step in target_steps]
        
        for step in source_steps:
            if step["content"] not in target_step_contents:
                source_unique_steps.append(step)
        
        # 将源分支独有步骤添加到目标分支
        for step in source_unique_steps:
            self.reasoning_tree[target_branch].append(step.copy())
            
        # 添加合并记录
        merged_step = {
            "content": f"合并分支: {source_branch} → {target_branch}",
            "timestamp": time.time()
        }
        self.reasoning_tree[target_branch].append(merged_step)
        
        # 切换到目标分支
        self.current_branch = target_branch
        
        return True

    def counter_factual_analysis(self, hypothesis: str):
        """
        执行反事实分析
        
        参数:
            hypothesis: 假设内容
            
        返回:
            str: 反事实分析结果
        """
        # 创建反事实分支
        branch_name = f"counter_factual_{int(time.time())}"
        self.branch_reasoning(branch_name)
        
        # 添加反事实假设
        self.add_reasoning_step(f"反事实假设: {hypothesis}")
        
        # 基于反事实假设进行推理
        prompt = COUNTERFACTUAL_ANALYSIS_PROMPT.format(hypothesis=hypothesis)
        
        response = self.llm.invoke(prompt)
        counter_analysis = response.content if hasattr(response, 'content') else str(response)
        
        # 添加分析结果
        self.add_reasoning_step(f"反事实分析结果:\n\n{counter_analysis}")
        
        # 对比原始推理和反事实推理
        prompt = COUNTERFACTUAL_COMPARISON_PROMPT.format(hypothesis=hypothesis)
        
        response = self.llm.invoke(prompt)
        comparison = response.content if hasattr(response, 'content') else str(response)
        
        # 添加比较结果
        self.add_reasoning_step(f"原始推理与反事实推理对比:\n\n{comparison}")
        
        # 回到主分支
        self.switch_branch("main")
        
        # 添加反事实分析的总结
        self.add_reasoning_step(f"反事实分析总结: 如果 {hypothesis}，那么 {self._extract_conclusion(counter_analysis)}")
        
        return comparison

    def _extract_conclusion(self, analysis):
        """
        从分析中提取结论
        
        参数:
            analysis: 分析文本
            
        返回:
            str: 提取的结论
        """
        # 查找结论标记
        conclusion_markers = ["结论", "总结", "因此", "所以", "综上所述"]
        
        for marker in conclusion_markers:
            marker_index = analysis.find(marker)
            if marker_index != -1:
                # 提取标记后的内容作为结论
                conclusion = analysis[marker_index:]
                # 限制长度
                conclusion = conclusion.split("\n")[0]
                if len(conclusion) > 100:
                    conclusion = conclusion[:100] + "..."
                return conclusion
                
        # 如果没有找到标记，返回最后一段
        paragraphs = analysis.split("\n\n")
        if paragraphs:
            last_paragraph = paragraphs[-1]
            if len(last_paragraph) > 100:
                last_paragraph = last_paragraph[:100] + "..."
            return last_paragraph
            
        # 如果分析内容为空，返回默认文本
        return "无法提取明确结论"
