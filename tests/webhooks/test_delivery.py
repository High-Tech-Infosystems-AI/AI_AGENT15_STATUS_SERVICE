"""Fan-out, signing, retries and switching off of outgoing webhooks."""
import hashlib
import hmac
import json
from datetime import datetime, timedelta

from app.webhooks_layer import delivery
from app.webhooks_layer.models import WebhookDelivery as D, WebhookEndpoint as E
from app.whatsapp_layer import crypto

NOW = datetime(2026, 11, 18, 10, 0, 0)
SECRET = "whsec_test_secret"


def _endpoint(db, org=1, events=("candidate_created",), url="https://hooks.example.test/in", active=1):
    e = E(organization_id=org, name="CRM", url=url, events=list(events), is_active=active,
          secret_enc=crypto.encrypt(SECRET), secret_last4=SECRET[-4:], consecutive_failures=0)
    db.add(e)
    db.commit()
    return e


class Recorder:
    def __init__(self, *results):
        self.results = list(results) or [(200, "ok", None, 5)]
        self.calls = []

    def __call__(self, url, body, headers):
        self.calls.append((url, body, headers))
        return self.results.pop(0) if len(self.results) > 1 else self.results[0]


def test_event_goes_only_to_subscribed_active_endpoints_of_its_org(wh_db):
    wanted = _endpoint(wh_db)
    _endpoint(wh_db, events=("job_created",))
    _endpoint(wh_db, org=2)
    _endpoint(wh_db, active=0)
    every = _endpoint(wh_db, events=("*",))
    ids = delivery.fan_out(wh_db, "candidate_created", {"organization_id": "1", "candidate_id": 7}, now=NOW)
    rows = wh_db.query(D).filter(D.id.in_(ids)).all()
    assert {r.endpoint_id for r in rows} == {wanted.id, every.id}
    assert len({r.event_id for r in rows}) == 1


def test_unknown_or_security_events_and_events_without_org_are_not_sent(wh_db):
    _endpoint(wh_db, events=("*",))
    assert delivery.fan_out(wh_db, "user_login_success", {"organization_id": 1}, now=NOW) == []
    assert delivery.fan_out(wh_db, "forgot_password_otp_sent", {"organization_id": 1}, now=NOW) == []
    assert delivery.fan_out(wh_db, "candidate_created", {"candidate_id": 7}, now=NOW) == []


def test_payload_drops_credential_like_fields(wh_db):
    _endpoint(wh_db)
    [delivery_id] = delivery.fan_out(wh_db, "candidate_created", {
        "organization_id": 1, "candidate_id": 7, "password": "x", "meta": {"api_key": "k", "name": "A"},
    }, now=NOW)
    payload = json.loads(wh_db.get(D, delivery_id).payload)
    assert payload["type"] == "candidate_created" and payload["organization_id"] == 1
    assert payload["data"] == {"organization_id": 1, "candidate_id": 7, "meta": {"name": "A"}}


def test_successful_delivery_is_signed_with_the_endpoint_secret(wh_db):
    e = _endpoint(wh_db)
    [delivery_id] = delivery.fan_out(wh_db, "candidate_created", {"organization_id": 1}, now=NOW)
    post = Recorder()
    d = delivery.attempt(wh_db, delivery_id, now=NOW, post=post)
    assert d.status == "succeeded" and d.attempts == 1 and d.response_status == 200
    url, body, headers = post.calls[0]
    assert url == e.url and headers["X-HRMIS-Event"] == "candidate_created"
    expected = hmac.new(SECRET.encode(), f"{headers['X-HRMIS-Timestamp']}.".encode() + body,
                        hashlib.sha256).hexdigest()
    assert headers["X-HRMIS-Signature"] == "sha256=" + expected
    assert wh_db.get(E, e.id).last_success_at == NOW


def test_failures_back_off_then_give_up(wh_db):
    _endpoint(wh_db)
    [delivery_id] = delivery.fan_out(wh_db, "candidate_created", {"organization_id": 1}, now=NOW)
    post = Recorder((500, "boom", None, 3))
    d = delivery.attempt(wh_db, delivery_id, now=NOW, post=post)
    assert d.status == "retrying" and d.next_attempt_at == NOW + timedelta(seconds=60)
    assert d.error == "The endpoint answered 500."
    now = NOW
    for _ in range(delivery.MAX_ATTEMPTS - 1):
        now = d.next_attempt_at
        delivery.run_due(wh_db, now=now, post=post)
        d = wh_db.get(D, delivery_id)
    assert d.status == "failed" and d.attempts == delivery.MAX_ATTEMPTS and d.next_attempt_at is None
    # Not sent again once it has given up.
    delivery.run_due(wh_db, now=now + timedelta(days=1), post=post)
    assert len(post.calls) == delivery.MAX_ATTEMPTS


def test_nothing_is_sent_before_its_retry_time(wh_db):
    _endpoint(wh_db)
    [delivery_id] = delivery.fan_out(wh_db, "candidate_created", {"organization_id": 1}, now=NOW)
    post = Recorder((None, "", "No response within 10 seconds.", 10000))
    delivery.attempt(wh_db, delivery_id, now=NOW, post=post)
    delivery.run_due(wh_db, now=NOW + timedelta(seconds=30), post=post)
    assert len(post.calls) == 1


def test_a_claimed_delivery_is_not_sent_twice(wh_db):
    _endpoint(wh_db)
    [delivery_id] = delivery.fan_out(wh_db, "candidate_created", {"organization_id": 1}, now=NOW)
    post = Recorder()
    assert delivery.attempt(wh_db, delivery_id, now=NOW, post=post) is not None
    assert delivery.attempt(wh_db, delivery_id, now=NOW, post=post) is None
    assert len(post.calls) == 1


def test_a_stuck_claim_is_released_and_retried(wh_db):
    _endpoint(wh_db)
    [delivery_id] = delivery.fan_out(wh_db, "candidate_created", {"organization_id": 1}, now=NOW)
    d = wh_db.get(D, delivery_id)
    d.status, d.claimed_at = "sending", NOW
    wh_db.commit()
    post = Recorder()
    out = delivery.run_due(wh_db, now=NOW + timedelta(minutes=11), post=post)
    assert out["released"] == 1 and out["succeeded"] == 1


def test_endpoint_switches_off_after_repeated_failures(wh_db, monkeypatch):
    monkeypatch.setattr(delivery, "AUTO_DISABLE_AFTER", 3)
    e = _endpoint(wh_db)
    post = Recorder((502, "", None, 1))
    for _ in range(3):
        [delivery_id] = delivery.fan_out(wh_db, "candidate_created", {"organization_id": 1}, now=NOW)
        delivery.attempt(wh_db, delivery_id, now=NOW, post=post)
    e = wh_db.get(E, e.id)
    assert e.is_active == 0 and "3 failed deliveries" in e.disabled_reason
    assert delivery.fan_out(wh_db, "candidate_created", {"organization_id": 1}, now=NOW) == []


def test_success_resets_the_failure_count(wh_db):
    e = _endpoint(wh_db)
    e.consecutive_failures = 7
    wh_db.commit()
    [delivery_id] = delivery.fan_out(wh_db, "candidate_created", {"organization_id": 1}, now=NOW)
    delivery.attempt(wh_db, delivery_id, now=NOW, post=Recorder())
    assert wh_db.get(E, e.id).consecutive_failures == 0


def test_private_urls_are_refused_at_send_time(wh_db):
    _endpoint(wh_db, url="https://internal.example.test/hook")
    [delivery_id] = delivery.fan_out(wh_db, "candidate_created", {"organization_id": 1}, now=NOW)
    post = Recorder()
    d = delivery.attempt(wh_db, delivery_id, now=NOW, post=post)
    assert post.calls == [] and d.status == "retrying" and "private network" in d.error


def test_ping_is_one_shot_and_does_not_count_towards_switching_off(wh_db):
    e = _endpoint(wh_db)
    d = delivery.ping(wh_db, e, now=NOW, post=Recorder((404, "nope", None, 2)))
    assert d.event_name == "webhook.ping" and d.status == "failed" and d.max_attempts == 1
    assert wh_db.get(E, e.id).consecutive_failures == 0


def test_redeliver_keeps_the_event_id(wh_db):
    _endpoint(wh_db)
    [delivery_id] = delivery.fan_out(wh_db, "candidate_created", {"organization_id": 1}, now=NOW)
    original = wh_db.get(D, delivery_id)
    again = delivery.redeliver(wh_db, original, now=NOW)
    assert again.id != original.id and again.event_id == original.event_id and again.status == "pending"


def test_cleanup_removes_only_old_finished_deliveries(wh_db):
    _endpoint(wh_db)
    old = NOW - timedelta(days=31)
    for status in ("succeeded", "failed", "retrying"):
        wh_db.add(D(endpoint_id=1, organization_id=1, event_id="evt", event_name="candidate_created",
                    payload="{}", status=status, created_at=old))
    wh_db.commit()
    assert delivery.cleanup(wh_db, now=NOW) == 2
    assert wh_db.query(D).one().status == "retrying"
