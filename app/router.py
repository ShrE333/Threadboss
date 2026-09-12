from __future__ import annotations

import logging
import re

from .agent_runtime import AgentRuntime
from .config import Settings
from .history_sync import HistorySyncService
from .media_processor import MediaProcessingError, MediaProcessor
from .menu import InteractiveMenu
from .models import NormalizedMessage
from .redis_bus import EventBus
from .tools import TOOLS_HELP, ToolEngine
from .waha import WahaClient

logger = logging.getLogger(__name__)


HELP_TEXT = """🧠 ThreadBoss V1.5

Type *hi* or *menu* to open the native WhatsApp menu.

Quick commands still work:
/help — this help
/status — backend status
/agents — open Agents menu
/tasks — open tasks
/followups — pending/overdue items
/memory <question> — force Knowledge Agent
/tools — open Tools menu
/sync 24h — backfill recent WhatsApp history
/sync 7d — backfill the last 7 days

Normal chats/groups are indexed silently. ThreadBoss replies only in Message Yourself.
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
        self.menu = InteractiveMenu(settings, bus, waha)
        self.history = HistorySyncService(settings, waha, runtime, media)

    async def handle(self, message: NormalizedMessage) -> None:
        body = message.body.strip()
        lowered = body.lower()

        if lowered in {'hi', 'hii', 'hiii', 'hello', 'hey', 'start', '/start', 'menu', '/menu', 'home'}:
            await self.menu.show_home(message)
            return

        choice = await self.menu.resolve(message)
        if choice and await self._handle_menu_choice(message, choice):
            return

        if lowered in {'/help', 'help', 'threadboss help'}:
            await self._reply(message, HELP_TEXT)
            return
        if lowered in {'/status', 'status', 'threadboss status'}:
            stats = await self.runtime.db.task_stats(message.tenant_id)
            await self._reply(
                message,
                '✅ ThreadBoss V1.5 is online.\n'
                f'Session: {message.session_id}\nTenant: {message.tenant_id}\n'
                f'Knowledge: {self.settings.knowledge_provider}/{self.settings.knowledge_model}\n'
                f'Embedding: {self.settings.embedding_provider}/{self.settings.embedding_model}\n'
                f'Planner extraction: {"on" if self.settings.enable_planner_extraction else "off"}\n'
                f'Open tasks: {stats.get("open", 0)}\n'
                f'Interactive menu: {"on" if self.settings.interactive_menu_enabled else "off"}\n'
                f'Local STT: {"on" if self.settings.enable_local_stt else "off"}\nTool Engine: on',
            )
            return
        if lowered in {'/agents', 'agents'}:
            await self.menu.show_agents(message)
            return
        if lowered in {'/tools', 'tools', 'threadboss tools'}:
            await self.menu.show_tools(message)
            return

        if lowered.startswith('/sync') or lowered.startswith('sync '):
            hours = self._parse_sync_hours(lowered)
            await self._reply(message, f'🔄 Syncing the last {self._human_hours(hours)} of WhatsApp history…')
            result = await self.history.sync(
                session=message.session_id,
                tenant_id=message.tenant_id,
                owner_id=message.owner_id,
                owner_lid=message.owner_lid,
                hours=hours,
            )
            await self._reply(
                message,
                '✅ Memory sync complete.\n'
                f'Fetched: {result["fetched"]}\n'
                f'New memories: {result["ingested"]}\n'
                f'Media extracted: {result["media_enriched"]}',
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
        elif await self._should_use_recent_attachment(message, body):
            refs = await self.bus.recent_attachments(message.session_id, message.chat_id)
            if refs:
                try:
                    media_context = await self.media.context_for_media(refs[-1], body)
                except MediaProcessingError as exc:
                    logger.warning('Recent attachment context failed: %s', exc)

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

    async def _handle_menu_choice(self, message: NormalizedMessage, choice: str) -> bool:
        if choice == 'tb_back_home':
            await self.menu.show_home(message)
            return True
        if choice == 'tb_home_memory':
            await self.bus.set_menu_state(message.session_id, message.chat_id, 'memory')
            await self._reply(message, '🧠 Ask Memory\n\nSend me your question, for example:\n“What did my professor say about the robotics review?”')
            return True
        if choice == 'tb_home_agents':
            await self.menu.show_agents(message)
            return True
        if choice == 'tb_home_tools':
            await self.menu.show_tools(message)
            return True
        if choice == 'tb_home_tasks':
            result = await self.runtime.query(message.tenant_id, '/tasks')
            await self._reply(message, result.answer)
            return True
        if choice == 'tb_home_followups':
            result = await self.runtime.query(message.tenant_id, '/followups')
            await self._reply(message, result.answer)
            return True

        if choice == 'tb_agent_knowledge':
            await self.bus.set_menu_state(message.session_id, message.chat_id, 'memory')
            await self._reply(message, '🧠 Knowledge Agent selected.\nAsk any question about your indexed chats and I’ll retrieve the evidence.')
            return True
        if choice == 'tb_agent_planner':
            result = await self.runtime.query(message.tenant_id, '/tasks')
            await self._reply(message, result.answer)
            return True
        if choice == 'tb_agent_followup':
            result = await self.runtime.query(message.tenant_id, '/followups')
            await self._reply(message, result.answer)
            return True
        if choice == 'tb_agent_action':
            await self._reply(message, '⚡ Action Agent selected.\nTell me what you want to do. V1.5 stays confirmation-first and will not silently execute risky actions.')
            return True

        tool_commands = {
            'tb_tool_ocr': 'ocr',
            'tb_tool_make_pdf': 'make pdf',
            'tb_tool_merge_pdf': 'merge pdfs',
            'tb_tool_compress_pdf': 'compress pdf',
            'tb_tool_transcribe': 'transcribe',
        }
        if choice in tool_commands:
            synthetic = message.model_copy(update={'body': tool_commands[choice]})
            try:
                result = await self.tools.handle(synthetic)
            except MediaProcessingError as exc:
                await self._reply(message, f'⚠️ Tool failed: {exc}')
                return True
            if result.text:
                await self._reply(message, result.text)
            return True
        if choice == 'tb_tool_resize':
            await self._reply(message, '🖼 Send an image, then tell me the size, for example: `resize 1080x1080`.')
            return True
        if choice == 'tb_tool_qr':
            await self._reply(message, '▣ Send `qr` followed by the text or URL, for example:\n`qr https://example.com`')
            return True

        return False

    @staticmethod
    def _parse_sync_hours(text: str) -> int:
        match = re.search(r'(\d{1,3})\s*(h|hr|hrs|hour|hours|d|day|days)\b', text, flags=re.I)
        if not match:
            return 24
        value = max(1, int(match.group(1)))
        unit = match.group(2).lower()
        hours = value * 24 if unit.startswith('d') else value
        return min(hours, 24 * 30)

    @staticmethod
    def _human_hours(hours: int) -> str:
        if hours % 24 == 0:
            days = hours // 24
            return f'{days} day' + ('s' if days != 1 else '')
        return f'{hours} hours'

    async def _should_use_recent_attachment(self, message: NormalizedMessage, body: str) -> bool:
        state = await self.bus.get_menu_state(message.session_id, message.chat_id)
        q = body.lower()
        referential = bool(re.search(
            r'\b(image|photo|picture|screenshot|poster|event|file|document|pdf|attachment|this|it)\b',
            q,
        ))
        return state == 'memory' and referential

    async def _reply(self, message: NormalizedMessage, text: str) -> None:
        await self.waha.send_text(message.session_id, message.chat_id, text)
