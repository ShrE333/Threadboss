from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .config import Settings
from .models import NormalizedMessage
from .waha import WahaClient, WahaError

if TYPE_CHECKING:
    from .redis_bus import EventBus

logger = logging.getLogger(__name__)


HOME_ROWS = [
    {"title": "🧠 Ask Memory", "rowId": "tb_home_memory", "description": "Search your indexed chats"},
    {"title": "🤖 Agents", "rowId": "tb_home_agents", "description": "Knowledge, Planner, Follow-up, Action"},
    {"title": "🛠 Tools", "rowId": "tb_home_tools", "description": "OCR, PDF, QR, voice and images"},
    {"title": "✅ Tasks", "rowId": "tb_home_tasks", "description": "Open tasks and commitments"},
    {"title": "🔔 Follow-ups", "rowId": "tb_home_followups", "description": "Overdue and upcoming items"},
]

AGENT_ROWS = [
    {"title": "🧠 Knowledge Agent", "rowId": "tb_agent_knowledge", "description": "Ask about past chats and facts"},
    {"title": "📋 Planner Agent", "rowId": "tb_agent_planner", "description": "Tasks, deadlines and commitments"},
    {"title": "🔔 Follow-up Agent", "rowId": "tb_agent_followup", "description": "Pending and overdue commitments"},
    {"title": "⚡ Action Agent", "rowId": "tb_agent_action", "description": "Plan an action safely"},
    {"title": "← Back", "rowId": "tb_back_home", "description": "Return to ThreadBoss home"},
]

TOOL_ROWS = [
    {"title": "🔎 OCR Image", "rowId": "tb_tool_ocr", "description": "Extract text from your latest image"},
    {"title": "📄 Make PDF", "rowId": "tb_tool_make_pdf", "description": "Turn recent images into one PDF"},
    {"title": "🧩 Merge PDFs", "rowId": "tb_tool_merge_pdf", "description": "Combine recent PDF files"},
    {"title": "🗜 Compress PDF", "rowId": "tb_tool_compress_pdf", "description": "Reduce size of your latest PDF"},
    {"title": "🖼 Resize Image", "rowId": "tb_tool_resize", "description": "Resize the latest image"},
    {"title": "▣ Generate QR", "rowId": "tb_tool_qr", "description": "Create a QR code from text or URL"},
    {"title": "🎙 Transcribe", "rowId": "tb_tool_transcribe", "description": "Transcribe latest voice note/audio"},
    {"title": "← Back", "rowId": "tb_back_home", "description": "Return to ThreadBoss home"},
]


_MENU_ROWS = {
    "home": HOME_ROWS,
    "agents": AGENT_ROWS,
    "tools": TOOL_ROWS,
}


def _clean(value: str | None) -> str:
    return " ".join((value or "").strip().lower().split())


def resolve_menu_choice(state: str | None, *, interactive_id: str | None, text: str | None) -> str | None:
    """Resolve a WAHA list row id or its visible text into a ThreadBoss menu action.

    WAHA engines can represent list replies differently. Prefer the stable rowId when
    present, then fall back to the visible title/body while a menu state is active.
    """
    if interactive_id:
        candidate = interactive_id.strip()
        if candidate.startswith("tb_"):
            return candidate

    normalized = _clean(text)
    if not normalized:
        return None

    # Let users type a few obvious navigation words too.
    typed = {
        "agents": "tb_home_agents",
        "tools": "tb_home_tools",
        "tasks": "tb_home_tasks",
        "follow-ups": "tb_home_followups",
        "followups": "tb_home_followups",
        "memory": "tb_home_memory",
        "back": "tb_back_home",
    }
    if normalized in typed:
        return typed[normalized]

    rows = _MENU_ROWS.get(state or "", [])
    for row in rows:
        if normalized == _clean(row["title"]):
            return str(row["rowId"])

    # Poll fallback returns the visible option text. Recognize all known titles
    # even if Redis menu state expired between poll send and vote.
    for menu_rows in _MENU_ROWS.values():
        for row in menu_rows:
            if normalized == _clean(row["title"]):
                return str(row["rowId"])
    return None


@dataclass
class MenuSendResult:
    mode: str  # list | poll | text


class InteractiveMenu:
    def __init__(self, settings: Settings, bus: EventBus, waha: WahaClient):
        self.settings = settings
        self.bus = bus
        self.waha = waha

    async def show_home(self, message: NormalizedMessage) -> MenuSendResult:
        return await self._send_menu(
            message,
            state="home",
            title="ThreadBoss",
            description="What would you like to do?",
            button="Open Menu",
            section_title="Choose an option",
            rows=HOME_ROWS,
        )

    async def show_agents(self, message: NormalizedMessage) -> MenuSendResult:
        return await self._send_menu(
            message,
            state="agents",
            title="🤖 ThreadBoss Agents",
            description="Choose the agent you want to use.",
            button="Choose Agent",
            section_title="Agents",
            rows=AGENT_ROWS,
        )

    async def show_tools(self, message: NormalizedMessage) -> MenuSendResult:
        return await self._send_menu(
            message,
            state="tools",
            title="🛠 ThreadBoss Tools",
            description="Choose a utility. Recent attachments are remembered temporarily.",
            button="Choose Tool",
            section_title="Tools",
            rows=TOOL_ROWS,
        )

    async def resolve(self, message: NormalizedMessage) -> str | None:
        state = await self.bus.get_menu_state(message.session_id, message.chat_id)
        visible = message.interactive_title or message.body
        return resolve_menu_choice(state, interactive_id=message.interactive_id, text=visible)

    async def _send_menu(
        self,
        message: NormalizedMessage,
        *,
        state: str,
        title: str,
        description: str,
        button: str,
        section_title: str,
        rows: list[dict[str, str]],
    ) -> MenuSendResult:
        await self.bus.set_menu_state(message.session_id, message.chat_id, state)

        if not self.settings.interactive_menu_enabled:
            await self.waha.send_text(message.session_id, message.chat_id, self._text_menu(title, rows))
            return MenuSendResult("text")

        try:
            await self.waha.send_list(
                message.session_id,
                message.chat_id,
                title=title,
                description=description,
                footer="ThreadBoss • private control plane",
                button=button,
                sections=[{"title": section_title, "rows": rows}],
            )
            return MenuSendResult("list")
        except WahaError as exc:
            logger.warning("WAHA list menu failed; trying fallback: %s", exc)

        # WAHA documents polls as a broadly supported interactive alternative to
        # list/buttons. This also keeps the UX tap-first if sendList is unavailable.
        if self.settings.interactive_menu_poll_fallback:
            try:
                result = await self.waha.send_poll(
                    message.session_id,
                    message.chat_id,
                    name=description,
                    options=[row["title"] for row in rows],
                    multiple_answers=False,
                )
                poll_id = str(result.get("id") or "")
                if poll_id:
                    await self.bus.remember_menu_poll(
                        message.session_id,
                        message.chat_id,
                        poll_id,
                        state,
                    )
                return MenuSendResult("poll")
            except WahaError as exc:
                logger.warning("WAHA poll fallback failed; using text menu: %s", exc)

        await self.waha.send_text(message.session_id, message.chat_id, self._text_menu(title, rows))
        return MenuSendResult("text")

    @staticmethod
    def _text_menu(title: str, rows: list[dict[str, str]]) -> str:
        lines = [title]
        for index, row in enumerate(rows, start=1):
            lines.append(f"{index}. {row['title']}")
        lines.append("\nSend the option name, or type menu to reopen this menu.")
        return "\n".join(lines)
