import asyncio
import os
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault('DATABASE_URL', 'sqlite:///billing_test.db')

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import main


async def _run_test():
    request = SimpleNamespace(
        query_params=SimpleNamespace(get=lambda key, default=None: default),
        json=AsyncMock(return_value={
            'contractor_id': '42',
            'agent_id': 'agent_wrong',
        }),
    )

    with patch.object(main, 'RETELL_API_KEY', 'test_key'), \
         patch.object(main, 'RETELL_AGENT_ID', 'agent_real'), \
         patch('main.httpx.AsyncClient') as client_factory:
        client = AsyncMock()
        client.__aenter__.return_value = client

        response = SimpleNamespace(
            status_code=200,
            text='ok',
            raise_for_status=lambda: None,
            json=lambda: {
                'access_token': 'token_123',
                'call_id': 'call_456',
                'transport': 'webrtc',
                'ice_servers': [],
            },
        )
        client.post.return_value = response
        client_factory.return_value = client

        result = await main.start_retell_call(request)

    assert result['call_id'] == 'call_456'
    post_json = client.post.call_args.kwargs['json']
    assert post_json['contractor_id'] == '42', post_json
    assert post_json['agent_id'] == 'agent_real', post_json
    assert post_json['transcript'] is True, post_json


asyncio.run(_run_test())
print('retell request test pass')
