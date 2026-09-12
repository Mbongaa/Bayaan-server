"""Deterministic session-lifetime checks using the real adapter state machine."""
import unittest

from google.genai import types
from livekit.agents import stt

import test_gemini_hybrid_stream as fixtures
from test_gemini_hybrid_stream import Client, event, pcm, push, until


class SessionContinuationTests(unittest.IsolatedAsyncioTestCase):
    make_stream = fixtures.GeminiHybridStreamTests.make_stream

    async def seal_punctuation(self, client, stream, source, committed):
        push(stream, source)
        await until(lambda: client.sessions and bytes(client.sessions[-1].audio) == source)
        client.sessions[-1].incoming.put_nowait(event(
            interim_input_transcription=types.Transcription(text='A complete sentence.')))
        await until(lambda: stream.stats['committed_windows'] == committed)

    async def test_two_default_wall_clock_rotations_preserve_every_committed_window(self):
        now = [0.0]
        client = Client()
        provider, stream = await self.make_stream(client, clock=lambda: now[0])
        self.assertEqual(provider.recovery.rotation_seconds, 480.0)
        sources = [pcm(value, 320) for value in range(1, 6)]
        for count, (timestamp, source) in enumerate(zip(
                [0.0, 479.99, 480.0, 959.99, 960.0], sources), 1):
            now[0] = timestamp
            await self.seal_punctuation(client, stream, source, count)
        stream.end_input()
        finals = [item async for item in stream if item.type == stt.SpeechEventType.FINAL_TRANSCRIPT]
        self.assertEqual(stream.stats['rotations'], 2)
        self.assertEqual(stream.stats['connections'], 3)
        self.assertEqual(stream.stats['retries'], 0)
        self.assertEqual(client.activities, sources)
        self.assertTrue(all(session.closed for session in client.sessions))
        self.assertEqual(len(set(item.request_id for item in finals)), 5)
        self.assertEqual([(item.alternatives[0].metadata['audio_window_start_sample'],
                           item.alternatives[0].metadata['audio_window_end_sample']) for item in finals],
                         [(index * 320, (index + 1) * 320) for index in range(5)])

    async def test_two_default_audio_duration_rotations_while_wall_clock_stays_still(self):
        client = Client()
        provider, stream = await self.make_stream(client, clock=lambda: 0.0)
        self.assertEqual(provider.recovery.rotation_seconds, 480.0)
        sources = []
        # Advance actual retained sample counts, not private connection counters.
        # 61 x16s crosses 480s and960s while a deterministic clock stays fixed.
        for index in range(61):
            source = pcm(index + 1, 16 * 16000)
            sources.append(source)
            push(stream, source)
            await until(lambda: stream.stats['committed_windows'] == index + 1)
        stream.end_input()
        finals = [item async for item in stream if item.type == stt.SpeechEventType.FINAL_TRANSCRIPT]
        self.assertEqual(stream.stats['rotations'], 2)
        self.assertEqual(stream.stats['connections'], 3)
        self.assertEqual(stream.stats['committed_windows'], 61)
        self.assertEqual(stream.stats['replayed_samples'], 0)
        self.assertEqual(client.activities, sources)
        self.assertEqual(len(set(item.request_id for item in finals)), 61)
        self.assertEqual([(item.alternatives[0].metadata['audio_window_start_sample'],
                           item.alternatives[0].metadata['audio_window_end_sample']) for item in finals],
                         [(index * 256000, (index + 1) * 256000) for index in range(61)])

    async def test_google_go_away_replays_only_uncommitted_audio_then_continues(self):
        client = Client()
        _, stream = await self.make_stream(client)
        first, second, following = pcm(1, 320), pcm(2, 640), pcm(3, 960)
        await self.seal_punctuation(client, stream, first, 1)
        push(stream, second)
        await until(lambda: bytes(client.sessions[0].audio) == second)
        client.sessions[0].incoming.put_nowait(types.LiveServerMessage(
            go_away=types.LiveServerGoAway(time_left='10s')))
        await until(lambda: len(client.sessions) == 2 and bytes(client.sessions[1].audio) == second)
        client.sessions[1].incoming.put_nowait(event(
            interim_input_transcription=types.Transcription(text='Second complete sentence.')))
        await until(lambda: stream.stats['committed_windows'] == 2)
        await self.seal_punctuation(client, stream, following, 3)
        stream.end_input()
        finals = [item async for item in stream if item.type == stt.SpeechEventType.FINAL_TRANSCRIPT]
        self.assertEqual(client.activities, [first, second, following])
        self.assertEqual(stream.stats['connections'], 2)
        self.assertEqual(stream.stats['retries'], 1)
        self.assertEqual(stream.stats['replayed_samples'], 640)
        self.assertEqual(stream.stats['committed_windows'], 3)
        self.assertEqual([item.alternatives[0].metadata['recovery_attempt'] for item in finals], [0, 1, 0])
        self.assertEqual([(item.alternatives[0].metadata['audio_window_start_sample'],
                           item.alternatives[0].metadata['audio_window_end_sample']) for item in finals],
                         [(0,320),(320,960),(960,1920)])


if __name__ == '__main__':
    unittest.main()
