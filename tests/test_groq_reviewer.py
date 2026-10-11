"""Mock the official SDK transport. All tests run without credentials/network."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import groq
import httpx
import pytest
from groq.types.chat import ChatCompletion

from auto_coding.integrations.groq import GroqReviewer, rate_blocked, rate_headers
from auto_coding.reviews import FileChange, ReviewConfig, ReviewError, encoded
from auto_coding.storage import TaskStore

from test_reviews import SHA, PATCH, packet, response


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request",
                        Mock(side_effect=AssertionError("Live HTTP forbidden")))


@pytest.fixture
def store(tmp_path, packet):
    with TaskStore(tmp_path / "tasks.sqlite3") as store:
        store.add_task(task_id=packet.task_id, title="Review task", prompt=packet.requirements,
                       model="codex-test", task_order=1,
                       acceptance_criteria=list(packet.acceptance_criteria), status="awaiting_review")
        store.record_commit(packet.task_id, SHA, packet.branch, None)
        yield store


def raw_reply(packet, *, data=None, finish="stop", model="openai/gpt-oss-120b", headers=None, usage=True):
    completion = ChatCompletion.model_validate({
        "id": "offline", "created": 1, "object": "chat.completion", "model": model,
        "choices": [{"index": 0, "finish_reason": finish, "message": {
            "role": "assistant", "content": data if isinstance(data, str) else encoded(data or response(packet)),
        }}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 25, "total_tokens": 125} if usage else None,
    })
    return Mock(headers=headers or {}, parse=Mock(return_value=completion))


def client_for(*items):
    client = Mock()
    client.with_options.return_value = client
    client.chat.completions.with_raw_response.create.side_effect = items
    return client


@pytest.mark.parametrize("decision", ["APPROVED", "REJECTED", "UNCERTAIN"])
def test_one_request_store_and_totals(store, packet, decision):
    data = response(packet, decision=decision,
                    blocking_issues=["Missing"] if decision == "REJECTED" else [])
    reply = raw_reply(packet, data=data, headers={
        "x-ratelimit-remaining-tokens": "4000", "Authorization": "private",
    })
    client = client_for(reply)
    result = GroqReviewer(store, client=client).review(packet)
    assert result.decision == decision
    assert result.eligible_to_proceed == (decision == "APPROVED")
    assert result.coverage_complete
    client.with_options.assert_called_once_with(timeout=45.0, max_retries=0)
    arguments = client.chat.completions.with_raw_response.create.call_args.kwargs
    assert arguments["model"] == "openai/gpt-oss-120b"
    assert arguments["reasoning_effort"] == "medium"
    assert arguments["response_format"]["json_schema"]["strict"]
    assert "tools" not in arguments
    history = store.reviews.get(result.review_id)
    assert history["decision"] == decision
    assert history["requests"][0]["submitted"] == arguments
    assert history["requests"][0]["response"]["choices"][0]["message"]["content"] == encoded(data)
    assert history["usage"]["total_tokens"] == 125
    usage = store.reviews.usage()
    assert usage["api_requests"] == 1 and usage["input_tokens"] == 100
    assert usage["output_tokens"] == 25 and usage["total_tokens"] == 125
    assert usage["account_remaining_quota"] is None and usage["estimate_only"]
    assert usage["provider_reported"]["x-ratelimit-remaining-tokens"] == "4000"
    assert "authorization" not in usage["provider_reported"]
    assert store.get_task(packet.task_id).status == "awaiting_review"
    assert store.get_commit(packet.task_id).commit_sha == SHA


@pytest.mark.parametrize("data", ["{", {"task_id": "TASK-TEST"}])
def test_invalid_response_stops(store, packet, data):
    client = client_for(raw_reply(packet, data=data))
    result = GroqReviewer(store, client=client).review(packet)
    assert result.decision == "UNCERTAIN" and not result.coverage_complete
    assert result.error
    assert client.chat.completions.with_raw_response.create.call_count == 1
    assert store.reviews.usage()["total_tokens"] == 125


@pytest.mark.parametrize("kwargs", [
    {"finish": "length"}, {"finish": "tool_calls"}, {"model": "different-model"},
    {"data": {"task_id": "wrong", "commit_sha": SHA, "decision": "APPROVED", "confidence": "HIGH",
              "requirements_checked": ["Works correctly"], "blocking_issues": [], "needs_final_review": []}},
    {"data": {"task_id": "TASK-TEST", "commit_sha": "a" * 12 + "b" * 28, "decision": "APPROVED",
              "confidence": "HIGH", "requirements_checked": ["Works correctly"], "blocking_issues": [],
              "needs_final_review": []}},
])
def test_provider_mismatch_truncation_fail_closed(store, packet, kwargs):
    result = GroqReviewer(store, client=client_for(raw_reply(packet, **kwargs))).review(packet)
    assert not result.eligible_to_proceed and result.error


@pytest.mark.parametrize("status,category", [
    (401, "authentication"), (403, "permission"), (429, "rate_limit"), (500, "provider_http_error"),
])
def test_api_http_errors_no_retry_or_secret_logging(store, packet, status, category, caplog):
    exception_cls = {401: groq.AuthenticationError, 403: groq.PermissionDeniedError,
                     429: groq.RateLimitError, 500: groq.InternalServerError}[status]
    error = exception_cls("Bearer " + "private" * 10, response=httpx.Response(
        status, request=httpx.Request("POST", "https://api.groq.com"),
        headers={"retry-after": "60", "Authorization": "secret"},
    ), body={"error": {"code": "rate_limit_exceeded"}})
    client = client_for(error)
    result = GroqReviewer(store, client=client).review(packet)
    assert result.decision == "UNCERTAIN" and result.error == category
    assert client.chat.completions.with_raw_response.create.call_count == 1
    history = store.reviews.get(result.review_id)
    assert "private" not in encoded(history) + caplog.text
    assert history["requests"][0]["rate_limits"] == {"retry-after": "60"}
    assert store.reviews.usage()["unknown_usage_requests"] == 1
    second = GroqReviewer(store, client=client).review(packet)
    assert second.error == "Provider rate limit/reset prevents another request"
    assert client.chat.completions.with_raw_response.create.call_count == 1


@pytest.mark.parametrize("kind", ["timeout", "network", "quota_exhausted", "unexpected_provider_error"])
def test_other_provider_failures(store, packet, kind):
    request = httpx.Request("POST", "https://api.groq.com")
    errors = {
        "timeout": groq.APITimeoutError(request=request),
        "network": groq.APIConnectionError(request=request),
        "quota_exhausted": groq.RateLimitError("quota", response=httpx.Response(429, request=request),
                                             body={"error": {"code": "insufficient_quota"}}),
        "unexpected_provider_error": RuntimeError("private error"),
    }
    result = GroqReviewer(store, client=client_for(errors[kind])).review(packet)
    assert result.error == kind and result.decision == "UNCERTAIN"
    assert "private error" not in encoded(store.reviews.get(result.review_id))


def large_packet(packet):
    header = PATCH[:PATCH.index("@@")]
    return replace(packet, changes=(FileChange("file.py", "M", header + "".join(
        f"@@ -{n},1 +{n},1 @@\n-" + "a" * 900 + "\n+" + "b" * 900 + "\n" for n in range(1, 4)
    )),))


@pytest.mark.parametrize("decisions,expected", [
    (["APPROVED"] * 3, "APPROVED"),
    (["APPROVED", "REJECTED", "APPROVED"], "REJECTED"),
    (["APPROVED", "UNCERTAIN", "APPROVED"], "UNCERTAIN"),
])
def test_multi_chunk_aggregation(store, packet, decisions, expected):
    packet = large_packet(packet)
    client = client_for(*(raw_reply(packet, data=response(
        packet, decision=decision, confidence="HIGH" if index == 0 else "MEDIUM",
        blocking_issues=["Bug", "Bug"] if decision == "REJECTED" else [],
        needs_final_review=["Inspect recovery", "Inspect recovery"],
    )) for index, decision in enumerate(decisions)))
    result = GroqReviewer(store, ReviewConfig(request_budget=8000, max_review_tokens=100000), client=client).review(packet)
    assert result.decision == expected
    assert result.confidence == ("LOW" if expected == "REJECTED" else "MEDIUM")
    assert result.needs_final_review.count("Inspect recovery") == 1
    assert "cross-file" in result.needs_final_review[-1]
    history = store.reviews.get(result.review_id)
    assert history["chunk_count"] == 3
    assert len(history["requests"]) == (2 if expected == "REJECTED" else 3)
    assert history["usage"]["total_tokens"] == len(history["requests"]) * 125
    assert len({s for chunk in history["chunks"] for s in chunk["coverage"]}) == 3
    assert result.eligible_to_proceed == (expected == "APPROVED")


def test_missing_chunk_or_rate_budget_is_uncertain(store, packet):
    packet = large_packet(packet)
    client = client_for(raw_reply(packet, headers={
        "x-ratelimit-remaining-tokens": "0", "x-ratelimit-reset-tokens": "1m0s",
    }))
    result = GroqReviewer(store, ReviewConfig(request_budget=8000, max_review_tokens=100000), client=client).review(packet)
    assert result.decision == "UNCERTAIN" and not result.coverage_complete
    assert client.chat.completions.with_raw_response.create.call_count == 1


def test_malformed_second_chunk(store, packet):
    packet = large_packet(packet)
    client = client_for(raw_reply(packet), raw_reply(packet, data="{"))
    result = GroqReviewer(store, ReviewConfig(request_budget=8000, max_review_tokens=100000), client=client).review(packet)
    assert result.decision == "UNCERTAIN" and not result.coverage_complete
    assert client.chat.completions.with_raw_response.create.call_count == 2


def test_sensitive_input_and_output_not_sent_or_stored(store, packet):
    secret = "gsk_" + "a" * 40
    packet = replace(packet, changes=(FileChange("file.py", "M", PATCH.replace("+new", "+" + secret)),))
    client = client_for()
    result = GroqReviewer(store, client=client).review(packet)
    assert result.decision == "UNCERTAIN"
    client.with_options.assert_not_called()
    assert secret not in encoded(store.reviews.get(result.review_id))
    packet = replace(packet, changes=(FileChange("file.py", "M", PATCH),))
    client = client_for(raw_reply(packet, data=response(packet, needs_final_review=[secret])))
    result = GroqReviewer(store, client=client).review(packet)
    assert result.decision == "UNCERTAIN"
    assert secret not in encoded(store.reviews.get(result.review_id))
    assert store.reviews.get(result.review_id)["requests"][0]["response_redacted"]


def test_missing_key_no_network_or_counted_request(store, packet):
    result = GroqReviewer(store).review(packet)
    assert result.decision == "UNCERTAIN"
    assert result.error == "Groq authentication configuration unavailable"
    assert store.reviews.usage()["api_requests"] == 0


def test_history_persists_reopen_and_privacy_boundary(store, packet, tmp_path):
    result = GroqReviewer(store, client=client_for(raw_reply(packet))).review(packet)
    expected = store.reviews.get(result.review_id)
    with TaskStore(tmp_path / "tasks.sqlite3") as reopened:
        assert reopened.reviews.get(result.review_id) == expected
        assert reopened.reviews.list(packet.task_id) == [expected]
        assert reopened.reviews.list("missing") == []
        assert reopened.reviews.get("missing") is None
        assert reopened.reviews.usage()["total_tokens"] == 125
    secret = "ghp_" + "Z" * 40
    other = store.reviews.create(packet.task_id, SHA, {"secret_value": secret})
    assert secret not in encoded(store.reviews.get(other))


def test_persistence_failure_cannot_return_approval(store, packet, monkeypatch):
    monkeypatch.setattr(store.reviews, "finish", Mock(side_effect=RuntimeError("disk failure")))
    with pytest.raises(RuntimeError, match="disk failure"):
        GroqReviewer(store, client=client_for(raw_reply(packet))).review(packet)
    assert store.reviews.list()[0]["decision"] == "UNCERTAIN"


def test_unknown_token_usage_not_invented(store, packet):
    result = GroqReviewer(store, client=client_for(raw_reply(packet, usage=False))).review(packet)
    assert result.decision == "APPROVED"
    usage = store.reviews.usage()
    assert usage["total_tokens"] == 0 and usage["unknown_usage_requests"] == 1


@pytest.mark.parametrize("limits,blocked", [
    ({"retry-after": "60"}, True), ({"retry-after": "0"}, False),
    ({"x-ratelimit-remaining-tokens": "0"}, True),
    ({"x-ratelimit-remaining-tokens": "0", "x-ratelimit-reset-tokens": "2m59.56s"}, True),
    ({"x-ratelimit-remaining-tokens": "0", "x-ratelimit-reset-tokens": "0s"}, False),
    ({"x-ratelimit-remaining-tokens": "many"}, True),
])
def test_provider_limits(limits, blocked):
    assert rate_blocked({"observed_at": datetime.now(timezone.utc).isoformat(), **limits}, 100) == blocked


def test_date_retry_after_and_allowlisted_headers():
    from email.utils import format_datetime
    future = format_datetime(datetime.now(timezone.utc) + timedelta(hours=1))
    assert rate_blocked({"observed_at": datetime.now(timezone.utc).isoformat(), "retry-after": future}, 1)
    assert rate_headers({"Authorization": "private", "Retry-After": "4"}) == {"retry-after": "4"}


@pytest.mark.parametrize("effort", ["low", "medium", "high"])
def test_configured_effort_and_timeout(store, packet, effort):
    client = client_for(raw_reply(packet))
    config = ReviewConfig(reasoning_effort=effort, timeout=12.5)
    assert GroqReviewer(store, config, client=client).review(packet).eligible_to_proceed
    assert client.chat.completions.with_raw_response.create.call_args.kwargs["reasoning_effort"] == effort
    client.with_options.assert_called_once_with(timeout=12.5, max_retries=0)


def test_sdk_real_serialization_mock_transport_no_live_http(store, packet):
    calls = []
    payload = raw_reply(packet).parse().model_dump(mode="json")

    def handle(request):
        calls.append(request)
        body = __import__("json").loads(request.content)
        assert body["model"] == "openai/gpt-oss-120b"
        assert body["reasoning_effort"] == "medium"
        assert body["response_format"]["json_schema"]["strict"] is True
        assert body["store"] is False and body["include_reasoning"] is False
        assert request.extensions["timeout"]["read"] == 45.0
        return httpx.Response(200, json=payload, headers={"x-ratelimit-remaining-requests": "42"})

    with httpx.Client(transport=httpx.MockTransport(handle)) as http:
        with groq.Groq(api_key="offline", http_client=http) as client:
            result = GroqReviewer(store, client=client).review(packet)
    assert result.decision == "APPROVED" and len(calls) == 1
    history = store.reviews.get(result.review_id)
    assert history["requests"][0]["response_text"]
    assert history["requests"][0]["rate_limits"]["x-ratelimit-remaining-requests"] == "42"


def test_sdk_default_retries_overridden_on_error(store, packet):
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(429, json={"error": {"code": "insufficient_quota"}},
                              headers={"retry-after": "15"})

    with httpx.Client(transport=httpx.MockTransport(handle)) as http:
        with groq.Groq(api_key="offline", http_client=http, max_retries=2) as client:
            result = GroqReviewer(store, client=client).review(packet)
    assert result.error == "quota_exhausted" and len(calls) == 1


def test_all_acceptance_criteria_must_be_checked(store, packet):
    store._connection.execute("UPDATE tasks SET acceptance_criteria = ? WHERE task_id = ?",
                              ('["Works correctly","Also safe"]', packet.task_id))
    store._connection.commit()
    packet = replace(packet, acceptance_criteria=("Works correctly", "Also safe"))
    reply = raw_reply(packet, data=response(packet, requirements_checked=["Works correctly"]))
    result = GroqReviewer(store, client=client_for(reply)).review(packet)
    assert result.decision == "UNCERTAIN" and result.coverage_complete
    assert not result.eligible_to_proceed


def test_task_drift_during_request_cannot_approve_or_overwrite_state(store, packet):
    reply = raw_reply(packet)

    def changed():
        store.update_status(packet.task_id, "failed")
        return reply.parse.return_value

    reply.parse.side_effect = changed
    result = GroqReviewer(store, client=client_for(reply)).review(packet)
    assert result.decision == "UNCERTAIN" and "state changed" in result.error
    assert store.get_task(packet.task_id).status == "failed"


def test_binding_mismatch_does_not_send(store, packet):
    client = client_for()
    with pytest.raises(ReviewError):
        GroqReviewer(store, client=client).review(replace(packet, commit_sha="b" * 40))
    client.with_options.assert_not_called()
    assert store.reviews.list() == []


def test_pending_request_crash_usage_is_unknown_not_approval(store, packet):
    review_id = store.reviews.create(packet.task_id, SHA, {"decision": "UNCERTAIN"})
    store.reviews.start_request(review_id, 1, {"model": "openai/gpt-oss-120b",
                                             "error": "Response missing", "completed": False})
    assert store.reviews.get(review_id)["decision"] == "UNCERTAIN"
    usage = store.reviews.usage()
    assert usage["api_requests"] == usage["unknown_usage_requests"] == 1
    assert usage["total_tokens"] == 0


def test_usage_explicit_period_and_malformed_provider_usage(store, packet):
    reply = raw_reply(packet)
    reply.parse.return_value.usage.total_tokens = 999
    result = GroqReviewer(store, client=client_for(reply)).review(packet)
    assert store.reviews.get(result.review_id)["unknown_usage_requests"] == 1
    assert store.reviews.usage(start="2000-01-01T00:00:00+00:00",
                               end="2000-01-02T00:00:00+00:00")["api_requests"] == 0


def test_sensitive_configuration_cannot_leak_to_logs():
    with pytest.raises(ReviewError, match="Sensitive"):
        ReviewConfig(model="gsk_" + "A" * 40)
