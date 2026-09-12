import asyncio
import hashlib
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from google.genai import types
from livekit import rtc
from livekit.agents import stt
from gemini_stt import recovery as current
from gemini_transcription import GeminiHybridSTT
from gemini_stt.hybrid import _PunctuationBoundary


def event(**content):
    return types.LiveServerMessage(server_content=types.LiveServerContent(**content))


def pcm(value=1, samples=1600):
    return value.to_bytes(2, 'little', signed=True) * samples


def push(stream, data):
    stream.push_frame(rtc.AudioFrame(data, 16000, 1, len(data)//2))


async def until(predicate):
    for _ in range(1000):
        if predicate():
            return
        await asyncio.sleep(.001)
    raise AssertionError('Timed out waiting for test event')


class Client:
    def __init__(self, *, fail_first_end=False):
        self.sessions, self.activities, self.calls = [], [], []
        self.closed = False
        self.fail_first_end = fail_first_end
        self.no_final = False
        self.final_text = 'كرر هذا.'
        self.hold_final = asyncio.Event()
        self.hold_final.set()
        self._api_client = SimpleNamespace(_websocket_ssl_ctx={})
        self.aio = SimpleNamespace(live=SimpleNamespace(connect=self.connect), aclose=self.aclose)

    def factory(self, **kwargs):
        return self

    def connect(self, **options):
        self.config = options['config']
        client = self

        class Session:
            def __init__(self):
                self.incoming = asyncio.Queue()
                self.audio = bytearray()
                self.closed = False
                self.number = len(client.sessions)

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_):
                self.closed = True

            async def send_realtime_input(self, **data):
                client.calls.append((self.number, data))
                if 'activity_start' in data:
                    self.audio = bytearray()
                if 'audio' in data:
                    self.audio.extend(data['audio'].data)
                if 'activity_end' in data:
                    client.activities.append(bytes(self.audio))
                    if client.fail_first_end and len(client.activities) == 1:
                        raise ConnectionError('simulated private provider body')
                    await client.hold_final.wait()
                    await self.incoming.put(event(generation_complete=True, turn_complete=True))
                    if not client.no_final:
                        await self.incoming.put(event(input_transcription=types.Transcription(text=client.final_text)))

            async def receive(self):
                while True:
                    response = await self.incoming.get()
                    if isinstance(response, Exception):
                        raise response
                    yield response
                    if response.server_content.turn_complete:
                        return

        session = Session()
        self.sessions.append(session)
        return session

    async def aclose(self):
        self.closed = True


class GeminiHybridStreamTests(unittest.IsolatedAsyncioTestCase):
    async def make_stream(self, client, *, language='ar-EG', clock=None, **recovery_options):
        for key, value in dict(finalize_timeout=.5, connect_timeout=.5, retry_delay=.001,
                               max_retry_delay=.001, replay_speed=10000).items():
            recovery_options.setdefault(key, value)
        provider = GeminiHybridSTT(model='test', vertexai=False, api_key='fake', language=language,
            recovery=current.RecoveryOptions(**recovery_options), _client_factory=client.factory,
            **({'_clock': clock} if clock is not None else {}))
        stream = provider.stream()
        self.addAsyncCleanup(provider.aclose)
        self.addAsyncCleanup(stream.aclose)
        return provider, stream

    async def make_hybrid(self, client, **options):
        return await self.make_stream(client, **options)

    async def test_hybrid_has_no_vad_pause_energy_or_soft8_cut(self):
        client = Client()
        provider,stream = await self.make_hybrid(client)
        speech,quiet,zeros = pcm(1000,8*16000),pcm(1,7*16000),pcm(0,1600)
        push(stream,speech)
        push(stream,quiet)
        push(stream,zeros)
        await until(lambda: client.sessions and bytes(client.sessions[0].audio)==speech+quiet+zeros)
        self.assertEqual(client.activities,[])
        self.assertIsNone(provider._vad)
        self.assertFalse(provider.recovery.vad_enabled)
        self.assertIsInstance(provider.recovery,current.RecoveryOptions)
        self.assertFalse(hasattr(provider.recovery,'max_window_seconds'))
        self.assertEqual(provider.max_segment_seconds,16.0)
        self.assertEqual(provider.boundary_revision,'punctuation_max16_no_pauses_v2')
        stream.end_input()
        finals = [e async for e in stream if e.type==stt.SpeechEventType.FINAL_TRANSCRIPT]
        self.assertEqual(client.activities,[speech+quiet+zeros])
        self.assertEqual(finals[0].alternatives[0].metadata['boundary_reason'],'eof')
        self.assertEqual(stream.stats.get('vad_discarded_samples', 0),0)


    async def test_hybrid_short_punctuation_wakes_sender_and_preserves_next_audio(self):
        client = Client()
        client.hold_final.clear()
        _,stream = await self.make_hybrid(client)
        first,second = pcm(1000,333),pcm(1,2057)
        push(stream,first)
        await until(lambda: client.sessions and bytes(client.sessions[0].audio)==first)
        for _ in range(3):
            client.sessions[0].incoming.put_nowait(event(interim_input_transcription=types.Transcription(text='Yes.')))
        await until(lambda: len(client.activities)==1)
        push(stream,second)
        client.hold_final.set()
        await until(lambda: stream.stats['committed_windows']==1 and bytes(client.sessions[0].audio)==second)
        stream.end_input()
        finals = [e async for e in stream if e.type==stt.SpeechEventType.FINAL_TRANSCRIPT]
        self.assertEqual(client.activities,[first,second])
        self.assertEqual([e.alternatives[0].metadata['boundary_reason'] for e in finals],['google_interim_punctuation','eof'])
        self.assertEqual(stream.stats['forced_boundaries'],0)


    async def test_hybrid_exact16_limit_for_quiet_speech_and_crossing_frames(self):
        for value in (1,1000):
            with self.subTest(value=value):
                client = Client()
                provider,stream = await self.make_hybrid(client)
                warnings=[]
                provider.on('warning',warnings.append)
                first,last = pcm(value,256000-333),pcm(value+1,2057)
                push(stream,first)
                push(stream,last)
                stream.end_input()
                finals = [e async for e in stream if e.type==stt.SpeechEventType.FINAL_TRANSCRIPT]
                joined=first+last
                self.assertEqual(client.activities,[joined[:512000],joined[512000:]])
                self.assertEqual([e.alternatives[0].metadata['audio_window_end_sample'] for e in finals],[256000,257724])
                self.assertEqual([e.alternatives[0].metadata['audio_window_start_sample'] for e in finals],[0,256000])
                self.assertEqual(stream.stats['forced_boundaries'],1)
                self.assertEqual(len(warnings),1)
                self.assertEqual(warnings[0]['audio_seconds'],16)
                self.assertEqual(stream.buffered_audio_seconds,0)


    async def test_hybrid_large_frame_splits_repeatedly_without_overshoot_or_empty_eof(self):
        client=Client()
        _,stream=await self.make_hybrid(client)
        data=pcm(7,48*16000)
        push(stream,data)
        stream.end_input()
        finals=[e async for e in stream if e.type==stt.SpeechEventType.FINAL_TRANSCRIPT]
        self.assertEqual([len(activity)//2 for activity in client.activities],[256000]*3)
        self.assertEqual(b''.join(client.activities),data)
        self.assertEqual(len(finals),3)
        self.assertEqual(stream.stats['forced_boundaries'],3)
        self.assertIsNone(stream._carry)


    async def test_hybrid_sealed_retry_retains_exact_window_carry_and_queued_tail(self):
        client=Client(fail_first_end=True)
        _,stream=await self.make_hybrid(client)
        crossing,queued=pcm(1000,256333),pcm(1,901)
        push(stream,crossing)
        push(stream,queued)
        stream.end_input()
        finals=[e async for e in stream if e.type==stt.SpeechEventType.FINAL_TRANSCRIPT]
        self.assertEqual(client.activities,[crossing[:512000],crossing[:512000],crossing[512000:]+queued])
        self.assertEqual(len(finals),2)
        self.assertEqual(finals[0].alternatives[0].metadata['recovery_attempt'],1)
        self.assertEqual(finals[1].alternatives[0].metadata['audio_window_start_sample'],256000)
        self.assertEqual(stream.stats['forced_boundaries'],1)
        self.assertEqual(stream.stats['replayed_samples'],256000)


    async def test_hybrid_hardcap_first_ignores_old_marker_after_carry(self):
        client=Client()
        client.hold_final.clear()
        _,stream=await self.make_hybrid(client)
        first,crossing=pcm(1000,1000),pcm(2000,256000)
        push(stream,first)
        await until(lambda: stream._attempt is not None and client.sessions[0].audio)
        old=stream._attempt.window.id
        push(stream,crossing)
        stream._input.put_nowait(_PunctuationBoundary(old,0,257000))
        await until(lambda: len(client.activities)==1)
        client.hold_final.set()
        await until(lambda: stream.stats['committed_windows']==1 and len(client.sessions[0].audio)==2000)
        self.assertEqual(len(client.activities),1)
        stream.end_input()
        finals=[e async for e in stream if e.type==stt.SpeechEventType.FINAL_TRANSCRIPT]
        self.assertEqual(b''.join(client.activities),first+crossing)
        self.assertEqual([e.alternatives[0].metadata['boundary_reason'] for e in finals],['duration_limit','eof'])


    async def test_hybrid_punctuation_during_carried_continuation_ends_only_that_window(self):
        client=Client()
        _,stream=await self.make_hybrid(client)
        data=pcm(1000,256777)
        push(stream,data)
        await until(lambda: stream.stats['committed_windows']==1 and len(client.sessions[0].audio)==1554)
        client.sessions[0].incoming.put_nowait(event(interim_input_transcription=types.Transcription(text='Next.')))
        await until(lambda: stream.stats['committed_windows']==2)
        stream.end_input()
        finals=[e async for e in stream if e.type==stt.SpeechEventType.FINAL_TRANSCRIPT]
        self.assertEqual(client.activities,[data[:512000],data[512000:]])
        self.assertEqual(finals[1].alternatives[0].metadata['boundary_reason'],'google_interim_punctuation')


    async def test_hybrid_split_idle_zero_carry_is_accounted_but_in_activity_zeros_remain(self):
        client=Client()
        _,stream=await self.make_hybrid(client)
        first=pcm(1000,255000)+pcm(0,1000)
        data=first+pcm(0,333)
        push(stream,data)
        stream.end_input()
        finals=[e async for e in stream if e.type==stt.SpeechEventType.FINAL_TRANSCRIPT]
        self.assertEqual(client.activities,[first])
        self.assertEqual(len(finals),1)
        self.assertEqual(stream.stats['accounted_idle_zero_samples'],333)
        self.assertEqual(stream.stats['retries'],0)


    async def test_hybrid_nonzero_quiet_without_google_final_still_fails_honestly(self):
        client=Client(); client.no_final=True
        _,stream=await self.make_hybrid(client,finalize_timeout=.02,max_retries=0)
        push(stream,pcm(1,1600))
        stream.end_input()
        with self.assertRaises(current.RecoveryError):
            async for _ in stream:
                pass
        self.assertEqual(stream.stats['committed_windows'],0)
        self.assertEqual(stream.stats.get('vad_discarded_samples', 0),0)


    async def test_punctuation_wakes_idle_sender_without_another_frame(self):
        client = Client()
        provider, stream = await self.make_stream(client)
        data = pcm()
        push(stream, data)
        await until(lambda: client.sessions and bytes(client.sessions[0].audio) == data)
        for _ in range(3):
            client.sessions[0].incoming.put_nowait(event(interim_input_transcription=types.Transcription(text='جملة كاملة.')))
        await until(lambda: stream.stats['committed_windows'] == 1)
        stream.end_input()
        result = [e async for e in stream]
        self.assertEqual(client.activities, [data])
        self.assertEqual(sum('activity_end' in d for _,d in client.calls), 1)
        finals = [e for e in result if e.type == stt.SpeechEventType.FINAL_TRANSCRIPT]
        self.assertEqual(len(finals), 1)
        self.assertEqual(finals[0].alternatives[0].metadata['boundary_reason'], 'google_interim_punctuation')
        self.assertEqual(finals[0].alternatives[0].metadata['punctuation_requested_end_sample'], 1600)
        self.assertIsNone(provider._vad)
        self.assertTrue(client.config.realtime_input_config.automatic_activity_detection.disabled)
        self.assertEqual(client.config.input_audio_transcription.mode, 'VERBATIM')


    async def test_capture_during_finalization_preserves_every_sample_once(self):
        client = Client()
        client.hold_final.clear()
        _, stream = await self.make_stream(client)
        first, second, third = pcm(1, 333), pcm(2, 2057), pcm(3, 901)
        push(stream, first)
        await until(lambda: client.sessions and bytes(client.sessions[0].audio) == first)
        client.sessions[0].incoming.put_nowait(event(interim_input_transcription=types.Transcription(text='First.')))
        await until(lambda: len(client.activities) == 1)
        push(stream, second)
        push(stream, third)
        self.assertEqual(stream.buffered_audio_seconds, (333+2057+901)/16000)
        client.hold_final.set()
        await until(lambda: stream.stats['committed_windows'] == 1 and bytes(client.sessions[0].audio) == second+third)
        stream.end_input()
        result = [e async for e in stream]
        self.assertEqual(client.activities, [first, second+third])
        self.assertEqual(b''.join(client.activities), first+second+third)
        finals = [e for e in result if e.type == stt.SpeechEventType.FINAL_TRANSCRIPT]
        self.assertEqual([e.alternatives[0].text for e in finals], ['كرر هذا.']*2)
        self.assertNotEqual(finals[0].request_id, finals[1].request_id)
        self.assertEqual([(e.alternatives[0].metadata['audio_window_start_sample'],e.alternatives[0].metadata['audio_window_end_sample']) for e in finals], [(0,333),(333,3291)])
        self.assertEqual(stream.stats['discarded_digital_silence_samples'], 0)
        self.assertEqual(stream.stats.get('vad_discarded_samples', 0), 0)


    async def test_sealed_failed_activity_replays_identical_pcm_once_and_commits_once(self):
        client = Client(fail_first_end=True)
        _, stream = await self.make_stream(client)
        data = pcm(6, 4817)
        push(stream, data)
        await until(lambda: client.sessions and bytes(client.sessions[0].audio) == data)
        client.sessions[0].incoming.put_nowait(event(interim_input_transcription=types.Transcription(text='Retry this.')))
        await until(lambda: stream.stats['committed_windows'] == 1)
        stream.end_input()
        result = [e async for e in stream]
        self.assertEqual(client.activities, [data, data])
        finals = [e for e in result if e.type == stt.SpeechEventType.FINAL_TRANSCRIPT]
        self.assertEqual(len(finals), 1)
        self.assertEqual(finals[0].alternatives[0].metadata['recovery_attempt'], 1)
        self.assertEqual(stream.stats['replayed_samples'], 4817)


    async def test_stale_marker_cannot_end_a_new_attempt(self):
        client = Client()
        _, stream = await self.make_stream(client)
        data = pcm()
        push(stream, data)
        await until(lambda: stream._attempt is not None and client.sessions[0].audio)
        stream._input.put_nowait(_PunctuationBoundary(stream._attempt.window.id, -1, 1600))
        stream._input.put_nowait(_PunctuationBoundary('old-window', 0, 1600))
        await asyncio.sleep(.02)
        self.assertEqual(client.activities, [])
        stream.end_input()
        async for _ in stream:
            pass
        self.assertEqual(client.activities, [data])


    async def test_exact_zero_idle_start_and_eof_need_no_google_activity(self):
        client = Client()
        _, stream = await self.make_stream(client)
        data = pcm(0, 70*16000)
        push(stream, data)
        stream.end_input()
        self.assertEqual([e async for e in stream], [])
        self.assertEqual(client.sessions, [])
        self.assertEqual(stream.stats['accounted_idle_zero_samples'], 70*16000)
        self.assertEqual(stream.stats['retries'], 0)


    async def test_exact_zero_tail_after_final_does_not_create_empty_activity(self):
        client = Client()
        _, stream = await self.make_stream(client)
        leading, speech, inside = pcm(0, 320), pcm(1, 1600), pcm(0, 161)
        push(stream, leading)
        push(stream, speech)
        push(stream, inside)
        await until(lambda: client.sessions and bytes(client.sessions[0].audio) == speech+inside)
        client.sessions[0].incoming.put_nowait(event(interim_input_transcription=types.Transcription(text='Done.')))
        await until(lambda: stream.stats['committed_windows'] == 1)
        push(stream, pcm(0, 20000))
        stream.end_input()
        result = [e async for e in stream]
        self.assertEqual(client.activities, [speech+inside])
        self.assertEqual(stream.stats['accounted_idle_zero_samples'], 20320)
        self.assertEqual(stream.stats['retries'], 0)
        final = next(e for e in result if e.type == stt.SpeechEventType.FINAL_TRANSCRIPT)
        self.assertEqual(final.alternatives[0].metadata['audio_window_start_sample'], 320)


    async def test_nonzero_quiet_without_authoritative_final_fails_honestly(self):
        client = Client()
        client.no_final = True
        _, stream = await self.make_stream(client,finalize_timeout=.02,max_retries=0)
        push(stream,pcm(1))
        stream.end_input()
        results, error = [], None
        try:
            async for response in stream:
                results.append(response)
        except Exception as exc:
            error = exc
        await stream._task
        self.assertIsInstance(error,current.RecoveryError)
        self.assertEqual(stream.stats['accounted_idle_zero_samples'],0)
        self.assertEqual(stream.stats['committed_windows'],0)
        self.assertEqual([e for e in results if e.type==stt.SpeechEventType.FINAL_TRANSCRIPT],[])


    async def test_authoritative_empty_final_resolves_nonempty_preview(self):
        client = Client()
        client.final_text = ''
        _, stream = await self.make_stream(client)
        data = pcm(1)
        push(stream,data)
        await until(lambda: client.sessions and bytes(client.sessions[0].audio)==data)
        client.sessions[0].incoming.put_nowait(event(interim_input_transcription=types.Transcription(text='Speculative words')))
        await until(lambda: stream._attempt.saw_interim)
        stream.end_input()
        events = [e async for e in stream]
        interims = [e for e in events if e.type==stt.SpeechEventType.INTERIM_TRANSCRIPT]
        finals = [e for e in events if e.type==stt.SpeechEventType.FINAL_TRANSCRIPT]
        self.assertEqual([e.alternatives[0].text for e in interims],['Speculative words'])
        self.assertEqual([e.alternatives[0].text for e in finals],[''])
        self.assertEqual(finals[0].request_id,interims[0].request_id)
        self.assertEqual(stream.stats['committed_windows'],1)
        self.assertEqual(stream.buffered_audio_seconds,0)
        self.assertEqual(stream.stats['retries'],0)


    async def test_authoritative_final_preserves_outer_whitespace(self):
        client = Client()
        client.final_text = '  قال: «هذا جيد.»\n '
        _, stream = await self.make_stream(client)
        push(stream,pcm(1))
        stream.end_input()
        finals = [e async for e in stream if e.type==stt.SpeechEventType.FINAL_TRANSCRIPT]
        self.assertEqual([e.alternatives[0].text for e in finals],[client.final_text])
