"""Run the prepared production worker against the existing localhost LiveKit.

This is a separate launch target: production main.py does not import it. Only
database/webhook boundaries are replaced; main.entrypoint, Translator and the
production Khutba PromptBuilder are used unchanged. No Supabase socket is opened.

Run with this server's .venv: python local_preview.py [--check]
"""
from __future__ import annotations

import argparse
import importlib
import importlib.metadata
import json
import os
from pathlib import Path
import re
import sys
from types import ModuleType, SimpleNamespace

SERVER = Path(__file__).resolve().parent
LAB = SERVER.parents[1] / "local-stt-lab"
LOCAL_URL = "ws://127.0.0.1:7880"
AGENT_NAME = "bayaan-classroom-preview"
HEALTH_PORT = 18082
ROOM_PATTERN = re.compile(r"local-classroom-[A-Za-z0-9-]{1,100}\Z")
SYSTEM_KEYS = {
    "PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP",
    "USERPROFILE", "APPDATA", "LOCALAPPDATA", "NUMBER_OF_PROCESSORS", "OS",
    "PROCESSOR_ARCHITECTURE",
}
SECRET_KEYS = {
    "OPENAI_API_KEY", "SPEECHMATICS_API_KEY", "GOOGLE_APPLICATION_CREDENTIALS",
    "GOOGLE_APPLICATION_CREDENTIALS_JSON", "GOOGLE_CLOUD_PROJECT",
}


def read_settings(path: Path) -> dict[str, str]:
    """Read only the named file; never search parents or interpolate variables."""
    values = {}
    if not path.is_file():
        return values
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = (part.strip() for part in line.split("=", 1))
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key] = value
    return values


def build_environment(inherited=None, settings=None) -> dict[str, str]:
    inherited = os.environ if inherited is None else inherited
    settings = read_settings(LAB / ".env.local") if settings is None else settings
    result = {key: value for key, value in inherited.items() if key.upper() in SYSTEM_KEYS}
    # Cloud, Supabase and LiveKit credentials from a parent shell are deliberately
    # not inherited. The lab's explicit model-key file is the sole secret input.
    result.update({key: settings[key] for key in SECRET_KEYS if settings.get(key)})
    credential_path = result.get("GOOGLE_APPLICATION_CREDENTIALS")
    if credential_path:
        path = Path(credential_path).expanduser()
        result["GOOGLE_APPLICATION_CREDENTIALS"] = str(
            (path if path.is_absolute() else LAB / path).resolve()
        )
    result.update({
        "LIVEKIT_URL": LOCAL_URL,
        "LIVEKIT_API_KEY": "devkey",
        "LIVEKIT_API_SECRET": "secret",
        "SUPABASE_URL": "http://127.0.0.1:9",
        "SUPABASE_SERVICE_ROLE_KEY": "local-preview-disabled",
        "PYTHON_DOTENV_DISABLED": "1",
        "PYTHONUNBUFFERED": "1",
        "PYTHONUTF8": "1",
        "OTEL_SDK_DISABLED": "true",
        "BAYAAN_LOCAL_PREVIEW": "1",
        "DEFAULT_MOSQUE_ID": "0",
        "GOOGLE_CLOUD_LOCATION": "global",
        "GEMINI_STT_BACKEND": "vertex",
    })
    return result


def assert_isolated_environment() -> None:
    expected = {
        "LIVEKIT_URL": LOCAL_URL, "LIVEKIT_API_KEY": "devkey",
        "LIVEKIT_API_SECRET": "secret", "SUPABASE_URL": "http://127.0.0.1:9",
        "SUPABASE_SERVICE_ROLE_KEY": "local-preview-disabled",
        "PYTHON_DOTENV_DISABLED": "1", "BAYAAN_LOCAL_PREVIEW": "1",
    }
    if any(os.environ.get(key) != value for key, value in expected.items()):
        raise RuntimeError("Local preview requires its fixed localhost environment.")
    if any(key.startswith("CLASSROOM_SUPABASE_") or key in {"HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"}
           for key in os.environ):
        raise RuntimeError("Local preview refuses inherited database or proxy configuration.")


async def _no_database_result(*args, **kwargs):
    return None


async def _no_database_write(*args, **kwargs):
    return False


def install_local_boundaries() -> None:
    """Install process-local substitutes before any production module imports."""
    assert_isolated_environment()
    existing = sys.modules.get("database")
    if existing is not None and not getattr(existing, "LOCAL_PREVIEW_BOUNDARY", False):
        raise RuntimeError("Database was imported before local preview isolation.")
    database = ModuleType("database")
    database.LOCAL_PREVIEW_BOUNDARY = True
    for name in (
        "query_room_by_name", "query_classroom_by_id", "get_active_session_for_room",
        "query_prompt_template_for_room", "ensure_active_session", "close_database_connections",
    ):
        setattr(database, name, _no_database_result)
    for name in ("close_room_session", "update_session_heartbeat", "store_transcript_in_database"):
        setattr(database, name, _no_database_write)
    database._pool = SimpleNamespace(_local=SimpleNamespace(session=None), close=_no_database_result)
    sys.modules["database"] = database
    webhook = ModuleType("webhook_handler")
    webhook.LOCAL_PREVIEW_BOUNDARY = True
    webhook.get_room_context = lambda room_name: {}
    sys.modules["webhook_handler"] = webhook


def load_production_main():
    install_local_boundaries()
    return importlib.import_module("main")


def allowed_room(name) -> bool:
    return isinstance(name, str) and bool(ROOM_PATTERN.fullmatch(name))


async def request_fnc(request):
    assert_isolated_environment()
    if not allowed_room(getattr(request.room, "name", None)) or request.agent_name != AGENT_NAME:
        await request.reject()
        return
    await request.accept(
        name="Local classroom worker", identity="local-classroom-agent-" + request.id,
        attributes={"local_preview": "true"},
    )


async def entrypoint(job):
    assert_isolated_environment()
    if not allowed_room(getattr(job.room, "name", None)):
        raise RuntimeError("Local preview refuses rooms outside local-classroom-*.")
    # LiveKit's assignment URL is distinct from its registration URL. Verify both
    # before the actual entrypoint can call JobContext.connect (Agents 1.2.1).
    if getattr(getattr(job, "_info", None), "url", None) != LOCAL_URL:
        raise RuntimeError("Local preview refuses a non-local job assignment.")
    production = load_production_main()
    await production.entrypoint(job)


def worker_load(worker):
    return min(len(worker.active_jobs) / 2, 1.0)


def prewarm(proc):
    assert_isolated_environment()
    from gemini_prewarm import prewarm_gemini
    prewarm_gemini()


def build_worker_options():
    from livekit.agents import WorkerOptions
    return WorkerOptions(
        entrypoint_fnc=entrypoint, request_fnc=request_fnc, prewarm_fnc=prewarm,
        agent_name=AGENT_NAME, ws_url=LOCAL_URL, api_key="devkey", api_secret="secret",
        host="127.0.0.1", port=HEALTH_PORT, http_proxy=None,
        num_idle_processes=0, load_threshold=0.9,
        load_fnc=worker_load,
        drain_timeout=120, shutdown_process_timeout=150,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Validate imports and isolation, then exit.")
    args = parser.parse_args()
    environment = build_environment()
    os.environ.clear()
    os.environ.update(environment)
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    assert_isolated_environment()
    production = load_production_main()  # Plugin registration must occur on the main thread.
    print(json.dumps({
        "localPreview": True, "workerModule": str(Path(production.__file__).resolve()),
        "livekitUrl": LOCAL_URL, "agentName": AGENT_NAME, "healthPort": HEALTH_PORT,
        "database": "disabled", "webhooks": "disabled", "prompt": "production PromptBuilder",
        "livekitAgentsVersion": importlib.metadata.version("livekit-agents"),
        "modelCredentialsPresent": {key: bool(environment.get(key)) for key in
            ("OPENAI_API_KEY", "SPEECHMATICS_API_KEY", "GOOGLE_APPLICATION_CREDENTIALS",
             "GOOGLE_APPLICATION_CREDENTIALS_JSON")},
    }), flush=True)
    if args.check:
        return
    from livekit.agents import cli
    # Do not forward URL/credential flags into the framework CLI.
    sys.argv = [sys.argv[0], "start"]
    cli.run_app(build_worker_options())


if __name__ == "__main__":
    main()
