"""Exercise the real entrypoint's context lookup without starting room services."""
import ast
import json
import logging
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch


MAIN_PATH = Path(__file__).resolve().parents[1] / "main.py"


def load_context_lookup(namespace):
    tree = ast.parse(MAIN_PATH.read_text(encoding="utf-8"))
    entrypoint = next(node for node in tree.body
                      if isinstance(node, ast.AsyncFunctionDef) and node.name == "entrypoint")
    start = next(index for index, node in enumerate(entrypoint.body)
                 if isinstance(node, ast.Assign)
                 and any(isinstance(target, ast.Name) and target.id == "tenant_context"
                         for target in node.targets))
    context_try = entrypoint.body[start + 1]
    if not isinstance(context_try, ast.Try):
        raise AssertionError("The entrypoint context lookup must be tested as a complete block")
    wrapper = ast.parse("async def lookup():\n    pass\n").body[0]
    wrapper.body = [entrypoint.body[start], context_try,
                    ast.Return(value=ast.Name(id="tenant_context", ctx=ast.Load()))]
    module = ast.fix_missing_locations(ast.Module(body=[wrapper], type_ignores=[]))
    exec(compile(module, str(MAIN_PATH), "exec"), namespace)
    return namespace["lookup"]


class RoomContextFallbackTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.job = SimpleNamespace(
            room=SimpleNamespace(name="local-classroom-test", metadata=""),
            job=SimpleNamespace(room=SimpleNamespace(metadata=json.dumps({
                "transcription_language": "ar-gemini", "translation_language": "en",
                "context_window_size": 12,
            }))),
        )
        self.config = SimpleNamespace(
            supabase=SimpleNamespace(url="http://unused.local", service_role_key=""),
            classroom_supabase=None, test_mosque_id=1, test_room_id=2, default_mosque_id=0,
        )
        self.database = AsyncMock(return_value=None)
        self.webhook = Mock(return_value={})
        self.namespace = {
            "job": self.job, "config": self.config, "json": json, "os": os,
            "logger": logging.getLogger("test-room-context"),
            "query_room_by_name": self.database,
            "query_classroom_by_id": AsyncMock(return_value=None),
            "get_active_session_for_room": AsyncMock(return_value=None),
            "get_webhook_room_context": self.webhook,
        }

    async def lookup(self):
        return await load_context_lookup(self.namespace)()

    async def test_assignment_metadata_is_available_before_rtc_connect(self):
        context = await self.lookup()
        self.assertEqual(context["transcription_language"], "ar-gemini")
        self.assertEqual(context["translation_language"], "en")
        self.assertEqual(context["context_window_size"], 12)
        self.assertEqual(self.job.room.metadata, "")

    async def test_connected_rtc_metadata_is_preferred_over_assignment_snapshot(self):
        self.job.room.metadata = json.dumps({
            "transcription_language": "fr", "translation_language": "de",
            "context_window_size": 7,
        })
        context = await self.lookup()
        self.assertEqual(context["transcription_language"], "fr")
        self.assertEqual(context["translation_language"], "de")
        self.assertEqual(context["context_window_size"], 7)

    async def test_database_keeps_priority_over_webhook_and_both_metadata_sources(self):
        self.config.supabase.service_role_key = "test-only"
        self.database.return_value = {
            "id": "classroom-id", "transcription_language": "ar",
            "translation__language": "nl", "context_window_size": 6,
            "translation_prompt": "Existing room prompt",
        }
        self.webhook.return_value = {"transcription_language": "fr"}
        self.job.room.metadata = json.dumps({"transcription_language": "de"})
        context = await self.lookup()
        self.assertEqual(context["transcription_language"], "ar")
        self.assertEqual(context["translation_language"], "nl")
        self.assertEqual(context["translation_prompt"], "Existing room prompt")
        self.assertEqual(context["context_window_size"], 6)
        self.webhook.assert_not_called()

    async def test_webhook_keeps_priority_when_database_has_no_room(self):
        self.config.supabase.service_role_key = "test-only"
        self.webhook.return_value = {
            "transcription_language": "ur", "translation_language": "en",
            "session_id": "webhook-session",
        }
        self.job.room.metadata = json.dumps({"transcription_language": "de"})
        context = await self.lookup()
        self.database.assert_awaited_once_with("local-classroom-test")
        self.assertEqual(context["transcription_language"], "ur")
        self.assertEqual(context["session_id"], "webhook-session")

    async def test_database_failure_still_allows_assignment_metadata(self):
        self.config.supabase.service_role_key = "test-only"
        self.database.side_effect = RuntimeError("Test database unavailable")
        self.assertEqual((await self.lookup())["translation_language"], "en")

    async def test_missing_or_invalid_metadata_preserves_legacy_defaults(self):
        for assignment_metadata in ("", "invalid-json"):
            with self.subTest(metadata=assignment_metadata), \
                    patch.dict(os.environ, {"DEFAULT_MOSQUE_ID": "0"}):
                self.job.job.room.metadata = assignment_metadata
                context = await self.lookup()
                self.assertEqual(context["transcription_language"], "ar")
                self.assertEqual(context["translation_language"], "nl")


if __name__ == "__main__":
    unittest.main()
