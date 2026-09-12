"""Execute the actual nested cleanup functions with the pinned SDK registration."""
import ast
import asyncio
import logging
from pathlib import Path
from types import MethodType, SimpleNamespace
from typing import Optional
import unittest
from unittest.mock import AsyncMock, Mock

from livekit.agents import JobContext

SERVER = Path(__file__).resolve().parents[1]


def cleanup_factory(namespace):
    tree = ast.parse((SERVER/'main.py').read_text(encoding='utf-8'))
    entry = next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef) and node.name == 'entrypoint')
    names = {'_cleanup_room_once', 'cleanup_room', 'on_job_shutdown'}
    functions = [node for node in entry.body if isinstance(node, ast.AsyncFunctionDef) and node.name in names]
    body = ast.parse('''
def make_cleanup():
    cleanup_lock = asyncio.Lock()
    shared_cleanup_task = None
    heartbeat_task = None
''').body[0]
    body.body += functions
    body.body += ast.parse('job.add_shutdown_callback(on_job_shutdown)\nreturn cleanup_room').body
    code = ast.fix_missing_locations(ast.Module(body=[body], type_ignores=[]))
    exec(compile(code, str(SERVER/'main.py'), 'exec'), namespace)
    return namespace['make_cleanup']()


class RoomShutdownTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.order = []
        self.tasks = {}
        self.translator_close = AsyncMock(side_effect=lambda: self.order.append('translator'))
        self.provider_close = AsyncMock(side_effect=lambda: self.order.append('provider'))
        self.close_session = AsyncMock(side_effect=lambda _: self.order.append('session'))
        self.room = SimpleNamespace(disconnect=AsyncMock(side_effect=lambda: self.order.append('disconnect')))
        self.job = SimpleNamespace(room=self.room, _shutdown_callbacks=[])
        # This is the real installed Agents1.2.1 method, not a guessed fake API.
        self.job.add_shutdown_callback = MethodType(JobContext.add_shutdown_callback, self.job)
        self.resource_manager = SimpleNamespace(log_stats=Mock(), shutdown=AsyncMock(),
            verify_cleanup_complete=AsyncMock(return_value={'complete': True}))
        self.namespace = dict(asyncio=asyncio, Optional=Optional,
            logger=logging.getLogger('room-shutdown-test'), job=self.job,
            resource_manager=self.resource_manager, participant_tasks=self.tasks, draining_tasks=set(),
            transcript_accumulators={}, translators={'en': SimpleNamespace(close=self.translator_close)},
            stt_providers={'ar-gemini': SimpleNamespace(aclose=self.provider_close)},
            tenant_context={'session_id': 'tenant-session'}, close_room_session=self.close_session,
            close_database_connections=AsyncMock())
        self.cleanup = cleanup_factory(self.namespace)

    async def draining_track(self):
        started, cancelled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        async def run():
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                await release.wait()
                self.order.append('track-drained')
        task = asyncio.create_task(run(), name='shutdown-test-track')
        self.tasks[('speaker', 'track')] = task
        await started.wait()
        async def finish():
            release.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.addAsyncCleanup(finish)
        return cancelled, release

    async def test_frontend_and_job_shutdown_await_one_shared_drain_and_session_close(self):
        cancelled, release = await self.draining_track()
        frontend = asyncio.create_task(self.cleanup('frontend_stop', session_id='frontend-session', disconnect=True))
        await cancelled.wait()
        self.assertEqual(len(self.job._shutdown_callbacks), 1)
        shutdown = asyncio.create_task(self.job._shutdown_callbacks[0]('worker stopping'))
        await asyncio.sleep(0)
        self.assertFalse(frontend.done())
        self.assertFalse(shutdown.done())
        self.translator_close.assert_not_awaited()
        self.provider_close.assert_not_awaited()
        self.close_session.assert_not_awaited()
        release.set()
        await asyncio.gather(frontend, shutdown)
        self.assertEqual(self.order, ['track-drained', 'translator', 'provider', 'session', 'disconnect'])
        self.close_session.assert_awaited_once_with('frontend-session')
        self.resource_manager.shutdown.assert_awaited_once()

    async def test_cancelled_frontend_waiter_cannot_abort_job_cleanup(self):
        cancelled, release = await self.draining_track()
        frontend = asyncio.create_task(self.cleanup('frontend_stop'))
        await cancelled.wait()
        frontend.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await frontend
        shutdown = asyncio.create_task(self.job._shutdown_callbacks[0]('room disconnected'))
        await asyncio.sleep(0)
        self.assertFalse(shutdown.done())
        self.provider_close.assert_not_awaited()
        release.set()
        await shutdown
        self.close_session.assert_awaited_once_with('tenant-session')
        self.assertEqual(self.order, ['track-drained', 'translator', 'provider', 'session'])

    async def test_shutdown_callback_alone_runs_cleanup_and_later_calls_wait_for_same_result(self):
        await self.job._shutdown_callbacks[0]('worker stopping')
        await self.cleanup('later duplicate')
        await self.job._shutdown_callbacks[0]('second shutdown notification')
        self.translator_close.assert_awaited_once()
        self.provider_close.assert_awaited_once()
        self.close_session.assert_awaited_once_with('tenant-session')
        self.namespace['close_database_connections'].assert_awaited_once()
        self.resource_manager.verify_cleanup_complete.assert_awaited_once()

    def test_shutdown_budget_in_both_workers_exceeds_track_drain_and_cleanup(self):
        for name in ('main.py', 'local_preview.py'):
            with self.subTest(worker=name):
                tree = ast.parse((SERVER/name).read_text(encoding='utf-8'))
                values = [keyword.value.value for node in ast.walk(tree)
                          if isinstance(node, ast.Call) for keyword in node.keywords
                          if keyword.arg == 'shutdown_process_timeout' and isinstance(keyword.value, ast.Constant)]
                self.assertEqual(values, [150])
                self.assertGreater(values[0], 115+10)
