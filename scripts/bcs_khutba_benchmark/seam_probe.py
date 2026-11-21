"""For seams where the live run lost words, re-transcribe the two adjacent windows' exact audio.

Shows, per seam: live text of window k and k+1, and file-model text of the same audio,
so we can see whether the lost words were in window k's tail or window k+1's head.
usage: python seam_probe.py ARM CLIP [MAX]
"""
import asyncio, json, sys, wave
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
from stt_run import lab_env  # noqa: E402
from score import load, norm_words, align  # noqa: E402
from google import genai  # noqa: E402
from google.genai import types  # noqa: E402
from google.oauth2 import service_account  # noqa: E402
from gemini_stt.eof import transcribe_window  # noqa: E402

AUDIO = Path(__file__).resolve().parent.parent / 'audio'


async def main():
    arm, clip = sys.argv[1], sys.argv[2]
    limit = int(sys.argv[3]) if len(sys.argv) > 3 else 12
    d, r = load(arm, clip), load('F-bs', clip)
    ref = norm_words(r['transcript'])
    finals = [f for f in d['finals'] if norm_words(f['text'])]
    hyp, starts = [], {}
    for k, f in enumerate(finals):
        starts[len(hyp)] = k
        hyp.extend(norm_words(f['text']))
    ops = align(ref, hyp)
    seams, k = [], 0
    while k < len(ops):
        if ops[k][0] != 'D':
            k += 1; continue
        run = []
        while k < len(ops) and ops[k][0] == 'D':
            run.append(ops[k]); k += 1
        j = run[0][2]
        if j in starts and starts[j] > 0:
            seams.append((starts[j], ' '.join(ref[run[0][1]:run[0][1] + len(run)])))
    with wave.open(str(AUDIO / f'{clip}.wav'), 'rb') as w:
        pcm = w.readframes(w.getnframes())
    env = lab_env()
    creds = service_account.Credentials.from_service_account_file(
        env['GOOGLE_APPLICATION_CREDENTIALS'], scopes=['https://www.googleapis.com/auth/cloud-platform'])
    client = genai.Client(enterprise=True, project=env['GOOGLE_CLOUD_PROJECT'], location='global', credentials=creds)
    cfg = types.AudioTranscriptionConfig(language_codes=['bs-BA'], mode='VERBATIM')
    out = []
    for nxt, missing in seams[:limit]:
        prev = nxt - 1
        texts = []
        for f in (finals[prev], finals[nxt]):
            a, b = int(f['start'] * 16000) * 2, int(f['end'] * 16000) * 2
            texts.append(await transcribe_window(client, model='gemini-3.5-transcribe-preview', pcm=pcm[a:b], audio_config=cfg))
        row = dict(missing=missing, seam_s=finals[nxt]['start'], reason=finals[prev]['reason'],
                   live_prev=finals[prev]['text'], file_prev=texts[0], live_next=finals[nxt]['text'], file_next=texts[1])
        out.append(row)
        print(f"\n== seam at {row['seam_s']}s (cut reason {row['reason']}) missing: [{missing}]")
        print(f"  live k  : …{row['live_prev'][-90:]}\n  file k  : …{row['file_prev'][-90:]}")
        print(f"  live k+1: {row['live_next'][:90]}…\n  file k+1: {row['file_next'][:90]}…")
    Path(f'{arm}_{clip}_seams.json').write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding='utf-8')


asyncio.run(main())
