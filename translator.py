"""
Translator module for LiveKit AI Translation Server.
Handles real-time translation with context management and error handling.
"""
import asyncio
import logging
from typing import Optional, Dict, Any, List, Callable
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
    - Real-time broadcasting to displays
    """
    
    # Class-level configuration from config module
    use_context = config.translation.use_context
    default_max_context_pairs = config.translation.max_context_pairs
    
    def __init__(self, room: rtc.Room, lang: Enum, tenant_context: Optional[Dict[str, Any]] = None, broadcast_callback: Optional[Callable] = None):
        """
        Initialize the Translator.
        
        Args:
            room: LiveKit room instance
            lang: Target language enum
            tenant_context: Optional context containing room_id, mosque_id, etc.
            broadcast_callback: Optional callback function for broadcasting translations
        """
        self.room = room
        self.lang = lang
        self.tenant_context = tenant_context or {}
        self.broadcast_callback = broadcast_callback
        self.llm = openai.LLM()
        
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
                    
                    # Broadcast to displays
                    await self._broadcast_translation(translated_message, sentence_id)
                    
                    # Update statistics
                    self.translation_count += 1
                    
                    # Log successful translation
                    logger.info(f"✅ Translated to {self.lang.value}: '{message}' → '{translated_message}'")
                    
                    return translated_message
                else:
                    logger.warning(f"Empty translation result for: '{message}'")
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
                        f"Message: '{message}'"
                    )
                    
        # If we get here, all retries failed
        error_msg = f"Translation failed for '{message}' after {max_retries} retries"
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
                    logger.info(f"📝 Direct prompt: {self.system_prompt[:100]}...")
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
            # Fallback to default prompt with dynamic source language
            source_lang_code = self.tenant_context.get('transcription_language', 'ar')
            source_lang_name = config.translation.supported_languages.get(
                source_lang_code,
                {"name": "Arabic"}
            )["name"]

            self.system_prompt = (
                f"You are an expert simultaneous interpreter. Your task is to translate from {source_lang_name} to {self.lang.value}. "
                f"Provide a direct and accurate translation of the user's input. Be concise and use natural-sounding language. "
                f"Do not add any additional commentary, explanations, or introductory phrases."
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

    async def _broadcast_translation(self, translated_text: str, sentence_id: Optional[str] = None) -> None:
        """
        Broadcast the translation to WebSocket displays.
        
        Args:
            translated_text: The translated text to broadcast
            sentence_id: Optional sentence ID for tracking
        """
        if self.broadcast_callback:
            try:
                # Use asyncio.create_task to avoid blocking
                # Include sentence context if provided
                sentence_context = None
                if sentence_id:
                    sentence_context = {
                        "sentence_id": sentence_id,
                        "is_complete": True,
                        "is_fragment": False
                    }
                
                asyncio.create_task(
                    self.broadcast_callback(
                        "translation", 
                        self.lang.value, 
                        translated_text, 
                        self.tenant_context,
                        sentence_context
                    )
                )
                logger.debug(f"📡 Broadcasted {self.lang.value} translation to displays")
            except Exception as e:
                logger.error(f"Failed to broadcast translation: {e}")
                # Don't re-raise - translation was successful even if broadcasting failed
        else:
            logger.debug("No broadcast callback provided, skipping broadcast")

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