from __future__ import annotations

import asyncio
import json
from datetime import datetime
from typing import Any

import asyncpg

from .config import Settings


class Database:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.pool: asyncpg.Pool | None = None
        self._init_lock = asyncio.Lock()
        self._initialized = False

    async def ensure(self) -> None:
        if self._initialized:
            return
        async with self._init_lock:
            if self._initialized:
                return
            self.pool = await asyncpg.create_pool(self.settings.database_url, min_size=1, max_size=5)
            async with self.pool.acquire() as conn:
                await conn.execute('CREATE EXTENSION IF NOT EXISTS vector')
                await conn.execute(f"""
                    CREATE TABLE IF NOT EXISTS messages (
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
                await conn.execute('CREATE INDEX IF NOT EXISTS idx_messages_tenant_ts ON messages(tenant_id, ts DESC)')
                await conn.execute('CREATE INDEX IF NOT EXISTS idx_messages_chat ON messages(tenant_id, channel, chat_id, ts DESC)')
                try:
                    await conn.execute(
                        'CREATE INDEX IF NOT EXISTS idx_messages_embedding '
                        'ON messages USING hnsw (embedding vector_cosine_ops)'
                    )
                except Exception:
                    pass
                await conn.execute("""
                    CREATE TABLE IF NOT EXISTS tasks (
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
                await conn.execute('CREATE INDEX IF NOT EXISTS idx_tasks_tenant_status_due ON tasks(tenant_id, status, due_at)')
                await conn.execute(
                    'CREATE INDEX IF NOT EXISTS idx_tasks_source_message '
                    'ON tasks(tenant_id, channel, source_message_id)'
                )
            self._initialized = True

    @staticmethod
    def vector_literal(values: list[float]) -> str:
        return '[' + ','.join(f'{v:.8f}' for v in values) + ']'

    async def upsert_message_detailed(self, payload: dict[str, Any], embedding: list[float] | None) -> str:
        await self.ensure()
        assert self.pool

        tenant_id = payload['tenant_id']
        channel = payload.get('channel', 'whatsapp')
        session_id = payload.get('session_id', '')
        message_id = payload['message_id']
        new_text = str(payload.get('text') or '').strip()
        vector = self.vector_literal(embedding) if embedding else None

        async with self.pool.acquire() as conn:
            async with conn.transaction():
                existing = await conn.fetchrow(
                    """
                    SELECT id, chat_id, chat_type, sender_id, recipient_id, participant_id,
                           ts, text, from_me, embedding IS NOT NULL AS has_embedding, metadata
                    FROM messages
                    WHERE tenant_id=$1 AND channel=$2 AND session_id=$3 AND message_id=$4
                    FOR UPDATE
                    """,
                    tenant_id, channel, session_id, message_id,
                )

                if existing is None:
                    try:
                        await conn.execute(
                            """
                            INSERT INTO messages(
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
                            FROM messages
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
                chat_changed = (
                    bool(payload.get('chat_id'))
                    and str(existing['chat_id'] or '') != str(payload.get('chat_id') or '')
                )

                if not (text_is_better or embedding_missing or timestamp_changed or chat_changed):
                    return 'duplicate'

                final_text = new_text if text_is_better else old_text
                final_vector = vector if (embedding is not None and (text_is_better or embedding_missing)) else None

                if final_vector is not None:
                    await conn.execute(
                        """
                        UPDATE messages
                        SET chat_id=$2,
                            chat_type=$3,
                            sender_id=COALESCE($4, sender_id),
                            recipient_id=COALESCE($5, recipient_id),
                            participant_id=COALESCE($6, participant_id),
                            ts=$7,
                            text=$8,
                            from_me=$9,
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
                        UPDATE messages
                        SET chat_id=$2,
                            chat_type=$3,
                            sender_id=COALESCE($4, sender_id),
                            recipient_id=COALESCE($5, recipient_id),
                            participant_id=COALESCE($6, participant_id),
                            ts=$7,
                            text=$8,
                            from_me=$9,
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
        await self.ensure()
        assert self.pool
        vector = self.vector_literal(embedding)
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT channel, session_id, chat_id, sender_id, message_id, ts, text, from_me,
                       1 - (embedding <=> $2::vector) AS similarity
                FROM messages
                WHERE tenant_id = $1 AND embedding IS NOT NULL AND text <> ''
                ORDER BY embedding <=> $2::vector
                LIMIT $3
                """,
                tenant_id, vector, limit,
            )
        return [dict(r) for r in rows]

    async def recent_messages(self, tenant_id: str, limit: int = 20) -> list[dict[str, Any]]:
        await self.ensure()
        assert self.pool
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT channel, chat_id, sender_id, message_id, ts, text, from_me
                FROM messages WHERE tenant_id=$1 AND text <> ''
                ORDER BY ts DESC LIMIT $2
                """,
                tenant_id, limit,
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
        await self.ensure()
        assert self.pool
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT channel, session_id, chat_id, sender_id, message_id, ts, text, from_me
                FROM messages
                WHERE tenant_id=$1
                  AND ts >= $2 AND ts < $3
                  AND text <> ''
                  AND ($4::boolean = FALSE OR from_me = FALSE)
                ORDER BY ts DESC
                LIMIT $5
                """,
                tenant_id, start, end, received_only, limit,
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
        await self.ensure()
        assert self.pool
        async with self.pool.acquire() as conn:
            value = await conn.fetchval(
                """
                SELECT count(*)::int
                FROM messages
                WHERE tenant_id=$1
                  AND ts >= $2 AND ts < $3
                  AND text <> ''
                  AND ($4::boolean = FALSE OR from_me = FALSE)
                """,
                tenant_id, start, end, received_only,
            )
        return int(value or 0)

    async def memory_stats(self, tenant_id: str) -> dict[str, Any]:
        await self.ensure()
        assert self.pool
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT
                    count(*)::int AS total,
                    count(*) FILTER (WHERE text <> '')::int AS with_text,
                    count(*) FILTER (WHERE embedding IS NOT NULL)::int AS with_embedding,
                    count(*) FILTER (WHERE from_me = FALSE)::int AS received,
                    min(ts) AS earliest,
                    max(ts) AS latest
                FROM messages
                WHERE tenant_id=$1
                """,
                tenant_id,
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
        await self.ensure()
        assert self.pool
        async with self.pool.acquire() as conn:
            if source_message_id:
                existing = await conn.fetchval(
                    """
                    SELECT id FROM tasks
                    WHERE tenant_id=$1 AND channel=$2 AND source_message_id=$3 AND title=$4
                    LIMIT 1
                    """,
                    tenant_id, channel, source_message_id, title,
                )
                if existing:
                    return int(existing)
            value = await conn.fetchval(
                """
                INSERT INTO tasks(
                    tenant_id, channel, title, owner, due_at, source_message_id,
                    source_chat_id, evidence, confidence
                )
                VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9)
                RETURNING id
                """,
                tenant_id, channel, title, owner, due_at, source_message_id,
                source_chat_id, evidence, confidence,
            )
        return int(value)

    async def list_tasks(self, tenant_id: str, *, status: str = 'open', limit: int = 30) -> list[dict[str, Any]]:
        await self.ensure()
        assert self.pool
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, title, owner, due_at, status, source_chat_id, evidence, confidence, created_at
                FROM tasks WHERE tenant_id=$1 AND status=$2
                ORDER BY due_at NULLS LAST, created_at DESC LIMIT $3
                """,
                tenant_id, status, limit,
            )
        return [dict(r) for r in rows]

    async def task_stats(self, tenant_id: str) -> dict[str, int]:
        await self.ensure()
        assert self.pool
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                'SELECT status, count(*)::int AS count FROM tasks WHERE tenant_id=$1 GROUP BY status',
                tenant_id,
            )
        return {r['status']: r['count'] for r in rows}
