"""Load Gemini modules before audio starts, without creating any providers."""
import importlib
import logging
import os
from typing import Mapping


def prewarm_gemini(env: Mapping[str, str] | None = None) -> bool:
    env = os.environ if env is None else env
    backend = env.get("GEMINI_STT_BACKEND", "vertex").lower()
    credential_keys = {
        "vertex": ("GOOGLE_APPLICATION_CREDENTIALS", "GOOGLE_APPLICATION_CREDENTIALS_JSON"),
        "developer": ("GOOGLE_API_KEY", "GEMINI_API_KEY"),
    }.get(backend, ())
    if not any(env.get(key) for key in credential_keys):
        return False

    try:
        importlib.import_module("gemini_transcription")
        importlib.import_module("gemini_track")
    except ImportError:
        # A missing optional Google dependency must not disable legacy rooms.
        # Selecting Gemini will still report the import failure on its own path.
        logging.getLogger("transcriber").warning(
            "Gemini module preparation failed; its source initialization will report the dependency error."
        )
        return False
    return True
