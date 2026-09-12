from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any, AsyncIterator
from urllib.parse import urlparse

import asyncpg

from .config import Settings


class Database:
    """Shared Neon/Postgres data layer.

    V1.7 separates website/control-plane tables (public schema) from agent memory
    tables (threadboss schema). Agent memory tables have PostgreSQL RLS enabled
    and forced; every memory/task transaction sets threadboss.tenant_id before
    reading or writing so an accidental query cannot cross tenant boundaries.
    """

    def __init__(self, settings: Settings):
        self.settings = settings
        self.pool: asyncpg.Pool | None = None
        self._init_lock = asyncio.Lock()
        self._initialized = False

    def _pool_kwargs(self) -> dict[str, Any]:
        host = (urlparse(self.settings.database_url).hostname or '').lower()
        # Neon pooled URLs use PgBouncer. Disabling asyncpg's client statement
        # cache avoids stale prepared-statement issues across pooled backends.
        statement_cache_size = 0 if '-pooler.' in host else 100
        return {
            'min_size': max(1, int(self.settings.database_pool_min_size)),
            'max_size': max(1, int(self.settings.database_pool_max_size)),
            'command_timeout': float(self.settings.database_command_timeout_seconds),
            'statement_cache_size': statement_cache_size,
            'max_inactive_connection_lifetime': 300.0,
        }

    async def ensure(self) -> None:
        if self._initialized:
            return
        async with self._init_lock:
            if self._initialized:
                return

            self.pool = await asyncpg.create_pool(self.settings.database_url, **self._pool_kwargs())
            async with self.pool.acquire() as conn:
                await conn.execute('CREATE EXTENSION IF NOT EXISTS vector')
                await conn.execute('CREATE EXTENSION IF NOT EXISTS pgcrypto')

                # ---------------------------------------------------------
                # Website / identity control plane (public schema)
                # ---------------------------------------------------------
                await conn.execute("""
                    CREATE TABLE IF NOT EXISTS public.users (
                        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                        auth_provider VARCHAR(50) NOT NULL,
                        auth_subject VARCHAR(255) NOT NULL,
                        email VARCHAR(255),
                        display_name VARCHAR(120),
                        avatar_url TEXT,
                        status VARCHAR(30) NOT NULL DEFAULT 'active',
                        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                        UNIQUE(auth_provider, auth_subject)
                    )
                """)
                await conn.execute("""
                    CREATE TABLE IF NOT EXISTS public.tenants (
                        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                        owner_user_id UUID NOT NULL REFERENCES public.users(id) ON DELETE CASCADE,
                        name VARCHAR(120),
                        status VARCHAR(30) NOT NULL DEFAULT 'active',
                        onboarding_completed BOOLEAN NOT NULL DEFAULT false,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                        updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
                    )
                """)
                await conn.execute("""
                    CREATE TABLE IF NOT EXISTS public.tenant_members (
                        tenant_id UUID NOT NULL REFERENCES public.tenants(id) ON DELETE CASCADE,
                        user_id UUID NOT NULL REFERENCES public.users(id) ON DELETE CASCADE,
                        role VARCHAR(30) NOT NULL DEFAULT 'owner',
                        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                        PRIMARY KEY (tenant_id, user_id)
                    )
                """)
                await conn.execute("""
                    CREATE TABLE IF NOT EXISTS public.tenant_permissions (
                        tenant_id UUID PRIMARY KEY REFERENCES public.tenants(id) ON DELETE CASCADE,
                        read_direct_messages BOOLEAN NOT NULL DEFAULT true,
                        read_group_messages BOOLEAN NOT NULL DEFAULT true,
                        process_images BOOLEAN NOT NULL DEFAULT true,
                        process_voice_notes BOOLEAN NOT NULL DEFAULT true,
                        process_documents BOOLEAN NOT NULL DEFAULT true,
                        create_reminders BOOLEAN NOT NULL DEFAULT true,
                        calendar_read BOOLEAN NOT NULL DEFAULT false,
                        calendar_write BOOLEAN NOT NULL DEFAULT false,
                        send_messages BOOLEAN NOT NULL DEFAULT false,
                        auto_followups BOOLEAN NOT NULL DEFAULT false,
                        allow_actions BOOLEAN NOT NULL DEFAULT false,
                        history_import_hours INTEGER NOT NULL DEFAULT 24,
                        updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
                    )
                """)
                await conn.execute("""
                    CREATE TABLE IF NOT EXISTS public.whatsapp_connections (
                        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                        tenant_id UUID NOT NULL REFERENCES public.tenants(id) ON DELETE CASCADE,
                        waha_session_name VARCHAR(150) NOT NULL UNIQUE,
                        phone_number_hash TEXT,
                        owner_jid TEXT,
                        owner_lid TEXT,
                        engine VARCHAR(30) NOT NULL DEFAULT 'GOWS',
                        status VARCHAR(30) NOT NULL DEFAULT 'pending',
                        connected_at TIMESTAMPTZ,
                        disconnected_at TIMESTAMPTZ,
                        last_seen_at TIMESTAMPTZ,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                        updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
                    )
                """)
                await conn.execute('CREATE INDEX IF NOT EXISTS idx_whatsapp_connections_tenant ON public.whatsapp_connections(tenant_id)')
                await conn.execute("""
                    CREATE TABLE IF NOT EXISTS public.onboarding_sessions (
                        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                        tenant_id UUID NOT NULL REFERENCES public.tenants(id) ON DELETE CASCADE,
                        whatsapp_connection_id UUID REFERENCES public.whatsapp_connections(id) ON DELETE CASCADE,
                        status VARCHAR(30) NOT NULL DEFAULT 'created',
                        qr_reference TEXT,
                        expires_at TIMESTAMPTZ,
                        error_message TEXT,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                        completed_at TIMESTAMPTZ
                    )
                """)
                await conn.execute("""
                    CREATE TABLE IF NOT EXISTS public.channel_connections (
                        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                        tenant_id UUID NOT NULL REFERENCES public.tenants(id) ON DELETE CASCADE,
                        channel_type VARCHAR(30) NOT NULL,
                        external_account_id TEXT,
                        status VARCHAR(30) NOT NULL DEFAULT 'connected',
                        metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                        updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
                    )
                """)

                # ---------------------------------------------------------
                # ThreadBoss private application schema
                # ---------------------------------------------------------
                await conn.execute('CREATE SCHEMA IF NOT EXISTS threadboss')
                await conn.execute(f"""
                    CREATE TABLE IF NOT EXISTS threadboss.messages (
                        id BIGSERIAL PRIMARY KEY,
                        tenant_id TEXT NOT NULL,
                        channel TEXT NOT NULL DEFAULT 'whatsapp',
                        session_id TEXT NOT NULL,
                        chat_id TEXT NOT NULL,
                        chat_type TEXT NOT NULL DEFAULT 'unknown',
                        sender_id TEXT,
                        recipient_id TEXT,
                        participant_id TEXT,
                        message_id TEXT NOT NULL,
                        ts TIMESTAMPTZ NOT NULL,
                        text TEXT NOT NULL DEFAULT '',
                        from_me BOOLEAN NOT NULL DEFAULT FALSE,
                        metadata JSONB NOT NULL DEFAULT '{{}}'::jsonb,
                        embedding vector({self.settings.embedding_dim}),
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        UNIQUE (tenant_id, channel, session_id, message_id)
                    )
                """)
                await conn.execute('CREATE INDEX IF NOT EXISTS idx_tb_messages_tenant_ts ON threadboss.messages(tenant_id, ts DESC)')
                await conn.execute('CREATE INDEX IF NOT EXISTS idx_tb_messages_chat ON threadboss.messages(tenant_id, channel, chat_id, ts DESC)')
                try:
                    await conn.execute(
                        'CREATE INDEX IF NOT EXISTS idx_tb_messages_embedding '
                        'ON threadboss.messages USING hnsw (embedding vector_cosine_ops)'
                    )
                except Exception:
                    # HNSW may not be available on every pgvector version; exact
                    # retrieval still works without this optional performance index.
                    pass

                await conn.execute("""
                    CREATE TABLE IF NOT EXISTS threadboss.tasks (
                        id BIGSERIAL PRIMARY KEY,
                        tenant_id TEXT NOT NULL,
                        channel TEXT NOT NULL DEFAULT 'whatsapp',
                        title TEXT NOT NULL,
                        owner TEXT,
                        due_at TIMESTAMPTZ,
                        status TEXT NOT NULL DEFAULT 'open',
                        source_message_id TEXT,
                        source_chat_id TEXT,
                        evidence TEXT,
                        confidence REAL NOT NULL DEFAULT 0.5,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    )
                """)
                await conn.execute('CREATE INDEX IF NOT EXISTS idx_tb_tasks_tenant_status_due ON threadboss.tasks(tenant_id, status, due_at)')
                await conn.execute('CREATE INDEX IF NOT EXISTS idx_tb_tasks_source_message ON threadboss.tasks(tenant_id, channel, source_message_id)')

                await conn.execute("""
                    CREATE TABLE IF NOT EXISTS threadboss.channel_api_tokens (
                        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                        tenant_id TEXT NOT NULL,
                        channel TEXT NOT NULL DEFAULT 'any',
                        token_sha256 TEXT NOT NULL UNIQUE,
                        label TEXT,
                        status TEXT NOT NULL DEFAULT 'active',
                        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                        last_used_at TIMESTAMPTZ
                    )
                """)
                await conn.execute('CREATE INDEX IF NOT EXISTS idx_tb_channel_tokens_tenant ON threadboss.channel_api_tokens(tenant_id, status)')

                await conn.execute("""
                    CREATE TABLE IF NOT EXISTS threadboss.audit_logs (
                        id BIGSERIAL PRIMARY KEY,
                        tenant_id TEXT,
                        actor_type TEXT NOT NULL,
                        actor_id TEXT,
                        action TEXT NOT NULL,
                        resource_type TEXT,
                        resource_id TEXT,
                        success BOOLEAN NOT NULL DEFAULT true,
                        metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
                    )
                """)
                await conn.execute('CREATE INDEX IF NOT EXISTS idx_tb_audit_tenant_created ON threadboss.audit_logs(tenant_id, created_at DESC)')

                # DB-enforced tenant fence for agent memory + tasks.
                for table in ('messages', 'tasks'):
                    await conn.execute(f'ALTER TABLE threadboss.{table} ENABLE ROW LEVEL SECURITY')
                    await conn.execute(f'ALTER TABLE threadboss.{table} FORCE ROW LEVEL SECURITY')
                    await conn.execute(f'DROP POLICY IF EXISTS tenant_isolation ON threadboss.{table}')
                    await conn.execute(f"""
                        CREATE POLICY tenant_isolation ON threadboss.{table}
                        USING (tenant_id = current_setting('threadboss.tenant_id', true))
                        WITH CHECK (tenant_id = current_setting('threadboss.tenant_id', true))
                    """)

            self._initialized = True

    @asynccontextmanager
    async def tenant_conn(self, tenant_id: str) -> AsyncIterator[asyncpg.Connection]:
        await self.ensure()
        assert self.pool
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("SELECT set_config('threadboss.tenant_id', $1, true)", str(tenant_id))
                yield conn

    @staticmethod
    def vector_literal(values: list[float]) -> str:
        return '[' + ','.join(f'{v:.8f}' for v in values) + ']'

    # ------------------------------------------------------------------
    # Identity / onboarding / channel registry
    # ------------------------------------------------------------------
    async def register_user_tenant(
        self,
        *,
        auth_provider: str,
        auth_subject: str,
        email: str | None,
        display_name: str | None,
        tenant_name: str | None,
    ) -> dict[str, Any]:
        await self.ensure()
        assert self.pool
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                user = await conn.fetchrow(
                    """
                    INSERT INTO public.users(auth_provider, auth_subject, email, display_name)
                    VALUES($1,$2,$3,$4)
                    ON CONFLICT(auth_provider, auth_subject) DO UPDATE SET
                        email=COALESCE(EXCLUDED.email, public.users.email),
                        display_name=COALESCE(EXCLUDED.display_name, public.users.display_name),
                        updated_at=now()
                    RETURNING id, email, display_name
                    """,
                    auth_provider, auth_subject, email, display_name,
                )
                tenant = await conn.fetchrow(
                    """
                    SELECT t.id, t.name, t.onboarding_completed
                    FROM public.tenants t
                    JOIN public.tenant_members tm ON tm.tenant_id=t.id
                    WHERE tm.user_id=$1 AND tm.role='owner' AND t.status='active'
                    ORDER BY t.created_at ASC LIMIT 1
                    """,
                    user['id'],
                )
                if tenant is None:
                    tenant = await conn.fetchrow(
                        """
                        INSERT INTO public.tenants(owner_user_id, name)
                        VALUES($1,$2)
                        RETURNING id, name, onboarding_completed
                        """,
                        user['id'], tenant_name or display_name or 'My ThreadBoss',
                    )
                    await conn.execute(
                        'INSERT INTO public.tenant_members(tenant_id,user_id,role) VALUES($1,$2,\'owner\') ON CONFLICT DO NOTHING',
                        tenant['id'], user['id'],
                    )
                    await conn.execute(
                        'INSERT INTO public.tenant_permissions(tenant_id) VALUES($1) ON CONFLICT DO NOTHING',
                        tenant['id'],
                    )
                connection = await conn.fetchrow(
                    """
                    SELECT id, waha_session_name, status, owner_jid, owner_lid
                    FROM public.whatsapp_connections
                    WHERE tenant_id=$1
                    ORDER BY created_at DESC LIMIT 1
                    """,
                    tenant['id'],
                )
        return {
            'user_id': str(user['id']),
            'tenant_id': str(tenant['id']),
            'tenant_name': tenant['name'],
            'onboarding_completed': bool(tenant['onboarding_completed']),
            'whatsapp_connection': dict(connection) if connection else None,
        }

    async def tenant_exists(self, tenant_id: str) -> bool:
        await self.ensure()
        assert self.pool
        async with self.pool.acquire() as conn:
            value = await conn.fetchval('SELECT 1 FROM public.tenants WHERE id::text=$1 AND status=\'active\'', str(tenant_id))
        return bool(value)

    async def get_tenant_permissions(self, tenant_id: str) -> dict[str, Any]:
        await self.ensure()
        assert self.pool
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT read_direct_messages, read_group_messages, process_images,
                       process_voice_notes, process_documents, create_reminders,
                       calendar_read, calendar_write, send_messages, auto_followups,
                       allow_actions, history_import_hours
                FROM public.tenant_permissions
                WHERE tenant_id::text=$1
                """,
                str(tenant_id),
            )
        if row:
            return dict(row)
        # Legacy tenant/session fallback defaults preserve V1.6 behavior.
        return {
            'read_direct_messages': True, 'read_group_messages': True,
            'process_images': True, 'process_voice_notes': True, 'process_documents': True,
            'create_reminders': True, 'calendar_read': False, 'calendar_write': False,
            'send_messages': False, 'auto_followups': False, 'allow_actions': False,
            'history_import_hours': 24,
        }

    async def update_tenant_permissions(self, tenant_id: str, changes: dict[str, Any]) -> dict[str, Any]:
        allowed = {
            'read_direct_messages', 'read_group_messages', 'process_images',
            'process_voice_notes', 'process_documents', 'create_reminders',
            'calendar_read', 'calendar_write', 'send_messages', 'auto_followups',
            'allow_actions', 'history_import_hours',
        }
        changes = {k: v for k, v in changes.items() if k in allowed and v is not None}
        if not changes:
            return await self.get_tenant_permissions(tenant_id)
        await self.ensure()
        assert self.pool
        async with self.pool.acquire() as conn:
            tenant_uuid = await conn.fetchval('SELECT id FROM public.tenants WHERE id::text=$1', str(tenant_id))
            if tenant_uuid is None:
                raise KeyError(f'Unknown tenant: {tenant_id}')
            await conn.execute(
                'INSERT INTO public.tenant_permissions(tenant_id) VALUES($1) ON CONFLICT DO NOTHING',
                tenant_uuid,
            )
            columns = list(changes)
            assignments = ', '.join(f'{name}=${i + 2}' for i, name in enumerate(columns))
            values = [changes[name] for name in columns]
            await conn.execute(
                f'UPDATE public.tenant_permissions SET {assignments}, updated_at=now() WHERE tenant_id=$1',
                tenant_uuid, *values,
            )
        return await self.get_tenant_permissions(tenant_id)

    async def tenant_for_waha_session(self, session: str) -> str | None:
        await self.ensure()
        assert self.pool
        async with self.pool.acquire() as conn:
            value = await conn.fetchval(
                """
                SELECT tenant_id::text FROM public.whatsapp_connections
                WHERE waha_session_name=$1 AND status <> 'disconnected'
                ORDER BY created_at DESC LIMIT 1
                """,
                session,
            )
        return str(value) if value else None

    async def create_whatsapp_connection(self, tenant_id: str, session_name: str) -> dict[str, Any]:
        await self.ensure()
        assert self.pool
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO public.whatsapp_connections(tenant_id, waha_session_name, status)
                SELECT id, $2, 'pending' FROM public.tenants WHERE id::text=$1
                ON CONFLICT(waha_session_name) DO UPDATE SET
                    tenant_id=EXCLUDED.tenant_id,
                    status=CASE WHEN public.whatsapp_connections.status='connected' THEN 'connected' ELSE 'pending' END,
                    updated_at=now()
                RETURNING id, tenant_id, waha_session_name, status
                """,
                str(tenant_id), session_name,
            )
            if row is None:
                raise KeyError(f'Unknown tenant: {tenant_id}')
            await conn.execute(
                """
                INSERT INTO public.onboarding_sessions(tenant_id, whatsapp_connection_id, status)
                VALUES($1,$2,'waiting_for_qr')
                """,
                row['tenant_id'], row['id'],
            )
        return {k: (str(v) if k in {'id', 'tenant_id'} else v) for k, v in dict(row).items()}

    async def latest_whatsapp_connection_for_tenant(self, tenant_id: str) -> dict[str, Any] | None:
        await self.ensure()
        assert self.pool
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT id, tenant_id, waha_session_name, status, owner_jid, owner_lid,
                       connected_at, disconnected_at, last_seen_at
                FROM public.whatsapp_connections
                WHERE tenant_id::text=$1 AND status <> 'disconnected'
                ORDER BY created_at DESC LIMIT 1
                """,
                str(tenant_id),
            )
        if not row:
            return None
        data = dict(row)
        data['id'] = str(data['id'])
        data['tenant_id'] = str(data['tenant_id'])
        return data

    async def whatsapp_connection_by_session(self, session: str) -> dict[str, Any] | None:
        await self.ensure()
        assert self.pool
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT id, tenant_id, waha_session_name, status, owner_jid, owner_lid,
                       connected_at, disconnected_at, last_seen_at
                FROM public.whatsapp_connections
                WHERE waha_session_name=$1
                ORDER BY created_at DESC LIMIT 1
                """,
                session,
            )
        if not row:
            return None
        data = dict(row)
        data['id'] = str(data['id'])
        data['tenant_id'] = str(data['tenant_id'])
        return data

    async def whatsapp_connection(self, connection_id: str) -> dict[str, Any] | None:
        await self.ensure()
        assert self.pool
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT id, tenant_id, waha_session_name, status, owner_jid, owner_lid,
                       connected_at, disconnected_at, last_seen_at
                FROM public.whatsapp_connections WHERE id::text=$1
                """,
                str(connection_id),
            )
        if not row:
            return None
        data = dict(row)
        data['id'] = str(data['id'])
        data['tenant_id'] = str(data['tenant_id'])
        return data

    async def update_whatsapp_status(
        self,
        connection_id: str,
        *,
        status: str,
        owner_jid: str | None = None,
        owner_lid: str | None = None,
    ) -> None:
        await self.ensure()
        assert self.pool
        connected = status == 'connected'
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    """
                    UPDATE public.whatsapp_connections
                    SET status=$2,
                        owner_jid=COALESCE($3, owner_jid),
                        owner_lid=COALESCE($4, owner_lid),
                        connected_at=CASE WHEN $5 THEN COALESCE(connected_at, now()) ELSE connected_at END,
                        disconnected_at=CASE WHEN $2='disconnected' THEN now() ELSE disconnected_at END,
                        last_seen_at=now(), updated_at=now()
                    WHERE id::text=$1
                    RETURNING tenant_id, id
                    """,
                    str(connection_id), status, owner_jid, owner_lid, connected,
                )
                if row and connected:
                    await conn.execute(
                        'UPDATE public.tenants SET onboarding_completed=true, updated_at=now() WHERE id=$1',
                        row['tenant_id'],
                    )
                    await conn.execute(
                        """
                        UPDATE public.onboarding_sessions
                        SET status='completed', completed_at=now()
                        WHERE whatsapp_connection_id=$1 AND completed_at IS NULL
                        """,
                        row['id'],
                    )

    async def bind_existing_waha_session(self, tenant_id: str, session: str) -> dict[str, Any]:
        return await self.create_whatsapp_connection(tenant_id, session)

    async def issue_channel_token(self, tenant_id: str, channel: str = 'any', label: str | None = None) -> str:
        if not await self.tenant_exists(tenant_id):
            raise KeyError(f'Unknown tenant: {tenant_id}')
        token = 'tbk_' + secrets.token_urlsafe(32)
        digest = hashlib.sha256(token.encode('utf-8')).hexdigest()
        await self.ensure()
        assert self.pool
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO threadboss.channel_api_tokens(tenant_id, channel, token_sha256, label)
                VALUES($1,$2,$3,$4)
                """,
                str(tenant_id), channel, digest, label,
            )
        return token

    async def tenant_for_channel_token(self, token: str, channel: str | None = None) -> str | None:
        digest = hashlib.sha256(token.encode('utf-8')).hexdigest()
        await self.ensure()
        assert self.pool
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                UPDATE threadboss.channel_api_tokens
                SET last_used_at=now()
                WHERE token_sha256=$1 AND status='active'
                  AND ($2::text IS NULL OR channel='any' OR channel=$2)
                RETURNING tenant_id
                """,
                digest, channel,
            )
        return str(row['tenant_id']) if row else None

    async def audit(
        self,
        *,
        tenant_id: str | None,
        actor_type: str,
        actor_id: str | None,
        action: str,
        resource_type: str | None = None,
        resource_id: str | None = None,
        success: bool = True,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        await self.ensure()
        assert self.pool
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO threadboss.audit_logs(
                    tenant_id, actor_type, actor_id, action, resource_type,
                    resource_id, success, metadata
                ) VALUES($1,$2,$3,$4,$5,$6,$7,$8::jsonb)
                """,
                tenant_id, actor_type, actor_id, action, resource_type,
                resource_id, success, json.dumps(metadata or {}),
            )

    # ------------------------------------------------------------------
    # Tenant-fenced memory + task APIs
    # ------------------------------------------------------------------
    async def upsert_message_detailed(self, payload: dict[str, Any], embedding: list[float] | None) -> str:
        tenant_id = str(payload['tenant_id'])
        channel = payload.get('channel', 'whatsapp')
        session_id = payload.get('session_id', '')
        message_id = payload['message_id']
        new_text = str(payload.get('text') or '').strip()
        vector = self.vector_literal(embedding) if embedding else None

        async with self.tenant_conn(tenant_id) as conn:
            existing = await conn.fetchrow(
                """
                SELECT id, chat_id, chat_type, sender_id, recipient_id, participant_id,
                       ts, text, from_me, embedding IS NOT NULL AS has_embedding, metadata
                FROM threadboss.messages
                WHERE tenant_id=$1 AND channel=$2 AND session_id=$3 AND message_id=$4
                FOR UPDATE
                """,
                tenant_id, channel, session_id, message_id,
            )

            if existing is None:
                try:
                    await conn.execute(
                        """
                        INSERT INTO threadboss.messages(
                            tenant_id, channel, session_id, chat_id, chat_type, sender_id,
                            recipient_id, participant_id, message_id, ts, text, from_me,
                            metadata, embedding
                        ) VALUES(
                            $1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13::jsonb,$14::vector
                        )
                        """,
                        tenant_id, channel, session_id, payload['chat_id'],
                        payload.get('chat_type', 'unknown'), payload.get('sender_id'),
                        payload.get('recipient_id'), payload.get('participant_id'),
                        message_id, payload['timestamp'], new_text,
                        bool(payload.get('from_me', False)),
                        json.dumps(payload.get('metadata') or {}), vector,
                    )
                    return 'inserted'
                except asyncpg.UniqueViolationError:
                    existing = await conn.fetchrow(
                        """
                        SELECT id, chat_id, chat_type, sender_id, recipient_id, participant_id,
                               ts, text, from_me, embedding IS NOT NULL AS has_embedding, metadata
                        FROM threadboss.messages
                        WHERE tenant_id=$1 AND channel=$2 AND session_id=$3 AND message_id=$4
                        FOR UPDATE
                        """,
                        tenant_id, channel, session_id, message_id,
                    )

            if existing is None:
                return 'duplicate'

            old_text = str(existing['text'] or '').strip()
            text_is_better = bool(new_text) and (
                not old_text
                or len(new_text) > len(old_text) + 20
                or ('--- Media context ---' in new_text and '--- Media context ---' not in old_text)
            )
            embedding_missing = (not bool(existing['has_embedding'])) and embedding is not None
            timestamp_changed = existing['ts'] != payload['timestamp']
            chat_changed = bool(payload.get('chat_id')) and str(existing['chat_id'] or '') != str(payload.get('chat_id') or '')

            if not (text_is_better or embedding_missing or timestamp_changed or chat_changed):
                return 'duplicate'

            final_text = new_text if text_is_better else old_text
            final_vector = vector if (embedding is not None and (text_is_better or embedding_missing)) else None
            if final_vector is not None:
                await conn.execute(
                    """
                    UPDATE threadboss.messages
                    SET chat_id=$2, chat_type=$3,
                        sender_id=COALESCE($4, sender_id),
                        recipient_id=COALESCE($5, recipient_id),
                        participant_id=COALESCE($6, participant_id),
                        ts=$7, text=$8, from_me=$9,
                        metadata=metadata || $10::jsonb,
                        embedding=$11::vector
                    WHERE id=$1
                    """,
                    existing['id'], payload['chat_id'], payload.get('chat_type', 'unknown'),
                    payload.get('sender_id'), payload.get('recipient_id'), payload.get('participant_id'),
                    payload['timestamp'], final_text, bool(payload.get('from_me', False)),
                    json.dumps(payload.get('metadata') or {}), final_vector,
                )
            else:
                await conn.execute(
                    """
                    UPDATE threadboss.messages
                    SET chat_id=$2, chat_type=$3,
                        sender_id=COALESCE($4, sender_id),
                        recipient_id=COALESCE($5, recipient_id),
                        participant_id=COALESCE($6, participant_id),
                        ts=$7, text=$8, from_me=$9,
                        metadata=metadata || $10::jsonb
                    WHERE id=$1
                    """,
                    existing['id'], payload['chat_id'], payload.get('chat_type', 'unknown'),
                    payload.get('sender_id'), payload.get('recipient_id'), payload.get('participant_id'),
                    payload['timestamp'], final_text, bool(payload.get('from_me', False)),
                    json.dumps(payload.get('metadata') or {}),
                )
            return 'repaired'

    async def upsert_message(self, payload: dict[str, Any], embedding: list[float] | None) -> bool:
        return (await self.upsert_message_detailed(payload, embedding)) in {'inserted', 'repaired'}

    async def search_messages(self, tenant_id: str, embedding: list[float], limit: int = 8) -> list[dict[str, Any]]:
        vector = self.vector_literal(embedding)
        async with self.tenant_conn(tenant_id) as conn:
            rows = await conn.fetch(
                """
                SELECT channel, session_id, chat_id, sender_id, message_id, ts, text, from_me,
                       1 - (embedding <=> $2::vector) AS similarity
                FROM threadboss.messages
                WHERE tenant_id=$1 AND embedding IS NOT NULL AND text <> ''
                ORDER BY embedding <=> $2::vector
                LIMIT $3
                """,
                str(tenant_id), vector, limit,
            )
        return [dict(r) for r in rows]

    async def recent_messages(self, tenant_id: str, limit: int = 20) -> list[dict[str, Any]]:
        async with self.tenant_conn(tenant_id) as conn:
            rows = await conn.fetch(
                """
                SELECT channel, chat_id, sender_id, message_id, ts, text, from_me
                FROM threadboss.messages WHERE tenant_id=$1 AND text <> ''
                ORDER BY ts DESC LIMIT $2
                """,
                str(tenant_id), limit,
            )
        return [dict(r) for r in rows]

    async def messages_in_range(
        self,
        tenant_id: str,
        start: datetime,
        end: datetime,
        *,
        received_only: bool = False,
        limit: int = 120,
    ) -> list[dict[str, Any]]:
        async with self.tenant_conn(tenant_id) as conn:
            rows = await conn.fetch(
                """
                SELECT channel, session_id, chat_id, sender_id, message_id, ts, text, from_me
                FROM threadboss.messages
                WHERE tenant_id=$1
                  AND ts >= $2 AND ts < $3
                  AND text <> ''
                  AND ($4::boolean = FALSE OR from_me = FALSE)
                ORDER BY ts DESC LIMIT $5
                """,
                str(tenant_id), start, end, received_only, limit,
            )
        return [dict(r) for r in rows]

    async def count_messages_in_range(
        self,
        tenant_id: str,
        start: datetime,
        end: datetime,
        *,
        received_only: bool = False,
    ) -> int:
        async with self.tenant_conn(tenant_id) as conn:
            value = await conn.fetchval(
                """
                SELECT count(*)::int FROM threadboss.messages
                WHERE tenant_id=$1 AND ts >= $2 AND ts < $3 AND text <> ''
                  AND ($4::boolean = FALSE OR from_me = FALSE)
                """,
                str(tenant_id), start, end, received_only,
            )
        return int(value or 0)

    async def memory_stats(self, tenant_id: str) -> dict[str, Any]:
        async with self.tenant_conn(tenant_id) as conn:
            row = await conn.fetchrow(
                """
                SELECT count(*)::int AS total,
                       count(*) FILTER (WHERE text <> '')::int AS with_text,
                       count(*) FILTER (WHERE embedding IS NOT NULL)::int AS with_embedding,
                       count(*) FILTER (WHERE from_me = FALSE)::int AS received,
                       min(ts) AS earliest, max(ts) AS latest
                FROM threadboss.messages WHERE tenant_id=$1
                """,
                str(tenant_id),
            )
        return dict(row) if row else {
            'total': 0, 'with_text': 0, 'with_embedding': 0,
            'received': 0, 'earliest': None, 'latest': None,
        }

    async def create_task(
        self,
        *,
        tenant_id: str,
        channel: str,
        title: str,
        owner: str | None,
        due_at: datetime | None,
        source_message_id: str | None,
        source_chat_id: str | None,
        evidence: str | None,
        confidence: float = 0.5,
    ) -> int:
        async with self.tenant_conn(tenant_id) as conn:
            if source_message_id:
                existing = await conn.fetchval(
                    """
                    SELECT id FROM threadboss.tasks
                    WHERE tenant_id=$1 AND channel=$2 AND source_message_id=$3 AND title=$4
                    LIMIT 1
                    """,
                    str(tenant_id), channel, source_message_id, title,
                )
                if existing:
                    return int(existing)
            value = await conn.fetchval(
                """
                INSERT INTO threadboss.tasks(
                    tenant_id, channel, title, owner, due_at, source_message_id,
                    source_chat_id, evidence, confidence
                ) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9) RETURNING id
                """,
                str(tenant_id), channel, title, owner, due_at, source_message_id,
                source_chat_id, evidence, confidence,
            )
        return int(value)

    async def list_tasks(self, tenant_id: str, *, status: str = 'open', limit: int = 30) -> list[dict[str, Any]]:
        async with self.tenant_conn(tenant_id) as conn:
            rows = await conn.fetch(
                """
                SELECT id, title, owner, due_at, status, source_chat_id, evidence, confidence, created_at
                FROM threadboss.tasks WHERE tenant_id=$1 AND status=$2
                ORDER BY due_at NULLS LAST, created_at DESC LIMIT $3
                """,
                str(tenant_id), status, limit,
            )
        return [dict(r) for r in rows]

    async def task_stats(self, tenant_id: str) -> dict[str, int]:
        async with self.tenant_conn(tenant_id) as conn:
            rows = await conn.fetch(
                'SELECT status, count(*)::int AS count FROM threadboss.tasks WHERE tenant_id=$1 GROUP BY status',
                str(tenant_id),
            )
        return {r['status']: r['count'] for r in rows}
