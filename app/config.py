from __future__ import annotations

import json
from functools import lru_cache
from typing import Dict

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file='.env', env_file_encoding='utf-8', extra='ignore')

    app_name: str = 'ThreadBoss Agent Backend'
    environment: str = 'development'
    log_level: str = 'INFO'

    public_base_url: str = 'http://localhost:8000'

    # WAHA
    waha_base_url: str = 'http://localhost:3000'
    waha_api_key: str = ''
    waha_webhook_hmac_key: str = ''
    waha_request_retries: int = 4
    waha_retry_base_seconds: float = 0.75

    # Legacy session mapping remains as a migration/fallback path only.
    # New V1.7 users are resolved from Neon public.whatsapp_connections.
    session_tenants_json: str = '{}'

    # Neon / PostgreSQL. Use the pooled Neon URL in production.
    database_url: str = 'postgresql://threadboss:threadboss@postgres:5432/threadboss'
    database_pool_min_size: int = 1
    database_pool_max_size: int = 5
    database_command_timeout_seconds: float = 60.0

    # Website -> ThreadBoss onboarding service key. This key must stay server-side.
    onboarding_api_key: str = 'change-onboarding-key'

    # Backward compatibility for old Slack/Telegram adapters. Keep false for real multi-user use.
    allow_legacy_global_channel_key: bool = False
    channel_api_key: str = 'change-me-too'

    # Free-tier AI providers.
    gemini_api_key: str = ''
    groq_api_key: str = ''
    cloudflare_account_id: str = ''
    cloudflare_api_token: str = ''
    ai_timeout_seconds: float = 60.0

    knowledge_provider: str = 'gemini'
    knowledge_model: str = 'gemini-3.5-flash-lite'
    planner_provider: str = 'gemini'
    planner_model: str = 'gemini-3.5-flash-lite'
    followup_provider: str = 'cloudflare'
    followup_model: str = '@cf/zai-org/glm-4.7-flash'
    action_provider: str = 'gemini'
    action_model: str = 'gemini-3.5-flash-lite'
    router_provider: str = 'cloudflare'
    router_model: str = '@cf/zai-org/glm-4.7-flash'
    vision_provider: str = 'gemini'
    vision_model: str = 'gemini-3.5-flash-lite'
    gemini_fallback_model: str = 'gemini-3.1-flash-lite'
    gemini_model_fallbacks_csv: str = 'gemini-3.5-flash-lite,gemini-3.1-flash-lite'

    default_timezone: str = 'Asia/Kolkata'
    knowledge_summary_limit: int = 120

    initial_backfill_hours: int = 48
    initial_backfill_max_messages: int = 3000
    history_sync_page_size: int = 100
    auto_sync_temporal_queries: bool = True
    temporal_sync_cache_seconds: int = 15 * 60

    index_normal_chat_images: bool = True
    index_normal_chat_documents: bool = True
    index_normal_chat_audio: bool = False

    embedding_provider: str = 'gemini'
    embedding_model: str = 'gemini-embedding-001'
    embedding_dim: int = 768
    min_embed_chars: int = 4
    knowledge_top_k: int = 8

    enable_ai_router: bool = False
    enable_planner_extraction: bool = True

    # Redis
    redis_url: str = 'redis://redis:6379/0'
    redis_stream: str = 'threadboss:events'
    redis_consumer_group: str = 'threadboss-workers'
    redis_consumer_name: str = 'worker-1'
    event_dedupe_ttl_seconds: int = 7 * 24 * 60 * 60
    owner_cache_ttl_seconds: int = 24 * 60 * 60

    interactive_menu_enabled: bool = True
    interactive_menu_poll_fallback: bool = True
    menu_state_ttl_seconds: int = 10 * 60

    media_download_timeout_seconds: float = 60.0
    max_media_mb: int = 25
    max_document_pages: int = 50
    media_context_max_chars: int = 12000
    attachment_memory_seconds: int = 10 * 60
    attachment_memory_max_items: int = 20
    enrich_normal_chat_media: bool = False

    enable_local_stt: bool = True
    whisper_model: str = 'base'
    whisper_device: str = 'cpu'
    whisper_compute_type: str = 'int8'
    whisper_cache_dir: str = '/root/.cache/huggingface'
    vision_timeout_seconds: float = 60.0

    admin_token: str = 'change-me'

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

    def legacy_tenant_for_session(self, session: str) -> str | None:
        return self.session_tenants.get(session)


@lru_cache
def get_settings() -> Settings:
    return Settings()
