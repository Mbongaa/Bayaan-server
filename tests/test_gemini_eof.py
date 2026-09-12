import asyncio
import io
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import wave

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from google.genai import types
from livekit.agents import stt
from gemini_stt.eof import completed_transcription, wav_bytes
from gemini_stt.buffer import forward_hybrid_sentences
from gemini_stt import recovery
import test_gemini_hybrid_stream as fixtures
from test_gemini_hybrid_stream import Client, event, pcm, push, until


def response(text=None, reason='STOP'):
    candidate = {'finish_reason': reason}
    if text is not None:
        candidate['content'] = {'parts': [{'text': text, 'audio_transcription': {'text': text}}]}
    return types.GenerateContentResponse(candidates=[candidate])


class EofResponseTests(unittest.TestCase):
    def test_completed_empty_is_distinct_from_missing_response(self):
        self.assertEqual(completed_transcription(response()), '')
        for incomplete in (None, types.GenerateContentResponse(), types.GenerateContentResponse(candidates=[])):
            with self.subTest(response=incomplete), self.assertRaises(ValueError):
                completed_transcription(incomplete)

    def test_text_and_matching_audio_annotation_are_not_duplicated(self):
        self.assertEqual(completed_transcription(response('السلام عليكم.')), 'السلام عليكم.')

    def test_truncated_and_blocked_results_are_never_confirmed(self):
        for reason in ('MAX_TOKENS', 'SAFETY', 'OTHER', 'FINISH_REASON_UNSPECIFIED'):
            with self.subTest(reason=reason), self.assertRaises(ValueError):
                completed_transcription(response('Unconfirmed', reason))

    def test_non_text_parts_are_not_interpreted_as_empty_transcription(self):
        value=types.GenerateContentResponse(candidates=[{'finish_reason':'STOP',
            'content':{'parts':[{'inline_data':{'data':b'x','mime_type':'application/octet-stream'}}]}}])
        with self.assertRaises(ValueError):
            completed_transcription(value)

    def test_wav_wrapper_preserves_every_source_sample_without_padding(self):
        source = pcm(2,3520)
        with wave.open(io.BytesIO(wav_bytes(source)), 'rb') as audio:
            self.assertEqual((audio.getnchannels(), audio.getsampwidth(), audio.getframerate()), (1,2,16000))
            self.assertEqual(audio.readframes(audio.getnframes()), source)


class EofStreamTests(unittest.IsolatedAsyncioTestCase):
    # Reuse only the stream factory, not the source test class's test methods.
    make_stream = fixtures.GeminiHybridStreamTests.make_stream

    async def make_eof(self, final_response=None, **options):
        client=Client()
        client.no_final=True
        client.file_calls=[]
        client.file_response=response() if final_response is None else final_response
        client.file_wait=None
        client.file_reader_closed=[]
        async def generate_content(**kwargs):
            client.file_calls.append(kwargs)
            client.file_reader_closed.append(all(session.closed for session in client.sessions))
            if client.file_wait is not None:
                await client.file_wait.wait()
            return client.file_response
        client.aio.models=SimpleNamespace(generate_content=generate_content)
        options.setdefault('finalize_timeout', .5)
        options.setdefault('max_retries', 3)
        provider,stream=await self.make_stream(client,**options)
        provider.model='gemini-3.5-transcribe-live-preview'
        return client,provider,stream

    async def test_eof_quiet_tail_commits_only_after_explicit_empty_file_result(self):
        client,_,stream=await self.make_eof()
        source=pcm(1,3520)
        push(stream,source)
        stream.end_input()
        finals=[e async for e in stream if e.type==stt.SpeechEventType.FINAL_TRANSCRIPT]
        self.assertEqual(len(finals),1)
        self.assertEqual(finals[0].alternatives[0].text,'')
        metadata=finals[0].alternatives[0].metadata
        self.assertEqual(metadata['finalization_source'],'eof_file_transcription')
        self.assertEqual(metadata['audio_window_start_sample'],0)
        self.assertEqual(metadata['audio_window_end_sample'],3520)
        self.assertEqual(stream.stats['committed_windows'],1)
        self.assertEqual(stream.stats['eof_file_empty_windows'],1)
        self.assertEqual(stream.stats['retries'],0)
        self.assertTrue(client.sessions[0].closed)
        request=client.file_calls[0]
        self.assertEqual(request['model'],'gemini-3.5-transcribe-preview')
        self.assertEqual(request['config'].audio_transcription_config.mode,'VERBATIM')
        with wave.open(io.BytesIO(request['contents'][0].inline_data.data),'rb') as audio:
            self.assertEqual(audio.readframes(audio.getnframes()),source)

    async def test_file_fallback_receives_only_uncommitted_tail(self):
        client,_,stream=await self.make_eof(response('Confirmed tail.'))
        client.no_final=False
        first,tail=pcm(4,1600),pcm(5,3520)
        push(stream,first)
        await until(lambda: client.sessions and client.sessions[0].audio)
        client.sessions[0].incoming.put_nowait(event(interim_input_transcription=types.Transcription(text='First.')))
        await until(lambda: stream.stats['committed_windows']==1)
        client.no_final=True
        push(stream,tail)
        stream.end_input()
        finals=[e async for e in stream if e.type==stt.SpeechEventType.FINAL_TRANSCRIPT]
        self.assertEqual(len(finals),2)
        self.assertEqual(finals[1].alternatives[0].text,'Confirmed tail.')
        self.assertNotEqual(finals[0].request_id,finals[1].request_id)
        self.assertEqual(finals[1].alternatives[0].metadata['audio_window_start_sample'],1600)
        with wave.open(io.BytesIO(client.file_calls[0]['contents'][0].inline_data.data),'rb') as audio:
            self.assertEqual(audio.readframes(audio.getnframes()),tail)

    async def test_normal_live_final_does_not_call_file_endpoint(self):
        client,_,stream=await self.make_eof()
        client.no_final=False
        push(stream,pcm(1))
        stream.end_input()
        finals=[e async for e in stream if e.type==stt.SpeechEventType.FINAL_TRANSCRIPT]
        self.assertEqual(len(finals),1)
        self.assertEqual(finals[0].alternatives[0].metadata['finalization_source'],'live')
        self.assertEqual(client.file_calls,[])

    async def test_missing_punctuation_final_is_confirmed_then_future_audio_continues(self):
        client,_,stream=await self.make_eof(response('Confirmed first activity.'), max_retries=0)
        first, tail = pcm(1, 3520), pcm(2, 1600)
        push(stream, first)
        await until(lambda: client.sessions and client.sessions[0].audio)
        client.sessions[0].incoming.put_nowait(event(interim_input_transcription=types.Transcription(text='Draft.')))
        # Continue capturing while the live endpoint has not returned a final.
        await until(lambda: len(client.activities) == 1)
        push(stream, tail)
        await until(lambda: stream.stats['committed_windows'] == 1)
        self.assertFalse(stream._input_ended)
        client.no_final=False
        stream.end_input()
        finals=[e async for e in stream if e.type==stt.SpeechEventType.FINAL_TRANSCRIPT]
        self.assertEqual([f.alternatives[0].text for f in finals], ['Confirmed first activity.', client.final_text])
        self.assertEqual([f.alternatives[0].metadata['finalization_source'] for f in finals],
                         ['activity_file_transcription', 'live'])
        self.assertEqual([f.alternatives[0].metadata['audio_window_start_sample'] for f in finals], [0,3520])
        self.assertEqual([f.alternatives[0].metadata['audio_window_end_sample'] for f in finals], [3520,5120])
        self.assertEqual(len(set(f.request_id for f in finals)), 2)
        self.assertEqual(client.activities, [first,tail])
        self.assertEqual(client.file_reader_closed, [True])
        self.assertEqual(len(client.sessions), 2)
        self.assertEqual(stream.stats['sealed_file_finalized_windows'],1)
        self.assertEqual(stream.stats.get('eof_file_requests',0),0)
        self.assertEqual(stream.stats['retries'],0)
        with wave.open(io.BytesIO(client.file_calls[0]['contents'][0].inline_data.data),'rb') as audio:
            self.assertEqual(audio.readframes(audio.getnframes()), first)

    async def test_quiet_16_second_activity_confirms_empty_and_retains_following_speech(self):
        client,_,stream=await self.make_eof()
        quiet, speech = pcm(1, 16*16000), pcm(3000, 2057)
        # A frame crossing the maximum also exercises the retained carry path.
        push(stream, quiet+speech)
        await until(lambda: stream.stats['committed_windows']==1)
        self.assertFalse(stream._input_ended)
        client.no_final=False
        stream.end_input()
        finals=[e async for e in stream if e.type==stt.SpeechEventType.FINAL_TRANSCRIPT]
        self.assertEqual([f.alternatives[0].text for f in finals], ['',client.final_text])
        self.assertEqual([f.alternatives[0].metadata['boundary_reason'] for f in finals], ['duration_limit','eof'])
        self.assertEqual(finals[1].alternatives[0].metadata['audio_window_start_sample'],256000)
        self.assertEqual(finals[1].alternatives[0].metadata['audio_window_end_sample'],258057)
        self.assertEqual(client.activities,[quiet,speech])
        self.assertEqual(stream.stats['sealed_file_empty_windows'],1)
        self.assertEqual(stream.stats['committed_windows'],2)
        self.assertEqual(stream.stats['retries'],0)

    async def test_ambiguous_non_eof_file_result_keeps_activity_uncommitted_and_never_replays(self):
        client,_,stream=await self.make_eof(types.GenerateContentResponse(), max_retries=3)
        push(stream,pcm(1))
        await until(lambda: client.sessions and client.sessions[0].audio)
        client.sessions[0].incoming.put_nowait(event(interim_input_transcription=types.Transcription(text='Unconfirmed draft.')))
        with self.assertRaises(recovery.RecoveryError):
            async for _ in stream:
                pass
        self.assertFalse(stream._input_ended)
        self.assertEqual(len(client.file_calls),1)
        self.assertEqual(stream.stats['committed_windows'],0)
        self.assertEqual(stream.stats['retries'],0)
        self.assertEqual(len(client.sessions),1)

    async def test_non_eof_late_live_final_wins_then_next_activity_opens_new_socket(self):
        client,_,stream=await self.make_eof()
        disconnect=stream._disconnect
        async def close_after_live_final():
            attempt=stream._attempt
            await disconnect()
            if attempt is not None and not attempt.done.done():
                attempt.done.set_result(('Late authoritative live final.','ar-EG'))
        stream._disconnect=close_after_live_final
        push(stream,pcm(1,3520))
        await until(lambda: client.sessions and client.sessions[0].audio)
        client.sessions[0].incoming.put_nowait(event(interim_input_transcription=types.Transcription(text='Draft.')))
        await until(lambda: stream.stats['committed_windows']==1)
        client.no_final=False
        push(stream,pcm(2,1600))
        stream.end_input()
        finals=[e async for e in stream if e.type==stt.SpeechEventType.FINAL_TRANSCRIPT]
        self.assertEqual([f.alternatives[0].text for f in finals],['Late authoritative live final.',client.final_text])
        self.assertEqual([f.alternatives[0].metadata['finalization_source'] for f in finals],['live','live'])
        self.assertEqual(client.file_calls,[])
        self.assertEqual(len(client.sessions),2)

    async def test_sentence_buffer_keeps_its_tail_across_file_rescue_and_socket_change(self):
        client,provider,stream=await self.make_eof(response('لله رب العالمين.'))
        client.no_final=False
        client.final_text='الحمد'
        publications,translations=[],[]
        ended=asyncio.Event()
        async def publish(*item):
            publications.append(item)
        async def translate(*item):
            translations.append(item)
        forwarding=asyncio.create_task(forward_hybrid_sentences(
            stream,publish,translate,ended,provider.language))
        push(stream,pcm(1,1600))
        await until(lambda: client.sessions and client.sessions[0].audio)
        client.sessions[0].incoming.put_nowait(event(interim_input_transcription=types.Transcription(text='الحمد.')))
        await until(lambda: stream.stats['committed_windows']==1)
        client.no_final=True
        push(stream,pcm(2,3520))
        await until(lambda: len(client.calls)>0 and bytes(client.sessions[0].audio)==pcm(2,3520))
        client.sessions[0].incoming.put_nowait(event(interim_input_transcription=types.Transcription(text='لله رب العالمين.')))
        await until(lambda: stream.stats['committed_windows']==2)
        client.no_final=False
        client.final_text='تستمر ترجمة هذا الكلام.'
        push(stream,pcm(3,1600))
        ended.set()
        stream.end_input()
        await asyncio.wait_for(forwarding,3)
        self.assertEqual([item[0] for item in translations],['الحمد لله رب العالمين.','تستمر ترجمة هذا الكلام.'])
        self.assertEqual(len(set(item[1] for item in translations)),2)
        self.assertEqual(len([item for item in publications if item[3]]),3)
        self.assertEqual(stream.stats['committed_windows'],3)
        self.assertEqual(len(client.sessions),2)

    async def test_ambiguous_file_result_fails_once_without_retry_or_empty_commit(self):
        client,_,stream=await self.make_eof(types.GenerateContentResponse())
        push(stream,pcm(1))
        stream.end_input()
        with self.assertRaises(recovery.RecoveryError):
            async for _ in stream:
                pass
        self.assertEqual(len(client.file_calls),1)
        self.assertEqual(stream.stats['committed_windows'],0)
        self.assertEqual(stream.stats['retries'],0)

    async def test_file_timeout_is_bounded_and_never_commits(self):
        client,_,stream=await self.make_eof()
        client.file_wait=asyncio.Event()
        push(stream,pcm(1))
        stream.end_input()
        with patch('gemini_stt.hybrid.EOF_REQUEST_TIMEOUT_SECONDS',.02):
            with self.assertRaises(recovery.RecoveryError):
                async for _ in stream:
                    pass
        self.assertEqual(len(client.file_calls),1)
        self.assertEqual(stream.stats['committed_windows'],0)

    async def test_cancelled_file_request_never_commits(self):
        client,_,stream=await self.make_eof()
        client.file_wait=asyncio.Event()
        push(stream,pcm(1))
        stream.end_input()
        await until(lambda: client.file_calls)
        await stream.aclose()
        self.assertEqual(stream.stats['committed_windows'],0)

    async def test_late_live_final_wins_disconnect_race_without_file_request(self):
        client,_,stream=await self.make_eof()
        disconnect=stream._disconnect
        async def close_after_live_final():
            attempt=stream._attempt
            await disconnect()
            if attempt is not None and not attempt.done.done():
                attempt.done.set_result(('Late authoritative live final.','ar-EG'))
        stream._disconnect=close_after_live_final
        push(stream,pcm(1,3520))
        stream.end_input()
        finals=[e async for e in stream if e.type==stt.SpeechEventType.FINAL_TRANSCRIPT]
        self.assertEqual([f.alternatives[0].text for f in finals],['Late authoritative live final.'])
        self.assertEqual(finals[0].alternatives[0].metadata['finalization_source'],'live')
        self.assertEqual(client.file_calls,[])
        self.assertEqual(stream.stats['committed_windows'],1)

    async def test_shared_stop_deadline_interrupts_file_request_before_its_own_timeout(self):
        client,_,stream=await self.make_eof(recovery_timeout=.9,finalize_timeout=.3)
        client.file_wait=asyncio.Event()
        push(stream,pcm(1))
        stream.end_input()
        async def drain():
            async for _ in stream:
                pass
        with patch('gemini_stt.hybrid.EOF_REQUEST_TIMEOUT_SECONDS',10):
            with self.assertRaises(recovery.RecoveryError):
                await asyncio.wait_for(drain(),3)
        self.assertEqual(len(client.file_calls),1)
        self.assertEqual(stream.stats['committed_windows'],0)
        self.assertEqual(stream.stats['retries'],0)


if __name__=='__main__':
    unittest.main()
