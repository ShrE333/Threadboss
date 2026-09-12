from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any

import httpx

from .config import Settings

logger = logging.getLogger(__name__)


class AIUnavailable(RuntimeError):
    pass


@dataclass
class AIResponse:
    text: str
    provider: str
    model: str


def _extract_json(text: str) -> Any:
    text = text.strip()
    if text.startswith('```'):
        text = re.sub(r'^```(?:json)?\s*', '', text)
        text = re.sub(r'\s*```$', '', text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r'(\{.*\}|\[.*\])', text, flags=re.S)
        if not match:
            raise
        return json.loads(match.group(1))


class AIClient:
    """Free-tier-friendly provider abstraction.

    Providers:
      - gemini: Google AI Studio free tier (quota-limited)
      - groq: Groq free plan (quota-limited)
      - cloudflare: Workers AI free allocation (quota-limited)

    ThreadBoss never silently enables a paid API. V1.5 also has a Gemini model
    fallback because Google currently limits some new projects from generating
    with older 2.5 models even though embeddings still work.
    """

    def __init__(self, settings: Settings):
        self.settings = settings

    async def chat(
        self,
        *,
        provider: str,
        model: str,
        system: str,
        user: str,
        temperature: float = 0.2,
        max_tokens: int = 1000,
    ) -> AIResponse:
        provider = provider.strip().lower()
        if provider == 'gemini':
            return await self._gemini_chat(model, system, user, temperature, max_tokens)
        if provider == 'groq':
            return await self._groq_chat(model, system, user, temperature, max_tokens)
        if provider == 'cloudflare':
            return await self._cloudflare_chat(model, system, user, temperature, max_tokens)
        raise AIUnavailable(f'Unsupported AI provider: {provider}')

    async def chat_json(self, **kwargs) -> Any:
        response = await self.chat(**kwargs)
        try:
            return _extract_json(response.text)
        except Exception as exc:
            raise AIUnavailable(f'{response.provider}/{response.model} did not return valid JSON') from exc

    async def embed(self, text: str, *, task_type: str = 'RETRIEVAL_DOCUMENT') -> list[float]:
        provider = self.settings.embedding_provider.lower()
        if provider == 'gemini':
            return await self._gemini_embed(text, task_type)
        if provider == 'cloudflare':
            return await self._cloudflare_embed(text)
        raise AIUnavailable(f'Unsupported embedding provider: {provider}')

    async def _gemini_chat(
        self,
        model: str,
        system: str,
        user: str,
        temperature: float,
        max_tokens: int,
    ) -> AIResponse:
        if not self.settings.gemini_api_key:
            raise AIUnavailable('GEMINI_API_KEY is not configured')

        candidates: list[str] = []
        for candidate in (model, self.settings.gemini_fallback_model):
            candidate = (candidate or '').strip()
            if candidate and candidate not in candidates:
                candidates.append(candidate)

        body = {
            'systemInstruction': {'parts': [{'text': system}]},
            'contents': [{'role': 'user', 'parts': [{'text': user}]}],
            'generationConfig': {
                'temperature': temperature,
                'maxOutputTokens': max_tokens,
            },
        }
        headers = {
            'x-goog-api-key': self.settings.gemini_api_key,
            'Content-Type': 'application/json',
        }

        last_error: Exception | None = None
        for index, candidate in enumerate(candidates):
            url = f'https://generativelanguage.googleapis.com/v1beta/models/{candidate}:generateContent'
            try:
                async with httpx.AsyncClient(timeout=self.settings.ai_timeout_seconds) as client:
                    r = await client.post(url, headers=headers, json=body)

                # New Google AI Studio projects can return 404 for legacy 2.5
                # generation models while embeddings still work. Retry the known
                # free-tier fallback instead of returning a useless RAG snippet.
                if r.status_code == 404 and index + 1 < len(candidates):
                    logger.warning(
                        'Gemini generation model %s returned 404; retrying with %s',
                        candidate,
                        candidates[index + 1],
                    )
                    continue

                r.raise_for_status()
                data = r.json()
                parts = data.get('candidates', [{}])[0].get('content', {}).get('parts', [])
                text = ''.join(str(p.get('text', '')) for p in parts).strip()
                if not text:
                    raise AIUnavailable(f'Gemini {candidate} returned an empty response')
                return AIResponse(text=text, provider='gemini', model=candidate)
            except AIUnavailable:
                raise
            except Exception as exc:
                last_error = exc
                # Only model-not-found automatically changes model. Network/quota
                # errors are surfaced so caller can use deterministic fallback.
                break

        raise AIUnavailable(f'Gemini request failed: {last_error}') from last_error

    async def _gemini_embed(self, text: str, task_type: str) -> list[float]:
        if not self.settings.gemini_api_key:
            raise AIUnavailable('GEMINI_API_KEY is not configured')
        model = self.settings.embedding_model
        url = f'https://generativelanguage.googleapis.com/v1beta/models/{model}:embedContent'
        body = {
            'model': f'models/{model}',
            'content': {'parts': [{'text': text[:12000]}]},
            'embedContentConfig': {
                'taskType': task_type,
                'outputDimensionality': self.settings.embedding_dim,
                'autoTruncate': True,
            },
        }
        headers = {'x-goog-api-key': self.settings.gemini_api_key, 'Content-Type': 'application/json'}
        try:
            async with httpx.AsyncClient(timeout=self.settings.ai_timeout_seconds) as client:
                r = await client.post(url, headers=headers, json=body)
            r.raise_for_status()
            values = r.json().get('embedding', {}).get('values') or []
            if len(values) != self.settings.embedding_dim:
                raise AIUnavailable(f'Expected {self.settings.embedding_dim} embedding dims, got {len(values)}')
            return [float(v) for v in values]
        except AIUnavailable:
            raise
        except Exception as exc:
            raise AIUnavailable(f'Gemini embedding failed: {exc}') from exc

    async def _groq_chat(self, model: str, system: str, user: str, temperature: float, max_tokens: int) -> AIResponse:
        if not self.settings.groq_api_key:
            raise AIUnavailable('GROQ_API_KEY is not configured')
        body = {
            'model': model,
            'messages': [
                {'role': 'system', 'content': system},
                {'role': 'user', 'content': user},
            ],
            'temperature': temperature,
            'max_tokens': max_tokens,
        }
        headers = {'Authorization': f'Bearer {self.settings.groq_api_key}', 'Content-Type': 'application/json'}
        try:
            async with httpx.AsyncClient(timeout=self.settings.ai_timeout_seconds) as client:
                r = await client.post('https://api.groq.com/openai/v1/chat/completions', headers=headers, json=body)
            r.raise_for_status()
            text = r.json()['choices'][0]['message']['content'].strip()
            return AIResponse(text=text, provider='groq', model=model)
        except Exception as exc:
            raise AIUnavailable(f'Groq request failed: {exc}') from exc

    async def _cloudflare_chat(self, model: str, system: str, user: str, temperature: float, max_tokens: int) -> AIResponse:
        if not self.settings.cloudflare_account_id or not self.settings.cloudflare_api_token:
            raise AIUnavailable('Cloudflare Workers AI credentials are not configured')
        url = f'https://api.cloudflare.com/client/v4/accounts/{self.settings.cloudflare_account_id}/ai/run/{model}'
        body = {
            'messages': [
                {'role': 'system', 'content': system},
                {'role': 'user', 'content': user},
            ],
            'temperature': temperature,
            'max_tokens': max_tokens,
        }
        headers = {'Authorization': f'Bearer {self.settings.cloudflare_api_token}', 'Content-Type': 'application/json'}
        try:
            async with httpx.AsyncClient(timeout=self.settings.ai_timeout_seconds) as client:
                r = await client.post(url, headers=headers, json=body)
            r.raise_for_status()
            data = r.json()
            result = data.get('result', {})
            text = result.get('response') or result.get('text') or ''
            if not text and result.get('choices'):
                text = result['choices'][0].get('message', {}).get('content', '')
            if not text:
                raise AIUnavailable('Cloudflare Workers AI returned an empty response')
            return AIResponse(text=str(text).strip(), provider='cloudflare', model=model)
        except AIUnavailable:
            raise
        except Exception as exc:
            raise AIUnavailable(f'Cloudflare Workers AI request failed: {exc}') from exc

    async def _cloudflare_embed(self, text: str) -> list[float]:
        if not self.settings.cloudflare_account_id or not self.settings.cloudflare_api_token:
            raise AIUnavailable('Cloudflare Workers AI credentials are not configured')
        model = self.settings.embedding_model
        url = f'https://api.cloudflare.com/client/v4/accounts/{self.settings.cloudflare_account_id}/ai/run/{model}'
        headers = {'Authorization': f'Bearer {self.settings.cloudflare_api_token}', 'Content-Type': 'application/json'}
        try:
            async with httpx.AsyncClient(timeout=self.settings.ai_timeout_seconds) as client:
                r = await client.post(url, headers=headers, json={'text': [text[:8000]]})
            r.raise_for_status()
            result = r.json().get('result', {})
            data = result.get('data') or []
            values = data[0] if data and isinstance(data[0], list) else data
            if len(values) != self.settings.embedding_dim:
                raise AIUnavailable(f'Expected {self.settings.embedding_dim} embedding dims, got {len(values)}')
            return [float(v) for v in values]
        except AIUnavailable:
            raise
        except Exception as exc:
            raise AIUnavailable(f'Cloudflare embedding failed: {exc}') from exc
