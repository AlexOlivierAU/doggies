from __future__ import annotations

from datetime import date

from fetch import FetchError
from parse_racingaustralia import MeetingList, fetch_meetings_for_date


def _calendar_html(key: str) -> str:
    return f'<html><body><a href="/FreeFields/Acceptances.aspx?Key={key}">Acceptances</a></body></html>'


class _Resp:
    def __init__(self, text: str) -> None:
        self.text = text


def test_one_state_timeout_keeps_other_meetings(monkeypatch):
    d = date(2026, 8, 29)

    def fake_get(url, **_kw):
        if "State=QLD" in url:
            raise FetchError("timeout")
        if "State=NSW" in url:
            return _Resp(_calendar_html("2026Aug29,NSW,Randwick"))
        if "State=VIC" in url:
            return _Resp(_calendar_html("2026Aug29,VIC,Flemington"))
        if "State=WA" in url:
            return _Resp(_calendar_html("2026Aug29,WA,Ascot"))
        return _Resp("<html></html>")

    monkeypatch.setattr("parse_racingaustralia.get", fake_get)
    meetings = fetch_meetings_for_date(d, ttl_seconds=1)
    assert isinstance(meetings, list)
    assert isinstance(meetings, MeetingList)
    venues = {m.venue for m in meetings}
    assert venues == {"Randwick", "Flemington", "Ascot"}
    assert "QLD" in meetings.failed_states
    assert "NSW" not in meetings.failed_states
    assert len(meetings) == 3


def test_all_states_failing_returns_empty_with_diagnostics(monkeypatch):
    def boom(url, **_kw):
        raise FetchError("timeout")

    monkeypatch.setattr("parse_racingaustralia.get", boom)
    meetings = fetch_meetings_for_date(date(2026, 8, 29), ttl_seconds=1)
    assert list(meetings) == []
    assert "NSW" in meetings.failed_states
    assert "QLD" in meetings.failed_states
    assert len(meetings.failed_states) >= 4
