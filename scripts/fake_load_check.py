import ast
import asyncio
import json
import logging
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional


SERVER_DIR = Path(__file__).resolve().parents[1]
MAIN_PATH = SERVER_DIR / "main.py"
sys.path.insert(0, str(SERVER_DIR))

from resource_management import ResourceManager
from text_processing import extract_complete_sentences


def load_main_symbols(*names):
    source = MAIN_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source)
    selected = [
        node for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name in names
    ]
    module = ast.Module(body=selected, type_ignores=[])
    ast.fix_missing_locations(module)
    namespace = {
        "asyncio": asyncio,
        "dataclass": dataclass,
        "Dict": Dict,
        "extract_complete_sentences": extract_complete_sentences,
        "logger": logging.getLogger("fake-load-main-helpers"),
        "Optional": Optional,
        "os": __import__("os"),
        "TrackKey": tuple[str, str],
        "uuid": uuid,
    }
    exec(compile(module, str(MAIN_PATH), "exec"), namespace)
    return namespace


class FakeSTTStream:
    def __init__(self):
        self.closed = False

    async def aclose(self):
        self.closed = True


class FakeSTTProvider:
    def stream(self):
        return FakeSTTStream()


async def run_fake_room(room_index, helpers):
    TranscriptAccumulator = helpers["TranscriptAccumulator"]
    collect_translation_segments = helpers["collect_translation_segments"]
    cancel_participant_track_states = helpers["cancel_participant_track_states"]

    manager = ResourceManager()
    await manager.__aenter__()
    provider = FakeSTTProvider()
    participant_id = f"speaker-{room_index}"
    stop_event = asyncio.Event()
    participant_tasks = {}
    transcript_accumulators = {}

    async def fake_track(track_id):
        async with manager.stt_manager.create_stream(provider, participant_id, track_id):
            accumulator = TranscriptAccumulator()
            transcript_accumulators[(participant_id, track_id)] = accumulator
            collect_translation_segments(accumulator, "a" * 5000, max_accumulated_chars=4000)
            await manager.heartbeat_monitor.update_heartbeat(participant_id, f"session-{room_index}")
            await asyncio.sleep(0.05)
            await stop_event.wait()

    for track_id in ("track-a", "track-b"):
        participant_tasks[(participant_id, track_id)] = manager.task_manager.create_task(
            fake_track(track_id),
            name=f"fake-room-{room_index}-{track_id}",
        )

    await asyncio.sleep(0.15)
    cancel_participant_track_states(participant_tasks, transcript_accumulators, participant_id)
    stop_event.set()
    await manager.shutdown()
    verification = await manager.verify_cleanup_complete()

    return {
        "room": room_index,
        "verification": verification,
        "task_count": len(manager.task_manager.get_active_tasks()),
        "stream_count": len(manager.stt_manager._streams),
        "accumulator_count": len(transcript_accumulators),
        "heartbeat_count": len(manager.heartbeat_monitor.participants),
    }


async def main():
    logging.basicConfig(level=logging.WARNING)
    helpers = load_main_symbols(
        "TranscriptAccumulator",
        "collect_translation_segments",
        "cancel_track_state",
        "cancel_participant_track_states",
    )
    results = await asyncio.gather(*(run_fake_room(index, helpers) for index in range(25)))
    totals = {
        "rooms": len(results),
        "active_task_count": sum(result["task_count"] for result in results),
        "active_stream_count": sum(result["stream_count"] for result in results),
        "accumulator_count": sum(result["accumulator_count"] for result in results),
        "heartbeat_count": sum(result["heartbeat_count"] for result in results),
        "cleanup_complete": all(result["verification"]["cleanup_complete"] for result in results),
    }
    print(json.dumps(totals, indent=2, sort_keys=True))
    if any(value for key, value in totals.items() if key.endswith("_count")):
        raise SystemExit(1)
    if not totals["cleanup_complete"]:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
