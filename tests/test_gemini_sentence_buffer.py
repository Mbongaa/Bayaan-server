import asyncio
from pathlib import Path
import sys
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from livekit.agents import stt
from gemini_stt.buffer import forward_hybrid_sentences, _SentenceBuffer
from gemini_stt.compat import SpeechData


def event(text, final=True, language='ar', request_id='', window_id=None):
    return stt.SpeechEvent(
        type=stt.SpeechEventType.FINAL_TRANSCRIPT if final else stt.SpeechEventType.INTERIM_TRANSCRIPT,
        request_id=request_id,
        alternatives=[SpeechData(language=language, text=text,
                                     metadata={'window_id': window_id} if window_id else None)])


async def stream(*events):
    for item in events:
        if isinstance(item, BaseException):
            raise item
        yield item


class GeminiSentenceForwardingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.ended = asyncio.Event()
        self.ended.set()
        self.publish, self.translate = AsyncMock(), AsyncMock()
        self.audit_patch = patch('gemini_stt.buffer.audit_log.emit')
        self.audit = self.audit_patch.start()
        self.addCleanup(self.audit_patch.stop)


    async def run_events(self, *events, source='ar', **options):
        await forward_hybrid_sentences(stream(*events), self.publish, self.translate,
                                       self.ended, source, **options)


    def translated(self):
        return [call.args[0] for call in self.translate.await_args_list]


    def releases(self):
        return [call.kwargs for call in self.audit.call_args_list
                if call.args[0] == 'translation_buffer_emitted']


    async def test_raw_draft_revisions_preserve_id_but_only_final_enters_buffer(self):
        await self.run_events(event('مقدمة غير صحيحة. نختبر', False),
                              event('مسودة أخرى طويلة.', False),
                              event('  الحمد لله. اليوم نتحدث\n'),
                              event('عن الصبر والصلاة.'))
        self.assertEqual(self.translated(), ['الحمد لله. اليوم نتحدث عن الصبر والصلاة.'])
        calls = self.publish.await_args_list
        self.assertEqual(len(calls), 4)
        self.assertEqual(len({call.args[0] for call in calls[:3]}), 1)
        self.assertNotEqual(calls[2].args[0], calls[3].args[0])
        self.assertEqual(calls[2].args[1:], ('  الحمد لله. اليوم نتحدث\n', 'ar', True))
        self.assertTrue(all(call.args[1].startswith('TX_') for call in self.translate.await_args_list))


    async def test_actual_arabic_greeting_carries_across_finals_with_provenance(self):
        await self.run_events(event('والتابعين. حياكم', request_id='request-1', window_id='window-1'),
                              event('اخواني واخواتي.', request_id='request-2', window_id='window-2'),
                              audit_scope={'room_name': 'test-room'})
        self.assertEqual(self.translated(), ['والتابعين. حياكم اخواني واخواتي.'])
        releases = self.releases()
        self.assertEqual(releases[0]['source_request_ids'], ['request-1', 'request-2'])
        self.assertEqual(releases[0]['source_window_ids'], ['window-1', 'window-2'])
        self.assertEqual(releases[0]['source_caption_ids'],
                         [call.args[0] for call in self.publish.await_args_list])
        self.assertEqual(releases[0]['room_name'], 'test-room')
        self.assertEqual(releases[0]['word_count'], 4)
        self.assertFalse(releases[0]['below_minimum'])


    async def test_complete_sentences_release_without_waiting_for_next_event(self):
        waiting, finish = asyncio.Event(), asyncio.Event()
        async def incoming():
            yield event('هذه جملة أولى. هذه جملة ثانية؟ ثم نتابع')
            waiting.set()
            await finish.wait()
        task = asyncio.create_task(forward_hybrid_sentences(incoming(), self.publish, self.translate, self.ended, 'ar'))
        try:
            await asyncio.wait_for(waiting.wait(), 1)
            self.assertEqual(self.translated(), ['هذه جملة أولى.', 'هذه جملة ثانية؟'])
            finish.set()
            await task
            self.assertEqual(self.translated()[-1], 'ثم نتابع')
            self.assertEqual([release['reason'] for release in self.releases()], ['sentence', 'sentence', 'eof'])
        finally:
            finish.set()
            if not task.done(): task.cancel()
            await asyncio.gather(task, return_exceptions=True)


    async def test_deadline_fires_without_new_stt_events(self):
        waiting, finish, released = asyncio.Event(), asyncio.Event(), asyncio.Event()
        self.translate.side_effect = lambda *_: released.set()
        async def incoming():
            yield event('شكرا.')
            waiting.set()
            await finish.wait()
        task = asyncio.create_task(forward_hybrid_sentences(incoming(), self.publish, self.translate,
                                                           self.ended, 'ar', max_tail_wait_seconds=0.03))
        try:
            await asyncio.wait_for(waiting.wait(), 1)
            await asyncio.wait_for(released.wait(), 1)
            self.assertEqual(self.translated(), ['شكرا.'])
            self.assertEqual(self.releases()[0]['reason'], 'tail_timeout')
            self.assertGreaterEqual(self.releases()[0]['held_seconds'], 0.025)
            self.assertTrue(self.releases()[0]['below_minimum'])
            self.assertEqual(self.releases()[0]['word_count'], 1)
            finish.set()
            await task
            self.translate.assert_awaited_once()
        finally:
            finish.set()
            if not task.done(): task.cancel()
            await asyncio.gather(task, return_exceptions=True)


    async def test_more_finals_do_not_reset_oldest_retained_tail_age(self):
        now = [100.0]
        buffer = _SentenceBuffer(self.translate, {}, 20.0, 4000, lambda: now[0])
        await buffer.append('اليوم نتحدث', 'ar', 'SG_1', None, None, 100.0)
        now[0] = 119.0
        await buffer.append('عن الصبر', 'ar', 'SG_2', None, None, 119.0)
        self.assertEqual(buffer.deadline, 120.0)
        self.assertEqual(buffer.state()['tail_age_seconds'], 19.0)
        now[0] = 120.0
        await buffer.release('tail_timeout')
        self.assertEqual(self.translated(), ['اليوم نتحدث عن الصبر'])
        self.assertEqual(self.releases()[0]['held_seconds'], 20.0)


    async def test_event_ready_at_expired_deadline_does_not_extend_old_tail(self):
        now = [100.0]
        async def incoming():
            yield event('كلمات سابقة')
            now[0] = 120.0
            yield event('ثم كلام جديد.')
        await forward_hybrid_sentences(incoming(), self.publish, self.translate,
                                       self.ended, 'ar', clock=lambda: now[0])
        self.assertEqual(self.translated(), ['كلمات سابقة', 'ثم كلام جديد.'])
        self.assertEqual([release['reason'] for release in self.releases()], ['tail_timeout', 'sentence'])


    async def test_event_arriving_before_deadline_can_complete_old_tail(self):
        now = [100.0]
        async def incoming():
            yield event('كلمات سابقة')
            now[0] = 119.0
            yield event('ثم كلام جديد.')
        await forward_hybrid_sentences(incoming(), self.publish, self.translate,
                                       self.ended, 'ar', clock=lambda: now[0])
        self.assertEqual(self.translated(), ['كلمات سابقة ثم كلام جديد.'])
        self.assertEqual(self.releases()[0]['reason'], 'sentence')


    async def test_new_tail_uses_its_own_source_arrival_after_old_prefix_released(self):
        now = [100.0]
        buffer = _SentenceBuffer(self.translate, {}, 20.0, 4000, lambda: now[0])
        await buffer.append('اليوم نتحدث', 'ar', 'SG_1', 'r1', 'w1', 100.0)
        now[0] = 117.0
        await buffer.append('عن الصبر. وغدا', 'ar', 'SG_2', 'r2', 'w2', 117.0)
        self.assertEqual(self.translated(), ['اليوم نتحدث عن الصبر.'])
        self.assertEqual(buffer.text, 'وغدا')
        self.assertEqual(buffer.deadline, 137.0)
        self.assertEqual(buffer.state()['source_caption_ids'], ['SG_2'])
        self.assertEqual(buffer.state()['tail_age_seconds'], 0)
        now[0] = 130.0
        await buffer.append('يأتي الفرج.', 'ar', 'SG_3', 'r3', 'w3', 130.0)
        self.assertEqual(self.translated()[-1], 'وغدا يأتي الفرج.')
        self.assertEqual(self.releases()[1]['held_seconds'], 13.0)
        self.assertIsNone(buffer.deadline)


    async def test_empty_final_clears_only_preview_and_keeps_confirmed_tail(self):
        await self.run_events(event('حياكم'), event('مسودة مرفوضة.', False), event('  '),
                              event('اخواني واخواتي.'))
        self.assertEqual(self.translated(), ['حياكم اخواني واخواتي.'])
        calls = self.publish.await_args_list
        self.assertEqual(calls[1].args[0], calls[2].args[0])
        self.assertEqual(calls[2].args[1:], ('', 'ar', True))


    async def test_empty_and_unrelated_events_do_not_manufacture_text(self):
        await self.run_events(stt.SpeechEvent(type=stt.SpeechEventType.END_OF_SPEECH),
                              stt.SpeechEvent(type=stt.SpeechEventType.FINAL_TRANSCRIPT),
                              event(''), event(' ', False))
        self.publish.assert_not_awaited()
        self.translate.assert_not_awaited()


    async def test_repeated_real_finals_have_distinct_translation_ids_and_are_retained(self):
        await self.run_events(event('هذا هو المقصود.', request_id='same'), event('هذا هو المقصود.', request_id='same'))
        self.assertEqual(self.translated(), ['هذا هو المقصود.', 'هذا هو المقصود.'])
        self.assertEqual(len({call.args[1] for call in self.translate.await_args_list}), 2)
        self.assertEqual(len({call.args[0] for call in self.publish.await_args_list}), 2)


    async def test_existing_decimal_and_abbreviation_splitter_behavior_is_preserved(self):
        await self.run_events(event('Dr. Ahmad paid 3.14 today. السعر ٣.١٤ اليوم. النهاية؟'))
        self.assertEqual(self.translated(), ['Dr. Ahmad paid 3.14 today.', 'السعر ٣.١٤ اليوم.', 'النهاية؟'])


    async def test_punctuation_continuation_attaches_without_invented_space(self):
        await self.run_events(event('قال المعلم'), event('، إن الصبر جميل'), event('!'))
        self.assertEqual(self.translated(), ['قال المعلم، إن الصبر جميل!'])


    async def test_detected_language_change_flushes_authoritative_tail_only(self):
        await self.run_events(event('كلمات عربية'), event('English draft.', False, language='en'),
                              event('This English statement.', language='en'), source='auto')
        self.assertEqual([(call.args[0], call.args[2]) for call in self.translate.await_args_list],
                         [('كلمات عربية', 'ar'), ('This English statement.', 'en')])
        self.assertEqual([release['reason'] for release in self.releases()], ['language_change', 'sentence'])
        self.assertTrue(self.releases()[0]['below_minimum'])


    async def test_fixed_source_language_keeps_google_detected_changes_from_splitting_tail(self):
        await self.run_events(event('هذه بداية', language='ar'), event('الكلام.', language='en'))
        self.assertEqual(self.translated(), ['هذه بداية الكلام.'])


    async def test_unfinished_preview_flushes_committed_tail_then_reports_missing_final(self):
        with self.assertRaisesRegex(RuntimeError, 'unfinished preview'):
            await self.run_events(event('كلمات مؤكدة'), event('مسودة ليست نهائية.', False))
        self.assertEqual(self.translated(), ['كلمات مؤكدة'])
        self.assertEqual(self.releases()[0]['reason'], 'eof')


    async def test_provider_error_flushes_tail_then_preserves_original_error(self):
        error = ConnectionError('original provider failure')
        with self.assertRaises(ConnectionError) as caught:
            await self.run_events(event('اكتملت هذه الجملة. نص مؤكد'), event('مسودة.', False), error)
        self.assertIs(caught.exception, error)
        self.assertEqual(self.translated(), ['اكتملت هذه الجملة.', 'نص مؤكد'])
        self.assertEqual(self.releases()[-1]['reason'], 'provider_error')


    async def test_failed_error_cleanup_enqueue_does_not_mask_provider_error_or_retry(self):
        error = ConnectionError('original provider failure')
        self.translate.side_effect = ValueError('enqueue failed')
        with self.assertRaises(ConnectionError) as caught:
            await self.run_events(event('نص مؤكد'), error)
        self.assertIs(caught.exception, error)
        self.translate.assert_awaited_once()


    async def test_translation_failure_never_resubmits_consumed_sentence(self):
        self.translate.side_effect = ConnectionError('enqueue ambiguous')
        with self.assertRaisesRegex(ConnectionError, 'enqueue ambiguous'):
            await self.run_events(event('هذه جملة أولى. هذه جملة ثانية.'))
        self.assertEqual(self.translated(), ['هذه جملة أولى.'])


    async def test_cancel_flushes_only_confirmed_tail_and_closes_pending_provider_read(self):
        waiting, closed = asyncio.Event(), asyncio.Event()
        async def incoming():
            try:
                yield event('نص مؤكد')
                yield event('مسودة غير مؤكدة.', False)
                waiting.set()
                await asyncio.Event().wait()
            finally:
                closed.set()
        task = asyncio.create_task(forward_hybrid_sentences(incoming(), self.publish, self.translate, self.ended, 'ar'))
        await asyncio.wait_for(waiting.wait(), 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(closed.is_set())
        self.assertEqual(self.translated(), ['نص مؤكد'])
        self.assertEqual(self.releases()[0]['reason'], 'cancelled')


    async def test_cancel_during_enqueue_never_resubmits_ambiguous_sentence(self):
        entered = asyncio.Event()
        async def translating(*_):
            entered.set()
            await asyncio.Event().wait()
        self.translate.side_effect = translating
        task = asyncio.create_task(forward_hybrid_sentences(stream(event('هذه جملة.')), self.publish,
                                                           self.translate, self.ended, 'ar'))
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.translate.assert_awaited_once()


    async def test_receive_end_with_active_audio_flushes_tail_and_reports_failure(self):
        self.ended.clear()
        with self.assertRaisesRegex(RuntimeError, 'microphone audio was still active'):
            await self.run_events(event('نص مؤكد'))
        self.assertEqual(self.translated(), ['نص مؤكد'])


    async def test_size_guard_keeps_whole_words_and_all_text(self):
        text = 'alpha beta gamma delta epsilon zeta'
        await self.run_events(event(text), max_tail_chars=12)
        self.assertEqual(' '.join(self.translated()), text)
        self.assertTrue(all(len(value) <= 12 for value in self.translated()))
        self.assertEqual(self.releases()[0]['reason'], 'size_limit')
        self.assertTrue(all(release['tail_chars'] < len(text) for release in self.releases()))


    async def test_oversized_single_token_released_intact_instead_of_split_or_held(self):
        token = 'ا' * 101
        await self.run_events(event(token + ' ثم نتابع'), max_tail_chars=10)
        self.assertEqual(self.translated(), [token, 'ثم نتابع'])
        self.assertEqual(self.releases()[0]['reason'], 'size_limit')


    async def test_audit_failure_cannot_interrupt_transcription_or_translation(self):
        self.audit.side_effect = OSError('disk full')
        await self.run_events(event('الحمد لله. ثم نتابع'), event('الكلام.'))
        self.assertEqual(self.translated(), ['الحمد لله. ثم نتابع الكلام.'])
        self.assertEqual(self.publish.await_count, 2)


    async def test_default_three_words_combines_two_word_sentence_and_one_word_sentence(self):
        await self.run_events(event('نَعَمْ حَقًّا.'), event('بالتأكيد.'))
        self.assertEqual(self.translated(), ['نَعَمْ حَقًّا. بالتأكيد.'])
        release = self.releases()[0]
        self.assertEqual(release['word_count'], 3)
        self.assertFalse(release['below_minimum'])
        self.assertEqual(release['reason'], 'sentence')
        self.assertEqual(release['source_caption_ids'], [call.args[0] for call in self.publish.await_args_list])


    async def test_default_three_repeated_one_word_finals_are_preserved_as_one_unit(self):
        waiting, finish = asyncio.Event(), asyncio.Event()
        async def incoming():
            yield event('نعم.', request_id='same')
            yield event('نعم.', request_id='same')
            waiting.set()
            await finish.wait()
            yield event('نعم.', request_id='same')
        task = asyncio.create_task(forward_hybrid_sentences(incoming(), self.publish, self.translate, self.ended, 'ar'))
        try:
            await asyncio.wait_for(waiting.wait(), 1)
            self.translate.assert_not_awaited()
            finish.set()
            await task
            self.assertEqual(self.translated(), ['نعم. نعم. نعم.'])
            self.assertEqual(len(self.releases()[0]['source_caption_ids']), 3)
            self.assertEqual(self.releases()[0]['source_request_ids'], ['same'])
            self.assertEqual(self.releases()[0]['reason'], 'sentence')
            self.assertEqual(len({call.args[0] for call in self.publish.await_args_list}), 3)
        finally:
            finish.set()
            if not task.done(): task.cancel()
            await asyncio.gather(task, return_exceptions=True)


    async def test_unfinished_words_cannot_satisfy_short_complete_prefix_threshold(self):
        now = [100.0]
        buffer = _SentenceBuffer(self.translate, {}, 20.0, 4000, lambda: now[0])
        await buffer.append('نعم.', 'ar', 'SG_1', None, None, 100.0)
        now[0] = 105.0
        await buffer.append('هذا كلام طويل', 'ar', 'SG_2', None, None, 105.0)
        self.translate.assert_not_awaited()
        self.assertEqual(buffer.text, 'نعم. هذا كلام طويل')
        self.assertEqual(buffer.deadline, 120.0)
        now[0] = 110.0
        await buffer.append('ومفيد.', 'ar', 'SG_3', None, None, 110.0)
        self.assertEqual(self.translated(), ['نعم. هذا كلام طويل ومفيد.'])
        self.assertEqual(self.releases()[0]['source_caption_ids'], ['SG_1', 'SG_2', 'SG_3'])
        self.assertEqual(self.releases()[0]['held_seconds'], 10.0)


    async def test_grouped_prefix_preserves_interior_whitespace_and_following_tail_provenance(self):
        await self.run_events(event('نعم.\n\tأكيد.', request_id='r1', window_id='w1'),
                              event('بالطبع!  هذا حديث غير مكتمل', request_id='r2', window_id='w2'),
                              event('بعد.', request_id='r3', window_id='w3'))
        self.assertEqual(self.translated(), ['نعم.\n\tأكيد. بالطبع!', 'هذا حديث غير مكتمل بعد.'])
        source_ids = [call.args[0] for call in self.publish.await_args_list]
        self.assertEqual(self.releases()[0]['source_caption_ids'], source_ids[:2])
        self.assertEqual(self.releases()[1]['source_caption_ids'], source_ids[1:])
        self.assertEqual(self.releases()[0]['source_request_ids'], ['r1', 'r2'])
        self.assertEqual(self.releases()[1]['source_window_ids'], ['w2', 'w3'])
        self.assertEqual(self.releases()[0]['tail_text'], 'هذا حديث غير مكتمل')


    async def test_short_punctuated_tail_is_preserved_on_stop_with_explicit_exception(self):
        await self.run_events(event('شكرا لكم.'))
        self.assertEqual(self.translated(), ['شكرا لكم.'])
        self.assertEqual(self.releases()[0]['reason'], 'eof')
        self.assertEqual(self.releases()[0]['word_count'], 2)
        self.assertTrue(self.releases()[0]['below_minimum'])


    async def test_language_change_releases_short_complete_sentence_without_mixing_languages(self):
        await self.run_events(event('نعم.'), event('This is clear.', language='en'), source='auto')
        self.assertEqual([(call.args[0], call.args[2]) for call in self.translate.await_args_list],
                         [('نعم.', 'ar'), ('This is clear.', 'en')])
        self.assertEqual(self.releases()[0]['reason'], 'language_change')
        self.assertTrue(self.releases()[0]['below_minimum'])


    async def test_injected_minimum_one_can_isolate_original_sentence_buffer_policy(self):
        await self.run_events(event('نعم. بالتأكيد.'), minimum_words=1)
        self.assertEqual(self.translated(), ['نعم.', 'بالتأكيد.'])
        self.assertTrue(all(release['reason'] == 'sentence' for release in self.releases()))
        self.assertTrue(all(not release['below_minimum'] for release in self.releases()))


    async def test_early_asyncio_timeout_does_not_release_before_monotonic_deadline(self):
        now, early_returned = [100.0], [False]
        finish = asyncio.Event()
        self.translate.side_effect = lambda *_: finish.set()
        original_wait = asyncio.wait
        async def incoming():
            yield event('نعم.')
            await finish.wait()
        async def early_wait(tasks, *, timeout=None):
            if timeout is None:
                return await original_wait(tasks)
            if not early_returned[0]:
                early_returned[0] = True
                now[0] = 119.99
                return set(), set(tasks)
            # The wake at119.99 must leave the20-second tail unsubmitted.
            self.translate.assert_not_awaited()
            now[0] = 120.0
            return set(), set(tasks)
        with patch('gemini_stt.buffer.asyncio.wait', side_effect=early_wait):
            await forward_hybrid_sentences(incoming(), self.publish, self.translate,
                                           self.ended, 'ar', clock=lambda: now[0])
        self.assertEqual(self.translated(), ['نعم.'])
        self.assertEqual(self.releases()[0]['held_seconds'], 20.0)
        self.assertEqual(self.releases()[0]['reason'], 'tail_timeout')


    async def test_policy_values_are_explicit_and_invalid_bounds_rejected(self):
        await self.run_events()
        policy = self.audit.call_args_list[0].kwargs['policy']
        self.assertEqual(policy['max_tail_wait_seconds'], 20.0)
        self.assertEqual(policy['max_tail_chars'], 4000)
        self.assertEqual(policy['minimum_words'], 3)
        self.assertEqual(policy['revision'], 'confirmed_sentences_min3_with_tail_v2')
        self.assertTrue(policy['confirmed_finals_only'])
        for options in ({'max_tail_chars': 0}, {'max_tail_chars': True},
                        {'max_tail_wait_seconds': 0}, {'max_tail_wait_seconds': True},
                        {'max_tail_wait_seconds': float('nan')}, {'minimum_words': 0},
                        {'minimum_words': True}, {'minimum_words': 1.5}):
            with self.assertRaises(ValueError):
                await self.run_events(**options)
