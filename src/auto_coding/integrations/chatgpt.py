"""One-message browser transport, independent of task/Git orchestration.

Only an owned persistent Chromium context is supported, not a daily browser or
an unrestricted CDP endpoint. Calls belong to the event loop that started it.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
import json
import logging
import math
import os
from pathlib import Path
import re
import sys
import tempfile
from threading import Lock
import time
from typing import Any, Callable
from urllib.parse import urlsplit
from uuid import uuid4

from playwright.async_api import Page, TimeoutError as PlaywrightTimeout, async_playwright

from ..workspace_lock import WorkspaceBusyError, WorkspaceLock
from .chatgpt_ui import ChatGPTUI


logger = logging.getLogger(__name__)
_HOME = "https://chatgpt.com/"
_PROFILE_MARKER = ".auto-coding-profile"
_JOURNAL = ".auto-coding-operation.json"


class TransportStatus(StrEnum):
    READY = "ready"
    SELECTED = "selected"
    AWAITING_RESPONSE = "awaiting_response"
    COMPLETED = "completed"
    CLOSED = "closed"
    LOGIN_REQUIRED = "login_required"
    CONVERSATION_UNAVAILABLE = "conversation_unavailable"
    COMPOSER_UNAVAILABLE = "composer_unavailable"
    INTERFACE_UNRECOGNIZED = "interface_unrecognized"
    INVALID_INPUT = "invalid_input"
    SUBMISSION_FAILED = "submission_failed_before_send"
    SUBMISSION_UNCERTAIN = "submission_uncertain"
    RESPONSE_TIMEOUT = "response_timeout"
    RESPONSE_INCOMPLETE = "response_incomplete"
    BROWSER_CLOSED = "browser_closed"
    PROFILE_BUSY = "profile_busy"
    STARTUP_FAILED = "startup_failed"
    CLEANUP_FAILED = "cleanup_failed"
    BUSY = "busy"
    INTERRUPTED = "interrupted"
    NOT_STARTED = "not_started"


class SubmissionState(StrEnum):
    NOT_SENT = "not_sent"
    ATTEMPTED = "attempted"
    CONFIRMED = "confirmed"


class ConversationKind(StrEnum):
    NORMAL = "normal"
    TEMPORARY = "temporary"


@dataclass(frozen=True)
class BrowserResult:
    status: TransportStatus
    duration_seconds: float
    conversation_kind: ConversationKind | None = None
    conversation_url: str | None = None
    operation_id: str | None = None
    submission: SubmissionState = SubmissionState.NOT_SENT
    response_text: str | None = None
    user_message_id: str | None = None
    response_message_id: str | None = None
    error: str | None = None
    uncertain: bool = False
    requires_manual_action: bool = False

    @property
    def completed(self) -> bool:
        return self.status == TransportStatus.COMPLETED and not self.uncertain


def _conversation_url(value: str) -> str | None:
    """Accept only credential/query-free canonical ChatGPT conversation URLs."""
    try:
        parts = urlsplit(value)
        if (parts.scheme != "https" or parts.netloc != "chatgpt.com" or parts.query or parts.fragment
                or not re.fullmatch(r"/(?:g/[A-Za-z0-9_-]+/)?c/[A-Za-z0-9_-]{1,128}/?", parts.path)):
            return None
        return "https://chatgpt.com" + parts.path.rstrip("/")
    except (ValueError, TypeError):
        return None


def _positive_timeout(value: float) -> bool:
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value) and value > 0


def _default_profile() -> Path:
    if os.name == "nt":
        root = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    elif sys.platform == "darwin":
        root = Path.home() / "Library" / "Application Support"
    else:
        root = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return root / "auto-coding" / "browser-profiles" / "chatgpt"


def _validate_profile(path: Path) -> Path:
    path = path.resolve()
    if path == Path.home().resolve() or path == Path(path.anchor):
        raise ValueError("A dedicated application profile directory is required")
    local = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    ordinary = [local / "Google" / "Chrome" / "User Data", local / "Microsoft" / "Edge" / "User Data",
                local / "Chromium" / "User Data", Path.home() / ".config" / "google-chrome",
                Path.home() / ".config" / "chromium",
                Path.home() / "Library" / "Application Support" / "Google" / "Chrome",
                Path.home() / "Library" / "Application Support" / "Microsoft Edge"]
    if any(path.is_relative_to(root.resolve()) for root in ordinary):
        raise ValueError("Ordinary browser profiles are not supported")
    if any((parent / ".git").exists() for parent in (path, *path.parents)):
        raise ValueError("Browser profiles must be outside Git repositories")
    return path


class ChatGPTTransport:
    """Explicit lifecycle and serialized, fail-closed exchanges.

    start/select/submit/collect/send_message/close return typed outcomes. No
    credential, prompt or output logging; callers own the privacy of responses.
    """

    def __init__(
        self, profile_directory: str | Path | None = None, *,
        headless: bool = False, channel: str | None = None,
        navigation_timeout: float = 30.0, response_timeout: float = 120.0,
        ui: ChatGPTUI | None = None,
        playwright_factory: Callable[..., Any] = async_playwright,
        lock_factory: Callable[[Path], WorkspaceLock] = WorkspaceLock,
    ) -> None:
        if not _positive_timeout(navigation_timeout) or not _positive_timeout(response_timeout):
            raise ValueError("Browser timeouts must be finite and positive")
        if channel not in (None, "chrome", "msedge"):
            raise ValueError("Supported channels are bundled Chromium, chrome and msedge")
        self.profile_directory = Path(profile_directory) if profile_directory is not None else _default_profile()
        self.headless, self.channel = headless, channel
        self.navigation_timeout, self.response_timeout = navigation_timeout, response_timeout
        self.ui = ui or ChatGPTUI()
        self._playwright_factory, self._lock_factory = playwright_factory, lock_factory
        self._gate = Lock()
        self._loop = None
        self._driver = self._context = self._profile_lock = self._page = None
        self._binding: dict[str, Any] | None = None
        self._kind: ConversationKind | None = None
        self._pending: dict[str, Any] | None = None
        self._interrupted = False
        self._invalidated = False

    @property
    def page(self) -> Page | None:
        """Owned page for manual sign-in/selection, never an ordinary profile."""
        return self._page

    @property
    def interrupted(self) -> bool:
        return self._interrupted

    def _result(self, started: float, status: TransportStatus, error: str | None = None,
                *, uncertain: bool = False, **metadata) -> BrowserResult:
        pending = self._pending or {}
        result = BrowserResult(
            status, time.monotonic() - started, self._kind,
            self._binding["url"] if self._kind == ConversationKind.NORMAL and self._binding else None,
            pending.get("id"), pending.get("submission", SubmissionState.NOT_SENT),
            error=error, uncertain=uncertain,
            requires_manual_action=status not in {
                TransportStatus.READY, TransportStatus.SELECTED, TransportStatus.AWAITING_RESPONSE,
                TransportStatus.COMPLETED, TransportStatus.CLOSED, TransportStatus.BUSY,
            } or uncertain, **metadata,
        )
        logger.info("ChatGPT transport: status=%s submission=%s uncertain=%s duration=%.3fs",
                    status, result.submission, uncertain, result.duration_seconds)
        return result

    async def _guard(self, operation) -> BrowserResult:
        started = time.monotonic()
        if not self._gate.acquire(blocking=False):
            return self._result(started, TransportStatus.BUSY, "Another browser operation is in progress")
        try:
            if self._loop is not None and self._loop is not asyncio.get_running_loop():
                return self._result(started, TransportStatus.INVALID_INPUT, "Use the event loop that started this transport")
            return await operation(started)
        except asyncio.CancelledError:
            if self._pending:
                self._interrupted = True  # Durable journal already exists before any click.
            raise
        finally:
            self._gate.release()

    def _unavailable(self, started: float) -> BrowserResult | None:
        if self._interrupted:
            return self._result(started, TransportStatus.INTERRUPTED,
                                "An interrupted operation requires manual review; no messages will be retried",
                                uncertain=True)
        if self._context is None or self._page is None:
            return self._result(started, TransportStatus.NOT_STARTED, "Start the dedicated browser first")
        if self._page.is_closed():
            if self._pending:
                self._interrupted = True
            return self._result(started, TransportStatus.BROWSER_CLOSED, "The selected browser page was closed",
                                uncertain=self._pending is not None)
        return None

    @staticmethod
    def _view_error(view: dict[str, Any]) -> tuple[TransportStatus, str] | None:
        if view["login_required"]:
            return TransportStatus.LOGIN_REQUIRED, "Sign in or complete the security challenge manually in the browser"
        if view["error"]:
            return TransportStatus.CONVERSATION_UNAVAILABLE, "ChatGPT reports a conversation or response error"
        if not view["authenticated"] or view["main_count"] != 1:
            return TransportStatus.INTERFACE_UNRECOGNIZED, "Authenticated conversation UI is not recognized; inspect selectors"
        if view["composer_count"] != 1 or not view["composer_usable"]:
            return TransportStatus.COMPOSER_UNAVAILABLE, "Exactly one usable message composer is required"
        return None

    async def start(self) -> BrowserResult:
        return await self._guard(self._start)

    async def _start(self, started: float) -> BrowserResult:
        if self._context is not None:
            return self._unavailable(started) or self._result(started, TransportStatus.READY)
        self._loop = asyncio.get_running_loop()
        try:
            self.profile_directory = _validate_profile(self.profile_directory)
            self.profile_directory.mkdir(parents=True, exist_ok=True)
            lock = self._lock_factory(self.profile_directory)
            lock.acquire()
            self._profile_lock = lock
            marker = self.profile_directory / _PROFILE_MARKER
            if marker.exists():
                if marker.read_text(encoding="utf-8") != "auto-coding-chatgpt-v1\n":
                    raise ValueError("Unrecognized dedicated browser profile")
            elif any(self.profile_directory.iterdir()):
                raise ValueError("Existing nonempty directories cannot be adopted as browser profiles")
            else:
                marker.write_text("auto-coding-chatgpt-v1\n", encoding="utf-8")
            self._interrupted = (self.profile_directory / _JOURNAL).exists()
            self._driver = await self._playwright_factory().start()
            self._context = await self._driver.chromium.launch_persistent_context(
                user_data_dir=str(self.profile_directory), headless=self.headless, channel=self.channel,
                chromium_sandbox=True, accept_downloads=False, timeout=self.navigation_timeout * 1000,
            )
            pages = self._context.pages
            self._page = pages[0] if len(pages) == 1 else await self._context.new_page()

            def navigated(frame):
                if frame == self._page.main_frame and self._binding:
                    if self._kind == ConversationKind.TEMPORARY or _conversation_url(self._page.url) != self._binding["url"]:
                        self._invalidated = True

            self._page.on("framenavigated", navigated)
            if self._page.url == "about:blank":
                await self._page.goto(_HOME, wait_until="domcontentloaded", timeout=self.navigation_timeout * 1000)
            if self._interrupted:
                return self._result(started, TransportStatus.INTERRUPTED,
                                    "An operation journal remains from an interrupted exchange; review it manually",
                                    uncertain=True)
            view = await self.ui.wait_ready(self._page, self.navigation_timeout)
            error = self._view_error(view)
            return self._result(started, *error) if error else self._result(started, TransportStatus.READY)
        except WorkspaceBusyError:
            return self._result(started, TransportStatus.PROFILE_BUSY, "The dedicated browser profile is already in use")
        except asyncio.CancelledError:
            await asyncio.shield(self._shutdown())
            raise
        except Exception:
            cleaned = await self._shutdown()
            return self._result(started, TransportStatus.STARTUP_FAILED if cleaned else TransportStatus.CLEANUP_FAILED,
                                "Could not start/verify the dedicated browser; check profile, browser installation and UI",
                                uncertain=not cleaned)

    async def select_conversation(self, url: str) -> BrowserResult:
        return await self._guard(lambda started: self._select(started, url, temporary=False))

    async def select_temporary_chat(self) -> BrowserResult:
        """Bind the current page's active, already-established Temporary Chat."""
        return await self._guard(lambda started: self._select(started, None, temporary=True))

    async def _select(self, started: float, url: str | None, *, temporary: bool) -> BrowserResult:
        unavailable = self._unavailable(started)
        if unavailable:
            return unavailable
        if self._pending:
            return self._result(started, TransportStatus.BUSY, "Collect the pending response before selecting a conversation")
        self._binding, self._kind, self._invalidated = None, None, False
        canonical = _conversation_url(url) if not temporary else None
        if not temporary and canonical is None:
            return self._result(started, TransportStatus.INVALID_INPUT, "An exact https://chatgpt.com/c/... conversation URL is required")
        try:
            if not temporary:
                await self._page.goto(canonical, wait_until="domcontentloaded", timeout=self.navigation_timeout * 1000)
            view = await self.ui.wait_ready(self._page, self.navigation_timeout)
            error = self._view_error(view)
            if error:
                return self._result(started, *error)
            if view["generating"]:
                return self._result(started, TransportStatus.CONVERSATION_UNAVAILABLE, "Wait for the existing response to finish")
            messages = view["messages"]
            if (any(not m["id"] for m in messages) or len({m["id"] for m in messages}) != len(messages)):
                return self._result(started, TransportStatus.INTERFACE_UNRECOGNIZED, "Stable, unique visible message IDs are required")
            if temporary:
                if (not view["temporary"] or not view["url"].startswith(_HOME)
                        or not messages or messages[-1]["role"] != "assistant" or not messages[-1]["complete"]):
                    return self._result(started, TransportStatus.CONVERSATION_UNAVAILABLE,
                                        "Select an active Temporary Chat with an already-completed manual exchange; recovery is not automatic")
            elif view["temporary"] or view["url"].rstrip("/") != canonical:
                return self._result(started, TransportStatus.CONVERSATION_UNAVAILABLE, "The intended conversation was not reached; no fallback chat was selected")
            self._kind = ConversationKind.TEMPORARY if temporary else ConversationKind.NORMAL
            self._binding = {"url": view["url"], "document_id": view["document_id"],
                             "temporary": temporary, "messages": messages}
            return self._result(started, TransportStatus.SELECTED)
        except Exception:
            status = TransportStatus.BROWSER_CLOSED if self._page.is_closed() else TransportStatus.CONVERSATION_UNAVAILABLE
            return self._result(started, status, "Conversation navigation or UI verification failed; select again manually")

    def _write_journal(self, operation_id: str) -> None:
        if (self.profile_directory / _JOURNAL).exists():
            raise ValueError("An unresolved operation journal already exists")
        metadata = {"operation_id": operation_id, "phase": "submission_attempted", "kind": self._kind,
                    "conversation_url": self._binding["url"] if self._kind == ConversationKind.NORMAL else None,
                    "created_at": datetime.now(timezone.utc).isoformat()}
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.profile_directory,
                                             prefix=".auto-coding-operation-", suffix=".tmp", delete=False) as stream:
                temporary = Path(stream.name)
                json.dump(metadata, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.profile_directory / _JOURNAL)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def _clear_journal(self) -> None:
        (self.profile_directory / _JOURNAL).unlink(missing_ok=True)

    async def submit_message(self, text: str) -> BrowserResult:
        """Attempt exactly one submission; collect_response must follow."""
        return await self._guard(lambda started: self._submit(started, text))

    async def send_message(self, text: str, *, timeout: float | None = None) -> BrowserResult:
        """Submit once and collect the new, observably completed assistant turn."""
        async def exchange(started):
            limit = self.response_timeout if timeout is None else timeout
            if not _positive_timeout(limit):
                return self._result(started, TransportStatus.INVALID_INPUT, "Response timeout must be finite and positive")
            result = await self._submit(started, text)
            return await self._collect(started, limit) if result.status == TransportStatus.AWAITING_RESPONSE else result
        return await self._guard(exchange)

    async def _submit(self, started: float, text: str) -> BrowserResult:
        if not isinstance(text, str) or not text.strip() or "\x00" in text:
            return self._result(started, TransportStatus.INVALID_INPUT, "Message text must be nonempty and contain no NUL characters")
        unavailable = self._unavailable(started)
        if unavailable:
            return unavailable
        if self._pending:
            return self._result(started, TransportStatus.BUSY, "A submission is already awaiting a response; do not resend")
        if self._binding is None or self._invalidated:
            return self._result(started, TransportStatus.CONVERSATION_UNAVAILABLE, "Explicitly select the intended conversation before sending")
        try:
            view = await self.ui.inspect(self._page)
            error = self._view_error(view)
            if error:
                return self._result(started, *error)
            if (any(view[key] != self._binding[key] for key in ("url", "document_id", "temporary"))
                    or view["messages"] != self._binding["messages"]):
                return self._result(started, TransportStatus.CONVERSATION_UNAVAILABLE, "Conversation changed after selection; no message was sent")
            if view["generating"] or view["composer_text"]:
                return self._result(started, TransportStatus.COMPOSER_UNAVAILABLE, "The conversation is generating or the composer contains a draft")
            await self.ui.fill(self._page, text, self.navigation_timeout)
            if self._invalidated:
                return self._result(started, TransportStatus.CONVERSATION_UNAVAILABLE, "Conversation navigation invalidated selection before submission")
            operation_id = str(uuid4())
            self._write_journal(operation_id)  # Durable intent precedes the click.
        except Exception:
            if (self.profile_directory / _JOURNAL).exists():
                self._interrupted = True
            status = TransportStatus.BROWSER_CLOSED if self._page.is_closed() else TransportStatus.SUBMISSION_FAILED
            return self._result(started, status, "Message preparation failed before submission; no send was attempted")
        self._pending = {"id": operation_id, "submission": SubmissionState.ATTEMPTED}
        try:
            sent = await self.ui.submit(self._page, self._binding, text, operation_id)
            if sent.get("sent") is False and sent.get("reason") in {"conversation", "login", "composer", "pending"}:
                self._clear_journal()
                self._pending = None
                status = {"conversation": TransportStatus.CONVERSATION_UNAVAILABLE, "login": TransportStatus.LOGIN_REQUIRED}.get(
                    sent.get("reason"), TransportStatus.SUBMISSION_FAILED)
                return self._result(started, status, "Atomic send guard rejected the UI before clicking; no message was sent")
            if sent.get("sent") is not True:
                raise ValueError("Unrecognized submission acknowledgement")
            return self._result(started, TransportStatus.AWAITING_RESPONSE)
        except Exception:
            self._interrupted = True
            status = TransportStatus.BROWSER_CLOSED if self._page.is_closed() else TransportStatus.SUBMISSION_UNCERTAIN
            return self._result(started, status, "Submission may have occurred; inspect the conversation manually and do not retry",
                                uncertain=True)

    async def collect_response(self, *, timeout: float | None = None) -> BrowserResult:
        return await self._guard(lambda started: self._collect(started, self.response_timeout if timeout is None else timeout))

    async def _collect(self, started: float, timeout: float) -> BrowserResult:
        if not _positive_timeout(timeout):
            return self._result(started, TransportStatus.INVALID_INPUT, "Response timeout must be finite and positive")
        unavailable = self._unavailable(started)
        if unavailable:
            return unavailable
        if not self._pending:
            return self._result(started, TransportStatus.INVALID_INPUT, "There is no pending submission to collect")
        operation_id = self._pending["id"]
        try:
            reply = await self.ui.collect(self._page, operation_id, timeout)
            if reply.get("confirmed"):
                self._pending["submission"] = SubmissionState.CONFIRMED
            if (reply.get("status") == "complete" and reply.get("confirmed") is True and not self._invalidated
                    and reply.get("text", "").strip() and reply.get("response_id") and reply.get("user_id")):
                await self.ui.dispose(self._page, operation_id)
                self._clear_journal()
                self._binding["messages"] = reply["messages"]
                result = self._result(started, TransportStatus.COMPLETED, response_text=reply["text"],
                                      user_message_id=reply["user_id"], response_message_id=reply["response_id"])
                self._pending = None
                return result
            self._interrupted = True
            status = {"lost": TransportStatus.CONVERSATION_UNAVAILABLE, "login": TransportStatus.LOGIN_REQUIRED}.get(
                reply.get("status"), TransportStatus.RESPONSE_INCOMPLETE)
            return self._result(started, status, "Conversation or response identity/completion could not be established",
                                uncertain=True)
        except PlaywrightTimeout:
            self._interrupted = True
            try:
                progress = await self.ui.progress(self._page, operation_id)
                if progress.get("confirmed"):
                    self._pending["submission"] = SubmissionState.CONFIRMED
            except Exception:
                pass
            status = (TransportStatus.BROWSER_CLOSED if self._page.is_closed() else
                      TransportStatus.RESPONSE_TIMEOUT if self._pending["submission"] == SubmissionState.CONFIRMED
                      else TransportStatus.SUBMISSION_UNCERTAIN)
            return self._result(started, status, "No confirmed complete response within the timeout; do not resend",
                                uncertain=True)
        except Exception:
            self._interrupted = True
            status = TransportStatus.BROWSER_CLOSED if self._page.is_closed() else TransportStatus.RESPONSE_INCOMPLETE
            return self._result(started, status, "Browser or response collection failed; outcome requires manual inspection",
                                uncertain=True)

    async def _shutdown(self) -> bool:
        # Keep profile ownership if closing owned resources cannot be confirmed.
        try:
            if self._context is not None:
                await self._context.close()
                self._context = None
            if self._driver is not None:
                await self._driver.stop()
                self._driver = None
            if self._profile_lock is not None:
                self._profile_lock.release()
                self._profile_lock = None
            self._page, self._binding, self._kind = None, None, None
            return True
        except Exception:
            return False

    async def close(self) -> BrowserResult:
        async def shutdown(started):
            unresolved = self._pending is not None or self._interrupted
            if unresolved:
                self._interrupted = True
            cleaned = await self._shutdown()
            return self._result(started, TransportStatus.CLOSED if cleaned else TransportStatus.CLEANUP_FAILED,
                                "Unresolved exchange retained for manual review" if unresolved else
                                (None if cleaned else "Browser cleanup failed; profile ownership is retained"),
                                uncertain=unresolved or not cleaned)
        return await self._guard(shutdown)
