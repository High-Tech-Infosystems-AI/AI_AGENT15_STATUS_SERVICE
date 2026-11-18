"""The event endpoint queues webhooks without letting them affect the event."""
import asyncio

from starlette.requests import Request

from app.api.endpoints.notifications import event_trigger_api as api
from app.notification_layer.schemas import EventTriggerRequest


def _http(headers=()):
    return Request({"type": "http", "headers": [(k.lower().encode(), v.encode()) for k, v in headers]})


def test_event_still_handled_when_webhook_queueing_fails(monkeypatch):
    def broken(*_):
        raise RuntimeError("db down")
    monkeypatch.setattr(api, "_queue_webhooks", broken)
    monkeypatch.setattr(api, "handle_event", lambda db, event_name, data: (True, 5, "ok"))
    out = asyncio.run(api.trigger_event(EventTriggerRequest(event_name="candidate_created", data={}),
                                        _http(), db=None))
    assert out.success and out.notification_id == 5


def test_queued_deliveries_are_sent_even_when_the_event_is_not_registered(monkeypatch):
    sent = []
    monkeypatch.setattr(api, "_queue_webhooks", lambda name, data: [11, 12])
    monkeypatch.setattr(api, "deliver_now", lambda ids: sent.append(ids))
    monkeypatch.setattr(api, "handle_event", lambda db, event_name, data: (False, None, "not found"))

    async def run():
        try:
            await api.trigger_event(EventTriggerRequest(event_name="candidate_created",
                                                        data={"organization_id": 1}), _http(), db=None)
        except Exception:
            pass
        await asyncio.gather(*api._deliveries_in_flight)

    asyncio.run(run())
    assert sent == [[11, 12]]


def test_events_that_came_through_the_gateway_never_reach_webhooks(monkeypatch):
    queued = []
    monkeypatch.setattr(api, "_queue_webhooks", lambda name, data: queued.append(name) or [1])
    monkeypatch.setattr(api, "handle_event", lambda db, event_name, data: (True, None, "ok"))
    out = asyncio.run(api.trigger_event(
        EventTriggerRequest(event_name="candidate_created", data={"organization_id": 1}),
        _http([("X-Trace-Id", "abc")]), db=None))
    assert out.success and queued == [] and not api._deliveries_in_flight
