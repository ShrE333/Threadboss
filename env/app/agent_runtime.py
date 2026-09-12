from __future__ import annotations

from .ai import AIClient
from .agents import ActionAgent, AgentOrchestrator, FollowupAgent, KnowledgeAgent, PlannerAgent
from .config import Settings
from .db import Database


class AgentRuntime:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.db = Database(settings)
        self.ai = AIClient(settings)
        self.knowledge = KnowledgeAgent(settings, self.db, self.ai)
        self.planner = PlannerAgent(settings, self.db, self.ai)
        self.followup = FollowupAgent(settings, self.db)
        self.action = ActionAgent()
        self.orchestrator = AgentOrchestrator(
            settings, self.knowledge, self.planner, self.followup, self.action, self.ai
        )

    async def ingest_detailed(self, payload: dict) -> str:
        status = await self.knowledge.ingest_detailed(payload)
        if status in {'inserted', 'repaired'} and self.settings.enable_planner_extraction:
            await self.planner.inspect_message(payload)
        return status

    async def ingest(self, payload: dict) -> bool:
        return (await self.ingest_detailed(payload)) in {'inserted', 'repaired'}

    async def query(self, tenant_id: str, question: str):
        return await self.orchestrator.route(tenant_id, question)
