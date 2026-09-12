from __future__ import annotations

import asyncio
import json
import logging

from fastapi import FastAPI, HTTPException, Request, status

from .agent_runtime import AgentRuntime
from .config import get_settings
from .history_sync import HistorySyncService
from .ids import normalize_jid
from .media_processor import MediaProcessor
from .models import (
    AgentQueryRequest,
    AgentQueryResponse,
    ChannelMessageRequest,
    CreateChannelTokenRequest,
    CreateChannelTokenResponse,
    HealthResponse,
    RegisterUserRequest,
    RegisterUserResponse,
    SessionBootstrapResponse,
    StartWhatsAppRequest,
    StartWhatsAppResponse,
    TenantPermissionsUpdate,
    WhatsAppStatusResponse,
)
from .normalizer import normalize_waha_event, normalize_waha_poll_vote_event
from .redis_bus import EventBus
from .security import (
    extract_channel_token,
    verify_admin_token,
    verify_legacy_channel_api_key,
    verify_onboarding_api_key,
    verify_waha_hmac,
)
from .waha import WahaClient, WahaError

settings = get_settings()
logging.basicConfig(level=getattr(logging, settings.log_level.upper(), logging.INFO))
logger = logging.getLogger(__name__)

app = FastAPI(title=settings.app_name, version='1.7.0')
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
    return {
        'ok': bool(redis_ok and db_ok),
        'redis': bool(redis_ok),
        'database': db_ok,
        'database_mode': 'neon/external' if '.neon.tech' in settings.database_url else 'postgres',
        'multi_tenant': True,
    }


async def _resolve_channel_tenant(request: Request, requested_tenant: str | None, channel: str) -> str:
    token = extract_channel_token(request)
    if token:
        tenant_id = await runtime.db.tenant_for_channel_token(token, channel)
        if tenant_id:
            if requested_tenant and str(requested_tenant) != tenant_id:
                raise HTTPException(status_code=403, detail='Token is not authorized for the requested tenant')
            return tenant_id

    if settings.allow_legacy_global_channel_key:
        verify_legacy_channel_api_key(request, settings)
        if not requested_tenant:
            raise HTTPException(status_code=422, detail='tenant_id is required for legacy channel authentication')
        return str(requested_tenant)

    raise HTTPException(status_code=401, detail='Invalid or missing tenant-scoped ThreadBoss channel token')


@app.post('/v1/messages')
async def ingest_channel_message(body: ChannelMessageRequest, request: Request) -> dict:
    """Multi-tenant data-plane API for Slack/Telegram/other adapters."""
    tenant_id = await _resolve_channel_tenant(request, body.tenant_id, body.channel)
    inserted = await runtime.ingest(body.payload(tenant_id=tenant_id))
    await runtime.db.audit(
        tenant_id=tenant_id,
        actor_type='channel_adapter',
        actor_id=body.channel,
        action='message.ingest',
        resource_type='message',
        resource_id=body.message_id,
        metadata={'channel': body.channel, 'inserted': inserted},
    )
    return {'ok': True, 'inserted': inserted, 'tenant_id': tenant_id}


@app.post('/v1/query', response_model=AgentQueryResponse)
async def query_agents(body: AgentQueryRequest, request: Request) -> AgentQueryResponse:
    """Multi-tenant control-plane API for Slack/Telegram/other adapters."""
    tenant_id = await _resolve_channel_tenant(request, body.tenant_id, body.channel)
    result = await runtime.query(tenant_id, body.question)
    await runtime.db.audit(
        tenant_id=tenant_id,
        actor_type='channel_adapter',
        actor_id=body.channel,
        action='agent.query',
        resource_type='agent',
        resource_id=result.agent,
        metadata={'channel': body.channel},
    )
    return AgentQueryResponse(answer=result.answer, agent=result.agent, sources=result.sources)


# ---------------------------------------------------------------------
# Website onboarding API
# The browser should call the website backend; the website backend calls
# these endpoints with X-ThreadBoss-Onboarding-Key. WAHA secrets never enter
# the browser.
# ---------------------------------------------------------------------
@app.post('/v1/onboarding/register', response_model=RegisterUserResponse)
async def register_user(body: RegisterUserRequest, request: Request) -> RegisterUserResponse:
    verify_onboarding_api_key(request, settings)
    result = await runtime.db.register_user_tenant(
        auth_provider=body.auth_provider,
        auth_subject=body.auth_subject,
        email=body.email,
        display_name=body.display_name,
        tenant_name=body.tenant_name,
    )
    await runtime.db.audit(
        tenant_id=result['tenant_id'],
        actor_type='website',
        actor_id=result['user_id'],
        action='tenant.register',
        resource_type='tenant',
        resource_id=result['tenant_id'],
    )
    return RegisterUserResponse(**result)


@app.post('/v1/onboarding/whatsapp/start', response_model=StartWhatsAppResponse)
async def start_whatsapp(body: StartWhatsAppRequest, request: Request) -> StartWhatsAppResponse:
    verify_onboarding_api_key(request, settings)
    if not await runtime.db.tenant_exists(body.tenant_id):
        raise HTTPException(status_code=404, detail='Tenant not found')

    existing = await runtime.db.latest_whatsapp_connection_for_tenant(body.tenant_id)
    if existing:
        connection = existing
        session = existing['waha_session_name']
    else:
        compact = body.tenant_id.replace('-', '')
        session = f'tb_{compact[:28]}'
        try:
            connection = await runtime.db.create_whatsapp_connection(body.tenant_id, session)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    webhook_url = settings.public_base_url.rstrip('/') + '/webhooks/waha'
    try:
        session_info = await waha.create_session_for_tenant(session, webhook_url, body.tenant_id)
    except WahaError as exc:
        await runtime.db.update_whatsapp_status(connection['id'], status='failed')
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    waha_status = str(session_info.get('status') or 'STARTING').upper()
    db_status = {
        'WORKING': 'connected',
        'SCAN_QR_CODE': 'qr_ready',
        'STARTING': 'connecting',
        'STOPPED': 'pending',
        'FAILED': 'failed',
    }.get(waha_status, 'connecting')
    await runtime.db.update_whatsapp_status(connection['id'], status=db_status)

    base = settings.public_base_url.rstrip('/')
    return StartWhatsAppResponse(
        connection_id=connection['id'],
        tenant_id=body.tenant_id,
        session=session,
        status=db_status,
        qr_endpoint=f'{base}/v1/onboarding/whatsapp/{connection["id"]}/qr',
        status_endpoint=f'{base}/v1/onboarding/whatsapp/{connection["id"]}/status',
    )


@app.get('/v1/onboarding/whatsapp/{connection_id}/qr')
async def get_whatsapp_qr(connection_id: str, request: Request) -> dict:
    verify_onboarding_api_key(request, settings)
    connection = await runtime.db.whatsapp_connection(connection_id)
    if not connection:
        raise HTTPException(status_code=404, detail='WhatsApp connection not found')
    if connection['status'] == 'connected':
        return {'connected': True, 'connection_id': connection_id, 'session': connection['waha_session_name']}
    try:
        qr = await waha.get_qr_base64(connection['waha_session_name'])
    except WahaError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {
        'connected': False,
        'connection_id': connection_id,
        'session': connection['waha_session_name'],
        'mimetype': qr.get('mimetype', 'image/png'),
        'data': qr.get('data'),
    }


@app.get('/v1/onboarding/whatsapp/{connection_id}/status', response_model=WhatsAppStatusResponse)
async def whatsapp_status(connection_id: str, request: Request) -> WhatsAppStatusResponse:
    verify_onboarding_api_key(request, settings)
    connection = await runtime.db.whatsapp_connection(connection_id)
    if not connection:
        raise HTTPException(status_code=404, detail='WhatsApp connection not found')

    session = connection['waha_session_name']
    try:
        info = await waha.get_session(session)
        waha_status = str(info.get('status') or '').upper()
    except WahaError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    owner_id = connection.get('owner_jid')
    owner_lid = connection.get('owner_lid')
    db_status = connection['status']

    if waha_status == 'WORKING':
        try:
            owner_id, owner_lid = await resolve_owner_identity(session, info)
            await runtime.db.update_whatsapp_status(
                connection_id, status='connected', owner_jid=owner_id, owner_lid=owner_lid
            )
            db_status = 'connected'
            _schedule_backfill(session, connection['tenant_id'], owner_id, owner_lid)
        except WahaError:
            db_status = 'connecting'
    elif waha_status == 'SCAN_QR_CODE':
        db_status = 'qr_ready'
        await runtime.db.update_whatsapp_status(connection_id, status=db_status)
    elif waha_status == 'FAILED':
        db_status = 'failed'
        await runtime.db.update_whatsapp_status(connection_id, status=db_status)
    elif waha_status == 'STOPPED':
        db_status = 'pending'
        await runtime.db.update_whatsapp_status(connection_id, status=db_status)
    else:
        db_status = 'connecting'
        await runtime.db.update_whatsapp_status(connection_id, status=db_status)

    return WhatsAppStatusResponse(
        connection_id=connection_id,
        tenant_id=connection['tenant_id'],
        session=session,
        status=db_status,
        waha_status=waha_status or None,
        owner_id=owner_id,
        owner_lid=owner_lid,
        self_chat_id=owner_lid or owner_id,
    )


@app.put('/v1/onboarding/permissions')
async def update_permissions(body: TenantPermissionsUpdate, request: Request) -> dict:
    verify_onboarding_api_key(request, settings)
    changes = body.model_dump(exclude={'tenant_id'}, exclude_none=True)
    try:
        permissions = await runtime.db.update_tenant_permissions(body.tenant_id, changes)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    await runtime.db.audit(
        tenant_id=body.tenant_id, actor_type='website', actor_id='onboarding',
        action='permissions.update', resource_type='tenant', resource_id=body.tenant_id,
        metadata={'fields': sorted(changes)},
    )
    return {'ok': True, 'tenant_id': body.tenant_id, 'permissions': permissions}


@app.post('/v1/onboarding/channel-token', response_model=CreateChannelTokenResponse)
async def create_channel_token(body: CreateChannelTokenRequest, request: Request) -> CreateChannelTokenResponse:
    verify_onboarding_api_key(request, settings)
    try:
        token = await runtime.db.issue_channel_token(body.tenant_id, body.channel, body.label)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    await runtime.db.audit(
        tenant_id=body.tenant_id,
        actor_type='website',
        actor_id='onboarding',
        action='channel_token.create',
        resource_type='channel',
        resource_id=body.channel,
    )
    # Token is intentionally returned only when created. Store it in the adapter's
    # secret manager; ThreadBoss stores only its SHA-256 digest.
    return CreateChannelTokenResponse(tenant_id=body.tenant_id, channel=body.channel, token=token)


async def resolve_tenant_for_session(session: str) -> str:
    tenant_id = await runtime.db.tenant_for_waha_session(session)
    if tenant_id:
        return tenant_id
    legacy = settings.legacy_tenant_for_session(session)
    if legacy:
        return legacy
    raise KeyError(f"No tenant is bound to WAHA session '{session}'")


async def resolve_owner_identity(session: str, event: dict) -> tuple[str, str | None]:
    event_me = event.get('me') if isinstance(event.get('me'), dict) else None
    if event_me is None and isinstance(event.get('payload'), dict):
        possible = event['payload'].get('me')
        event_me = possible if isinstance(possible, dict) else None

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


async def _run_initial_backfill(session: str, tenant_id: str, owner_id: str, owner_lid: str | None) -> None:
    permissions = await runtime.db.get_tenant_permissions(tenant_id)
    hours = int(permissions.get('history_import_hours') or settings.initial_backfill_hours)
    if hours <= 0:
        return
    if await bus.is_initial_backfill_done(session):
        return
    try:
        result = await history.sync(
            session=session,
            tenant_id=tenant_id,
            owner_id=owner_id,
            owner_lid=owner_lid,
            hours=hours,
            max_messages=settings.initial_backfill_max_messages,
        )
        await bus.mark_initial_backfill_done(session)
        logger.info('Initial WAHA history backfill complete for %s: %s', session, result)
    except Exception:
        logger.exception('Initial WAHA history backfill failed for %s', session)


def _schedule_backfill(session: str, tenant_id: str, owner_id: str, owner_lid: str | None) -> None:
    task = asyncio.create_task(_run_initial_backfill(session, tenant_id, owner_id, owner_lid))
    background_tasks.add(task)
    task.add_done_callback(background_tasks.discard)


@app.post('/webhooks/waha')
async def waha_webhook(request: Request) -> dict:
    raw_body = await request.body()
    verify_waha_hmac(raw_body, request, settings)
    try:
        event = json.loads(raw_body)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail='Invalid JSON') from exc

    event_name = str(event.get('event') or '')
    if event_name not in {'message.any', 'poll.vote', 'poll.vote.failed', 'session.status'}:
        return {'ok': True, 'ignored': event_name}

    session = str(event.get('session') or 'default')
    try:
        tenant_id = await resolve_tenant_for_session(session)
    except KeyError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    if event_name == 'session.status':
        connection = await runtime.db.whatsapp_connection_by_session(session)
        if not connection:
            return {'ok': True, 'ignored': 'legacy_session_status'}
        payload = event.get('payload') if isinstance(event.get('payload'), dict) else {}
        waha_status = str(payload.get('status') or event.get('status') or '').upper()
        mapped = {
            'WORKING': 'connected',
            'SCAN_QR_CODE': 'qr_ready',
            'STARTING': 'connecting',
            'STOPPED': 'pending',
            'FAILED': 'failed',
        }.get(waha_status, 'connecting')
        owner_id = owner_lid = None
        if waha_status == 'WORKING':
            try:
                owner_id, owner_lid = await resolve_owner_identity(session, event)
            except WahaError:
                logger.exception('Session became WORKING but owner resolution failed for %s', session)
        await runtime.db.update_whatsapp_status(
            connection['id'], status=mapped, owner_jid=owner_id, owner_lid=owner_lid
        )
        if mapped == 'connected' and owner_id:
            _schedule_backfill(session, tenant_id, owner_id, owner_lid)
        return {'ok': True, 'session': session, 'tenant_id': tenant_id, 'status': mapped}

    if event_name == 'poll.vote.failed':
        return {'ok': True, 'ignored': event_name}

    try:
        owner_id, owner_lid = await resolve_owner_identity(session, event)
    except WahaError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    if event_name == 'poll.vote':
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
        'tenant_id': tenant_id,
        'route': 'self_chat' if message.is_self_chat else 'agent_ingestion',
    }


# ---------------------------------------------------------------------
# Admin migration/inspection endpoints
# ---------------------------------------------------------------------
@app.post('/admin/tenants/{tenant_id}/bind-session/{session}')
async def bind_existing_session(tenant_id: str, session: str, request: Request) -> dict:
    verify_admin_token(request, settings)
    if not await runtime.db.tenant_exists(tenant_id):
        raise HTTPException(status_code=404, detail='Tenant not found')
    connection = await runtime.db.bind_existing_waha_session(tenant_id, session)
    try:
        me = await waha.get_me(session)
        owner_id = normalize_jid(str(me['id']))
        owner_lid = normalize_jid(str(me.get('lid'))) if me.get('lid') else None
        if not owner_lid:
            owner_lid = await waha.get_lid_by_phone_number(session, owner_id)
        await runtime.db.update_whatsapp_status(
            connection['id'], status='connected', owner_jid=owner_id, owner_lid=owner_lid
        )
        await waha.configure_message_any_webhook(
            session, settings.public_base_url.rstrip('/') + '/webhooks/waha'
        )
        _schedule_backfill(session, tenant_id, owner_id, owner_lid)
    except WahaError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {
        'ok': True,
        'connection_id': connection['id'],
        'tenant_id': tenant_id,
        'session': session,
        'owner_id': owner_id,
        'owner_lid': owner_lid,
    }


@app.post('/admin/sessions/{session}/bootstrap', response_model=SessionBootstrapResponse)
async def bootstrap_session(session: str, request: Request, configure_webhook: bool = True):
    verify_admin_token(request, settings)
    try:
        tenant_id = await resolve_tenant_for_session(session)
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
        connection = await runtime.db.whatsapp_connection_by_session(session)
        if connection:
            await runtime.db.update_whatsapp_status(
                connection['id'], status='connected', owner_jid=owner_id, owner_lid=owner_lid
            )
    except WahaError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    _schedule_backfill(session, tenant_id, owner_id, owner_lid)
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
    try:
        tenant_id = await resolve_tenant_for_session(session)
    except KeyError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
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
