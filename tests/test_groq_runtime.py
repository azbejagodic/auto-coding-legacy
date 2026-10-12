"""Real saved rate metadata and offline provider-sized complete reviews."""

from datetime import datetime, timedelta, timezone
import json
from unittest.mock import Mock

import pytest

from auto_coding.integrations import groq as integration
from auto_coding.integrations.groq import GroqReviewer, rate_blocked, rate_headers
from auto_coding.integrations.groq_limits import provider_plan, request_tokens, tokenizer
from auto_coding.reviews import ReviewConfig, ReviewError, request_arguments, template

from test_groq_reviewer import store, packet, offline, client_for, raw_reply
from test_review_budgeting import practical_packet, assert_complete


HEADERS = {
    "x-ratelimit-limit-tokens": "8000", "x-ratelimit-remaining-tokens": "8000",
    "x-ratelimit-limit-requests": "1000", "x-ratelimit-remaining-requests": "1000",
    "retry-after": "2", "x-ratelimit-reset-tokens": "1ms",
    "x-ratelimit-reset-requests": "1ms",
}


def old_limits(**changes):
    return {**HEADERS, "observed_at": (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat(), **changes}


def test_exact_legacy_headers_expire_without_comparing_bytes_to_tokens():
    assert rate_headers(HEADERS) == HEADERS
    assert integration._seconds("1ms") == .001
    assert integration._seconds("2m59.5s") == 179.5
    assert integration._seconds("1s50ms") == 1.05
    assert 29405 > 8000  # Old incompatible comparison; this is NOT the required token count.
    assert not rate_blocked(old_limits(), 7000)
    assert rate_blocked(old_limits(), 8001)  # A reset never expands the provider capacity.


@pytest.mark.parametrize("observed", [None, "not-a-date", "2026-01-01T00:00:00"])
def test_missing_or_naive_timestamps_fail_closed(observed):
    limits = {**HEADERS}
    if observed is not None:
        limits["observed_at"] = observed
    assert rate_blocked(limits, 3000)


def test_active_and_expired_token_and_retry_restrictions():
    now = datetime.now(timezone.utc).isoformat()
    assert rate_blocked(old_limits(observed_at=now), 7000)
    depleted = old_limits(**{"x-ratelimit-remaining-tokens": "0"})
    assert not rate_blocked(depleted, 7000)
    assert rate_blocked({**depleted, "observed_at": now}, 7000)
    assert rate_blocked({**depleted, "x-ratelimit-reset-tokens": "unknown"}, 7000)
    assert rate_blocked(old_limits(observed_at=(datetime.now(timezone.utc) + timedelta(days=1)).isoformat()), 1000)


def test_observation_timestamp_is_not_rebased_or_overridden(store, packet):
    review = store.reviews.create(packet.task_id, packet.commit_sha, {})
    observed = old_limits()["observed_at"]
    store.reviews.start_request(review, 1, {"model": "openai/gpt-oss-120b", "completed": True,
        "completed_at": datetime.now(timezone.utc).isoformat(), "rate_limits_observed_at": observed,
        "rate_limits": {**HEADERS, "observed_at": "untrusted"}})
    assert store.reviews.latest_limits("openai/gpt-oss-120b")["observed_at"] == observed
    assert store.reviews.usage()["provider_reported"]["observed_at"] == observed
    assert not rate_blocked(store.reviews.latest_limits("openai/gpt-oss-120b"), 7000)


def test_tokenizer_is_local_and_handles_literal_special_tokens(monkeypatch):
    import requests
    monkeypatch.setattr(requests, "get", Mock(side_effect=AssertionError("Tokenizer network forbidden")))
    tokenizer.cache_clear()
    codec = tokenizer()
    assert len(codec.encode_ordinary("hello world")) == 2
    text = "č Unicode <|start|> literal"
    assert codec.decode(codec.encode_ordinary(text)) == text


def test_practical_commit_is_split_to_fit_real_token_capacity(practical_packet):
    packet = practical_packet
    config = ReviewConfig()
    planned = provider_plan(packet, config, old_limits())
    assert 1 < len(planned.chunks) <= config.max_chunks
    # Reuse lossless reconstruction checks, not the legacy total wire-byte cap.
    from dataclasses import replace
    assert_complete(packet, planned.chunks, replace(config, max_review_tokens=1000000))
    sizes = [request_tokens(request_arguments(config, template(), c.content)) for c in planned.chunks]
    assert max(sizes) <= 8000 and sum(sizes) <= config.max_review_tokens
    assert planned.diagnostics.estimated_total_tokens == sum(sizes)
    assert planned.diagnostics.largest_request_tokens == max(sizes)
    assert planned.diagnostics.estimated_total_units > config.max_review_tokens
    assert provider_plan(packet, config, old_limits()) == planned


def test_impossible_provider_capacity_preserves_essential_evidence(practical_packet):
    with pytest.raises(ReviewError, match="Essential review evidence"):
        provider_plan(practical_packet, ReviewConfig(), {"x-ratelimit-limit-tokens": "1000"})


def test_larger_configured_margin_is_counted(packet):
    config = ReviewConfig(headroom=1024)
    arguments = request_arguments(config, template(), "content")
    assert request_tokens(arguments, headroom=config.headroom) == request_tokens(arguments) + 512


def test_sequential_multichunk_review_waits_once_for_reset_without_retries(store, packet, monkeypatch):
    from test_groq_reviewer import large_packet
    packet = large_packet(packet)
    current = [datetime.now(timezone.utc)]
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return current[0]
    sleeps = []
    def advance(seconds):
        sleeps.append(seconds)
        current[0] += timedelta(seconds=seconds + .001)
    monkeypatch.setattr(integration, "datetime", Clock)
    monkeypatch.setattr(integration, "utc_now", lambda: current[0].isoformat())
    monkeypatch.setattr(integration.time, "sleep", advance)
    client = client_for(raw_reply(packet, headers={"x-ratelimit-limit-tokens": "8000",
        "x-ratelimit-remaining-tokens": "0", "x-ratelimit-reset-tokens": "20ms"}),
        raw_reply(packet), raw_reply(packet))
    result = GroqReviewer(store, ReviewConfig(request_budget=8000, max_review_tokens=30000), client=client).review(packet)
    assert result.eligible_to_proceed and result.coverage_complete
    assert len(sleeps) == 1 and 0 < sleeps[0] <= .02
    assert client.chat.completions.with_raw_response.create.call_count == 3
    record = store.reviews.get(result.review_id)
    assert all(q["max_retries"] == 0 and q["estimated_provider_tokens"] <= 8000 for q in record["requests"])


def test_unknown_reset_or_active_first_request_cannot_approve(store, packet):
    old = store.reviews.create(packet.task_id, packet.commit_sha, {})
    store.reviews.start_request(old, 1, {"model": "openai/gpt-oss-120b", "rate_limits": HEADERS})
    client = client_for(raw_reply(packet))
    result = GroqReviewer(store, client=client).review(packet)
    assert result.decision == "UNCERTAIN" and not result.eligible_to_proceed
    assert result.error == "Provider rate limit/reset prevents another request"
    assert store.reviews.get(result.review_id)["requests"] == []
    client.chat.completions.with_raw_response.create.assert_not_called()


@pytest.mark.parametrize("kind", ["safe", "unknown_delivery", "finding", "tool_call"])
def test_only_complete_approved_prefix_can_be_rechecked_after_local_block(store, packet, monkeypatch, kind):
    from copy import deepcopy
    from test_groq_reviewer import large_packet
    from auto_coding.review_recovery import _local_failure
    packet = large_packet(packet)
    client = client_for(raw_reply(packet))
    monkeypatch.setattr(integration, "wait_for_capacity", lambda *args: False)
    result = GroqReviewer(store, ReviewConfig(request_budget=8000), client=client).review(packet)
    assert result.decision == "UNCERTAIN" and not result.eligible_to_proceed
    record = deepcopy(store.reviews.get(result.review_id))
    assert record["api_requests"] == 1 < record["chunk_count"]
    if kind == "safe":
        _local_failure(record, packet)
    else:
        if kind == "unknown_delivery":
            record["requests"][0]["completed"] = False
        elif kind == "tool_call":
            record["requests"][0]["response"]["choices"][0]["message"]["tool_calls"] = [{"id": "unexpected"}]
        else:
            response = record["requests"][0]["response"]["choices"][0]["message"]
            content = json.loads(response["content"])
            content["needs_final_review"] = ["Unresolved finding"]
            response["content"] = json.dumps(content)
        with pytest.raises(ValueError):
            _local_failure(record, packet)
    client.chat.completions.with_raw_response.create.assert_called_once()
