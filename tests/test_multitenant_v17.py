import pytest

from app.config import Settings
from app.models import ChannelMessageRequest
from app.waha import WahaClient


class FakeResponse:
    def __init__(self, data): self._data = data
    def json(self): return self._data


class CaptureWaha(WahaClient):
    def __init__(self, settings):
        super().__init__(settings)
        self.calls = []

    async def _request(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs))
        return FakeResponse({'name': 'tb_demo', 'status': 'SCAN_QR_CODE'})


@pytest.mark.asyncio
async def test_waha_multitenant_session_has_metadata_and_status_webhook():
    client = CaptureWaha(Settings(waha_webhook_hmac_key='secret'))
    await client.create_session_for_tenant('tb_demo', 'https://tb.example/webhooks/waha', 'tenant-123')
    method, path, kwargs = client.calls[0]
    assert method == 'POST'
    assert path == '/api/sessions'
    payload = kwargs['json']
    assert payload['config']['metadata']['threadbossTenantId'] == 'tenant-123'
    assert 'session.status' in payload['config']['webhooks'][0]['events']
    assert payload['config']['webhooks'][0]['hmac']['key'] == 'secret'


def test_channel_payload_uses_authenticated_tenant_not_body_tenant():
    body = ChannelMessageRequest(
        tenant_id='spoofed-tenant',
        channel='telegram',
        chat_id='chat-1',
        message_id='m1',
        timestamp='2026-09-12T00:00:00Z',
        text='hello',
    )
    payload = body.payload(tenant_id='authenticated-tenant')
    assert payload['tenant_id'] == 'authenticated-tenant'


def test_legacy_session_mapping_is_only_fallback():
    settings = Settings(session_tenants_json='{"old-session":"legacy-tenant"}')
    assert settings.legacy_tenant_for_session('old-session') == 'legacy-tenant'
    assert settings.legacy_tenant_for_session('new-session') is None
