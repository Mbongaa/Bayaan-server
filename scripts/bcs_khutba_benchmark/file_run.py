"""Transcribe a whole WAV (<=15 min) with the Gemini 3.5 file model: no live activity cuts.

usage: python file_run.py WAV OUT.json --codes bs-BA [--vocab FILE]
"""
import argparse, asyncio, json, sys, wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from stt_run import lab_env  # noqa: E402  (also puts Bayaan-server on sys.path)
from google import genai  # noqa: E402
from google.genai import types  # noqa: E402
from google.oauth2 import service_account  # noqa: E402
from gemini_stt.eof import transcribe_window  # noqa: E402


async def main():
    p = argparse.ArgumentParser()
    p.add_argument('wav'); p.add_argument('out')
    p.add_argument('--codes', default='bs-BA'); p.add_argument('--vocab')
    a = p.parse_args()
    env = lab_env()
    creds = service_account.Credentials.from_service_account_file(
        env['GOOGLE_APPLICATION_CREDENTIALS'], scopes=['https://www.googleapis.com/auth/cloud-platform'])
    client = genai.Client(enterprise=True, project=env['GOOGLE_CLOUD_PROJECT'], location='global', credentials=creds)
    vocab = None
    if a.vocab:
        vocab = [w.strip() for w in Path(a.vocab).read_text(encoding='utf-8').splitlines() if w.strip() and not w.startswith('#')]
    cfg = types.AudioTranscriptionConfig(language_codes=a.codes.split(','), custom_vocabulary=vocab, mode='VERBATIM')
    with wave.open(a.wav, 'rb') as w:
        pcm = w.readframes(w.getnframes())
    text = await transcribe_window(client, model='gemini-3.5-transcribe-preview', pcm=pcm, audio_config=cfg)
    Path(a.out).write_text(json.dumps(dict(wav=Path(a.wav).name, codes=a.codes, transcript=text), ensure_ascii=False, indent=1), encoding='utf-8')
    print(a.out, len(text.split()), 'words')


if __name__ == '__main__':
    asyncio.run(main())
