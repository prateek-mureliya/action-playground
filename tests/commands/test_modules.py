from __future__ import annotations

import pytest

from coredis.tokens import PrefixToken
from tests.conftest import targets


@targets(
    "redis_stack",
    "redis_stack_raw",
    "redis_stack_resp2",
    "redis_stack_raw_resp2",
    "redis_stack_cluster",
)
async def test_modules_list(client, _s):
    module_info = await client.module_list()
    assert {_s("args"), _s("name"), _s("path"), _s("ver")} & module_info[0].keys()


@targets(
    "redis_basic",
    "redis_basic_raw",
    "redis_basic_resp2",
    "redis_basic_raw_resp2",
    "redis_cluster",
)
async def test_no_modules(client):
    module_info = await client.module_list()
    assert module_info == ()


@pytest.mark.parametrize(
    "redis",
    [
        pytest.param(pytest.lazy_fixture("fake_redis")),
        pytest.param(pytest.lazy_fixture("fake_redis_cluster")),
    ],
)
async def test_module_load(redis):
    redis.responses[b"MODULE LOAD"] = {
        ("/var/tmp/module.so",): b"OK",
        ("/var/tmp/module.so", "1"): b"OK",
    }
    assert await redis.module_load("/var/tmp/module.so")
    assert await redis.module_load("/var/tmp/module.so", "1")


@pytest.mark.parametrize(
    "redis",
    [
        pytest.param(pytest.lazy_fixture("fake_redis")),
        pytest.param(pytest.lazy_fixture("fake_redis_cluster")),
    ],
)
async def test_module_unload(redis):
    redis.responses[b"MODULE UNLOAD"] = {("module",): b"OK"}
    assert await redis.module_unload("module")


@pytest.mark.parametrize(
    "redis",
    [
        pytest.param(pytest.lazy_fixture("fake_redis")),
        pytest.param(pytest.lazy_fixture("fake_redis_cluster")),
    ],
)
async def test_module_loadex(redis):
    redis.responses[b"MODULE LOADEX"] = {
        ("/var/tmp/module.so",): b"OK",
        ("/var/tmp/module.so", PrefixToken.CONFIG, "fu", "bar"): b"OK",
        ("/var/tmp/module.so", PrefixToken.ARGS, "1"): b"OK",
        (
            "/var/tmp/module.so",
            PrefixToken.CONFIG,
            "fu",
            "bar",
            PrefixToken.CONFIG,
            "bar",
            "fu",
        ): b"OK",
    }

    assert await redis.module_loadex("/var/tmp/module.so")
    assert await redis.module_loadex("/var/tmp/module.so", configs={"fu": "bar"})
    assert await redis.module_loadex(
        "/var/tmp/module.so", configs={"fu": "bar", "bar": "fu"}
    )
    assert await redis.module_loadex("/var/tmp/module.so", args=["1"])
