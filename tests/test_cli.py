from auto_coding.cli import main


def test_doctor_returns_failure_when_dependency_is_missing(monkeypatch, capsys):
    monkeypatch.setattr(
        "auto_coding.cli.run_doctor",
        lambda: [type("Result", (), {"available": False, "format": lambda self: "[FAIL] test"})()],
    )
    assert main(["doctor"]) == 1
    assert "[FAIL] test" in capsys.readouterr().out

