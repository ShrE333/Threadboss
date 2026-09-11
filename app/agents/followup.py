from __future__ import annotations

from datetime import datetime, timezone

from .types import AgentAnswer
from ..config import Settings
from ..db import Database


class FollowupAgent:
    def __init__(self, settings: Settings, db: Database):
        self.settings = settings
        self.db = db

    async def answer(self, tenant_id: str) -> AgentAnswer:
        tasks = await self.db.list_tasks(tenant_id)
        now = datetime.now(timezone.utc)
        overdue = [t for t in tasks if t.get('due_at') and t['due_at'] < now]
        undated = [t for t in tasks if not t.get('due_at')]
        future = [t for t in tasks if t.get('due_at') and t['due_at'] >= now]
        lines = []
        if overdue:
            lines.append('⚠️ Overdue:')
            for t in overdue[:10]:
                lines.append(f"• #{t['id']} {t['title']} — due {t['due_at'].isoformat()}")
        if future:
            lines.append('\nUpcoming:')
            for t in future[:10]:
                lines.append(f"• #{t['id']} {t['title']} — {t['due_at'].isoformat()}")
        if undated:
            lines.append('\nNo deadline:')
            for t in undated[:8]:
                lines.append(f"• #{t['id']} {t['title']}")
        if not lines:
            return AgentAnswer(agent='followup', answer='No follow-ups are pending.')
        return AgentAnswer(agent='followup', answer='\n'.join(lines))
