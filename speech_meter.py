"""Report the minutes in which this worker processed speech, for billing.

The classroom app bills translation on these minutes, and only while a host is
connected, so waiting before a lecture, silence and breaks cost nothing. A
minute counts once at least one non-empty transcription was published in it.

Reports go to the classroom app (POST {CLASSROOM_APP_URL}/api/v2/speech-minutes,
authorised with SPEECH_METER_SECRET) as sets of whole UTC minutes, numbered
as seconds since the epoch divided by 60. Sets make every report idempotent:
a retry, or a second worker in the same room, never counts a minute twice.
Without both settings the meter is off and the worker runs as before.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Awaitable, Callable, Optional

logger = logging.getLogger("speech-meter")

FLUSH_INTERVAL_SECONDS = 30.0
MAX_MINUTES_PER_REPORT = 240
# Kept while the classroom app is unreachable: one day of speech.
MAX_PENDING_MINUTES = 24 * 60
CLOSE_TIMEOUT_SECONDS = 5.0
REQUEST_TIMEOUT_SECONDS = 10.0
# Answers that mean "try again later"; every other 4xx is final.
RETRY_STATUSES = {408, 425, 429}

# Sends one report body and returns the HTTP status.
Poster = Callable[[dict], Awaitable[int]]


class SpeechMeter:
    def __init__(
        self,
        room_name: str,
        post: Optional[Poster],
        *,
        clock: Callable[[], float] = time.time,
        flush_interval: float = FLUSH_INTERVAL_SECONDS,
    ):
        self._room_name = room_name
        self._post = post
        self._clock = clock
        self._flush_interval = flush_interval
        self._pending: set[int] = set()
        # Recently confirmed minutes, so speech later in the same minute is not resent.
        self._reported: set[int] = set()
        self._flush_lock = asyncio.Lock()
        self._task: Optional[asyncio.Task] = None
        self._closed = False

    @property
    def enabled(self) -> bool:
        return self._post is not None

    @property
    def pending_minutes(self) -> list[int]:
        return sorted(self._pending)

    def mark(self) -> None:
        """Count the current minute as a minute with processed speech."""
        if self._post is None or self._closed:
            return
        minute = int(self._clock() // 60)
        if minute in self._reported:
            return
        self._pending.add(minute)
        if len(self._pending) > MAX_PENDING_MINUTES:
            oldest = min(self._pending)
            self._pending.discard(oldest)
            logger.warning("Speech meter backlog full for room %s; dropped minute %s", self._room_name, oldest)

    def start(self) -> None:
        if self._post is None or self._task is not None or self._closed:
            return
        self._task = asyncio.create_task(self._run(), name=f"speech-meter-{self._room_name}")

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self._flush_interval)
            try:
                await self.flush()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Speech meter flush failed for room %s", self._room_name)

    async def flush(self) -> None:
        """Send pending minutes; keep them for the next flush when the app is unreachable."""
        if self._post is None:
            return
        async with self._flush_lock:
            while self._pending:
                batch = sorted(self._pending)[:MAX_MINUTES_PER_REPORT]
                try:
                    status = await self._post({"roomName": self._room_name, "minutes": batch})
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    logger.warning(
                        "Speech minutes for room %s not reported yet (%s); retrying later",
                        self._room_name, error,
                    )
                    return
                if 200 <= status < 300:
                    self._pending.difference_update(batch)
                    self._reported.update(batch)
                elif status >= 500 or status in RETRY_STATUSES:
                    logger.warning(
                        "Speech minutes for room %s not reported yet (HTTP %s); retrying later",
                        self._room_name, status,
                    )
                    return
                else:
                    logger.error(
                        "Speech minutes for room %s rejected (HTTP %s); dropped %s minute(s). "
                        "Check CLASSROOM_APP_URL and SPEECH_METER_SECRET.",
                        self._room_name, status, len(batch),
                    )
                    self._pending.difference_update(batch)
            # Only the current minute can be marked again.
            current = int(self._clock() // 60)
            self._reported = {minute for minute in self._reported if minute >= current - 1}

    async def aclose(self) -> None:
        """Stop the loop and send what is left, within a short shutdown budget."""
        if self._closed:
            return
        self._closed = True
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        try:
            await asyncio.wait_for(self.flush(), timeout=CLOSE_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            logger.warning(
                "Speech meter for room %s closed with %s minute(s) unreported",
                self._room_name, len(self._pending),
            )
        except Exception:
            logger.exception("Final speech meter flush failed for room %s", self._room_name)


def _http_poster(endpoint: str, secret: str) -> Poster:
    import aiohttp

    timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECONDS)
    headers = {"Authorization": f"Bearer {secret}"}

    async def post(body: dict) -> int:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(endpoint, json=body, headers=headers) as response:
                return response.status

    return post


def speech_meter_for_room(room_name: Optional[str], env=None) -> SpeechMeter:
    """The meter for one room, or a disabled one when reporting is not configured."""
    env = os.environ if env is None else env
    app_url = (env.get("CLASSROOM_APP_URL") or "").strip().rstrip("/")
    secret = (env.get("SPEECH_METER_SECRET") or "").strip()
    room = room_name or ""
    if not (app_url and secret and room):
        logger.warning(
            "Speech meter off for room %s: CLASSROOM_APP_URL and SPEECH_METER_SECRET must both be set",
            room or "unknown",
        )
        return SpeechMeter(room, None)
    return SpeechMeter(room, _http_poster(f"{app_url}/api/v2/speech-minutes", secret))
