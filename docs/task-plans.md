# Offline task plans (schema version 1)

A plan is a complete prewritten batch. Review the entire file before approving
it: approval is explicit local consent, not an AI decision or a cryptographic
signature. The importer never launches Codex, tests, Git, GitHub, or APIs.

## PowerShell usage

With the existing Python 3.12 environment and editable installation:

```powershell
python -m auto_coding plan validate examples/task-plan.json
python -m auto_coding plan import examples/task-plan.json --db runtime/tasks.sqlite3 --approve
```

Validation reads only the input file: no database, directories or logs are
created. It checks syntax and structure without inspecting the repository,
resolving executables, substituting placeholders, or running approved commands.
Missing approval and invalid input are rejected before a database is opened.
Commands return 0 on success and 1 on rejection/storage failure (argparse usage
errors return 2). Successful output includes batch ID, task count and SHA-256,
never prompts or test arguments. Neither command alters Git or calls a network.

The three-task [example](../examples/task-plan.json) is illustrative only: it
freezes documentation tasks and existing test argument arrays, not an execution
request. Import it into a disposable database when trying the commands.
The model string is preserved, not checked against a CLI, subscription or API.

## Exact JSON contract

JSON must be UTF-8 (an optional UTF-8 BOM is accepted). Duplicate JSON object
keys, unknown fields, non-finite numbers and unsupported versions are rejected.
JSON comments/trailing commas are not supported.

Batch fields (all required):

| Field | Type / rule |
| --- | --- |
| `schema_version` | Integer `1`; booleans are not integers in this format |
| `batch_id` | Unique simple identifier, 1–128 ASCII letters/digits/`_.-`, starting with a letter/digit |
| `expected_branch` | Literal short development branch name, not `main`, `master` or `HEAD` (case-insensitive), ref syntax, or a malformed Git branch |
| `tasks` | Nonempty array in strictly increasing task order |

Task fields:

| Field | Type / rule |
| --- | --- |
| `task_id` | Required identifier, same syntax as batch ID; unique across the database |
| `title` | Required nonempty string |
| `prompt` | Required complete nonempty original prompt; Unicode, whitespace and line endings are preserved |
| `model` | Required literal model identifier; never substituted |
| `reasoning_effort` | Required `minimal`, `low`, `medium`, `high` or `xhigh` |
| `task_order` | Required positive integer up to `2**63 - 1`, unique within the batch; array order must already be increasing (gaps allowed) |
| `acceptance_criteria` | Required nonempty array of nonempty strings |
| `dependencies` | Required array of unique IDs of earlier tasks in this batch; `[]` allowed |
| `permitted_files` | Required nonempty array of unique explicit repository-relative file paths |
| `test_commands` | Required nonempty array of nonempty argument arrays, e.g. `[["python", "-m", "pytest"]]` |
| `approved_placeholders` | Optional array of unique approved names: `"BRANCH"` and/or `"LATEST_SHA"`; omitted means none approved |

Dependencies cannot be external, missing, self-referential, forward-referencing
or circular. Nothing is sorted, deduplicated, trimmed, expanded or rewritten to
make an invalid plan pass. All supplied fields are retained. Unsupported reasoning
efforts are rejected structurally; supported values are not a promise that any
particular model/CLI accepts that combination.

Permitted paths use forward slashes or Windows backslashes, including Unicode
and spaces. Absolute paths, drives, UNC, `.`/`..`/empty components, wildcard and
pathspec magic, colon/alternate streams, Windows device names, trailing dots/spaces,
credential-sensitive paths and `.git` are refused. Windows-equivalent duplicates
(case and separators) are rejected. Paths need not exist yet; this validation
does **not** establish real filesystem containment or rule out symlinks. Existing
Git runtime containment and credential checks are still necessary before execution.

Test arguments must be strings without NUL; the executable must be nonblank.
An empty non-executable argument is legal and retained. A command string such as
`"python -m pytest"` is not an argument array. Executable availability, command
meaning and safety are not inferred. Approved commands can execute arbitrary
programs later: human approval must cover every argument, not just the prompt.
The importer does not install anything or evaluate commands.

Only exact prompt syntax `{{BRANCH}}` and `{{LATEST_SHA}}` is supported, and each
used name must be listed in that task's approvals. Unknown, unapproved, whitespace
variants and unmatched double braces are rejected. This also means literal double
braces in a prompt are reserved, not an escaping mechanism in version 1.
Unused known approvals are allowed and fingerprinted. Placeholders in test
arguments are forbidden. No placeholders are expanded during validation/import;
the exact original prompt remains stored. No future value of `LATEST_SHA` is
guessed at import time.

## Fingerprint and atomic freezing

`auto_coding.plans.load_plan(path)` / `validate_plan(data)` return a frozen
`FrozenPlan` snapshot. `to_dict()` returns a detached copy.
Canonicalization is Python JSON with `sort_keys=True`, `ensure_ascii=False`,
`separators=(",", ":")` and `allow_nan=False`. The fingerprint is lowercase
SHA-256 of its UTF-8 bytes.

Object key order, file indentation and equivalent JSON escapes do not affect the
fingerprint. Every defined value does: schema version, batch/branch, every task
field, order of arrays, full prompt text, model, reasoning effort, criteria,
dependencies, scopes, commands, arguments and placeholder approvals. Omission
of an optional field is distinct from explicitly supplying an empty array.
Runtime status, timestamps and commit metadata are not approved-plan inputs and
are not included. The fingerprint detects definition differences; it is not a
signature, authorization mechanism or defense against an owner rewriting SQLite.

`TaskStore.import_plan(plan, approved=True)` revalidates even a manually
constructed `FrozenPlan`. It takes a SQLite write lock with `BEGIN IMMEDIATE`,
checks batch/task collisions, and inserts batch metadata, every queued task and
membership in one transaction. Any insert/constraint failure rolls back all of
them. Even an identical reimport is rejected; there is no replacement, upsert,
automatic renaming, or merge behavior.

Schema additions are `task_plans`, `plan_tasks` and immutability triggers,
initialized in the existing schema transaction. Existing task/commit rows remain
untouched. Frozen definition columns, task identities and membership cannot be
updated/deleted/replaced through normal SQL. Status, `updated_at` and existing
commit metadata may change. `get_plan(batch_id)` revalidates canonical JSON and
its fingerprint. `list_plan_tasks(batch_id)` returns the batch's tasks in order;
legacy `list_tasks()` still lists all stored tasks with its original ordering.
Plan definitions and prompts are local private data, not encrypted at rest.
Runtime databases remain excluded from Git. Do not put API keys in plans.

## Execution boundary

This task does **not** connect plans to execution. Imported tasks are queued,
but `claim_task` raises `PlanExecutionUnavailableError` for plan members.
Consequently the legacy single-task controller cannot silently ignore their
reasoning effort, dependencies, frozen tests/scope or unresolved placeholders.
No task status is changed by that refusal. Existing manually added tasks,
controller safeguards, Git operations, locks and process containment are unchanged.
Manual status changes do not enable imported-task execution.

A separately approved plan-aware controller must enforce those fields and
placeholder resolution before execution is enabled. No multi-task loop, reviewer,
API, Gemini integration or PR automation is implemented here.
