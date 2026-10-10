"""Offline transport tests: no real browser, account, or model execution."""

import asyncio
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from playwright.async_api import TimeoutError as PlaywrightTimeout

from auto_coding.integrations.chatgpt import (
    ChatGPTTransport, ConversationKind, SubmissionState, TransportStatus as S,
    _conversation_url, _validate_profile,
)
from auto_coding.workspace_lock import WorkspaceLock


URL = "https://chatgpt.com/c/approved-test"


def message(identity, role, text, complete=False):
    return {"id": identity, "role": role, "text": text, "complete": complete}


class FakePage:
    def __init__(self):
        self.url = "about:blank"
        self.closed = False
        self.main_frame = object()
        self.listeners = {}
        self.destinations = []
        self.view = {
            "url": self.url, "document_id": "document-1",
            "authenticated": True, "login_required": False, "temporary": False,
            "main_count": 1, "composer_count": 1, "composer_usable": True,
            "composer_text": "", "send_count": 1, "send_usable": True,
            "generating": False, "error": False,
            "messages": [message("old-user", "user", "old question"),
                         message("old-answer", "assistant", "old answer", True)],
        }

    def is_closed(self):
        return self.closed

    def on(self, name, callback):
        self.listeners[name] = callback

    async def goto(self, url, **kwargs):
        self.destinations.append(url)
        self.url = self.view["url"] = url
        callback = self.listeners.get("framenavigated")
        if callback:
            callback(self.main_frame)


class Harness:
    def __init__(self, tmp_path, **options):
        self.page = FakePage()
        self.context = SimpleNamespace(pages=[self.page], close=AsyncMock(), new_page=AsyncMock())
        self.driver = SimpleNamespace(
            chromium=SimpleNamespace(launch_persistent_context=AsyncMock(return_value=self.context)),
            stop=AsyncMock(),
        )
        self.factory = lambda: SimpleNamespace(start=AsyncMock(return_value=self.driver))
        self.ui = SimpleNamespace(
            inspect=AsyncMock(side_effect=lambda page: deepcopy(page.view)),
            wait_ready=AsyncMock(side_effect=lambda page, timeout: deepcopy(page.view)),
            fill=AsyncMock(side_effect=self.fill), submit=AsyncMock(return_value={"sent": True}),
            collect=AsyncMock(side_effect=self.complete), progress=AsyncMock(return_value={"confirmed": False}),
            dispose=AsyncMock(),
        )
        self.profile = tmp_path / "profiles" / "Ažbe Č Unicode"
        self.lock_root = tmp_path / "locks"
        self.transport = self.make_transport(**options)

    def make_transport(self, **options):
        return ChatGPTTransport(
            self.profile, ui=self.ui, playwright_factory=self.factory,
            lock_factory=lambda profile: WorkspaceLock(profile, lock_directory=self.lock_root),
            **options,
        )

    async def fill(self, page, prompt, timeout):
        page.view["composer_text"] = prompt

    async def complete(self, page, operation_id, timeout):
        prompt = page.view["composer_text"]
        page.view["messages"] += [
            message(operation_id + "-user", "user", prompt),
            message(operation_id + "-answer", "assistant", "complete visible response", True),
        ]
        page.view["composer_text"] = ""
        return {
            "status": "complete", "confirmed": True, "text": "complete visible response",
            "user_id": operation_id + "-user", "response_id": operation_id + "-answer",
            "messages": deepcopy(page.view["messages"]),
        }

    async def ready(self):
        assert (await self.transport.start()).status == S.READY
        assert (await self.transport.select_conversation(URL)).status == S.SELECTED


def test_authenticated_startup_unicode_owned_profile_cleanup(tmp_path):
    async def scenario():
        h = Harness(tmp_path)
        assert (await h.transport.start()).status == S.READY
        args = h.driver.chromium.launch_persistent_context.call_args.kwargs
        assert args["user_data_dir"] == str(h.profile.resolve())
        assert args["headless"] is False and args["chromium_sandbox"] is True
        assert args["accept_downloads"] is False
        assert h.transport.page is h.page
        assert (await h.transport.close()).status == S.CLOSED
        h.context.close.assert_awaited_once()
        h.driver.stop.assert_awaited_once()
        assert (h.profile / ".auto-coding-profile").is_file()
        assert not (h.profile / ".auto-coding-operation.json").exists()
    asyncio.run(scenario())


def test_login_is_manual_and_can_resume_without_restart(tmp_path):
    async def scenario():
        h = Harness(tmp_path)
        h.page.view.update(login_required=True, authenticated=False)
        result = await h.transport.start()
        assert result.status == S.LOGIN_REQUIRED and result.requires_manual_action
        h.context.close.assert_not_awaited()
        h.ui.submit.assert_not_awaited()
        h.page.view.update(login_required=False, authenticated=True)
        assert (await h.transport.select_conversation(URL)).status == S.SELECTED
        await h.transport.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("url", [
    "", "http://chatgpt.com/c/id", "https://chatgpt.com/", "https://chatgpt.com/share/id",
    "https://evil.invalid/c/id", "https://chatgpt.com.evil.invalid/c/id",
    "https://user:secret@chatgpt.com/c/id", URL + "?token=secret", URL + "#fragment",
    "https://chatgpt.com:443/c/id", "https://chatgpt.com/c/../other", None,
])
def test_invalid_conversation_urls_rejected_without_navigation(tmp_path, url):
    async def scenario():
        h = Harness(tmp_path)
        await h.transport.start()
        destinations = list(h.page.destinations)
        assert (await h.transport.select_conversation(url)).status == S.INVALID_INPUT
        assert h.page.destinations == destinations
        assert (await h.transport.send_message("test")).status == S.CONVERSATION_UNAVAILABLE
        h.ui.submit.assert_not_awaited()
        await h.transport.close()
    asyncio.run(scenario())


def test_canonical_project_conversation_url():
    assert _conversation_url("https://chatgpt.com/g/project-id/c/conversation-id/") == (
        "https://chatgpt.com/g/project-id/c/conversation-id"
    )


def test_normal_conversation_reuse_and_full_text(tmp_path):
    async def scenario():
        h = Harness(tmp_path)
        await h.ready()
        prompt = "  Ažbe\n" + ("long Unicode 🐍 content\n" * 500) + " trailing  "
        first = await h.transport.send_message(prompt)
        second = await h.transport.send_message("another question")
        assert first.completed and second.completed
        assert first.response_text == "complete visible response" != "old answer"
        assert first.submission == SubmissionState.CONFIRMED
        assert first.conversation_url == second.conversation_url == URL
        assert first.operation_id != second.operation_id
        assert first.response_message_id != second.response_message_id
        assert first.duration_seconds >= 0
        assert h.ui.fill.call_args_list[0].args[1] == prompt
        assert h.ui.submit.await_count == h.ui.collect.await_count == 2
        assert h.page.destinations == ["https://chatgpt.com/", URL]
        assert not (h.profile / ".auto-coding-operation.json").exists()
        await h.transport.close()
    asyncio.run(scenario())


def test_redirect_does_not_fallback_to_new_conversation(tmp_path):
    async def scenario():
        h = Harness(tmp_path)
        await h.transport.start()
        h.ui.wait_ready.side_effect = lambda page, timeout: dict(page.view, url="https://chatgpt.com/")
        assert (await h.transport.select_conversation(URL)).status == S.CONVERSATION_UNAVAILABLE
        assert (await h.transport.send_message("test")).status == S.CONVERSATION_UNAVAILABLE
        h.ui.submit.assert_not_awaited()
        assert h.page.destinations == ["https://chatgpt.com/", URL]
        await h.transport.close()
    asyncio.run(scenario())


def test_temporary_chat_is_active_bound_and_has_no_recoverable_url(tmp_path):
    async def scenario():
        h = Harness(tmp_path)
        await h.transport.start()
        h.page.view["temporary"] = True
        before = list(h.page.destinations)
        selected = await h.transport.select_temporary_chat()
        assert selected.status == S.SELECTED
        assert selected.conversation_kind == ConversationKind.TEMPORARY
        assert selected.conversation_url is None
        assert h.page.destinations == before
        result = await h.transport.send_message("harmless")
        assert result.completed and result.conversation_url is None
        await h.page.goto("https://chatgpt.com/")
        assert (await h.transport.send_message("do not send")).status == S.CONVERSATION_UNAVAILABLE
        assert h.ui.submit.await_count == 1
        await h.transport.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("changes", [
    {"temporary": False}, {"temporary": True, "messages": []},
    {"temporary": True, "messages": [message("u", "user", "no answer")]},
    {"temporary": True, "url": "https://evil.invalid/"},
    {"temporary": True, "messages": [message("a", "assistant", "partial", False)]},
])
def test_temporary_chat_requires_completed_visible_anchor(tmp_path, changes):
    async def scenario():
        h = Harness(tmp_path)
        await h.transport.start()
        h.page.view.update(changes)
        assert (await h.transport.select_temporary_chat()).status == S.CONVERSATION_UNAVAILABLE
        h.ui.submit.assert_not_awaited()
        await h.transport.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("text", ["", " \n ", "bad\x00text", None, 1])
def test_invalid_message_never_prepared_or_sent(tmp_path, text):
    async def scenario():
        h = Harness(tmp_path)
        await h.ready()
        assert (await h.transport.send_message(text)).status == S.INVALID_INPUT
        h.ui.fill.assert_not_awaited()
        h.ui.submit.assert_not_awaited()
        await h.transport.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("changes,status", [
    ({"login_required": True}, S.LOGIN_REQUIRED),
    ({"authenticated": False}, S.INTERFACE_UNRECOGNIZED),
    ({"main_count": 2}, S.INTERFACE_UNRECOGNIZED),
    ({"composer_count": 0}, S.COMPOSER_UNAVAILABLE),
    ({"composer_count": 2}, S.COMPOSER_UNAVAILABLE),
    ({"composer_usable": False}, S.COMPOSER_UNAVAILABLE),
    ({"composer_text": "user's draft"}, S.COMPOSER_UNAVAILABLE),
    ({"generating": True}, S.COMPOSER_UNAVAILABLE),
    ({"error": True}, S.CONVERSATION_UNAVAILABLE),
    ({"document_id": "another-document"}, S.CONVERSATION_UNAVAILABLE),
    ({"url": "https://chatgpt.com/c/wrong"}, S.CONVERSATION_UNAVAILABLE),
    ({"messages": []}, S.CONVERSATION_UNAVAILABLE),
])
def test_changed_ui_or_conversation_prevents_send(tmp_path, changes, status):
    async def scenario():
        h = Harness(tmp_path)
        await h.ready()
        h.page.view.update(changes)
        assert (await h.transport.send_message("do not send")).status == status
        h.ui.submit.assert_not_awaited()
        await h.transport.close()
    asyncio.run(scenario())


def test_fill_failure_is_before_send_and_does_not_log_private_data(tmp_path, caplog):
    async def scenario():
        h = Harness(tmp_path)
        await h.ready()
        h.ui.fill.side_effect = RuntimeError("credential-secret private-session")
        with caplog.at_level("INFO"):
            result = await h.transport.send_message("private-prompt")
        assert result.status == S.SUBMISSION_FAILED
        assert result.submission == SubmissionState.NOT_SENT and not result.uncertain
        assert "credential-secret" not in result.error + caplog.text
        assert "private-prompt" not in caplog.text
        assert not (h.profile / ".auto-coding-operation.json").exists()
        h.ui.submit.assert_not_awaited()
        await h.transport.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("reason,status", [
    ("conversation", S.CONVERSATION_UNAVAILABLE), ("login", S.LOGIN_REQUIRED),
    ("composer", S.SUBMISSION_FAILED), ("pending", S.SUBMISSION_FAILED),
])
def test_atomic_guard_rejection_is_proven_not_sent(tmp_path, reason, status):
    async def scenario():
        h = Harness(tmp_path)
        await h.ready()
        h.ui.submit.return_value = {"sent": False, "reason": reason}
        result = await h.transport.send_message("test")
        assert result.status == status and result.submission == SubmissionState.NOT_SENT
        assert not result.uncertain
        assert not (h.profile / ".auto-coding-operation.json").exists()
        h.ui.collect.assert_not_awaited()
        await h.transport.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("ack", [{}, {"sent": None}, {"sent": False}, {"sent": False, "reason": "unknown"}, None])
def test_malformed_send_acknowledgement_is_uncertain_never_retried(tmp_path, ack):
    async def scenario():
        h = Harness(tmp_path)
        await h.ready()
        h.ui.submit.return_value = ack
        result = await h.transport.send_message("test")
        assert result.status == S.SUBMISSION_UNCERTAIN and result.uncertain
        assert result.submission == SubmissionState.ATTEMPTED
        assert (await h.transport.send_message("retry forbidden")).status == S.INTERRUPTED
        h.ui.submit.assert_awaited_once()
        assert (h.profile / ".auto-coding-operation.json").exists()
        await h.transport.close()
    asyncio.run(scenario())


def test_uncertain_click_keeps_private_free_journal_and_blocks_restart(tmp_path):
    async def scenario():
        h = Harness(tmp_path)
        await h.ready()
        h.ui.submit.side_effect = RuntimeError("secret authentication detail")
        result = await h.transport.send_message("private full prompt")
        assert result.status == S.SUBMISSION_UNCERTAIN and not result.completed
        journal = h.profile / ".auto-coding-operation.json"
        data = json.loads(journal.read_text(encoding="utf-8"))
        assert data["operation_id"] == result.operation_id and data["conversation_url"] == URL
        assert "private" not in journal.read_text() and "secret" not in journal.read_text()
        assert (await h.transport.close()).uncertain
        reopened = h.make_transport()
        assert (await reopened.start()).status == S.INTERRUPTED
        assert (await reopened.send_message("never retry")).status == S.INTERRUPTED
        h.ui.submit.assert_awaited_once()
        await reopened.close()
        assert journal.exists()
    asyncio.run(scenario())


def test_separate_submission_collection_and_pending_duplicate_prevention(tmp_path):
    async def scenario():
        h = Harness(tmp_path)
        await h.ready()
        submitted = await h.transport.submit_message("one")
        assert submitted.status == S.AWAITING_RESPONSE
        assert submitted.submission == SubmissionState.ATTEMPTED
        assert (await h.transport.submit_message("two")).status == S.BUSY
        assert (await h.transport.select_conversation(URL)).status == S.BUSY
        assert (await h.transport.collect_response()).completed
        assert (await h.transport.collect_response()).status == S.INVALID_INPUT
        h.ui.submit.assert_awaited_once()
        await h.transport.close()
    asyncio.run(scenario())


def test_concurrent_sends_are_serialized_without_duplicate_click(tmp_path):
    async def scenario():
        h = Harness(tmp_path)
        await h.ready()
        entered, release = asyncio.Event(), asyncio.Event()
        async def blocked_collect(*args):
            entered.set()
            await release.wait()
            return await h.complete(*args)
        h.ui.collect.side_effect = blocked_collect
        first = asyncio.create_task(h.transport.send_message("one"))
        await entered.wait()
        assert (await h.transport.send_message("two")).status == S.BUSY
        assert (await h.transport.close()).status == S.BUSY
        release.set()
        assert (await first).completed
        h.ui.submit.assert_awaited_once()
        await h.transport.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("confirmed,status", [(True, S.RESPONSE_TIMEOUT), (False, S.SUBMISSION_UNCERTAIN)])
def test_timeout_is_not_partial_success_or_permission_to_retry(tmp_path, confirmed, status):
    async def scenario():
        h = Harness(tmp_path)
        await h.ready()
        h.ui.collect.side_effect = PlaywrightTimeout("secret raw detail")
        h.ui.progress.return_value = {"confirmed": confirmed, "text": "partial answer"}
        result = await h.transport.send_message("one", timeout=0.01)
        assert result.status == status and result.uncertain and result.response_text is None
        assert not result.completed and "secret" not in result.error
        assert (await h.transport.send_message("two")).status == S.INTERRUPTED
        h.ui.submit.assert_awaited_once()
        await h.transport.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("reply,status", [
    ({"status": "lost"}, S.CONVERSATION_UNAVAILABLE),
    ({"status": "login"}, S.LOGIN_REQUIRED),
    ({"status": "ambiguous", "confirmed": True}, S.RESPONSE_INCOMPLETE),
    ({"status": "error"}, S.RESPONSE_INCOMPLETE),
    ({"status": "pending", "text": "partial"}, S.RESPONSE_INCOMPLETE),
    ({"status": "complete", "text": "", "user_id": "u", "response_id": "a"}, S.RESPONSE_INCOMPLETE),
    ({"status": "complete", "text": "answer"}, S.RESPONSE_INCOMPLETE),
    ({"status": "complete", "text": "answer", "user_id": "u", "response_id": "a"}, S.RESPONSE_INCOMPLETE),
    ({}, S.RESPONSE_INCOMPLETE),
])
def test_uncertain_or_malformed_response_is_never_success(tmp_path, reply, status):
    async def scenario():
        h = Harness(tmp_path)
        await h.ready()
        h.ui.collect.side_effect = None
        h.ui.collect.return_value = reply
        result = await h.transport.send_message("one")
        assert result.status == status and result.uncertain
        assert result.response_text is None and not result.completed
        assert h.transport.interrupted
        await h.transport.close()
    asyncio.run(scenario())


def test_browser_closure_while_collecting_is_uncertain(tmp_path):
    async def scenario():
        h = Harness(tmp_path)
        await h.ready()
        async def closed(*args):
            h.page.closed = True
            raise RuntimeError("Target closed")
        h.ui.collect.side_effect = closed
        result = await h.transport.send_message("one")
        assert result.status == S.BROWSER_CLOSED and result.uncertain
        assert (await h.transport.send_message("two")).status == S.INTERRUPTED
        h.ui.submit.assert_awaited_once()
        await h.transport.close()
    asyncio.run(scenario())


def test_browser_closed_before_submission(tmp_path):
    async def scenario():
        h = Harness(tmp_path)
        await h.ready()
        h.page.closed = True
        result = await h.transport.send_message("one")
        assert result.status == S.BROWSER_CLOSED and not result.uncertain
        h.ui.submit.assert_not_awaited()
        await h.transport.close()
    asyncio.run(scenario())


def test_interruption_after_click_is_durable(tmp_path):
    async def scenario():
        h = Harness(tmp_path)
        await h.ready()
        entered = asyncio.Event()
        async def wait_forever(*args):
            entered.set()
            await asyncio.Event().wait()
        h.ui.collect.side_effect = wait_forever
        sending = asyncio.create_task(h.transport.send_message("one"))
        await entered.wait()
        sending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await sending
        assert h.transport.interrupted
        assert (await h.transport.send_message("two")).status == S.INTERRUPTED
        await h.transport.close()
        assert (h.profile / ".auto-coding-operation.json").exists()
    asyncio.run(scenario())


def test_profile_lock_prevents_another_transport_and_process(tmp_path):
    async def scenario():
        h = Harness(tmp_path)
        await h.transport.start()
        other = h.make_transport()
        assert (await other.start()).status == S.PROFILE_BUSY
        # Independent Python interpreter, same canonical Unicode profile/lock directory.
        code = (
            "import sys; from auto_coding.workspace_lock import WorkspaceLock, WorkspaceBusyError\n"
            "try:\n"
            "    lock = WorkspaceLock(sys.argv[1], lock_directory=sys.argv[2]); lock.acquire()\n"
            "except WorkspaceBusyError:\n"
            "    sys.exit(17)\n"
            "else:\n"
            "    lock.release(); sys.exit(0)\n"
        )
        child = subprocess.run([sys.executable, "-c", code, str(h.profile), str(h.lock_root)],
                               capture_output=True, timeout=15, check=False)
        assert child.returncode == 17, child.stderr.decode(errors="replace")
        await h.transport.close()
        assert (await other.start()).status == S.READY
        await other.close()
    asyncio.run(scenario())


def test_cleanup_failure_retains_profile_lock_until_owned_context_closes(tmp_path):
    async def scenario():
        h = Harness(tmp_path)
        await h.transport.start()
        h.context.close.side_effect = RuntimeError("close failure")
        result = await h.transport.close()
        assert result.status == S.CLEANUP_FAILED and result.uncertain
        other = h.make_transport()
        assert (await other.start()).status == S.PROFILE_BUSY
        h.driver.stop.assert_not_awaited()
        h.context.close.side_effect = None
        assert (await h.transport.close()).status == S.CLOSED
        assert (await other.start()).status == S.READY
        await other.close()
    asyncio.run(scenario())


def test_startup_failure_releases_owned_resources_and_lock(tmp_path):
    async def scenario():
        h = Harness(tmp_path)
        h.ui.wait_ready.side_effect = PlaywrightTimeout("changed UI private info")
        result = await h.transport.start()
        assert result.status == S.STARTUP_FAILED and "private" not in result.error
        h.context.close.assert_awaited_once()
        h.driver.stop.assert_awaited_once()
        h.ui.wait_ready.side_effect = lambda page, timeout: deepcopy(page.view)
        other = h.make_transport()
        assert (await other.start()).status == S.READY
        await other.close()
    asyncio.run(scenario())


def test_existing_non_application_profile_is_not_adopted(tmp_path):
    async def scenario():
        h = Harness(tmp_path)
        h.profile.mkdir(parents=True)
        (h.profile / "Cookies").write_text("sensitive existing browser data")
        assert (await h.transport.start()).status == S.STARTUP_FAILED
        h.driver.chromium.launch_persistent_context.assert_not_awaited()
        assert not (h.profile / ".auto-coding-profile").exists()
        assert (h.profile / "Cookies").read_text() == "sensitive existing browser data"
    asyncio.run(scenario())


def test_launch_failure_stops_playwright_and_releases_profile(tmp_path):
    async def scenario():
        h = Harness(tmp_path)
        h.driver.chromium.launch_persistent_context.side_effect = RuntimeError("binary missing")
        assert (await h.transport.start()).status == S.STARTUP_FAILED
        h.driver.stop.assert_awaited_once()
        h.context.close.assert_not_awaited()
        h.driver.chromium.launch_persistent_context.side_effect = None
        other = h.make_transport()
        assert (await other.start()).status == S.READY
        await other.close()
    asyncio.run(scenario())


def test_temporary_pending_journal_never_claims_recoverable_url(tmp_path):
    async def scenario():
        h = Harness(tmp_path)
        await h.transport.start()
        h.page.view["temporary"] = True
        assert (await h.transport.select_temporary_chat()).status == S.SELECTED
        result = await h.transport.submit_message("private temporary prompt")
        assert result.status == S.AWAITING_RESPONSE and result.conversation_url is None
        data = json.loads((h.profile / ".auto-coding-operation.json").read_text())
        assert data["kind"] == "temporary" and data["conversation_url"] is None
        assert "private" not in json.dumps(data)
        assert (await h.transport.close()).uncertain
    asyncio.run(scenario())


def test_navigation_during_fill_does_not_retarget_another_conversation(tmp_path):
    async def scenario():
        h = Harness(tmp_path)
        await h.ready()
        async def navigate(*args):
            await h.page.goto("https://chatgpt.com/c/wrong")
        h.ui.fill.side_effect = navigate
        result = await h.transport.send_message("do not send")
        assert result.status == S.CONVERSATION_UNAVAILABLE
        assert result.submission == SubmissionState.NOT_SENT
        h.ui.submit.assert_not_awaited()
        assert not (h.profile / ".auto-coding-operation.json").exists()
        await h.transport.close()
    asyncio.run(scenario())


def test_cancelled_startup_closes_owned_browser_and_releases_profile(tmp_path):
    async def scenario():
        h = Harness(tmp_path)
        entered = asyncio.Event()
        async def waiting(*args):
            entered.set()
            await asyncio.Event().wait()
        h.ui.wait_ready.side_effect = waiting
        starting = asyncio.create_task(h.transport.start())
        await entered.wait()
        starting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await starting
        h.context.close.assert_awaited_once()
        h.driver.stop.assert_awaited_once()
        h.ui.wait_ready.side_effect = lambda page, timeout: deepcopy(page.view)
        other = h.make_transport()
        assert (await other.start()).status == S.READY
        await other.close()
    asyncio.run(scenario())


def test_repository_and_ordinary_profiles_are_forbidden(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    for path in [repo / "browser-profiles", Path.home(),
                 tmp_path / "local/Google/Chrome/User Data/Default",
                 tmp_path / "local/Microsoft/Edge/User Data/Profile 1"]:
        with pytest.raises(ValueError):
            _validate_profile(path)


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf"), True, "10"])
def test_invalid_timeouts_never_send(tmp_path, timeout):
    async def scenario():
        h = Harness(tmp_path)
        await h.ready()
        assert (await h.transport.send_message("one", timeout=timeout)).status == S.INVALID_INPUT
        h.ui.submit.assert_not_awaited()
        await h.transport.close()
    asyncio.run(scenario())
    with pytest.raises(ValueError):
        ChatGPTTransport(navigation_timeout=timeout)
