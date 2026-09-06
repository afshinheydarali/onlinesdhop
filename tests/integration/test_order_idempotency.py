import asyncio
import unittest

from tests.integration.test_inventory import InventoryFixture


class OrderIdempotencyPGTests(unittest.IsolatedAsyncioTestCase, InventoryFixture):
    async def asyncSetUp(self):
        await self.fixture_setup()

    async def asyncTearDown(self):
        await self.fixture_teardown()

    async def test_same_key_replay_changed_conflict_and_client_total_rejected(self):
        barrier = asyncio.Barrier(2)

        async def run():
            await barrier.wait()
            return await self.create("same", (("A", 1),))

        results = await asyncio.wait_for(asyncio.gather(run(), run()), 10)
        self.assertEqual(sum(x.created for x in results), 1)
        from backend.services.orders import IdempotencyConflict

        with self.assertRaises(IdempotencyConflict):
            await self.create("same", (("B", 1),))
