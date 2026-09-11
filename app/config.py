from __future__ import annotations

import json
from functools import lru_cache
from typing import Dict

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file='.env', env_file_encoding='utf-8', extra='ignore')

    app_name: str = 'ThreadBoss Agent Backend'
    environment: str = 'development'
    log_level: str = 'INFO'

    # Public URL of this service, used when configuring WAHA webhooks.
    public_base_url: str = 'http://localhost:8000'

    # WAHA
    waha_base_url: str = 'http://localhost:3000'
    waha_api_key: str = ''
    waha_webhook_hmac_key: str = ''

    # Map each WAHA session to one ThreadBoss tenant.
    # Example: {"default":"tenant_shriram","rehan":"tenant_rehan"}
    session_tenants_json: str = '{"default":"tenant_demo"}'



    # Database / vector memory. The EasyPanel compose file ships a free local pgvector Postgres.
    # You can replace this with a free Neon Postgres URL if preferred.
    database_url: str = 'postgresql://threadboss:threadboss@postgres:5432/threadboss'

    # Free-tier AI providers only. No OpenAI/Anthropic paid dependency is required.
    # Provider values: gemini | groq | cloudflare
    gemini_api_key: str = ''
    groq_api_key: str = ''
    cloudflare_account_id: str = ''
    cloudflare_api_token: str = ''
    ai_timeout_seconds: float = 60.0

    # Agent models. Defaults use free-tier-capable endpoints as of Sep 2026.
    knowledge_provider: str = 'gemini'
    knowledge_model: str = 'gemini-2.5-flash-lite'
    planner_provider: str = 'gemini'
    planner_model: str = 'gemini-2.5-flash-lite'
    followup_provider: str = 'cloudflare'
    followup_model: str = '@cf/zai-org/glm-4.7-flash'
    action_provider: str = 'gemini'
    action_model: str = 'gemini-2.5-flash-lite'
    router_provider: str = 'cloudflare'
    router_model: str = '@cf/zai-org/glm-4.7-flash'
    vision_provider: str = 'gemini'
    vision_model: str = 'gemini-2.5-flash-lite'

    # Embeddings. Gemini embedding has a free tier; 768 dims keeps pgvector compact.
    embedding_provider: str = 'gemini'
    embedding_model: str = 'gemini-embedding-001'
    embedding_dim: int = 768
    min_embed_chars: int = 4
    knowledge_top_k: int = 8

    # Cost/quota controls. Router is deterministic by default to save free-tier calls.
    enable_ai_router: bool = False
    enable_planner_extraction: bool = True

    # Redis provides durable queueing, session-owner cache and de-duplication.
    redis_url: str = 'redis://redis:6379/0'
    redis_stream: str = 'threadboss:events'
    redis_consumer_group: str = 'threadboss-workers'
    redis_consumer_name: str = 'worker-1'
    event_dedupe_ttl_seconds: int = 7 * 24 * 60 * 60
    owner_cache_ttl_seconds: int = 24 * 60 * 60

    # Media + multimodal self-chat
    media_download_timeout_seconds: float = 60.0
    max_media_mb: int = 25
    max_document_pages: int = 50
    media_context_max_chars: int = 12000
    attachment_memory_seconds: int = 10 * 60
    attachment_memory_max_items: int = 20
    enrich_normal_chat_media: bool = False

    # Local speech-to-text (runs in the worker container)
    enable_local_stt: bool = True
    whisper_model: str = 'base'
    whisper_device: str = 'cpu'
    whisper_compute_type: str = 'int8'
    whisper_cache_dir: str = '/root/.cache/huggingface'

    # Optional image understanding timeout. If Gemini is not configured, images fall back to OCR.
    vision_timeout_seconds: float = 60.0

    # Protect admin/bootstrap endpoints.
    admin_token: str = 'change-me'
    channel_api_key: str = 'change-me-too'

    # Worker behavior
    worker_block_ms: int = 5000
    worker_batch_size: int = 20
    worker_max_attempts: int = 4
    worker_retry_base_seconds: float = 2.0
    redis_deadletter_stream: str = 'threadboss:deadletter'

    @property
    def max_media_bytes(self) -> int:
        return self.max_media_mb * 1024 * 1024

    @property
    def session_tenants(self) -> Dict[str, str]:
        value = json.loads(self.session_tenants_json or '{}')
        if not isinstance(value, dict):
            raise ValueError('SESSION_TENANTS_JSON must be a JSON object')
        return {str(k): str(v) for k, v in value.items()}

    def tenant_for_session(self, session: str) -> str:
        mapping = self.session_tenants
        if session not in mapping:
            raise KeyError(
                f"No tenant mapping for WAHA session '{session}'. "
                'Add it to SESSION_TENANTS_JSON.'
            )
        return mapping[session]


@lru_cache
def get_settings() -> Settings:
    return Settings()
