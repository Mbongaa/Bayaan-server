"""
Text processing utilities for LiveKit AI Translation Server.
Handles sentence extraction and text manipulation for Arabic and other languages.
"""
import re
import logging
from typing import Tuple, List

logger = logging.getLogger("transcriber.text_processing")


def extract_complete_sentences(text: str) -> Tuple[List[str], str]:
    """
    Extract complete sentences from text and return them along with remaining incomplete text.
    
    This function is designed to work with Arabic text and handles Arabic punctuation marks.
    It identifies complete sentences based on punctuation and returns both the complete
    sentences and any remaining incomplete text.
    
    Args:
        text: The input text to process
        
    Returns:
        A tuple containing:
        - List of complete sentences
        - Remaining incomplete text
    """
    if not text.strip():
        return [], ""
    
    # Arabic sentence ending punctuation marks
    sentence_endings = ['.', '!', '?', '؟']  # Including Arabic question mark
    
    complete_sentences = []
    remaining_text = ""
    
    logger.debug(f"🔍 Processing text for sentence extraction: '{text}'")
    
    # Check if this is standalone punctuation
    if text.strip() in sentence_endings:
        logger.debug(f"📝 Detected standalone punctuation: '{text.strip()}'")
        # This is standalone punctuation - signal to complete any accumulated sentence
        return ["PUNCTUATION_COMPLETE"], ""
    
    # Split text into parts ending with punctuation
    # This regex splits on punctuation but keeps the punctuation in the result
    parts = re.split(r'([.!?؟])', text)
    
    current_building = ""
    i = 0
    while i < len(parts):
        part = parts[i].strip()
        if not part:
            i += 1
            continue
            
        if part in sentence_endings:
            # Found punctuation - complete the current sentence
            if current_building.strip():
                complete_sentence = current_building.strip() + part
                complete_sentences.append(complete_sentence)
                logger.debug(f"✅ Complete sentence found: '{complete_sentence}'")
                current_building = ""
            i += 1
        else:
            # Regular text - add to current building
            current_building += (" " + part if current_building else part)
            i += 1
    
    # Any remaining text becomes the incomplete part
    if current_building.strip():
        remaining_text = current_building.strip()
        logger.debug(f"🔄 Remaining incomplete text: '{remaining_text}'")
    
    logger.debug(f"📊 Extracted {len(complete_sentences)} complete sentences, remaining: '{remaining_text}'")
    return complete_sentences, remaining_text


def is_sentence_ending(text: str) -> bool:
    """
    Check if the text ends with a sentence-ending punctuation mark.
    
    Args:
        text: The text to check
        
    Returns:
        True if the text ends with sentence-ending punctuation
    """
    if not text.strip():
        return False
    
    sentence_endings = ['.', '!', '?', '؟']
    return text.strip()[-1] in sentence_endings


def clean_text(text: str) -> str:
    """
    Clean and normalize text for processing.
    
    Args:
        text: The text to clean
        
    Returns:
        Cleaned text
    """
    # Remove extra whitespace
    text = ' '.join(text.split())
    # Remove leading/trailing whitespace
    text = text.strip()
    return text


def split_into_chunks(text: str, max_length: int = 500) -> List[str]:
    """
    Split text into chunks of maximum length, preferring to split at sentence boundaries.
    
    Args:
        text: The text to split
        max_length: Maximum length of each chunk
        
    Returns:
        List of text chunks
    """
    if len(text) <= max_length:
        return [text]
    
    chunks = []
    sentences, _ = extract_complete_sentences(text)
    
    current_chunk = ""
    for sentence in sentences:
        if len(current_chunk) + len(sentence) + 1 <= max_length:
            current_chunk += (" " + sentence if current_chunk else sentence)
        else:
            if current_chunk:
                chunks.append(current_chunk)
            current_chunk = sentence
    
    if current_chunk:
        chunks.append(current_chunk)
    
    return chunks