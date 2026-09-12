from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .types import AgentAnswer
from ..ai import AIClient, AIUnavailable
from ..config import Settings
from ..db import Database


class KnowledgeAgent:
    def __init__(self, settings: Settings, db: Database, ai: AIClient):
        self.settings = settings
        self.db = db
        self.ai = ai

    async def ingest_detailed(self, payload: dict) -> str:
        raw_text = payload.get('text') or ''
        text = raw_text.strip()
        payload = dict(payload)
        payload['text'] = text

        embedding = None
        if text and len(text) >= self.settings.min_embed_chars:
            try:
                embedding = await self.ai.embed(text, task_type='RETRIEVAL_DOCUMENT')
            except AIUnavailable:
                embedding = None
        return await self.db.upsert_message_detailed(payload, embedding)

    async def ingest(self, payload: dict) -> bool:
        return (await self.ingest_detailed(payload)) in {'inserted', 'repaired'}

    def temporal_window(self, question: str) -> tuple[datetime, datetime] | None:
        q = question.lower()
        try:
            tz = ZoneInfo(self.settings.default_timezone)
        except Exception:
            tz = timezone.utc

        now = datetime.now(tz)
        today = now.replace(hour=0, minute=0, second=0, microsecond=0)

        if 'yesterday' in q:
            start = today - timedelta(days=1)
            end = today
        elif re.search(r'\btoday\b', q):
            start = today
            end = min(today + timedelta(days=1), now + timedelta(seconds=1))
        elif 'this week' in q:
            start = today - timedelta(days=today.weekday())
            end = now + timedelta(seconds=1)
        else:
            match = re.search(r'\b(?:last|past)\s+(\d{1,3})\s+days?\b', q)
            if not match:
                return None
            days = max(1, min(int(match.group(1)), 90))
            start = now - timedelta(days=days)
            end = now + timedelta(seconds=1)

        return start.astimezone(timezone.utc), end.astimezone(timezone.utc)

    @staticmethod
    def received_only(question: str) -> bool:
        q = question.lower()
        return bool(re.search(r'\b(receive|received|got|sent to me|messages to me|incoming)\b', q))

    @staticmethod
    def _format_hit(hit: dict, index: int) -> tuple[str, dict]:
        ts = hit.get('ts')
        stamp = ts.isoformat() if hasattr(ts, 'isoformat') else str(ts)
        text = str(hit.get('text') or '').strip()
        sender = hit.get('sender_id') or 'unknown'
        line = (
            f'[{index}] channel={hit.get("channel")} chat={hit.get("chat_id")} '
            f'sender={sender} date={stamp} from_me={bool(hit.get("from_me"))}\n{text}'
        )
        source = {
            'message_id': hit.get('message_id'),
            'chat_id': hit.get('chat_id'),
            'sender_id': hit.get('sender_id'),
            'timestamp': stamp,
            'similarity': float(hit.get('similarity', 0) or 0),
        }
        return line, source

    async def answer(self, tenant_id: str, question: str) -> AgentAnswer:
        temporal = self.temporal_window(question)
        received_only = self.received_only(question)

        if temporal:
            start, end = temporal
            hits = await self.db.messages_in_range(
                tenant_id,
                start,
                end,
                received_only=received_only,
                limit=self.settings.knowledge_summary_limit,
            )
        else:
            try:
                qvec = await self.ai.embed(question, task_type='RETRIEVAL_QUERY')
                hits = await self.db.search_messages(tenant_id, qvec, self.settings.knowledge_top_k)
            except AIUnavailable:
                hits = await self.db.recent_messages(tenant_id, min(self.settings.knowledge_top_k, 12))

        hits = [h for h in hits if str(h.get('text') or '').strip()]
        if not hits:
            if temporal:
                return AgentAnswer(
                    agent='knowledge',
                    answer=(
                        'I could not find indexed messages in that time range. '
                        'ThreadBoss V1.7 normally auto-syncs temporal questions; '
                        'use `/memory stats` to inspect the local memory range.'
                    ),
                    sources=[],
                )
            return AgentAnswer(agent='knowledge', answer='I do not have enough indexed chat history to answer that yet.', sources=[])

        evidence_lines: list[str] = []
        sources: list[dict] = []
        for i, hit in enumerate(hits, 1):
            line, source = self._format_hit(hit, i)
            evidence_lines.append(line)
            sources.append(source)

        if temporal:
            system = (
                'You are ThreadBoss Knowledge Agent. The user asked about a time range. '
                'Use ONLY the supplied messages. If they ask what is important, select genuinely actionable or '
                'high-value items such as deadlines, meetings, requests, money, travel, commitments, urgent changes, '
                'or concrete information they may need. Do not call casual chatter important. '
                'If there are no important items, say so. Never invent. Include sender and date/time when useful. '
                'Keep the answer concise and use bullets for multiple messages.'
            )
        else:
            system = (
                'You are ThreadBoss Knowledge Agent. Answer ONLY from the supplied chat evidence. '
                'Never invent facts. If evidence is insufficient, say that clearly. Mention sender/date when useful. '
                'For images/documents, extracted OCR/transcript text is evidence too. Keep the answer concise and practical.'
            )

        user = f'Question:\n{question}\n\nRetrieved chat evidence:\n' + '\n\n'.join(evidence_lines)
        try:
            response = await self.ai.chat(
                provider=self.settings.knowledge_provider,
                model=self.settings.knowledge_model,
                system=system,
                user=user,
                temperature=0.1,
                max_tokens=1200,
            )
            answer = response.text
        except AIUnavailable:
            selected = hits[:5]
            lines = ['I found these relevant indexed messages:']
            for hit in selected:
                ts = hit.get('ts')
                stamp = ts.isoformat() if hasattr(ts, 'isoformat') else str(ts)
                sender = hit.get('sender_id') or 'unknown'
                snippet = str(hit.get('text') or '').strip().replace('\n', ' ')
                if len(snippet) > 350:
                    snippet = snippet[:347] + '...'
                lines.append(f'• {sender} — {stamp}: {snippet}')
            answer = '\n'.join(lines)

        return AgentAnswer(agent='knowledge', answer=answer, sources=sources)
