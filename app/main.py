from __future__ import annotations

import json
import logging

from fastapi import FastAPI, HTTPException, Request

from .agent_runtime import AgentRuntime
from .config import get_settings
from .ids import normalize_jid
from .models import (
    AgentQueryRequest,
    AgentQueryResponse,
    ChannelMessageRequest,
    HealthResponse,
    SessionBootstrapResponse,
)
from .normalizer import normalize_waha_event
from .redis_bus import EventBus
from .security import verify_admin_token, verify_channel_api_key, verify_waha_hmac
from .waha import WahaClient, WahaError

settings = get_settings()
logging.basicConfig(level=getattr(logging, settings.log_level.upper(), logging.INFO))
logger = logging.getLogger(__name__)

app = FastAPI(title=settings.app_name, version='1.3.0')
bus = EventBus(settings)
waha = WahaClient(settings)
runtime = AgentRuntime(settings)


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
    if event.get('event') != 'message.any':
        return {'ok': True, 'ignored': event.get('event')}

    session = str(event.get('session') or 'default')
    try:
        tenant_id = settings.tenant_for_session(session)
    except KeyError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    try:
        owner_id, owner_lid = await resolve_owner_identity(session, event)
    except WahaError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    message = normalize_waha_event(event, tenant_id=tenant_id, owner_id=owner_id, owner_lid=owner_lid)
    redis_id = await bus.publish(message)
    return {
        'ok': True,
        'queued': True,
        'redis_id': redis_id,
        'route': 'self_chat' if message.is_self_chat else 'agent_ingestion',
    }


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
