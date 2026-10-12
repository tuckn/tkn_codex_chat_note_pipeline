"""Stop command-wide generation once, retain checkpoints, and resume with a fresh budget."""
from unittest.mock import Mock

import pytest
from test_api_inference import SCHEMA, settings
from tkn_genai_bridge.providers import litellm as bridge_backend

from tkn_codex_chat_note.api_inference import ApiBudgetExceeded, ApiClient


def test_budget_stop_remains_sticky_even_if_a_cheaper_request_would_fit(monkeypatch):
    client = ApiClient(settings(max_cost_jpy=1))
    client.reserved_jpy = 0.99
    monkeypatch.setattr(client, "estimate", lambda *a: 1000)
    auth = Mock(side_effect=AssertionError("authentication"))
    monkeypatch.setattr(bridge_backend, "azure_headers", auth)
    with pytest.raises(ApiBudgetExceeded):
        client.invoke("first", SCHEMA, timeout=10)
    details = dict(client.budget_stop)
    client.reserved_jpy = 0  # Even sufficient funds do not implicitly resume this command.
    with pytest.raises(ApiBudgetExceeded) as exc:
        client.invoke("second", SCHEMA, timeout=10)
    assert exc.value.details == details and client.records == []
    auth.assert_not_called()
