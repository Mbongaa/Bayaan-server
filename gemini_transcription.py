"""Gemini 3.5 hybrid transcription, independent of rooms and GPT prompts.

The public stream API is ``stream()``, ``push_frame()``, ``end_input()``, async
iteration, and ``aclose()``. The caller drains the iterator after ``end_input``
before closing it. Audio must be mono PCM16 at 16 kHz. Source captions may show
drafts; ``forward_hybrid_sentences`` translates authoritative finals only.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Mapping

from gemini_stt.buffer import forward_hybrid_sentences
from gemini_stt.hybrid import GeminiHybridSTT
from gemini_stt.recovery import RecoveryError, RecoveryOptions

__all__ = [
    'GeminiHybridSTT', 'RecoveryOptions', 'RecoveryError', 'create_gemini_stt', 'create_gemini_provider',
    'gemini_provider_options', 'forward_hybrid_sentences',
]


def gemini_provider_options(language_hint: str, env: Mapping[str, str] | None = None) -> dict:
    """Read only Google settings. Language never selects LiveKit credentials.

Vertex uses the same global Enterprise endpoint and preview model as the tested
lab. Developer API remains an explicit server setting, never a client toggle.
Credentials are returned only to the SDK factory and must not be logged.
"""
    env = os.environ if env is None else env
    if not isinstance(language_hint, str) or not language_hint.strip():
        raise ValueError('Gemini requires an explicit supported language hint')
    options = dict(language=language_hint, language_codes=[language_hint],
                   sample_rate=16000, transcription_mode='VERBATIM')
    backend = env.get('GEMINI_STT_BACKEND', 'vertex').lower()
    if backend == 'vertex':
        credentials_json = env.get('GOOGLE_APPLICATION_CREDENTIALS_JSON')
        credentials_path = env.get('GOOGLE_APPLICATION_CREDENTIALS')
        project = env.get('GOOGLE_CLOUD_PROJECT') or env.get('GOOGLE_PROJECT_ID')
        if credentials_json:
            try:
                info = json.loads(credentials_json)
            except (TypeError, ValueError):
                raise ValueError('GOOGLE_APPLICATION_CREDENTIALS_JSON is not valid JSON') from None
            options['credentials'] = info
        elif credentials_path:
            try:
                path = Path(credentials_path).expanduser().resolve(strict=True)
                info = json.loads(path.read_text(encoding='utf-8-sig'))
            except (OSError, ValueError):
                raise ValueError('GOOGLE_APPLICATION_CREDENTIALS must name a readable service account JSON file') from None
            options['credentials_path'] = str(path)
        else:
            raise ValueError('Vertex transcription requires GOOGLE_APPLICATION_CREDENTIALS_JSON or GOOGLE_APPLICATION_CREDENTIALS')
        if not isinstance(info, dict) or info.get('type') != 'service_account':
            raise ValueError('Vertex transcription credentials must contain a service account')
        project = project or info.get('project_id')
        if not isinstance(project, str) or not project.strip():
            raise ValueError('Vertex transcription requires GOOGLE_CLOUD_PROJECT or service account project_id')
        options.update(vertexai=True, project=project, location='global',
                       model='gemini-3.5-transcribe-live-preview')
    elif backend == 'developer':
        key = env.get('GOOGLE_API_KEY') or env.get('GEMINI_API_KEY')
        if not key:
            raise ValueError('Developer transcription requires GOOGLE_API_KEY or GEMINI_API_KEY')
        options.update(vertexai=False, api_key=key, model='gemini-3.5-transcribe-live')
    else:
        raise ValueError('GEMINI_STT_BACKEND must be vertex or developer')
    return options


def create_gemini_stt(language_hint: str, env: Mapping[str, str] | None = None) -> GeminiHybridSTT:
    return GeminiHybridSTT(**gemini_provider_options(language_hint, env))


def create_gemini_provider(*, language: str, language_hint: str,
                           env: Mapping[str, str] | None = None) -> GeminiHybridSTT:
    """Keep the selected classroom language on captions, with a Google-only hint."""
    if not isinstance(language, str) or not language.strip():
        raise ValueError('The classroom transcription language is required')
    options = gemini_provider_options(language_hint, env)
    options['language'] = language
    return GeminiHybridSTT(**options)
