import pytest
from datetime import datetime, timezone, timedelta

from app.config import Settings
from app.history_sync import HistorySyncService


class FakeWaha:
    def __init__(self):
        self.offsets = []

    async def get_messages_range(self, session, since, until, *, limit, offset, download_media):
        self.offsets.append(offset)
        if offset == 0:
            return [
                {'id':'false_111@c.us_A','timestamp':1789080000,'from':'111@c.us','fromMe':False,'body':'one'},
                {'id':'false_222@c.us_B','timestamp':1789080001,'from':'222@c.us','fromMe':False,'body':'two'},
            ]
        if offset == limit:
            return [
                {'id':'false_333@c.us_C','timestamp':1789080002,'from':'333@c.us','fromMe':False,'body':'three'},
            ]
        return []


class FakeRuntime:
    def __init__(self): self.payloads=[]
    async def ingest_detailed(self, payload):
        self.payloads.append(payload)
        return 'inserted'


class FakeMedia:
    pass


@pytest.mark.asyncio
async def test_filtered_short_page_does_not_end_pagination_early():
    settings = Settings(history_sync_page_size=10, initial_backfill_max_messages=100, session_tenants_json='{"default":"t"}')
    waha = FakeWaha(); runtime = FakeRuntime()
    svc = HistorySyncService(settings, waha, runtime, FakeMedia())
    start = datetime(2026,9,11,tzinfo=timezone.utc)
    end = start + timedelta(days=1)
    result = await svc.sync_range(session='default',tenant_id='t',owner_id='999@c.us',owner_lid=None,start=start,end=end)
    assert result['fetched'] == 3
    assert result['inserted'] == 3
    assert waha.offsets[:3] == [0,10,20]
