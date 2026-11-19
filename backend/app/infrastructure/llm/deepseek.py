from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Any

import httpx

from app.core.config import Settings
from app.domain.pricing import estimate_llm_cost

logger = logging.getLogger(__name__)


class LLMClientError(Exception):
    def __init__(self, error_type: str, message: str) -> None:
        super().__init__(message)
        self.error_type = error_type


@dataclass(frozen=True)
class LLMResponse:
    data: dict[str, Any]
    model: str
    input_tokens: int
    output_tokens: int
    estimated_cost: float
    latency_ms: int


class DeepSeekClient:
    """
    OpenAI-compatible DeepSeek API client (Section 8).
    - Supports configurable model routing (deepseek-flash / deepseek-v4-pro).
    - Guards against blank JSON responses even on HTTP 200.
    - Retries transient errors (429, 5xx, timeout, blank/invalid JSON) with exponential backoff.
    - Supports deterministic mock mode for local dev and automated testing.
    """

    def __init__(self, settings: Settings, http_client: httpx.Client | None = None) -> None:
        self.settings = settings
        self._http = http_client

    def select_model(self, tier: str) -> str:
        if tier == "analysis":
            return self.settings.deepseek_analysis_model
        return self.settings.deepseek_extract_model

    def chat_json(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        tier: str = "extract",
        temperature: float = 0.1,
        max_tokens: int = 4096,
        task_hint: str = "generic",
    ) -> LLMResponse:
        model = self.select_model(tier)
        if self.settings.deepseek_mock_mode or not self.settings.deepseek_api_key:
            return self._mock_response(model=model, user_prompt=user_prompt, task_hint=task_hint)

        url = f"{self.settings.deepseek_base_url.rstrip('/')}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.settings.deepseek_api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "response_format": {"type": "json_object"},
            "temperature": temperature,
            "max_tokens": max_tokens,
        }

        last_err: LLMClientError | None = None
        max_attempts = self.settings.llm_max_retries + 1

        for attempt in range(1, max_attempts + 1):
            t0 = time.monotonic()
            try:
                resp = self._post(url, headers=headers, json_body=payload)
                latency_ms = int((time.monotonic() - t0) * 1000)

                if resp.status_code == 429:
                    raise LLMClientError("RATE_LIMIT_429", "DeepSeek API rate limit exceeded")
                if resp.status_code >= 500:
                    raise LLMClientError(
                        f"HTTP_{resp.status_code}", f"DeepSeek server error: {resp.status_code}"
                    )
                if resp.status_code >= 400:
                    raise LLMClientError(
                        f"HTTP_{resp.status_code}",
                        f"DeepSeek client error {resp.status_code}: {resp.text[:300]}",
                    )

                body = resp.json()
                choices = body.get("choices") or []
                content = (choices[0].get("message", {}).get("content") if choices else "") or ""
                content = content.strip()
                if not content:
                    raise LLMClientError("EMPTY_JSON_CONTENT", "DeepSeek returned blank JSON content")

                parsed = json.loads(content)
                if not isinstance(parsed, dict):
                    raise LLMClientError("INVALID_JSON_OBJECT", "Expected top-level JSON object")

                usage = body.get("usage") or {}
                in_tok = int(usage.get("prompt_tokens", 0))
                out_tok = int(usage.get("completion_tokens", 0))
                cost = estimate_llm_cost(model, in_tok, out_tok, self.settings)
                return LLMResponse(
                    data=parsed,
                    model=model,
                    input_tokens=in_tok,
                    output_tokens=out_tok,
                    estimated_cost=cost,
                    latency_ms=latency_ms,
                )
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_err = LLMClientError("NETWORK_ERROR", str(exc))
            except json.JSONDecodeError as exc:
                last_err = LLMClientError("JSON_DECODE_ERROR", str(exc))
            except LLMClientError as exc:
                last_err = exc
                if exc.error_type.startswith("HTTP_4") and exc.error_type != "RATE_LIMIT_429":
                    break

            if attempt < max_attempts:
                backoff = min(2 ** (attempt - 1), 8)
                logger.warning(
                    "DeepSeek call failed (attempt %d/%d, %s); retrying in %ss",
                    attempt,
                    max_attempts,
                    last_err.error_type if last_err else "unknown",
                    backoff,
                )
                time.sleep(backoff)

        assert last_err is not None
        raise last_err

    def _post(self, url: str, headers: dict[str, str], json_body: dict[str, Any]) -> httpx.Response:
        if self._http is not None:
            return self._http.post(url, headers=headers, json=json_body)
        with httpx.Client(timeout=self.settings.llm_timeout_seconds) as client:
            return client.post(url, headers=headers, json=json_body)

    def _mock_response(self, model: str, user_prompt: str, task_hint: str) -> LLMResponse:
        """
        Deterministic heuristic mock so the full pipeline can run end-to-end offline
        without real API credentials.
        """
        t0 = time.monotonic()
        data: dict[str, Any]
        if task_hint == "extract_mentions":
            # Extract simple Chinese 2-3 char names preceded/followed by common dialogue/action cues
            names = sorted(
                set(
                    re.findall(
                        r"(韩立|南宫婉|蟹道人|魔光|石穿空|柳乐儿|高升|甘九真|轮回殿主|曲鳞|白素媛)",
                        user_prompt,
                    )
                )
            )
            mentions = []
            for name in names:
                idx = user_prompt.find(name)
                if idx != -1:
                    mentions.append(
                        {
                            "surface_text": name,
                            "entity_type": "person",
                            "quote_text": name,
                            "confidence": 0.92,
                        }
                    )
            data = {"mentions": mentions}
        elif task_hint == "extract_facts":
            data = {"facts": [], "events": [], "relationships": []}
            # Scan only the actual excerpt after "原文片段：" so appearance order matches text
            excerpt = user_prompt.split("原文片段：", 1)[-1]
            names = list(
                dict.fromkeys(
                    re.findall(
                        r"(韩立|南宫婉|蟹道人|魔光|石穿空|柳乐儿|高升|甘九真|轮回殿主|曲鳞|白素媛)",
                        excerpt,
                    )
                )
            )
            if len(names) >= 2:
                data["relationships"].append(
                    {
                        "source": names[0],
                        "target": names[1],
                        "relation_type": "acquaintance",
                        "description": f"{names[0]}与{names[1]}在本章有所交集",
                        "quote_text": names[0],
                        "confidence": 0.85,
                    }
                )
            if names:
                data["events"].append(
                    {
                        "title": f"{names[0]}出场互动",
                        "event_type": "encounter",
                        "participants": [{"name": n, "role": "participant"} for n in names[:4]],
                        "quote_text": names[0],
                        "story_time_text": None,
                        "certainty": "medium",
                    }
                )
        elif task_hint == "analyze_character_full":
            data = {
                "overview": "基于当前已提取章节证据生成的阶段性人物综合分析（Mock模式）。",
                "cultivation_and_identity": "需结合更多章节证据持续完善。",
                "key_experiences": "详见关联事件时间线。",
                "relationships_summary": "详见人物关系图谱。",
                "personality": "沉稳谨慎，善于谋划。",
                "motivation": "追求大道长生与实力提升。",
                "character_arc": "随章节推进逐步演化。",
                "disputes_and_unknowns": "当前为 Mock 推演，正式分析请配置 DeepSeek API Key。",
                "claims": [
                    {
                        "section": "overview",
                        "claim_text": "该人物在已分析章节中多次登场。",
                        "claim_type": "fact",
                        "certainty": "high",
                        "evidence_ids": [],
                    },
                    {
                        "section": "personality",
                        "claim_text": "人物行为表现出较强的谨慎与自我保护倾向。",
                        "claim_type": "inference",
                        "certainty": "medium",
                        "evidence_ids": [],
                    },
                ],
            }
        else:
            data = {"ok": True}

        in_tok = max(50, len(user_prompt) // 2)
        out_tok = max(40, len(json.dumps(data, ensure_ascii=False)) // 2)
        cost = estimate_llm_cost(model, in_tok, out_tok, self.settings)
        latency_ms = max(5, int((time.monotonic() - t0) * 1000))
        return LLMResponse(
            data=data,
            model=model,
            input_tokens=in_tok,
            output_tokens=out_tok,
            estimated_cost=cost,
            latency_ms=latency_ms,
        )
