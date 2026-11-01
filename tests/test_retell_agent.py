import asyncio
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import main


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload
        self.status_code = 200
        self.text = "{}"

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class FakeAsyncClient:
    def __init__(self, calls):
        self.calls = calls

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def post(self, url, headers=None, json=None, timeout=None):
        self.calls.append({
            "url": url,
            "headers": headers,
            "json": json,
            "timeout": timeout,
        })
        return FakeResponse({
            "access_token": "token_test",
            "call_id": "call_test",
            "transport": "webrtc",
            "ice_servers": [],
        })


async def main_test():
    calls = []
    original_api_key = main.RETELL_API_KEY
    original_agent_id = main.RETELL_AGENT_ID
    try:
        main.RETELL_API_KEY = "key_test"
        main.RETELL_AGENT_ID = "agent_from_env"

        async def fake_client_factory():
            return FakeAsyncClient(calls)

        original_client = main.httpx.AsyncClient
        main.httpx.AsyncClient = fake_client_factory
        try:
            result = await main.start_retell_call(
                SimpleNamespace(query_params={}),
                {"agent_id": "wrong_agent_id", "contractor_id": "2"},
            )
        finally:
            main.httpx.AsyncClient = original_client

        assert result["status"] == "success", result
        assert calls[0]["json"]["agent_id"] == "agent_from_env", calls
        assert calls[0]["json"]["retell_llm_dynamic_variables"]["contractor_id"] == "2", calls
    finally:
        main.RETELL_API_KEY = original_api_key
        main.RETELL_AGENT_ID = original_agent_id


asyncio.run(main_test())
print("retell agent test passed")
