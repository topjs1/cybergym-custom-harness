from __future__ import annotations

from dataclasses import replace

import httpx
import pytest
from a2a.client import A2ACardResolver, ClientConfig, ClientFactory
from a2a.types import TaskArtifactUpdateEvent

from cybergym_harness.server import build_application
from tests.helpers import challenge_message


@pytest.mark.asyncio
async def test_agent_card_and_challenge_delivery_over_asgi(settings):
    short = replace(settings, max_attempts=1, feedback_timeout_seconds=1)
    url = "http://purple.test"
    app = build_application(short, url)
    transport = httpx.ASGITransport(app=app)
    artifact_seen = False
    async with httpx.AsyncClient(transport=transport, base_url=url, timeout=5) as http:
        card = await A2ACardResolver(httpx_client=http, base_url=url).get_agent_card()
        assert card.name == "CyberGym Custom Harness"
        client = ClientFactory(ClientConfig(httpx_client=http, streaming=True)).create(card)
        async for event in client.send_message(challenge_message("mock:a2a-delivery")):
            match event:
                case (task, TaskArtifactUpdateEvent()):
                    artifact_seen = bool(task.artifacts)
                case _:
                    pass
    assert artifact_seen
    assert short.results_csv.read_text().splitlines()[1].split(",")[1] == "mock:a2a-delivery"

