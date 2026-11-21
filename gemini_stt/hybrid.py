"""Fixed hybrid policy: Google draft punctuation or 16 seconds; no pauses.

Draft punctuation requests an audio boundary. Only Google's authoritative final
is committed. Captured audio after that boundary stays queued for the next one.
"""
from __future__ import annotations
import array
import asyncio
from collections import deque
from dataclasses import dataclass
import logging
from google.genai import types
from livekit.agents import stt
from . import recovery as current
from .compat import SpeechData
from .sentences import split_complete_sentences
from .eof import FILE_MODELS, EOF_REQUEST_TIMEOUT_SECONDS, transcribe_window
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

    def _edge_padding_pcm(self):
        return bytes(int(getattr(self._provider, 'edge_padding_seconds', 0.0) * 16000) * 2)

    async def _send_edge_padding(self):
        """Synthetic silence around an activity's real audio (never a captured sample).

        Google drops the first or last word of an activity whose speech starts or ends
        right at its edge. Silence on both sides lets those words be recognised. The
        padding counts towards the connection's audio for rotation.
        """
        padding = self._edge_padding_pcm()
        if padding:
            await asyncio.wait_for(self._guard(self._session.send_realtime_input(
                audio=types.Blob(data=padding, mime_type='audio/pcm;rate=16000'))), self._opts.connect_timeout)
            self._connection_samples += len(padding) // 2
            self.stats['edge_padding_samples'] = self.stats.get('edge_padding_samples', 0) + len(padding) // 2

    def _accept_punctuation_marker(self, attempt, marker):
        return marker.boundary_reason == 'google_interim_punctuation'

    async def _attempt_window(self, window, number, begin_budget, healthy):
        attempt = self._attempt = current._Attempt(window, number, asyncio.get_running_loop().create_future())
        begin_budget()
        await asyncio.wait_for(self._guard(self._session.send_realtime_input(activity_start=types.ActivityStart())), self._opts.connect_timeout)
        await self._send_edge_padding()
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
        await self._send_edge_padding()
        attempt.end_sent = True
        await asyncio.wait_for(self._guard(self._session.send_realtime_input(activity_end=types.ActivityEnd())), self._opts.connect_timeout)
        try:
            text, language = await asyncio.wait_for(
                self._guard(asyncio.shield(attempt.done)), self._opts.finalize_timeout)
        except asyncio.TimeoutError:
            model = FILE_MODELS.get(self._provider.model)
            if not (window.sealed and attempt.end_sent and model
                    and 0 < window.samples <= self._provider.max_segment_seconds * 16000):
                raise current.RecoveryError('Google did not provide an authoritative final for the punctuation-requested activity; audio retained for retry') from None
            reason = getattr(window, 'boundary_reason', None)
            is_eof = reason == 'eof' and self._input_ended and self._eof
            # End the live receiver before a second endpoint can decide the same
            # uncommitted audio. A final that already arrived still wins. The
            # next activity opens a fresh socket while captured audio stays FIFO.
            await self._disconnect()
            if attempt.done.done() and not attempt.done.cancelled():
                text, language = attempt.done.result()
            else:
                self.stats['sealed_file_requests'] = self.stats.get('sealed_file_requests', 0) + 1
                if is_eof:
                    self.stats['eof_file_requests'] = self.stats.get('eof_file_requests', 0) + 1
                logger.warning('Google live final missing; confirming retained activity through file transcription window=%s reason=%s audio_seconds=%.2f',
                               window.id, reason, window.samples / 16000)
                self._connect_phase = 'file_transcription'
                try:
                    text = await asyncio.wait_for(transcribe_window(
                        self._client, model=model,
                        pcm=self._edge_padding_pcm() + b''.join(chunk.pcm for chunk in window.chunks) + self._edge_padding_pcm(),
                        audio_config=self._provider._config.input_audio_transcription,
                    ), EOF_REQUEST_TIMEOUT_SECONDS)
                except asyncio.CancelledError:
                    raise
                except ValueError as error:
                    # A completed ambiguous/blocked/truncated response cannot
                    # confirm audio. Transport/timeouts instead propagate to the
                    # existing bounded replay loop: nothing was published, and
                    # the cancelled request is awaited before another attempt.
                    raise current.ProtocolError(
                        'Google could not confirm the retained audio activity through its transcription endpoint; review this recording'
                    ) from error
                language = self._provider.language
                attempt.finalization_source = 'eof_file_transcription' if is_eof else 'activity_file_transcription'
                attempt.finalization_model = model
                self.stats['sealed_file_finalized_windows'] = self.stats.get('sealed_file_finalized_windows', 0) + 1
                if is_eof:
                    self.stats['eof_file_finalized_windows'] = self.stats.get('eof_file_finalized_windows', 0) + 1
                if not text:
                    self.stats['sealed_file_empty_windows'] = self.stats.get('sealed_file_empty_windows', 0) + 1
                    if is_eof:
                        self.stats['eof_file_empty_windows'] = self.stats.get('eof_file_empty_windows', 0) + 1
                logger.info('Google sealed activity transcription confirmed window=%s reason=%s model=%s source_samples=%s empty=%s',
                            window.id, reason, model, window.samples, not bool(text))
        # An authoritative empty final still resolves a prior speculative preview.
        # Forward that provider event so the caller can clear it without GPT work.
        await self._emit(stt.SpeechEventType.FINAL_TRANSCRIPT, text, language, attempt)
        self.stats['committed_windows'] += 1
        self._held_bytes -= window.samples * 2
        self._attempt = None



class GeminiHybridSTT(current.ResilientGeminiSTT):
    """STT-compatible stream factory with the chosen lab policy fixed in code.

    By default an activity ends at Google's first draft punctuation or at 16 s.
    ``pause_cut_seconds`` switches to cutting at the first real pause of that length
    once the activity holds ``min_cut_seconds`` of audio (still 16 s at most), so cuts
    lie in silence instead of wherever the draft punctuation happens to arrive.
    ``edge_padding_seconds`` sends that much synthetic silence before and after each
    activity's real audio, because Google drops words spoken right at an activity edge.
    All default off; ``gemini_provider_options`` enables them per language.
    """
    segmentation_mode = 'HYBRID_16S'
    boundary_revision = 'punctuation_max16_no_pauses_v2'
    max_segment_seconds = 16.0
    punctuation_stability = False
    earlier_translation = False

    def __init__(self, *, transcription_mode='VERBATIM', pause_cut_seconds=0.0, min_cut_seconds=0.0,
                 edge_padding_seconds=0.0, **options):
        if transcription_mode != 'VERBATIM':
            raise ValueError('The promoted hybrid mode uses VERBATIM transcription')
        for name, value, limit in (('pause_cut_seconds', pause_cut_seconds, 2),
                                   ('min_cut_seconds', min_cut_seconds, self.max_segment_seconds - 1),
                                   ('edge_padding_seconds', edge_padding_seconds, 2)):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= limit:
                raise ValueError(f'{name} must be between 0 and {limit:g}')
        if min_cut_seconds and not pause_cut_seconds:
            raise ValueError('min_cut_seconds applies to pause cuts only')
        super().__init__(transcription_mode=transcription_mode, **options)
        self.pause_cut_seconds = float(pause_cut_seconds)
        self.min_cut_seconds = float(min_cut_seconds)
        self.edge_padding_seconds = float(edge_padding_seconds)
        if self.pause_cut_seconds or self.edge_padding_seconds:
            policy = (f'pause{self.pause_cut_seconds * 1000:g}ms_min{self.min_cut_seconds:g}'
                      if self.pause_cut_seconds else 'punctuation')
            pad = f'_pad{self.edge_padding_seconds:g}' if self.edge_padding_seconds else ''
            self.boundary_revision = f'{policy}{pad}_max16_v1'

    def stream(self):
        stream = _HybridStream(self)
        self._streams.add(stream)
        stream._task.add_done_callback(lambda _: self._streams.discard(stream))
        return stream


PAUSE_QUIET_RATIO = 0.2      # a 10 ms frame is quiet below this share of the recent speech level
PAUSE_QUIET_FLOOR = 60.0     # ... or below this absolute RMS
_PAUSE_LEVEL_FRAMES = 300    # recent frames used for the speech level (3 s of 10 ms frames)


class _HybridStream(_PunctuationStream):
    def __init__(self, provider):
        self._carry = None
        self._max_samples = int(provider.max_segment_seconds * 16000)
        self._pause_cut_samples = int(getattr(provider, 'pause_cut_seconds', 0.0) * 16000)
        self._min_cut_samples = int(getattr(provider, 'min_cut_seconds', 0.0) * 16000)
        self._quiet_samples = 0
        self._levels = deque(maxlen=_PAUSE_LEVEL_FRAMES)
        super().__init__(provider)

    def _accept_punctuation_marker(self, attempt, marker):
        expected = 'pause' if self._pause_cut_samples else 'google_interim_punctuation'
        return marker.boundary_reason == expected

    async def _emit(self, kind, text, language, attempt):
        if self._pause_cut_samples:
            # Pause mode ends activities only at real pauses (push_frame) or at 16 s.
            return await _emit_window(self, kind, text, language, attempt)
        return await super()._emit(kind, text, language, attempt)

    def push_frame(self, frame):
        super().push_frame(frame)
        if not self._pause_cut_samples:
            return
        samples = array.array('h', frame.data.tobytes())
        if not samples:
            return
        level = (sum(s * s for s in samples) / len(samples)) ** 0.5
        self._levels.append(level)
        speech = sorted(self._levels)[int(len(self._levels) * 0.7)]
        quiet = level < max(PAUSE_QUIET_FLOOR, PAUSE_QUIET_RATIO * speech)
        self._quiet_samples = self._quiet_samples + len(samples) if quiet else 0
        attempt = self._attempt
        if (attempt is not None and not attempt.end_sent and not attempt.window.sealed
                and attempt.window.chunks and not getattr(attempt, 'punctuation_requested', False)
                and self._received_samples - attempt.window.chunks[0].start_sample >= self._min_cut_samples
                and self._quiet_samples >= self._pause_cut_samples):
            # A real pause after the minimum length: end the activity right after this
            # captured frame, so FIFO sample order holds and the cut lies in silence.
            attempt.punctuation_requested = True
            self._input.put_nowait(_PunctuationBoundary(attempt.window.id, attempt.number,
                                                        self._received_samples, 'pause'))
            logger.info('Pause boundary requested window=%s attempt=%s window_seconds=%.2f',
                        attempt.window.id, attempt.number,
                        (self._received_samples - attempt.window.chunks[0].start_sample) / 16000)

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
