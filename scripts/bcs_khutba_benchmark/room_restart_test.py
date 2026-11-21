"""Real LiveKit room test of run_gemini_track_with_restarts (local livekit-server --dev).

A speaker participant publishes a WAV as a live microphone track at real time. A worker
participant subscribes and runs the production restart wrapper with the real Gemini
provider, mirroring main.py's task bookkeeping (unpublish cancels the task). Google
connection refusals are simulated by patching ResilientGeminiStream._connect.

usage: python room_restart_test.py A|B WAV OUT.json
  A: after FAULT_AT s the connection breaks and reconnects are refused for OUTAGE s
  B: reconnects refused permanently from FAULT_AT s; the speaker leaves at LEAVE_AT s
"""
import asyncio, json, logging, sys, time, wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from stt_run import lab_env  # noqa: E402  (puts Bayaan-server on sys.path)
from livekit import api, rtc  # noqa: E402
from gemini_transcription import gemini_provider_options  # noqa: E402
from gemini_stt.hybrid import GeminiHybridSTT  # noqa: E402
from gemini_stt import recovery as R  # noqa: E402
import gemini_track  # noqa: E402

URL, ROOM = 'ws://127.0.0.1:7880', f'restart-test-{int(time.time())}'
SCENARIOS = {
    'A': dict(fault_at=40.0, outage=25.0, recovery_timeout=12.0, leave_at=None),
    'B': dict(fault_at=10.0, outage=1e9, recovery_timeout=5.0, leave_at=30.0),
}


def token(identity):
    return (api.AccessToken('devkey', 'secret').with_identity(identity)
            .with_grants(api.VideoGrants(room_join=True, room=ROOM, can_publish=True, can_subscribe=True)).to_jwt())


class Capture(logging.Handler):
    def __init__(self):
        super().__init__(logging.WARNING)
        self.lines = []

    def emit(self, record):
        self.lines.append((round(time.monotonic() - T0, 1), record.name, record.getMessage()[:160]))


async def main():
    global T0
    which, wav_path, out_path = sys.argv[1], sys.argv[2], sys.argv[3]
    sc = SCENARIOS[which]
    capture = Capture()
    logging.getLogger().addHandler(capture)
    logging.getLogger().setLevel(logging.WARNING)
    T0 = time.monotonic()
    events = dict(finals=[], units=[], connects=[], refused=0, task_end=None, unpublished_at=None)
    outage = dict(start=None, end=None)

    original_connect = R.ResilientGeminiStream._connect

    async def connect(self):
        now = time.monotonic()
        if outage['start'] is not None and outage['start'] <= now < outage['end']:
            events['refused'] += 1
            raise ConnectionError('simulated Google connection refusal')
        events['connects'].append(round(now - T0, 1))
        return await original_connect(self)

    R.ResilientGeminiStream._connect = connect

    opts = gemini_provider_options('bs-BA', lab_env())
    opts['language'] = 'bs'
    opts['recovery'] = R.RecoveryOptions(recovery_timeout=sc['recovery_timeout'],
                                         fault_after_audio_seconds=sc['fault_at'])
    provider = GeminiHybridSTT(**opts)

    # --- worker (mirrors main.py: one task per (identity, track sid); unpublish cancels it)
    worker = rtc.Room()
    tasks = {}

    async def publish(segment_id, text, language, final):
        if final and text.strip():
            events['finals'].append(dict(t=round(time.monotonic() - T0, 1), text=text))

    async def translate(text, sentence_id, language):
        events['units'].append(dict(t=round(time.monotonic() - T0, 1), text=text))

    async def run_track(participant, track, key):
        this = asyncio.current_task()
        try:
            await gemini_track.run_gemini_track_with_restarts(
                still_current=lambda: tasks.get(key) is this,
                participant=participant, track=track, provider=provider,
                publish=publish, translate=translate, heartbeat=None)
            events['task_end'] = ('returned', round(time.monotonic() - T0, 1))
        except asyncio.CancelledError:
            events['task_end'] = ('cancelled', round(time.monotonic() - T0, 1))
            raise
        except Exception as error:
            events['task_end'] = (f'failed: {type(error).__name__}', round(time.monotonic() - T0, 1))

    @worker.on('track_subscribed')
    def on_subscribed(track, publication, participant):
        if track.kind == rtc.TrackKind.KIND_AUDIO:
            key = (participant.identity, track.sid)
            tasks[key] = asyncio.create_task(run_track(participant, track, key))

    @worker.on('track_unpublished')
    def on_unpublished(publication, participant):
        task = tasks.pop((participant.identity, publication.sid), None)
        if task:
            task.cancel()

    await worker.connect(URL, token('worker'))

    # --- speaker
    speaker = rtc.Room()
    await speaker.connect(URL, token('imam'))
    source = rtc.AudioSource(16000, 1)
    mic = rtc.LocalAudioTrack.create_audio_track('mic', source)
    publication = await speaker.local_participant.publish_track(
        mic, rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE))
    with wave.open(wav_path, 'rb') as w:
        pcm = w.readframes(w.getnframes())
    await asyncio.sleep(1.0)
    start = asyncio.get_running_loop().time()
    audio_t0 = time.monotonic()
    for i in range(len(pcm) // 320):
        audio_s = (i + 1) * 0.01
        if outage['start'] is None and audio_s >= sc['fault_at']:
            outage['start'] = time.monotonic(); outage['end'] = outage['start'] + sc['outage']
        if sc['leave_at'] and audio_s >= sc['leave_at']:
            break
        await source.capture_frame(rtc.AudioFrame(pcm[i * 320:(i + 1) * 320], 16000, 1, 160))
        delay = start + audio_s - asyncio.get_running_loop().time()
        if delay > 0:
            await asyncio.sleep(delay)
    events['audio_seconds'] = round(time.monotonic() - audio_t0, 1)
    if not sc['leave_at']:
        await asyncio.sleep(8)  # let the last window finalize before leaving
    events['unpublished_at'] = round(time.monotonic() - T0, 1)
    await speaker.local_participant.unpublish_track(publication.sid)
    for _ in range(300):
        if events['task_end']:
            break
        await asyncio.sleep(0.2)
    connects_before = len(events['connects']); refused_before = events['refused']
    await asyncio.sleep(10)  # nothing may keep retrying after the speaker left
    events['activity_after_leave'] = dict(connects=len(events['connects']) - connects_before,
                                          refused=events['refused'] - refused_before)
    await speaker.disconnect(); await worker.disconnect()
    events['outage'] = dict(start=round(outage['start'] - T0, 1) if outage['start'] else None,
                            seconds=sc['outage'])
    events['log'] = [l for l in capture.lines if 'restart' in l[2].lower() or 'failed' in l[2].lower()
                     or 'recovery' in l[2].lower()][:40]
    Path(out_path).write_text(json.dumps(events, ensure_ascii=False, indent=1), encoding='utf-8')
    print(json.dumps({k: v for k, v in events.items() if k not in ('finals', 'units', 'log')}, indent=1))


asyncio.run(main())
