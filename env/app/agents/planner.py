from __future__ import annotations

from datetime import datetime
import re

from dateutil import parser as dateparser

from .types import AgentAnswer
from ..ai import AIClient, AIUnavailable
from ..config import Settings
from ..db import Database


class PlannerAgent:
    def __init__(self, settings: Settings, db: Database, ai: AIClient):
        self.settings = settings
        self.db = db
        self.ai = ai

    async def inspect_message(self, payload: dict) -> None:
        text = (payload.get('text') or '').strip()
        if len(text) < 8:
            return
        # Free-tier quota saver: only invoke the planner model for messages that look actionable.
        actionable = re.search(
            r'\b(send|submit|finish|complete|pay|call|book|schedule|meet|meeting|deadline|due|remind|tomorrow|today|friday|monday|tuesday|wednesday|thursday|saturday|sunday|by\s+\d|before|after)\b',
            text, flags=re.I,
        )
        if not actionable:
            return
        system = (
            'You extract actionable commitments from chat messages. Return JSON only. '
            'Schema: {"tasks":[{"title":"...","owner":null,"due_at":null,"confidence":0.0}]}. '
            'Only create a task when the message clearly contains a commitment, request, deadline, payment, meeting, '
            'or action that someone needs to do. due_at must be ISO-8601 if explicitly inferable; otherwise null. '
            'If nothing actionable, return {"tasks":[]}.'
        )
        user = f'Message timestamp: {payload.get("timestamp")}\nSender: {payload.get("sender_id")}\nMessage: {text}'
        try:
            data = await self.ai.chat_json(
                provider=self.settings.planner_provider,
                model=self.settings.planner_model,
                system=system,
                user=user,
                temperature=0.0,
                max_tokens=500,
            )
        except AIUnavailable:
            return
        for task in (data or {}).get('tasks', [])[:4]:
            title = str(task.get('title') or '').strip()
            if not title:
                continue
            due_at = None
            if task.get('due_at'):
                try:
                    due_at = dateparser.isoparse(str(task['due_at']))
                except Exception:
                    due_at = None
            await self.db.create_task(
                tenant_id=payload['tenant_id'],
                channel=payload.get('channel', 'whatsapp'),
                title=title,
                owner=task.get('owner'),
                due_at=due_at,
                source_message_id=payload.get('message_id'),
                source_chat_id=payload.get('chat_id'),
                evidence=text[:1500],
                confidence=float(task.get('confidence') or 0.6),
            )

    async def answer(self, tenant_id: str, question: str) -> AgentAnswer:
        tasks = await self.db.list_tasks(tenant_id)
        if not tasks:
            return AgentAnswer(agent='planner', answer='You have no open tasks extracted yet.')
        lines = []
        for t in tasks[:20]:
            due = t['due_at'].isoformat() if t.get('due_at') else 'no deadline'
            lines.append(f"• #{t['id']} {t['title']} — {due}")
        return AgentAnswer(agent='planner', answer='Open tasks:\n' + '\n'.join(lines))
