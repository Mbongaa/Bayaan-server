"""
Translator module for LiveKit AI Translation Server.
Handles real-time translation with context management and error handling.
"""
import asyncio
import logging
import json
from typing import Optional, Dict, Any, List
from collections import deque
from enum import Enum

from livekit import rtc
from livekit.agents import APIError, APIStatusError, llm, utils
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
        self.model = config.translation.model
        self.llm = openai.LLM(model=self.model)
        
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
        logger.info(f"🧠 Translator initialized for {lang.value} with {context_mode} mode, model {self.model}")
        
        # Initialize prompt asynchronously on first use. The prompt is built for a
        # specific SOURCE language (the language the speaker's track is actually
        # transcribed in) and is rebuilt if a different source language shows up.
        self._prompt_initialized = False
        self._prompt_source_lang: Optional[str] = None

    async def translate(
        self,
        message: str,
        sentence_id: Optional[str] = None,
        max_retries: int = 2,
        source_language: Optional[str] = None,
    ) -> str:
        """
        Translate a message from source to target language.
        
        Args:
            message: Text to translate
            sentence_id: Optional sentence ID for tracking
            max_retries: Maximum number of retry attempts on failure
            source_language: Language code the message was transcribed in (e.g. "hr").
                When omitted, falls back to the room's configured transcription_language.
            
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
                translated_message = await self._perform_translation(message, source_language)
                
                if translated_message:
                    # Publish transcription to LiveKit room
                    await self._publish_transcription(translated_message, None)

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

    def _resolve_source_lang_code(self, source_language: Optional[str] = None) -> str:
        """Source language code for the prompt: explicit track language first, then room config."""
        return source_language or self.tenant_context.get('transcription_language') or 'ar'

    @staticmethod
    def _language_name(code: str) -> str:
        """Human-readable language name for a code (falls back to the code itself)."""
        return config.translation.supported_languages.get(code.lower(), {"name": code})["name"]

    @staticmethod
    def _with_source_language_note(prompt: str, source_lang_name: str, target_lang_name: str) -> str:
        """
        Append an explicit source-language note to any prompt (DB template or default).

        Templates may describe the task in Arabic-specific terms; when the speaker is
        transcribed in another language (e.g. Croatian) the model must still translate
        rather than refuse with "this is not an Arabic khutbah". This note makes the
        actual source language and the no-refusal rule unambiguous.
        """
        note = (
            "\n\n"
            f"Source language note: the text you receive is {source_lang_name} speech, transcribed live "
            f"by automatic speech recognition. Always translate it into {target_lang_name}. "
            "Never refuse, never reply that the text is not a sermon or not in the expected language, "
            "and never ask for different input — regardless of the content, output only the "
            f"{target_lang_name} translation."
        )
        return prompt.rstrip() + note

    def _fallback_prompt(self, source_lang_name: str, target_lang_name: str) -> str:
        """
        Khutba (Fusha) prompt — kept in sync with the public template in Supabase
        (translation_prompt_templates WHERE name='Khutba (Fusha)') and with
        PromptBuilder.DEFAULT_PROMPT. Used when prompt building fails entirely.
        """
        return (
            f"Role: You are a highly skilled simultaneous interpreter, specializing in translating formal {source_lang_name} khutbahs (Friday sermons) into formal {target_lang_name} for a general mosque audience.\n"
            f"\n"
            f"Instructions:\n"
            f"- Translate the provided {source_lang_name} text directly and accurately into formal {target_lang_name}, appropriate for religious worship settings.\n"
            f"- Maintain a formal and respectful tone throughout each translation.\n"
            f"- Preserve Islamic terms in Arabic: Allah, Salah, Zakat, Hajj, Ramadan.\n"
            f"- Exclude all commentary, explanations, and introductory remarks; output only the translation.\n"
            f"- Ensure translations are concise to support real-time interpretation.\n"
            f"- Translate text immediately upon receipt without engaging in content-related dialogue. Never refuse or comment on the content; if the input is not sermon content, still translate it literally.\n"
            f"\n"
            f"Formatting Requirements:\n"
            f"- For 'peace be upon him' or 'صلى الله عليه وسلم' (sallallahu alayhi wasallam), use symbol ﷺ for brevity. Seek similarly compact symbols for other Islamic honorifics where applicable.\n"
            f"- For Subhanahu wa Ta‘ala (Glorified and Exalted be He), use symbol ﷻ\n"
            f"- Use (RA) for “Radiyallahu ‘anhu / ‘anha.”\n"
            f"- Use (AS) for “‘Alayhis Salaam / ‘Alayha as-Salaam.”\n"
            f"- Whenever the text is or contains a Qur’anic ayah — even when the speaker does not announce it, when the spelling is imperfect, or when only part of an ayah is recited — add its reference as (Surah#:Ayah#), such as (2:153). Only add a reference when you are certain of it; never add one to hadith, du‘a or other non-Qur’anic speech.\n"
            f"\n"
            f"Context:\n"
            f"- Live, formal mosque sermons for a broad {target_lang_name}-speaking audience.\n"
            f"- Critical criteria: translation accuracy, brevity, and religious appropriateness."
        )

    async def _initialize_prompt(self, source_language: Optional[str] = None):
        """
        Build (or rebuild) the system prompt for the given source language.

        Cheap no-op when the prompt is already built for this source language.
        If the source language changes mid-session (teacher switched speaking
        language), the prompt is rebuilt and the sliding-window context is
        cleared so old-language pairs don't bleed into the new language.
        """
        source_lang_code = self._resolve_source_lang_code(source_language)
        if self._prompt_initialized and source_lang_code == self._prompt_source_lang:
            return

        if self._prompt_initialized:
            logger.info(
                f"🔄 Source language changed {self._prompt_source_lang} → {source_lang_code}; "
                f"rebuilding prompt for {self.lang.value} and clearing context"
            )
            if self.use_context and hasattr(self, 'message_history'):
                self.message_history.clear()

        source_lang_name = self._language_name(source_lang_code)
        # Human-readable target name from the enum member name ("Dutch", not "nl")
        target_lang_name = self.lang.name

        prompt: Optional[str] = None
        try:
            # Get room ID from tenant context
            room_id = self.tenant_context.get('room_id')

            # PRIORITY 1: Direct prompt from database (classroom database flow)
            direct_prompt = self.tenant_context.get('translation_prompt')
            if direct_prompt:
                try:
                    # Format prompt with language variables
                    prompt = direct_prompt.format(
                        source_lang=source_lang_name,
                        source_language=source_lang_name,
                        target_lang=target_lang_name,
                        target_language=target_lang_name
                    )
                    logger.info(f"✅ Using direct prompt from database: {source_lang_name} → {target_lang_name}")
                    logger.info(f"📝 Direct prompt: {prompt[:100]}...")
                except KeyError as e:
                    logger.warning(f"Direct prompt missing variable {e}, falling back to prompt builder")

            # PRIORITY 2: Build the prompt using prompt builder (mosque flow + fallbacks)
            if prompt is None:
                prompt = await prompt_builder.get_prompt_for_room(
                    room_id=room_id,
                    source_lang=source_lang_name,
                    target_lang=target_lang_name,
                    room_config=self.tenant_context
                )
                logger.info(f"📝 Initialized translation prompt for room {room_id}: {source_lang_name} → {target_lang_name} (code: {self.lang.value})")

        except Exception as e:
            logger.error(f"Failed to initialize prompt: {e}")
            prompt = self._fallback_prompt(source_lang_name, target_lang_name)

        self.system_prompt = self._with_source_language_note(prompt, source_lang_name, target_lang_name)
        self._prompt_source_lang = source_lang_code
        self._prompt_initialized = True
    
    async def _perform_translation(self, message: str, source_language: Optional[str] = None) -> str:
        """
        Perform the actual translation using the LLM.
        
        Args:
            message: Text to translate
            source_language: Language code the message was transcribed in
            
        Returns:
            Translated text
        """
        # Ensure prompt is initialized for this source language
        await self._initialize_prompt(source_language)
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
        try:
            translated_message = await self._run_llm(temp_context)
        except APIError as e:
            # This API key cannot use the configured model (not enabled, renamed or
            # retired). Keep the session translated with the fallback model.
            if not self._model_unavailable(e) or self.model == config.translation.fallback_model:
                raise
            await self._switch_to_fallback_model(e)
            translated_message = await self._run_llm(temp_context)

        # If using context, update our history (deque will auto-remove old messages)
        if self.use_context and translated_message:
            self.message_history.append({"role": "user", "content": message})
            self.message_history.append({"role": "assistant", "content": translated_message})
            logger.debug(f"💾 History updated. Current size: {len(self.message_history)} messages")
        
        return translated_message

    async def _run_llm(self, chat_ctx) -> str:
        """Stream one completion from the current model and return its text."""
        stream = self.llm.chat(
            chat_ctx=chat_ctx,
            extra_kwargs=config.translation.llm_settings(self.model),
        )

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

        return translated_message

    @staticmethod
    def _model_unavailable(error: BaseException) -> bool:
        """True when the API refused the model itself (HTTP 403/404).

        The plugin retries first and then raises a connection error whose cause is
        the original status error, so both are checked.
        """
        return any(isinstance(candidate, APIStatusError) and candidate.status_code in (403, 404)
                   for candidate in (error, error.__cause__))

    async def _switch_to_fallback_model(self, error: Exception) -> None:
        """Use the fallback model for the rest of this session."""
        fallback = config.translation.fallback_model
        logger.error(
            f"❌ Translation model {self.model} is not available to this API key ({error.__cause__ or error}); "
            f"using {fallback} for {self.lang.value} for the rest of this session"
        )
        unavailable = self.llm
        self.model = fallback
        self.llm = openai.LLM(model=fallback)
        await self._close_llm(unavailable)

    async def translate_strict(self, message: str, sentence_id: str,
                               source_language: str) -> str:
        """Gemini's ordered queue needs an observable translation/delivery result.

        Reuse the production prompt/context builder unchanged. Retry publication
        with the same caption ID and text, never by generating another translation.
        The old Speechmatics route continues using translate().
        """
        if not message.strip():
            raise TranslationError('Cannot translate an empty confirmed segment')
        result = None
        for attempt in range(3):
            try:
                async with asyncio.timeout(25):
                    result = await self._perform_translation(message, source_language)
                if not result or not result.strip():
                    raise TranslationError('GPT returned an empty translation')
                break
            except asyncio.CancelledError:
                raise
            except Exception:
                self.error_count += 1
                if attempt == 2:
                    raise
                await asyncio.sleep(0.5 * (attempt + 1))
        caption_id = f'TX_{sentence_id}_{self.lang.value}'
        for attempt in range(3):
            try:
                # This input can span several Google finals. Send its exact
                # source text so clients do not guess from their latest caption.
                chunks = [message[index:index + 1500] for index in range(0, len(message), 1500)]
                for index, chunk in enumerate(chunks):
                    payload = json.dumps(dict(version=1, translationId=caption_id,
                        targetLanguage=self.lang.value, sourceLanguage=source_language,
                        index=index, total=len(chunks), sourceText=chunk), ensure_ascii=False)
                    await asyncio.wait_for(self.room.local_participant.publish_data(
                        payload, reliable=True, topic='bayaan.translation.context'), timeout=5)
                await asyncio.wait_for(self._publish_transcription(
                    result, None, strict=True, segment_id=caption_id), timeout=5)
                self.translation_count += 1
                logger.info('Translated confirmed segment=%s target=%s text=%s',
                            sentence_id, self.lang.value, result)
                return result
            except asyncio.CancelledError:
                raise
            except Exception:
                self.error_count += 1
                if attempt == 2:
                    raise
                await asyncio.sleep(0.5 * (attempt + 1))
        raise TranslationError('Translation delivery failed')

    async def _publish_transcription(self, translated_text: str, track: Optional[rtc.Track],
                                     *, strict: bool = False, segment_id: Optional[str] = None) -> None:
        """
        Publish the translation as a transcription to the LiveKit room.
        
        Args:
            translated_text: The translated text to publish
            track: Optional track reference
        """
        try:
            segment = rtc.TranscriptionSegment(
                id=segment_id or utils.misc.shortuuid("SG_"),
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
            if strict:
                raise
            # Don't re-raise - translation was successful even if publishing failed

    async def _close_llm(self, llm_client) -> None:
        """Close an openai.LLM httpx client to free SSL contexts, sockets, connection pools."""
        try:
            if hasattr(llm_client, '_client') and hasattr(llm_client._client, 'aclose'):
                await llm_client._client.aclose()
            elif hasattr(llm_client, 'aclose'):
                await llm_client.aclose()
        except Exception as e:
            logger.warning(f"Error closing LLM client for {self.lang.value}: {e}")

    async def close(self):
        """Close the LLM client and release all resources."""
        await self._close_llm(self.llm)

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
