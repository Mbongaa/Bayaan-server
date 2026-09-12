"""Confirm only an uncommitted EOF window through Google's Transcribe endpoint.

Google Live can acknowledge a short nonzero audio activity without emitting an
input_transcription. ACTIVITY_END alone is not a text checkpoint. The documented
file endpoint can instead return a completed transcription, including an empty
STOP candidate. Verified on Vertex with quiet220ms and audible5s/10s controls.
https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/gemini/3-5-transcribe
"""
import io
import wave
from google.genai import types

FILE_MODELS = {
    'gemini-3.5-transcribe-live-preview': 'gemini-3.5-transcribe-preview',
    'gemini-3.5-transcribe-live': 'gemini-3.5-transcribe',
}
EOF_REQUEST_TIMEOUT_SECONDS = 20.0


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
        raise ValueError('Google EOF transcription was blocked')
    candidates = getattr(response, 'candidates', None)
    if not candidates or len(candidates) != 1:
        raise ValueError('Google EOF transcription did not return one final candidate')
    candidate = candidates[0]
    if candidate.finish_reason != types.FinishReason.STOP:
        raise ValueError('Google EOF transcription did not complete normally')
    parts = getattr(getattr(candidate, 'content', None), 'parts', None) or []
    texts = []
    for part in parts:
        annotation = getattr(part, 'audio_transcription', None)
        text = getattr(annotation, 'text', None) if annotation is not None else None
        if text is None:
            text = getattr(part, 'text', None)
        if not isinstance(text, str):
            raise ValueError('Google EOF transcription returned a non-text part')
        texts.append(text)
    # A real completed STOP candidate with no parts is Google's empty result,
    # distinct from an absent live input_transcription or a missing candidate.
    return ''.join(texts)


async def transcribe_eof(client, *, model, pcm, audio_config):
    response = await client.aio.models.generate_content(
        model=model,
        contents=[types.Part.from_bytes(data=wav_bytes(pcm), mime_type='audio/wav')],
        config=types.GenerateContentConfig(
            audio_transcription_config=audio_config,
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        ),
    )
    return completed_transcription(response)
