"""The event endpoint queues webhooks without letting them affect the event."""
import asyncio

from fastapi import BackgroundTasks
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
                                        BackgroundTasks(), _http(), db=None))
    assert out.success and out.notification_id == 5


def test_queued_deliveries_are_sent_after_the_response(monkeypatch):
    monkeypatch.setattr(api, "_queue_webhooks", lambda name, data: [11, 12])
    monkeypatch.setattr(api, "handle_event", lambda db, event_name, data: (True, None, "ok"))
    tasks = BackgroundTasks()
    asyncio.run(api.trigger_event(EventTriggerRequest(event_name="candidate_created",
                                                      data={"organization_id": 1}), tasks, _http(), db=None))
    assert [(t.func, t.args) for t in tasks.tasks] == [(api.deliver_now, ([11, 12],))]


def test_events_that_came_through_the_gateway_never_reach_webhooks(monkeypatch):
    queued = []
    monkeypatch.setattr(api, "_queue_webhooks", lambda name, data: queued.append(name) or [1])
    monkeypatch.setattr(api, "handle_event", lambda db, event_name, data: (True, None, "ok"))
    tasks = BackgroundTasks()
    out = asyncio.run(api.trigger_event(
        EventTriggerRequest(event_name="candidate_created", data={"organization_id": 1}),
        tasks, _http([("X-Trace-Id", "abc")]), db=None))
    assert out.success and queued == [] and tasks.tasks == []
