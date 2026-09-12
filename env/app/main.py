from __future__ import annotations

import asyncio
import json
import logging

from fastapi import FastAPI, HTTPException, Request

from .agent_runtime import AgentRuntime
from .config import get_settings
from .history_sync import HistorySyncService
from .ids import normalize_jid
from .media_processor import MediaProcessor
from .models import (
    AgentQueryRequest,
    AgentQueryResponse,
    ChannelMessageRequest,
    HealthResponse,
    SessionBootstrapResponse,
)
from .normalizer import normalize_waha_event, normalize_waha_poll_vote_event
from .redis_bus import EventBus
from .security import verify_admin_token, verify_channel_api_key, verify_waha_hmac
from .waha import WahaClient, WahaError

settings = get_settings()
logging.basicConfig(level=getattr(logging, settings.log_level.upper(), logging.INFO))
logger = logging.getLogger(__name__)

app = FastAPI(title=settings.app_name, version='1.6.0')
bus = EventBus(settings)
waha = WahaClient(settings)
runtime = AgentRuntime(settings)
media = MediaProcessor(settings)
history = HistorySyncService(settings, waha, runtime, media)
background_tasks: set[asyncio.Task] = set()


@app.get('/health', response_model=HealthResponse)
async def health() -> HealthResponse:
    return HealthResponse(ok=True, service=settings.app_name, environment=settings.environment)


@app.get('/ready')
async def ready() -> dict:
    try:
        redis_ok = await bus.ping()
        await runtime.db.ensure()
        db_ok = True
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f'Dependency unavailable: {exc}') from exc
    return {'ok': bool(redis_ok and db_ok), 'redis': bool(redis_ok), 'database': db_ok}


@app.post('/v1/messages')
async def ingest_channel_message(body: ChannelMessageRequest, request: Request) -> dict:
    """Common data-plane API for Slack/Telegram/other channel adapters."""
    verify_channel_api_key(request, settings)
    inserted = await runtime.ingest(body.payload())
    return {'ok': True, 'inserted': inserted}


@app.post('/v1/query', response_model=AgentQueryResponse)
async def query_agents(body: AgentQueryRequest, request: Request) -> AgentQueryResponse:
    """Common control-plane API for WhatsApp, Slack and Telegram."""
    verify_channel_api_key(request, settings)
    result = await runtime.query(body.tenant_id, body.question)
    return AgentQueryResponse(answer=result.answer, agent=result.agent, sources=result.sources)


async def resolve_owner_identity(session: str, event: dict) -> tuple[str, str | None]:
    event_me = event.get('me') if isinstance(event.get('me'), dict) else None
    owner_id: str | None = None
    owner_lid: str | None = None

    if event_me and event_me.get('id'):
        owner_id = normalize_jid(str(event_me['id']))
        await bus.set_owner(session, owner_id)
    if event_me and event_me.get('lid'):
        owner_lid = normalize_jid(str(event_me['lid']))
        await bus.set_owner_lid(session, owner_lid)
    if not owner_id:
        owner_id = await bus.get_owner(session)
    if not owner_lid:
        owner_lid = await bus.get_owner_lid(session)
    if not owner_id:
        me = await waha.get_me(session)
        owner_id = normalize_jid(str(me['id']))
        await bus.set_owner(session, owner_id)
        if me.get('lid'):
            owner_lid = normalize_jid(str(me['lid']))
            await bus.set_owner_lid(session, owner_lid)
    if owner_id and not owner_lid:
        try:
            owner_lid = await waha.get_lid_by_phone_number(session, owner_id)
        except WahaError as exc:
            logger.warning('Could not resolve owner LID for session %s: %s', session, exc)
            owner_lid = None
        if owner_lid:
            await bus.set_owner_lid(session, owner_lid)
    if not owner_id:
        raise WahaError(f"Could not resolve WhatsApp owner for session '{session}'")
    return owner_id, owner_lid


@app.post('/webhooks/waha')
async def waha_webhook(request: Request) -> dict:
    raw_body = await request.body()
    verify_waha_hmac(raw_body, request, settings)
    try:
        event = json.loads(raw_body)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail='Invalid JSON') from exc
    event_name = str(event.get('event') or '')
    if event_name not in {'message.any', 'poll.vote', 'poll.vote.failed'}:
        return {'ok': True, 'ignored': event_name}

    # Failed poll decryption has no usable selection. The next 'hi'/'menu' simply
    # re-opens the interactive menu, so do not poison the event queue with it.
    if event_name == 'poll.vote.failed':
        return {'ok': True, 'ignored': event_name}

    session = str(event.get('session') or 'default')
    try:
        tenant_id = settings.tenant_for_session(session)
    except KeyError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    try:
        owner_id, owner_lid = await resolve_owner_identity(session, event)
    except WahaError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    if event_name == 'poll.vote':
        # Only handle votes for polls created by this WhatsApp account. Menu polls
        # are a fallback when native list messages are unavailable.
        poll = (event.get('payload') or {}).get('poll') or {}
        if not bool(poll.get('fromMe')):
            return {'ok': True, 'ignored': 'foreign_poll_vote'}
        message = normalize_waha_poll_vote_event(
            event, tenant_id=tenant_id, owner_id=owner_id, owner_lid=owner_lid
        )
        if not message.is_self_chat:
            return {'ok': True, 'ignored': 'non_self_poll_vote'}
    else:
        message = normalize_waha_event(
            event, tenant_id=tenant_id, owner_id=owner_id, owner_lid=owner_lid
        )
    redis_id = await bus.publish(message)
    return {
        'ok': True,
        'queued': True,
        'redis_id': redis_id,
        'route': 'self_chat' if message.is_self_chat else 'agent_ingestion',
    }


async def _run_initial_backfill(session: str, tenant_id: str, owner_id: str, owner_lid: str | None) -> None:
    if settings.initial_backfill_hours <= 0:
        return
    if await bus.is_initial_backfill_done(session):
        return
    try:
        result = await history.sync(
            session=session,
            tenant_id=tenant_id,
            owner_id=owner_id,
            owner_lid=owner_lid,
            hours=settings.initial_backfill_hours,
            max_messages=settings.initial_backfill_max_messages,
        )
        await bus.mark_initial_backfill_done(session)
        logger.info('Initial WAHA history backfill complete for %s: %s', session, result)
    except Exception:
        # Do not fail WhatsApp onboarding just because history sync failed. The
        # user can retry later with /sync 24h or /sync 7d.
        logger.exception('Initial WAHA history backfill failed for %s', session)



@app.post('/admin/sessions/{session}/bootstrap', response_model=SessionBootstrapResponse)
async def bootstrap_session(session: str, request: Request, configure_webhook: bool = True):
    verify_admin_token(request, settings)
    try:
        tenant_id = settings.tenant_for_session(session)
    except KeyError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    try:
        me = await waha.get_me(session)
        owner_id = normalize_jid(str(me['id']))
        await bus.set_owner(session, owner_id)
        owner_lid = normalize_jid(str(me.get('lid'))) if me.get('lid') else None
        if not owner_lid:
            owner_lid = await waha.get_lid_by_phone_number(session, owner_id)
        if owner_lid:
            await bus.set_owner_lid(session, owner_lid)
        webhook_url = settings.public_base_url.rstrip('/') + '/webhooks/waha'
        if configure_webhook:
            await waha.configure_message_any_webhook(session, webhook_url)
        await runtime.db.ensure()
    except WahaError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    # First successful bootstrap warms the Knowledge Agent with recent history in
    # the background. It is idempotent because DB inserts are unique and Redis
    # stores a completion marker.
    task = asyncio.create_task(_run_initial_backfill(session, tenant_id, owner_id, owner_lid))
    background_tasks.add(task)
    task.add_done_callback(background_tasks.discard)

    return SessionBootstrapResponse(
        session=session,
        tenant_id=tenant_id,
        owner_id=owner_id,
        owner_lid=owner_lid,
        self_chat_id=owner_lid or owner_id,
        webhook_url=webhook_url,
        webhook_configured=configure_webhook,
    )


@app.get('/admin/sessions/{session}')
async def inspect_session(session: str, request: Request):
    verify_admin_token(request, settings)
    tenant_id = settings.tenant_for_session(session)
    try:
        owner_id, owner_lid = await resolve_owner_identity(session, {})
    except WahaError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {
        'session': session,
        'tenant_id': tenant_id,
        'owner_id': owner_id,
        'owner_lid': owner_lid,
        'self_chat_id': owner_lid or owner_id,
    }
