# Known issues

## Background processes started by approved test commands

The existing test runner does not contain successful commands in Windows Job
Objects. Commands that detach background children can leave processes modifying
the workspace after the test command exits; timeout cleanup also has documented
detached-descendant limitations. Workspace locks only coordinate cooperating
controllers, not these children or unrelated editors.

Use trusted foreground test commands that wait for all their children. Extending
test-runner process containment is outside TASK-007; its existing behavior has
not been changed. Codex's Windows Job Object containment remains in place.
