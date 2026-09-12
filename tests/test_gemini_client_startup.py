import asyncio
from pathlib import Path
import sys
import threading
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from gemini_stt import recovery
from gemini_stt.hybrid import GeminiHybridSTT
from test_gemini_hybrid_stream import Client, pcm, push, until


class TrackedClient(Client):
    def __init__(self):
        super().__init__()
        self.async_closes = self.sync_closes = 0

    async def aclose(self):
        self.async_closes += 1
        await super().aclose()

    def close(self):
        self.sync_closes += 1


class ClientStartupTests(unittest.IsolatedAsyncioTestCase):
    async def make_stream(self, **options):
        client = TrackedClient()
        started, release, finished = threading.Event(), threading.Event(), threading.Event()
        calls = []
        loop = asyncio.get_running_loop()
        def factory(**kwargs):
            calls.append(threading.get_ident())
            started.set()
            if not release.wait(5):
                raise TimeoutError('The test did not release client startup')
            finished.set()
            return client
        defaults = dict(connect_timeout=2, finalize_timeout=1, recovery_timeout=5,
                        retry_delay=.01, max_retry_delay=.01, max_retries=3)
        defaults.update(options)
        provider = GeminiHybridSTT(model='test', vertexai=False, api_key='test',
            language='ar-gemini', _client_factory=factory,
            recovery=recovery.RecoveryOptions(**defaults))
        stream = provider.stream()
        async def cleanup():
            release.set()
            await stream.aclose()
            await provider.aclose()
        self.addAsyncCleanup(cleanup)
        return client, stream, started, release, finished, calls, loop

    async def test_constructor_does_not_block_audio_ingestion_or_loop(self):
        client, stream, started, release, finished, calls, _ = await self.make_stream()
        chunks = [pcm(1)]
        push(stream, chunks[0])
        await until(started.is_set)
        for index in range(20):
            chunks.append(pcm(index + 2))
            push(stream, chunks[-1])
            await asyncio.sleep(.002)
        self.assertFalse(finished.is_set())
        self.assertEqual(stream._received_samples, 21 * 1600)
        self.assertNotEqual(calls, [threading.get_ident()])
        release.set()
        stream.end_input()
        async for _ in stream:
            pass
        self.assertEqual(client.activities, [b''.join(chunks)])
        self.assertEqual((client.async_closes, client.sync_closes), (1, 1))

    async def test_cancelled_track_closes_late_constructor_result_once(self):
        client, stream, started, release, _, calls, _ = await self.make_stream()
        push(stream, pcm(1))
        await until(started.is_set)
        before = time.monotonic()
        await stream.aclose()
        self.assertLess(time.monotonic() - before, 1)
        self.assertEqual(client.sessions, [])
        release.set()
        await until(lambda: client.async_closes == client.sync_closes == 1)
        self.assertEqual(len(calls), 1)
        self.assertIsNone(stream._client)
        self.assertEqual(stream.stats['committed_windows'], 0)

    async def test_connection_timeout_reuses_pending_constructor(self):
        client, stream, started, release, _, calls, _ = await self.make_stream(connect_timeout=.3)
        push(stream, pcm(1))
        await until(started.is_set)
        await until(lambda: stream.stats['retries'] >= 1)
        self.assertEqual(len(calls), 1)
        release.set()
        stream.end_input()
        async for _ in stream:
            pass
        self.assertEqual(len(calls), 1)
        self.assertEqual(stream.stats['committed_windows'], 1)
        self.assertEqual((client.async_closes, client.sync_closes), (1, 1))

    async def test_completed_but_unadopted_client_is_cleaned_once(self):
        # Exercise the ownership handoff's other outcome: the worker completed
        # the Future, but cancellation arrived before _ensure_client adopted it.
        from concurrent.futures import Future
        client = TrackedClient()
        stream = object.__new__(recovery.ResilientGeminiStream)
        stream._client = None
        stream._client_startup = Future()
        stream._client_startup.set_result(client)
        stream._client_ready = asyncio.wrap_future(stream._client_startup)
        await stream._release_client()
        await stream._release_client()
        self.assertEqual((client.async_closes, client.sync_closes), (1, 1))

    async def test_transport_validation_failure_closes_constructed_client(self):
        client, stream, started, release, _, _, _ = await self.make_stream(max_retries=0)
        client._api_client._websocket_ssl_ctx = None
        push(stream, pcm(1))
        await until(started.is_set)
        release.set()
        with self.assertRaises(recovery.RecoveryError):
            async for _ in stream:
                pass
        self.assertEqual((client.async_closes, client.sync_closes), (1, 1))
        self.assertEqual(client.sessions, [])


if __name__ == '__main__':
    unittest.main()
