"""Local preview isolation must hold even in a production-configured shell."""
import asyncio
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import local_preview as preview


class PreviewIsolationTests(unittest.TestCase):
    def test_environment_drops_remote_infrastructure_and_inherited_model_keys(self):
        inherited = {
            "PATH": "system-path", "LIVEKIT_URL": "wss://production.example",
            "SUPABASE_SERVICE_ROLE_KEY": "production-db", "CLASSROOM_SUPABASE_URL": "remote",
            "OPENAI_API_KEY": "inherited-key", "HTTPS_PROXY": "http://remote-proxy",
        }
        env = preview.build_environment(inherited, {"OPENAI_API_KEY": "explicit-lab-key",
            "LIVEKIT_URL": "wss://ignored.example", "CLASSROOM_SUPABASE_URL": "ignored"})
        self.assertEqual(env["PATH"], "system-path")
        self.assertEqual(env["OPENAI_API_KEY"], "explicit-lab-key")
        self.assertEqual(env["LIVEKIT_URL"], preview.LOCAL_URL)
        self.assertEqual(env["LIVEKIT_API_KEY"], "devkey")
        self.assertEqual(env["SUPABASE_SERVICE_ROLE_KEY"], "local-preview-disabled")
        self.assertNotIn("CLASSROOM_SUPABASE_URL", env)
        self.assertNotIn("HTTPS_PROXY", env)
        self.assertEqual(env["PYTHON_DOTENV_DISABLED"], "1")

    def test_missing_explicit_model_key_does_not_fall_back_to_inherited_secret(self):
        self.assertNotIn("OPENAI_API_KEY", preview.build_environment({"OPENAI_API_KEY": "secret"}, {}))

    def test_infrastructure_tampering_is_rejected(self):
        env = preview.build_environment({}, {})
        for key, value in (("LIVEKIT_URL", "wss://prod.example"),
                           ("SUPABASE_URL", "https://db.example"),
                           ("PYTHON_DOTENV_DISABLED", "0"),
                           ("HTTPS_PROXY", "http://proxy")):
            with self.subTest(key=key), patch.dict(os.environ, {**env, key: value}, clear=True):
                with self.assertRaises(RuntimeError):
                    preview.assert_isolated_environment()

    def test_database_and_webhook_boundaries_are_local_and_non_writing(self):
        with patch.dict(os.environ, preview.build_environment({}, {}), clear=True), patch.dict(sys.modules):
            sys.modules.pop("database", None)
            preview.install_local_boundaries()
            database = sys.modules["database"]
            self.assertTrue(database.LOCAL_PREVIEW_BOUNDARY)
            self.assertIsNone(asyncio.run(database.query_room_by_name("prod-room")))
            self.assertIsNone(asyncio.run(database.query_prompt_template_for_room(12)))
            self.assertFalse(asyncio.run(database.store_transcript_in_database("text")))
            self.assertEqual(sys.modules["webhook_handler"].get_room_context("prod-room"), {})

    def test_already_loaded_production_database_is_rejected(self):
        with patch.dict(os.environ, preview.build_environment({}, {}), clear=True), \
                patch.dict(sys.modules, {"database": SimpleNamespace()}):
            with self.assertRaisesRegex(RuntimeError, "Database was imported"):
                preview.install_local_boundaries()

    def test_room_filter_is_strict(self):
        self.assertTrue(preview.allowed_room("local-classroom-a09-123"))
        for name in ("local-stt-123", "classroom-123", "local-classroom-", "local-classroom-a\n", None):
            self.assertFalse(preview.allowed_room(name), name)

    def test_only_named_preview_jobs_are_accepted(self):
        async def run():
            for room_name, agent_name, accepted in (
                ("local-classroom-test", preview.AGENT_NAME, True),
                ("production-room", preview.AGENT_NAME, False),
                ("local-classroom-test", "production-agent", False),
            ):
                request = SimpleNamespace(room=SimpleNamespace(name=room_name), agent_name=agent_name,
                                          id="test", accept=AsyncMock(), reject=AsyncMock())
                await preview.request_fnc(request)
                self.assertEqual(request.accept.await_count, int(accepted))
                self.assertEqual(request.reject.await_count, int(not accepted))
        with patch.dict(os.environ, preview.build_environment({}, {}), clear=True):
            asyncio.run(run())

    def test_remote_job_assignment_is_rejected_before_loading_main(self):
        job = SimpleNamespace(room=SimpleNamespace(name="local-classroom-test"),
                              _info=SimpleNamespace(url="wss://remote.example"))
        with patch.dict(os.environ, preview.build_environment({}, {}), clear=True), \
                patch.object(preview, "load_production_main") as loader:
            with self.assertRaisesRegex(RuntimeError, "non-local job assignment"):
                asyncio.run(preview.entrypoint(job))
            loader.assert_not_called()

    def test_local_job_calls_actual_entrypoint_without_replacing_translator(self):
        job = SimpleNamespace(room=SimpleNamespace(name="local-classroom-test"),
                              _info=SimpleNamespace(url=preview.LOCAL_URL))
        production = SimpleNamespace(entrypoint=AsyncMock())
        with patch.dict(os.environ, preview.build_environment({}, {}), clear=True), \
                patch.object(preview, "load_production_main", return_value=production):
            asyncio.run(preview.entrypoint(job))
            production.entrypoint.assert_awaited_once_with(job)

    def test_preview_prewarm_only_prepares_google_modules_in_isolated_environment(self):
        proc = SimpleNamespace(userdata={})
        with patch.dict(os.environ, preview.build_environment({}, {}), clear=True), \
                patch("gemini_prewarm.prewarm_gemini") as prepare, \
                patch.object(preview, "load_production_main") as production_loader:
            preview.prewarm(proc)
            prepare.assert_called_once_with()
            production_loader.assert_not_called()
            self.assertEqual(proc.userdata, {})

    def test_preview_prewarm_refuses_nonlocal_environment(self):
        with patch.dict(os.environ, {}, clear=True), \
                patch("gemini_prewarm.prewarm_gemini") as prepare:
            with self.assertRaises(RuntimeError):
                preview.prewarm(SimpleNamespace(userdata={}))
            prepare.assert_not_called()

    def test_worker_options_use_preview_specific_prewarm(self):
        options = preview.build_worker_options()
        self.assertIs(options.prewarm_fnc, preview.prewarm)
        self.assertIs(options.entrypoint_fnc, preview.entrypoint)


if __name__ == "__main__":
    unittest.main()
