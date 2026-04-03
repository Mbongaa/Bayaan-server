import asyncio
import logging
import json
import time
import re
import os
import uuid
from typing import Set, Any, Dict, Optional
from collections import defaultdict, deque
from datetime import datetime, timedelta

from enum import Enum
from dataclasses import dataclass, asdict

from livekit import rtc
from livekit.agents import (
    AutoSubscribe,
    JobContext,
    JobProcess,
    JobRequest,
    WorkerOptions,
    cli,
    stt,
    utils,
)
from livekit.plugins import silero, speechmatics, elevenlabs
from livekit.plugins.speechmatics import TurnDetectionMode
from speechmatics.voice import SpeechSegmentConfig

# Import configuration
from config import get_config, ApplicationConfig

# Import database operations
from database import (
    query_room_by_name,
    query_classroom_by_id,
    get_active_session_for_room,
    close_database_connections,
    close_room_session,
    update_session_heartbeat
)

# Import text processing and translation helpers
from text_processing import extract_complete_sentences
from translation_helpers import translate_sentences

# Import Translator class
from translator import Translator

# Import resource management
from resource_management import ResourceManager, TaskManager, STTStreamManager

# Import webhook handler for room context
try:
    from webhook_handler import get_room_context as get_webhook_room_context
except ImportError:
    # Webhook handler not available, use empty context
    def get_webhook_room_context(room_name: str):
        return {}


# Load configuration
config = get_config()

logger = logging.getLogger("transcriber")

# Suppress verbose DEBUG logging from HTTP/OpenAI libraries to reduce memory pressure
# These libraries generate millions of temporary string allocations that fragment memory
for _noisy_logger in ['httpcore', 'httpx', 'openai', 'openai._base_client', 'hpack']:
    logging.getLogger(_noisy_logger).setLevel(logging.WARNING)


@dataclass
class Language:
    code: str
    name: str
    flag: str


# Build languages dictionary from config
languages = {}
for code, lang_info in config.translation.supported_languages.items():
    languages[code] = Language(
        code=code,
        name=lang_info["name"],
        flag=lang_info["flag"]
    )

LanguageCode = Enum(
    "LanguageCode",  # Name of the Enum
    {lang.name: code for code, lang in languages.items()},  # Enum entries: name -> code mapping
)


# Translator class has been moved to translator.py


def prewarm(proc: JobProcess):
    proc.userdata["vad"] = silero.VAD.load()


async def entrypoint(job: JobContext):
    # Configure source language - ARABIC as default
    # This will be the language that users are actually speaking (host/speaker language)
    source_language = config.translation.default_source_language
    
    # Initialize resource manager
    resource_manager = ResourceManager()
    
    # Register heartbeat timeout callback
    async def on_participant_timeout(participant_id: str):
        logger.warning(f"💔 Participant {participant_id} timed out - initiating cleanup")
        # Could trigger cleanup here if needed
    
    resource_manager.heartbeat_monitor.register_callback(on_participant_timeout)
    
    # Start periodic session heartbeat task
    heartbeat_task = None
    async def update_session_heartbeat_periodic():
        """Periodically update the session heartbeat in the database."""
        while True:
            try:
                await asyncio.sleep(20)  # Update every 20 seconds
                
                session_id = tenant_context.get('session_id')
                if session_id:
                    success = await update_session_heartbeat(session_id)
                    if success:
                        logger.debug(f"💓 Updated database session heartbeat for {session_id}")
                    else:
                        logger.warning(f"⚠️ Failed to update session heartbeat for {session_id}")
                else:
                    logger.debug("No session_id available for heartbeat update")
                    
            except asyncio.CancelledError:
                logger.info("💔 Session heartbeat task cancelled")
                break
            except Exception as e:
                logger.error(f"❌ Error in session heartbeat task: {e}")
                await asyncio.sleep(5)  # Wait before retrying
    
    # Extract tenant context from room metadata or webhook handler
    tenant_context = {}
    try:
        # Try to query Supabase directly for room information
        if job.room and job.room.name:
            logger.info(f"🔍 Looking up room context for: {job.room.name}")
            
            # Check if database is configured
            logger.info(f"🔑 Supabase URL: {config.supabase.url}")
            logger.info(f"🔑 Supabase key available: {'Yes' if config.supabase.service_role_key else 'No'}")
            
            if config.supabase.service_role_key:
                try:
                    # Query room by LiveKit room name using the new database module
                    logger.info(f"🔍 Querying database for room: {job.room.name}")
                    # Query room directly without task wrapper
                    room_data = await query_room_by_name(job.room.name)

                    # NEW: If not found in mosque DB, try classroom DB
                    if not room_data and config.classroom_supabase:
                        logger.info("🔍 Room not found in mosque DB, trying classroom DB...")
                        room_data = await query_classroom_by_id(job.room.name, config.classroom_supabase)

                        if room_data:
                            logger.info("🎓 Using classroom database configuration")

                    if room_data:
                        tenant_context = {
                            "room_id": room_data.get("id"),
                            "mosque_id": room_data.get("mosque_id"),
                            "room_title": room_data.get("Title"),
                            "transcription_language": room_data.get("transcription_language", "ar"),
                            "translation_language": room_data.get("translation__language", "nl"),
                            "context_window_size": room_data.get("context_window_size", 6),
                            "created_at": room_data.get("created_at"),
                            "translation_prompt": room_data.get("translation_prompt"),  # NEW: Direct prompt from DB
                            "max_delay": room_data.get("max_delay"),
                            "punctuation_sensitivity": room_data.get("punctuation_sensitivity")
                        }
                        # Also store the double underscore version for compatibility
                        if room_data.get("translation__language"):
                            tenant_context["translation__language"] = room_data.get("translation__language")

                        logger.info(f"✅ Found room in database: room_id={tenant_context.get('room_id')}, mosque_id={tenant_context.get('mosque_id')}")
                        logger.info(f"🗣️ Languages: transcription={tenant_context.get('transcription_language')}, translation={tenant_context.get('translation_language')} (or {tenant_context.get('translation__language')})")

                        # Log if custom prompt is present
                        if tenant_context.get('translation_prompt'):
                            logger.info(f"📝 Custom prompt configured: {tenant_context['translation_prompt'][:60]}...")

                        # Try to get active session for this room (only for mosque rooms with numeric ID)
                        if tenant_context['room_id'] and isinstance(tenant_context['room_id'], int):
                            session_id = await get_active_session_for_room(tenant_context['room_id'])
                            if session_id:
                                tenant_context["session_id"] = session_id
                                logger.info(f"📝 Found active session: {tenant_context['session_id']}")
                except Exception as e:
                    logger.warning(f"⚠️ Could not query Supabase: {e}")
        
        # Fallback to webhook handler if available
        if not tenant_context:
            webhook_context = get_webhook_room_context(job.room.name if job.room else "")
            if webhook_context:
                tenant_context = {
                    "room_id": webhook_context.get("room_id"),
                    "mosque_id": webhook_context.get("mosque_id"),
                    "session_id": webhook_context.get("session_id"),
                    "room_title": webhook_context.get("room_title"),
                    "transcription_language": webhook_context.get("transcription_language", "ar"),
                    "translation_language": webhook_context.get("translation_language", "nl"),
                    "created_at": webhook_context.get("created_at")
                }
                logger.info(f"🏢 Tenant context from webhook handler: mosque_id={tenant_context.get('mosque_id')}, room_id={tenant_context.get('room_id')}")
        
        # Fallback to room metadata if available
        if not tenant_context and job.room and job.room.metadata:
            try:
                metadata = json.loads(job.room.metadata)
                tenant_context = {
                    "room_id": metadata.get("room_id"),
                    "mosque_id": metadata.get("mosque_id"),
                    "session_id": metadata.get("session_id"),
                    "room_title": metadata.get("room_title"),
                    "transcription_language": metadata.get("transcription_language", "ar"),
                    "translation_language": metadata.get("translation_language", "nl"),
                    "created_at": metadata.get("created_at")
                }
                logger.info(f"🏢 Tenant context from room metadata: mosque_id={tenant_context.get('mosque_id')}, room_id={tenant_context.get('room_id')}")
            except:
                pass
        
        # Final fallback to default context with hardcoded values for testing
        if not tenant_context:
            logger.warning(f"⚠️ No tenant context available for room: {job.room.name if job.room else 'unknown'}")
            # TEMPORARY: Use hardcoded values for mosque_546012 rooms
            if job.room and f"mosque_{config.test_mosque_id}" in job.room.name:
                tenant_context = {
                    "room_id": config.test_room_id,
                    "mosque_id": config.test_mosque_id,
                    "session_id": None,
                    "transcription_language": "ar",
                    "translation_language": "nl"
                }
                logger.info(f"🔧 Using hardcoded tenant context for testing: mosque_id={tenant_context['mosque_id']}, room_id={tenant_context['room_id']}")
            else:
                tenant_context = {
                    "room_id": None,
                    "mosque_id": int(os.getenv('DEFAULT_MOSQUE_ID', str(config.default_mosque_id))),
                    "session_id": None,
                    "transcription_language": "ar",
                    "translation_language": "nl"
                }
    except Exception as e:
        logger.warning(f"⚠️ Could not extract tenant context: {e}")
    
    # Configure Speechmatics STT with room-specific settings
    # Use tenant_context which already has room configuration
    room_config = None
    if tenant_context and tenant_context.get('room_id'):
        # We already have the room data in tenant_context from earlier query
        room_config = tenant_context
        logger.info(f"📋 Using room-specific configuration from context: "
                  f"lang={room_config.get('transcription_language', 'ar')}, "
                  f"target={room_config.get('translation_language', 'nl')}, "
                  f"delay={room_config.get('max_delay', 2.0)}, "
                  f"punct={room_config.get('punctuation_sensitivity', 0.5)}, "
                  f"context_window={room_config.get('context_window_size', 6)}")
        
        # If we need full room data and it's not in context, query it
        if not room_config.get('max_delay'):
            try:
                full_room_data = await query_room_by_name(job.room.name if job.room else None)
                if full_room_data:
                    # Merge the full room data with tenant context
                    room_config.update({
                        'max_delay': full_room_data.get('max_delay'),
                        'punctuation_sensitivity': full_room_data.get('punctuation_sensitivity'),
                        'translation__language': full_room_data.get('translation__language'),
                        'context_window_size': full_room_data.get('context_window_size', 6)
                    })
                    logger.info(f"📋 Fetched additional room config: delay={room_config.get('max_delay')}, punct={room_config.get('punctuation_sensitivity')}, context_window={room_config.get('context_window_size')}")
            except Exception as e:
                logger.warning(f"Failed to fetch additional room config: {e}")
    
    # Create STT configuration with room-specific overrides
    stt_config = config.speechmatics.with_room_settings(room_config)
    
    # Speechmatics 1.4.6 STT — direct kwargs on speechmatics.STT().
    # Verified from actual plugin source (stt.py):
    #   - include_partials (NOT enable_partials — that's deprecated)
    #   - punctuation_overrides: {"sensitivity": 0.5} — controls how aggressively
    #     Speechmatics inserts periods. Without this, it puts a period after every word.
    #   - domain: intentionally omitted — concatenates with language (ar-broadcast fails)
    logger.info(f"📋 Speechmatics STT config: lang={stt_config.language}, "
               f"punct_sensitivity={stt_config.punctuation_sensitivity}, "
               f"max_delay={stt_config.max_delay}s")

    # Initialize STT providers dictionary for multi-language support
    stt_providers = {}  # language_code -> STT provider

    def get_display_language_code(code: str) -> str:
        """Map internal routing code to BCP-47 for publishing. ar-eleven -> ar"""
        if code.startswith("ar"):
            return "ar"
        return code

    # Helper function to get or create STT provider for a language
    def get_or_create_stt_provider(language_code: str):
        """Get existing STT provider or create new one for the specified language."""
        if language_code not in stt_providers:
            if language_code == "ar-eleven":
                stt_providers[language_code] = elevenlabs.STT(
                    model_id="scribe_v2_realtime",
                    language_code="ar",
                )
                logger.info("🆕 Created ElevenLabs Scribe v2 realtime STT provider for Arabic")
            else:
                # Direct kwargs — verified against livekit-plugins-speechmatics 1.4.6 source
                stt_language = get_display_language_code(language_code)

                provider = speechmatics.STT(
                    language=stt_language,
                    operating_point=stt_config.operating_point,
                    include_partials=stt_config.enable_partials,
                    max_delay=stt_config.max_delay,
                    punctuation_overrides={"sensitivity": stt_config.punctuation_sensitivity},
                    enable_diarization=bool(stt_config.diarization),
                    # FIXED mode = no VAD, no adaptive — behaves like old 1.2.x RT API
                    turn_detection_mode=TurnDetectionMode.FIXED,
                )

                # Force sentence-based emission like 1.2.x RT API did.
                # The FIXED preset defaults to emit_sentences=False (VAD-driven).
                # We override to True so Speechmatics waits for real sentence
                # boundaries before emitting segments.
                original_prepare = provider._prepare_config
                def _patched_prepare(*args, _orig=original_prepare, **kwargs):
                    config = _orig(*args, **kwargs)
                    config.speech_segment_config = SpeechSegmentConfig(emit_sentences=True)
                    config.vad_config = None  # disable VAD
                    return config
                provider._prepare_config = _patched_prepare

                stt_providers[language_code] = provider
                logger.info(f"🆕 Created Speechmatics STT provider for language: {stt_language} "
                           f"(FIXED mode, emit_sentences=True, no VAD, "
                           f"punct_sensitivity={stt_config.punctuation_sensitivity}, "
                           f"max_delay={stt_config.max_delay}s)")

        return stt_providers[language_code]

    # Update source language based on room config
    source_language = config.translation.get_source_language(room_config)
    logger.info(f"🗣️ STT configured for {languages[source_language].name} speech recognition")

    # Create default STT provider for database-configured language
    default_stt_provider = get_or_create_stt_provider(source_language)
    
    translators = {}
    
    # Get target language from room config or use default
    target_language = config.translation.get_target_language(room_config)
    logger.info(f"🎯 Target language resolved to: '{target_language}' (from room_config: {room_config.get('translation_language') if room_config else 'None'} or {room_config.get('translation__language') if room_config else 'None'})")
    
    # Create translator for the configured target language
    if target_language in languages:
        # Get language enum dynamically
        lang_info = languages[target_language]
        lang_enum = getattr(LanguageCode, lang_info.name)
        translators[target_language] = Translator(job.room, lang_enum, tenant_context)
        logger.info(f"📝 Initialized {lang_info.name} translator ({target_language})")
    else:
        logger.warning(f"⚠️ Target language '{target_language}' not supported, falling back to Dutch")
        dutch_enum = getattr(LanguageCode, 'Dutch')
        translators["nl"] = Translator(job.room, dutch_enum, tenant_context)
    
    # Sentence accumulation for proper sentence-by-sentence translation
    accumulated_text = ""  # Accumulates text until we get a complete sentence
    last_final_transcript = ""  # Keep track of the last final transcript to avoid duplicates
    current_sentence_id = None  # Track current sentence being built
    
    # Track participant tasks for cleanup
    participant_tasks = {}  # participant_id -> task
    
    logger.info(f"🚀 Starting entrypoint for room: {job.room.name if job.room else 'unknown'}")
    logger.info(f"🔍 Translators dict ID: {id(translators)}")
    logger.info(f"🎯 Configuration: {languages[source_language].name} → {languages.get(target_language, languages['nl']).name}")
    logger.info(f"⚙️ STT Settings: delay={stt_config.max_delay}s, punctuation={stt_config.punctuation_sensitivity}")

    async def _forward_transcription(
        stt_stream: stt.SpeechStream,
        track: rtc.Track,
    ):
        """Forward the transcription and log the transcript in the console"""
        nonlocal accumulated_text, last_final_transcript, current_sentence_id
        
        try:
            async for ev in stt_stream:
                # Only process final transcripts since partials are disabled
                if ev.type == stt.SpeechEventType.FINAL_TRANSCRIPT:
                    final_text = ev.alternatives[0].text.strip()
                    print(" -> ", final_text)
                    logger.info(f"Final Arabic transcript: {final_text}")

                    if final_text and final_text != last_final_transcript:
                        last_final_transcript = final_text
                        
                        # Publish final transcription for the original language (Arabic)
                        try:
                            final_segment = rtc.TranscriptionSegment(
                                id=utils.misc.shortuuid("SG_"),
                                text=final_text,
                                start_time=0,
                                end_time=0,
                                language=get_display_language_code(source_language),  # BCP-47 code
                                final=True,
                            )
                            final_transcription = rtc.Transcription(
                                job.room.local_participant.identity, "", [final_segment]
                            )
                            await job.room.local_participant.publish_transcription(final_transcription)
                            
                            logger.info(f"✅ Published final {languages.get(source_language, languages.get('ar')).name} transcription: '{final_text}'")
                        except Exception as e:
                            logger.error(f"❌ Failed to publish final transcription: {str(e)}")
                        
                        # Generate sentence ID if we don't have one for this sentence
                        if not current_sentence_id:
                            current_sentence_id = str(uuid.uuid4())
                        
                        # Handle translation logic
                        if translators:
                            # SIMPLE ACCUMULATION LOGIC - ONLY APPEND, NEVER REPLACE
                            if accumulated_text:
                                # ALWAYS append new final transcript to existing accumulated text
                                accumulated_text = accumulated_text.strip() + " " + final_text
                            else:
                                # First transcript - start accumulation
                                accumulated_text = final_text
                            
                            logger.info(f"📝 Updated accumulated Arabic text: '{accumulated_text}'")
                            
                            # Extract complete sentences from accumulated text
                            complete_sentences, remaining_text = extract_complete_sentences(accumulated_text)

                            # Handle special punctuation completion signal
                            if complete_sentences and complete_sentences[0] == "PUNCTUATION_COMPLETE":
                                if accumulated_text.strip():
                                    # Complete the accumulated sentence with this punctuation
                                    print(f"📝 PUNCTUATION SIGNAL: Completing accumulated text: '{accumulated_text}'")

                                    # Translate the completed sentence (don't include the punctuation marker)
                                    await translate_sentences([accumulated_text], translators, source_language, current_sentence_id)

                                    # Clear accumulated text as sentence is now complete
                                    accumulated_text = ""
                                    current_sentence_id = None
                                    print(f"📝 Cleared accumulated text after punctuation completion")
                                else:
                                    print(f"⚠️ Received punctuation completion signal but no accumulated text")
                            elif complete_sentences:
                                # We have complete sentences - translate them immediately
                                print(f"🎯 Found {len(complete_sentences)} complete Arabic sentences: {complete_sentences}")

                                # Translate each complete sentence
                                for sentence in complete_sentences:
                                    sentence_id = current_sentence_id if len(complete_sentences) == 1 else str(uuid.uuid4())
                                    await translate_sentences([sentence], translators, source_language, sentence_id)

                                # Update accumulated text to only remaining incomplete text
                                accumulated_text = remaining_text
                                # Generate new sentence ID for the remaining text
                                current_sentence_id = str(uuid.uuid4()) if remaining_text else None
                                print(f"📝 Updated accumulated Arabic text after sentence extraction: '{accumulated_text}'")
                            
                            # Log remaining incomplete text (no delayed translation)
                            if accumulated_text.strip():
                                logger.info(f"📝 Incomplete Arabic text remaining: '{accumulated_text}'")
                                # Note: Incomplete text will be translated when the next sentence completes
                        else:
                            logger.warning(f"⚠️ No translators available in room {job.room.name}, only {languages[source_language].name} transcription published")
                    else:
                        logger.debug("Empty or duplicate transcription, skipping")
        except Exception as e:
            logger.error(f"STT transcription error: {str(e)}")
            raise

    async def transcribe_track(participant: rtc.RemoteParticipant, track: rtc.Track, stt_provider):
        """Transcribe audio track using the provided STT provider."""
        # Get language from provider's config
        if hasattr(stt_provider, '_transcription_config'):
            provider_lang = stt_provider._transcription_config.language
        else:
            provider_lang = get_display_language_code(source_language)
        language_name = languages[provider_lang].name if provider_lang in languages else provider_lang

        logger.info(f"🎤 Starting {language_name} transcription for participant {participant.identity}, track {track.sid}")

        try:
            audio_stream = rtc.AudioStream(track)

            # Use context manager for STT stream with provided provider
            async with resource_manager.stt_manager.create_stream(stt_provider, participant.identity) as stt_stream:
                # Create transcription task with tracking
                stt_task = resource_manager.task_manager.create_task(
                    _forward_transcription(stt_stream, track),
                    name=f"transcribe-{participant.identity}",
                    metadata={"participant": participant.identity, "track": track.sid}
                )
                
                frame_count = 0
                try:
                    async for ev in audio_stream:
                        frame_count += 1
                        if frame_count % 100 == 0:  # Log every 100 frames to avoid spam
                            logger.debug(f"🔊 Received audio frame #{frame_count} from {participant.identity}")
                            # Update heartbeat every 100 frames
                            await resource_manager.heartbeat_monitor.update_heartbeat(
                                participant.identity, 
                                tenant_context.get('session_id')
                            )
                        stt_stream.push_frame(ev.frame)
                except asyncio.CancelledError:
                    logger.debug(f"Audio stream cancelled for {participant.identity}")
                    raise
                finally:
                    logger.warning(f"🔇 Audio stream ended for {participant.identity}")
                
                # Cancel the transcription task if still running
                if not stt_task.done():
                    stt_task.cancel()
                    try:
                        await stt_task
                    except asyncio.CancelledError:
                        logger.debug(f"STT task cancelled for {participant.identity}")
                        
        except Exception as e:
            logger.error(f"❌ Transcription track error for {participant.identity}: {str(e)}")
        
        logger.info(f"🧹 Transcription cleanup completed for {participant.identity}")

    @job.room.on("track_subscribed")
    def on_track_subscribed(
        track: rtc.Track,
        publication: rtc.TrackPublication,
        participant: rtc.RemoteParticipant,
    ):
        logger.info(f"🎵 Track subscribed: {track.kind} from {participant.identity} (track: {track.sid})")
        logger.info(f"Track details - muted: {publication.muted}")
        if track.kind == rtc.TrackKind.KIND_AUDIO:
            # Determine transcription language from participant attributes (priority chain):
            # 1. Participant's speaking_language attribute (teacher's UI selection)
            # 2. source_language variable (database configuration or attribute update)
            # 3. Default 'ar'
            participant_lang = participant.attributes.get('speaking_language', source_language)
            language_name = languages[participant_lang].name if participant_lang in languages else participant_lang

            logger.info(f"✅ Adding {language_name} transcriber for participant: {participant.identity}")
            logger.info(f"🔍 Language source: {'participant attribute (speaking_language)' if participant.attributes.get('speaking_language') else 'database/variable'}")

            # Get appropriate STT provider for this language
            provider = get_or_create_stt_provider(participant_lang)

            task = resource_manager.task_manager.create_task(
                transcribe_track(participant, track, provider),  # Pass language-specific provider
                name=f"track-handler-{participant.identity}",
                metadata={"participant": participant.identity, "track": track.sid, "language": participant_lang}
            )
            # Store task reference for cleanup
            participant_tasks[participant.identity] = task
        else:
            logger.info(f"❌ Ignoring non-audio track: {track.kind}")

    @job.room.on("track_published")
    def on_track_published(publication: rtc.TrackPublication, participant: rtc.RemoteParticipant):
        logger.info(f"📡 Track published: {publication.kind} from {participant.identity} (track: {publication.sid})")
        logger.info(f"Publication details - muted: {publication.muted}")

    @job.room.on("track_unpublished") 
    def on_track_unpublished(publication: rtc.TrackPublication, participant: rtc.RemoteParticipant):
        logger.info(f"📡 Track unpublished: {publication.kind} from {participant.identity}")

    @job.room.on("participant_connected")
    def on_participant_connected(participant: rtc.RemoteParticipant):
        logger.info(f"👥 Participant connected: {participant.identity}")
        
        # Try to extract metadata from participant if available
        if hasattr(participant, 'metadata') and participant.metadata:
            try:
                participant_metadata = json.loads(participant.metadata)
                if participant_metadata:
                    # Update tenant context with participant metadata
                    tenant_context.update({
                        "room_id": participant_metadata.get("room_id", tenant_context.get("room_id")),
                        "mosque_id": participant_metadata.get("mosque_id", tenant_context.get("mosque_id")),
                        "session_id": participant_metadata.get("session_id", tenant_context.get("session_id")),
                        "room_title": participant_metadata.get("room_title", tenant_context.get("room_title"))
                    })
                    logger.info(f"📋 Updated tenant context from participant metadata: {tenant_context}")
                    
                    # Update all translators with new context
                    for translator in translators.values():
                        translator.tenant_context = tenant_context
                        
                    # If we got a new session_id and don't have heartbeat task, start it
                    if participant_metadata.get("session_id") and not heartbeat_task:
                        heartbeat_task = resource_manager.task_manager.create_task(
                            update_session_heartbeat_periodic(),
                            name="session-heartbeat",
                            metadata={"session_id": participant_metadata.get("session_id")}
                        )
                        logger.info(f"💓 Started session heartbeat task for new session {participant_metadata.get('session_id')}")
            except Exception as e:
                logger.debug(f"Could not parse participant metadata: {e}")

    @job.room.on("participant_disconnected")
    def on_participant_disconnected(participant: rtc.RemoteParticipant):
        logger.info(f"👥 Participant disconnected: {participant.identity}")
        
        # Cancel participant's transcription task if it exists
        if participant.identity in participant_tasks:
            task = participant_tasks[participant.identity]
            if not task.done():
                logger.info(f"🚫 Cancelling transcription task for {participant.identity}")
                task.cancel()
            del participant_tasks[participant.identity]
        
        # Remove from heartbeat monitoring
        resource_manager.heartbeat_monitor.remove_participant(participant.identity)
        
        # Immediately close any STT streams for this participant
        async def cleanup_participant_streams():
            try:
                await resource_manager.stt_manager.close_participant_stream(participant.identity)
                logger.info(f"✅ STT stream closed for disconnected participant {participant.identity}")
            except Exception as e:
                logger.error(f"Error closing STT stream for {participant.identity}: {e}")
        
        # Schedule immediate cleanup
        resource_manager.task_manager.create_task(
            cleanup_participant_streams(),
            name=f"cleanup-stt-{participant.identity}"
        )
        
        # Log current resource statistics
        resource_manager.log_stats()
        logger.info(f"🧹 Participant cleanup initiated for {participant.identity}")

    @job.room.on("participant_attributes_changed")
    def on_attributes_changed(
        changed_attributes: dict[str, str], participant: rtc.Participant
    ):
        """
        When participant attributes change, handle new translation requests
        and source language updates.
        """
        logger.info(f"🌍 Participant {participant.identity} attributes changed: {changed_attributes}")

        # Check for speaking_language changes (teacher's transcription language)
        speaking_lang = changed_attributes.get("speaking_language", None)
        if speaking_lang:
            nonlocal source_language

            if speaking_lang != source_language:
                logger.info(f"🔄 Teacher changed source language: {source_language} → {speaking_lang}")
                language_name = languages[speaking_lang].name if speaking_lang in languages else speaking_lang
                logger.info(f"🎤 Participant {participant.identity} (teacher) now speaking in: {language_name}")

                # Update the source language for future transcriptions
                old_language = source_language
                source_language = speaking_lang

                logger.info(f"✅ Updated transcription source language from {old_language} to {source_language}")
                new_language_name = languages[source_language].name if source_language in languages else source_language
                logger.info(f"📝 New audio tracks will be transcribed in: {new_language_name}")
            else:
                logger.debug(f"Speaking language unchanged: {speaking_lang}")

        # Check for captions_language changes (student's translation language)
        lang = changed_attributes.get("captions_language", None)
        if lang:
            if lang == source_language:
                logger.info(f"✅ Participant {participant.identity} requested {languages[source_language].name} (source language - Arabic)")
            elif lang in translators:
                logger.info(f"✅ Participant {participant.identity} requested existing language: {lang}")
                logger.info(f"📊 Current translators for this room: {list(translators.keys())}")
            else:
                # Check if the language is supported and different from source language
                if lang in languages:
                    try:
                        # Create a translator for the requested language using the language enum
                        language_obj = languages[lang]
                        language_enum = getattr(LanguageCode, language_obj.name)
                        translators[lang] = Translator(job.room, language_enum, tenant_context)
                        logger.info(f"🆕 Added translator for ROOM {job.room.name} (requested by {participant.identity}), language: {language_obj.name}")
                        logger.info(f"🏢 Translator created with tenant context: mosque_id={tenant_context.get('mosque_id')}")
                        logger.info(f"📊 Total translators for room {job.room.name}: {len(translators)} -> {list(translators.keys())}")
                        logger.info(f"🔍 Translators dict ID: {id(translators)}")
                        
                        # Debug: Verify the translator was actually added
                        if lang in translators:
                            logger.info(f"✅ Translator verification: {lang} successfully added to room translators")
                        else:
                            logger.error(f"❌ Translator verification FAILED: {lang} not found in translators dict")
                            
                    except Exception as e:
                        logger.error(f"❌ Error creating translator for {lang}: {str(e)}")
                else:
                    logger.warning(f"❌ Unsupported language requested by {participant.identity}: {lang}")
                    logger.info(f"💡 Supported languages: {list(languages.keys())}")
        else:
            logger.debug(f"No caption language change for participant {participant.identity}")

    logger.info("Connecting to room...")
    await job.connect(auto_subscribe=AutoSubscribe.AUDIO_ONLY)
    logger.info(f"Successfully connected to room: {job.room.name}")
    logger.info(f"📡 Real-time transcription data will be sent via LiveKit publish_transcription")
    
    # Start the heartbeat task after connection
    if tenant_context.get('session_id'):
        heartbeat_task = resource_manager.task_manager.create_task(
            update_session_heartbeat_periodic(),
            name="session-heartbeat",
            metadata={"session_id": tenant_context.get('session_id')}
        )
        logger.info(f"💓 Started session heartbeat task for session {tenant_context.get('session_id')}")
    
    # Debug room state after connection
    logger.info(f"Room participants: {len(job.room.remote_participants)}")
    for participant in job.room.remote_participants.values():
        logger.info(f"Participant: {participant.identity}")
        logger.info(f"  Audio tracks: {len(participant.track_publications)}")
        for sid, pub in participant.track_publications.items():
            logger.info(f"    Track {sid}: {pub.kind}, muted: {pub.muted}")

    # Also check local participant
    logger.info(f"Local participant: {job.room.local_participant.identity}")
    logger.info(f"Local participant tracks: {len(job.room.local_participant.track_publications)}")

    @job.room.local_participant.register_rpc_method("get/languages")
    async def get_languages(data: rtc.RpcInvocationData):
        languages_list = [asdict(lang) for lang in languages.values()]
        return json.dumps(languages_list)
    
    @job.room.local_participant.register_rpc_method("request/cleanup")
    async def request_cleanup(data: rtc.RpcInvocationData):
        """Handle cleanup request from frontend"""
        try:
            payload = json.loads(data.payload)
            reason = payload.get('reason', 'unknown')
            session_id = payload.get('session_id')
            
            logger.info(f"🧹 Cleanup requested by frontend: reason={reason}, session_id={session_id}")
            
            # Initiate graceful shutdown (tracked task)
            def _on_graceful_cleanup_done(t):
                if t.cancelled():
                    logger.warning("Graceful cleanup task was cancelled")
                elif t.exception():
                    logger.error(f"Graceful cleanup failed: {t.exception()}", exc_info=t.exception())

            graceful_task = asyncio.create_task(perform_graceful_cleanup(reason, session_id))
            graceful_task.add_done_callback(_on_graceful_cleanup_done)
            
            return json.dumps({
                "success": True,
                "message": "Cleanup initiated"
            })
        except Exception as e:
            logger.error(f"Error handling cleanup request: {e}")
            return json.dumps({
                "success": False,
                "error": str(e)
            })
    
    async def perform_graceful_cleanup(reason: str, session_id: Optional[str]):
        """Perform graceful cleanup when requested by frontend"""
        logger.info(f"🛑 Starting graceful cleanup: {reason}")
        
        # Log current resource state
        resource_manager.log_stats()
        
        # If session_id provided, update it in database
        if session_id and tenant_context.get('room_id'):
            try:
                from database import query_database
                result = await query_database(
                    "SELECT cleanup_session_idempotent(%s, %s, %s)",
                    [session_id, f"frontend_{reason}", datetime.utcnow()]
                )
                logger.info(f"Session cleanup result: {result}")
            except Exception as e:
                logger.error(f"Error updating session: {e}")
        
        # Shutdown all resources
        await resource_manager.shutdown()
        
        # Verify cleanup is complete
        verification = await resource_manager.verify_cleanup_complete()
        logger.info(f"🔍 Cleanup verification: {verification}")
        
        # Disconnect from room (this will trigger on_room_disconnected)
        await job.room.disconnect()
        
        logger.info("✅ Graceful cleanup completed")

    @job.room.on("disconnected")
    def on_room_disconnected():
        """Handle room disconnection - cleanup all resources"""
        logger.info("Room disconnected, starting cleanup...")

        async def cleanup():
            try:
                # Log final resource statistics
                resource_manager.log_stats()

                # Cancel heartbeat task first if it exists
                if heartbeat_task and not heartbeat_task.done():
                    heartbeat_task.cancel()
                    try:
                        await heartbeat_task
                    except asyncio.CancelledError:
                        pass

                # CRITICAL: Close all translators and their LLM clients FIRST
                # Each Translator holds an openai.LLM() with httpx.AsyncClient,
                # SSL contexts, and connection pools that leak ~200-500KB each
                translator_count = len(translators)
                for lang, translator in translators.items():
                    try:
                        await translator.close()
                    except Exception as e:
                        logger.error(f"Error closing translator {lang}: {e}")
                translators.clear()
                logger.info(f"Closed and cleared {translator_count} translators")

                # Close all STT providers (each holds connection state)
                stt_count = len(stt_providers)
                for lang, provider in stt_providers.items():
                    try:
                        if hasattr(provider, 'aclose'):
                            await provider.aclose()
                        elif hasattr(provider, 'close'):
                            await provider.close()
                    except Exception as e:
                        logger.error(f"Error closing STT provider {lang}: {e}")
                stt_providers.clear()
                logger.info(f"Closed and cleared {stt_count} STT providers")

                # Shutdown resource manager (cancels all tasks, closes all streams)
                await resource_manager.shutdown()

                # Close the room session in database
                try:
                    session_id = tenant_context.get('session_id') if tenant_context else None
                    if session_id:
                        await close_room_session(session_id)
                except Exception as e:
                    logger.error(f"Failed to close room session: {e}")

                # Close database connections
                try:
                    await close_database_connections()
                except Exception as e:
                    logger.debug(f"Database cleanup error: {e}")

                # Force garbage collection to free any remaining cyclic references
                import gc
                gc.collect()

                # Final verification
                verification = await resource_manager.verify_cleanup_complete()
                logger.info(f"Final cleanup verification: {verification}")

                logger.info("Room cleanup completed successfully")
            except Exception as e:
                logger.error(f"CRITICAL: Room cleanup failed: {e}", exc_info=True)

        # Store task reference and log failures (cannot await in sync handler)
        def _on_cleanup_done(t):
            if t.cancelled():
                logger.warning("Room cleanup task was cancelled before completion")
            elif t.exception():
                logger.error(f"CRITICAL: Room cleanup task failed: {t.exception()}", exc_info=t.exception())

        cleanup_task = asyncio.create_task(cleanup())
        cleanup_task.add_done_callback(_on_cleanup_done)


async def request_fnc(req: JobRequest):
    logger.info(f"🎯 Received job request for room: {req.room.name if req.room else 'unknown'}")
    logger.info(f"📋 Request details: job_id={req.id}, room_name={req.room.name if req.room else 'unknown'}")
    await req.accept(
        name="agent",
        identity="agent",
    )
    logger.info(f"✅ Accepted job request for room: {req.room.name if req.room else 'unknown'}")


if __name__ == "__main__":
    cli.run_app(
        WorkerOptions(
            entrypoint_fnc=entrypoint, prewarm_fnc=prewarm, request_fnc=request_fnc
        )
    )