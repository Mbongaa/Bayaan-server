"""
Enhanced Database operations with ghost session prevention.
This module should replace the ensure_active_session function in database.py
"""
import asyncio
import logging
from typing import Optional, Dict, Any
from datetime import datetime, timedelta
import aiohttp

from config import get_config

logger = logging.getLogger("transcriber.database_enhanced")
config = get_config()


async def ensure_active_session_atomic(
    room_id: int, 
    mosque_id: int,
    session: aiohttp.ClientSession,
    headers: Dict[str, str]
) -> Optional[str]:
    """
    Atomically ensure there's an active session for the room.
    Uses database-level locking to prevent ghost sessions.
    
    Args:
        room_id: The room ID
        mosque_id: The mosque ID
        session: Active aiohttp session
        headers: Supabase headers with auth
        
    Returns:
        Session ID or None on failure
    """
    try:
        # Use the atomic database function
        url = f"{config.supabase.url}/rest/v1/rpc/ensure_room_session_atomic"
        data = {
            "p_room_id": room_id,
            "p_mosque_id": mosque_id,
            "p_source": "livekit_agent"
        }
        
        timeout = aiohttp.ClientTimeout(total=config.supabase.http_timeout)
        
        async with session.post(
            url, 
            json=data, 
            headers=headers, 
            timeout=timeout
        ) as response:
            if response.status == 200:
                result = await response.json()
                if result and isinstance(result, list) and len(result) > 0:
                    result_data = result[0]
                elif isinstance(result, dict):
                    result_data = result
                else:
                    logger.error(f"Unexpected response format: {result}")
                    return None
                
                if 'error' in result_data:
                    logger.error(f"Database error: {result_data['error']}")
                    return None
                
                session_id = result_data.get('session_id')
                cleaned = result_data.get('cleaned_sessions', 0)
                
                if cleaned > 0:
                    logger.info(f"🧹 Cleaned {cleaned} stale sessions before creating new one")
                
                if session_id:
                    logger.info(f"✅ Active session ensured: {session_id}")
                    return session_id
                else:
                    logger.error("No session_id returned from atomic function")
                    return None
            else:
                error_text = await response.text()
                logger.error(f"Failed to ensure session: {response.status} - {error_text}")
                return None
                
    except asyncio.TimeoutError:
        logger.error("Timeout in ensure_active_session_atomic")
        return None
    except Exception as e:
        logger.error(f"Error in ensure_active_session_atomic: {e}")
        return None


async def update_session_heartbeat_enhanced(
    session_id: str,
    session: aiohttp.ClientSession,
    headers: Dict[str, str]
) -> bool:
    """
    Update session heartbeat with enhanced tracking.
    
    Args:
        session_id: The session ID to update
        session: Active aiohttp session
        headers: Supabase headers with auth
        
    Returns:
        True if successful, False otherwise
    """
    if not session_id:
        return False
        
    try:
        url = f"{config.supabase.url}/rest/v1/rpc/update_session_heartbeat_enhanced"
        data = {"p_session_id": session_id}
        
        timeout = aiohttp.ClientTimeout(total=5.0)  # Quick timeout for heartbeats
        
        async with session.post(
            url, 
            json=data, 
            headers=headers, 
            timeout=timeout
        ) as response:
            if response.status == 200:
                result = await response.json()
                if result and isinstance(result, list) and len(result) > 0:
                    result_data = result[0]
                elif isinstance(result, dict):
                    result_data = result
                else:
                    return False
                
                success = result_data.get('success', False)
                if success:
                    logger.debug(f"💓 Heartbeat updated for session {session_id}")
                else:
                    reason = result_data.get('reason', 'unknown')
                    logger.warning(f"Heartbeat failed for {session_id}: {reason}")
                
                return success
            else:
                error_text = await response.text()
                logger.warning(f"Heartbeat update failed: {response.status} - {error_text}")
                return False
                
    except asyncio.TimeoutError:
        logger.warning(f"Heartbeat timeout for session {session_id}")
        return False
    except Exception as e:
        logger.error(f"Error updating heartbeat for {session_id}: {e}")
        return False


class SessionHealthMonitor:
    """
    Monitors session health and handles automatic recovery.
    """
    
    def __init__(self):
        self.missed_heartbeats: Dict[str, int] = {}
        self.recovery_attempts: Dict[str, int] = {}
        self.last_heartbeat: Dict[str, datetime] = {}
        
    async def monitor_heartbeat(
        self, 
        session_id: str,
        session: aiohttp.ClientSession,
        headers: Dict[str, str]
    ) -> bool:
        """
        Monitor heartbeat with automatic recovery on failure.
        
        Returns:
            True if healthy, False if recovery needed
        """
        try:
            # Update heartbeat
            success = await update_session_heartbeat_enhanced(
                session_id, session, headers
            )
            
            if success:
                self.missed_heartbeats[session_id] = 0
                self.recovery_attempts[session_id] = 0
                self.last_heartbeat[session_id] = datetime.utcnow()
                return True
            else:
                self.missed_heartbeats[session_id] = \
                    self.missed_heartbeats.get(session_id, 0) + 1
                
                # Check if we need recovery
                if self.missed_heartbeats[session_id] >= 3:
                    logger.warning(
                        f"Session {session_id} missed {self.missed_heartbeats[session_id]} heartbeats"
                    )
                    return False
                    
                return True
                
        except Exception as e:
            logger.error(f"Heartbeat monitoring error: {e}")
            return False
    
    def should_force_cleanup(self, session_id: str) -> bool:
        """
        Determine if a session should be forcefully cleaned up.
        """
        # Too many recovery attempts
        if self.recovery_attempts.get(session_id, 0) >= 3:
            return True
            
        # No heartbeat for too long
        last_beat = self.last_heartbeat.get(session_id)
        if last_beat and (datetime.utcnow() - last_beat) > timedelta(minutes=10):
            return True
            
        # Too many missed heartbeats
        if self.missed_heartbeats.get(session_id, 0) >= 10:
            return True
            
        return False
    
    def increment_recovery_attempt(self, session_id: str):
        """Track recovery attempts."""
        self.recovery_attempts[session_id] = \
            self.recovery_attempts.get(session_id, 0) + 1
    
    def cleanup_session_tracking(self, session_id: str):
        """Remove session from tracking."""
        self.missed_heartbeats.pop(session_id, None)
        self.recovery_attempts.pop(session_id, None)
        self.last_heartbeat.pop(session_id, None)


# Example integration in your main code:
"""
# In database.py, replace ensure_active_session with:
from database_enhanced import ensure_active_session_atomic

async def ensure_active_session(room_id: int, mosque_id: int) -> Optional[str]:
    session = await _pool.get_session()
    async with get_db_headers() as headers:
        return await ensure_active_session_atomic(
            room_id, mosque_id, session, headers
        )

# In main.py, add health monitoring:
from database_enhanced import SessionHealthMonitor

# Initialize monitor
health_monitor = SessionHealthMonitor()

# In your heartbeat periodic function:
async def update_session_heartbeat_periodic():
    while not stop_heartbeat:
        try:
            if tenant_context and tenant_context.get('session_id'):
                session_id = tenant_context['session_id']
                
                # Use health monitor
                healthy = await health_monitor.monitor_heartbeat(
                    session_id, session, headers
                )
                
                if not healthy:
                    if health_monitor.should_force_cleanup(session_id):
                        logger.error(f"Force cleanup needed for {session_id}")
                        await close_room_session(session_id)
                        break
                    else:
                        health_monitor.increment_recovery_attempt(session_id)
                        
            await asyncio.sleep(30)
        except Exception as e:
            logger.error(f"Heartbeat error: {e}")
            await asyncio.sleep(30)
"""