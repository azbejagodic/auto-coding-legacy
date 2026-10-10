"""Explicit, conservative Git operations without workflow orchestration."""

from __future__ import annotations

from dataclasses import dataclass
import logging
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
from typing import Sequence
from urllib.parse import urlsplit
from uuid import uuid4


logger = logging.getLogger(__name__)
_SHA = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")


class GitError(RuntimeError):
    """Predictable failure; diagnostics exclude raw potentially secret output."""

    success = False

    def __init__(self, operation: str, detail: str, exit_code: int | None = None):
        self.operation = operation
        self.detail = detail
        self.exit_code = exit_code
        super().__init__(f"{operation}: {detail}")


@dataclass(frozen=True)
class GitResult:
    repository_path: Path
    branch: str
    commit_sha: str
    github_commit_url: str | None = None
    success: bool = True


@dataclass(frozen=True)
class RepositoryState:
    repository_path: Path
    branch: str
    commit_sha: str
    has_changes: bool
    origin_exists: bool
    success: bool = True


def _github_url(remote: str, sha: str) -> str | None:
    """Recognize github.com HTTPS, SSH URLs, and scp-style SSH remotes."""
    if remote.startswith("git@github.com:"):
        path = remote[len("git@github.com:"):]
    else:
        try:
            parsed = urlsplit(remote)
        except ValueError:
            return None
        if parsed.scheme not in ("https", "ssh") or parsed.hostname != "github.com":
            return None
        path = parsed.path.lstrip("/")
    path = path.removesuffix(".git")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", path):
        return None
    return f"https://github.com/{path}/commit/{sha}"


class GitRepository:
    """Operate on one repository root. Callers must serialize repository access.

    No mutations happen on import or construction. Git hooks and interactive
    terminal prompts are disabled per invocation; global config is untouched.
    Failures after staging leave the index intact for manual inspection.
    """

    def __init__(self, repository_path: str | Path, timeout: float = 60.0):
        if not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
            raise GitError("validate", "Timeout must be finite and positive")
        self.timeout = timeout
        self.executable = shutil.which("git")
        if self.executable is None:
            raise GitError("validate", "Git was not found on PATH")
        if Path(self.executable).suffix.lower() in (".cmd", ".bat"):
            raise GitError("validate", "Use a native Git executable, not a batch shim")
        try:
            self.path = Path(repository_path).resolve()
            if not self.path.is_dir():
                raise GitError("validate", "Repository directory does not exist")
        except (OSError, ValueError, TypeError) as exc:
            raise GitError("validate", "Repository directory is inaccessible or invalid") from exc
        checked = self._run("rev-parse", "--is-inside-work-tree", allowed=(0, 128))
        if checked.returncode != 0 or checked.stdout.strip() != "true":
            raise GitError("validate", "Directory is not a working Git repository")
        root = Path(self._run("rev-parse", "--show-toplevel").stdout.strip()).resolve()
        if root != self.path:
            raise GitError("validate", "Supply the repository root, not a subdirectory")

    def _run(self, *args: str, allowed: tuple[int, ...] = (0,), input: str | None = None) -> subprocess.CompletedProcess[str]:
        # Ignore inherited Git repository/index overrides: all commands target
        # the configured root. Keep PATH/auth helpers available for normal Git.
        environment = {key: value for key, value in os.environ.items()
                       if not key.upper().startswith("GIT_")}
        environment["GIT_TERMINAL_PROMPT"] = "0"
        environment["GCM_INTERACTIVE"] = "Never"
        # check-ignore takes literal file names already and rejects pathspec
        # magic, including the global literal-pathspecs option.
        literal = [] if args[0] == "check-ignore" else ["--literal-pathspecs"]
        try:
            completed = subprocess.run(
                [self.executable, *literal, "-c", f"core.hooksPath={os.devnull}", *args],
                cwd=self.path, input=input, capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=self.timeout,
                check=False, shell=False, env=environment,
            )
        except subprocess.TimeoutExpired:
            logger.warning("Git %s timed out", args[0])
            raise GitError(args[0], "Git command timed out; inspect repository/remote state before retrying") from None
        except OSError as exc:
            logger.warning("Git %s failed to launch", args[0])
            raise GitError(args[0], f"Git could not launch (errno={exc.errno})") from None
        if completed.returncode not in allowed:
            output = completed.stderr.lower()
            category = "Git command failed"
            if any(word in output for word in ("authentication", "permission denied", "could not read username")):
                category = "Authentication or permission failure"
            elif any(word in output for word in ("resolve host", "unable to access", "connection", "network")):
                category = "Network or remote access failure"
            logger.warning("Git %s failed: exit_code=%s", args[0], completed.returncode)
            raise GitError(args[0], f"{category} (exit code {completed.returncode})", completed.returncode)
        return completed

    def active_branch(self) -> str:
        ref = self._run("symbolic-ref", "--quiet", "HEAD", allowed=(0, 1)).stdout.strip()
        if not ref.startswith("refs/heads/"):
            raise GitError("branch", "Detached HEAD is not supported")
        return ref.removeprefix("refs/heads/")

    def current_commit_sha(self) -> str:
        return self._sha(self._run("rev-parse", "--verify", "HEAD^{commit}").stdout.strip())

    @staticmethod
    def _sha(value: str) -> str:
        if not _SHA.fullmatch(value):
            raise GitError("verify", "Git returned an invalid full commit SHA")
        return value

    def verify_origin(self) -> str:
        if "origin" not in self._run("remote").stdout.splitlines():
            raise GitError("origin", "The origin remote does not exist")
        fetch_urls = self._run("remote", "get-url", "--all", "origin").stdout.splitlines()
        push_urls = self._run("remote", "get-url", "--push", "--all", "origin").stdout.splitlines()
        if len(fetch_urls) != 1 or fetch_urls != push_urls:
            raise GitError("origin", "Origin must have one matching fetch and push URL")
        return fetch_urls[0]

    def has_uncommitted_changes(self) -> bool:
        return bool(self._run("status", "--porcelain=v1", "--untracked-files=all").stdout)

    def validate(self) -> RepositoryState:
        self.verify_origin()
        return RepositoryState(self.path, self.active_branch(), self.current_commit_sha(),
                               self.has_uncommitted_changes(), True)

    def _branch_name(self, name: str) -> None:
        if not name or name.casefold() == "main" or name.startswith("-") or name == "HEAD":
            raise GitError("branch", "Invalid or protected development branch")
        self._run("check-ref-format", f"refs/heads/{name}")

    def _expect_branch(self, expected: str) -> None:
        self._branch_name(expected)
        branch = self.active_branch()
        if branch.casefold() == "main" or branch != expected:
            raise GitError("branch", "Active branch is protected or differs from the expected development branch")

    def _check_operation_state(self) -> None:
        root = Path(self._run("rev-parse", "--show-toplevel").stdout.strip()).resolve()
        if root != self.path:
            raise GitError("state", "Repository root changed; inspect local Git configuration")
        for marker in ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "rebase-merge", "rebase-apply", "BISECT_LOG", "sequencer"):
            path = Path(self._run("rev-parse", "--git-path", marker).stdout.strip())
            if not path.is_absolute():
                path = self.path / path
            if path.exists():
                raise GitError("state", "An unfinished Git operation requires manual attention")

    def create_development_branch(self, branch: str | None = None) -> GitResult:
        branch = branch if branch is not None else f"automation/batch-{uuid4().hex}"
        self._branch_name(branch)
        self._check_operation_state()
        if self.has_uncommitted_changes():
            raise GitError("create branch", "Repository must be clean")
        self.verify_origin()
        self.active_branch()  # Refuse unexpected detached state before mutation.
        if self._run("show-ref", "--verify", "--quiet", f"refs/heads/{branch}", allowed=(0, 1)).returncode == 0:
            raise GitError("create branch", "Development branch already exists locally")
        remote = self._run("ls-remote", "--exit-code", "--heads", "origin", f"refs/heads/{branch}", allowed=(0, 2))
        if remote.returncode == 0:
            raise GitError("create branch", "Development branch already exists on origin")
        self._run("fetch", "--no-tags", "origin", "refs/heads/main:refs/remotes/origin/main")
        base = self._sha(self._run("rev-parse", "--verify", "refs/remotes/origin/main^{commit}").stdout.strip())
        if self.has_uncommitted_changes():
            raise GitError("create branch", "Repository changed during fetch")
        self._run("switch", "--no-track", "-c", branch, base)
        self._expect_branch(branch)
        sha = self.current_commit_sha()
        if sha != base:
            raise GitError("create branch", "New branch does not match fetched main")
        logger.info("Development branch created: branch=%r sha=%s", branch, sha)
        return GitResult(self.path, branch, sha)

    def _selected_paths(self, files: Sequence[str | Path], *, allow_missing: bool = False) -> list[str]:
        if isinstance(files, (str, bytes)) or not files:
            raise GitError("commit", "Supply a nonempty explicit list of files")
        selected = []
        sensitive_parts = {".git", ".codex", ".aws", ".ssh", ".auth", "credentials", "secrets",
                           "browser-profile", "browser_profiles", "browser-profiles"}
        sensitive_names = {"auth.json", "credentials.json", "credentials", "storage-state.json",
                           "storage_state.json", "cookies.json", "cookies", "login data",
                           "token.json", "tokens.json", "session.json", "sessions.json",
                           ".netrc", "_netrc", ".npmrc", ".pypirc", "credentials.yml", "credentials.yaml",
                           "id_rsa", "id_ed25519", "config.toml"}
        for supplied in files:
            try:
                raw = Path(supplied)
                if ".." in raw.parts:
                    raise GitError("commit", "Parent traversal is not allowed")
                if any(any(char in part for char in "*?[]") or part.startswith(":") for part in raw.parts):
                    raise GitError("commit", "Wildcard and pathspec-magic inputs are forbidden")
                path = raw if raw.is_absolute() else self.path / raw
                relative = path.relative_to(self.path)
                resolved = path.resolve()
                resolved.relative_to(self.path)
                if resolved != path or path.is_symlink() or (path.exists() and not path.is_file()):
                    raise GitError("commit", "Selected paths must be regular files without symlinks")
            except (ValueError, TypeError, OSError):
                raise GitError("commit", "Selected file path is invalid or outside the repository") from None
            parts = {part.casefold() for part in relative.parts}
            name = path.name.casefold()
            if (parts & sensitive_parts or name in sensitive_names or name.startswith(".env")
                    or path.suffix.casefold() in {".pem", ".key", ".p12", ".pfx", ".secret"}):
                raise GitError("commit", "Credential or session file selection is forbidden")
            file = relative.as_posix()
            if self._run("check-ignore", "--no-index", "--quiet", "--", file, allowed=(0, 1)).returncode == 0:
                raise GitError("commit", "Ignored file selection is forbidden")
            if not path.exists() and not allow_missing:
                # The index must be clean before this method is called. Verify
                # an exact committed file, not a directory/prefix match, gitlink,
                # removed symlink, or an untracked missing name.
                entries = self._run("ls-tree", "-z", "HEAD", "--", file).stdout.split("\x00")
                records = [entry.partition("\t") for entry in entries if entry]
                if (len(records) != 1 or records[0][2] != file
                        or records[0][0].split()[:2] not in (["100644", "blob"], ["100755", "blob"])):
                    raise GitError("commit", "Missing path must be an exact tracked regular file")
            selected.append(file)
        return list(dict.fromkeys(selected))

    def commit_url(self, sha: str) -> str | None:
        """Return a credential-free GitHub URL, or None for other origins."""
        return _github_url(self.verify_origin(), self._sha(sha))

    def validate_execution(self, expected_branch: str, allowed_files: Sequence[str | Path]) -> RepositoryState:
        """Read-only preflight; repeat under the caller's workspace lock."""
        self._expect_branch(expected_branch)
        self._check_operation_state()
        state = self.validate()
        if state.has_changes:
            raise GitError("execute", "Working directory and staging area must be clean")
        self._selected_paths(allowed_files, allow_missing=True)
        if self.commit_url(state.commit_sha) is None:
            raise GitError("execute", "Automatic task execution requires a github.com origin")
        return state

    def changed_files(self, expected_branch: str, base_sha: str,
                      allowed_files: Sequence[str | Path], expected_origin: str) -> list[str]:
        """Discover literal changes, rejecting staged/unapproved files or Git drift."""
        self._expect_branch(expected_branch)
        self._check_operation_state()
        if self.current_commit_sha() != self._sha(base_sha):
            raise GitError("execute", "HEAD changed during task execution; manual recovery required")
        if self.verify_origin() != expected_origin:
            raise GitError("execute", "Origin changed during task execution")
        if self._run("diff", "--cached", "--name-only", "--no-renames", "-z").stdout:
            raise GitError("execute", "Unexpected staged changes; inspect the index manually")
        approved = {os.path.normcase(path) for path in self._selected_paths(allowed_files, allow_missing=True)}
        tracked = self._run("diff", "--name-only", "--no-renames", "-z", "HEAD", "--").stdout
        untracked = self._run("ls-files", "--others", "--exclude-standard", "-z").stdout
        files = list(dict.fromkeys(path for path in (tracked + untracked).split("\x00") if path))
        if not files:
            raise GitError("execute", "No valid changes to commit")
        selected = self._selected_paths(files)
        if any(os.path.normcase(path) not in approved for path in selected):
            raise GitError("execute", "Unexpected changed files outside the approved file list")
        return selected

    def commit_files(self, expected_branch: str, files: Sequence[str | Path], message: str,
                     *, expected_base_sha: str | None = None) -> GitResult:
        self._expect_branch(expected_branch)
        self._check_operation_state()
        if not isinstance(message, str) or not message.strip() or "\x00" in message:
            raise GitError("commit", "Commit message must be nonempty and contain no NUL")
        if self._run("diff", "--cached", "--name-only", "-z").stdout:
            raise GitError("commit", "Index already contains staged changes; inspect it manually")
        selected = self._selected_paths(files)
        before = self.current_commit_sha()
        if expected_base_sha is not None and before != self._sha(expected_base_sha):
            raise GitError("commit", "HEAD differs from the tested task baseline")
        self._run("add", "--no-ignore-removal", "--", *selected)
        staged = set(self._run("diff", "--cached", "--name-only", "-z").stdout.strip("\x00").split("\x00")) - {""}
        if not staged:
            raise GitError("commit", "Selected files have no changes; empty commits are forbidden")
        if not staged.issubset(set(selected)):
            raise GitError("commit", "Unexpected staged files; inspect the index manually")
        self._expect_branch(expected_branch)
        if self.current_commit_sha() != before:
            raise GitError("commit", "HEAD changed during staging")
        self._run("commit", "--no-gpg-sign", "--file=-", input=message)
        self._expect_branch(expected_branch)
        sha = self.current_commit_sha()
        if sha == before:
            raise GitError("commit", "Commit did not advance HEAD")
        logger.info("Development commit created: branch=%r sha=%s", expected_branch, sha)
        return GitResult(self.path, expected_branch, sha)

    def push_branch(self, expected_branch: str, expected_commit_sha: str) -> GitResult:
        self._expect_branch(expected_branch)
        self._check_operation_state()
        self._sha(expected_commit_sha)
        if self.current_commit_sha() != expected_commit_sha:
            raise GitError("push", "HEAD differs from the expected commit")
        origin = self.verify_origin()
        # Explicit refspec prevents push.default and configured extra refspecs
        # from selecting other branches. Disable inherited remote force/mirror.
        self._run("-c", "remote.origin.mirror=false", "push", "--no-force", "--no-follow-tags",
                  "origin", f"{expected_commit_sha}:refs/heads/{expected_branch}")
        remote = self._run("ls-remote", "--exit-code", "--heads", "origin", f"refs/heads/{expected_branch}")
        rows = [line.split() for line in remote.stdout.splitlines()]
        if rows != [[expected_commit_sha, f"refs/heads/{expected_branch}"]]:
            raise GitError("push", "Remote branch does not point to the expected commit")
        self._expect_branch(expected_branch)
        if self.current_commit_sha() != expected_commit_sha:
            raise GitError("push", "Local HEAD changed during push")
        logger.info("Development push verified: branch=%r sha=%s", expected_branch, expected_commit_sha)
        return GitResult(self.path, expected_branch, expected_commit_sha, _github_url(origin, expected_commit_sha))
