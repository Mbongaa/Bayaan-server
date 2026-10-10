"""Execute the real prompt/translator classes with only network boundaries mocked."""
import ast
import asyncio
from collections import deque
from enum import Enum
import json
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Optional, Dict, Any, List
import unittest
from unittest.mock import AsyncMock

from config import TranslationConfig
from language_routing import translation_language

SERVER = Path(__file__).resolve().parents[1]


def load_class(path, name, namespace):
    tree = ast.parse(path.read_text(encoding='utf-8'))
    selected = [node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == name]
    module = ast.fix_missing_locations(ast.Module(body=selected, type_ignores=[]))
    exec(compile(module, str(path), 'exec'), namespace)
    return namespace[name]


class ProductionPromptTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.config = SimpleNamespace(translation=TranslationConfig())
        namespace = dict(config=self.config, asyncio=asyncio, deque=deque, Enum=Enum,
                         Optional=Optional, Dict=Dict, Any=Any, List=List,
                         logger=logging.getLogger('prompt-test'),
                         query_prompt_template_for_room=AsyncMock(return_value=None))
        self.builder = load_class(SERVER / 'prompt_builder.py', 'PromptBuilder', namespace)()
        self.room = SimpleNamespace(local_participant=SimpleNamespace(identity='agent', publish_transcription=AsyncMock(), publish_data=AsyncMock()))
        from livekit import rtc
        from livekit.agents import utils
        namespace.update(rtc=rtc, utils=utils, json=json, prompt_builder=self.builder,
                         openai=SimpleNamespace(LLM=lambda **kwargs: SimpleNamespace()),
                         TranslationError=RuntimeError)
        self.Translator = load_class(SERVER / 'translator.py', 'Translator', namespace)

    async def test_default_prompt_and_source_note_identical_for_both_arabic_routes(self):
        old = self.Translator(self.room, translation_language('en', self.config.translation.supported_languages))
        new = self.Translator(self.room, old.lang)
        await old._initialize_prompt('ar')
        await new._initialize_prompt('ar-gemini')
        self.assertEqual(old.system_prompt, new.system_prompt)
        self.assertIn('formal Arabic khutbahs', new.system_prompt)
        self.assertIn('Source language note: the text you receive is Arabic speech', new.system_prompt)
        self.assertEqual(new.max_context_pairs, 12)

    async def test_direct_room_prompt_and_context_override_are_preserved(self):
        context = dict(translation_prompt='Translate {source_lang} into {target_lang}; retain Quran citations.', context_window_size=6)
        old = self.Translator(self.room, translation_language('nl', self.config.translation.supported_languages), context)
        new = self.Translator(self.room, old.lang, context)
        await old._initialize_prompt('ar')
        await new._initialize_prompt('ar-gemini')
        self.assertEqual(new.system_prompt, old.system_prompt)
        self.assertTrue(new.system_prompt.startswith('Translate Arabic into Dutch; retain Quran citations.'))
        self.assertEqual(new.max_context_pairs, 6)

    async def test_delivery_retry_reuses_translation_and_caption_id(self):
        translator = self.Translator(self.room, translation_language('en'))
        translator._perform_translation = AsyncMock(return_value='Praise be to Allah.')
        self.room.local_participant.publish_transcription.side_effect = [RuntimeError('temporary transport loss'), None]
        result = await translator.translate_strict('الحمد لله رب العالمين.', 'SEG_test', 'ar-gemini')
        self.assertEqual(result, 'Praise be to Allah.')
        translator._perform_translation.assert_awaited_once()
        sent = self.room.local_participant.publish_transcription.await_args_list
        self.assertEqual(sent[0].args[0].segments[0].id, sent[1].args[0].segments[0].id)

    async def test_failed_delivery_is_not_counted_as_success(self):
        translator = self.Translator(self.room, translation_language('en'))
        translator._perform_translation = AsyncMock(return_value='Praise be to Allah.')
        self.room.local_participant.publish_transcription.side_effect = RuntimeError('transport lost')
        with self.assertRaises(RuntimeError):
            await translator.translate_strict('الحمد لله رب العالمين.', 'SEG_test', 'ar-gemini')
        self.assertEqual(translator.translation_count, 0)
        translator._perform_translation.assert_awaited_once()

    def test_fallback_prompt_stays_identical_to_default_prompt(self):
        # The Khutba prompt lives in prompt_builder.py, in the translator fallback and in
        # Supabase. The two copies in this repository must never drift apart.
        translator = self.Translator(self.room, translation_language('nl', self.config.translation.supported_languages))
        self.assertEqual(translator._fallback_prompt('Arabic', 'Dutch'),
                         self.builder.DEFAULT_PROMPT.format(source_lang='Arabic', target_lang='Dutch'))
        self.assertIn('Whenever the text is or contains a Qur’anic ayah', self.builder.DEFAULT_PROMPT)


if __name__ == '__main__':
    unittest.main()
