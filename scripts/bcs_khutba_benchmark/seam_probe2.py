"""Which side of a seam holds the lost words, and does silence padding recover them?

usage: python seam_probe2.py ARM CLIP   (reads ARM_CLIP_seams.json written by seam_probe.py)
"""
import asyncio, json, sys, wave
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
from stt_run import lab_env  # noqa: E402
from score import load  # noqa: E402
from google import genai  # noqa: E402
from google.genai import types  # noqa: E402
from google.oauth2 import service_account  # noqa: E402
from gemini_stt.eof import transcribe_window  # noqa: E402

AUDIO = Path(__file__).resolve().parent.parent / 'audio'
PAD = b'\x00\x00' * 8000  # 0.5 s


async def main():
    arm, clip = sys.argv[1], sys.argv[2]
    seams = json.loads(Path(f'{arm}_{clip}_seams.json').read_text(encoding='utf-8'))
    finals = load(arm, clip)['finals']
    with wave.open(str(AUDIO / f'{clip}.wav'), 'rb') as w:
        pcm = w.readframes(w.getnframes())
    env = lab_env()
    creds = service_account.Credentials.from_service_account_file(
        env['GOOGLE_APPLICATION_CREDENTIALS'], scopes=['https://www.googleapis.com/auth/cloud-platform'])
    client = genai.Client(enterprise=True, project=env['GOOGLE_CLOUD_PROJECT'], location='global', credentials=creds)
    cfg = types.AudioTranscriptionConfig(language_codes=['bs-BA'], mode='VERBATIM')

    async def tx(a):
        return await transcribe_window(client, model='gemini-3.5-transcribe-preview', pcm=a, audio_config=cfg)

    for s in seams:
        k1 = next(i for i, f in enumerate(finals) if abs(f['start'] - s['seam_s']) < 0.006)
        gap = round(finals[k1]['start'] - finals[k1 - 1]['end'], 3)
        cut = int(round(s['seam_s'] * 16000)) * 2
        tail, head = pcm[cut - 3 * 32000:cut], pcm[cut:cut + 3 * 32000]
        span = pcm[cut - 3 * 32000:cut + 3 * 32000]
        rms = lambda b: (sum(x * x for x in memoryview(b).cast('h')) / max(1, len(b) // 2)) ** .5
        around = [round(rms(pcm[cut + o * 320:cut + (o + 1) * 320])) for o in range(-5, 5)]
        r = dict(tail=await tx(tail), tail_pad=await tx(tail + PAD), head=await tx(head),
                 pad_head=await tx(PAD + head), span=await tx(span))
        print(f"\n== {s['seam_s']}s missing [{s['missing']}]  gap_between_windows={gap}s  rms 10ms frames around cut: {around}")
        for k, v in r.items():
            print(f'  {k:9s}: {v}')


asyncio.run(main())
