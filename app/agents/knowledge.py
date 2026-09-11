from __future__ import annotations

from .types import AgentAnswer
from ..ai import AIClient, AIUnavailable
from ..config import Settings
from ..db import Database


class KnowledgeAgent:
    def __init__(self, settings: Settings, db: Database, ai: AIClient):
        self.settings = settings
        self.db = db
        self.ai = ai

    async def ingest(self, payload: dict) -> bool:
        text = (payload.get('text') or '').strip()
        embedding = None
        if text and len(text) >= self.settings.min_embed_chars:
            try:
                embedding = await self.ai.embed(text, task_type='RETRIEVAL_DOCUMENT')
            except AIUnavailable:
                # Preserve raw memory even if the free embedding API is temporarily rate-limited.
                embedding = None
        return await self.db.upsert_message(payload, embedding)

    async def answer(self, tenant_id: str, question: str) -> AgentAnswer:
        try:
            qvec = await self.ai.embed(question, task_type='RETRIEVAL_QUERY')
            hits = await self.db.search_messages(tenant_id, qvec, self.settings.knowledge_top_k)
        except AIUnavailable:
            hits = await self.db.recent_messages(tenant_id, min(self.settings.knowledge_top_k, 12))

        if not hits:
            return AgentAnswer(agent='knowledge', answer='I do not have enough indexed chat history to answer that yet.', sources=[])

        evidence_lines = []
        sources = []
        for i, hit in enumerate(hits, 1):
            ts = hit.get('ts')
            stamp = ts.isoformat() if hasattr(ts, 'isoformat') else str(ts)
            evidence_lines.append(
                f'[{i}] channel={hit.get("channel")} chat={hit.get("chat_id")} sender={hit.get("sender_id")} '
                f'date={stamp}\n{hit.get("text", "")}'
            )
            sources.append({
                'message_id': hit.get('message_id'),
                'chat_id': hit.get('chat_id'),
                'sender_id': hit.get('sender_id'),
                'timestamp': stamp,
                'similarity': float(hit.get('similarity', 0) or 0),
            })

        system = (
            'You are ThreadBoss Knowledge Agent. Answer ONLY from the supplied chat evidence. '
            'Never invent facts. If evidence is insufficient, say that clearly. Mention sender/date when useful. '
            'Keep the answer concise and practical.'
        )
        user = f'Question:\n{question}\n\nRetrieved chat evidence:\n' + '\n\n'.join(evidence_lines)
        try:
            response = await self.ai.chat(
                provider=self.settings.knowledge_provider,
                model=self.settings.knowledge_model,
                system=system,
                user=user,
                temperature=0.1,
                max_tokens=900,
            )
            answer = response.text
        except AIUnavailable:
            # Graceful free-tier fallback: return evidence rather than failing completely.
            first = hits[0]
            answer = f'I found this relevant message: “{first.get("text", "")[:700]}”'
        return AgentAnswer(agent='knowledge', answer=answer, sources=sources)
