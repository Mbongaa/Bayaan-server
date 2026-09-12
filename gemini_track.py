"""Run the chosen hybrid policy with the production translation callback.

Audio ingestion, Google events and ordered GPT requests run independently. This
module owns one track and drains its confirmed text before releasing resources.
"""
import asyncio
import logging
import math

from livekit import rtc

from gemini_transcription import forward_hybrid_sentences

logger = logging.getLogger('transcriber.gemini_track')

# Keep receiving immediately, but retain up to 30 seconds of 10 ms mono frames
# during another SDK's first-use stall. LiveKit's bounded ring silently drops
# oldest frames when full; 100 frames previously retained only about one second.
GEMINI_AUDIO_FRAME_MS = 10
GEMINI_AUDIO_QUEUE_FRAMES = 3000


async def run_gemini_track(*, participant, track, provider, publish, translate,
                          heartbeat=None, stt_drain_seconds=65.0,
                          translation_drain_seconds=45.0, audio_factory=rtc.AudioStream):
    for name, seconds in (('stt_drain_seconds', stt_drain_seconds),
                          ('translation_drain_seconds', translation_drain_seconds)):
        if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not math.isfinite(seconds) or seconds <= 0:
            raise ValueError(f'{name} must be a finite positive number')
    audio = stream = feed = receive = consumer = None
    input_ended = asyncio.Event()
    queue = asyncio.Queue(maxsize=128)
    sentinel = object()

    async def consume_translations():
        while True:
            item = await queue.get()
            try:
                if item is sentinel:
                    return
                await translate(*item)
            finally:
                queue.task_done()

    async def enqueue(text, sentence_id, language):
        if consumer.done():
            await consumer
            raise RuntimeError('Gemini translation queue stopped')
        try:
            queue.put_nowait((text, sentence_id, language))
        except asyncio.QueueFull:
            raise RuntimeError('Translation backlog reached 128 segments; stop and check the provider') from None

    async def feed_audio():
        frames = 0
        async for event in audio:
            stream.push_frame(event.frame)
            frames += 1
            if heartbeat and frames % 100 == 0:
                await heartbeat()

    async def drain():
        errors = []

        def failed(error):
            # A provider or translation failure remains the primary cause. Later
            # cleanup failures must neither hide it nor skip another resource.
            errors.append(RuntimeError('A Gemini track lifecycle step was cancelled unexpectedly')
                          if isinstance(error, asyncio.CancelledError) else error)

        async def cancel_and_wait(task):
            if task is not None:
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)

        await cancel_and_wait(feed)
        if stream is not None:
            input_ended.set()
            try:
                stream.end_input()
            except (Exception, asyncio.CancelledError) as error:
                failed(error)
        try:
            if receive is not None and not errors:
                await asyncio.wait_for(asyncio.shield(receive), stt_drain_seconds)
        except (Exception, asyncio.CancelledError) as error:
            failed(error)
        finally:
            await cancel_and_wait(receive)
        if consumer is not None:
            put_sentinel = None
            try:
                # One budget covers the full queue drain, including admitting the
                # sentinel. A dead consumer must wake a blocked queue.put.
                async with asyncio.timeout(translation_drain_seconds):
                    if consumer.done():
                        await consumer
                        raise RuntimeError('Gemini translation consumer stopped before its drain marker')
                    put_sentinel = asyncio.create_task(queue.put(sentinel))
                    done, _ = await asyncio.wait([put_sentinel, consumer],
                                                  return_when=asyncio.FIRST_COMPLETED)
                    if consumer in done:
                        await consumer
                    await put_sentinel
                    await asyncio.shield(consumer)
            except (Exception, asyncio.CancelledError) as error:
                failed(error)
            finally:
                await cancel_and_wait(put_sentinel)
                await cancel_and_wait(consumer)
        for resource in (stream, audio):
            if resource is not None:
                try:
                    await resource.aclose()
                except (Exception, asyncio.CancelledError) as error:
                    failed(error)
        return errors

    cancelled = False
    problem = None
    try:
        audio = audio_factory(track, sample_rate=16000, num_channels=1,
                              capacity=GEMINI_AUDIO_QUEUE_FRAMES,
                              frame_size_ms=GEMINI_AUDIO_FRAME_MS)
        stream = provider.stream()
        consumer = asyncio.create_task(consume_translations(), name=f'gemini-translate-{track.sid}')
        receive = asyncio.create_task(forward_hybrid_sentences(
            stream, publish, enqueue, input_ended, provider.language,
            audit_scope={'room_name': getattr(participant, 'room_name', ''),
                         'participant_identity': participant.identity, 'track_sid': track.sid}),
            name=f'gemini-receive-{track.sid}')
        feed = asyncio.create_task(feed_audio(), name=f'gemini-audio-{track.sid}')
        done, _ = await asyncio.wait([feed, receive, consumer], return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            if task.cancelled():
                raise RuntimeError('A Gemini pipeline task was cancelled while audio was active')
            await task
        if feed not in done:
            raise RuntimeError('Transcription pipeline stopped while audio was active')
    except asyncio.CancelledError:
        cancelled = True
    except Exception as error:
        problem = error
    finally:
        draining = asyncio.create_task(drain(), name=f'gemini-drain-{track.sid}')
        # Unpublish, unsubscribe and disconnect can all arrive for one track.
        # The same shield protects both draining and releasing owned resources.
        while not draining.done():
            try:
                await asyncio.shield(draining)
            except asyncio.CancelledError:
                cancelled = True
        cleanup_errors = await draining
        if problem is None and cleanup_errors:
            problem = cleanup_errors[0]
        for error in cleanup_errors:
            if error is not problem:
                logger.error('Secondary Gemini track cleanup failure: %s', type(error).__name__)
    if problem:
        raise problem
    if cancelled:
        raise asyncio.CancelledError
