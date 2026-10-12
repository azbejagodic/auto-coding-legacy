"""The live script is exercised only with a mocked SDK; never opted in by pytest."""

import importlib.util
from pathlib import Path
from unittest.mock import Mock

import pytest


@pytest.fixture
def smoke():
    path = Path(__file__).parents[1] / "scripts" / "groq_smoke.py"
    spec = importlib.util.spec_from_file_location("groq_smoke", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("flag,key", [(False, False), (False, True), (True, False)])
def test_opt_in_and_key_both_required(smoke, monkeypatch, flag, key):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    if key:
        monkeypatch.setenv("GROQ_API_KEY", "offline")
    factory = Mock(side_effect=AssertionError("SDK must not be created"))
    monkeypatch.setattr(smoke.groq, "Groq", factory)
    assert smoke.main(["--allow-live"] if flag else []) == 1
    factory.assert_not_called()


def test_opted_in_script_mocked_only(smoke, monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "offline")
    completion = Mock(model="openai/gpt-oss-120b", choices=[
        Mock(finish_reason="stop", message=Mock(content='{"ok":true}')),
    ])
    client = Mock()
    client.chat.completions.create.return_value = completion
    factory = Mock()
    factory.return_value.__enter__ = Mock(return_value=client)
    factory.return_value.__exit__ = Mock(return_value=False)
    monkeypatch.setattr(smoke.groq, "Groq", factory)
    assert smoke.main(["--allow-live"]) == 0
    arguments = client.chat.completions.create.call_args.kwargs
    assert "store" not in arguments
    assert arguments["model"] == "openai/gpt-oss-120b"
    assert arguments["reasoning_effort"] == "medium"
    assert arguments["response_format"]["type"] == "json_schema"
    assert client.chat.completions.create.call_count == 1
