"""Preparation must never create providers or change the legacy worker path."""
import ast
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, call, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import gemini_prewarm


class GeminiPrewarmTests(unittest.TestCase):
    def test_configured_backends_import_modules_without_constructing_providers(self):
        configurations = (
            {"GOOGLE_APPLICATION_CREDENTIALS": "not-read-during-prewarm.json"},
            {"GOOGLE_APPLICATION_CREDENTIALS_JSON": "not-parsed-during-prewarm"},
            {"GEMINI_STT_BACKEND": "developer", "GOOGLE_API_KEY": "test-only"},
            {"GEMINI_STT_BACKEND": "developer", "GEMINI_API_KEY": "test-only"},
        )
        for env in configurations:
            with self.subTest(keys=list(env)), \
                    patch.object(gemini_prewarm.importlib, "import_module") as importer:
                self.assertTrue(gemini_prewarm.prewarm_gemini(env))
                self.assertEqual(importer.call_args_list,
                                 [call("gemini_transcription"), call("gemini_track")])
                self.assertEqual(importer.return_value.mock_calls, [])

    def test_no_google_configuration_does_not_import_optional_modules(self):
        configurations = (
            {}, {"SPEECHMATICS_API_KEY": "legacy-only"},
            {"GOOGLE_CLOUD_PROJECT": "project-without-credentials"},
            {"GOOGLE_API_KEY": "ignored-by-default-vertex-backend"},
            {"GEMINI_STT_BACKEND": "developer", "GOOGLE_APPLICATION_CREDENTIALS": "ignored"},
        )
        for env in configurations:
            with self.subTest(keys=list(env)), \
                    patch.object(gemini_prewarm.importlib, "import_module") as importer:
                self.assertFalse(gemini_prewarm.prewarm_gemini(env))
                importer.assert_not_called()

    def test_missing_optional_dependency_does_not_disable_legacy_rooms(self):
        with patch.object(gemini_prewarm.importlib, "import_module", side_effect=ImportError("missing")), \
                self.assertLogs("transcriber", level="WARNING"):
            self.assertFalse(gemini_prewarm.prewarm_gemini({"GOOGLE_APPLICATION_CREDENTIALS": "test.json"}))

    def test_production_prewarm_retains_its_existing_vad(self):
        main_path = Path(__file__).resolve().parents[1] / "main.py"
        tree = ast.parse(main_path.read_text(encoding="utf-8"))
        prewarm = next(node for node in tree.body
                       if isinstance(node, ast.FunctionDef) and node.name == "prewarm")
        module = ast.fix_missing_locations(ast.Module(body=[prewarm], type_ignores=[]))
        vad = Mock()
        namespace = {"JobProcess": object, "silero": SimpleNamespace(VAD=vad)}
        exec(compile(module, str(main_path), "exec"), namespace)
        proc = SimpleNamespace(userdata={})
        with patch.object(gemini_prewarm, "prewarm_gemini") as prepare:
            namespace["prewarm"](proc)
            prepare.assert_called_once_with()
        vad.load.assert_called_once_with()
        self.assertIs(proc.userdata["vad"], vad.load.return_value)


if __name__ == "__main__":
    unittest.main()
