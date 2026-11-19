"""Speech minutes are what the classroom app bills; reports must be exact and survive outages."""
import asyncio
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

SERVER = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVER))
import speech_meter
from speech_meter import SpeechMeter, speech_meter_for_room


class Clock:
    def __init__(self, seconds: float):
        self.seconds = seconds

    def __call__(self) -> float:
        return self.seconds


class Recorder:
    """Stands in for the classroom app: answers with the queued statuses, then 200."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.bodies = []

    async def __call__(self, body):
        self.bodies.append(body)
        answer = self.answers.pop(0) if self.answers else 200
        if isinstance(answer, BaseException):
            raise answer
        return answer


MINUTE = 29_000_000  # a Unix minute number in 2025


class SpeechMeterTests(unittest.IsolatedAsyncioTestCase):
    def meter(self, post, clock=None, **kwargs):
        return SpeechMeter('room-1', post, clock=clock or Clock(MINUTE * 60 + 5), **kwargs)

    async def test_reports_each_minute_with_speech_once(self):
        clock, post = Clock(MINUTE * 60 + 1), Recorder()
        meter = self.meter(post, clock)
        meter.mark()
        clock.seconds += 30
        meter.mark()  # same minute
        clock.seconds += 60
        meter.mark()
        await meter.flush()
        self.assertEqual(post.bodies, [{'roomName': 'room-1', 'minutes': [MINUTE, MINUTE + 1]}])
        # More speech later in a minute that is already reported is not resent.
        meter.mark()
        await meter.flush()
        self.assertEqual(len(post.bodies), 1)
        self.assertEqual(meter.pending_minutes, [])

    async def test_silence_reports_nothing(self):
        post = Recorder()
        await self.meter(post).flush()
        self.assertEqual(post.bodies, [])

    async def test_unreachable_app_keeps_minutes_for_the_next_flush(self):
        for answer in (ConnectionError('refused'), 503, 429):
            with self.subTest(answer=answer):
                post = Recorder(answer)
                meter = self.meter(post)
                meter.mark()
                await meter.flush()
                self.assertEqual(meter.pending_minutes, [MINUTE])
                await meter.flush()
                self.assertEqual(meter.pending_minutes, [])
                self.assertEqual(post.bodies[0], post.bodies[1])

    async def test_rejected_report_is_final(self):
        # 307: CLASSROOM_APP_URL points at a redirecting host.
        for status in (307, 400, 401):
            with self.subTest(status=status):
                post = Recorder(status)
                meter = self.meter(post)
                meter.mark()
                await meter.flush()
                self.assertEqual(meter.pending_minutes, [])
                self.assertEqual(len(post.bodies), 1)

    async def test_long_backlog_is_sent_in_batches(self):
        clock, post = Clock(MINUTE * 60), Recorder()
        meter = self.meter(post, clock)
        for _ in range(300):
            meter.mark()
            clock.seconds += 60
        await meter.flush()
        self.assertEqual([len(body['minutes']) for body in post.bodies], [240, 60])
        sent = [minute for body in post.bodies for minute in body['minutes']]
        self.assertEqual(sent, list(range(MINUTE, MINUTE + 300)))

    async def test_backlog_is_bounded_by_dropping_the_oldest_minute(self):
        clock = Clock(MINUTE * 60)
        meter = self.meter(Recorder(), clock)
        with patch.object(speech_meter, 'MAX_PENDING_MINUTES', 3):
            for _ in range(5):
                meter.mark()
                clock.seconds += 60
        self.assertEqual(meter.pending_minutes, [MINUTE + 2, MINUTE + 3, MINUTE + 4])

    async def test_loop_flushes_periodically(self):
        post = Recorder()
        meter = self.meter(post, flush_interval=0.01)
        meter.start()
        meter.mark()
        for _ in range(100):
            if post.bodies:
                break
            await asyncio.sleep(0.01)
        await meter.aclose()
        self.assertEqual(post.bodies, [{'roomName': 'room-1', 'minutes': [MINUTE]}])

    async def test_close_sends_the_last_minutes_and_stops_counting(self):
        post = Recorder()
        meter = self.meter(post, flush_interval=3600)
        meter.start()
        meter.mark()
        await meter.aclose()
        self.assertEqual(post.bodies, [{'roomName': 'room-1', 'minutes': [MINUTE]}])
        meter.mark()
        await meter.flush()
        await meter.aclose()
        self.assertEqual(len(post.bodies), 1)

    async def test_close_does_not_wait_past_its_budget(self):
        release = asyncio.Event()

        async def hanging(body):
            await release.wait()
            return 200

        meter = self.meter(hanging)
        meter.mark()
        with patch.object(speech_meter, 'CLOSE_TIMEOUT_SECONDS', 0.05):
            await asyncio.wait_for(meter.aclose(), timeout=2)
        self.assertEqual(meter.pending_minutes, [MINUTE])

    def test_off_without_livekit_keys_or_in_local_preview(self):
        keys = {'LIVEKIT_API_KEY': 'APIkey', 'LIVEKIT_API_SECRET': 'secret'}
        for env in ({}, {'LIVEKIT_API_KEY': 'APIkey'}, {'LIVEKIT_API_SECRET': 'secret'},
                    {**keys, 'BAYAAN_LOCAL_PREVIEW': '1'}):
            with self.subTest(env=env):
                meter = speech_meter_for_room('room-1', env)
                self.assertFalse(meter.enabled)
                meter.mark()
                self.assertEqual(meter.pending_minutes, [])
        self.assertFalse(speech_meter_for_room(None, keys).enabled)

    def test_on_with_the_workers_livekit_keys_and_no_extra_settings(self):
        meter = speech_meter_for_room('room-1', {'LIVEKIT_API_KEY': 'APIkey', 'LIVEKIT_API_SECRET': 'secret'})
        self.assertTrue(meter.enabled)
        self.assertEqual(speech_meter.DEFAULT_APP_URL, 'https://www.bayaan.app')

    def test_report_token_only_covers_reporting_for_one_room(self):
        import jwt

        secret = 's' * 40
        token = speech_meter.report_token('room-1', 'APIkey', secret)
        claims = jwt.decode(token, secret, algorithms=['HS256'])
        self.assertEqual(claims['iss'], 'APIkey')
        self.assertEqual(claims['sub'], 'bayaan-speech-meter')
        self.assertEqual(claims['video']['room'], 'room-1')
        self.assertFalse(claims['video'].get('roomJoin', False))
        self.assertLessEqual(claims['exp'] - claims['nbf'], 5 * 60)
        with self.assertRaises(jwt.InvalidSignatureError):
            jwt.decode(token, 'o' * 40, algorithms=['HS256'])


class WorkerWiringTests(unittest.TestCase):
    def test_worker_counts_published_speech_and_reports_on_cleanup(self):
        source = (SERVER / 'main.py').read_text(encoding='utf-8')
        forward = source[source.index('async def _forward_transcription'):source.index('async def transcribe_track')]
        publish = forward.index('await job.room.local_participant.publish_transcription(final_transcription)')
        self.assertLess(publish, forward.index('speech_meter.mark()'))
        gemini = source[source.index('async def publish_gemini'):source.index('async def heartbeat')]
        self.assertIn('speech_meter.mark()', gemini)
        self.assertIn('speech_meter.start()', source)
        cleanup = source[source.index('async def _cleanup_room_once'):source.index('async def cleanup_room(')]
        self.assertLess(cleanup.index('await asyncio.wait(stopping_tasks'), cleanup.index('await speech_meter.aclose()'))
        self.assertLess(cleanup.index('await speech_meter.aclose()'), cleanup.index('await translator.close()'))


if __name__ == '__main__':
    unittest.main()
