"""Google's own activity detection (no manual activities), for comparison with our cut policy.

Streams a WAV in 100 ms chunks at real time to gemini-3.5-transcribe-live-preview with
automatic activity detection on (low end-of-speech sensitivity, long silence), and
records input_transcription finals. One session; keep clips under 10 minutes.

usage: python auto_vad_run.py WAV OUT.json [--silence-ms 1000] [--prefix-ms 300]
"""
import argparse, asyncio, json, sys, time, wave
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
from stt_run import lab_env  # noqa: E402
from google import genai  # noqa: E402
from google.genai import types  # noqa: E402
from google.oauth2 import service_account  # noqa: E402


async def main():
    p = argparse.ArgumentParser()
    p.add_argument('wav'); p.add_argument('out')
    p.add_argument('--silence-ms', type=int, default=1000); p.add_argument('--prefix-ms', type=int, default=300)
    a = p.parse_args()
    env = lab_env()
    creds = service_account.Credentials.from_service_account_file(
        env['GOOGLE_APPLICATION_CREDENTIALS'], scopes=['https://www.googleapis.com/auth/cloud-platform'])
    client = genai.Client(enterprise=True, project=env['GOOGLE_CLOUD_PROJECT'], location='global', credentials=creds)
    config = types.LiveConnectConfig(
        response_modalities=['TEXT'],
        realtime_input_config=types.RealtimeInputConfig(automatic_activity_detection=types.AutomaticActivityDetection(
            disabled=False, end_of_speech_sensitivity=types.EndSensitivity.END_SENSITIVITY_LOW,
            silence_duration_ms=a.silence_ms, prefix_padding_ms=a.prefix_ms)),
        input_audio_transcription=types.AudioTranscriptionConfig(language_codes=['bs-BA'], mode='VERBATIM'))
    with wave.open(a.wav, 'rb') as w:
        pcm = w.readframes(w.getnframes())
    finals, t0 = [], time.monotonic()
    async with client.aio.live.connect(model='gemini-3.5-transcribe-live-preview', config=config) as session:
        async def receive():
            while True:
                async for message in session.receive():
                    content = getattr(message, 'server_content', None)
                    final = getattr(content, 'input_transcription', None) if content else None
                    if final is not None and (final.text or '').strip():
                        finals.append(dict(text=final.text, arrived=round(time.monotonic() - t0, 2)))

        reader = asyncio.create_task(receive())
        start = asyncio.get_running_loop().time()
        step = 3200  # 100 ms
        for i in range(0, len(pcm), step):
            await session.send_realtime_input(audio=types.Blob(data=pcm[i:i + step], mime_type='audio/pcm;rate=16000'))
            delay = start + (i + step) / 32000 - asyncio.get_running_loop().time()
            if delay > 0:
                await asyncio.sleep(delay)
        await session.send_realtime_input(audio=types.Blob(data=bytes(32000), mime_type='audio/pcm;rate=16000'))
        await session.send_realtime_input(audio_stream_end=True)
        await asyncio.sleep(15)
        reader.cancel()
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(json.dumps(dict(finals=finals, transcript=' '.join(f['text'].strip() for f in finals)),
                                      ensure_ascii=False, indent=1), encoding='utf-8')
    print(a.out, len(finals), 'finals')


asyncio.run(main())
