from __future__ import annotations

import os
import random
import time
import hashlib
from dataclasses import dataclass
from typing import Any, Dict, Optional, Sequence, Type, Union, List

from openai import OpenAI, APIError, APIConnectionError, RateLimitError, APITimeoutError
from pydantic import BaseModel

from .logging_setup import get_logger, setup_logger

log = get_logger("openai_client")


def _normalize_tools(tools: Optional[Sequence[Any]]) -> Optional[List[Dict[str, Any]]]:
    if not tools:
        return None
    norm: List[Dict[str, Any]] = []
    for t in tools:
        if isinstance(t, str):
            key = t.strip().lower()
            if key in {"web_search", "web-search", "web_search_preview"}:
                norm.append({"type": "web_search_preview"})
                continue
            raise ValueError(f"Unsupported tool string: {t!r}")

        if isinstance(t, dict):
            if "type" in t:
                t2 = dict(t)
                ttype = str(t2.get("type")).strip().lower()
                if ttype in {"web_search", "web-search"}:
                    t2["type"] = "web_search_preview"
                norm.append(t2)
                continue

            if "function" in t:
                name = t.get("function")
                if not isinstance(name, str) or not name:
                    raise ValueError("Function tool requires a non-empty 'function' name.")
                tool_obj: Dict[str, Any] = {
                    "type": "function",
                    "name": name,
                }
                if "parameters" in t:
                    tool_obj["parameters"] = t["parameters"]
                if "description" in t:
                    tool_obj["description"] = t["description"]
                if "strict" in t:
                    tool_obj["strict"] = t["strict"]
                norm.append(tool_obj)
                continue

        raise ValueError("Each tool must be either a supported string alias, a full Responses API dict with 'type', or a simplified {'function': name, ...} dict.")
    return norm


@dataclass
class LLMResult:
    text: Optional[str]
    parsed: Optional[Any]
    raw: Any


class LLMClient:
    def __init__(
        self,
        model: str = "gpt-5-mini",
        *,
        structured_output: Optional[Type[BaseModel]] = None,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        max_retries: int = 3,
        retry_backoff: float = 1.5,
        cache_prompt: Optional[str] = None,
        prompt_cache_key: Optional[str] = None,
        tools: Optional[Sequence[Any]] = None,
    ) -> None:
        self.model = model
        self.structured_output = structured_output
        self.max_retries = max(1, int(max_retries))
        self.retry_backoff = float(retry_backoff)
        self.cache_prompt = cache_prompt
        self._tools = _normalize_tools(tools)
        if prompt_cache_key is None and cache_prompt:
            digest = hashlib.sha1(cache_prompt.encode("utf-8")).hexdigest()
            prompt_cache_key = f"mm-{digest[:16]}"
        self.prompt_cache_key = prompt_cache_key

        client_kwargs: Dict[str, Any] = {}
        if api_key:
            client_kwargs["api_key"] = api_key
        # Resolve base_url precedence: explicit arg > env > official default
        resolved_base_url = base_url or os.getenv("OPENAI_BASE_URL") or "https://api.openai.com/v1"
        client_kwargs["base_url"] = resolved_base_url
        self._client = OpenAI(**client_kwargs)

    def chat(
        self,
        *,
        user: Optional[str] = None,
        system: Optional[str] = None,
        assistant: Optional[str] = None,
        return_result: bool = False,
    ) -> Union[str, Any, LLMResult]:
        messages: List[Dict[str, str]] = []
        if self.cache_prompt:
            messages.append({"role": "system", "content": self.cache_prompt})
        if system:
            messages.append({"role": "system", "content": system})
        if assistant:
            messages.append({"role": "assistant", "content": assistant})
        if user:
            messages.append({"role": "user", "content": user})
        if not messages:
            raise ValueError("No content provided. Provide at least one of `user`, `system`, or `assistant`.")

        attempt = 0
        last_err: Optional[Exception] = None
        parsed_obj: Optional[Any] = None
        text: Optional[str] = None
        raw_resp: Any = None

        while attempt < self.max_retries:
            try:
                log.debug(
                    f"LLM call attempt {attempt + 1}/{self.max_retries} | model={self.model}"
                )
                if self.structured_output is not None and not self._tools:
                    raw_resp = self._client.responses.parse(  # type: ignore[attr-defined]
                        model=self.model,
                        input=list(messages),
                        text_format=self.structured_output,
                    )
                    parsed_obj = raw_resp.output_parsed  # type: ignore[attr-defined]
                    result = LLMResult(text=None, parsed=parsed_obj, raw=raw_resp)
                    return result if return_result else parsed_obj
                create_kwargs: Dict[str, Any] = {
                    "model": self.model,
                    "input": list(messages),
                }
                if self.prompt_cache_key:
                    create_kwargs["prompt_cache_key"] = self.prompt_cache_key
                if self._tools:
                    create_kwargs["tools"] = self._tools
                raw_resp = self._client.responses.create(  # type: ignore[attr-defined]
                    **create_kwargs
                )
                try:
                    usage = getattr(raw_resp, "usage", None)
                    cached_tokens = None
                    if usage is not None:
                        details = getattr(usage, "prompt_tokens_details", None)
                        cached_tokens = getattr(details, "cached_tokens", None)
                    if cached_tokens is None:
                        to_dict = getattr(raw_resp, "to_dict", None)
                        if callable(to_dict):
                            usage_dict = to_dict().get("usage", {})
                            cached_tokens = (
                                (usage_dict.get("prompt_tokens_details") or {}).get("cached_tokens")
                            )
                    if cached_tokens is not None:
                        log.debug(f"Prompt caching: cached_tokens={cached_tokens}")
                except Exception as _log_e:
                    log.debug(f"Prompt caching usage log skipped: {_log_e}")
                text = getattr(raw_resp, "output_parsed", None)
                if text is None:
                    text = getattr(raw_resp, "output_text", None)
                if text is None:
                    text = ""
                if self.structured_output is not None:
                    try:
                        parsed_obj = self.structured_output.model_validate_json(text)  # type: ignore[attr-defined]
                    except Exception:
                        try:
                            import json as _json
                            parsed_obj = self.structured_output.model_validate(_json.loads(text))  # type: ignore[attr-defined]
                        except Exception as pe:
                            log.warning(f"Parsing to structured output failed: {pe}")
                            raise
                    result = LLMResult(text=text, parsed=parsed_obj, raw=raw_resp)
                    return result if return_result else parsed_obj
                result = LLMResult(text=text, parsed=None, raw=raw_resp)
                return result if return_result else (text or "")

            except (RateLimitError, APIError, APIConnectionError, APITimeoutError) as e:
                last_err = e
                log.warning(
                    f"API error during LLM call (attempt {attempt + 1}/{self.max_retries}): "
                    f"{self._format_error(e)}"
                )
                self._sleep_with_backoff(attempt)
                attempt += 1
                continue
            except Exception as e:
                last_err = e
                log.warning(
                    f"Unexpected error during LLM call (attempt {attempt + 1}/{self.max_retries}): "
                    f"{self._format_error(e)}"
                )
                self._sleep_with_backoff(attempt)
                attempt += 1
                continue

        if last_err:
            raise last_err
        raise RuntimeError("LLM call failed after retries with no exception captured.")

    def _format_error(self, e: Exception) -> str:
        etype = e.__class__.__name__
        msg = str(e)
        status = getattr(e, "status_code", getattr(e, "status", None))
        code = getattr(e, "code", None)
        request_id = getattr(e, "request_id", None)
        parts = [etype]
        if status is not None:
            parts.append(f"status={status}")
        if code is not None:
            parts.append(f"code={code}")
        if request_id is not None:
            parts.append(f"request_id={request_id}")
        if msg:
            parts.append(f"message={msg}")
        return ", ".join(parts)

    def _sleep_with_backoff(self, attempt: int) -> None:
        delay = self.retry_backoff * (2 ** attempt)
        delay += random.uniform(0, 0.25 * (attempt + 1))
        log.warning(f"Retrying after error; sleeping for {delay:.2f}s (attempt {attempt + 1})")
        time.sleep(delay)


if __name__ == "__main__":
    setup_logger(name="openai_client")
    log.info("[LLMClient] Manual tests starting...")

    def _section(title: str) -> None:
        log.info(f"=== {title} ===")

    api_key_present = bool(os.getenv("OPENAI_API_KEY"))
    base_url = os.getenv("OPENAI_BASE_URL") or "(default)"
    model_env = os.getenv("OPENAI_MODEL")
    model_name = model_env or "gpt-5-mini"

    log.info(f"OPENAI_API_KEY present: {'yes' if api_key_present else 'no'}")
    log.info(f"OPENAI_BASE_URL: {base_url}")
    log.info(f"Model: {model_name}")

    if not api_key_present:
        log.warning("[Skip] No OPENAI_API_KEY in env; tests require a valid API key.")
    else:
        # Test 1: Simple chat
        try:
            _section("Test 1: Simple chat")
            llm = LLMClient(model=model_name)
            system_msg = "You are a concise assistant."
            user_msg = "Say hello in one short sentence."
            log.info(f"system: {system_msg}")
            log.info(f"user:   {user_msg}")
            reply = llm.chat(system=system_msg, user=user_msg)
            log.info(f"response: {reply}")
        except Exception as e:
            log.error(f"[Error] Simple chat failed: {e}")

        # Test 2: Structured output
        try:
            _section("Test 2: Structured output")
            class Todo(BaseModel):
                title: str
                due: str
                priority: int

            llm_struct = LLMClient(model=model_name, structured_output=Todo)
            system_msg = "Extract a TODO item."
            user_msg = "Finish the draft by tomorrow, high priority."
            log.info(f"system: {system_msg}")
            log.info(f"user:   {user_msg}")
            todo = llm_struct.chat(system=system_msg, user=user_msg)
            log.info(f"parsed type: {type(todo)}")
            log.info(f"parsed value: {todo}")
        except Exception as e:
            log.error(f"[Error] Structured output failed: {e}")

        # Test 3: Web search tool (may require specific model/tool availability)
        try:
            _section("Test 3: Web search tool")
            llm_ws = LLMClient(model=model_name, tools=["web_search"])  # maps to web_search_preview
            system_msg = "You are helpful."
            user_msg = "Summarize headlines about AI this week."
            log.info(f"system: {system_msg}")
            log.info(f"user:   {user_msg}")
            result_ws = llm_ws.chat(system=system_msg, user=user_msg, return_result=True)
            log.info(f"response (text): {getattr(result_ws, 'text', None)}")
        except Exception as e:
            log.warning(f"[Note] Web search tool test skipped/failed: {e}")
