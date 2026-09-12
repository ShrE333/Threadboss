from __future__ import annotations

from .action import ActionAgent
from .followup import FollowupAgent
from .knowledge import KnowledgeAgent
from .planner import PlannerAgent
from .types import AgentAnswer
from ..ai import AIClient, AIUnavailable
from ..config import Settings


class AgentOrchestrator:
    def __init__(self, settings: Settings, knowledge: KnowledgeAgent, planner: PlannerAgent,
                 followup: FollowupAgent, action: ActionAgent, ai: AIClient):
        self.settings = settings
        self.knowledge = knowledge
        self.planner = planner
        self.followup = followup
        self.action = action
        self.ai = ai

    async def route(self, tenant_id: str, question: str) -> AgentAnswer:
        q = question.strip()
        low = q.lower()
        if low.startswith('/tasks') or low.startswith('/today') or 'my tasks' in low or 'todo' in low:
            return await self.planner.answer(tenant_id, q)
        if low.startswith('/followups') or 'overdue' in low or 'follow up' in low:
            return await self.followup.answer(tenant_id)
        if low.startswith('/action') or low.startswith('/send ') or low.startswith('send a message'):
            return await self.action.answer(q)
        if low.startswith('/memory') or low.startswith('/ask'):
            return await self.knowledge.answer(tenant_id, q.split(' ', 1)[1] if ' ' in q else q)

        # Default to knowledge to minimize free-tier router calls. Optional AI router can be enabled.
        if not self.settings.enable_ai_router:
            return await self.knowledge.answer(tenant_id, q)
        try:
            data = await self.ai.chat_json(
                provider=self.settings.router_provider,
                model=self.settings.router_model,
                system=(
                    'Route the user request to exactly one agent. Return JSON only: '
                    '{"agent":"knowledge|planner|followup|action"}. '
                    'knowledge=questions about past chat facts; planner=tasks/schedule; '
                    'followup=overdue/pending commitments; action=doing/sending something.'
                ),
                user=q,
                temperature=0.0,
                max_tokens=80,
            )
            route = str((data or {}).get('agent') or 'knowledge').lower()
        except AIUnavailable:
            route = 'knowledge'
        if route == 'planner':
            return await self.planner.answer(tenant_id, q)
        if route == 'followup':
            return await self.followup.answer(tenant_id)
        if route == 'action':
            return await self.action.answer(q)
        return await self.knowledge.answer(tenant_id, q)
