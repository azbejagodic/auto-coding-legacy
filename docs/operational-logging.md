# Operational diagnostics

Authorized CLI batch execution and review recovery (`--execute`) enable a
dedicated Python logging handler at `runtime/logs/operations.jsonl`, relative
to the controller's current directory. `runtime/` and log directories are
already excluded by `.gitignore`. No new service or database is involved.
Doctor, plan validation/import, batch status and recovery preflight do not
configure file logging or create operational logs.

The UTF-8 JSONL file rotates at 5,000,000 bytes, retaining three backups named
`operations.jsonl.1` through `.3`. Concise stage/outcome messages go to stderr;
token metrics and error diagnostics stay in the file. This dedicated logger
does not capture SDK, subprocess or existing free-form application messages.

From the controller repository, view the latest events in PowerShell:

```powershell
Get-Content .\runtime\logs\operations.jsonl -Tail 30
```

Follow new events (restart after rotation if following the old file):

```powershell
Get-Content .\runtime\logs\operations.jsonl -Tail 10 -Wait
```

Each event has an ISO-8601 UTC timestamp, event name, stage, outcome,
`duration_seconds`, and nullable batch/task IDs. Applicable events include
operation/span/parent-span IDs, frozen fingerprint, commit SHA, review/recovery
IDs, and chunk identity/index/count. Start and instantaneous transition events
have zero duration; completed spans measure elapsed time using a monotonic clock.
Standalone integrations have no batch/task identity unless the caller supplies it.

Groq events distinguish planning, local blocks, submission intent, completed or
failed requests, and the final decision. Input estimates include framing and
safety margin; output estimates are the existing maximum output reservation,
not a prediction. Wire units, local token estimates and actual reported
input/output/total tokens are separate fields. Missing actual usage is not
invented; review totals include `unknown_usage_requests`. Submission intent
does not prove provider delivery. Review decision events include the exact
source-owned technical reason for UNCERTAIN, or distinguish provider uncertainty,
incomplete coverage and unchecked criteria without exposing model findings.
Sanitized numeric HTTP status, allowlisted provider error type/code and safe
rate-limit headers/observation timestamps are included when available.
Internal Git command timeouts, launch failures and nonzero exits also record
the command category, duration and safe error category, never its arguments or
output. Existing timeouts and fail-closed verification are unchanged.

Only allowlisted metadata is emitted. Prompts, test arguments/output, diffs,
request/response bodies, findings, URLs, credentials and arbitrary exception
messages are excluded. Identifier fields are bounded/screened; known secret
patterns and sensitive environment values are withheld. Unknown diagnostic
text is withheld rather than copied into logs. This is defense in depth, not
permission to put secrets in identifiers. Keep local logs private.

Logging does not change SQLite records, task/review decisions, authorization,
retry rules, process containment or Git operations. Handler/setup failures
produce a static terminal warning when possible and never turn a failed task
into success or prevent execution. A full/unwritable disk can lose diagnostics;
logs are best-effort observability, not authoritative recovery/audit records.
SQLite history and existing recovery checks remain authoritative.

Programmatic callers can opt in before execution:

```python
from auto_coding.operations import configure_operations, operation_context

configure_operations()  # Optional configurable directory; no import-time I/O.
with operation_context(batch_id="BATCH", task_id="TASK-1"):
    # Call the existing integration/controller interface here.
    pass
```

Standard-library rotation is intended for one local controller process per
log destination. Independent concurrent processes should use separate configured
directories; this task does not introduce cross-process logging coordination.
