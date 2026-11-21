"""Stream a 16 kHz mono WAV through the production Gemini hybrid adapter at real time.

Records raw Google finals (with boundary metadata) and the exact sentence units the
production buffer would send to GPT. Arms vary only the AudioTranscriptionConfig.

usage: python stt_run.py WAV OUT.json --codes bs-BA[,ar-SA] [--vocab FILE] [--speed 1.0] [--pad S] [--legacy]

Production applies the 400 ms pause / 8 s minimum / 0.5 s padding policy to bs/hr/sr itself;
--legacy drops it (old punctuation baseline); --snap/--splitfix are early prototypes.
"""
import argparse, asyncio, json, os, sys, time, wave
from pathlib import Path

BAYAAN = Path(__file__).resolve().parents[2]
# Google credentials come from the local STT lab (never production settings); override with BCS_LAB_ENV.
LAB_ENV = Path(os.environ.get('BCS_LAB_ENV') or Path(__file__).resolve().parents[4] / 'local-stt-lab' / '.env.local')
sys.path.insert(0, str(BAYAAN))

from livekit import rtc  # noqa: E402
from livekit.agents import stt  # noqa: E402
from gemini_transcription import gemini_provider_options, forward_hybrid_sentences  # noqa: E402
from gemini_stt.hybrid import GeminiHybridSTT  # noqa: E402


def apply_splitfix():
    """Candidate fix: a period followed by a lowercase letter or , ; : continues the sentence."""
    from gemini_stt import sentences as S
    original = S._period_ends

    def _period_ends(text, index):
        j = index + 1
        while j < len(text) and (text[j] in S._ENDINGS or text[j] in S._CLOSERS or text[j].isspace()):
            j += 1
        following = text[j] if j < len(text) else ''
        if following.islower() or following in (',', ';', ':'):
            return False
        return original(text, index)

    S._period_ends = _period_ends


def snap_provider_class(quiet_frames=4, max_wait_s=1.2, ratio=0.2, min_window_s=0.0):
    """Production hybrid policy, but a punctuation-requested cut waits for a short pause.

    The marker is queued right after the first run of `quiet_frames` quiet 10 ms frames
    (or after `max_wait_s`), so the activity still ends exactly at a captured sample
    boundary and every sample stays in FIFO order; only the cut position moves.
    """
    from collections import deque
    import array
    from gemini_stt import hybrid as H
    from gemini_stt.sentences import split_complete_sentences

    class SnapStream(H._HybridStream):
        def __init__(self, provider):
            self._pending = None
            self._quiet_run = 0
            self._levels = deque(maxlen=300)  # last 3 s of frame RMS
            self.snap_log = []
            super().__init__(provider)

        async def _emit(self, kind, text, language, attempt):
            if (kind == stt.SpeechEventType.INTERIM_TRANSCRIPT and not attempt.end_sent
                    and not attempt.window.sealed and not getattr(attempt, 'punctuation_requested', False)
                    and attempt.window.samples >= min_window_s * 16000
                    and split_complete_sentences(text)[0]):
                attempt.punctuation_requested = True
                self._pending = (attempt.window.id, attempt.number, self._received_samples)
                self._quiet_run = 0
            await H._emit_window(self, kind, text, language, attempt)

        def push_frame(self, frame):
            super().push_frame(frame)
            samples = array.array('h', frame.data.tobytes())
            rms = (sum(s * s for s in samples) / max(1, len(samples))) ** 0.5
            self._levels.append(rms)
            if self._pending is None:
                return
            window_id, number, since = self._pending
            speech = sorted(self._levels)[int(len(self._levels) * 0.7)]
            self._quiet_run = self._quiet_run + 1 if rms < max(60.0, ratio * speech) else 0
            waited = self._received_samples - since
            if self._quiet_run >= quiet_frames or waited >= max_wait_s * 16000:
                self._input.put_nowait(H._PunctuationBoundary(window_id, number, self._received_samples))
                self.snap_log.append(dict(wait_ms=round(waited / 16), quiet=self._quiet_run >= quiet_frames))
                self._pending = None

    class SnapSTT(GeminiHybridSTT):
        def stream(self):
            stream = SnapStream(self)
            self._streams.add(stream)
            stream._task.add_done_callback(lambda _: self._streams.discard(stream))
            return stream

    return SnapSTT


def lab_env():
    env = dict(os.environ)
    for line in LAB_ENV.read_text(encoding='utf-8-sig').splitlines():
        if '=' in line and not line.lstrip().startswith('#'):
            k, v = line.split('=', 1)
            if v.strip():
                env[k.strip()] = v.strip().strip('"')
    cred = env.get('GOOGLE_APPLICATION_CREDENTIALS')
    if cred and not Path(cred).is_absolute():
        env['GOOGLE_APPLICATION_CREDENTIALS'] = str(LAB_ENV.parent / cred)
    env['GEMINI_STT_BACKEND'] = 'vertex'
    return env


class Recorder:
    def __init__(self, stream, t0):
        self.stream, self.t0, self.finals = stream, t0, []

    def __aiter__(self):
        return self

    async def __anext__(self):
        event = await self.stream.__anext__()
        if event.type == stt.SpeechEventType.FINAL_TRANSCRIPT and event.alternatives:
            a = event.alternatives[0]
            md = a.metadata if isinstance(getattr(a, 'metadata', None), dict) else {}
            self.finals.append(dict(text=a.text, start=round(a.start_time, 2), end=round(a.end_time, 2),
                                    reason=md.get('boundary_reason'), source=md.get('finalization_source'),
                                    arrived=round(time.monotonic() - self.t0, 2)))
        return event


async def main():
    p = argparse.ArgumentParser()
    p.add_argument('wav'); p.add_argument('out')
    p.add_argument('--codes', default='bs-BA')
    p.add_argument('--vocab')
    p.add_argument('--speed', type=float, default=1.0)
    p.add_argument('--lang', default='bs')
    p.add_argument('--snap', action='store_true')
    p.add_argument('--min-window', type=float, default=0.0)
    p.add_argument('--pad', type=float, default=0.0)
    p.add_argument('--pause-cut', type=float, default=0.0)
    p.add_argument('--legacy', action='store_true')
    p.add_argument('--splitfix', action='store_true')
    a = p.parse_args()
    if a.splitfix:
        apply_splitfix()
    codes = [c for c in a.codes.split(',') if c]
    opts = gemini_provider_options(codes[0], lab_env())
    opts['language'] = a.lang
    if a.legacy:
        for name in ('pause_cut_seconds', 'min_cut_seconds', 'edge_padding_seconds'):
            opts.pop(name, None)
    if a.pad:
        opts['edge_padding_seconds'] = a.pad
    if a.pause_cut:
        opts['pause_cut_seconds'] = a.pause_cut
    opts['language_codes'] = codes
    vocab = None
    if a.vocab:
        vocab = [w.strip() for w in Path(a.vocab).read_text(encoding='utf-8').splitlines()
                 if w.strip() and not w.startswith('#')]
        opts['custom_vocabulary'] = vocab
    provider = (snap_provider_class(min_window_s=a.min_window) if a.snap else GeminiHybridSTT)(**opts)
    stream = provider.stream()
    t0 = time.monotonic()
    rec = Recorder(stream, t0)
    units = []

    async def publish(segment_id, text, language, final):
        return None

    async def translate(text, sentence_id, language):
        units.append(dict(text=text, id=sentence_id, arrived=round(time.monotonic() - t0, 2)))

    input_ended = asyncio.Event()
    receiver = asyncio.create_task(forward_hybrid_sentences(rec, publish, translate, input_ended, a.lang))
    with wave.open(a.wav, 'rb') as w:
        assert w.getframerate() == 16000 and w.getnchannels() == 1 and w.getsampwidth() == 2
        pcm = w.readframes(w.getnframes())
    frame_bytes = 320  # 10 ms
    loop = asyncio.get_running_loop()
    start = loop.time()
    for i in range(0, len(pcm) // frame_bytes):
        chunk = pcm[i * frame_bytes:(i + 1) * frame_bytes]
        stream.push_frame(rtc.AudioFrame(chunk, 16000, 1, 160))
        if i % 10 == 9:
            target = start + (i + 1) * 0.01 / a.speed
            delay = target - loop.time()
            if delay > 0:
                await asyncio.sleep(delay)
        if receiver.done():
            break
    input_ended.set()
    stream.end_input()
    error = None
    try:
        await asyncio.wait_for(receiver, 90)
    except Exception as e:  # keep partial results
        error = f'{type(e).__name__}: {e}'
    await stream.aclose()
    Path(a.out).write_text(json.dumps(dict(
        wav=os.path.basename(a.wav), codes=codes, vocab_terms=len(vocab or []), speed=a.speed,
        error=error, stats=stream.stats, finals=rec.finals, units=units,
        snap_log=getattr(stream, 'snap_log', None),
        transcript=' '.join(f['text'].strip() for f in rec.finals if f['text'].strip()),
    ), ensure_ascii=False, indent=1), encoding='utf-8')
    print(a.out, 'finals', len(rec.finals), 'units', len(units), 'error', error)


if __name__ == '__main__':
    asyncio.run(main())
