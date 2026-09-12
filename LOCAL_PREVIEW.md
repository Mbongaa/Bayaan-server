# Local classroom preview

From this server directory, run:

```powershell
.\.venv\Scripts\python.exe .\local_preview.py --check
.\.venv\Scripts\python.exe .\local_preview.py
```

The existing local lab must already provide LiveKit at `ws://127.0.0.1:7880`. This launcher does not restart it. The worker registers with the distinct agent name `bayaan-classroom-preview`, accepts only `local-classroom-*` rooms, and exposes its framework health endpoint at `http://127.0.0.1:18082/`.

The classroom developer page `/local-transcription-test` uses this named worker. Its token endpoint must dispatch this exact agent name. Room metadata supplies `transcription_language` and `translation_language`; participant attributes carry the usual teacher `speaking_language` and listener `captions_language`. Select `ar` for Arabic (Fusha old), or `ar-gemini` for Arabic (Full Dialects).

`local_preview.py` runs the actual prepared `main.entrypoint`, `Translator`, and Khutba `PromptBuilder`. It imports process-local database and webhook substitutes first: room/template queries return no database data, and storage/session writes return false. It consequently exercises the default production Khutba prompt, without room-specific database template overrides. Production's Docker launch target remains `main_production.py start`, which calls `main.build_worker_options`; neither imports these preview substitutes.

Only explicitly allowed model credentials are read from `../../local-stt-lab/.env.local`. LiveKit credentials are always overwritten with local development credentials. Parent-shell secrets, production database variables and HTTP proxies are discarded; dotenv loading is disabled. No copied key values are written to source or printed. The runner rejects remote job-assignment URLs as well as remote registration URLs.

Press Ctrl+C in this worker's terminal after leaving the preview room. Source edits require restarting this worker; the runner intentionally does not reload the existing lab or any other process. This local launch option makes no production deploy, Git operation, database migration or remote room connection.
