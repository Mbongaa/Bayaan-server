"""Offline seam simulation: re-transcribe a live run's exact windows with the file model.

The file model reproduces the live output on identical window audio, so edge treatments
(silence padding, overlap) can be compared cheaply without live sessions.
Writes runs/OUT_ARM/CLIP.json in the stt_run format (finals with start/end/text).

usage: python sim_windows.py SRC_ARM CLIP OUT_ARM [--pad-start S] [--pad-end S] [--overlap S] [--jobs N]
"""
import argparse, asyncio, json, sys, wave
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
from stt_run import lab_env  # noqa: E402
from score import RUNS, load, norm_words  # noqa: E402
from google import genai  # noqa: E402
from google.genai import types  # noqa: E402
from google.oauth2 import service_account  # noqa: E402
from gemini_stt.eof import transcribe_window  # noqa: E402

AUDIO = Path(__file__).resolve().parent.parent / 'audio'


def drop_overlap(prev_words, text, max_words=8):
    """Remove from the start of `text` the longest prefix that repeats the end of prev_words."""
    words = text.split()
    norm = [norm_words(w) for w in words]
    flat_prev = prev_words[-max_words:]
    best = 0
    for n in range(1, min(max_words, len(words)) + 1):
        head = [x for w in norm[:n] for x in w]
        if head and len(head) <= len(flat_prev) and flat_prev[-len(head):] == head:
            best = n
    return ' '.join(words[best:])


async def main():
    p = argparse.ArgumentParser()
    p.add_argument('src'); p.add_argument('clip'); p.add_argument('out')
    p.add_argument('--pad-start', type=float, default=0.0); p.add_argument('--pad-end', type=float, default=0.0)
    p.add_argument('--overlap', type=float, default=0.0); p.add_argument('--jobs', type=int, default=6)
    a = p.parse_args()
    finals = [f for f in load(a.src, a.clip)['finals']]
    with wave.open(str(AUDIO / f'{a.clip}.wav'), 'rb') as w:
        pcm = w.readframes(w.getnframes())
    env = lab_env()
    creds = service_account.Credentials.from_service_account_file(
        env['GOOGLE_APPLICATION_CREDENTIALS'], scopes=['https://www.googleapis.com/auth/cloud-platform'])
    client = genai.Client(enterprise=True, project=env['GOOGLE_CLOUD_PROJECT'], location='global', credentials=creds)
    cfg = types.AudioTranscriptionConfig(language_codes=['bs-BA'], mode='VERBATIM')
    zeros = lambda s: b'\x00\x00' * int(s * 16000)
    sem = asyncio.Semaphore(a.jobs)

    async def one(f):
        start, end = int(round(f['start'] * 16000)), int(round(f['end'] * 16000))
        ostart = max(0, start - int(a.overlap * 16000))
        audio = zeros(a.pad_start) + pcm[ostart * 2:end * 2] + zeros(a.pad_end)
        async with sem:
            for attempt in range(6):
                try:
                    return await transcribe_window(client, model='gemini-3.5-transcribe-preview', pcm=audio, audio_config=cfg)
                except Exception:
                    if attempt == 5:
                        raise
                    await asyncio.sleep(5 * (attempt + 1))

    texts = await asyncio.gather(*(one(f) for f in finals))
    out, prev = [], []
    for f, t in zip(finals, texts):
        if a.overlap and prev:
            t = drop_overlap(prev, t)
        prev = (prev + norm_words(t))[-20:]
        out.append(dict(text=t, start=f['start'], end=f['end'], reason=f['reason']))
    dest = RUNS / a.out
    dest.mkdir(parents=True, exist_ok=True)
    (dest / f'{a.clip}.json').write_text(json.dumps(dict(finals=out, transcript=' '.join(x['text'] for x in out)),
                                                   ensure_ascii=False, indent=1), encoding='utf-8')
    print(a.out, a.clip, len(out), 'windows')


asyncio.run(main())
