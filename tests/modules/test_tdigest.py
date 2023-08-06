from __future__ import annotations

import asyncio

import pytest

from coredis import Redis
from tests.conftest import targets


@pytest.mark.min_module_version("bf", "2.4.0")
@targets(
    "redis_stack",
    "redis_stack_resp2",
    "redis_stack_cached",
    "redis_stack_cluster",
)
class TestTdigest:
    async def test_create(self, client: Redis):
        await client.tdigest.create("digest")
        await client.tdigest.create("digest_lowcompress", 1)
        info = await asyncio.gather(
            client.tdigest.info("digest"),
            client.tdigest.info("digest_lowcompress"),
        )
        assert info[0]["Compression"] == 100
        assert info[1]["Compression"] == 1

    async def test_reset(self, client: Redis):
        await client.tdigest.create("digest")
        await client.tdigest.add("digest", [1, 2, 3, 4])
        info = await client.tdigest.info("digest")
        assert 4 == (info["Merged nodes"] + info["Unmerged nodes"])
        await client.tdigest.reset("digest")
        info = await client.tdigest.info("digest")
        assert 0 == (info["Merged nodes"] + info["Unmerged nodes"])

    async def test_add(self, client: Redis):
        await client.tdigest.create("digest")
        assert await client.tdigest.add("digest", [1, 2, 3])
        assert await client.tdigest.add("digest", [1, 2, 3, 4, 5, 6])

    async def test_ranks(self, client: Redis):
        await client.tdigest.create("digest")
        assert await client.tdigest.add("digest", [1, 2, 3])
        assert await client.tdigest.add("digest", [1, 2, 3, 4, 5, 6])
        assert (1.0, 1.0, 2.0) == await client.tdigest.byrank("digest", [0, 1, 2])
        assert (6.0, 5.0, 4.0) == await client.tdigest.byrevrank("digest", [0, 1, 2])
        assert (1, 3, 5) == await client.tdigest.rank("digest", [1, 2, 3])
        assert (2, 1, 0) == await client.tdigest.revrank("digest", [4, 5, 6])

    async def test_min_max_mean(self, client: Redis):
        await client.tdigest.create("digest")
        assert await client.tdigest.add("digest", [1, 2, 3])
        assert await client.tdigest.add("digest", [1, 2, 3, 4, 5, 6])
        assert 1.0 == await client.tdigest.min("digest")
        assert 6.0 == await client.tdigest.max("digest")
        assert 3.0 == await client.tdigest.trimmed_mean("digest", 0, 1)
        assert 1.8 == await client.tdigest.trimmed_mean("digest", 0, 0.5)
        assert 4.2 == await client.tdigest.trimmed_mean("digest", 0.5, 1)

    async def test_cdf(self, client: Redis):
        await client.tdigest.create("digest")
        assert await client.tdigest.add("digest", [1, 2, 3])
        assert await client.tdigest.add("digest", [1, 2, 3, 4, 5, 6])

        assert (
            pytest.approx(0.1111111),
            pytest.approx(0.5555555),
            pytest.approx(0.9444444),
        ) == await client.tdigest.cdf("digest", [1.0, 3.0, 6.0])

    async def test_quantiles(self, client: Redis):
        await client.tdigest.create("digest")
        assert await client.tdigest.add("digest", [1, 2, 3])
        assert await client.tdigest.add("digest", [1, 2, 3, 4, 5, 6])

        assert (1.0, 3.0, 6.0) == await client.tdigest.quantile("digest", [0, 0.5, 1])

    async def test_merge(self, client: Redis):
        await client.tdigest.create("digestA{a}", compression=60)
        await client.tdigest.create("digestB{a}", compression=50)
        assert await client.tdigest.add("digestA{a}", [1, 2, 3])
        assert await client.tdigest.add("digestB{a}", [1, 2, 3, 4, 5, 6])

        assert await client.tdigest.merge("digest{a}", ["digestA{a}", "digestB{a}"])
        assert 60 == (await client.tdigest.info("digest{a}"))["Compression"]
        assert (1.0, 3.0, 6.0) == await client.tdigest.quantile(
            "digest{a}", [0, 0.5, 1]
        )
        assert await client.tdigest.merge(
            "digest{a}", ["digestA{a}", "digestB{a}"], compression=1, override=True
        )
        assert 1 == (await client.tdigest.info("digest{a}"))["Compression"]

    @pytest.mark.parametrize("transaction", [True, False])
    async def test_pipeline(self, client: Redis, transaction: bool):
        p = await client.pipeline(transaction=transaction)
        await p.tdigest.create("digest1{a}")
        await p.tdigest.create("digest2{a}")
        await p.tdigest.add("digest1{a}", [1, 2, 3])
        await p.tdigest.add("digest2{a}", [4, 5, 6])
        await p.tdigest.merge("digest1{a}", ["digest2{a}"])
        await p.tdigest.quantile("digest1{a}", [0, 0.5, 1])
        assert (True, True, True, True, True, (1.0, 4.0, 6.0)) == await p.execute()
