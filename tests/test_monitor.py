from __future__ import annotations

import asyncio

import pytest

from tests.conftest import targets


@targets("redis_basic", "redis_basic_blocking", "redis_basic_resp2")
@pytest.mark.min_server_version("6.2.0")
class TestMonitor:
    async def test_explicit_fetch(self, client, cloner):
        monitored = await cloner(client)
        await monitored.ping()
        monitor = await client.monitor()
        response = await asyncio.gather(monitor.get_command(), monitored.get("test"))
        assert response[0].command == "GET"
        response = await asyncio.gather(monitor.get_command(), monitored.get("test2"))
        assert response[0].command == "GET"

    async def test_iterator(self, client):
        async def delayed():
            await asyncio.sleep(0.1)
            return await client.get("test")

        async def collect():
            results = []
            async for command in client.monitor():
                results.append(command)
                break
            return results

        results = await asyncio.gather(delayed(), collect())
        assert results[1][0].command in ["HELLO", "GET"]

    async def test_threaded_listener(self, client, mocker):
        monitor = await client.monitor()
        thread = monitor.run_in_thread(lambda cmd: None)
        await asyncio.sleep(0.01)
        send_command = mocker.spy(monitor.connection, "create_request")
        thread.stop()
        await asyncio.sleep(0.01)
        send_command.assert_called_with(b"RESET", decode=False)
