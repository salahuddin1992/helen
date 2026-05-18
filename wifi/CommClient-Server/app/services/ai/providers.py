"""
LLM provider abstraction layer.

Supported backends (each optional — import-error → ProviderUnavailable):
    * AnthropicProvider  (anthropic-py)
    * OpenAIProvider     (openai-py)
    * OllamaProvider     (raw httpx — http://localhost:11434)

All three expose the same surface:
    complete(prompt, opts) -> CompletionResult
    stream(prompt, opts)   -> AsyncIterator[str]
    embed(text)            -> list[float]
"""
from __future__ import annotations

import abc
import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, ClassVar

import httpx

from app.core.logging import get_logger

logger = get_logger(__name__)


# Per-1M-token pricing in micro-USD (≈ 0.1-cent units), tunable per model.
_PRICING: dict[str, tuple[int, int]] = {
    # (input_per_million, output_per_million) in micro-USD
    "claude-3-5-sonnet-latest":     (3_000_000, 15_000_000),
    "claude-3-5-haiku-latest":      (800_000, 4_000_000),
    "gpt-4o":                       (2_500_000, 10_000_000),
    "gpt-4o-mini":                  (150_000, 600_000),
    "llama3.1:8b":                  (0, 0),
    "qwen2.5:7b":                   (0, 0),
}


@dataclass
class CompletionOptions:
    max_tokens: int = 1024
    temperature: float = 0.4
    system: str | None = None
    stop: list[str] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class CompletionResult:
    text: str
    input_tokens: int
    output_tokens: int
    latency_ms: int
    cost_micro_usd: int
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


def _estimate_cost(model: str, in_t: int, out_t: int) -> int:
    p = _PRICING.get(model, (1_000_000, 5_000_000))
    return (in_t * p[0] + out_t * p[1]) // 1_000_000


class ProviderUnavailable(RuntimeError):
    pass


class LLMProvider(abc.ABC):
    name: ClassVar[str] = ""

    def __init__(self, model: str, api_key: str | None = None,
                 base_url: str | None = None) -> None:
        self.model = model
        self.api_key = api_key
        self.base_url = base_url

    @abc.abstractmethod
    async def complete(self, prompt: str, opts: CompletionOptions) -> CompletionResult: ...

    @abc.abstractmethod
    def stream(self, prompt: str, opts: CompletionOptions) -> AsyncIterator[str]: ...

    @abc.abstractmethod
    async def embed(self, text: str) -> list[float]: ...


# ── Anthropic ───────────────────────────────────────────────


try:
    import anthropic                                          # type: ignore
    _ANTHROPIC_OK = True
except Exception:                                            # pragma: no cover
    anthropic = None
    _ANTHROPIC_OK = False


class AnthropicProvider(LLMProvider):
    name = "anthropic"

    def __init__(self, model: str, api_key: str | None,
                 base_url: str | None = None) -> None:
        if not _ANTHROPIC_OK:
            raise ProviderUnavailable("install: pip install anthropic")
        if not api_key:
            raise ProviderUnavailable("anthropic api_key required")
        super().__init__(model, api_key, base_url)
        self._client = anthropic.AsyncAnthropic(
            api_key=api_key, base_url=base_url,
        )

    async def complete(self, prompt: str, opts: CompletionOptions) -> CompletionResult:
        t0 = time.monotonic()
        msg = await self._client.messages.create(
            model=self.model,
            max_tokens=opts.max_tokens,
            temperature=opts.temperature,
            system=opts.system or anthropic.NOT_GIVEN,
            stop_sequences=opts.stop or anthropic.NOT_GIVEN,
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(getattr(b, "text", "") for b in msg.content)
        in_t = msg.usage.input_tokens
        out_t = msg.usage.output_tokens
        return CompletionResult(
            text=text, input_tokens=in_t, output_tokens=out_t,
            latency_ms=int((time.monotonic() - t0) * 1000),
            cost_micro_usd=_estimate_cost(self.model, in_t, out_t),
            raw={"id": msg.id, "stop_reason": msg.stop_reason},
        )

    async def stream(self, prompt: str, opts: CompletionOptions):  # type: ignore[override]
        async with self._client.messages.stream(
            model=self.model, max_tokens=opts.max_tokens,
            temperature=opts.temperature,
            system=opts.system or anthropic.NOT_GIVEN,
            messages=[{"role": "user", "content": prompt}],
        ) as stream:
            async for chunk in stream.text_stream:
                yield chunk

    async def embed(self, text: str) -> list[float]:
        # Anthropic doesn't expose embeddings; fall through to a deterministic
        # local hash-based vector (kept stable across calls).
        return _hash_embed(text)


# ── OpenAI ──────────────────────────────────────────────────


try:
    import openai                                             # type: ignore
    _OPENAI_OK = True
except Exception:                                            # pragma: no cover
    openai = None
    _OPENAI_OK = False


class OpenAIProvider(LLMProvider):
    name = "openai"

    def __init__(self, model: str, api_key: str | None,
                 base_url: str | None = None) -> None:
        if not _OPENAI_OK:
            raise ProviderUnavailable("install: pip install openai")
        if not api_key:
            raise ProviderUnavailable("openai api_key required")
        super().__init__(model, api_key, base_url)
        self._client = openai.AsyncOpenAI(
            api_key=api_key, base_url=base_url,
        )

    async def complete(self, prompt: str, opts: CompletionOptions) -> CompletionResult:
        t0 = time.monotonic()
        msgs: list[dict[str, str]] = []
        if opts.system:
            msgs.append({"role": "system", "content": opts.system})
        msgs.append({"role": "user", "content": prompt})
        resp = await self._client.chat.completions.create(
            model=self.model, messages=msgs,
            max_tokens=opts.max_tokens, temperature=opts.temperature,
            stop=opts.stop or None,
        )
        text = resp.choices[0].message.content or ""
        in_t = resp.usage.prompt_tokens if resp.usage else 0
        out_t = resp.usage.completion_tokens if resp.usage else 0
        return CompletionResult(
            text=text, input_tokens=in_t, output_tokens=out_t,
            latency_ms=int((time.monotonic() - t0) * 1000),
            cost_micro_usd=_estimate_cost(self.model, in_t, out_t),
            raw={"id": resp.id, "finish_reason": resp.choices[0].finish_reason},
        )

    async def stream(self, prompt: str, opts: CompletionOptions):  # type: ignore[override]
        msgs: list[dict[str, str]] = []
        if opts.system:
            msgs.append({"role": "system", "content": opts.system})
        msgs.append({"role": "user", "content": prompt})
        stream = await self._client.chat.completions.create(
            model=self.model, messages=msgs,
            max_tokens=opts.max_tokens, temperature=opts.temperature,
            stream=True,
        )
        async for chunk in stream:
            delta = chunk.choices[0].delta.content if chunk.choices else None
            if delta:
                yield delta

    async def embed(self, text: str) -> list[float]:
        try:
            resp = await self._client.embeddings.create(
                model="text-embedding-3-small", input=text,
            )
            return list(resp.data[0].embedding)
        except Exception:
            return _hash_embed(text)


# ── Ollama (local) ──────────────────────────────────────────


class OllamaProvider(LLMProvider):
    name = "ollama"

    def __init__(self, model: str, api_key: str | None = None,
                 base_url: str | None = None) -> None:
        super().__init__(model, api_key, base_url or "http://localhost:11434")
        self._http = httpx.AsyncClient(timeout=120.0)

    async def complete(self, prompt: str, opts: CompletionOptions) -> CompletionResult:
        t0 = time.monotonic()
        body = {
            "model": self.model,
            "prompt": prompt,
            "system": opts.system or "",
            "stream": False,
            "options": {
                "temperature": opts.temperature,
                "num_predict": opts.max_tokens,
                "stop": opts.stop or [],
            },
        }
        resp = await self._http.post(f"{self.base_url}/api/generate", json=body)
        resp.raise_for_status()
        data = resp.json()
        text = data.get("response", "")
        in_t = int(data.get("prompt_eval_count", 0) or 0)
        out_t = int(data.get("eval_count", 0) or 0)
        return CompletionResult(
            text=text, input_tokens=in_t, output_tokens=out_t,
            latency_ms=int((time.monotonic() - t0) * 1000),
            cost_micro_usd=0,
            raw={"model": self.model, "done": data.get("done")},
        )

    async def stream(self, prompt: str, opts: CompletionOptions):  # type: ignore[override]
        body = {
            "model": self.model, "prompt": prompt,
            "system": opts.system or "", "stream": True,
            "options": {"temperature": opts.temperature,
                        "num_predict": opts.max_tokens},
        }
        async with self._http.stream(
            "POST", f"{self.base_url}/api/generate", json=body,
        ) as resp:
            async for line in resp.aiter_lines():
                if not line:
                    continue
                import json as _json
                try:
                    obj = _json.loads(line)
                except Exception:
                    continue
                if obj.get("response"):
                    yield obj["response"]
                if obj.get("done"):
                    break

    async def embed(self, text: str) -> list[float]:
        try:
            resp = await self._http.post(
                f"{self.base_url}/api/embeddings",
                json={"model": self.model, "prompt": text},
            )
            resp.raise_for_status()
            return list(resp.json().get("embedding") or [])
        except Exception:
            return _hash_embed(text)


# ── deterministic fallback embeddings ───────────────────────


def _hash_embed(text: str, dim: int = 256) -> list[float]:
    """Cheap, deterministic embedding (rolling hash) — only used when the
    provider lacks an embeddings endpoint. Not semantic, but stable."""
    import hashlib
    h = hashlib.sha256(text.encode("utf-8", errors="ignore")).digest()
    rep = (h * ((dim // len(h)) + 1))[:dim]
    return [(b - 128) / 128.0 for b in rep]


# ── Registry / factory ──────────────────────────────────────


def make_provider(provider: str, *, model: str, api_key: str | None,
                  base_url: str | None) -> LLMProvider:
    p = (provider or "none").lower()
    if p == "anthropic":
        return AnthropicProvider(model, api_key, base_url)
    if p == "openai":
        return OpenAIProvider(model, api_key, base_url)
    if p == "ollama":
        return OllamaProvider(model, api_key, base_url)
    raise ProviderUnavailable(f"unknown / disabled provider: {provider!r}")


def installed_providers() -> dict[str, bool]:
    return {
        "anthropic": _ANTHROPIC_OK,
        "openai": _OPENAI_OK,
        "ollama": True,                                       # always usable
    }
