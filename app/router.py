from __future__ import annotations

import asyncio
import logging
import re

from .agent_runtime import AgentRuntime
from .config import Settings
from .demo_actions import match_demo_flight_action
from .history_sync import HistorySyncService
from .media_processor import MediaProcessingError, MediaProcessor
from .menu import InteractiveMenu
from .models import NormalizedMessage
from .redis_bus import EventBus
from .tools import ToolEngine
from .waha import WahaClient

logger = logging.getLogger(__name__)

HELP_TEXT = """🧠 ThreadBoss V1.7

Type *hi* or *menu* to open the native WhatsApp menu.

Quick commands:
/help
/status
/agents
/tasks
/followups
/memory <question>
/memory stats
/tools
/sync 24h
/sync 7d
/sync yesterday

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

        # Deterministic live-demo action: bypass LLM routing and return the exact
        # Ixigo deep link after a short handoff delay so ThreadBoss Hands / the
        # browser extension can take over reliably during the product demo.
        demo_flight = match_demo_flight_action(body)
        if demo_flight:
            await self._reply(
                message,
                f'✈️ ThreadBoss Hands is preparing {demo_flight.origin} → {demo_flight.destination} for {demo_flight.date_label}…'
            )
            await asyncio.sleep(2)
            await self._reply(message, demo_flight.url)
            return

        if lowered in {'hi','hii','hiii','hello','hey','start','/start','menu','/menu','home'}:
            await self.menu.show_home(message); return

        choice = await self.menu.resolve(message)
        if choice and await self._handle_menu_choice(message, choice):
            return

        if lowered in {'/help','help','threadboss help'}:
            await self._reply(message, HELP_TEXT); return

        if lowered in {'/status','status','threadboss status'}:
            task_stats = await self.runtime.db.task_stats(message.tenant_id)
            mem = await self.runtime.db.memory_stats(message.tenant_id)
            await self._reply(
                message,
                '✅ ThreadBoss V1.7 is online.\n'
                f'Knowledge: {self.settings.knowledge_provider}/{self.settings.knowledge_model}\n'
                f'Embedding: {self.settings.embedding_provider}/{self.settings.embedding_model}\n'
                f'Indexed messages: {mem.get("with_text",0)}\n'
                f'Open tasks: {task_stats.get("open",0)}\n'
                f'Interactive menu: {"on" if self.settings.interactive_menu_enabled else "off"}'
            ); return

        if lowered in {'/memory stats','memory stats'}:
            mem = await self.runtime.db.memory_stats(message.tenant_id)
            await self._reply(
                message,
                '🧠 Memory stats\n'
                f'Total rows: {mem.get("total",0)}\n'
                f'With text: {mem.get("with_text",0)}\n'
                f'With embeddings: {mem.get("with_embedding",0)}\n'
                f'Received: {mem.get("received",0)}\n'
                f'Earliest: {mem.get("earliest")}\nLatest: {mem.get("latest")}'
            ); return

        if lowered in {'/agents','agents'}:
            await self.menu.show_agents(message); return
        if lowered in {'/tools','tools','threadboss tools'}:
            await self.menu.show_tools(message); return

        if lowered.startswith('/sync') or lowered.startswith('sync '):
            await self._manual_sync(message, lowered); return

        try:
            tool_result = await self.tools.handle(message)
        except MediaProcessingError as exc:
            await self._reply(message, f'⚠️ Tool failed: {exc}'); return
        except Exception as exc:
            logger.exception('Tool execution failed')
            await self._reply(message, f'⚠️ Tool execution failed: {exc}'); return
        if tool_result.handled:
            if tool_result.text:
                await self._reply(message, tool_result.text)
            return

        media_context = ''
        if message.has_media and message.media:
            try:
                media_context = await self.media.context_for_media(message.media, body)
            except MediaProcessingError as exc:
                await self._reply(message, f'⚠️ Attachment processing failed: {exc}'); return
        elif await self._should_use_recent_attachment(message, body):
            refs = await self.bus.recent_attachments(message.session_id, message.chat_id)
            if refs:
                try:
                    media_context = await self.media.context_for_media(refs[-1], body)
                except MediaProcessingError as exc:
                    logger.warning('Recent attachment context failed: %s', exc)

        if not body and not media_context:
            return
        question = body
        if body and media_context:
            question = f'{body}\n\n--- Attachment context ---\n{media_context}'
        elif media_context:
            question = f'Use this attachment content and respond helpfully:\n\n{media_context}'

        await self._auto_sync_temporal_if_needed(message, question)
        result = await self.runtime.query(message.tenant_id, question)
        await self._reply(message, result.answer.strip() or 'I could not produce an answer.')

    async def _auto_sync_temporal_if_needed(self, message: NormalizedMessage, question: str) -> None:
        if not self.settings.auto_sync_temporal_queries:
            return
        window = self.runtime.knowledge.temporal_window(question)
        if not window:
            return
        start, end = window
        key_start, key_end = start.isoformat(), end.isoformat()
        if await self.bus.is_temporal_sync_fresh(message.session_id, key_start, key_end):
            return
        try:
            await self.history.sync_range(
                session=message.session_id, tenant_id=message.tenant_id,
                owner_id=message.owner_id, owner_lid=message.owner_lid,
                start=start, end=end,
            )
            await self.bus.mark_temporal_sync_fresh(message.session_id, key_start, key_end)
        except Exception:
            logger.exception('Temporal auto-sync failed; continuing with local memory')

    async def _manual_sync(self, message: NormalizedMessage, lowered: str) -> None:
        if 'yesterday' in lowered:
            window = self.runtime.knowledge.temporal_window('yesterday')
            assert window
            await self._reply(message, '🔄 Syncing the full previous calendar day…')
            result = await self.history.sync_range(
                session=message.session_id, tenant_id=message.tenant_id,
                owner_id=message.owner_id, owner_lid=message.owner_lid,
                start=window[0], end=window[1],
            )
        else:
            hours = self._parse_sync_hours(lowered)
            await self._reply(message, f'🔄 Syncing the last {self._human_hours(hours)} of WhatsApp history…')
            result = await self.history.sync(
                session=message.session_id, tenant_id=message.tenant_id,
                owner_id=message.owner_id, owner_lid=message.owner_lid,
                hours=hours,
            )
        await self._reply(
            message,
            '✅ Memory sync complete.\n'
            f'Fetched: {result["fetched"]}\n'
            f'Inserted: {result["inserted"]}\n'
            f'Repaired: {result["repaired"]}\n'
            f'Already indexed: {result["duplicates"]}\n'
            f'Skipped self-chat: {result["skipped_self"]}\n'
            f'Empty/system: {result["empty"]}\n'
            f'Media extracted: {result["media_enriched"]}\n'
            f'Failed rows: {result["failed"]}'
        )

    async def _handle_menu_choice(self, message: NormalizedMessage, choice: str) -> bool:
        if choice == 'tb_back_home':
            await self.menu.show_home(message); return True
        if choice == 'tb_home_memory':
            await self.bus.set_menu_state(message.session_id, message.chat_id, 'memory')
            await self._reply(message, '🧠 Ask Memory\n\nAsk anything about your indexed chats. Temporal questions like “what was important yesterday?” auto-sync that exact time window.'); return True
        if choice == 'tb_home_agents':
            await self.menu.show_agents(message); return True
        if choice == 'tb_home_tools':
            await self.menu.show_tools(message); return True
        if choice == 'tb_home_tasks':
            r=await self.runtime.query(message.tenant_id,'/tasks'); await self._reply(message,r.answer); return True
        if choice == 'tb_home_followups':
            r=await self.runtime.query(message.tenant_id,'/followups'); await self._reply(message,r.answer); return True
        if choice == 'tb_agent_knowledge':
            await self.bus.set_menu_state(message.session_id,message.chat_id,'memory')
            await self._reply(message,'🧠 Knowledge Agent selected. Ask about your indexed chats.'); return True
        if choice == 'tb_agent_planner':
            r=await self.runtime.query(message.tenant_id,'/tasks'); await self._reply(message,r.answer); return True
        if choice == 'tb_agent_followup':
            r=await self.runtime.query(message.tenant_id,'/followups'); await self._reply(message,r.answer); return True
        if choice == 'tb_agent_action':
            await self._reply(message,'⚡ Action Agent selected. Tell me what you want to do; risky actions stay confirmation-first.'); return True

        tool_commands={'tb_tool_ocr':'ocr','tb_tool_make_pdf':'make pdf','tb_tool_merge_pdf':'merge pdfs','tb_tool_compress_pdf':'compress pdf','tb_tool_transcribe':'transcribe'}
        if choice in tool_commands:
            synthetic=message.model_copy(update={'body':tool_commands[choice]})
            try:
                r=await self.tools.handle(synthetic)
            except MediaProcessingError as exc:
                await self._reply(message,f'⚠️ Tool failed: {exc}'); return True
            if r.text: await self._reply(message,r.text)
            return True
        if choice=='tb_tool_resize':
            await self._reply(message,'🖼 Send an image, then `resize 1080x1080`.'); return True
        if choice=='tb_tool_qr':
            await self._reply(message,'▣ Send `qr` followed by text or a URL.'); return True
        return False

    @staticmethod
    def _parse_sync_hours(text: str) -> int:
        match=re.search(r'(\d{1,3})\s*(h|hr|hrs|hour|hours|d|day|days)\b',text,flags=re.I)
        if not match: return 24
        value=max(1,int(match.group(1))); unit=match.group(2).lower()
        return min(value*24 if unit.startswith('d') else value,24*30)

    @staticmethod
    def _human_hours(hours:int)->str:
        if hours%24==0:
            d=hours//24; return f'{d} day'+('s' if d!=1 else '')
        return f'{hours} hours'

    async def _should_use_recent_attachment(self,message:NormalizedMessage,body:str)->bool:
        state=await self.bus.get_menu_state(message.session_id,message.chat_id)
        referential=bool(re.search(r'\b(image|photo|picture|screenshot|poster|event|file|document|pdf|attachment|this|it)\b',body.lower()))
        return state=='memory' and referential

    async def _reply(self,message:NormalizedMessage,text:str)->None:
        await self.waha.send_text(message.session_id,message.chat_id,text)
