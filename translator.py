"""
Translator module for LiveKit AI Translation Server.
Handles real-time translation with context management and error handling.
"""
import asyncio
import logging
from typing import Optional, Dict, Any, List
from collections import deque
from enum import Enum

from livekit import rtc
from livekit.agents import llm, utils
from livekit.plugins import openai

from config import get_config
from prompt_builder import get_prompt_builder

logger = logging.getLogger("transcriber.translator")
config = get_config()
prompt_builder = get_prompt_builder()


class TranslationError(Exception):
    """Custom exception for translation-related errors."""
    pass


class Translator:
    """
    Handles translation from source language to target language with context management.

    Features:
    - Sliding window context for better translation coherence
    - Automatic retry on failures
    - Comprehensive error handling
    """

    # Class-level configuration from config module
    use_context = config.translation.use_context
    default_max_context_pairs = config.translation.max_context_pairs

    def __init__(self, room: rtc.Room, lang: Enum, tenant_context: Optional[Dict[str, Any]] = None):
        """
        Initialize the Translator.

        Args:
            room: LiveKit room instance
            lang: Target language enum
            tenant_context: Optional context containing room_id, mosque_id, etc.
        """
        self.room = room
        self.lang = lang
        self.tenant_context = tenant_context or {}
        self.llm = openai.LLM(model="gpt-4.1")
        
        # Initialize system prompt as None - will be built dynamically
        self.system_prompt = None
        self._prompt_template = None
        
        # Get context window size from room config or use default
        self.max_context_pairs = config.translation.get_context_window_size(tenant_context)
        
        # Use deque for automatic sliding window (old messages auto-removed)
        if self.use_context:
            self.message_history: deque = deque(maxlen=(self.max_context_pairs * 2))
        
        # Track translation statistics
        self.translation_count = 0
        self.error_count = 0
        
        # Log the context mode being used
        context_mode = f"TRUE SLIDING WINDOW ({self.max_context_pairs}-pair memory)" if self.use_context else "FRESH CONTEXT (no memory)"
        logger.info(f"🧠 Translator initialized for {lang.value} with {context_mode} mode")
        
        # Initialize prompt asynchronously on first use
        self._prompt_initialized = False

    async def translate(self, message: str, sentence_id: Optional[str] = None, max_retries: int = 2) -> str:
        """
        Translate a message from source to target language.
        
        Args:
            message: Text to translate
            sentence_id: Optional sentence ID for tracking
            max_retries: Maximum number of retry attempts on failure
            
        Returns:
            Translated text (empty string on failure)
            
        Raises:
            TranslationError: If translation fails after all retries
        """
        if not message or not message.strip():
            logger.debug("Empty message, skipping translation")
            return ""
        
        retry_count = 0
        last_error = None
        
        while retry_count <= max_retries:
            try:
                translated_message = await self._perform_translation(message)
                
                if translated_message:
                    # Publish transcription to LiveKit room
                    await self._publish_transcription(translated_message, None)

                    # Update statistics
                    self.translation_count += 1
                    
                    # Log successful translation without emitting sermon text.
                    logger.info(
                        "Translated to %s: source_chars=%s target_chars=%s sentence_id=%s",
                        self.lang.value,
                        len(message),
                        len(translated_message),
                        sentence_id,
                    )
                    
                    return translated_message
                else:
                    logger.warning(
                        "Empty translation result: source_chars=%s sentence_id=%s",
                        len(message),
                        sentence_id,
                    )
                    return ""
                    
            except Exception as e:
                last_error = e
                retry_count += 1
                self.error_count += 1
                
                if retry_count <= max_retries:
                    wait_time = retry_count * 0.5  # Exponential backoff
                    logger.warning(
                        f"Translation attempt {retry_count} failed: {e}. "
                        f"Retrying in {wait_time}s..."
                    )
                    await asyncio.sleep(wait_time)
                else:
                    logger.error(
                        f"❌ Translation failed after {max_retries} retries: {e}\n"
                        f"Message chars: {len(message)}"
                    )
                    
        # If we get here, all retries failed
        error_msg = f"Translation failed after {max_retries} retries (chars={len(message)})"
        if last_error:
            error_msg += f": {last_error}"
        
        # Don't raise exception - return empty string to keep stream going
        logger.error(error_msg)
        return ""

    async def _initialize_prompt(self):
        """Initialize the system prompt using the prompt builder."""
        if self._prompt_initialized:
            return

        try:
            # Get room ID from tenant context
            room_id = self.tenant_context.get('room_id')

            # Get source language from room config
            source_lang_code = self.tenant_context.get('transcription_language', 'ar')
            source_lang_name = config.translation.supported_languages.get(
                source_lang_code,
                {"name": "Arabic"}
            )["name"]

            # Get target language name from the enum value
            target_lang_name = self.lang.name  # This should give us "Dutch" instead of "nl"

            # PRIORITY 1: Direct prompt from database (classroom database flow)
            direct_prompt = self.tenant_context.get('translation_prompt')
            if direct_prompt:
                try:
                    # Format prompt with language variables
                    self.system_prompt = direct_prompt.format(
                        source_lang=source_lang_name,
                        source_language=source_lang_name,
                        target_lang=target_lang_name,
                        target_language=target_lang_name
                    )
                    logger.info(f"✅ Using direct prompt from database: {source_lang_name} → {target_lang_name}")
                    logger.info("Direct prompt initialized: chars=%s", len(self.system_prompt))
                    self._prompt_initialized = True
                    return
                except KeyError as e:
                    logger.warning(f"Direct prompt missing variable {e}, falling back to prompt builder")

            # PRIORITY 2: Build the prompt using prompt builder (mosque flow + fallbacks)
            self.system_prompt = await prompt_builder.get_prompt_for_room(
                room_id=room_id,
                source_lang=source_lang_name,
                target_lang=target_lang_name,
                room_config=self.tenant_context
            )

            logger.info(f"📝 Initialized translation prompt for room {room_id}: {source_lang_name} → {target_lang_name} (code: {self.lang.value})")
            self._prompt_initialized = True

        except Exception as e:
            logger.error(f"Failed to initialize prompt: {e}")
            # Fallback to the Khutba (Fusha) prompt — kept byte-for-byte in sync
            # with the public template in Supabase (translation_prompt_templates
            # WHERE name='Khutba (Fusha)'). When a room has no tenant_context
            # (e.g. the classroom-side Khutba Quickstart rooms with random codes
            # that aren't registered in Supabase), this gives the same effective
            # prompt as a configured mosque would get.
            source_lang_code = self.tenant_context.get('transcription_language', 'ar')
            source_lang_name = config.translation.supported_languages.get(
                source_lang_code,
                {"name": "Arabic"}
            )["name"]
            target_lang_name = self.lang.value

            self.system_prompt = (
                f"Role: You are a highly skilled simultaneous interpreter, specializing in translating formal {source_lang_name} khutbahs (Friday sermons) into formal {target_lang_name} for a general mosque audience.\n"
                f"\n"
                f"Instructions:\n"
                f"- Translate provided Arabic text directly and accurately into formal {target_lang_name}, appropriate for religious worship settings.\n"
                f"- Maintain a formal and respectful tone throughout each translation.\n"
                f"- Preserve Islamic terms in Arabic: Allah, Salah, Zakat, Hajj, Ramadan.\n"
                f"- Exclude all commentary, explanations, and introductory remarks; output only the translation.\n"
                f"- Ensure translations are concise to support real-time interpretation.\n"
                f"- Translate text immediately upon receipt without engaging in content-related dialogue.\n"
                f"\n"
                f"Begin with a concise checklist (3-7 bullets) of your translation process; keep items conceptual, not implementation-level.\n"
                f"\n"
                f"Formatting Requirements:\n"
                f"- For 'peace be upon him' or 'صلى الله عليه وسلم' (sallallahu alayhi wasallam), use symbol ﷺ for brevity. Seek similarly compact symbols for other Islamic honorifics where applicable.\n"
                f"- For Subhanahu wa Ta‘ala (Glorified and Exalted be He), use symbol ﷻ\n"
                f"- Use (RA) for “Radiyallahu ‘anhu / ‘anha.”\n"
                f"- Use (AS) for “‘Alayhis Salaam / ‘Alayha as-Salaam.”\n"
                f"- Format Qur’anic ayahs as (Surah#:Ayah#), such as (2:153).\n"
                f"\n"
                f"After producing each translation, perform a brief validation to confirm accuracy and brevity, then proceed or self-correct if criteria are not met.\n"
                f"\n"
                f"Context:\n"
                f"- Live, formal mosque sermons for a broad {target_lang_name}-speaking audience.\n"
                f"- Critical criteria: translation accuracy, brevity, and religious appropriateness."
            )
            self._prompt_initialized = True
    
    async def _perform_translation(self, message: str) -> str:
        """
        Perform the actual translation using the LLM.
        
        Args:
            message: Text to translate
            
        Returns:
            Translated text
        """
        # Ensure prompt is initialized
        await self._initialize_prompt()
        # Build a fresh context for every translation (rebuild method)
        temp_context = llm.ChatContext()
        temp_context.add_message(role="system", content=self.system_prompt)
        
        # If using context, add the message history from our deque
        if self.use_context and hasattr(self, 'message_history'):
            logger.debug(f"🔄 Building context with {len(self.message_history)} historical messages")
            for msg in self.message_history:
                temp_context.add_message(role=msg['role'], content=msg['content'])
        
        # Add the current message to translate
        temp_context.add_message(content=message, role="user")
        
        # Get translation from LLM with the freshly built context
        stream = self.llm.chat(chat_ctx=temp_context)

        translated_message = ""
        try:
            async for chunk in stream:
                if chunk.delta is None:
                    continue
                content = chunk.delta.content
                if content is None:
                    break
                translated_message += content
        finally:
            # Ensure stream is properly closed to release HTTP connections, SSL contexts,
            # and socket buffers. Without this, each orphaned stream leaks ~50-100KB.
            try:
                if hasattr(stream, 'aclose'):
                    await stream.aclose()
                elif hasattr(stream, 'close'):
                    await stream.close()
            except Exception:
                pass  # Best-effort cleanup - falls back to GC if this fails

        # If using context, update our history (deque will auto-remove old messages)
        if self.use_context and translated_message:
            self.message_history.append({"role": "user", "content": message})
            self.message_history.append({"role": "assistant", "content": translated_message})
            logger.debug(f"💾 History updated. Current size: {len(self.message_history)} messages")
        
        return translated_message

    async def _publish_transcription(self, translated_text: str, track: Optional[rtc.Track]) -> None:
        """
        Publish the translation as a transcription to the LiveKit room.
        
        Args:
            translated_text: The translated text to publish
            track: Optional track reference
        """
        try:
            segment = rtc.TranscriptionSegment(
                id=utils.misc.shortuuid("SG_"),
                text=translated_text,
                start_time=0,
                end_time=0,
                language=self.lang.value,
                final=True,
            )
            transcription = rtc.Transcription(
                self.room.local_participant.identity, 
                track.sid if track else "", 
                [segment]
            )
            await self.room.local_participant.publish_transcription(transcription)
            logger.debug(f"📤 Published {self.lang.value} transcription to LiveKit room")
        except Exception as e:
            logger.error(f"Failed to publish transcription: {e}")
            # Don't re-raise - translation was successful even if publishing failed

    async def close(self):
        """Close the LLM client and release all resources."""
        # Close the openai.LLM httpx client to free SSL contexts, sockets, connection pools
        try:
            if hasattr(self.llm, '_client') and hasattr(self.llm._client, 'aclose'):
                await self.llm._client.aclose()
            elif hasattr(self.llm, 'aclose'):
                await self.llm.aclose()
        except Exception as e:
            logger.warning(f"Error closing LLM client for {self.lang.value}: {e}")

        # Clear context history to release string references
        if self.use_context and hasattr(self, 'message_history'):
            self.message_history.clear()

        # Clear system prompt reference
        self.system_prompt = None
        self._prompt_template = None

        logger.info(f"Translator closed for {self.lang.value} (translations={self.translation_count}, errors={self.error_count})")

    def get_statistics(self) -> Dict[str, Any]:
        """
        Get translation statistics.
        
        Returns:
            Dictionary containing translation stats
        """
        return {
            "language": self.lang.value,
            "translation_count": self.translation_count,
            "error_count": self.error_count,
            "error_rate": self.error_count / max(1, self.translation_count),
            "context_enabled": self.use_context,
            "context_size": len(self.message_history) if self.use_context else 0
        }

    def clear_context(self) -> None:
        """Clear the translation context history."""
        if self.use_context and hasattr(self, 'message_history'):
            self.message_history.clear()
            logger.info(f"🧹 Cleared translation context for {self.lang.value}")

    def __repr__(self) -> str:
        """String representation of the Translator."""
        return (
            f"Translator(lang={self.lang.value}, "
            f"context={self.use_context}, "
            f"translations={self.translation_count}, "
            f"errors={self.error_count})"
        )
