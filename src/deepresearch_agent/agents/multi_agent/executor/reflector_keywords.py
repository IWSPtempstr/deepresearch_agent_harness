# 由 agents/reflector.py 拆分（T-014），方法体与原位置逐行一致。
"""关键词建议与文本分词归一（原 _derive_keyword_suggestions 等 9 个方法，含 3 个 @staticmethod）。"""

from typing import Any, Dict, Optional, Tuple, List
import re
from collections import Counter
from deepresearch_agent.agents.multi_agent.core.state import PlanExecuteState


class ReflectionKeywordsMixin:
    def _derive_keyword_suggestions(
        self,
        query: str,
        answer: str,
        *,
        reference_keywords: Optional[Dict[str, List[str]]] = None,
    ) -> List[str]:
        keywords: Optional[Dict[str, List[str]]] = reference_keywords
        if keywords is None:
            extractor = getattr(self._validation_tool.validator, "keyword_extractor", None)
            if extractor is None:
                return []
            try:
                keywords = extractor(query) or {}
            except Exception:
                return []

        if not keywords:
            return []

        suggestions: List[str] = []

        high_level = keywords.get("high_level") or []
        missing_high = [
            kw
            for kw in high_level
            if isinstance(kw, str)
            and (kw if self._contains_chinese(kw) else kw.lower())
            not in (answer if self._contains_chinese(kw) else answer.lower())
        ]
        if missing_high:
            suggestions.append(f"补充高层关键词: {', '.join(missing_high[:5])}")

        low_level = keywords.get("low_level") or []
        missing_low = [
            kw
            for kw in low_level
            if isinstance(kw, str)
            and (kw if self._contains_chinese(kw) else kw.lower())
            not in (answer if self._contains_chinese(kw) else answer.lower())
        ]
        if missing_low:
            suggestions.append(f"加强细节描述，覆盖关键词: {', '.join(missing_low[:6])}")

        return suggestions

    def _resolve_evaluation_text(
        self,
        answer: Optional[Any],
        state: PlanExecuteState,
        target_task_id: Optional[str],
    ) -> str:
        if isinstance(answer, str) and answer.strip():
            return answer.strip()

        exec_context = state.execution_context
        if exec_context is not None and target_task_id is not None:
            intermediate = exec_context.intermediate_results.get(target_task_id)
            if isinstance(intermediate, dict):
                candidate = intermediate.get("answer") or intermediate.get("research_result")
                extracted = self._extract_possible_text(candidate)
                if extracted:
                    return extracted
                raw_payload = intermediate.get("raw_result")
                extracted = self._extract_possible_text(raw_payload)
                if extracted:
                    return extracted

        if isinstance(state.response, str) and state.response.strip():
            return state.response.strip()

        evidence_texts = self._collect_evidence_from_records(
            state,
            exclude_tasks=None,
            primary_task_id=target_task_id,
        )
        if evidence_texts:
            combined = "\n".join(evidence_texts[:5]).strip()
            if combined:
                return combined

        return ""

    def _build_reference_keywords(
        self,
        state: PlanExecuteState,
        target_task_id: Optional[str],
        query: str,
        *,
        reflection_task_id: str,
    ) -> Optional[Dict[str, List[str]]]:
        exec_context = state.execution_context
        if exec_context is None:
            return None

        high_counter: Counter[str] = Counter()
        low_counter: Counter[str] = Counter()

        # 1) 目标任务的缓存证据
        if target_task_id is not None:
            cache_entries = exec_context.retrieval_cache.get(target_task_id, [])
            self._accumulate_keywords_from_entries(cache_entries, high_counter, low_counter)
        else:
            cache_entries = []

        # 2) 若仍为空，回退到执行记录中的证据
        if not high_counter and not low_counter:
            evidence_texts = self._collect_evidence_from_records(
                state,
                exclude_tasks={reflection_task_id},
                primary_task_id=target_task_id,
            )
            self._accumulate_keywords_from_strings(evidence_texts, high_counter, low_counter)

        # 3) 若仍没有有效证据，最后使用原始查询兜底
        if not high_counter and not low_counter:
            fallback_texts: List[str] = [query]
            if isinstance(state.response, str) and state.response.strip():
                fallback_texts.append(state.response)
            self._accumulate_keywords_from_strings(fallback_texts, high_counter, low_counter)

        high_keywords = self._counter_to_list(high_counter, limit=8)
        low_keywords = self._counter_to_list(low_counter, limit=10)

        if not high_keywords and not low_keywords:
            return None

        return {
            "high_level": high_keywords,
            "low_level": low_keywords,
        }

    def _tokenize_reference_text(self, text: str) -> List[str]:
        if not text:
            return []
        raw_tokens = re.findall(r"[A-Za-z0-9\u4e00-\u9fff]{2,}", text)
        tokens: List[str] = []
        for token in raw_tokens:
            if self._contains_chinese(token):
                clean_token = token.strip()
                length = len(clean_token)
                if length < 2:
                    continue
                max_window = min(4, length)
                for window in range(2, max_window + 1):
                    for idx in range(0, length - window + 1):
                        substring = clean_token[idx : idx + window]
                        if substring:
                            tokens.append(substring)
            else:
                normalized = self._normalize_keyword_token(token)
                if not normalized:
                    continue
                if len(normalized) > 20:
                    continue
                tokens.append(normalized)
        return tokens

    def _accumulate_keywords_from_entries(
        self,
        entries: List[Any],
        high_counter: Counter[str],
        low_counter: Counter[str],
    ) -> None:
        if not entries:
            return
        for entry in entries:
            text = self._extract_evidence_text(entry)
            if not text:
                continue
            tokens = self._tokenize_reference_text(text)
            for token in tokens:
                if len(token) >= 4:
                    high_counter[token] += 1
                else:
                    low_counter[token] += 1

    def _accumulate_keywords_from_strings(
        self,
        texts: List[str],
        high_counter: Counter[str],
        low_counter: Counter[str],
    ) -> None:
        for text in texts:
            if not isinstance(text, str) or not text.strip():
                continue
            for token in self._tokenize_reference_text(text):
                if len(token) >= 4:
                    high_counter[token] += 1
                else:
                    low_counter[token] += 1

    def _normalize_keyword_token(self, token: str) -> str:
        token = token.strip()
        if not token:
            return ""
        if self._contains_chinese(token):
            return token
        return token.lower()

    @staticmethod
    def _counter_to_list(counter: Counter[str], limit: int) -> List[str]:
        if not counter:
            return []
        ordered = [token for token, _ in counter.most_common(limit * 3)]
        dedup = []
        seen = set()
        for token in ordered:
            if token not in seen:
                seen.add(token)
                dedup.append(token)
            if len(dedup) >= limit:
                break
        return dedup

    @staticmethod
    def _contains_chinese(token: str) -> bool:
        return any("\u4e00" <= ch <= "\u9fff" for ch in token)
