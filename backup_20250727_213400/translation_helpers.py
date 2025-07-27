"""
Translation helper functions for LiveKit AI Translation Server.
Handles translation orchestration and related utilities.
"""
import asyncio
import logging
from typing import List, Dict, Any, Optional

logger = logging.getLogger("transcriber.translation")


async def translate_sentences(
    sentences: List[str], 
    translators: Dict[str, Any],
    source_language: str = "ar",
    sentence_id: Optional[str] = None
) -> None:
    """
    Translate complete sentences to all target languages.
    
    This function takes a list of sentences and sends them to all available
    translators concurrently for better performance.
    
    Args:
        sentences: List of sentences to translate
        translators: Dictionary of language code to translator instances
        source_language: Source language code (default: "ar" for Arabic)
        sentence_id: Optional sentence ID for tracking
    """
    if not sentences or not translators:
        return
        
    for sentence in sentences:
        if sentence.strip():
            logger.info(f"🎯 TRANSLATING COMPLETE {source_language.upper()} SENTENCE: '{sentence}'")
            logger.info(f"📊 Processing sentence for {len(translators)} translators")
            
            # Send to all translators concurrently for better performance
            translation_tasks = []
            for lang, translator in translators.items():
                logger.info(f"📤 Sending complete {source_language.upper()} sentence '{sentence}' to {lang} translator")
                translation_tasks.append(translator.translate(sentence, sentence_id))
            
            # Execute all translations concurrently
            if translation_tasks:
                results = await asyncio.gather(*translation_tasks, return_exceptions=True)
                # Check for any exceptions
                for i, result in enumerate(results):
                    if isinstance(result, Exception):
                        logger.error(f"❌ Translation failed: {result}")


async def translate_single_sentence(
    sentence: str,
    translator: Any,
    target_language: str
) -> Optional[str]:
    """
    Translate a single sentence to a specific target language.
    
    Args:
        sentence: The sentence to translate
        translator: The translator instance to use
        target_language: Target language code
        
    Returns:
        Translated text or None if translation failed
    """
    try:
        if not sentence.strip():
            return None
            
        logger.debug(f"Translating to {target_language}: '{sentence}'")
        result = await translator.translate(sentence, None)
        return result
    except Exception as e:
        logger.error(f"Translation to {target_language} failed: {e}")
        return None


def should_translate_text(text: str, min_length: int = 3) -> bool:
    """
    Determine if text should be translated based on various criteria.
    
    Args:
        text: The text to evaluate
        min_length: Minimum length for translation (default: 3 characters)
        
    Returns:
        True if text should be translated
    """
    if not text or not text.strip():
        return False
    
    # Don't translate very short text
    if len(text.strip()) < min_length:
        return False
    
    # Don't translate if it's only punctuation
    if all(c in '.!?؟,،;؛:' for c in text.strip()):
        return False
    
    return True


def format_translation_output(
    original_text: str,
    translated_text: str,
    source_lang: str,
    target_lang: str
) -> Dict[str, str]:
    """
    Format translation output for consistent structure.
    
    Args:
        original_text: Original text
        translated_text: Translated text
        source_lang: Source language code
        target_lang: Target language code
        
    Returns:
        Formatted translation dictionary
    """
    return {
        "original": original_text,
        "translated": translated_text,
        "source_language": source_lang,
        "target_language": target_lang,
        "type": "translation"
    }


async def batch_translate(
    texts: List[str],
    translators: Dict[str, Any],
    batch_size: int = 5
) -> Dict[str, List[str]]:
    """
    Translate multiple texts in batches for efficiency.
    
    Args:
        texts: List of texts to translate
        translators: Dictionary of language code to translator instances
        batch_size: Number of texts to process in each batch
        
    Returns:
        Dictionary mapping language codes to lists of translations
    """
    results = {lang: [] for lang in translators.keys()}
    
    for i in range(0, len(texts), batch_size):
        batch = texts[i:i + batch_size]
        
        # Process each batch
        for text in batch:
            if should_translate_text(text):
                await translate_sentences([text], translators)
    
    return results