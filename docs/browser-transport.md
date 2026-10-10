# ChatGPT browser transport (TASK-008)

This is a reusable Python transport, not a workflow controller or a new CLI
command. It does not read plans, approve commits, advance SQLite tasks, or call
OpenAI APIs. Authentication happens manually in the visible browser; web session
credentials remain browser-managed, consistent with the
[ChatGPT authentication documentation](https://learn.chatgpt.com/docs/auth).
There is no token/cookie extraction or automated MFA/CAPTCHA handling.

## Windows PowerShell setup

From the repository, with Python 3.12:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --editable ".[test]"
python -m playwright install chromium
python -m pytest
```

Installation downloads dependencies/browsers; subsequent automated tests need no
network or account. The DOM tests use owned headless Chromium and intercepted
HTML at `https://fixture.invalid`, never ChatGPT. Without the installed binary
they skip explicitly; fake transport tests still run. No test launches Codex.

### Windows virtual-environment test caveat

On installations where the venv executable redirects to a separate Python
worker, the pre-existing direct-process termination lock test can fail: killing
the launcher does not kill its worker. This is independent of the browser
transport and no lock assertion has been relaxed. If
`test_terminated_process_releases_os_lock` fails in that configuration, run the
unchanged complete suite with the real Python 3.12 executable and the same venv
dependencies (from the repository with the venv activated):

```powershell
$taskPython = python -c "import sys; print(sys._base_executable)"
$taskSitePackages = python -c "import sysconfig; print(sysconfig.get_path('purelib'))"
$taskPreviousPythonPath = $env:PYTHONPATH
try {
    $env:PYTHONPATH = (Join-Path (Get-Location) 'src') + ';' + $taskSitePackages
    & $taskPython -m pytest
} finally {
    $env:PYTHONPATH = $taskPreviousPythonPath
}
```

This changes only the current shell environment temporarily, not system settings.
It does not disable containment, skip the test, or alter application lock behavior.

## Architecture and interface

`auto_coding.integrations.chatgpt.ChatGPTTransport` owns a Playwright persistent
Chromium context. `chatgpt_ui.ChatGPTUI` contains all selectors, DOM snapshots,
atomic send validation, and response-completion evidence. Existing controller,
storage, Codex, Git, and Windows process-containment modules are not connected
to this transport.

Constructor options:

- `profile_directory=None`: configurable dedicated directory. Default on Windows:
  `%LOCALAPPDATA%\auto-coding\browser-profiles\chatgpt`; on macOS:
  `~/Library/Application Support/auto-coding/browser-profiles/chatgpt`; on Linux:
  `$XDG_DATA_HOME/auto-coding/browser-profiles/chatgpt` (default `~/.local/share`).
  Relative custom paths resolve against the caller's current directory.
- `headless=False`: visible initially, recommended for manual authentication.
- `channel=None`: bundled Chromium; optionally `"chrome"` or `"msedge"` uses an
  installed browser **with the application's dedicated profile**, not its normal
  profile. Only bundled Chromium is exercised by automated DOM tests.
- `navigation_timeout=30.0`, `response_timeout=120.0`: finite positive seconds.
- `ui`, `playwright_factory`, `lock_factory`: injectable adapters for testing.
  All cooperating processes must use the same profile-lock directory.

The transport explicitly enables Chromium's sandbox and does not require
administrator rights. Playwright documents
[persistent profile ownership and browser channels](https://playwright.dev/python/docs/api/class-browsertype#browser-type-launch-persistent-context).
Never point it at a daily browser profile. Ordinary known Chrome/Edge paths,
Git-contained paths, and existing nonempty unmarked directories are refused.
The application marks new dedicated profiles and reuses only its own profiles.
Unicode paths are supported. Treat the entire directory as credential-sensitive:
do not share it, commit it, or enable browser tracing/HAR dumps containing secrets.

Public async methods return `BrowserResult`:

- `start()`: opens the owned browser and returns `READY`, `LOGIN_REQUIRED`, or
  a typed failure. Recognized login/challenge UI remains open for manual action.
  `page` exposes only the owned page, for manual interaction.
- `select_conversation(url)`: explicitly navigates to a canonical
  `https://chatgpt.com/c/<id>` or `https://chatgpt.com/g/<id>/c/<id>` URL,
  without query, fragment, credentials, or explicit port. Redirects, unavailable
  conversations, and active generation fail; there is no new-chat fallback.
- `select_temporary_chat()`: binds the **current** visible Temporary Chat without
  navigation. First complete a harmless exchange manually, then bind it. The
  completed visible transcript is a required identity anchor; an empty Temporary
  Chat is deliberately refused. Reload, disappearance, or identity change
  invalidates it. No durable/recoverable URL is returned for Temporary Chat.
- `submit_message(text)`: inserts the full text and attempts one submission.
  `AWAITING_RESPONSE` means only that collection may begin, not success.
- `collect_response(timeout=None)`: collects that submission's assistant turn.
- `send_message(text, timeout=None)`: combines submit/collect under one gate.
- `close()`: closes only the owned context/Playwright resources, releases profile
  ownership once cleanup succeeds, and preserves the authenticated profile.

Use one event loop throughout the lifecycle and `try/finally` for cleanup.
An instance's nonblocking gate rejects overlapping operations; a pending send
also blocks subsequent sends and conversation selection. An OS-backed
`WorkspaceLock` coordinates the same canonical profile across local processes.
Failed cleanup retains ownership until an explicit `close()` can confirm it.
Locks do not coordinate arbitrary noncooperating programs or separate computers.
No browser PID scanning, unrelated-process termination, or profile deletion occurs.

## Outcomes and safe interruption handling

`BrowserResult` contains status, elapsed seconds, conversation kind, normal-chat
URL when known, operation UUID, submission state (`NOT_SENT`, `ATTEMPTED`, or
`CONFIRMED`), response/user message IDs and complete response text on success,
static safe error details, `uncertain`, and `requires_manual_action`.
Use `result.completed`, not process/browser availability, as the success test.

Before clicking, the adapter checks the same document, URL, Temporary Chat
mode, transcript, exact full composer contents, and exactly one usable send
button **in the same JavaScript task as the single click**. Existing drafts
are not overwritten. Known guard rejection or preparation failure is not sent;
lost/unrecognized acknowledgement is uncertain, never silently retried.

Collection requires the previous visible transcript to remain unchanged,
followed by the exact new user text and one uniquely identified assistant turn.
It requires observed generation, absence of stop/streaming state, nonempty
answer text, and a visible turn-specific completion/copy action. Text stability
or stop-button disappearance alone does not establish completion. Changed,
virtualized, ambiguous, or incomplete UI stops the exchange. Partial text is
never returned as a completed response.

Logs use the existing Python logger (configure it explicitly if wanted). They
contain status, submission certainty and duration, **not** prompts, responses,
URLs, raw browser exceptions, cookies, tokens, or session identifiers.
Returned response text is private application data; callers must protect it.

Immediately before a possible click, the profile gets an atomically replaced,
flushed `.auto-coding-operation.json` containing only operation UUID, timestamp,
phase, kind and normal-chat URL (never Temporary Chat URL, prompt or credentials).
Confirmed completion or a proven pre-click guard rejection clears this journal.
Cancellation, crashes and uncertain outcomes retain it. On reopening,
`INTERRUPTED` blocks all sends, even if the journal is malformed. No automatic
recovery/retry is provided.

If interrupted, inspect the browser/conversation and intended message manually;
do not resend just because Python timed out. Close the transport before any
manual journal removal, and remove it **only after deciding how the prior exchange
was resolved**. This acknowledgement is deliberately outside the automatic API.
For a lost Temporary Chat, there may be nothing recoverable; do not pretend its
old prompt/reply was preserved. A fresh chat must be an explicit user decision.
Browser cleanup failure is also unsuccessful, even after an answer was collected.

## OPTIONAL live manual smoke test

**Not run by pytest or during development.** A live send uses the signed-in
account and is performed only when the user chooses it. Save this example to a
local ignored scratch file, activate the environment, and run it interactively:

```python
import asyncio
from auto_coding.integrations.chatgpt import ChatGPTTransport, TransportStatus

async def main():
    browser = ChatGPTTransport()  # Visible, dedicated persistent profile.
    try:
        started = await browser.start()
        print(started.status, started.error)
        if started.status not in (TransportStatus.READY, TransportStatus.LOGIN_REQUIRED):
            return
        await asyncio.to_thread(
            input, "Sign in/complete challenges manually. When ready, press Enter: "
        )
        url = await asyncio.to_thread(
            input, "Paste a harmless test conversation URL (or type temporary): "
        )
        # For Temporary Chat, first create it and finish one harmless exchange
        # manually in this browser before choosing the temporary option.
        selected = (await browser.select_temporary_chat() if url == "temporary"
                    else await browser.select_conversation(url))
        print(selected.status, selected.error)
        if selected.status != TransportStatus.SELECTED:
            return
        await asyncio.to_thread(input, "Press Enter to send ONE harmless test message: ")
        result = await browser.send_message("Please reply with: browser transport test")
        print(result.status, result.submission, result.uncertain, result.error)
        if result.completed:
            print(result.response_text)
        # Otherwise STOP and inspect; do not retry an uncertain send.
    finally:
        closed = await browser.close()
        print("Cleanup:", closed.status, closed.uncertain)

asyncio.run(main())
```

Confirm the returned text is the new, finished answer (not an earlier answer).
Then reopen the same dedicated profile with `start()` and inspect whether login
remains available, **without resending**. Login persistence is subject to expiry
and account policy. Explicitly reselect a normal conversation after restart;
Temporary Chat recovery is never assumed.

## Maintaining the DOM contract

No live ChatGPT account was used to validate the initial selector contract.
ChatGPT UI variations/locales may require updates before the manual smoke passes.
Inspect only visible DOM, never private endpoints, storage, cookies, or auth state.

Update `ChatGPTSelectors` and snapshot/completion detection in `chatgpt_ui.py`.
Selectors prefer scoped test IDs and explicit accessible names; there are no
positional button indices or generic fallback clicks. Require exactly one main,
composer, send button, and account indicator. Stable unique message IDs and
turn-local completion controls are part of the safety contract.
An alternate selector set can be supplied via `ChatGPTUI(ChatGPTSelectors(...))`.

Update the offline HTML fixture to reflect the new visible markup and add
regressions for wrong conversations, unrelated controls, partial streams and
ambiguous responses. Run fake and DOM tests, then optionally repeat the manual
smoke on a harmless chat. Never relax completion/identity evidence merely to
make an unknown UI appear successful. See [known issues](known-issues.md).
