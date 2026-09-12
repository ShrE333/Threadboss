from datetime import timedelta
from zoneinfo import ZoneInfo

from app.agents.knowledge import KnowledgeAgent
from app.config import Settings


class Dummy:
    pass


def test_yesterday_window_is_exact_calendar_day():
    settings = Settings(default_timezone='Asia/Kolkata', session_tenants_json='{"default":"t"}')
    agent = KnowledgeAgent(settings, Dummy(), Dummy())
    start, end = agent.temporal_window('what did I receive yesterday?')
    assert end - start == timedelta(days=1)
    local_start = start.astimezone(ZoneInfo('Asia/Kolkata'))
    local_end = end.astimezone(ZoneInfo('Asia/Kolkata'))
    assert (local_start.hour, local_start.minute, local_start.second) == (0, 0, 0)
    assert (local_end.hour, local_end.minute, local_end.second) == (0, 0, 0)


def test_received_only_detection():
    settings = Settings(session_tenants_json='{"default":"t"}')
    agent = KnowledgeAgent(settings, Dummy(), Dummy())
    assert agent.received_only('what messages did I receive yesterday?') is True
    assert agent.received_only('what did I send yesterday?') is False
