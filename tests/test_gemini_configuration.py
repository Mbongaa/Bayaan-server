import asyncio
from dataclasses import dataclass
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from livekit.agents import stt
import gemini_transcription as adapter
from gemini_stt.compat import SpeechData
from gemini_stt.buffer import forward_hybrid_sentences


class GeminiConfigurationTests(unittest.TestCase):
    def test_vertex_defaults_exact_lab_endpoint_model_and_language_hint(self):
        options = adapter.gemini_provider_options('ar-EG', {
            'GOOGLE_APPLICATION_CREDENTIALS_JSON': json.dumps({'type': 'service_account', 'project_id': 'test-project'}),
            'LIVEKIT_URL': 'must-not-be-used', 'LIVEKIT_VERTEX_URL': 'must-not-be-used',
            'LOCAL_GEMINI_FAULT_AFTER_AUDIO_SECONDS': '1',
        })
        self.assertEqual(options['model'], 'gemini-3.5-transcribe-live-preview')
        self.assertTrue(options['vertexai'])
        self.assertEqual(options['location'], 'global')
        self.assertEqual(options['language_codes'], ['ar-EG'])
        self.assertEqual(options['transcription_mode'], 'VERBATIM')
        self.assertEqual(options['project'], 'test-project')
        self.assertFalse(any('livekit' in name.lower() for name in options))
        self.assertIsNone(adapter.RecoveryOptions().fault_after_audio_seconds)

    def test_explicit_developer_api_uses_stable_model_and_key(self):
        options = adapter.gemini_provider_options('en-US', {'GEMINI_STT_BACKEND': 'developer', 'GEMINI_API_KEY': 'test-only'})
        self.assertFalse(options['vertexai'])
        self.assertEqual(options['model'], 'gemini-3.5-transcribe-live')
        self.assertEqual(options['api_key'], 'test-only')

    def test_classroom_selection_and_google_hint_remain_separate(self):
        provider = adapter.create_gemini_provider(language='ar-gemini', language_hint='ar-EG',
            env={'GEMINI_STT_BACKEND': 'developer', 'GEMINI_API_KEY': 'test-only'})
        self.assertEqual(provider.language, 'ar-gemini')
        self.assertEqual(provider._config.input_audio_transcription.language_codes, ['ar-EG'])

    def test_service_account_file_project_and_explicit_project_override(self):
        with TemporaryDirectory() as folder:
            path = Path(folder)/'google.json'
            path.write_text(json.dumps({'type': 'service_account', 'project_id': 'from-file'}), encoding='utf-8-sig')
            env = {'GOOGLE_APPLICATION_CREDENTIALS': str(path)}
            self.assertEqual(adapter.gemini_provider_options('fr-FR', env)['project'], 'from-file')
            env['GOOGLE_CLOUD_PROJECT'] = 'override'
            options = adapter.gemini_provider_options('fr-FR', env)
            self.assertEqual(options['project'], 'override')
            self.assertEqual(options['credentials_path'], str(path.resolve()))

    def test_missing_malformed_credentials_fail_without_echoing_values(self):
        secret = 'private-value-must-not-appear'
        for env in ({}, {'GEMINI_STT_BACKEND': 'developer'}, {'GEMINI_STT_BACKEND': 'invalid'},
                    {'GOOGLE_APPLICATION_CREDENTIALS_JSON': secret},
                    {'GOOGLE_APPLICATION_CREDENTIALS_JSON': json.dumps({'type': secret})},
                    {'GOOGLE_APPLICATION_CREDENTIALS': secret},
                    {'GOOGLE_APPLICATION_CREDENTIALS_JSON': json.dumps({'type': 'service_account'})}):
            with self.subTest(keys=tuple(env)), self.assertRaises(ValueError) as raised:
                adapter.gemini_provider_options('ar-EG', env)
            self.assertNotIn(secret, str(raised.exception))

    def test_factory_does_not_accept_lab_optional_modes(self):
        for options in ({'transcription_mode': 'SMART'}, {'recovery': adapter.RecoveryOptions(vad_enabled=False), 'punctuation_stability': True}):
            with self.subTest(options=tuple(options)), self.assertRaises((ValueError, TypeError)):
                adapter.GeminiHybridSTT(model='test', vertexai=False, api_key='fake', **options)
        with self.assertRaises(ValueError):
            adapter.RecoveryOptions(vad_enabled=True)

    def test_agents121_capabilities_constructor_compatibility(self):
        # Exact public constructor exported by official livekit-agents1.2.1.
        @dataclass
        class Capabilities121:
            streaming: bool
            interim_results: bool
        with patch.object(stt, 'STTCapabilities', Capabilities121):
            provider = adapter.GeminiHybridSTT(model='test', vertexai=False, api_key='fake')
        self.assertEqual(provider.capabilities, Capabilities121(True, True))

    def test_agents121_speechdata_supports_provenance_without_sdk_mutation(self):
        # Exact fields exported by official livekit-agents1.2.1; inspected from
        # the PyPI wheel. Run the actual compatibility module against that type.
        @dataclass
        class SpeechData121:
            language: str
            text: str
            start_time: float = 0.0
            end_time: float = 0.0
            confidence: float = 0.0
            speaker_id: str | None = None
        source = Path(adapter.__file__).parent/'gemini_stt/compat.py'
        scope = {}
        with patch.object(stt, 'SpeechData', SpeechData121):
            exec(compile(source.read_text(encoding='utf-8'), str(source), 'exec'), scope)
            data = scope['SpeechData'](language='ar', text='opening', metadata={'window_id': 'first'})
        self.assertIsInstance(data, SpeechData121)
        self.assertEqual(data.metadata['window_id'], 'first')
        self.assertNotIn('metadata', SpeechData121.__dataclass_fields__)


class GeminiMultilingualBufferTests(unittest.IsolatedAsyncioTestCase):
    async def test_unspaced_languages_release_first_punctuated_sentence_without_waiting(self):
        for language, text in (('zh-CN', '今天我们学习耐心。'), ('ja-JP', '今日は忍耐について学びます。'),
                               ('th-TH', 'วันนี้เราจะเรียนรู้เรื่องความอดทน.'), ('my-MM', 'စာသင်ခန်း.')):
            with self.subTest(language=language):
                finished = asyncio.Event()
                ended = asyncio.Event()
                ended.set()
                publish, translate = AsyncMock(), AsyncMock()
                async def events():
                    yield stt.SpeechEvent(type=stt.SpeechEventType.FINAL_TRANSCRIPT,
                                         alternatives=[SpeechData(language=language, text=text)])
                    translate.assert_awaited_once()
                    finished.set()
                await forward_hybrid_sentences(events(), publish, translate, ended, language)
                self.assertTrue(finished.is_set())
                self.assertEqual(translate.await_args.args[0], text)

    async def test_arabic_ui_alias_keeps_exact_three_word_rule(self):
        ended = asyncio.Event()
        ended.set()
        publish, translate = AsyncMock(), AsyncMock()
        async def events():
            yield stt.SpeechEvent(type=stt.SpeechEventType.FINAL_TRANSCRIPT,
                                 alternatives=[SpeechData(language='ar-EG', text='الحمد لله.')])
            translate.assert_not_awaited()
            yield stt.SpeechEvent(type=stt.SpeechEventType.FINAL_TRANSCRIPT,
                                 alternatives=[SpeechData(language='ar-EG', text='دائما.')])
        await forward_hybrid_sentences(events(), publish, translate, ended, 'ar-gemini')
        self.assertEqual(translate.await_args.args[0], 'الحمد لله. دائما.')
        self.assertEqual(translate.await_args.args[2], 'ar-gemini')

    async def test_cjk_audio_cuts_rejoin_without_manufacturing_a_space(self):
        ended = asyncio.Event()
        ended.set()
        publish, translate = AsyncMock(), AsyncMock()
        async def events():
            for text in ('今天我们', '学习耐心。'):
                yield stt.SpeechEvent(type=stt.SpeechEventType.FINAL_TRANSCRIPT,
                                     alternatives=[SpeechData(language='zh-CN', text=text)])
        await forward_hybrid_sentences(events(), publish, translate, ended, 'zh-CN')
        self.assertEqual(translate.await_args.args[0], '今天我们学习耐心。')
