"""The translator's model choice, its tuned request settings and the fallback path."""
import asyncio
from collections import deque
from enum import Enum
import json
import logging
import os
from types import SimpleNamespace
from typing import Optional, Dict, Any, List
import unittest
from unittest.mock import AsyncMock, patch

from livekit import rtc
from livekit.agents import APIConnectionError, APIError, APIStatusError, llm, utils

from config import TranslationConfig
from language_routing import translation_language
from test_production_prompt_reuse import SERVER, load_class

LUNA = {'temperature': 0, 'reasoning_effort': 'none', 'service_tier': 'priority'}
MINI = {'temperature': 0, 'reasoning_effort': 'none'}


def after_plugin_retries(status_code):
    """The error the LiveKit plugin raises once its own retries are used up: the status error is only the cause."""
    try:
        raise APIConnectionError('failed to generate LLM completion after 4 attempts') from APIStatusError(
            'rejected', status_code=status_code)
    except APIConnectionError as error:
        return error


class FakeStream:
    def __init__(self, outcome):
        self.outcome = outcome

    def __aiter__(self):
        return self

    async def __anext__(self):
        if isinstance(self.outcome, Exception):
            raise self.outcome
        if self.outcome is None:
            raise StopAsyncIteration
        text, self.outcome = self.outcome, None
        return SimpleNamespace(delta=SimpleNamespace(content=text))

    async def aclose(self):
        pass


class FakeLLM:
    """Stands in for livekit.plugins.openai.LLM and records how it was called."""

    def __init__(self, outcomes, model):
        self.model, self.requests, self.closed = model, [], False
        self.outcome = outcomes[model]

    def chat(self, *, chat_ctx, extra_kwargs):
        self.requests.append(extra_kwargs)
        return FakeStream(self.outcome)

    async def aclose(self):
        self.closed = True


class TranslationModelTests(unittest.IsolatedAsyncioTestCase):
    def make_translator(self, outcomes, **environment):
        with patch.dict(os.environ):
            os.environ.pop('TRANSLATION_MODEL', None)
            os.environ.update(environment)
            config = SimpleNamespace(translation=TranslationConfig())
        self.created = []

        def create_llm(*, model):
            self.created.append(FakeLLM(outcomes, model))
            return self.created[-1]

        namespace = dict(config=config, asyncio=asyncio, deque=deque, Enum=Enum,
                         Optional=Optional, Dict=Dict, Any=Any, List=List,
                         logger=logging.getLogger('model-test'),
                         query_prompt_template_for_room=AsyncMock(return_value=None))
        builder = load_class(SERVER / 'prompt_builder.py', 'PromptBuilder', namespace)()
        namespace.update(rtc=rtc, utils=utils, json=json, llm=llm, APIError=APIError, APIStatusError=APIStatusError,
                         prompt_builder=builder, openai=SimpleNamespace(LLM=create_llm),
                         TranslationError=RuntimeError)
        translator_class = load_class(SERVER / 'translator.py', 'Translator', namespace)
        room = SimpleNamespace(local_participant=SimpleNamespace(identity='agent'))
        return translator_class(room, translation_language('nl', config.translation.supported_languages))

    def test_default_model_runs_with_tuned_settings_and_others_run_plain(self):
        with patch.dict(os.environ):
            os.environ.pop('TRANSLATION_MODEL', None)
            settings = TranslationConfig()
        self.assertEqual(settings.model, 'gpt-6-luna')
        self.assertEqual(settings.llm_settings('gpt-6-luna'), LUNA)
        # The rollback model keeps the settings it was benchmarked with, without Fast processing.
        self.assertEqual(settings.llm_settings('gpt-5.4-mini'), MINI)
        self.assertEqual(settings.llm_settings('gpt-5.4-mini-2026-03-17'), MINI)
        # gpt-4.1 rejects reasoning_effort, so it must be called without extra settings.
        self.assertEqual(settings.llm_settings('gpt-4.1'), {})

    async def test_translation_request_uses_the_configured_model_and_settings(self):
        translator = self.make_translator({'gpt-6-luna': 'Wij zeggen niet dat de aanbidding ongeldig is.'})
        result = await translator._perform_translation('ما نقولو احنا العباده راها باطله.', 'ar')
        self.assertEqual(result, 'Wij zeggen niet dat de aanbidding ongeldig is.')
        self.assertEqual([(fake.model, fake.requests) for fake in self.created], [('gpt-6-luna', [LUNA])])

    async def test_environment_variable_rolls_back_to_the_previous_models(self):
        for model, settings in (('gpt-5.4-mini', MINI), ('gpt-4.1', {})):
            with self.subTest(model=model):
                translator = self.make_translator({model: 'Alle lof behoort toe aan Allah.'}, TRANSLATION_MODEL=model)
                result = await translator._perform_translation('الحمد لله.', 'ar')
                self.assertEqual(result, 'Alle lof behoort toe aan Allah.')
                self.assertEqual([(fake.model, fake.requests) for fake in self.created], [(model, [settings])])

    async def test_inaccessible_model_falls_back_for_the_rest_of_the_session(self):
        direct = APIStatusError('Project does not have access to model', status_code=403)
        for no_access in (after_plugin_retries(403), after_plugin_retries(404), direct):
            with self.subTest(error=repr(no_access)):
                translator = self.make_translator({'gpt-6-luna': no_access, 'gpt-4.1': 'Alle lof behoort toe aan Allah.'})
                first = await translator._perform_translation('الحمد لله.', 'ar')
                second = await translator._perform_translation('الحمد لله.', 'ar')
                self.assertEqual([first, second], ['Alle lof behoort toe aan Allah.'] * 2)
                primary, fallback = self.created
                self.assertEqual((primary.model, len(primary.requests), primary.closed), ('gpt-6-luna', 1, True))
                self.assertEqual((fallback.model, fallback.requests), ('gpt-4.1', [{}, {}]))
                self.assertEqual(translator.model, 'gpt-4.1')

    async def test_other_failures_are_not_hidden_by_the_fallback(self):
        for failure in (after_plugin_retries(500), APIConnectionError('network down')):
            with self.subTest(error=repr(failure)):
                translator = self.make_translator({'gpt-6-luna': failure})
                with self.assertRaises(APIError):
                    await translator._perform_translation('الحمد لله.', 'ar')
                self.assertEqual((translator.model, len(self.created)), ('gpt-6-luna', 1))

        # Already on the fallback model: there is nothing left to switch to.
        translator = self.make_translator({'gpt-4.1': after_plugin_retries(404)}, TRANSLATION_MODEL='gpt-4.1')
        with self.assertRaises(APIError):
            await translator._perform_translation('الحمد لله.', 'ar')
        self.assertEqual(len(self.created), 1)


if __name__ == '__main__':
    unittest.main()
