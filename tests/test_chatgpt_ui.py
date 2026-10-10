"""Real DOM contract tests. All browser requests are fulfilled locally.

These tests need installed Playwright Chromium, not a ChatGPT account/network.
Without that optional binary they skip; fake transport coverage always runs.
"""

import asyncio
from pathlib import Path

import pytest
from playwright.async_api import TimeoutError as PlaywrightTimeout, async_playwright

from auto_coding.integrations.chatgpt_ui import ChatGPTSelectors, ChatGPTUI


HTML = (Path(__file__).parent / "fixtures" / "chatgpt.html").read_text(encoding="utf-8")
FIXTURE_URL = "https://fixture.invalid/c/offline-test"


def run_browser(check):
    async def scenario():
        async with async_playwright() as driver:
            if not Path(driver.chromium.executable_path).is_file():
                pytest.skip("Install Playwright Chromium to run offline DOM tests")
            browser = await driver.chromium.launch(headless=True, chromium_sandbox=True)
            context = await browser.new_context(service_workers="block")
            requests = []
            async def route(request):
                requests.append(request.request.url)
                await request.fulfill(status=200, content_type="text/html", body=HTML)
            await context.route("**/*", route)
            try:
                page = await context.new_page()
                await page.goto(FIXTURE_URL)
                await check(page, ChatGPTUI())
                assert requests and all(url.startswith("https://fixture.invalid/") for url in requests)
            finally:
                await context.close()
                await browser.close()
    asyncio.run(scenario())


async def select(ui, page):
    view = await ui.wait_ready(page, 5)
    return {key: view[key] for key in ("url", "document_id", "temporary", "messages")}


async def submit(ui, page, text="Full prompt\nAžbe 🐍", operation="operation-1"):
    binding = await select(ui, page)
    await ui.fill(page, text, 5)
    assert await ui.submit(page, binding, text, operation) == {"sent": True}
    return binding


def test_local_streaming_requires_complete_new_response_and_preserves_full_text():
    async def check(page, ui):
        prompt = "  Ažbe 🐍\n" + ("line\n" * 1000) + "last  "
        await submit(ui, page, prompt)
        progress = await ui.progress(page, "operation-1")
        assert progress == {"status": "pending", "confirmed": True, "generation_seen": True}
        view = await ui.inspect(page)
        assert view["messages"][-2]["text"] == prompt
        assert view["messages"][-1]["text"] == "Partial"
        await page.evaluate("finish()")
        result = await ui.collect(page, "operation-1", 1)
        assert result["status"] == "complete" and result["confirmed"]
        assert result["text"] == "Full response\nSecond line 🐍"
        assert result["response_id"] == "new-answer-1" and result["user_id"] == "new-user-1"
        assert await page.evaluate("submitCount") == 1
        await ui.dispose(page, "operation-1")
        await submit(ui, page, "second", "operation-2")
        await page.evaluate("finish({text: 'second answer'})")
        assert (await ui.collect(page, "operation-2", 1))["response_id"] == "new-answer-2"
        assert await page.evaluate("submitCount") == 2
    run_browser(check)


@pytest.mark.parametrize("finish", [
    None, "finish({copy: false})", "finish({clearBusy: false})", "finish({text: ''})",
])
def test_partial_stop_disappearance_or_copy_alone_never_proves_completion(finish):
    async def check(page, ui):
        await submit(ui, page)
        if finish:
            await page.evaluate(finish)
        with pytest.raises(PlaywrightTimeout):
            await ui.collect(page, "operation-1", 0.15)
        assert (await ui.progress(page, "operation-1"))["status"] == "pending"
        assert await page.evaluate("submitCount") == 1
    run_browser(check)


def test_no_observed_generation_is_uncertain_even_when_copy_is_present():
    async def check(page, ui):
        await page.evaluate("""() => {
            const send = document.querySelector('[data-testid="send-button"]');
            const original = send.onclick;
            send.onclick = () => { original(); finish(); };
        }""")
        await submit(ui, page)
        with pytest.raises(PlaywrightTimeout):
            await ui.collect(page, "operation-1", 0.15)
        assert not (await ui.progress(page, "operation-1"))["generation_seen"]
    run_browser(check)


@pytest.mark.parametrize("change", [
    "history.pushState({}, '', '/c/wrong')",
    "document.querySelector('[data-testid=\"temporary-chat-button\"]').setAttribute('aria-pressed', 'true')",
    "document.querySelector('[data-message-id=\"old-answer\"]').textContent = 'changed old answer'",
    "window.__autoCodingChatGPT.documentId = 'new-document'",
])
def test_atomic_send_guard_rechecks_identity_after_fill_and_never_clicks(change):
    async def check(page, ui):
        binding = await select(ui, page)
        await ui.fill(page, "do not send", 1)
        await page.evaluate(change)
        result = await ui.submit(page, binding, "do not send", "operation-1")
        assert result == {"sent": False, "reason": "conversation"}
        assert await page.evaluate("submitCount") == 0
    run_browser(check)


@pytest.mark.parametrize("change", [
    "document.querySelector('[data-testid=\"send-button\"]').remove()",
    "document.querySelector('[data-testid=\"send-button\"]').disabled = true",
    "document.querySelector('main').append(document.querySelector('[data-testid=\"send-button\"]').cloneNode(true))",
    "document.querySelector('#prompt-textarea').value = 'truncated'",
    "document.querySelector('main').setAttribute('aria-busy', 'true')",
])
def test_send_guard_requires_one_usable_button_and_exact_full_prompt(change):
    async def check(page, ui):
        binding = await select(ui, page)
        await ui.fill(page, "full prompt", 1)
        await page.evaluate(change)
        assert await ui.submit(page, binding, "full prompt", "operation-1") == {
            "sent": False, "reason": "composer",
        }
        assert await page.evaluate("submitCount") == 0
    run_browser(check)


def test_ui_pending_guard_never_submits_twice():
    async def check(page, ui):
        await submit(ui, page)
        # Even a fresh binding cannot bypass the pending-operation guard.
        await page.evaluate("finish()")
        binding = await select(ui, page)
        await ui.fill(page, "duplicate", 1)
        assert await ui.submit(page, binding, "duplicate", "operation-2") == {
            "sent": False, "reason": "pending",
        }
        assert await page.evaluate("submitCount") == 1
    run_browser(check)


@pytest.mark.parametrize("change,status", [
    ("appendMessage('assistant', 'extra', 'unrelated answer')", "ambiguous"),
    ("appendMessage('user', 'extra', 'unrelated question')", "ambiguous"),
    ("document.querySelector('[data-message-id=\"new-user-1\"]').textContent = 'wrong user text'", "ambiguous"),
    ("document.querySelector('[data-message-id=\"new-answer-1\"]').dataset.messageId = 'old-answer'", "ambiguous"),
    ("document.querySelector('[data-message-id=\"new-answer-1\"]').removeAttribute('data-message-id')", "ambiguous"),
    ("document.querySelector('[data-message-id=\"old-user\"]').remove()", "lost"),
    ("history.pushState({}, '', '/c/wrong')", "lost"),
    ("document.querySelector('[data-testid=\"accounts-profile-button\"]').remove()", "login"),
    ("document.querySelector('main').insertAdjacentHTML('beforeend', '<div role=\"alert\">Response failed</div>')", "error"),
])
def test_response_association_rejects_ambiguous_lost_or_error_state(change, status):
    async def check(page, ui):
        await submit(ui, page)
        await page.evaluate(change)
        result = await ui.collect(page, "operation-1", 1)
        assert result["status"] == status and "text" not in result
    run_browser(check)


def test_temporary_identity_loss_stays_lost_even_if_page_returns():
    async def check(page, ui):
        await page.evaluate("""document.querySelector('[data-testid="temporary-chat-button"]')
                              .setAttribute('aria-pressed', 'true')""")
        binding = await submit(ui, page)
        assert binding["temporary"] is True
        await page.evaluate("history.pushState({}, '', '/c/wrong')")
        # A separate event-loop turn lets the mutation observer observe the loss.
        await page.evaluate("document.body.append(document.createElement('div'))")
        await page.wait_for_function("window.__autoCodingChatGPT.pending.lost")
        await page.evaluate("history.pushState({}, '', '/c/offline-test')")
        await page.evaluate("finish()")
        assert (await ui.collect(page, "operation-1", 1))["status"] == "lost"
    run_browser(check)


def test_page_reload_loses_pending_operation():
    async def check(page, ui):
        await submit(ui, page)
        await page.reload()
        assert (await ui.collect(page, "operation-1", 1))["status"] == "lost"
    run_browser(check)


def test_login_challenge_and_changed_selectors_fail_without_clicking():
    async def check(page, ui):
        await page.evaluate("""() => {
            document.querySelector('[data-testid="accounts-profile-button"]').remove();
            document.body.insertAdjacentHTML('beforeend', '<input autocomplete="one-time-code">');
        }""")
        assert (await ui.wait_ready(page, 1))["login_required"]
        await page.evaluate("document.querySelector('input').remove()")
        changed = ChatGPTUI(ChatGPTSelectors(composer="#does-not-exist"))
        with pytest.raises(PlaywrightTimeout):
            await changed.wait_ready(page, 0.15)
        assert await page.evaluate("submitCount") == 0
    run_browser(check)


def test_contenteditable_composer_support():
    async def check(page, ui):
        await page.evaluate("""() => {
            const textarea = document.querySelector('#prompt-textarea');
            const editor = document.createElement('div');
            editor.id = textarea.id;
            editor.contentEditable = 'true';
            editor.style.minHeight = '30px';
            textarea.replaceWith(editor);
            document.querySelector('[data-testid="send-button"]').onclick = () => { window.submitCount++; };
        }""")
        binding = await select(ui, page)
        await ui.fill(page, "Unicode 🐍\nSecond line", 1)
        assert (await ui.inspect(page))["composer_text"] == "Unicode 🐍\nSecond line"
        assert await ui.submit(page, binding, "Unicode 🐍\nSecond line", "operation-1") == {"sent": True}
        assert await page.evaluate("submitCount") == 1
    run_browser(check)
