from types import SimpleNamespace

from auto_coding import diagnostics


def test_doctor_reports_python_and_tools(monkeypatch):
    monkeypatch.setattr(diagnostics, "sys", SimpleNamespace(version_info=(3, 12, 1), executable="python"))
    monkeypatch.setattr(diagnostics.shutil, "which", lambda executable: f"/bin/{executable}")
    monkeypatch.setattr(
        diagnostics.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="tool version\n", stderr=""),
    )
    results = diagnostics.run_doctor()
    assert [result.name for result in results] == ["Python", "Git", "GitHub CLI", "Codex CLI"]
    assert all(result.available for result in results)


def test_doctor_handles_missing_tool(monkeypatch):
    monkeypatch.setattr(diagnostics.shutil, "which", lambda executable: None if executable == "codex" else "/bin/tool")
    results = diagnostics.run_doctor()
    codex = next(result for result in results if result.name == "Codex CLI")
    assert not codex.available
    assert "not found" in codex.detail

