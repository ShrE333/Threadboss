from __future__ import annotations

import logging

from .agent_runtime import AgentRuntime
from .config import Settings
from .media_processor import MediaProcessingError, MediaProcessor
from .models import NormalizedMessage
from .redis_bus import EventBus
from .tools import TOOLS_HELP, ToolEngine
from .waha import WahaClient

logger = logging.getLogger(__name__)


HELP_TEXT = """🧠 ThreadBoss V1.3

Message Yourself is your private ThreadBoss control plane.

Agents:
• Knowledge — searches your indexed chats
• Planner — extracts commitments/tasks
• Follow-up — shows overdue/upcoming commitments
• Action — safe action planning (confirmation-first)
• Tool Engine — PDF/image/OCR/voice utilities

Commands:
/help — this help
/status — backend status
/agents — agent/model configuration
/tasks — open tasks
/followups — pending/overdue items
/memory <question> — force Knowledge Agent
/tools — utility commands

Normal chats/groups are indexed silently. ThreadBoss replies only here.
""".strip()


class SelfChatRouter:
    def __init__(self, settings: Settings, bus: EventBus, waha: WahaClient,
                 runtime: AgentRuntime, media: MediaProcessor):
        self.settings = settings
        self.bus = bus
        self.waha = waha
        self.runtime = runtime
        self.media = media
        self.tools = ToolEngine(settings, bus, waha, media)

    async def handle(self, message: NormalizedMessage) -> None:
        body = message.body.strip()
        lowered = body.lower()

        if lowered in {'/help', 'help', 'threadboss help'}:
            await self._reply(message, HELP_TEXT)
            return
        if lowered in {'/status', 'status', 'threadboss status'}:
            stats = await self.runtime.db.task_stats(message.tenant_id)
            await self._reply(
                message,
                '✅ ThreadBoss V1.3 is online.\n'
                f'Session: {message.session_id}\nTenant: {message.tenant_id}\n'
                f'Knowledge: {self.settings.knowledge_provider}/{self.settings.knowledge_model}\n'
                f'Embedding: {self.settings.embedding_provider}/{self.settings.embedding_model}\n'
                f'Planner extraction: {"on" if self.settings.enable_planner_extraction else "off"}\n'
                f'Open tasks: {stats.get("open", 0)}\n'
                f'Local STT: {"on" if self.settings.enable_local_stt else "off"}\nTool Engine: on',
            )
            return
        if lowered in {'/agents', 'agents'}:
            await self._reply(
                message,
                '🤖 Agents\n'
                f'Knowledge: {self.settings.knowledge_provider} / {self.settings.knowledge_model}\n'
                f'Planner: {self.settings.planner_provider} / {self.settings.planner_model}\n'
                f'Follow-up: {self.settings.followup_provider} / {self.settings.followup_model}\n'
                f'Action: {self.settings.action_provider} / {self.settings.action_model}\n'
                f'Router: {self.settings.router_provider} / {self.settings.router_model} '
                f'({"AI" if self.settings.enable_ai_router else "deterministic"})\n'
                f'Embeddings: {self.settings.embedding_provider} / {self.settings.embedding_model}',
            )
            return

        try:
            tool_result = await self.tools.handle(message)
        except MediaProcessingError as exc:
            await self._reply(message, f'⚠️ Tool failed: {exc}')
            return
        except Exception as exc:
            logger.exception('Tool execution failed')
            await self._reply(message, f'⚠️ Tool execution failed: {exc}')
            return
        if tool_result.handled:
            if tool_result.text:
                await self._reply(message, tool_result.text)
            return

        media_context = ''
        if message.has_media and message.media:
            try:
                media_context = await self.media.context_for_media(message.media, body)
            except MediaProcessingError as exc:
                await self._reply(message, f'⚠️ Attachment processing failed: {exc}')
                return

        if not body and not media_context:
            return
        if body and media_context:
            question = f'{body}\n\n--- Attachment context ---\n{media_context}'
        elif media_context:
            question = f'Use this attachment content and respond helpfully:\n\n{media_context}'
        else:
            question = body

        result = await self.runtime.query(message.tenant_id, question)
        await self._reply(message, result.answer.strip() or 'I could not produce an answer.')

    async def _reply(self, message: NormalizedMessage, text: str) -> None:
        await self.waha.send_text(message.session_id, message.chat_id, text)
