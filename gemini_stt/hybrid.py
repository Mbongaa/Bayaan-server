"""Fixed hybrid policy: Google draft punctuation or 16 seconds; no pauses.

Draft punctuation requests an audio boundary. Only Google's authoritative final
is committed. Captured audio after that boundary stays queued for the next one.
"""
from __future__ import annotations
import asyncio
from dataclasses import dataclass
import logging
from google.genai import types
from livekit.agents import stt
from . import recovery as current
from .compat import SpeechData
from .sentences import split_complete_sentences
from .eof import FILE_MODELS, EOF_REQUEST_TIMEOUT_SECONDS, transcribe_eof
logger = logging.getLogger('bayaan.gemini.segmentation')

@dataclass(frozen=True)
class _PunctuationBoundary:
    window_id: str
    attempt: int
    requested_end_sample: int
    boundary_reason: str = 'google_interim_punctuation'


async def _emit_window(stream, kind, text, language, attempt):
    window = attempt.window
    start = window.chunks[0].start_sample
    end = window.chunks[-1].start_sample + window.chunks[-1].samples
    forced = getattr(window, 'forced', False)
    reason = getattr(window, 'boundary_reason', None)
    if reason is None and kind == stt.SpeechEventType.FINAL_TRANSCRIPT:
        reason = 'duration_limit' if forced else 'eof'
    await stream._output.put(stt.SpeechEvent(
        type=kind, request_id=window.id,
        alternatives=[SpeechData(
            text=text, language=language or stream._provider.language,
            start_time=start / 16000, end_time=end / 16000,
            metadata={'audio_window_start_sample': start, 'audio_window_end_sample': end,
                      'window_id': window.id, 'recovery_attempt': attempt.number,
                      'forced_boundary': forced, 'voice_end_offset': attempt.voice_end_offset,
                      'segmentation_mode': stream.segmentation_mode, 'boundary_reason': reason,
                      'punctuation_requested_end_sample': getattr(window, 'punctuation_requested_end_sample', None),
                      'finalization_source': getattr(attempt, 'finalization_source', 'live'),
                      'finalization_model': getattr(attempt, 'finalization_model', stream._provider.model),
                      },
        )]))
    if kind == stt.SpeechEventType.FINAL_TRANSCRIPT:
        logger.info('Segmentation final mode=%s reason=%s window=%s attempt=%s source_samples=[%s,%s) request_end_sample=%s',
                    stream.segmentation_mode, reason, window.id, attempt.number, start, end,
                    getattr(window, 'punctuation_requested_end_sample', None))


class _PunctuationStream(current.ResilientGeminiStream):
    segmentation_mode = 'PUNCTUATION'

    def __init__(self, provider):
        super().__init__(provider)
        self.segmentation_mode = provider.segmentation_mode
        self.stats['accounted_idle_zero_samples'] = 0

    def _append(self, window, chunk):
        # Retain every source sample, including zeros; no energy or duration cut.
        window.chunks.append(chunk)
        window.samples += chunk.samples
        return chunk

    async def _next_input(self):
        return await self._input.get()

    async def _first_window(self):
        while True:
            item = await self._next_input()
            if isinstance(item, Exception):
                raise item
            if isinstance(item, _PunctuationBoundary):
                continue  # Delayed marker from an already finalized attempt.
            if item is current._END:
                self._eof = True
                return None
            if not any(item.pcm):
                # Exact digital zeros contain no encoded speech. Account for
                # them without opening an idle activity that may never receive
                # Google's authoritative empty final. Inside an active window
                # zeros are retained/sent normally and never end that window.
                self._held_bytes -= len(item.pcm)
                self.stats['accounted_idle_zero_samples'] += item.samples
                continue
            self._sequence += 1
            window = current._Window(f'{self._stream_id}:{self._sequence}')
            self._append(window, item)
            return window

    async def _emit(self, kind, text, language, attempt):
        completed = split_complete_sentences(text)[0] if kind == stt.SpeechEventType.INTERIM_TRANSCRIPT else []
        punctuation_ready = bool(completed)
        if (kind == stt.SpeechEventType.INTERIM_TRANSCRIPT and not attempt.end_sent
                and not attempt.window.sealed and not getattr(attempt, 'punctuation_requested', False)
                and punctuation_ready):
            attempt.punctuation_requested = True
            # A marker wakes an idle input.get immediately. FIFO order includes
            # captured backlog before this marker and retains later frames for
            # the next activity. Only the sender may seal/send activity_end.
            marker = _PunctuationBoundary(attempt.window.id, attempt.number, self._punctuation_end_sample())
            self._input.put_nowait(marker)
            logger.info('Punctuation boundary requested window=%s attempt=%s captured_end_sample=%s',
                        marker.window_id, marker.attempt, marker.requested_end_sample)
        await _emit_window(self, kind, text, language, attempt)

    def _punctuation_end_sample(self):
        return self._received_samples

    def _accept_punctuation_marker(self, attempt, marker):
        return marker.boundary_reason == 'google_interim_punctuation'

    async def _attempt_window(self, window, number, begin_budget, healthy):
        attempt = self._attempt = current._Attempt(window, number, asyncio.get_running_loop().create_future())
        begin_budget()
        await asyncio.wait_for(self._guard(self._session.send_realtime_input(activity_start=types.ActivityStart())), self._opts.connect_timeout)
        for chunk in window.chunks:
            begin_budget()
            await self._send_chunk(chunk, replay=number > 0)
        if not window.sealed:
            healthy()
        while not window.sealed:
            item = await self._guard(self._next_input())
            if isinstance(item, Exception):
                raise item
            if isinstance(item, _PunctuationBoundary):
                if item.window_id != window.id or item.attempt != number:
                    continue
                if not self._accept_punctuation_marker(attempt, item):
                    continue
                if item.requested_end_sample <= window.chunks[0].start_sample:
                    continue
                actual_end = window.chunks[-1].start_sample + window.chunks[-1].samples
                if actual_end != item.requested_end_sample:
                    raise current.ProtocolError('Punctuation boundary did not match the captured source sample order')
                window.sealed = True
                window.boundary_reason = item.boundary_reason
                window.punctuation_requested_end_sample = item.requested_end_sample
                break
            if item is current._END:
                self._eof = True
                window.sealed = True
                window.boundary_reason = 'eof'
                break
            admitted = self._append(window, item)
            begin_budget()
            await self._send_chunk(admitted, replay=False)
            if not window.sealed:
                healthy()
        begin_budget()
        attempt.end_sent = True
        await asyncio.wait_for(self._guard(self._session.send_realtime_input(activity_end=types.ActivityEnd())), self._opts.connect_timeout)
        try:
            text, language = await asyncio.wait_for(
                self._guard(asyncio.shield(attempt.done)), self._opts.finalize_timeout)
        except asyncio.TimeoutError:
            model = FILE_MODELS.get(self._provider.model)
            if not (self._input_ended and self._eof and getattr(window, 'boundary_reason', None) == 'eof' and model):
                raise current.RecoveryError('Google did not provide an authoritative final for the punctuation-requested activity; audio retained for retry') from None
            # End the live receiver before a second endpoint can decide the same
            # uncommitted audio. A final that already arrived still wins.
            await self._disconnect()
            if attempt.done.done() and not attempt.done.cancelled():
                text, language = attempt.done.result()
            else:
                self.stats['eof_file_requests'] = self.stats.get('eof_file_requests', 0) + 1
                try:
                    text = await asyncio.wait_for(transcribe_eof(
                        self._client, model=model,
                        pcm=b''.join(chunk.pcm for chunk in window.chunks),
                        audio_config=self._provider._config.input_audio_transcription,
                    ), EOF_REQUEST_TIMEOUT_SECONDS)
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    # One bounded file attempt; uncertain responses never commit
                    # source audio or trigger repeated ambiguous requests.
                    raise current.ProtocolError(
                        'Google could not confirm the final EOF audio through its transcription endpoint; review this recording'
                    ) from error
                language = self._provider.language
                attempt.finalization_source = 'eof_file_transcription'
                attempt.finalization_model = model
                self.stats['eof_file_finalized_windows'] = self.stats.get('eof_file_finalized_windows', 0) + 1
                if not text:
                    self.stats['eof_file_empty_windows'] = self.stats.get('eof_file_empty_windows', 0) + 1
                logger.info('Google EOF transcription confirmed window=%s model=%s source_samples=%s empty=%s',
                            window.id, model, window.samples, not bool(text))
        # An authoritative empty final still resolves a prior speculative preview.
        # Forward that provider event so the caller can clear it without GPT work.
        await self._emit(stt.SpeechEventType.FINAL_TRANSCRIPT, text, language, attempt)
        self.stats['committed_windows'] += 1
        self._held_bytes -= window.samples * 2
        self._attempt = None



class GeminiHybridSTT(current.ResilientGeminiSTT):
    """STT-compatible stream factory with the chosen lab policy fixed in code."""
    segmentation_mode = 'HYBRID_16S'
    boundary_revision = 'punctuation_max16_no_pauses_v2'
    max_segment_seconds = 16.0
    punctuation_stability = False
    earlier_translation = False

    def __init__(self, *, transcription_mode='VERBATIM', **options):
        if transcription_mode != 'VERBATIM':
            raise ValueError('The promoted hybrid mode uses VERBATIM transcription')
        super().__init__(transcription_mode=transcription_mode, **options)

    def stream(self):
        stream = _HybridStream(self)
        self._streams.add(stream)
        stream._task.add_done_callback(lambda _: self._streams.discard(stream))
        return stream


class _HybridStream(_PunctuationStream):
    def __init__(self, provider):
        self._carry = None
        self._max_samples = int(provider.max_segment_seconds * 16000)
        super().__init__(provider)

    async def _next_input(self):
        if self._carry is not None:
            chunk, self._carry = self._carry, None
            return chunk
        return await super()._next_input()

    def _append(self, window, chunk):
        remaining = self._max_samples - window.samples
        if remaining <= 0:
            raise current.ProtocolError('Cannot append audio to a sealed 16-second activity')
        if chunk.samples > remaining:
            if self._carry is not None:
                raise current.ProtocolError('Unconsumed audio remainder before a new frame')
            self._carry = current._Chunk(chunk.pcm[remaining * 2:], chunk.start_sample + remaining)
            chunk = current._Chunk(chunk.pcm[:remaining * 2], chunk.start_sample)
        super()._append(window, chunk)
        if window.samples == self._max_samples:
            window.sealed = window.forced = True
            window.boundary_reason = 'duration_limit'
            self.stats['forced_boundaries'] += 1
            message = 'The audio activity reached 16 seconds; check words around this forced boundary.'
            logger.warning('%s window=%s source_samples=[%s,%s)', message, window.id,
                           window.chunks[0].start_sample, chunk.start_sample + chunk.samples)
            self._provider.emit('warning', {'code': 'forced_boundary', 'message': message,
                                           'window_id': window.id, 'audio_seconds': self._provider.max_segment_seconds})
        return chunk

    async def _run(self):
        try:
            await super()._run()
        finally:
            self._carry = None
