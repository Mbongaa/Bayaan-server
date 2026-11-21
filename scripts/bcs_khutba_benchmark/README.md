# Bosnian khutba benchmark (Nov 2026)

Offline tests of the live Bosnian path: Gemini 3.5 transcribe-live (Vertex) → hybrid cut policy → sentence buffer → gpt-6-luna. Everything runs locally against the real production modules; no room, worker or database is touched.

## What ships (bs, hr, sr only; every other language is unchanged)

| Change | Where | Evidence |
|---|---|---|
| An activity ends at the first **400 ms pause after 8 s** (still 16 s at most), not at Google's first draft punctuation | `gemini_stt/hybrid.py`, `gemini_transcription.py` | First 8 min of both khutbas: WER 10.9% → 5.6%, dropped words 73 → 12, mean word-to-final delay ~5.7 s (round 3) |
| **0.5 s of silence** before and after each activity's real audio | `gemini_stt/hybrid.py` | Dropped words −40%, no hallucinated lines (round 2) |
| Ordinals such as "31. godišnjicu" no longer end a sentence | `gemini_stt/sentences.py` | 7 false splits in the two khutbas (round 1) |
| Bosnian source notes in the translation prompt | `prompt_builder.py`, `translator.py` | Won all 4 decisive blind comparisons out of 16 (translation section) |
| A failed Gemini stream restarts for the same track instead of ending captions | `gemini_track.py`, `main.py` (all Gemini languages) | Unit tests, plus real local LiveKit room tests (round 3) |

## Material

Public YouTube audio of well-known Balkan speakers, shared by Emir (17 Nov 2026). Downloaded with the repo's `node_modules/yt-dlp-exec` binary and converted to 16 kHz mono WAV (`audio/`, git-ignored).

| Set | Clips |
|---|---|
| Khutbas (tuning) | Gazi Husrev-begova džamija, Sarajevo: Mensur Malkić (QOXCF5IEHD0, 14.5 min), Enes Ljevaković (FT8ehg7WVyc, 14 min) |
| Lectures (tuning) | 4-min clips (10:00–14:00): Kuduzović, Pezić, Gološ, Kadri |
| Held out | 4-min clips: Kapić, Poturković |

## Tools

- `stt_run.py WAV OUT --codes bs-BA` streams a WAV in real time through `GeminiHybridSTT` and the production sentence buffer. It records Google finals, with cut reasons, and the exact GPT input units.
- `file_run.py` transcribes a whole clip with the uncut file model (`gemini-3.5-transcribe-preview`). The result serves as a reference with no live cuts.
- `score.py REF ARM…` reports WER against the reference. It splits errors into those within one word of a live cut and those elsewhere.
- `pairdiff.py X Y CLIP` prints disagreeing passages in random A/B order for blind manual judging. It writes the key to a file.
- `tx_run.py UNITS OUT --target Dutch --key-file F` replays units in order with the production prompt, a 12-pair history and gpt-6-luna settings.
- `batch.sh ARM CODES [VOCAB]` runs all clips in parallel. Use `CLIPS=` and `FLAGS=` to select clips and options.

Keep at most about 8 live sessions open at once, and at most 2 parallel file-model requests: 24 parallel requests got Vertex 429 RESOURCE_EXHAUSTED. At 16 concurrent sessions, reconnects at the 480 s rotation were refused for 90 s, and two hr-HR khutba runs died. The Vertex project may be shared with production. Gemini Pro was not used as a judge, per the owner's rule against Gemini spending.

## Results: transcription

WER is measured against the uncut file transcript. That transcript has its own errors (for example "pokuran"), so the main decisions were also checked by blind manual A/B review. Run-to-run noise: two identical baseline runs scored 18.7% and 18.0%.

| Arm | Khutbas | Lectures | Held out | Blind A/B vs baseline |
|---|---|---|---|---|
| Production before (bs-BA, cut at first draft punctuation, ~5 s) | 11.1% | 18.7% | 13.6% | — |
| hr-HR hint | 10.0% | 17.4% | — | not judged |
| + 100-term custom vocabulary (hr-HR) | 14.4% | 25.5% | — | worse, dropped |
| Cut snapped to a pause | 9.0% | 17.2% | — | lost 10–15 (lectures) |
| **Snap + ignore punctuation before 8 s (superseded in round 3)** | **6.8%** | **16.1%** | **11.5%** | **won 15–10 (khutbas), 10–9 (held out)** |

- Within one word of a live cut, ~30% of words are wrong, versus ~13% elsewhere. Cutting at the first draft punctuation lands mid-sentence, because Google's interim arrives after the speaker has moved on.
- The cost of the new policy is about +1 s of average word-to-final delay (5.8 s vs 4.8 s).
- Sentence-splitter bug: a period after a number ending a sentence ("31. | godišnjicu", "u 34. | ajetu") split captions and triggered audio cuts, 7 times in the two khutbas. It is fixed in `gemini_stt/sentences.py`.
- Bosnian (bs-BA) is "Experimental" for this model and Croatian (hr-HR) is "Supported". hr-HR dropped fewer words, but its overall gain was within noise, so the hint stays bs-BA.

## Round 2: words lost at the seams between activities

- `edges.py ARMS CLIPS [N]` reports where deleted words sit. Even with the 8 s policy, 76% of the words lost on the khutbas were at a seam, including whole phrases such as "sallallahu alejhi ve sellem", "što im je Allah dželle šanuhu" and "zekjata u Bejtul-mal".
- `seam_probe.py` and `seam_probe2.py` re-transcribe the exact audio around those seams. They found no gap between windows: every sample reaches Google. Instead, the model drops speech that sits right at an activity's edge, mostly the first words and sometimes the last. The file model does the same on identical audio, and 0.5 s of silence around the audio recovered most of it.
- `sim_windows.py SRC CLIP OUT [--pad-start/--pad-end/--overlap]` re-transcribes a live run's windows with the file model. It is a cheap offline proxy that reproduces live output closely: 66 vs 55 deleted words.

Live results on both khutbas. All arms use the 8 s policy.

| Arm | WER | Deleted words | Hallucinated lines |
|---|---|---|---|
| No padding, two runs | 6.8% / 7.1% | 55 / 49 | 1 ("Ćao, how are you?") |
| **0.5 s silence before and after each activity, two runs (kept)** | **6.6% / 6.8%** | **31 / 32** | 0 |
| + cut at a pause after 13 s without punctuation | 7.2% | 36 | 0 (rejected, removed) |
| + 0.8 s audio overlap with text dedupe (offline) | 8.2% | 31 | — (dedupe garbles seams, rejected) |

The shipping configuration on the held-out clips: WER 13.6% → 9.9% and deleted words 25 → 21, against the old production.

Comparing two arms directly, with no reference: two runs of the same policy each have words the other lacks (53 vs 61). The padded run has 65 words the unpadded run lacks, against 42 the other way, and 75 vs 44 against the earlier run.

Reliability: a stream that cannot reconnect within its 90 s recovery budget used to end captions for the rest of the session. This happened to two hr-HR khutba runs at the 480 s rotation while 16 sessions were open. `run_gemini_track_with_restarts` now starts a fresh pipeline for the same track, up to 10 times with 1–15 s backoff. Tested in a real local LiveKit room in round 3.

## Round 3: Google guidance, cutting at real pauses, restart in a real room

**What Google and others advise** (web research, Nov 2026):
- Google documents no padding or segment length for manual activities.
- Its "hybrid" mode keeps server VAD on so prefix audio is padded, "preventing front-word truncation".
- VAD libraries pad speech segments by 0.4–0.5 s (faster-whisper 400 ms, LiveKit Silero 0.5 s), which matches our 0.5 s.
- Google's endless-streaming sample bridges restarts by replaying unfinalized audio, not by deduping text, which matches our overlap result.

**Google's own activity detection** (`auto_vad_run.py`: VAD on, low end sensitivity, 1 s silence):
- Khutba A: 2.1% WER and no seam losses, but a caption could wait 51 s.
- Khutba B: one single final for the whole 8 minutes.
- Unusable for live captions. It did show that cutting at real pauses is what avoids lost words.

**Our 8–16 s bounds, cutting at a real pause** (`--pause-cut`; `sim_policy.py` shows that 400 ms pauses occur within ~1 s after 8 s). Live, first 8 min of both khutbas, all with 0.5 s padding:

| Policy | WER | Dropped words (at seams) | Mean delay |
|---|---|---|---|
| Original production (first draft punctuation) | 10.9% | 73 (61) | ~4.9 s |
| 8 s + punctuation + 40 ms snap, padded (round 2), two runs | 6.6% / 7.0% | 32 / 19 | 6.1–6.6 s |
| Pause 200 ms | 4.7% | 16 (4) | 5.8 s |
| Pause 300 ms | 5.8% | 19 (10) | 5.6 s |
| **Pause 400 ms (ships), two runs + production-code run** | **5.5% / 5.6%** (khutba A 3.6% twice) | **12 (6)** | 5.4–6.0 s |

On the held-out lecture (Poturković), pause 400 ms scored 10.6% against 10.2% for round 2's policy (a tie), and 14.3% for the original. The 200, 300 and 400 ms results are within run-to-run noise of each other. 400 ms had the fewest drops and the most stable repeat.

**Restart in a real room** (`room_restart_test.py`, local `livekit-server --dev`, a speaker publishing the khutba as a microphone track):
- Scenario A: a 25 s connection refusal, with the recovery budget shortened to 12 s. The track restarted and captions resumed on the same track. A later fault was absorbed by the stream's own recovery.
- Scenario B: a permanent refusal, with the speaker leaving at 30 s. It restarted 3 times with 1/2/4 s backoff, the task was cancelled 0.2 s after the speaker left, and nothing ran afterwards.
- Speech during the outage is lost. In production the stream's own 90 s recovery replays shorter outages without loss.

**Quota caution:** the file-model fallback (used when a live final is missing, 1–2× per khutba) shares the Vertex quota. Offline simulations running in parallel made it 429 and killed two live test runs, so production may share that exposure.

## Results: translation (gpt-6-luna, both khutbas, Dutch + English, ~$0.20 total)

The production Arabic khutba prompt already handles the Bosnian formulas: ﷻ for dželle šanuhu, (AS), ﷺ. Adding `source_notes_bs_tested.txt` (now `PromptBuilder.SOURCE_NOTES`) won all 4 decisive blind comparisons out of 16 sampled, with 12 ties. The improvements:

- "Cijenjeni džemate" became "Beste broeders en zusters" instead of "Beste aanwezigen".
- The misheard camp name "Crnopolje" was corrected to Trnopolje.
- Jahannam was kept, as the Dutch notes require.
- The meaning was inferred through ASR errors.

A first version also told the model to cite ayah references for Bosnian renderings. It produced one wrong reference (11:28 for 7:88), so that line was removed. Every reference in the final version checked out.
