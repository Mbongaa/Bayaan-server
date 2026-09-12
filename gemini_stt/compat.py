"""Speech provenance across the existing Agents1.2 and lab Agents1.8 runtimes."""
from dataclasses import dataclass
from livekit.agents import stt


@dataclass
class SpeechData(stt.SpeechData):
    # Agents1.2 has no metadata field. A subclass preserves SDK identity without
    # mutating SDK classes; Agents1.8 already has it and permits this override.
    metadata: dict | None = None
