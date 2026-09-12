"""Translate complete confirmed sentences while carrying a hybrid final's tail.

This text-only stage preserves raw Google source captions. Complete sentences
release once they contain at least three words. An unfinished tail is retained
for the next final, with the selected lab policy's 20-second text deadline.
This deadline is independent of the provider's 16-second audio boundary.
"""
import asyncio
from dataclasses import dataclass, replace
import math
import time
from uuid import uuid4

from livekit.agents import stt

from . import audit as audit_log
from .text import word_count
from .sentences import split_complete_sentences


HYBRID_BUFFER_REVISION = 'confirmed_sentences_min3_with_tail_v2'
DEFAULT_MAX_TAIL_WAIT_SECONDS = 20.0
DEFAULT_MAX_TAIL_CHARS = 4000
MINIMUM_SENTENCE_WORDS = 3
# These writing systems do not reliably separate words with whitespace. Release
# their complete punctuated sentence as one unit, rather than calling characters
# words or forcing three full sentences into a single translation request.
_UNSPACED_LANGUAGE_BASES = frozenset({'zh', 'cmn', 'yue', 'ja', 'th', 'km', 'my', 'lo'})
_NO_SPACE_BEFORE = frozenset('.,!?;:،؛؟。！？｡।॥۔…%٪)]}»”’」』】》〉〕）］｝')
_NO_SPACE_AFTER = frozenset('([{«“‘「『【《〈〔（［｛')


def _audit(event, scope, **fields):
    # Keep this guard even though audit_log.emit is also defensive: a custom
    # logger/diagnostic hook must never affect transcription or enqueueing.
    try:
        audit_log.emit(event, **{**(scope or {}), **fields})
    except Exception:
        pass


@dataclass
class _Fragment:
    text: str
    arrived: float
    source_caption_id: str
    request_id: str | None
    window_id: str | None


def _ids(fragments, attribute):
    return list(dict.fromkeys(getattr(fragment, attribute) for fragment in fragments
                              if fragment.text.strip() and getattr(fragment, attribute)))


class _SentenceBuffer:
    def __init__(self, translate, scope, max_wait, max_chars, clock, minimum_words=MINIMUM_SENTENCE_WORDS):
        self.translate, self.scope = translate, scope
        self.max_wait, self.max_chars, self.clock = max_wait, max_chars, clock
        self.minimum_words = minimum_words
        self.fragments = []
        self.language = None

    @property
    def text(self):
        return ''.join(fragment.text for fragment in self.fragments)

    @property
    def deadline(self):
        return self.fragments[0].arrived + self.max_wait if self.fragments and self.max_wait is not None else None

    def state(self):
        return dict(tail_text=self.text, tail_chars=sum(len(f.text) for f in self.fragments),
                    tail_age_seconds=max(0.0, self.clock() - self.fragments[0].arrived)
                    if self.fragments else 0.0,
                    source_caption_ids=_ids(self.fragments, 'source_caption_id'),
                    source_request_ids=_ids(self.fragments, 'request_id'),
                    source_window_ids=_ids(self.fragments, 'window_id'))

    def update(self):
        _audit('translation_buffer_updated', self.scope, language=self.language, **self.state())

    def _take(self, length):
        taken = []
        while length and self.fragments:
            first = self.fragments[0]
            size = min(length, len(first.text))
            taken.append(replace(first, text=first.text[:size]))
            if size == len(first.text):
                self.fragments.pop(0)
            else:
                self.fragments[0] = replace(first, text=first.text[size:])
            length -= size
        return taken

    async def release(self, reason, length=None):
        remaining = len(self.text) if length is None else length
        while self.fragments and remaining > 0:
            value = self.text[:remaining]
            end = len(value)
            before = len(self.text)
            await self._release_once(reason, end)
            remaining -= before - len(self.text)

    async def _release_once(self, reason, length):
        if not self.fragments:
            return
        value = self.text
        length = len(value) if length is None else length
        text, language = value[:length].strip(), self.language
        taken = self._take(length)
        remainder = self.text
        self._take(len(remainder) - len(remainder.lstrip()))
        if not self.fragments:
            self.language = None
        if not text:
            self.update()
            return
        segment_id = 'TX_' + uuid4().hex
        state = self.state()
        # Consume before the callback. Once enqueueing has been attempted, an
        # exception cannot safely be interpreted as permission to resubmit it.
        _audit('translation_buffer_emitted', self.scope, segment_id=segment_id,
               text=text, language=language, reason=reason,
               word_count=word_count(text), below_minimum=word_count(text) < self.minimum_words,
               source_chars=len(text),
               source_caption_ids=_ids(taken, 'source_caption_id'),
               source_request_ids=_ids(taken, 'request_id'),
               source_window_ids=_ids(taken, 'window_id'),
               held_seconds=max(0.0, self.clock() - min(f.arrived for f in taken)),
               tail_text=state['tail_text'], tail_chars=state['tail_chars'],
               tail_age_seconds=state['tail_age_seconds'], enqueue_status='attempted')
        self.update()
        try:
            await self.translate(text, segment_id, language)
        except BaseException as error:
            _audit('translation_buffer_enqueue_failed', self.scope, segment_id=segment_id,
                   reason=reason, error_type=type(error).__name__)
            raise

    async def append(self, text, language, source_id, request_id, window_id, arrived):
        if self.fragments and language != self.language:
            await self.release('language_change')
        value = text.strip()
        previous = self.text
        separator = (' ' if previous and value and previous[-1] not in _NO_SPACE_AFTER
                     and value[0] not in _NO_SPACE_BEFORE else '')
        if str(language).replace('_', '-').split('-')[0].lower() in _UNSPACED_LANGUAGE_BASES:
            separator = ''
        self.language = language
        self.fragments.append(_Fragment(separator + value, arrived, source_id, request_id, window_id))
        self.update()
        # Locate boundaries in the original text so grouped sentences preserve
        # tabs/newlines/interior spacing and the matching provenance spans.
        value = self.text
        sentences, _ = split_complete_sentences(value)
        cursor, consumed, completed_words = 0, 0, 0
        for sentence in sentences:
            cursor = value.index(sentence, cursor) + len(sentence)
            completed_words += word_count(sentence)
            if completed_words >= self.minimum_words:
                await self.release('sentence', cursor - consumed)
                consumed = len(value) - len(self.text)
                completed_words = 0
        # A short complete prefix stays held with the unfinished tail. Words in
        # that unfinished tail cannot satisfy the sentence release threshold.
        # If punctuation released an older prefix, the remaining first fragment
        # now supplies its own arrival time, not that released prefix's age.
        if self.deadline is not None and self.clock() >= self.deadline:
            await self.release('tail_timeout')
        while len(self.text) >= self.max_chars:
            value = self.text
            # Prefer a whole-word prefix within the limit. A single oversized
            # token is released intact rather than split or retained forever.
            end = max((i for i in range(1, min(len(value), self.max_chars) + 1)
                       if i == len(value) or value[i].isspace()), default=0)
            if not end:
                end = next((i for i in range(self.max_chars, len(value))
                            if value[i].isspace()), len(value))
            await self.release('size_limit', end)


async def forward_hybrid_sentences(events, publish, translate, input_ended, source_language,
                                   *, audit_scope=None,
                                   max_tail_wait_seconds=DEFAULT_MAX_TAIL_WAIT_SECONDS,
                                   max_tail_chars=DEFAULT_MAX_TAIL_CHARS,
                                   minimum_words=None,
                                   clock=time.monotonic):
    """Publish raw drafts/finals; only confirmed sentence text enters translation.

    Committed tails flush on the text deadline, a size safeguard, EOF, detected
    language changes, provider errors and cancellation. A provider failure stays
    the primary exception even if its final cleanup enqueue also fails.
    """
    if isinstance(max_tail_wait_seconds, bool) or not isinstance(max_tail_wait_seconds, (int, float)) or not math.isfinite(max_tail_wait_seconds) or max_tail_wait_seconds <= 0:
        raise ValueError('max_tail_wait_seconds must be a finite positive number')
    if isinstance(max_tail_chars, bool) or not isinstance(max_tail_chars, int) or max_tail_chars < 1:
        raise ValueError('max_tail_chars must be a positive integer')
    if minimum_words is None:
        base = source_language.replace('_', '-').split('-')[0].lower()
        minimum_words = 1 if base in _UNSPACED_LANGUAGE_BASES else MINIMUM_SENTENCE_WORDS
    if isinstance(minimum_words, bool) or not isinstance(minimum_words, int) or minimum_words < 1:
        raise ValueError('minimum_words must be a positive integer')
    buffer = _SentenceBuffer(translate, audit_scope, max_tail_wait_seconds, max_tail_chars, clock,
                             minimum_words)
    _audit('translation_buffer_started', audit_scope, policy={
        'revision': HYBRID_BUFFER_REVISION,
        'confirmed_finals_only': True, 'earlier_translation': False,
        'character_count': 'unicode_code_points_including_spaces_and_punctuation',
        'max_tail_wait_seconds': max_tail_wait_seconds, 'max_tail_chars': max_tail_chars,
        'minimum_words': minimum_words,
        'normal_release': 'sentence',
        'retains_under_limit_tail': False,
        'timeout_basis': 'oldest_retained_final_arrival',
        'audio_boundaries_unchanged': True,
        'oversized_single_token': 'release_intact',
    })
    iterator, next_event, pending_id = events.__aiter__(), None, None

    async def receive():
        return await iterator.__anext__(), clock()

    async def flush_preserving_error(reason):
        try:
            await buffer.release(reason)
        except BaseException as error:
            _audit('translation_buffer_cleanup_failed', audit_scope, reason=reason,
                   error_type=type(error).__name__, **buffer.state())

    try:
        while True:
            if next_event is None:
                next_event = asyncio.create_task(receive())
            deadline = buffer.deadline
            timeout = None if deadline is None else max(0.0, deadline - clock())
            done, _ = await asyncio.wait([next_event], timeout=timeout)
            if not done:
                # Windows timer resolution can wake asyncio.wait a tick early.
                # Re-check the actual policy clock before releasing held text.
                if buffer.deadline is not None and clock() >= buffer.deadline:
                    await buffer.release('tail_timeout')
                continue
            try:
                event, arrived = next_event.result()
            except StopAsyncIteration:
                await buffer.release('eof')
                break
            except asyncio.CancelledError:
                raise
            except Exception:
                await flush_preserving_error('provider_error')
                raise
            next_event = None
            # A late provider event must not extend already expired text. An
            # event received before the deadline can still complete that text.
            if deadline is not None and arrived >= deadline:
                await buffer.release('tail_timeout')
            if event.type not in (stt.SpeechEventType.INTERIM_TRANSCRIPT, stt.SpeechEventType.FINAL_TRANSCRIPT) or not event.alternatives:
                continue
            alternative = event.alternatives[0]
            text = alternative.text
            language = str(alternative.language or source_language) if source_language == 'auto' else source_language
            if not text.strip():
                if event.type == stt.SpeechEventType.FINAL_TRANSCRIPT and pending_id is not None:
                    await publish(pending_id, '', language, True)
                    pending_id = None
                continue
            segment_id = pending_id or 'SG_' + uuid4().hex
            if event.type == stt.SpeechEventType.INTERIM_TRANSCRIPT:
                pending_id = segment_id
                await publish(segment_id, text, language, False)
            else:
                await publish(segment_id, text, language, True)
                pending_id = None
                metadata = getattr(alternative, 'metadata', None)
                window_id = metadata.get('window_id') if isinstance(metadata, dict) else None
                request_id = getattr(event, 'request_id', None)
                await buffer.append(text, language, segment_id,
                                    request_id if isinstance(request_id, str) else None,
                                    window_id if isinstance(window_id, str) else None, arrived)
    except asyncio.CancelledError:
        await flush_preserving_error('cancelled')
        raise
    finally:
        if next_event is not None:
            if not next_event.done():
                next_event.cancel()
            await asyncio.gather(next_event, return_exceptions=True)
    if pending_id is not None:
        raise RuntimeError('Gemini stream ended with an unfinished preview; the final speech tail may be missing')
    if not input_ended.is_set():
        raise RuntimeError('Gemini receive stream stopped while microphone audio was still active')
