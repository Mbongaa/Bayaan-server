"""Replayable Gemini Live transport for the promoted hybrid transcription path.

The unit of recovery is a manual audio activity, never a text overlap. An activity's
PCM stays in memory until its authoritative ``input_transcription`` arrives. Only
one activity is in flight, and its final is published once. A failed attempt is
discarded and the uncommitted PCM is replayed through a fresh Google connection.

Verified with google-genai 2.22 / LiveKit Agents 1.8 on 2026-09-10: this model emits
input_transcription after activity_end, WITHOUT ``finished`` or ``turn_complete``.
generation_complete is independent and is never a transcription checkpoint.
See https://ai.google.dev/gemini-api/docs/live-api/live-transcribe and
https://ai.google.dev/api/live. This is an observed model contract, not a Google
exactly-once guarantee. No utterance timestamps or fuzzy text dedup are assumed.
"""
from __future__ import annotations

import asyncio
from concurrent.futures import Future, InvalidStateError
from dataclasses import dataclass, field
import json
import logging
import math
import threading
import time
from typing import Any, Callable
from uuid import uuid4

from google import genai
from google.genai import types
from google.oauth2 import service_account
from google.auth.transport.requests import Request as GoogleAuthRequest
from livekit import rtc
from livekit.agents import stt

logger = logging.getLogger("bayaan.gemini.recovery")
_END = object()


class RecoveryError(RuntimeError):
    """A sanitized failure; callers should display it and stop this track."""


class ProtocolError(RecoveryError):
    """Google did not follow the tested one-activity/one-final contract."""


class TransportCompatibilityError(RecoveryError):
    """The pinned SDK's local transport workaround requires a fresh review."""


def _enable_happy_eyeballs(client):
    """Per-client workaround for the pinned Gen AI SDK's websocket kwargs filter.

    2.22.0 drops public async_client_args not explicitly named by websockets,
    including asyncio's supported connection-racing options. Preserve the SDK's
    TLS context/proxy settings and only add dual-stack racing to this instance.
    Remove this workaround when the SDK forwards these public options correctly.
    """
    options = getattr(getattr(client, '_api_client', None), '_websocket_ssl_ctx', None)
    if genai.__version__ != '2.22.0' or not isinstance(options, dict):
        raise TransportCompatibilityError('Google SDK transport compatibility changed; verify the local Happy Eyeballs configuration')
    client._api_client._websocket_ssl_ctx = {**options, 'happy_eyeballs_delay': 0.25, 'interleave': 1}


async def _close_client(client):
    """Close both SDK transports; its async close does not close the sync one."""
    closers = [client.aio.aclose()]
    close = getattr(client, 'close', None)
    if close is not None:
        closers.append(asyncio.to_thread(close))
    # These are independent transports in the pinned SDK. Both get the existing
    # three-second resource budget; adding sync cleanup must not extend Stop.
    results = await asyncio.wait_for(asyncio.gather(*closers, return_exceptions=True), 3)
    for result in results:
        if isinstance(result, BaseException):
            raise result


def _close_unused_client(client):
    # This client never reached an async API call, so no session belongs to the
    # job loop. The constructor thread can dispose it even after that loop exits.
    try:
        asyncio.run(_close_client(client))
    except Exception:
        logger.warning('Could not completely close an abandoned Google client')


@dataclass(frozen=True)
class RecoveryOptions:
    vad_enabled: bool = False
    rotation_seconds: float = 480.0
    max_buffer_seconds: float = 120.0
    finalize_timeout: float = 10.0
    connect_timeout: float = 15.0
    max_retries: int = 8
    retry_delay: float = 1.0
    max_retry_delay: float = 8.0
    recovery_timeout: float = 60.0  # connection/replay or finalization episode; excludes healthy speech
    replay_speed: float = 2.0
    fault_after_audio_seconds: float | None = None  # local, one-shot fault injection


    def __post_init__(self):
        for name in ("rotation_seconds",
                     "max_buffer_seconds",
                     "finalize_timeout", "connect_timeout", "replay_speed", "max_retry_delay", "recovery_timeout"):
            if not math.isfinite(getattr(self, name)) or getattr(self, name) <= 0:
                raise ValueError(f"{name} must be positive and finite")
        if self.max_retries < 0 or not math.isfinite(self.retry_delay) or self.retry_delay < 0:
            raise ValueError("Recovery counts, delays and thresholds cannot be negative")
        if self.fault_after_audio_seconds is not None and (not math.isfinite(self.fault_after_audio_seconds)
                                                          or self.fault_after_audio_seconds <= 0):
            raise ValueError("fault_after_audio_seconds must be positive")
        if self.vad_enabled is not False:
            raise ValueError("The promoted hybrid mode does not use local VAD or pauses")


@dataclass
class _Chunk:
    pcm: bytes
    start_sample: int

    @property
    def samples(self):
        return len(self.pcm) // 2


@dataclass
class _Window:
    id: str
    chunks: list[_Chunk] = field(default_factory=list)
    samples: int = 0
    sealed: bool = False


@dataclass
class _Attempt:
    window: _Window
    number: int
    done: asyncio.Future
    end_sent: bool = False
    saw_interim: bool = False
    voice_end_offset: str | None = None


class ResilientGeminiSTT(rtc.EventEmitter):
    """Small STT-compatible facade; recovery belongs to each individual stream."""

    label = "bayaan.gemini.ResilientGeminiSTT"

    def __init__(self, *, model: str, vertexai: bool, language: str | None = None,
                 language_codes: list[str] | None = None, sample_rate: int = 16000,
                 api_key: str | None = None, project: str | None = None,
                 location: str = "global", credentials: str | dict | None = None,
                 credentials_path: str | None = None, custom_vocabulary=None,
                 transcription_mode: str = "VERBATIM",
                 recovery: RecoveryOptions | None = None,
                 _client_factory: Callable[..., Any] = genai.Client,
                 _clock: Callable[[], float] = time.monotonic):
        super().__init__()
        if sample_rate != 16000:
            raise ValueError("The local Gemini adapter requires 16 kHz mono PCM16")
        if transcription_mode not in ("SMART", "VERBATIM"):
            raise ValueError("transcription_mode must be SMART or VERBATIM")
        self.transcription_mode = transcription_mode
        self.model, self.language, self.sample_rate = model, language or "", sample_rate
        self.recovery = recovery or RecoveryOptions()
        self.capabilities = stt.STTCapabilities(streaming=True, interim_results=True)
        if hasattr(self.capabilities, 'offline_recognize'):
            self.capabilities.offline_recognize = False
        self._clock, self._client_factory = _clock, _client_factory
        self._credential_refresh = None
        self._client_kwargs: dict[str, Any] = {"vertexai": False, "enterprise": False}
        if vertexai:
            # Match LiveKit Google 1.8's current Enterprise/Vertex endpoint selection.
            self._client_kwargs = {"enterprise": True, "project": project, "location": location}
            if credentials:
                info = json.loads(credentials) if isinstance(credentials, str) else credentials
                self._client_kwargs["credentials"] = service_account.Credentials.from_service_account_info(
                    info, scopes=["https://www.googleapis.com/auth/cloud-platform"])
            elif credentials_path:
                self._client_kwargs["credentials"] = service_account.Credentials.from_service_account_file(
                    credentials_path, scopes=["https://www.googleapis.com/auth/cloud-platform"])
        else:
            self._client_kwargs["api_key"] = api_key
        self._config = types.LiveConnectConfig(
            response_modalities=["TEXT"],
            realtime_input_config=types.RealtimeInputConfig(
                automatic_activity_detection=types.AutomaticActivityDetection(disabled=True)),
            input_audio_transcription=types.AudioTranscriptionConfig(
                language_codes=language_codes if language_codes is not None else ([language] if language else []),
                custom_vocabulary=custom_vocabulary or None, mode=transcription_mode),
        )
        self._streams: set[ResilientGeminiStream] = set()
        self._vad = None  # No pause detector; only Google punctuation and the 16s cap.


    async def _ensure_credentials(self):
        credentials = self._client_kwargs.get('credentials')
        if credentials is None or (credentials.token and credentials.valid):
            return
        # Gen AI refreshes these synchronously inside async live.connect. A single
        # retained refresh lets audio keep flowing and prevents retry/cancellation
        # from spawning concurrent refreshes against the same credential object.
        if self._credential_refresh is None or self._credential_refresh.done():
            loop = asyncio.get_running_loop()
            future = self._credential_refresh = loop.create_future()
            future.add_done_callback(lambda result: result.exception() if not result.cancelled() else None)
            deadline = time.monotonic() + self.recovery.connect_timeout

            def complete(error):
                if not future.done():
                    if error is None:
                        future.set_result(None)
                    else:
                        future.set_exception(error)

            def refresh():
                transport = None
                error = None
                try:
                    transport = GoogleAuthRequest()
                    def request(*args, **kwargs):
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise TimeoutError('Local Google credential refresh deadline exceeded')
                        kwargs['timeout'] = (min(3.0, remaining), min(5.0, remaining))
                        return transport(*args, **kwargs)
                    credentials.refresh(request)
                except Exception as exc:
                    error = exc
                finally:
                    if transport is not None:
                        try:
                            transport.session.close()
                        except Exception:
                            pass
                try:
                    loop.call_soon_threadsafe(complete, error)
                except RuntimeError:
                    pass  # The job loop has already closed; no audio is owned here.

            # Request connect/read timeouts bound normal I/O. OS DNS calls cannot
            # be forcibly cancelled; a daemon prevents one from holding shutdown.
            threading.Thread(target=refresh, daemon=True, name='local-google-auth-refresh').start()
        await asyncio.shield(self._credential_refresh)

    async def aclose(self):
        await asyncio.gather(*(stream.aclose() for stream in tuple(self._streams)))


class ResilientGeminiStream:
    def __init__(self, provider: ResilientGeminiSTT):
        self._provider, self._opts = provider, provider.recovery
        self._input: asyncio.Queue = asyncio.Queue()
        self._output: asyncio.Queue = asyncio.Queue(maxsize=128)
        self._closed = self._input_ended = self._eof = False
        self._drain_deadline = self._drain_loop_deadline = self._budget_timer = None
        self._held_bytes = self._received_samples = 0
        self._client = self._session = self._connection = self._reader = None
        self._client_startup = self._client_ready = None
        self._attempt: _Attempt | None = None
        self._connection_start = 0.0
        self._last_checkpoint = 0.0
        self._connect_phase = 'idle'
        self._connection_samples = self._fresh_sent_samples = 0
        self._fault_injected = False
        self._stream_id, self._sequence = uuid4().hex[:12], 0
        self.stats = {"connections": 0, "rotations": 0, "retries": 0,
                      "committed_windows": 0, "replayed_samples": 0,
                      "peak_buffer_bytes": 0, "discarded_digital_silence_samples": 0,
                      "forced_boundaries": 0}
        self._task = asyncio.create_task(self._run(), name="local-resilient-gemini")

    @property
    def buffered_audio_seconds(self):
        return self._held_bytes / 32000

    def push_frame(self, frame: rtc.AudioFrame):
        if self._input_ended or self._closed or self._task.done():
            raise RuntimeError("Gemini stream is no longer accepting audio")
        if frame.sample_rate != 16000 or frame.num_channels != 1:
            raise ValueError("Expected mono 16 kHz audio")
        pcm = frame.data.tobytes()
        if not pcm:
            return
        if self._held_bytes + len(pcm) > int(self._opts.max_buffer_seconds * 32000):
            raise RecoveryError(f"Google recovery audio buffer is full ({self._opts.max_buffer_seconds:g} seconds of retained audio); no duration cut was made. Stop and restart transcription. Unfinalized speech must be reviewed.")
        chunk = _Chunk(pcm, self._received_samples)
        self._input.put_nowait(chunk)
        self._received_samples += len(pcm) // 2
        self._held_bytes += len(pcm)
        self.stats["peak_buffer_bytes"] = max(self.stats["peak_buffer_bytes"], self._held_bytes)

    def end_input(self):
        if not self._input_ended:
            self._input_ended = True
            # Once the caller stops, all queued windows share one drain budget.
            # It must fit the worker's65s STT drain, even after a long healthy turn.
            self._drain_deadline = self._provider._clock() + self._opts.recovery_timeout
            self._drain_loop_deadline = asyncio.get_running_loop().time() + self._opts.recovery_timeout
            if self._budget_timer is not None and not self._budget_timer.expired():
                current = self._budget_timer.when()
                self._budget_timer.reschedule(min(current, self._drain_loop_deadline)
                                              if current is not None else self._drain_loop_deadline)
            self._input.put_nowait(_END)

    def __aiter__(self):
        return self

    async def __anext__(self):
        item = await self._output.get()
        if item is _END:
            raise StopAsyncIteration
        if isinstance(item, Exception):
            raise item
        return item

    async def aclose(self):
        self._closed = True
        self.end_input()
        self._task.cancel()
        await asyncio.gather(self._task, return_exceptions=True)
        self._held_bytes = 0
        # Unblock an iterator even when cancellation preceded the runner's first step.
        while not self._output.empty():
            self._output.get_nowait()
        self._output.put_nowait(_END)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        await self.aclose()




    async def _guard(self, operation):
        """Notice a dead receiver even when the sender is waiting for new audio."""
        waiter = asyncio.ensure_future(operation)
        try:
            done, _ = await asyncio.wait([waiter, self._reader], return_when=asyncio.FIRST_COMPLETED)
            # A queue.get that wins simultaneously MUST be returned, never discarded.
            if waiter in done:
                return waiter.result()
            self._reader.result()
            raise RecoveryError("Google receive loop ended unexpectedly")
        finally:
            if not waiter.done():
                waiter.cancel()
                await asyncio.gather(waiter, return_exceptions=True)

    async def _ensure_client(self):
        if self._client is not None:
            return
        if self._client_startup is None:
            # GenAI 2.22 constructs TLS contexts and HTTP transports synchronously.
            # Keep that work away from LiveKit's audio loop. Reconnect timeouts
            # share one pending constructor instead of spawning another client.
            startup = self._client_startup = Future()
            self._client_ready = asyncio.wrap_future(startup)
            self._client_ready.add_done_callback(
                lambda result: result.exception() if not result.cancelled() else None)
            factory = self._provider._client_factory
            kwargs = dict(self._provider._client_kwargs)

            def construct():
                if startup.cancelled():
                    return
                client = None
                try:
                    client = factory(**kwargs)
                    _enable_happy_eyeballs(client)
                except Exception as error:
                    if client is not None:
                        _close_unused_client(client)
                    try:
                        startup.set_exception(error)
                    except InvalidStateError:
                        pass  # The track ended while the constructor was running.
                else:
                    try:
                        startup.set_result(client)
                    except InvalidStateError:
                        _close_unused_client(client)

            # Cancellation cannot stop synchronous TLS work. A daemon plus a
            # cancellable ownership handoff lets a late result clean itself up
            # without holding job shutdown or leaking an unclaimed client.
            threading.Thread(target=construct, daemon=True,
                             name='google-client-startup').start()
        try:
            self._client = await asyncio.shield(self._client_ready)
        except Exception:
            self._client_startup = self._client_ready = None
            raise

    async def _release_client(self):
        client, self._client = self._client, None
        startup, self._client_startup = self._client_startup, None
        self._client_ready = None
        if client is None and startup is not None and not startup.cancel():
            # The constructor won the handoff just before cancellation, but the
            # awaiting coroutine never adopted its result. Cleanup owns it now.
            try:
                client = startup.result()
            except Exception:
                pass
        if client is not None:
            try:
                await _close_client(client)
            except Exception:
                pass  # Preserve the primary stream failure.

    async def _connect(self):
        async with asyncio.timeout(self._opts.connect_timeout):
            self._connect_phase = 'credential_refresh'
            await self._provider._ensure_credentials()
            self._connect_phase = 'client_initialization'
            await self._ensure_client()
            self._connect_phase = 'websocket_setup'
            self._connection = self._client.aio.live.connect(model=self._provider.model, config=self._provider._config)
            self._session = await self._connection.__aenter__()
        self._connect_phase = 'connected'
        self._connection_start, self._connection_samples = self._provider._clock(), 0
        self.stats["connections"] += 1
        self._reader = asyncio.create_task(self._receive(), name="local-google-receive")
        logger.info("Google connection ready: stream=%s connection=%d rotation_seconds=%.0f buffered_audio=%.2fs",
                    self._stream_id, self.stats["connections"], self._opts.rotation_seconds,
                    self.buffered_audio_seconds)

    async def _disconnect(self):
        if self._reader is not None:
            self._reader.cancel()
            await asyncio.gather(self._reader, return_exceptions=True)
        self._reader = None
        if self._connection is not None:
            try:
                await asyncio.wait_for(self._connection.__aexit__(None, None, None), 3)
            except Exception:
                pass  # The primary receive/send error is reported by the caller.
        self._session = self._connection = None

    async def _receive(self):
        while True:
            received = False
            # The SDK iterator ends at turn_complete; input transcription is independent.
            async for message in self._session.receive():
                received = True
                if getattr(message, "go_away", None):
                    logger.info("Google requested connection rollover: stream=%s connection=%d connection_age=%.2fs",
                                self._stream_id, self.stats["connections"],
                                self._provider._clock() - self._connection_start)
                    raise RecoveryError("Google requested a new connection; retaining uncommitted audio")
                attempt = self._attempt
                voice = getattr(message, "voice_activity", None)
                if attempt and voice and voice.voice_activity_type == types.VoiceActivityType.ACTIVITY_END:
                    attempt.voice_end_offset = voice.audio_offset
                content = getattr(message, "server_content", None)
                if not content:
                    continue
                interim = getattr(content, "interim_input_transcription", None)
                final = getattr(content, "input_transcription", None)
                if (interim is not None or final is not None) and attempt is None:
                    raise ProtocolError("Google sent transcription without an active audio window")
                if attempt and interim is not None and not attempt.done.done():
                    text = (interim.text or "").strip()
                    if text:
                        attempt.saw_interim = attempt.saw_interim or bool(text)
                        await self._emit(stt.SpeechEventType.INTERIM_TRANSCRIPT, text,
                                         interim.language_code, attempt)
                if final is not None:
                    if not attempt.end_sent or attempt.done.done():
                        raise ProtocolError("Google manual activity returned an unexpected final; audio retained for review")
                    # Empty authoritative input_transcription is allowed; an absent one is not.
                    attempt.done.set_result((final.text or "", final.language_code))
                # Never promote an interim on generation_complete/turn_complete.
            if not received:
                raise RecoveryError("Google receive loop ended unexpectedly")




    async def _send_chunk(self, chunk, replay):
        await asyncio.wait_for(self._guard(self._session.send_realtime_input(
            audio=types.Blob(data=chunk.pcm, mime_type="audio/pcm;rate=16000"))), self._opts.connect_timeout)
        self._connection_samples += chunk.samples
        if replay:
            self.stats["replayed_samples"] += chunk.samples
            await asyncio.sleep(chunk.samples / (16000 * self._opts.replay_speed))
        else:
            self._fresh_sent_samples += chunk.samples
            threshold = self._opts.fault_after_audio_seconds
            if threshold and not self._fault_injected and self._fresh_sent_samples >= threshold * 16000:
                self._fault_injected = True
                raise RecoveryError("Injected local Google connection interruption after audio send")


    @staticmethod
    def _cause_types(error):
        """Useful transport diagnostics without provider bodies, hosts or secrets."""
        names, seen = [], set()
        while error is not None and id(error) not in seen and len(names) < 6:
            seen.add(id(error))
            names.append(type(error).__name__)
            error = error.__cause__ or error.__context__
        return " -> ".join(names)

    async def _run_window(self, window):
        # Keep the same PCM across retries. Time healthy continuous input neither
        # as an outage nor as a forced finalization; memory remains bounded.
        started = self._provider._clock()
        deadline = None
        last_error = None
        try:
            async with asyncio.timeout(None) as timer:
                self._budget_timer = timer
                def remaining_budget():
                    deadlines = [value for value in (deadline, self._drain_deadline) if value is not None]
                    return min(deadlines) - self._provider._clock() if deadlines else self._opts.recovery_timeout

                def begin_budget():
                    nonlocal deadline
                    if deadline is None:
                        deadline = self._provider._clock() + self._opts.recovery_timeout
                    remaining = remaining_budget()
                    if remaining <= 0:
                        raise TimeoutError()
                    timer.reschedule(asyncio.get_running_loop().time() + remaining)

                def healthy():
                    nonlocal deadline, last_error
                    deadline = None
                    last_error = None
                    if self._drain_deadline is None:
                        timer.reschedule(None)
                    else:
                        remaining = remaining_budget()
                        if remaining <= 0:
                            raise TimeoutError()
                        timer.reschedule(asyncio.get_running_loop().time() + remaining)

                for number in range(self._opts.max_retries + 1):
                    if remaining_budget() <= 0:
                        raise TimeoutError()
                    try:
                        if self._session is None:
                            begin_budget()
                            await self._connect()
                        await self._attempt_window(window, number, begin_budget, healthy)
                        return
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        last_error = exc
                        begin_budget()  # A reader failure can arrive during healthy input wait.
                        code = getattr(exc, "code", getattr(exc, "status_code", None))
                        fatal = code in (400, 401, 403, 404) or isinstance(exc, (ProtocolError, TransportCompatibilityError))
                        error = RecoveryError(
                            f"Google authentication rejected ({code}); update the server credentials" if code in (401, 403)
                            else str(exc) if isinstance(exc, RecoveryError)
                            else f"Google connection interrupted ({type(exc).__name__})")
                        phase = self._connect_phase if self._session is None else 'audio_activity'
                        logger.warning("Google activity attempt failed: window=%s attempt=%d phase=%s cause_types=%s window_elapsed=%.2fs window_audio=%.2fs buffered_audio=%.2fs",
                                       window.id, number + 1, phase, self._cause_types(exc),
                                       self._provider._clock() - started, window.samples / 16000, self.buffered_audio_seconds)
                        await self._disconnect()
                        if fatal or number >= self._opts.max_retries:
                            raise RecoveryError(f"{error}. Uncommitted audio could not be finalized; review this recording.") from exc
                        remaining = remaining_budget()
                        if remaining <= 0:
                            raise TimeoutError()
                        delay = min(self._opts.max_retry_delay, self._opts.retry_delay * (2 ** number), remaining)
                        self.stats["retries"] += 1
                        self._provider.emit("error", stt.STTError(timestamp=time.time(), label=self._provider.label,
                                                                 error=error, recoverable=True))
                        logger.warning("Retrying Google %s for retained window %s (retry %d/%d in %.2fs; %.2fs budget remaining; buffered_audio=%.2fs)",
                                       'connection' if phase != 'audio_activity' else 'audio replay',
                                       window.id, number + 1, self._opts.max_retries, delay, remaining, self.buffered_audio_seconds)
                        await asyncio.sleep(delay)
        except TimeoutError as exc:
            cause = last_error or exc
            logger.error("Google recovery deadline exhausted: window=%s window_elapsed=%.2fs cause_types=%s window_audio=%.2fs buffered_audio=%.2fs",
                         window.id, self._provider._clock() - started, self._cause_types(cause), window.samples / 16000, self.buffered_audio_seconds)
            raise RecoveryError(
                f"Google recovery deadline exhausted after {self._opts.recovery_timeout:g} seconds; "
                "uncommitted audio could not be finalized. Review this recording.") from cause
        finally:
            self._budget_timer = None

    async def _run(self):
        try:
            while not self._eof:
                window = await self._first_window()
                if window is None:
                    break
                if self._session and (self._provider._clock() - self._connection_start >= self._opts.rotation_seconds
                                      or self._connection_samples / 16000 >= self._opts.rotation_seconds):
                    self.stats["rotations"] += 1
                    logger.info("Rotating Google connection at committed audio boundary: stream=%s connection=%d connection_age=%.2fs connection_audio=%.2fs committed_windows=%d buffered_audio=%.2fs",
                                self._stream_id, self.stats["connections"],
                                self._provider._clock() - self._connection_start,
                                self._connection_samples / 16000,
                                self.stats["committed_windows"], self.buffered_audio_seconds)
                    await self._disconnect()
                await self._run_window(window)
                now = self._provider._clock()
                if now - self._last_checkpoint >= 60:
                    self._last_checkpoint = now
                    logger.info("Google stream checkpoint: stream=%s received_audio=%.2fs buffered_audio=%.2fs statistics=%s",
                                self._stream_id, self._received_samples / 16000,
                                self.buffered_audio_seconds, json.dumps(self.stats, sort_keys=True))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._provider.emit("error", stt.STTError(timestamp=time.time(), label=self._provider.label,
                                                     error=exc, recoverable=False))
            await self._output.put(exc)
        finally:
            await self._disconnect()
            await self._release_client()
            self._attempt = None
            while not self._input.empty():
                self._input.get_nowait()
            self._held_bytes = 0
            logger.info("Gemini stream recovery statistics: %s", json.dumps(self.stats, sort_keys=True))
            if not self._closed:
                await self._output.put(_END)
