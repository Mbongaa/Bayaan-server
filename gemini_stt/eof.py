"""Confirm a sealed, uncommitted audio window through Google's Transcribe endpoint.

Google Live can acknowledge a short nonzero audio activity without emitting an
input_transcription. ACTIVITY_END alone is not a text checkpoint. The documented
file endpoint can instead return a completed transcription, including an empty
STOP candidate. The caller closes the live reader before this single request so
two endpoints cannot commit the same audio. This applies both during continuous
recording and at EOF; no speculative live text is treated as final.
https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/gemini/3-5-transcribe
"""
import io
import wave
from google.genai import types

FILE_MODELS = {
    'gemini-3.5-transcribe-live-preview': 'gemini-3.5-transcribe-preview',
    'gemini-3.5-transcribe-live': 'gemini-3.5-transcribe',
}
# Keep the existing name for EOF probe/test compatibility. The same bounded
# request now also rescues sealed activities during a continuing recording.
# A real 16-second quiet window required 21.6 seconds on Vertex. Allow 40
# seconds here, still inside the shared 60-second recovery/Stop deadline and
# the worker's 65-second STT drain. A short request must not end a long lecture.
EOF_REQUEST_TIMEOUT_SECONDS = 40.0


def wav_bytes(pcm):
    output = io.BytesIO()
    with wave.open(output, 'wb') as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16000)
        audio.writeframes(pcm)
    return output.getvalue()


def completed_transcription(response):
    """Reject absent, blocked, truncated or ambiguous responses; never guess text."""
    feedback = getattr(response, 'prompt_feedback', None)
    if feedback and getattr(feedback, 'block_reason', None):
        raise ValueError('Google file transcription was blocked')
    candidates = getattr(response, 'candidates', None)
    if not candidates or len(candidates) != 1:
        raise ValueError('Google file transcription did not return one final candidate')
    candidate = candidates[0]
    if candidate.finish_reason != types.FinishReason.STOP:
        raise ValueError('Google file transcription did not complete normally')
    parts = getattr(getattr(candidate, 'content', None), 'parts', None) or []
    texts = []
    for part in parts:
        annotation = getattr(part, 'audio_transcription', None)
        text = getattr(annotation, 'text', None) if annotation is not None else None
        if text is None:
            text = getattr(part, 'text', None)
        if not isinstance(text, str):
            raise ValueError('Google file transcription returned a non-text part')
        texts.append(text)
    # A real completed STOP candidate with no parts is Google's empty result,
    # distinct from an absent live input_transcription or a missing candidate.
    return ''.join(texts)


async def transcribe_window(client, *, model, pcm, audio_config):
    response = await client.aio.models.generate_content(
        model=model,
        contents=[types.Part.from_bytes(data=wav_bytes(pcm), mime_type='audio/wav')],
        config=types.GenerateContentConfig(
            audio_transcription_config=audio_config,
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        ),
    )
    return completed_transcription(response)


# Existing local EOF verification tools may import this name.
transcribe_eof = transcribe_window
