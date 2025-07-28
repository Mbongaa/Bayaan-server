"""
Database operations for LiveKit AI Translation Server.
Handles all Supabase database interactions with connection pooling and async support.
FIXED: Thread-safe connection pool that works with LiveKit's multi-process architecture.
"""
import asyncio
import logging
import uuid
from typing import Optional, Dict, Any
from datetime import datetime
import aiohttp
from contextlib import asynccontextmanager
import threading

from config import get_config

logger = logging.getLogger("transcriber.database")
config = get_config()


class ThreadSafeDatabasePool:
    """Thread-safe database connection pool that creates separate pools per thread/process."""
    
    def __init__(self, max_connections: int = 10):
        self.max_connections = max_connections
        self._local = threading.local()
        self._lock = threading.Lock()
        
    async def get_session(self) -> aiohttp.ClientSession:
        """Get or create a session for the current thread."""
        # Check if current thread has a session
        if not hasattr(self._local, 'session') or self._local.session is None or self._local.session.closed:
            # Create new session for this thread
            connector = aiohttp.TCPConnector(
                limit=self.max_connections,
                limit_per_host=self.max_connections,
                force_close=True  # Force close to avoid connection issues
            )
            self._local.session = aiohttp.ClientSession(
                connector=connector,
                trust_env=True  # Trust environment proxy settings
            )
            logger.debug(f"Created new connection pool for thread {threading.current_thread().ident}")
        
        return self._local.session
    
    async def close(self):
        """Close the session for current thread."""
        if hasattr(self._local, 'session') and self._local.session and not self._local.session.closed:
            await self._local.session.close()
            self._local.session = None
            logger.debug(f"Closed connection pool for thread {threading.current_thread().ident}")


# Use thread-safe pool
_pool = ThreadSafeDatabasePool()


@asynccontextmanager
async def get_db_headers():
    """Get headers for Supabase API requests."""
    if not config.supabase.service_role_key:
        raise ValueError("SUPABASE_SERVICE_ROLE_KEY not configured")
    
    yield {
        'apikey': config.supabase.service_role_key,
        'Authorization': f'Bearer {config.supabase.service_role_key}',
        'Content-Type': 'application/json'
    }


async def ensure_active_session(room_id: int, mosque_id: int) -> Optional[str]:
    """
    Ensure there's an active session for the room and return session_id.
    
    This function:
    1. Checks for existing active sessions
    2. Creates a new session if none exists
    3. Returns the session ID or None on failure
    """
    try:
        # Get session from thread-safe pool
        session = await _pool.get_session()
        
        async with get_db_headers() as headers:
            # Check for existing active session
            url = f"{config.supabase.url}/rest/v1/room_sessions"
            params = {
                "room_id": f"eq.{room_id}",
                "status": "eq.active",
                "select": "id,started_at",
                "order": "started_at.desc",
                "limit": "1"
            }
            
            timeout = aiohttp.ClientTimeout(total=config.supabase.http_timeout)
            
            try:
                async with session.get(url, headers=headers, params=params, timeout=timeout) as response:
                    if response.status == 200:
                        sessions = await response.json()
                        if sessions and len(sessions) > 0:
                            session_id = sessions[0]["id"]
                            logger.debug(f"📝 Using existing active session: {session_id}")
                            return session_id
                    else:
                        error_text = await response.text()
                        logger.warning(f"Failed to check existing sessions: {response.status} - {error_text}")
            except asyncio.TimeoutError:
                logger.warning("Timeout checking for existing sessions")
            except Exception as e:
                logger.error(f"Error checking sessions: {e}")
            
            # Create new session if none exists
            new_session_id = str(uuid.uuid4())
            session_data = {
                "id": new_session_id,
                "room_id": room_id,
                "mosque_id": mosque_id,
                "status": "active",
                "started_at": datetime.utcnow().isoformat() + "Z",
                "logging_enabled": True
            }
            
            try:
                async with session.post(
                    url,
                    json=session_data,
                    headers={**headers, 'Prefer': 'return=minimal'},
                    timeout=timeout
                ) as response:
                    if response.status in [200, 201]:
                        logger.info(f"📝 Created new session: {new_session_id}")
                        return new_session_id
                    else:
                        error_text = await response.text()
                        logger.error(f"❌ Failed to create session: {response.status} - {error_text}")
                        return None
            except asyncio.TimeoutError:
                logger.error("Timeout creating new session")
                return None
            except Exception as e:
                logger.error(f"Error creating session: {e}")
                return None
                    
    except Exception as e:
        logger.error(f"❌ Session management failed: {e}")
        return None


async def store_transcript_in_database(
    message_type: str, 
    language: str, 
    text: str, 
    tenant_context: Dict[str, Any],
    sentence_context: Optional[Dict[str, Any]] = None
) -> bool:
    """
    Store transcription/translation in Supabase database.
    
    Args:
        message_type: Either "transcription" or "translation"
        language: Language code (e.g., "ar", "nl")
        text: The text to store
        tenant_context: Context containing room_id, mosque_id, session_id
        sentence_context: Optional context containing sentence_id, is_complete, is_fragment
        
    Returns:
        bool: True if successful, False otherwise
    """
    try:
        if not config.supabase.service_role_key:
            logger.error("❌ SUPABASE_SERVICE_ROLE_KEY not found - cannot store transcripts")
            return False
            
        room_id = tenant_context.get("room_id")
        mosque_id = tenant_context.get("mosque_id")
        session_id = tenant_context.get("session_id")
        
        if not room_id or not mosque_id:
            logger.warning(f"⚠️ Missing room context: room_id={room_id}, mosque_id={mosque_id}")
            return False
            
        # Ensure we have an active session
        if not session_id:
            session_id = await ensure_active_session(room_id, mosque_id)
            if session_id:
                tenant_context["session_id"] = session_id
            else:
                logger.error("❌ Could not establish session - skipping database storage")
                return False
        
        # Prepare transcript data
        transcript_data = {
            "room_id": room_id,
            "session_id": session_id,
            "timestamp": datetime.utcnow().isoformat() + "Z",
        }
        
        # Add sentence context if provided
        if sentence_context:
            transcript_data["sentence_id"] = sentence_context.get("sentence_id")
            transcript_data["is_complete"] = sentence_context.get("is_complete", False)
            transcript_data["is_fragment"] = sentence_context.get("is_fragment", True)
        
        # Set appropriate field based on message type
        if message_type == "transcription":
            transcript_data["transcription_segment"] = text
        else:  # translation
            transcript_data["translation_segment"] = text
            
        # Store in database
        session = await _pool.get_session()
        async with get_db_headers() as headers:
            timeout = aiohttp.ClientTimeout(total=config.supabase.http_timeout)
            
            try:
                async with session.post(
                    f"{config.supabase.url}/rest/v1/transcripts",
                    json=transcript_data,
                    headers={**headers, 'Prefer': 'return=minimal'},
                    timeout=timeout
                ) as response:
                    if response.status in [200, 201]:
                        logger.debug(f"✅ Stored {message_type} in database: room_id={room_id}, session_id={session_id[:8]}")
                        return True
                    else:
                        error_text = await response.text()
                        logger.warning(f"⚠️ Database storage failed with status {response.status}: {error_text}")
                        return False
            except asyncio.TimeoutError:
                logger.warning("Timeout storing transcript")
                return False
            except Exception as e:
                logger.error(f"Error storing transcript: {e}")
                return False
                    
    except Exception as e:
        logger.error(f"❌ Database storage error: {e}")
        return False


async def query_room_by_name(room_name: str) -> Optional[Dict[str, Any]]:
    """
    Query room information by LiveKit room name.
    
    Args:
        room_name: The LiveKit room name
        
    Returns:
        Room data dictionary or None if not found
    """
    try:
        session = await _pool.get_session()
        async with get_db_headers() as headers:
            url = f"{config.supabase.url}/rest/v1/rooms"
            params = {"Livekit_room_name": f"eq.{room_name}"}
            
            timeout = aiohttp.ClientTimeout(total=config.supabase.http_timeout)
            
            try:
                async with session.get(url, headers=headers, params=params, timeout=timeout) as response:
                    if response.status == 200:
                        rooms = await response.json()
                        if rooms and len(rooms) > 0:
                            return rooms[0]
                    else:
                        error_text = await response.text()
                        logger.warning(f"Failed to query room: {response.status} - {error_text}")
            except asyncio.TimeoutError:
                logger.warning("Timeout querying room")
            except Exception as e:
                logger.error(f"Error querying room: {e}")
        
        return None
        
    except Exception as e:
        logger.error(f"❌ Room query failed: {e}")
        return None


async def get_active_session_for_room(room_id: int) -> Optional[str]:
    """
    Get the active session ID for a room if one exists.
    
    Args:
        room_id: The room ID
        
    Returns:
        Session ID or None if no active session
    """
    try:
        session = await _pool.get_session()
        async with get_db_headers() as headers:
            url = f"{config.supabase.url}/rest/v1/room_sessions"
            params = {
                "room_id": f"eq.{room_id}",
                "status": "eq.active",
                "select": "id",
                "order": "started_at.desc",
                "limit": "1"
            }
            
            timeout = aiohttp.ClientTimeout(total=config.supabase.http_timeout)
            
            try:
                async with session.get(url, headers=headers, params=params, timeout=timeout) as response:
                    if response.status == 200:
                        sessions = await response.json()
                        if sessions and len(sessions) > 0:
                            return sessions[0].get("id")
            except asyncio.TimeoutError:
                logger.warning("Timeout getting active session")
            except Exception as e:
                logger.error(f"Error getting active session: {e}")
        
        return None
        
    except Exception as e:
        logger.error(f"❌ Active session query failed: {e}")
        return None


async def broadcast_to_channel(
    channel_name: str,
    event_type: str,
    payload: Dict[str, Any]
) -> bool:
    """
    Broadcast a message to a Supabase channel.
    
    Args:
        channel_name: The channel to broadcast to
        event_type: The event type (e.g., "transcription", "translation")
        payload: The data to broadcast
        
    Returns:
        bool: True if successful, False otherwise
    """
    try:
        if not config.supabase.service_role_key:
            logger.warning("⚠️ SUPABASE_SERVICE_ROLE_KEY not found - skipping broadcast")
            return False
            
        session = await _pool.get_session()
        
        async with get_db_headers() as headers:
            # Use broadcast-specific timeout
            broadcast_timeout = aiohttp.ClientTimeout(total=config.supabase.broadcast_timeout)
            
            try:
                async with session.post(
                    f"{config.supabase.url}/functions/v1/broadcast",
                    json={
                        "channel": channel_name,
                        "event": event_type,
                        "payload": payload
                    },
                    headers=headers,
                    timeout=broadcast_timeout
                ) as response:
                    if response.status == 200:
                        return True
                    else:
                        error_text = await response.text()
                        logger.warning(f"⚠️ Broadcast failed: {response.status} - {error_text}")
                        return False
            except asyncio.TimeoutError:
                logger.warning(f"⚠️ Broadcast timeout for channel {channel_name}")
                return False
            except Exception as e:
                logger.error(f"Error broadcasting: {e}")
                return False
                    
    except Exception as e:
        logger.error(f"❌ Broadcast error: {e}")
        return False


async def query_prompt_template_for_room(room_id: int) -> Optional[Dict[str, Any]]:
    """
    Query the prompt template for a specific room.
    
    Args:
        room_id: The room ID
        
    Returns:
        Template data dictionary or None if not found
    """
    try:
        session = await _pool.get_session()
        async with get_db_headers() as headers:
            # Use the database function to get the appropriate template
            url = f"{config.supabase.url}/rest/v1/rpc/get_room_prompt_template"
            data = {"room_id": room_id}
            
            timeout = aiohttp.ClientTimeout(total=config.supabase.http_timeout)
            
            try:
                async with session.post(url, headers=headers, json=data, timeout=timeout) as response:
                    if response.status == 200:
                        result = await response.json()
                        if result and len(result) > 0:
                            template = result[0]
                            # Parse template_variables if it's a string
                            if isinstance(template.get('template_variables'), str):
                                try:
                                    import json
                                    template['template_variables'] = json.loads(template['template_variables'])
                                except:
                                    template['template_variables'] = {}
                            return template
                    else:
                        error_text = await response.text()
                        logger.warning(f"Failed to query prompt template: {response.status} - {error_text}")
            except asyncio.TimeoutError:
                logger.warning("Timeout querying prompt template")
            except Exception as e:
                logger.error(f"Error querying prompt template: {e}")
        
        return None
        
    except Exception as e:
        logger.error(f"❌ Prompt template query failed: {e}")
        return None


async def update_session_heartbeat(session_id: str) -> bool:
    """
    Update the last_active timestamp for a session to prevent it from being cleaned up.
    
    Args:
        session_id: The session ID to update
        
    Returns:
        True if successful, False otherwise
    """
    if not session_id:
        return False
        
    try:
        session = await _pool.get_session()
        async with get_db_headers() as headers:
            # Update the last_active timestamp
            url = f"{config.supabase.url}/rest/v1/room_sessions"
            params = {"id": f"eq.{session_id}"}
            data = {"last_active": datetime.utcnow().isoformat()}
            
            timeout = aiohttp.ClientTimeout(total=config.supabase.http_timeout)
            
            try:
                async with session.patch(url, headers=headers, params=params, json=data, timeout=timeout) as response:
                    if response.status in [200, 204]:
                        logger.debug(f"💓 Session heartbeat updated for {session_id}")
                        return True
                    else:
                        error_text = await response.text()
                        logger.warning(f"Failed to update session heartbeat: {response.status} - {error_text}")
                        return False
            except asyncio.TimeoutError:
                logger.warning(f"Timeout updating session heartbeat {session_id}")
                return False
            except Exception as e:
                logger.error(f"Error updating session heartbeat {session_id}: {e}")
                return False
                
    except Exception as e:
        logger.error(f"❌ Failed to update session heartbeat {session_id}: {e}")
        return False


async def close_room_session(session_id: str) -> bool:
    """
    Close a room session by marking it as completed in the database.
    
    Args:
        session_id: The session ID to close
        
    Returns:
        True if successful, False otherwise
    """
    if not session_id:
        logger.warning("No session_id provided to close_room_session")
        return False
        
    try:
        session = await _pool.get_session()
        async with get_db_headers() as headers:
            # Call the cleanup_session_idempotent function
            url = f"{config.supabase.url}/rest/v1/rpc/cleanup_session_idempotent"
            data = {
                "p_session_id": session_id,
                "p_source": "agent_disconnect"
            }
            
            timeout = aiohttp.ClientTimeout(total=config.supabase.http_timeout)
            
            try:
                async with session.post(url, headers=headers, json=data, timeout=timeout) as response:
                    if response.status == 200:
                        result = await response.json()
                        logger.info(f"✅ Session {session_id} closed successfully")
                        return True
                    else:
                        error_text = await response.text()
                        logger.error(f"Failed to close session: {response.status} - {error_text}")
                        return False
            except asyncio.TimeoutError:
                logger.warning(f"Timeout closing session {session_id}")
                return False
            except Exception as e:
                logger.error(f"Error closing session {session_id}: {e}")
                return False
                
    except Exception as e:
        logger.error(f"❌ Failed to close session {session_id}: {e}")
        return False


async def close_database_connections():
    """Close all database connections. Call this on shutdown."""
    await _pool.close()
    logger.info("✅ Database connections closed")