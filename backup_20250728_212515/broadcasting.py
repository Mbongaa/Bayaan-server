"""
Broadcasting module for LiveKit AI Translation Server.
Handles real-time broadcasting of transcriptions and translations to displays.
"""
import asyncio
import logging
import hashlib
import uuid
from typing import Optional, Dict, Any
from datetime import datetime

from config import get_config
from database import broadcast_to_channel, store_transcript_in_database

logger = logging.getLogger("transcriber.broadcasting")
config = get_config()


class BroadcastError(Exception):
    """Custom exception for broadcasting-related errors."""
    pass


async def broadcast_to_displays(
    message_type: str, 
    language: str, 
    text: str, 
    tenant_context: Optional[Dict[str, Any]] = None,
    sentence_context: Optional[Dict[str, Any]] = None
) -> bool:
    """
    Send transcription/translation to frontend via Supabase Broadcast and store in database.
    
    This function handles both real-time broadcasting and database storage of
    transcriptions and translations. It uses Supabase's broadcast feature for
    real-time updates and stores the data for persistence.
    
    Args:
        message_type: Type of message ("transcription" or "translation")
        language: Language code (e.g., "ar", "nl")
        text: The text content to broadcast
        tenant_context: Optional context containing room_id, mosque_id, etc.
        sentence_context: Optional context for sentence tracking (sentence_id, is_complete, etc.)
        
    Returns:
        bool: True if broadcast was successful, False otherwise
    """
    if not text or not text.strip():
        logger.debug("Empty text provided, skipping broadcast")
        return False
    
    success = False
    
    # Phase 1: Immediate broadcast via Supabase for real-time display
    if tenant_context and tenant_context.get("room_id") and tenant_context.get("mosque_id"):
        try:
            channel_name = f"live-transcription-{tenant_context['room_id']}-{tenant_context['mosque_id']}"
            
            # Generate unique message ID based on timestamp and content hash
            timestamp = datetime.utcnow().isoformat() + "Z"
            text_hash = hashlib.md5(text.encode('utf-8')).hexdigest()[:8]
            msg_id = f"{timestamp}_{text_hash}"
            
            # Build payload with optional sentence context
            data_payload = {
                "text": text,
                "language": language,
                "timestamp": timestamp,
                "msg_id": msg_id
            }
            
            # Add sentence context if provided
            if sentence_context:
                data_payload.update({
                    "sentence_id": sentence_context.get("sentence_id"),
                    "is_complete": sentence_context.get("is_complete", False),
                    "is_fragment": sentence_context.get("is_fragment", True)
                })
            
            payload = {
                "type": message_type,
                "room_id": tenant_context["room_id"],
                "mosque_id": tenant_context["mosque_id"],
                "data": data_payload
            }
            
            # Use the broadcast_to_channel function from database module
            success = await broadcast_to_channel(channel_name, message_type, payload)
            
            if success:
                logger.info(
                    f"📡 LIVE: Sent {message_type} ({language}) via Supabase broadcast: "
                    f"{text[:50]}{'...' if len(text) > 50 else ''}"
                )
            else:
                logger.warning(f"⚠️ Failed to broadcast {message_type} to Supabase")
                
        except Exception as e:
            logger.error(f"❌ Broadcast error: {e}")
            success = False
    else:
        logger.warning("⚠️ Missing tenant context for Supabase broadcast")
    
    # Phase 2: Direct database storage (no batching)
    if tenant_context and tenant_context.get("room_id") and tenant_context.get("mosque_id"):
        try:
            # Store directly in database using existing function
            # Use create_task to avoid blocking the broadcast with proper error handling
            task = asyncio.create_task(
                _store_with_error_handling(message_type, language, text, tenant_context, sentence_context)
            )
            task.add_done_callback(lambda t: None if not t.exception() else logger.error(f"Storage task failed: {t.exception()}"))
            logger.debug(
                f"💾 DIRECT: Storing {message_type} directly to database "
                f"for room {tenant_context['room_id']}"
            )
        except Exception as e:
            logger.error(f"❌ Failed to initiate database storage: {e}")
    else:
        logger.warning("⚠️ Missing tenant context for database storage")
    
    return success


async def _store_with_error_handling(
    message_type: str, 
    language: str, 
    text: str, 
    tenant_context: Dict[str, Any],
    sentence_context: Optional[Dict[str, Any]] = None
) -> None:
    """
    Store transcript with proper error handling.
    
    This is a wrapper around store_transcript_in_database that ensures
    errors don't propagate and crash the application.
    
    Args:
        message_type: Type of message ("transcription" or "translation")
        language: Language code
        text: The text content to store
        tenant_context: Context containing room_id, mosque_id, etc.
        sentence_context: Optional context containing sentence_id, is_complete, is_fragment
    """
    try:
        success = await store_transcript_in_database(
            message_type, language, text, tenant_context, sentence_context
        )
        if not success:
            logger.warning(
                f"⚠️ Failed to store {message_type} in database for "
                f"room {tenant_context.get('room_id')}"
            )
    except Exception as e:
        logger.error(
            f"❌ Database storage error for {message_type}: {e}\n"
            f"Room: {tenant_context.get('room_id')}, "
            f"Language: {language}"
        )


async def broadcast_batch(
    messages: list[tuple[str, str, str, Dict[str, Any]]]
) -> Dict[str, int]:
    """
    Broadcast multiple messages in batch for efficiency.
    
    Args:
        messages: List of tuples (message_type, language, text, tenant_context)
        
    Returns:
        Dictionary with counts of successful and failed broadcasts
    """
    results = {"success": 0, "failed": 0}
    
    # Process all broadcasts concurrently
    tasks = []
    for message_type, language, text, tenant_context in messages:
        task = broadcast_to_displays(message_type, language, text, tenant_context)
        tasks.append(task)
    
    # Wait for all broadcasts to complete
    broadcast_results = await asyncio.gather(*tasks, return_exceptions=True)
    
    # Count results
    for result in broadcast_results:
        if isinstance(result, Exception):
            results["failed"] += 1
            logger.error(f"Batch broadcast error: {result}")
        elif result:
            results["success"] += 1
        else:
            results["failed"] += 1
    
    logger.info(
        f"📊 Batch broadcast complete: "
        f"{results['success']} successful, {results['failed']} failed"
    )
    
    return results


def create_broadcast_payload(
    message_type: str,
    language: str,
    text: str,
    room_id: int,
    mosque_id: int,
    additional_data: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """
    Create a standardized broadcast payload.
    
    Args:
        message_type: Type of message
        language: Language code
        text: The text content
        room_id: Room ID
        mosque_id: Mosque ID
        additional_data: Optional additional data to include
        
    Returns:
        Formatted payload dictionary
    """
    # Generate unique message ID
    timestamp = datetime.utcnow().isoformat() + "Z"
    text_hash = hashlib.md5(text.encode('utf-8')).hexdigest()[:8]
    msg_id = f"{timestamp}_{text_hash}"
    
    payload = {
        "type": message_type,
        "room_id": room_id,
        "mosque_id": mosque_id,
        "data": {
            "text": text,
            "language": language,
            "timestamp": timestamp,
            "msg_id": msg_id
        }
    }
    
    if additional_data:
        payload["data"].update(additional_data)
    
    return payload


def get_channel_name(room_id: int, mosque_id: int) -> str:
    """
    Generate the channel name for a room.
    
    Args:
        room_id: Room ID
        mosque_id: Mosque ID
        
    Returns:
        Channel name string
    """
    return f"live-transcription-{room_id}-{mosque_id}"