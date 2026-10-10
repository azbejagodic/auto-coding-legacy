"""Centralized, conservative ChatGPT DOM contract (no private HTTP APIs).

Keep selectors and completion evidence here. Unknown markup must fail closed;
text stability or disappearance of the stop button alone never proves completion.
"""

from dataclasses import asdict, dataclass
from typing import Any

from playwright.async_api import Page


@dataclass(frozen=True)
class ChatGPTSelectors:
    main: str = "main"
    composer: str = '#prompt-textarea'
    send: str = 'button[data-testid="send-button"], button[aria-label="Send prompt"]'
    stop: str = 'button[data-testid="stop-button"], button[aria-label="Stop generating"]'
    account: str = '[data-testid="accounts-profile-button"], button[aria-label="Open profile menu"]'
    login: str = 'button[data-testid="login-button"], a[href^="/auth/login"]'
    challenge: str = 'iframe[src*="challenges.cloudflare.com"], input[autocomplete="one-time-code"]'
    temporary: str = ('[data-testid="temporary-chat-indicator"], '
                      'button[data-testid="temporary-chat-button"][aria-pressed="true"]')
    message: str = '[data-message-author-role="user"], [data-message-author-role="assistant"]'
    turn: str = 'article, [data-testid^="conversation-turn-"]'
    copy: str = 'button[data-testid="copy-turn-action-button"], button[aria-label="Copy"]'
    streaming: str = '[aria-busy="true"], [data-is-streaming="true"], .result-streaming'
    error: str = '[data-testid="conversation-error"], [data-testid="error-message"], [role="alert"]'


# Each invocation builds the same DOM helpers; state is local to this document.
# Only visible conversation DOM is inspected, never cookies, storage or tokens.
_RUNTIME = r"""
const visible = el => !!el && el.isConnected && !el.hidden &&
    getComputedStyle(el).visibility !== 'hidden' && el.getClientRects().length > 0;
const all = (selector, root = document) => [...root.querySelectorAll(selector)].filter(visible);
const text = el => (el.value ?? el.innerText ?? '').replace(/\r\n/g, '\n');
const usable = el => visible(el) && !el.disabled && el.getAttribute('aria-disabled') !== 'true';
const busy = root => root.matches(s.streaming) || all(s.streaming, root).length > 0;
const state = window.__autoCodingChatGPT ??= {documentId: crypto.randomUUID(), pending: null};
const snapshot = () => {
    const mains = all(s.main), composers = all(s.composer), sends = all(s.send);
    const messages = mains.length === 1 ? all(s.message, mains[0]).map(el => {
        const turn = el.closest(s.turn);
        const unique = turn && all(s.message, turn).filter(m => m.dataset.messageAuthorRole === 'assistant').length === 1;
        return {id: el.getAttribute('data-message-id'), role: el.dataset.messageAuthorRole, text: text(el),
                complete: !!unique && all(s.copy, turn).length > 0 && !busy(turn)};
    }) : [];
    return {url: location.origin + location.pathname, document_id: state.documentId,
            authenticated: all(s.account).length === 1,
            login_required: all(s.login).length > 0 || all(s.challenge).length > 0 ||
                            location.hostname === 'auth.openai.com',
            temporary: all(s.temporary).length === 1,
            main_count: mains.length, composer_count: composers.length,
            composer_usable: composers.length === 1 && usable(composers[0]) &&
                (composers[0].isContentEditable || composers[0].tagName === 'TEXTAREA') && !composers[0].readOnly,
            composer_text: composers.length === 1 ? text(composers[0]) : null,
            send_count: sends.length, send_usable: sends.length === 1 && usable(sends[0]),
            generating: all(s.stop).length > 0 || (mains.length === 1 && busy(mains[0])),
            error: all(s.error).length > 0, messages};
};
const sameMessages = (left, right) => left.length === right.length && left.every((m, i) =>
    m.id === right[i].id && m.role === right[i].role && m.text === right[i].text);
const identity = (view, binding) => view.document_id === binding.document_id && view.url === binding.url &&
    view.temporary === binding.temporary;
"""

_SNAPSHOT = "s => {" + _RUNTIME + "return snapshot();}"
_READY = "s => {" + _RUNTIME + "const v = snapshot(); return v.login_required || v.error || (v.authenticated && v.composer_usable) ? v : false;}"

_SUBMIT = "({s, binding, prompt, operationId}) => {" + _RUNTIME + r"""
const view = snapshot();
if (!identity(view, binding) || !sameMessages(view.messages, binding.messages)) return {sent: false, reason: 'conversation'};
if (!view.authenticated || view.login_required) return {sent: false, reason: 'login'};
if (view.error || view.generating || !view.composer_usable || !view.send_usable ||
    view.composer_text !== prompt.replace(/\r\n/g, '\n')) return {sent: false, reason: 'composer'};
if (state.pending) return {sent: false, reason: 'pending'};
const pending = state.pending = {operationId, binding, prompt, generationSeen: false, lost: false};
const navigated = () => { pending.lost = true; };
window.addEventListener('popstate', navigated);
window.addEventListener('hashchange', navigated);
pending.observer = new MutationObserver(() => {
    const current = snapshot();
    pending.generationSeen ||= current.generating;
    if (!identity(current, binding)) pending.lost = true;
});
pending.observer.observe(document.documentElement, {childList: true, subtree: true, attributes: true});
pending.dispose = () => {
    pending.observer.disconnect();
    window.removeEventListener('popstate', navigated);
    window.removeEventListener('hashchange', navigated);
};
// Identity, full composer contents and one enabled send button are checked in
// the same JS task as this single click. No auto-wait can retarget another chat.
all(s.send)[0].click();
pending.generationSeen ||= snapshot().generating;
return {sent: true};
} """

_POLL = "({s, operationId, terminalOnly}) => {" + _RUNTIME + r"""
const p = state.pending, v = snapshot();
const result = (status, extra = {}) => ({status, ...extra});
if (!p || p.operationId !== operationId) return result('lost');
if (p.lost || !identity(v, p.binding)) return result('lost');
if (v.login_required || !v.authenticated) return result('login');
if (v.error) return result('error');
if (v.main_count !== 1 || v.messages.some(m => !m.id)) return result('ambiguous');
const previous = p.binding.messages;
if (!sameMessages(v.messages.slice(0, previous.length), previous)) return result('lost');
const added = v.messages.slice(previous.length);
if (added.length > 2 || (added.length && (added[0].role !== 'user' ||
    added[0].text.trim() !== p.prompt.replace(/\r\n/g, '\n').trim())) ||
    (added.length === 2 && added[1].role !== 'assistant')) return result('ambiguous');
if (new Set(v.messages.map(m => m.id)).size !== v.messages.length) return result('ambiguous');
p.generationSeen ||= v.generating;
const confirmed = added.length > 0;
if (added.length === 2 && p.generationSeen && !v.generating && added[1].complete && added[1].text.trim()) {
    return result('complete', {confirmed, text: added[1].text, user_id: added[0].id,
                              response_id: added[1].id, messages: v.messages});
}
return terminalOnly ? false : result('pending', {confirmed, generation_seen: p.generationSeen});
} """

_DISPOSE = "operationId => {const s = window.__autoCodingChatGPT; if (s?.pending?.operationId === operationId) {s.pending.dispose(); s.pending = null;}}"


class ChatGPTUI:
    """Replaceable UI adapter; all waits observe DOM state, not fixed sleeps."""

    def __init__(self, selectors: ChatGPTSelectors | None = None) -> None:
        self.selectors = selectors or ChatGPTSelectors()

    async def inspect(self, page: Page) -> dict[str, Any]:
        return await page.evaluate(_SNAPSHOT, asdict(self.selectors))

    async def wait_ready(self, page: Page, timeout: float) -> dict[str, Any]:
        handle = await page.wait_for_function(_READY, arg=asdict(self.selectors), timeout=timeout * 1000)
        try:
            return await handle.json_value()
        finally:
            await handle.dispose()

    async def fill(self, page: Page, prompt: str, timeout: float) -> None:
        await page.locator(self.selectors.composer).fill(prompt, timeout=timeout * 1000)

    async def submit(self, page: Page, binding: dict[str, Any], prompt: str, operation_id: str) -> dict[str, Any]:
        return await page.evaluate(_SUBMIT, {"s": asdict(self.selectors), "binding": binding,
                                           "prompt": prompt, "operationId": operation_id})

    async def collect(self, page: Page, operation_id: str, timeout: float) -> dict[str, Any]:
        handle = await page.wait_for_function(_POLL, arg={"s": asdict(self.selectors), "operationId": operation_id,
                                                        "terminalOnly": True}, timeout=timeout * 1000, polling="raf")
        try:
            return await handle.json_value()
        finally:
            await handle.dispose()

    async def progress(self, page: Page, operation_id: str) -> dict[str, Any]:
        return await page.evaluate(_POLL, {"s": asdict(self.selectors), "operationId": operation_id, "terminalOnly": False})

    async def dispose(self, page: Page, operation_id: str) -> None:
        await page.evaluate(_DISPOSE, operation_id)
