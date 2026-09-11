from __future__ import annotations

from .types import AgentAnswer


class ActionAgent:
    """Safe action planner.

    V1.3 plans outbound actions but does not silently execute arbitrary actions.
    WhatsApp deterministic sends can be added behind an explicit confirmation token later.
    """

    async def answer(self, question: str) -> AgentAnswer:
        return AgentAnswer(
            agent='action',
            answer=(
                'Action Agent is enabled in safe planning mode. I can prepare an action, but V1.3 will not send '
                'messages/book/pay anything without an explicit confirmation flow. Tell me the exact action you want.'
            ),
        )
