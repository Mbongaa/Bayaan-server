import ast
import asyncio
import logging
import os
import unittest
import uuid
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional
from unittest.mock import patch

from text_processing import extract_complete_sentences


SERVER_DIR = Path(__file__).resolve().parents[1]
MAIN_PATH = SERVER_DIR / "main.py"


def load_main_symbols(*names):
    source = MAIN_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source)
    selected = [
        node for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        and node.name in names
    ]
    module = ast.Module(body=selected, type_ignores=[])
    ast.fix_missing_locations(module)
    namespace = {
        "asyncio": asyncio,
        "dataclass": dataclass,
        "Dict": Dict,
        "extract_complete_sentences": extract_complete_sentences,
        "logger": logging.getLogger("test-main-hardening"),
        "Optional": Optional,
        "os": os,
        "TrackKey": tuple[str, str],
        "uuid": uuid,
    }
    exec(compile(module, str(MAIN_PATH), "exec"), namespace)
    return namespace


class MainStaticHardeningTests(unittest.TestCase):
    def setUp(self):
        self.source = MAIN_PATH.read_text(encoding="utf-8")

    def test_audio_stream_is_bounded_and_closed_with_stt_drain(self):
        self.assertIn('AUDIO_STREAM_CAPACITY", 100', self.source)
        self.assertIn("rtc.AudioStream(track, capacity=audio_stream_capacity)", self.source)
        self.assertIn('hasattr(stt_stream, "end_input")', self.source)
        self.assertIn("await asyncio.wait_for(stt_task, timeout=stt_drain_timeout)", self.source)
        self.assertIn("await audio_stream.aclose()", self.source)

    def test_track_lifecycle_uses_track_keyed_tasks(self):
        self.assertIn("participant_tasks: Dict[TrackKey, asyncio.Task]", self.source)
        self.assertIn("participant_tasks[(participant.identity, track.sid)] = task", self.source)
        self.assertIn('@job.room.on("track_unsubscribed")', self.source)
        self.assertIn("close_participant_stream(participant_id, track_id)", self.source)

    def test_cleanup_path_uses_close_room_session_not_missing_query_database(self):
        self.assertIn("async def cleanup_room(", self.source)
        self.assertIn("await close_room_session(session_to_close)", self.source)
        self.assertNotIn("query_database", self.source)
        self.assertNotIn("perform_graceful_cleanup", self.source)

    def test_heartbeat_late_metadata_guard_and_worker_load_options_are_wired(self):
        self.assertIn("nonlocal heartbeat_task", self.source)
        self.assertIn("heartbeat_task is None or heartbeat_task.done()", self.source)
        self.assertIn("load_fnc=compute_worker_load", self.source)
        self.assertIn("load_threshold=_env_float", self.source)
        self.assertIn("drain_timeout=1800", self.source)


    def test_translation_source_language_follows_track_not_room_default(self):
        """Regression: a Croatian (or any non-Arabic) track must reach the translator
        with ITS language, not the room-level source_language / 'Arabic'. Otherwise the
        LLM is told it is translating an Arabic khutbah and refuses non-Arabic input."""
        forward_start = self.source.index("async def _forward_transcription")
        forward_end = self.source.index("async def transcribe_track")
        forward_source = self.source[forward_start:forward_end]

        self.assertIn("track_language: str,", forward_source)
        self.assertIn("language=track_language,", forward_source)
        self.assertIn(
            "await translate_confirmed(sentence, sentence_id, track_language)",
            forward_source,
        )
        self.assertIn('await translate_sentences([text], dict(translators), language, sentence_id)', self.source)
        self.assertNotIn("translate_sentences([sentence], translators, source_language", forward_source)
        self.assertIn("_forward_transcription(stt_stream, track, track_key, provider_lang)", self.source)
        self.assertIn("transcribe_track(participant, track, provider, participant_lang)", self.source)

        helpers = (SERVER_DIR / "translation_helpers.py").read_text(encoding="utf-8")
        self.assertIn("translator.translate(sentence, sentence_id, source_language=source_language)", helpers)

        translator_src = (SERVER_DIR / "translator.py").read_text(encoding="utf-8")
        self.assertIn("async def _initialize_prompt(self, source_language: Optional[str] = None):", translator_src)
        self.assertIn("_with_source_language_note", translator_src)
        self.assertNotIn("Translate provided Arabic text", translator_src)
        self.assertNotIn("Translate provided Arabic text", (SERVER_DIR / "prompt_builder.py").read_text(encoding="utf-8"))


class MainHelperTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancel_track_state_cancels_one_track_not_another(self):
        namespace = load_main_symbols(
            "TranscriptAccumulator",
            "cancel_track_state",
            "cancel_participant_track_states",
        )
        cancel_track_state = namespace["cancel_track_state"]
        TranscriptAccumulator = namespace["TranscriptAccumulator"]

        async def wait_forever():
            await asyncio.Event().wait()

        task_one = asyncio.create_task(wait_forever())
        task_two = asyncio.create_task(wait_forever())
        tasks = {
            ("speaker", "track-1"): task_one,
            ("speaker", "track-2"): task_two,
        }
        accumulators = {
            ("speaker", "track-1"): TranscriptAccumulator("text"),
            ("speaker", "track-2"): TranscriptAccumulator("text"),
        }

        cancelled = cancel_track_state(tasks, accumulators, "speaker", "track-1")
        await asyncio.sleep(0)

        self.assertIs(cancelled, task_one)
        self.assertTrue(task_one.cancelled())
        self.assertFalse(task_two.cancelled())
        self.assertNotIn(("speaker", "track-1"), tasks)
        self.assertIn(("speaker", "track-2"), tasks)
        self.assertNotIn(("speaker", "track-1"), accumulators)
        self.assertIn(("speaker", "track-2"), accumulators)

        task_two.cancel()
        with suppress(asyncio.CancelledError):
            await task_two

    async def test_cancel_participant_track_states_cancels_all_participant_tracks_only(self):
        namespace = load_main_symbols(
            "TranscriptAccumulator",
            "cancel_track_state",
            "cancel_participant_track_states",
        )
        cancel_participant_track_states = namespace["cancel_participant_track_states"]
        TranscriptAccumulator = namespace["TranscriptAccumulator"]

        async def wait_forever():
            await asyncio.Event().wait()

        speaker_task_one = asyncio.create_task(wait_forever())
        speaker_task_two = asyncio.create_task(wait_forever())
        other_task = asyncio.create_task(wait_forever())
        tasks = {
            ("speaker", "track-1"): speaker_task_one,
            ("speaker", "track-2"): speaker_task_two,
            ("other", "track-1"): other_task,
        }
        accumulators = {
            key: TranscriptAccumulator("text")
            for key in tasks
        }

        cancelled = cancel_participant_track_states(tasks, accumulators, "speaker")
        await asyncio.sleep(0)

        self.assertEqual(set(cancelled), {speaker_task_one, speaker_task_two})
        self.assertTrue(speaker_task_one.cancelled())
        self.assertTrue(speaker_task_two.cancelled())
        self.assertFalse(other_task.cancelled())
        self.assertEqual(set(tasks), {("other", "track-1")})
        self.assertEqual(set(accumulators), {("other", "track-1")})

        other_task.cancel()
        with suppress(asyncio.CancelledError):
            await other_task

    def test_accumulator_flushes_on_complete_sentence_and_char_cap(self):
        namespace = load_main_symbols("TranscriptAccumulator", "collect_translation_segments")
        TranscriptAccumulator = namespace["TranscriptAccumulator"]
        collect_translation_segments = namespace["collect_translation_segments"]

        complete_accumulator = TranscriptAccumulator()
        complete_segments = collect_translation_segments(
            complete_accumulator,
            "hello.",
            max_accumulated_chars=4000,
        )
        self.assertEqual([text for text, _ in complete_segments], ["hello."])
        self.assertEqual(complete_accumulator.accumulated_text, "")
        self.assertIsNone(complete_accumulator.current_sentence_id)

        forced_accumulator = TranscriptAccumulator()
        forced_segments = collect_translation_segments(
            forced_accumulator,
            "a" * 25,
            max_accumulated_chars=10,
        )
        self.assertEqual([text for text, _ in forced_segments], ["a" * 25])
        self.assertEqual(forced_accumulator.accumulated_text, "")
        self.assertIsNone(forced_accumulator.current_sentence_id)

    def test_compute_load_reaches_threshold_at_nine_active_jobs_when_configured(self):
        namespace = load_main_symbols("_env_int", "_env_float", "compute_worker_load")
        compute_worker_load = namespace["compute_worker_load"]

        class Worker:
            active_jobs = [object()] * 9

        with patch.dict(os.environ, {"MAX_ACTIVE_JOBS_PER_WORKER": "10"}):
            self.assertAlmostEqual(compute_worker_load(Worker()), 0.9)


if __name__ == "__main__":
    unittest.main()
