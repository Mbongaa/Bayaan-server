# Gemini session continuation — September 12, 2026

The classroom room and GPT translator outlive individual Google connections.
Gemini 3.5 Transcribe Live documents a ten-minute session limit on both Vertex
and the Developer API. The adapter rotates at the next committed audio boundary
after 480 seconds of connection time or sent audio. The sentence buffer, GPT
Khutba prompt and sliding context remain alive across this rotation.

## Production failure and correction

The previous release (`bea2f08`) already contained that rotation and replay code.
Production logs instead showed repeated missing authoritative live finals for
individual activities, including failures well before ten minutes. Reconnecting
and replaying the same activity did not always produce a final. Its sixty-second
recovery deadline then stopped the entire track.

A sealed activity with no live final now follows this path:

1. Wait the existing ten seconds for the live final.
2. Close and await the live receiver. Use a final that already arrived if present.
3. Otherwise send only that activity's retained PCM (at most sixteen seconds) to
   Google's matching Gemini 3.5 file transcription endpoint, with the same
   credentials, language hint and VERBATIM configuration.
4. Accept exactly one normally completed STOP candidate. An explicitly completed
   empty result is valid; an absent, blocked or truncated result is not.
5. Commit the activity once. Keep incoming audio in order and open a fresh live
   connection for the following activity. Already committed audio is not replayed.

The file request has a forty-second limit inside the existing shared sixty-second
recovery/Stop deadline; the worker STT drain remains sixty-five seconds. Real
Vertex probes needed 21.6–22.5 seconds to return an empty result for sixteen
seconds of quiet, nonzero PCM. The previous twenty-second file deadline was too
short. The file endpoint is a recovery path, not a replacement for normal live
transcription. Provider failures can still exceed the bounded recovery budget;
ambiguous audio is never silently committed as empty.

Punctuation/16-second segmentation, the minimum-three-word sentence buffer,
translation prompts and Speechmatics routing are unchanged. No environment
variable or client feature flag is required for this fix.

## Plugin and native resumption assessment

Google GenAI 2.22.0 does not reconnect automatically. LiveKit's newer
`google.beta.GeminiSTT` has framework retries for GoAway/session expiry, but its
1.8.1 release requires a newer Agents SDK than the deployed 1.2.1. Its stream
does not provide this adapter's retained-audio replay ledger. It is not a drop-in
replacement for the current final-only commit policy.

Google's general Live API documents resumption handles. A bounded real Vertex
Transcribe Live probe accepted SessionResumptionConfig and produced transcripts,
but emitted no resumable handle. Native resumption is therefore unverified for
this integration. The adapter uses fresh connections and its own audio ledger;
it does not mix resume handles with replay, which could duplicate buffered audio.

## Diagnostics and validation

Logs identify each connection, requested GoAway, scheduled rotation, file rescue
and minute checkpoint. Checkpoints contain received/buffered audio durations and
commit/retry/rotation counters, without audio or credentials.

Regression tests cover missing finals during continuing capture, completed empty
audio, late-final races, exact retained PCM, ambiguous/timeout/cancelled file
requests, sentence-buffer continuity, GoAway replay and two default rotations.
Real-provider and deployment results are recorded with the release verification
artifacts. A short successful recording alone does not validate long sessions.

## Sources

- [Vertex Gemini 3.5 Transcribe](https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/gemini/3-5-transcribe)
- [Developer Gemini 3.5 Transcribe](https://ai.google.dev/gemini-api/docs/models/gemini-3.5-transcribe)
- [Google Live session management](https://ai.google.dev/gemini-api/docs/live-api/session-management)
- [Google Live server message ordering](https://ai.google.dev/api/live#BidiGenerateContentServerContent)
- [LiveKit Google beta API](https://docs.livekit.io/reference/python/livekit/plugins/google/beta/index.html)
- [LiveKit beta Gemini stream source](https://github.com/livekit/agents/blob/main/livekit-plugins/livekit-plugins-google/livekit/plugins/google/beta/gemini_stt.py)
