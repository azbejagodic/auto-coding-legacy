import os
from pathlib import Path
import subprocess
from unittest.mock import Mock

import pytest

from auto_coding.integrations.git import GitError, GitRepository, _github_url


def git(path, *args):
    return subprocess.run(
        ["git", "-c", f"core.hooksPath={os.devnull}", *args], cwd=path,
        capture_output=True, text=True, encoding="utf-8", check=True,
        timeout=15, shell=False,
    ).stdout.strip()


@pytest.fixture
def repository(tmp_path):
    path = tmp_path / "repo"
    path.mkdir()
    git(path, "init", "-b", "main")
    # Local-only identity: no global config is changed by tests.
    git(path, "config", "--local", "user.name", "Test User")
    git(path, "config", "--local", "user.email", "tests@example.invalid")
    (path / "initial.txt").write_text("initial\n", encoding="utf-8")
    git(path, "add", "--", "initial.txt")
    git(path, "commit", "--no-gpg-sign", "-m", "Initial")
    origin = tmp_path / "origin.git"
    origin.mkdir()
    git(origin, "init", "--bare", "-b", "main")
    git(path, "remote", "add", "origin", str(origin))
    git(path, "push", "origin", "main")  # Temporary local bare remote only.
    return GitRepository(path)


def test_validate_and_full_commit_sha(repository):
    state = repository.validate()
    assert state.success and state.origin_exists
    assert state.branch == "main"
    assert not state.has_changes
    assert state.repository_path == repository.path
    assert len(state.commit_sha) == 40
    assert state.commit_sha == git(repository.path, "rev-parse", "HEAD")


def test_missing_git(monkeypatch, tmp_path):
    monkeypatch.setattr("auto_coding.integrations.git.shutil.which", lambda name: None)
    with pytest.raises(GitError, match="not found on PATH"):
        GitRepository(tmp_path)


def test_invalid_repository(tmp_path):
    with pytest.raises(GitError):
        GitRepository(tmp_path)
    with pytest.raises(GitError, match="does not exist"):
        GitRepository(tmp_path / "missing")


def test_missing_origin(repository):
    git(repository.path, "remote", "remove", "origin")
    with pytest.raises(GitError, match="origin remote does not exist"):
        repository.validate()


def test_subdirectory_and_detached_head_rejected(repository):
    sub = repository.path / "sub"
    sub.mkdir()
    with pytest.raises(GitError, match="repository root"):
        GitRepository(sub)
    git(repository.path, "switch", "--detach")
    with pytest.raises(GitError, match="Detached HEAD"):
        repository.active_branch()


def test_create_branch_from_latest_remote_main(repository):
    # Put the checkout on an older branch, then advance origin/main.
    old_sha = repository.current_commit_sha()
    git(repository.path, "branch", "old", old_sha)
    (repository.path / "initial.txt").write_text("new main\n", encoding="utf-8")
    git(repository.path, "add", "--", "initial.txt")
    git(repository.path, "commit", "--no-gpg-sign", "-m", "Advance remote main")
    latest = repository.current_commit_sha()
    git(repository.path, "push", "origin", "main")
    git(repository.path, "switch", "old")
    result = repository.create_development_branch("automation/batch-001")
    assert result.success and result.branch == repository.active_branch()
    assert result.commit_sha == latest != old_sha
    assert git(repository.path, "rev-parse", "main") == latest


def test_generated_branch_name_is_unique(repository):
    first = repository.create_development_branch()
    second = repository.create_development_branch()
    assert first.branch.startswith("automation/batch-")
    assert first.branch != second.branch


def test_dirty_tree_rejected_without_discarding_changes(repository):
    file = repository.path / "untracked.txt"
    file.write_text("keep me", encoding="utf-8")
    assert repository.has_uncommitted_changes()
    with pytest.raises(GitError, match="must be clean"):
        repository.create_development_branch("automation/batch-001")
    assert repository.active_branch() == "main"
    assert file.read_text(encoding="utf-8") == "keep me"


def test_existing_branch_rejected_locally_and_remotely(repository):
    branch = "automation/existing"
    git(repository.path, "branch", branch)
    with pytest.raises(GitError, match="exists locally"):
        repository.create_development_branch(branch)
    git(repository.path, "push", "origin", f"{branch}:{branch}")
    # Use a fresh name for the local check while retaining the remote branch.
    git(repository.path, "branch", "-m", branch, "automation/renamed")
    with pytest.raises(GitError, match="exists on origin"):
        repository.create_development_branch(branch)


def test_main_and_unexpected_branch_are_protected(repository):
    # An identically named tag must not make main appear as "heads/main".
    git(repository.path, "tag", "main")
    assert repository.active_branch() == "main"
    for expected in ("main", "automation/other"):
        with pytest.raises(GitError, match="protected|expected"):
            repository.commit_files(expected, ["initial.txt"], "message")
        with pytest.raises(GitError, match="protected|expected"):
            repository.push_branch(expected, repository.current_commit_sha())
    assert repository.active_branch() == "main"


def test_commit_stages_only_explicit_files(repository):
    branch = repository.create_development_branch("automation/batch-001").branch
    (repository.path / "chosen.txt").write_text("chosen\n", encoding="utf-8")
    (repository.path / "user.txt").write_text("do not stage\n", encoding="utf-8")
    message = "Commit quotes ' and shell $(literal)\n\nFull message"
    result = repository.commit_files(branch, ["chosen.txt"], message)
    assert result.commit_sha == repository.current_commit_sha()
    assert git(repository.path, "show", "--format=", "--name-only", "HEAD") == "chosen.txt"
    assert git(repository.path, "log", "-1", "--format=%B") == message
    assert "user.txt" in git(repository.path, "status", "--porcelain")


def test_existing_staged_changes_rejected(repository):
    branch = repository.create_development_branch().branch
    (repository.path / "user.txt").write_text("keep staged", encoding="utf-8")
    git(repository.path, "add", "--", "user.txt")
    with pytest.raises(GitError, match="already contains staged"):
        repository.commit_files(branch, ["initial.txt"], "message")
    assert git(repository.path, "diff", "--cached", "--name-only") == "user.txt"


@pytest.mark.parametrize("file", ["missing.txt", "../outside.txt", ".", ".git/config"])
def test_invalid_files_rejected(repository, file):
    branch = repository.create_development_branch().branch
    with pytest.raises(GitError):
        repository.commit_files(branch, [file], "message")
    assert not git(repository.path, "diff", "--cached", "--name-only")


def test_absolute_outside_path_rejected(repository, tmp_path):
    branch = repository.create_development_branch().branch
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    with pytest.raises(GitError, match="outside"):
        repository.commit_files(branch, [outside], "message")


@pytest.mark.parametrize("name", [".env", "auth.json", "cookies.json", "credentials/key.txt", "browser_profiles/state.json", "private.pem"])
def test_sensitive_files_rejected(repository, name):
    branch = repository.create_development_branch().branch
    file = repository.path / name
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text("private", encoding="utf-8")
    with pytest.raises(GitError, match="Credential or session"):
        repository.commit_files(branch, [name], "message")


def test_ignored_files_rejected(repository):
    branch = repository.create_development_branch().branch
    (repository.path / ".gitignore").write_text("ignored.txt\n", encoding="utf-8")
    (repository.path / "ignored.txt").write_text("ignored", encoding="utf-8")
    with pytest.raises(GitError, match="Ignored"):
        repository.commit_files(branch, ["ignored.txt"], "message")


def test_empty_commit_rejected(repository):
    branch = repository.create_development_branch().branch
    with pytest.raises(GitError, match="empty commits"):
        repository.commit_files(branch, ["initial.txt"], "message")
    with pytest.raises(GitError, match="nonempty explicit"):
        repository.commit_files(branch, [], "message")


def test_push_to_local_remote_and_verify_commit(repository):
    branch = repository.create_development_branch().branch
    (repository.path / "change.txt").write_text("change", encoding="utf-8")
    commit = repository.commit_files(branch, ["change.txt"], "Add change")
    result = repository.push_branch(branch, commit.commit_sha)
    assert result.success and result.commit_sha == commit.commit_sha
    assert result.github_commit_url is None
    assert git(repository.path, "ls-remote", "origin", f"refs/heads/{branch}").split()[0] == commit.commit_sha


def test_push_verification_mismatch(repository, monkeypatch):
    branch = repository.create_development_branch().branch
    original = repository._run

    def run(*args, **kwargs):
        if args[0] == "ls-remote":
            return subprocess.CompletedProcess(args, 0, f"{'0' * 40}\trefs/heads/{branch}\n", "")
        return original(*args, **kwargs)

    monkeypatch.setattr(repository, "_run", run)
    with pytest.raises(GitError, match="does not point"):
        repository.push_branch(branch, repository.current_commit_sha())


def test_wrong_expected_sha_prevents_push(repository):
    branch = repository.create_development_branch().branch
    with pytest.raises(GitError, match="HEAD differs"):
        repository.push_branch(branch, "0" * 40)


def test_github_push_result_and_explicit_refspec(repository, monkeypatch):
    branch = repository.create_development_branch().branch
    sha = repository.current_commit_sha()
    original = repository._run
    pushed = []

    def run(*args, **kwargs):
        if args[:2] == ("remote", "get-url"):
            return subprocess.CompletedProcess(args, 0, "https://github.com/team/project.git\n", "")
        if "push" in args:
            pushed.append(args)
            return subprocess.CompletedProcess(args, 0, "", "")
        if args[0] == "ls-remote":
            return subprocess.CompletedProcess(args, 0, f"{sha}\trefs/heads/{branch}\n", "")
        return original(*args, **kwargs)

    monkeypatch.setattr(repository, "_run", run)
    result = repository.push_branch(branch, sha)
    assert result.github_commit_url == f"https://github.com/team/project/commit/{sha}"
    assert pushed == [("-c", "remote.origin.mirror=false", "push", "--no-force", "--no-follow-tags",
                       "origin", f"refs/heads/{branch}:refs/heads/{branch}")]


def test_push_command_failure_stops_verification(repository, monkeypatch):
    branch = repository.create_development_branch().branch
    sha = repository.current_commit_sha()
    original = repository._run

    def run(*args, **kwargs):
        if "push" in args:
            raise GitError("push", "Authentication failure", 128)
        if args[0] == "ls-remote":
            pytest.fail("Remote verification must not run after a failed push")
        return original(*args, **kwargs)

    monkeypatch.setattr(repository, "_run", run)
    with pytest.raises(GitError, match="Authentication"):
        repository.push_branch(branch, sha)


@pytest.mark.parametrize("stderr, category", [
    ("Authentication failed https://token:secret@github.com/x/y", "Authentication"),
    ("Could not resolve host github.com", "Network"),
    ("rejected non-fast-forward", "Git command failed"),
])
def test_command_failure_is_clear_and_does_not_expose_tokens(repository, monkeypatch, caplog, stderr, category):
    process = Mock(return_value=subprocess.CompletedProcess([], 128, "", stderr))
    monkeypatch.setattr("auto_coding.integrations.git.subprocess.run", process)
    with pytest.raises(GitError, match=category) as error:
        repository._run("push", "origin", "refs/heads/test:refs/heads/test")
    assert error.value.exit_code == 128
    assert "secret" not in str(error.value) + caplog.text
    assert process.call_args.kwargs["shell"] is False


@pytest.mark.parametrize("failure", [subprocess.TimeoutExpired(["git"], 1), OSError(5, "private")])
def test_process_errors(repository, monkeypatch, failure):
    monkeypatch.setattr("auto_coding.integrations.git.subprocess.run", Mock(side_effect=failure))
    with pytest.raises(GitError):
        repository._run("push", "origin", "refs/heads/test:refs/heads/test")


@pytest.mark.parametrize("remote", ["https://github.com/team/project.git", "git@github.com:team/project.git", "ssh://git@github.com/team/project.git", "https://token:secret@github.com/team/project.git"])
def test_github_commit_url_without_credentials(remote):
    assert _github_url(remote, "a" * 40) == f"https://github.com/team/project/commit/{'a' * 40}"


def test_non_github_url_not_fabricated():
    assert _github_url("https://example.org/team/project.git", "a" * 40) is None
    assert _github_url("https://[invalid", "a" * 40) is None


def test_unfinished_operation_rejected(repository):
    branch = repository.create_development_branch().branch
    (repository.path / ".git" / "MERGE_HEAD").write_text(repository.current_commit_sha(), encoding="utf-8")
    with pytest.raises(GitError, match="unfinished"):
        repository.commit_files(branch, ["initial.txt"], "message")


def test_tracked_deletion_is_explicitly_staged_and_committed(repository, monkeypatch):
    branch = repository.create_development_branch().branch
    previous = repository.current_commit_sha()
    (repository.path / "initial.txt").unlink()
    (repository.path / "unselected.txt").write_text("keep untracked", encoding="utf-8")
    original = repository._run
    staged = []

    def run(*args, **kwargs):
        if args[0] == "commit":
            staged.append(git(repository.path, "diff", "--cached", "--name-status"))
        return original(*args, **kwargs)

    monkeypatch.setattr(repository, "_run", run)
    result = repository.commit_files(branch, ["initial.txt"], "Delete selected file")
    assert staged == ["D\tinitial.txt"]
    assert result.commit_sha != previous
    assert result.commit_sha == repository.current_commit_sha()
    assert git(repository.path, "diff-tree", "--no-commit-id", "--name-status", "-r", "HEAD") == "D\tinitial.txt"
    assert not (repository.path / "initial.txt").exists()
    assert "unselected.txt" in git(repository.path, "status", "--porcelain")


def test_mixed_add_modify_delete_only_selected_files(repository):
    branch = repository.create_development_branch().branch
    for name in ("modify.txt", "untouched.txt"):
        (repository.path / name).write_text("original\n", encoding="utf-8")
    repository.commit_files(branch, ["modify.txt", "untouched.txt"], "Seed tracked files")
    (repository.path / "initial.txt").unlink()
    (repository.path / "modify.txt").write_text("modified\n", encoding="utf-8")
    (repository.path / "added.txt").write_text("new content\n", encoding="utf-8")
    (repository.path / "untouched.txt").unlink()  # Deliberately not selected.
    result = repository.commit_files(branch, ["initial.txt", "modify.txt", "added.txt"], "Mixed changes")
    assert set(git(repository.path, "diff-tree", "--no-commit-id", "--no-renames", "--name-status", "-r", "HEAD").splitlines()) == {
        "D\tinitial.txt", "M\tmodify.txt", "A\tadded.txt",
    }
    assert len(result.commit_sha) == 40
    assert git(repository.path, "status", "--porcelain") == "D untouched.txt"


def test_untracked_missing_file_does_not_stage_valid_deletion(repository):
    branch = repository.create_development_branch().branch
    (repository.path / "initial.txt").unlink()
    with pytest.raises(GitError, match="exact tracked regular file"):
        repository.commit_files(branch, ["initial.txt", "never-tracked.txt"], "Reject plan")
    assert not git(repository.path, "diff", "--cached", "--name-only")
    assert git(repository.path, "status", "--porcelain") == "D initial.txt"


@pytest.mark.parametrize("name", [".env", "auth.json", "credentials/token.json"])
def test_tracked_sensitive_deletions_still_rejected(repository, name):
    branch = repository.create_development_branch().branch
    path = repository.path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("dummy fixture data", encoding="utf-8")
    git(repository.path, "add", "--", name)
    git(repository.path, "commit", "--no-gpg-sign", "-m", "Seed sensitive-name fixture")
    path.unlink()
    with pytest.raises(GitError, match="Credential or session"):
        repository.commit_files(branch, [name], "Reject sensitive deletion")
    assert not git(repository.path, "diff", "--cached", "--name-only")


def test_missing_protected_and_outside_paths_rejected(repository, tmp_path):
    branch = repository.create_development_branch().branch
    for path in (".git/missing", "../missing.txt", tmp_path / "missing.txt"):
        with pytest.raises(GitError):
            repository.commit_files(branch, [path], "Reject unsafe deletion")
    assert not git(repository.path, "diff", "--cached", "--name-only")


@pytest.mark.parametrize("pathspec", ["*", "*.txt", "initial.?xt", "[i]nitial.txt", ":(glob)*", ":(top)initial.txt", ":!initial.txt"])
def test_unsafe_deletion_pathspecs_rejected(repository, pathspec):
    branch = repository.create_development_branch().branch
    (repository.path / "initial.txt").unlink()
    with pytest.raises(GitError, match="Wildcard and pathspec-magic"):
        repository.commit_files(branch, [pathspec], "Reject pathspec")
    assert not git(repository.path, "diff", "--cached", "--name-only")


def test_deleted_file_on_main_cannot_be_committed(repository):
    sha = repository.current_commit_sha()
    (repository.path / "initial.txt").unlink()
    with pytest.raises(GitError, match="protected"):
        repository.commit_files("main", ["initial.txt"], "Forbidden")
    assert repository.current_commit_sha() == sha
    assert not git(repository.path, "diff", "--cached", "--name-only")


def test_deleted_directory_not_treated_as_a_file(repository):
    branch = repository.create_development_branch().branch
    directory = repository.path / "nested"
    directory.mkdir()
    file = directory / "file.txt"
    file.write_text("tracked", encoding="utf-8")
    repository.commit_files(branch, ["nested/file.txt"], "Seed nested file")
    file.unlink()
    directory.rmdir()
    with pytest.raises(GitError, match="exact tracked regular file"):
        repository.commit_files(branch, ["nested"], "Reject directory selection")
    assert not git(repository.path, "diff", "--cached", "--name-only")
    # A missing parent directory still permits its exact selected tracked file.
    repository.commit_files(branch, ["nested/file.txt"], "Delete nested file")


def test_deleted_file_rejects_symlink_parent_escape(repository, tmp_path, monkeypatch):
    branch = repository.create_development_branch().branch
    candidate = repository.path / "link" / "deleted.txt"
    original = Path.resolve

    def resolve(path, *args, **kwargs):
        # Simulate a symlink/junction on Windows without requiring symlink privileges.
        if path == candidate:
            return tmp_path / "outside" / "deleted.txt"
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", resolve)
    with pytest.raises(GitError, match="outside the repository"):
        repository.commit_files(branch, ["link/deleted.txt"], "Reject escape")
    assert not git(repository.path, "diff", "--cached", "--name-only")


def test_deleted_symlink_git_mode_rejected(repository, monkeypatch):
    branch = repository.create_development_branch().branch
    original = repository._run

    def run(*args, **kwargs):
        if args[0] == "ls-tree":
            return subprocess.CompletedProcess(args, 0, f"120000 blob {'a' * 40}\tdeleted-link\x00", "")
        return original(*args, **kwargs)

    monkeypatch.setattr(repository, "_run", run)
    with pytest.raises(GitError, match="exact tracked regular file"):
        repository.commit_files(branch, ["deleted-link"], "Reject deleted symlink")


def test_deletion_accepts_native_absolute_path_with_spaces(repository):
    branch = repository.create_development_branch().branch
    file = repository.path / "nested folder" / "deleted file.txt"
    file.parent.mkdir()
    file.write_text("tracked", encoding="utf-8")
    repository.commit_files(branch, [file], "Seed spaced path")
    file.unlink()
    repository.commit_files(branch, [str(file)], "Delete native absolute path")
    assert git(repository.path, "diff-tree", "--no-commit-id", "--name-status", "-r", "HEAD") == "D\tnested folder/deleted file.txt"
