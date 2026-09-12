import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
import sys
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from livekit import rtc
from livekit.agents import stt
from livekit.rtc._utils import RingQueue
from gemini_stt.compat import SpeechData
from gemini_track import run_gemini_track

END = object()


def final(text, *, is_final=True, language='ar-EG'):
    return stt.SpeechEvent(type=(stt.SpeechEventType.FINAL_TRANSCRIPT if is_final
                               else stt.SpeechEventType.INTERIM_TRANSCRIPT),
                           alternatives=[SpeechData(text=text, language=language)])


class Audio:
    def __init__(self):
        self.incoming = asyncio.Queue()
        self.closed = False
        self.close_error = None
        self.options = None

    def factory(self, track, **options):
        self.options = options
        return self

    def push(self, value=1):
        frame = rtc.AudioFrame(value.to_bytes(2, 'little', signed=True)*320, 16000, 1, 320)
        self.incoming.put_nowait(SimpleNamespace(frame=frame))
        return frame.data.tobytes()

    def finish(self):
        self.incoming.put_nowait(END)

    def __aiter__(self):
        return self

    async def __anext__(self):
        item = await self.incoming.get()
        if item is END:
            raise StopAsyncIteration
        if isinstance(item, BaseException):
            raise item
        return item

    async def aclose(self):
        self.closed = True
        if self.close_error:
            raise self.close_error


class GoogleStream:
    def __init__(self):
        self.incoming = asyncio.Queue()
        self.frames = []
        self.ended = asyncio.Event()
        self.close_started = asyncio.Event()
        self.closed = False
        self.end_calls = 0
        self.end_error = self.close_error = None
        self.end_finals = []
        self.finish_on_end = True
        self.close_gate = self.final_gate = None
        self.delivery = None
        self.yield_receipts = False

    def push_frame(self, frame):
        self.frames.append(frame.data.tobytes())

    def end_input(self):
        self.end_calls += 1
        self.ended.set()
        if self.end_error:
            raise self.end_error
        if self.end_calls > 1:
            return
        async def deliver():
            if self.final_gate:
                await self.final_gate.wait()
            for event in self.end_finals:
                self.incoming.put_nowait(event)
            if self.finish_on_end:
                self.incoming.put_nowait(END)
        self.delivery = asyncio.create_task(deliver())

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self.yield_receipts:
            await asyncio.sleep(0)
        item = await self.incoming.get()
        if item is END:
            raise StopAsyncIteration
        if isinstance(item, BaseException):
            raise item
        return item

    async def aclose(self):
        self.close_started.set()
        if self.close_gate:
            await self.close_gate.wait()
        self.closed = True
        if self.delivery:
            self.delivery.cancel()
            await asyncio.gather(self.delivery, return_exceptions=True)
        if self.close_error:
            raise self.close_error


async def until(predicate):
    for _ in range(1000):
        if predicate():
            return
        await asyncio.sleep(.001)
    raise AssertionError('Timed out waiting for the track test state')


class GeminiTrackTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.audio, self.stream = Audio(), GoogleStream()
        self.provider = SimpleNamespace(language='ar-gemini', stream=lambda: self.stream)
        self.publish, self.translate = AsyncMock(), AsyncMock()
        self.participant = SimpleNamespace(identity='teacher-test', room_name='local-test')
        self.track = SimpleNamespace(sid='audio-test')

    def launch(self, **options):
        task = asyncio.create_task(run_gemini_track(participant=self.participant, track=self.track,
            provider=self.provider, publish=self.publish, translate=self.translate,
            audio_factory=self.audio.factory, stt_drain_seconds=options.pop('stt_drain_seconds', 1),
            translation_drain_seconds=options.pop('translation_drain_seconds', 1), **options))
        async def cleanup():
            if self.stream.final_gate:
                self.stream.final_gate.set()
            if self.stream.close_gate:
                self.stream.close_gate.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.addAsyncCleanup(cleanup)
        return task

    def assert_closed(self):
        self.assertTrue(self.stream.closed)
        self.assertTrue(self.audio.closed)
        self.assertEqual(self.stream.end_calls, 1)
        dangling = [task.get_name() for task in asyncio.all_tasks()
                    if not task.done() and task.get_name().startswith('gemini-')]
        self.assertEqual(dangling, [])

    async def test_natural_audio_end_drains_google_finals_and_ordered_translations(self):
        expected = self.audio.push(1) + self.audio.push(2)
        self.stream.end_finals = [final('الحمد لله.'), final('رب العالمين. هذا كلام جديد.')]
        self.audio.finish()
        active, peak, order = 0, 0, []
        async def translate(text, *_):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(.001)
            order.append(text)
            active -= 1
        self.translate.side_effect = translate
        await self.launch()
        self.assertEqual(b''.join(self.stream.frames), expected)
        self.assertEqual(self.audio.options, dict(sample_rate=16000, num_channels=1,
                                                capacity=3000, frame_size_ms=10))
        self.assertEqual(order, ['الحمد لله. رب العالمين.', 'هذا كلام جديد.'])
        self.assertEqual(peak, 1)
        self.assertTrue(all(call.args[2] == 'ar-gemini' for call in self.publish.await_args_list))
        self.assert_closed()

    async def test_stop_and_repeated_cancellation_wait_for_late_final_and_gpt(self):
        gate = asyncio.Event()
        self.stream.final_gate = asyncio.Event()
        self.stream.end_finals = [final('هذا هو النص المؤكد.')]
        async def translating(*_):
            await gate.wait()
        self.translate.side_effect = translating
        self.audio.push()
        task = self.launch()
        await until(lambda: len(self.stream.frames) == 1)
        task.cancel()
        await self.stream.ended.wait()
        task.cancel()
        self.stream.final_gate.set()
        await until(lambda: self.translate.await_count == 1)
        self.assertFalse(task.done())
        self.assertFalse(self.stream.closed)
        task.cancel()
        gate.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.translate.assert_awaited_once()
        self.assert_closed()

    async def test_capture_queue_preserves_twenty_five_second_frame_burst(self):
        # Use the installed SDK's real bounded ring: it drops oldest frames on
        # overflow. Deliver the burst before the consumer can run, as FFI does
        # when callbacks accumulated during another SDK's first-use stall.
        original_factory = self.audio.factory
        def ring_factory(track, **options):
            result = original_factory(track, **options)
            result.incoming = RingQueue(options['capacity'])
            return result
        self.audio.factory = ring_factory
        task = self.launch()
        await until(lambda: self.audio.options is not None)
        expected = []
        for value in range(1, 2501):
            data = value.to_bytes(2, 'little', signed=True) * 160
            expected.append(data)
            frame = rtc.AudioFrame(data, 16000, 1, 160)
            self.audio.incoming.put(SimpleNamespace(frame=frame))
        self.audio.incoming.put(END)
        await task
        self.assertEqual(b''.join(self.stream.frames), b''.join(expected))
        self.assertEqual(sum(len(frame) for frame in self.stream.frames) // 2, 400000)
        self.assert_closed()

    async def test_repeated_cancel_during_resource_close_cannot_skip_audio_close(self):
        self.stream.close_gate = asyncio.Event()
        self.audio.finish()
        task = self.launch()
        await self.stream.close_started.wait()
        task.cancel()
        task.cancel()
        await asyncio.sleep(0)
        self.assertFalse(task.done())
        self.stream.close_gate.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assert_closed()

    async def test_stream_creation_failure_releases_already_created_audio(self):
        failure = RuntimeError('stream creation rejected')
        def broken():
            raise failure
        self.provider.stream = broken
        with self.assertRaises(RuntimeError) as raised:
            await self.launch()
        self.assertIs(raised.exception, failure)
        self.assertTrue(self.audio.closed)

    async def test_end_input_failure_still_cancels_children_and_closes_resources(self):
        failure = self.stream.end_error = RuntimeError('end input rejected')
        self.audio.finish()
        with self.assertRaises(RuntimeError) as raised:
            await self.launch()
        self.assertIs(raised.exception, failure)
        self.assert_closed()

    async def test_stream_close_failure_does_not_skip_audio_close(self):
        failure = self.stream.close_error = RuntimeError('close failed')
        self.audio.finish()
        with self.assertRaises(RuntimeError) as raised:
            await self.launch()
        self.assertIs(raised.exception, failure)
        self.assert_closed()

    async def test_audio_failure_remains_primary_when_end_and_close_also_fail(self):
        failure = RuntimeError('audio source failed')
        self.audio.incoming.put_nowait(failure)
        self.stream.end_error = RuntimeError('end failed second')
        self.stream.close_error = RuntimeError('close failed third')
        self.audio.close_error = RuntimeError('audio close failed fourth')
        with self.assertRaises(RuntimeError) as raised:
            await self.launch()
        self.assertIs(raised.exception, failure)
        self.assert_closed()

    async def test_cancelled_end_input_is_reported_and_cannot_leak_children(self):
        self.stream.end_error = asyncio.CancelledError()
        self.audio.finish()
        with self.assertRaisesRegex(RuntimeError, 'cancelled unexpectedly'):
            await self.launch()
        self.assert_closed()

    async def test_provider_failure_flushes_confirmed_tail_and_keeps_original_error(self):
        failure = RuntimeError('provider continuity lost')
        self.stream.incoming.put_nowait(final('نص غير مكتمل'))
        self.stream.incoming.put_nowait(failure)
        with self.assertRaises(RuntimeError) as raised:
            await self.launch()
        self.assertIs(raised.exception, failure)
        self.assertEqual(self.translate.await_args.args[0], 'نص غير مكتمل')
        self.assert_closed()

    async def test_source_publication_failure_is_not_treated_as_successful_delivery(self):
        failure = RuntimeError('caption delivery failed')
        self.publish.side_effect = failure
        self.stream.incoming.put_nowait(final('هذا نص غير منشور.'))
        with self.assertRaises(RuntimeError) as raised:
            await self.launch()
        self.assertIs(raised.exception, failure)
        self.translate.assert_not_awaited()
        self.assert_closed()

    async def test_translation_failure_stops_audio_and_surfaces_original_failure(self):
        failure = RuntimeError('GPT unavailable')
        self.translate.side_effect = failure
        self.stream.incoming.put_nowait(final('هذا نص مؤكد.'))
        with self.assertRaises(RuntimeError) as raised:
            await self.launch()
        self.assertIs(raised.exception, failure)
        self.translate.assert_awaited_once()
        self.assert_closed()

    async def test_queue_overflow_with_dead_consumer_fails_promptly_and_preserves_primary_error(self):
        gate = asyncio.Event()
        async def translate(*_):
            await gate.wait()
            raise RuntimeError('GPT failed while backlog full')
        self.translate.side_effect = translate
        for _ in range(140):
            self.stream.incoming.put_nowait(final('هذا نص مؤكد.'))
        task = self.launch(translation_drain_seconds=1)
        await self.stream.ended.wait()
        started = time.monotonic()
        gate.set()
        with self.assertRaisesRegex(RuntimeError, 'backlog reached 128'):
            await task
        self.assertLess(time.monotonic()-started, .5)
        self.assert_closed()

    async def test_stt_drain_timeout_flushes_confirmed_tail_then_closes(self):
        self.stream.finish_on_end = False
        self.stream.incoming.put_nowait(final('نص مؤكد'))
        self.audio.finish()
        with self.assertRaises(TimeoutError):
            await self.launch(stt_drain_seconds=.02)
        self.assertEqual(self.translate.await_args.args[0], 'نص مؤكد')
        self.assert_closed()

    async def test_translation_drain_has_one_budget_even_when_sentinel_waits_for_space(self):
        self.stream.yield_receipts = True
        self.stream.end_finals = [final('هذا نص مؤكد.') for _ in range(129)]
        calls = 0
        async def translate(*_):
            nonlocal calls
            calls += 1
            await asyncio.sleep(.12 if calls == 1 else 1)
        self.translate.side_effect = translate
        self.audio.finish()
        drain_started = []
        original_timeout = asyncio.timeout
        @asynccontextmanager
        async def measured_timeout(seconds):
            drain_started.append(time.monotonic())
            async with original_timeout(seconds):
                yield
        with patch('gemini_track.asyncio.timeout', measured_timeout):
            with self.assertRaises(TimeoutError):
                await self.launch(translation_drain_seconds=.2)
        self.assertEqual(len(drain_started), 1)
        self.assertLess(time.monotonic()-drain_started[0], .29)
        self.assertGreaterEqual(calls, 2)
        self.assert_closed()

    async def test_cancelled_translation_callback_is_a_reported_failure(self):
        async def translate(*_):
            raise asyncio.CancelledError
        self.translate.side_effect = translate
        self.stream.incoming.put_nowait(final('هذا نص مؤكد.'))
        with self.assertRaisesRegex(RuntimeError, 'cancelled'):
            await self.launch()
        self.assert_closed()

    async def test_receive_end_while_audio_active_is_a_reported_failure(self):
        self.stream.incoming.put_nowait(END)
        with self.assertRaisesRegex(RuntimeError, 'microphone audio was still active'):
            await self.launch()
        self.assert_closed()

    async def test_heartbeat_and_frame_ingestion_do_not_change_audio(self):
        heartbeat = AsyncMock()
        frames = [self.audio.push(index) for index in range(100)]
        self.audio.finish()
        await self.launch(heartbeat=heartbeat)
        heartbeat.assert_awaited_once()
        self.assertEqual(self.stream.frames, frames)
        self.assert_closed()
