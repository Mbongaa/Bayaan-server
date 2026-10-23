"""Validate production options without importing STT clients or starting a worker."""
import ast
import os
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


class NamedDispatchTests(unittest.TestCase):
    def build(self):
        source = Path(__file__).resolve().parents[1] / "main.py"
        tree = ast.parse(source.read_text(encoding="utf-8"))
        function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "build_worker_options")
        namespace = dict(os=os, WorkerOptions=lambda **kwargs: SimpleNamespace(**kwargs),
                         entrypoint=object(), prewarm=object(), request_fnc=object(),
                         compute_worker_load=object(), _env_float=lambda *args, **kwargs: 0.9)
        exec(compile(ast.Module(body=[function], type_ignores=[]), str(source), "exec"), namespace)
        return namespace["build_worker_options"]()

    def test_named_worker_targets_the_configured_dispatch_name(self):
        with patch.dict(os.environ, {"LIVEKIT_AGENT_NAME": " bayaan-translator "}):
            self.assertEqual(self.build().agent_name, "bayaan-translator")

    def test_missing_or_blank_name_fails_before_registering_an_unnamed_worker(self):
        for value in ("", "  "):
            with patch.dict(os.environ, {"LIVEKIT_AGENT_NAME": value}):
                with self.assertRaisesRegex(ValueError, "LIVEKIT_AGENT_NAME"):
                    self.build()


if __name__ == "__main__":
    unittest.main()
