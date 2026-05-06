"""
Resource management module for LiveKit AI Translation Server.
Handles tracking and cleanup of async tasks, STT streams, and other resources.
"""
import asyncio
import logging
from typing import Set, List, Dict, Any, Optional, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
import time
import weakref

logger = logging.getLogger("transcriber.resources")


@dataclass
class ResourceStats:
    """Statistics about managed resources."""
    tasks_created: int = 0
    tasks_completed: int = 0
    tasks_failed: int = 0
    tasks_cancelled: int = 0
    streams_opened: int = 0
    streams_closed: int = 0
    active_tasks: int = 0
    active_streams: int = 0


class TaskManager:
    """
    Manages async tasks with proper tracking and cleanup.
    
    Features:
    - Automatic task tracking
    - Graceful cancellation
    - Resource leak prevention
    - Statistics tracking
    """
    
    def __init__(self, name: str = "default"):
        self.name = name
        self._tasks: Set[asyncio.Task] = set()
        self._task_metadata: Dict[asyncio.Task, Dict[str, Any]] = {}
        self._stats = ResourceStats()
        self._cleanup_interval = 30.0  # seconds
        self._cleanup_task: Optional[asyncio.Task] = None
        self._shutdown = False
        logger.info(f"📋 TaskManager '{self.name}' initialized")
    
    async def __aenter__(self):
        """Context manager entry."""
        if not self._cleanup_task or self._cleanup_task.done():
            self._cleanup_task = asyncio.create_task(self._periodic_cleanup())
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit with cleanup."""
        await self.shutdown()
    
    def create_task(
        self, 
        coro, 
        name: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None
    ) -> asyncio.Task:
        """
        Create and track an async task.
        
        Args:
            coro: Coroutine to run
            name: Optional task name
            metadata: Optional metadata for the task
            
        Returns:
            Created task
        """
        if self._shutdown:
            raise RuntimeError("TaskManager is shutting down")
        
        task = asyncio.create_task(coro, name=name)
        self._tasks.add(task)
        
        if metadata:
            self._task_metadata[task] = metadata
        
        # Add callback to clean up when done
        task.add_done_callback(self._task_done_callback)
        
        self._stats.tasks_created += 1
        self._stats.active_tasks = len(self._tasks)
        
        logger.debug(f"📌 Created task: {name or task.get_name()} (total: {len(self._tasks)})")
        return task
    
    def _task_done_callback(self, task: asyncio.Task):
        """Callback when a task completes."""
        self._tasks.discard(task)
        self._task_metadata.pop(task, None)
        
        try:
            if task.cancelled():
                self._stats.tasks_cancelled += 1
                logger.debug(f"🚫 Task cancelled: {task.get_name()}")
            elif task.exception():
                self._stats.tasks_failed += 1
                logger.error(f"❌ Task failed: {task.get_name()}", exc_info=task.exception())
            else:
                self._stats.tasks_completed += 1
                logger.debug(f"✅ Task completed: {task.get_name()}")
        except Exception as e:
            logger.debug(f"Error in task callback: {e}")
        
        self._stats.active_tasks = len(self._tasks)
    
    async def _periodic_cleanup(self):
        """Periodically clean up completed tasks."""
        while not self._shutdown:
            try:
                await asyncio.sleep(self._cleanup_interval)
                
                # Clean up any references to completed tasks
                completed = [t for t in self._tasks if t.done()]
                for task in completed:
                    self._tasks.discard(task)
                    self._task_metadata.pop(task, None)
                
                if completed:
                    logger.debug(f"🧹 Cleaned up {len(completed)} completed tasks")
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in periodic cleanup: {e}")
    
    async def cancel_all(self, timeout: float = 5.0) -> int:
        """
        Cancel all active tasks.
        
        Args:
            timeout: Maximum time to wait for cancellation
            
        Returns:
            Number of tasks cancelled
        """
        if not self._tasks:
            return 0
        
        tasks_to_cancel = list(self._tasks)
        cancelled_count = 0
        
        logger.info(f"🚫 Cancelling {len(tasks_to_cancel)} tasks...")
        
        # Cancel all tasks
        for task in tasks_to_cancel:
            if not task.done():
                task.cancel()
                cancelled_count += 1
        
        # Wait for cancellation with timeout
        if tasks_to_cancel:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*tasks_to_cancel, return_exceptions=True),
                    timeout=timeout
                )
            except asyncio.TimeoutError:
                logger.warning(f"⏰ Timeout waiting for {len(tasks_to_cancel)} tasks to cancel")
        
        logger.info(f"✅ Cancelled {cancelled_count} tasks")
        return cancelled_count
    
    async def shutdown(self):
        """Shutdown the task manager and cleanup all resources."""
        if self._shutdown:
            return
        
        self._shutdown = True
        logger.info(f"🛑 Shutting down TaskManager '{self.name}'...")
        
        # Cancel cleanup task
        if self._cleanup_task and not self._cleanup_task.done():
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass
        
        # Cancel all managed tasks
        await self.cancel_all()
        
        logger.info(f"✅ TaskManager '{self.name}' shutdown complete")
    
    def get_stats(self) -> ResourceStats:
        """Get current statistics."""
        return self._stats
    
    def get_active_tasks(self) -> List[asyncio.Task]:
        """Get list of active tasks."""
        return list(self._tasks)


class STTStreamManager:
    """
    Manages STT (Speech-to-Text) streams with proper cleanup.
    """
    
    def __init__(self):
        self._streams: Set[Any] = set()
        self._stream_metadata: Dict[Any, Dict[str, Any]] = {}
        self._participant_streams: Dict[tuple[str, str], Any] = {}  # (participant, track) -> stream
        self._cleanup_locks: Dict[tuple[str, str], asyncio.Lock] = {}  # Prevent race conditions
        self._participant_disconnect_times: Dict[str, float] = {}  # Track disconnect times
        self._reconnect_grace_period = 3.0  # seconds to wait before allowing reconnect
        self._stats = ResourceStats()
        logger.info("🎤 STTStreamManager initialized")
    
    def _stream_key(self, participant_id: str, track_id: Optional[str] = None) -> tuple[str, str]:
        return (participant_id, track_id or "__default__")
    
    @asynccontextmanager
    async def create_stream(self, stt_provider, participant_id: str, track_id: Optional[str] = None):
        """
        Create and manage an STT stream.
        
        Args:
            stt_provider: STT provider instance
            participant_id: ID of the participant
            track_id: Optional LiveKit track SID for exact track lifecycle cleanup
            
        Yields:
            STT stream instance
        """
        stream_key = self._stream_key(participant_id, track_id)
        
        # Get or create lock for this participant track
        if stream_key not in self._cleanup_locks:
            self._cleanup_locks[stream_key] = asyncio.Lock()
        
        async with self._cleanup_locks[stream_key]:
            # Check for recent disconnect - implement debouncing
            last_disconnect = self._participant_disconnect_times.get(participant_id, 0)
            time_since_disconnect = time.time() - last_disconnect
            
            if time_since_disconnect < self._reconnect_grace_period:
                wait_time = self._reconnect_grace_period - time_since_disconnect
                logger.warning(f"⏳ Participant {participant_id} reconnecting too quickly. Waiting {wait_time:.1f}s")
                await asyncio.sleep(wait_time)
            
            # Check if there's already an active stream for this participant track
            existing_stream = self._participant_streams.get(stream_key)
            if existing_stream:
                logger.warning(f"⚠️ Active STT stream already exists for {participant_id}, closing it first")
                try:
                    await existing_stream.aclose()
                except Exception as e:
                    logger.error(f"Error closing existing stream: {e}")
                finally:
                    self._streams.discard(existing_stream)
                    self._stream_metadata.pop(existing_stream, None)
                    self._participant_streams.pop(stream_key, None)
            
            stream = None
            try:
                # Create stream
                stream = stt_provider.stream()
                self._streams.add(stream)
                self._stream_metadata[stream] = {
                    "participant_id": participant_id,
                    "track_id": track_id,
                    "created_at": datetime.utcnow()
                }
                self._participant_streams[stream_key] = stream
                self._stats.streams_opened += 1
                self._stats.active_streams = len(self._streams)
                
                logger.info(f"🎤 Created STT stream for {participant_id}")
                yield stream
            
            finally:
                # Cleanup stream
                if stream:
                    stream_was_active = (
                        stream in self._streams or
                        self._participant_streams.get(stream_key) is stream
                    )
                    try:
                        if stream_was_active:
                            # Force close the stream
                            await stream.aclose()
                            logger.info(f"✅ STT stream closed for {participant_id}")
                    except Exception as e:
                        logger.error(f"Error closing STT stream: {e}")
                    finally:
                        self._streams.discard(stream)
                        self._stream_metadata.pop(stream, None)
                        if self._participant_streams.get(stream_key) is stream:
                            self._participant_streams.pop(stream_key, None)
                        # Record disconnect time for debouncing
                        self._participant_disconnect_times[participant_id] = time.time()
                        if stream_was_active:
                            self._stats.streams_closed += 1
                        self._stats.active_streams = len(self._streams)
    
    async def close_all(self):
        """Close all active streams."""
        if not self._streams:
            return
        
        logger.info(f"🚫 Closing {len(self._streams)} STT streams...")
        
        streams_to_close = list(self._streams)
        for stream in streams_to_close:
            try:
                await stream.aclose()
            except Exception as e:
                logger.error(f"Error closing stream: {e}")
            finally:
                self._streams.discard(stream)
                self._stream_metadata.pop(stream, None)
        
        self._participant_streams.clear()
        self._cleanup_locks.clear()
        self._participant_disconnect_times.clear()
        self._stats.active_streams = 0
        logger.info("✅ All STT streams closed")
    
    async def close_participant_stream(self, participant_id: str, track_id: Optional[str] = None):
        """Close a participant stream. If track_id is omitted, close all participant streams."""
        if track_id is None:
            keys_to_close = [
                key for key in list(self._participant_streams.keys())
                if key[0] == participant_id
            ]
        else:
            keys_to_close = [self._stream_key(participant_id, track_id)]
        
        keys_to_close = [key for key in keys_to_close if key in self._participant_streams]
        if not keys_to_close:
            return
        
        for stream_key in keys_to_close:
            if stream_key not in self._cleanup_locks:
                self._cleanup_locks[stream_key] = asyncio.Lock()
            
            async with self._cleanup_locks[stream_key]:
                stream = self._participant_streams.get(stream_key)
                if stream:
                    logger.info(
                        f"Closing STT stream for participant {participant_id} "
                        f"track={stream_key[1]}"
                    )
                    try:
                        await stream.aclose()
                    except Exception as e:
                        logger.error(f"Error closing participant stream: {e}")
                    finally:
                        self._streams.discard(stream)
                        self._stream_metadata.pop(stream, None)
                        self._participant_streams.pop(stream_key, None)
                        # Record disconnect time for debouncing
                        self._participant_disconnect_times[participant_id] = time.time()
                        self._stats.streams_closed += 1
                        self._stats.active_streams = len(self._streams)
    
    def prune_stale_data(self, max_age: float = 300.0):
        """
        Remove disconnect times and locks for participants who disconnected
        more than max_age seconds ago. Prevents unbounded dict growth over
        many connect/disconnect cycles.
        """
        now = time.time()
        stale = [pid for pid, t in self._participant_disconnect_times.items()
                 if now - t > max_age]
        for pid in stale:
            self._participant_disconnect_times.pop(pid, None)
            # Only remove lock if participant doesn't have an active stream
            if not any(key[0] == pid for key in self._participant_streams):
                for lock_key in [key for key in self._cleanup_locks if key[0] == pid]:
                    self._cleanup_locks.pop(lock_key, None)
        if stale:
            logger.debug(f"🧹 Pruned stale data for {len(stale)} disconnected participants")

    def get_stats(self) -> ResourceStats:
        """Get current statistics."""
        return self._stats


class HeartbeatMonitor:
    """
    Monitors participant activity and detects stuck sessions.
    """
    
    def __init__(self, timeout: float = 30.0):
        self.timeout = timeout
        self.participants: Dict[str, datetime] = {}
        self.session_info: Dict[str, Dict[str, Any]] = {}
        self._monitor_task: Optional[asyncio.Task] = None
        self._callbacks: List[Callable[[str], Any]] = []
        logger.info(f"💓 HeartbeatMonitor initialized with {timeout}s timeout")
    
    def register_callback(self, callback: Callable[[str], Any]):
        """Register a callback to be called when a participant times out."""
        self._callbacks.append(callback)
    
    async def update_heartbeat(self, participant_id: str, session_id: Optional[str] = None):
        """Update the heartbeat timestamp for a participant."""
        self.participants[participant_id] = datetime.utcnow()
        if session_id:
            self.session_info[participant_id] = {
                "session_id": session_id,
                "last_seen": datetime.utcnow()
            }
        logger.debug(f"💓 Heartbeat updated for {participant_id}")
    
    async def check_timeouts(self) -> List[str]:
        """Check for timed-out participants."""
        now = datetime.utcnow()
        timed_out = []
        
        for participant_id, last_seen in list(self.participants.items()):
            elapsed = (now - last_seen).total_seconds()
            if elapsed > self.timeout:
                timed_out.append(participant_id)
                logger.warning(f"⏰ Participant {participant_id} timed out (last seen {elapsed:.1f}s ago)")
                
                # Remove from tracking
                self.participants.pop(participant_id, None)
                session_info = self.session_info.pop(participant_id, None)
                
                # Call registered callbacks
                for callback in self._callbacks:
                    try:
                        if asyncio.iscoroutinefunction(callback):
                            await callback(participant_id)
                        else:
                            callback(participant_id)
                    except Exception as e:
                        logger.error(f"Error in heartbeat callback: {e}")
        
        return timed_out
    
    async def start_monitoring(self):
        """Start the heartbeat monitoring loop."""
        if self._monitor_task and not self._monitor_task.done():
            return
        
        async def monitor_loop():
            while True:
                try:
                    await asyncio.sleep(10)  # Check every 10 seconds
                    timed_out = await self.check_timeouts()
                    if timed_out:
                        logger.info(f"💔 {len(timed_out)} participants timed out")
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    logger.error(f"Error in heartbeat monitor: {e}")
        
        self._monitor_task = asyncio.create_task(monitor_loop())
        logger.info("💓 Heartbeat monitoring started")
    
    async def stop_monitoring(self):
        """Stop the heartbeat monitoring loop."""
        if self._monitor_task and not self._monitor_task.done():
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass
        self.participants.clear()
        self.session_info.clear()
        logger.info("💔 Heartbeat monitoring stopped")
    
    def remove_participant(self, participant_id: str):
        """Remove a participant from monitoring."""
        self.participants.pop(participant_id, None)
        self.session_info.pop(participant_id, None)
        logger.debug(f"💔 Participant {participant_id} removed from heartbeat monitoring")


class ResourceManager:
    """
    Central resource manager for the application.
    Coordinates TaskManager and STTStreamManager.
    """
    
    def __init__(self):
        self.task_manager = TaskManager("main")
        self.stt_manager = STTStreamManager()
        self.heartbeat_monitor = HeartbeatMonitor(timeout=45.0)  # 45 seconds timeout
        self._shutdown_handlers: List[Callable] = []
        logger.info("🏗️ ResourceManager initialized")
    
    async def __aenter__(self):
        """Context manager entry."""
        await self.start()
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit with cleanup."""
        await self.shutdown()
    
    def add_shutdown_handler(self, handler: Callable):
        """Add a handler to be called on shutdown."""
        self._shutdown_handlers.append(handler)
    
    async def start(self):
        """Start background resource monitoring tasks."""
        await self.task_manager.__aenter__()
        await self.heartbeat_monitor.start_monitoring()
    
    async def shutdown(self):
        """Shutdown all managed resources."""
        logger.info("🛑 Starting ResourceManager shutdown...")
        
        # Run shutdown handlers
        for handler in self._shutdown_handlers:
            try:
                if asyncio.iscoroutinefunction(handler):
                    await handler()
                else:
                    handler()
            except Exception as e:
                logger.error(f"Error in shutdown handler: {e}")
        
        # Shutdown managers
        await self.heartbeat_monitor.stop_monitoring()
        await self.task_manager.shutdown()
        await self.stt_manager.close_all()
        
        logger.info("✅ ResourceManager shutdown complete")
    
    def get_all_stats(self) -> Dict[str, Any]:
        """Get statistics from all managers."""
        return {
            "tasks": self.task_manager.get_stats(),
            "stt_streams": self.stt_manager.get_stats(),
            "heartbeat": {
                "active_participants": len(self.heartbeat_monitor.participants),
                "timeout": self.heartbeat_monitor.timeout
            }
        }
    
    def log_stats(self):
        """Log current resource statistics. Also prunes stale disconnect data."""
        # Prune stale disconnect times/locks on each stats log
        self.stt_manager.prune_stale_data()
        stats = self.get_all_stats()
        logger.info(
            f"📊 Resource Stats - "
            f"Tasks: {stats['tasks'].active_tasks} active "
            f"({stats['tasks'].tasks_completed} completed, "
            f"{stats['tasks'].tasks_failed} failed, "
            f"{stats['tasks'].tasks_cancelled} cancelled), "
            f"STT Streams: {stats['stt_streams'].active_streams} active, "
            f"Heartbeat: {stats['heartbeat']['active_participants']} participants"
        )
    
    async def verify_cleanup_complete(self) -> Dict[str, Any]:
        """Verify all resources are properly cleaned up."""
        active_tasks = self.task_manager.get_active_tasks()
        active_streams = len(self.stt_manager._streams)
        active_participants = len(self.heartbeat_monitor.participants)
        
        # Check if connection pool is closed
        db_closed = True
        try:
            from database import _pool
            if hasattr(_pool, '_local') and hasattr(_pool._local, 'session'):
                db_closed = _pool._local.session is None or _pool._local.session.closed
        except:
            pass
        
        cleanup_complete = (
            len(active_tasks) == 0 and 
            active_streams == 0 and 
            active_participants == 0 and
            db_closed
        )
        
        result = {
            "cleanup_complete": cleanup_complete,
            "tasks_remaining": len(active_tasks),
            "active_task_names": [t.get_name() for t in active_tasks],
            "streams_remaining": active_streams,
            "participants_remaining": active_participants,
            "participant_ids": list(self.heartbeat_monitor.participants.keys()),
            "db_connections_closed": db_closed,
            "timestamp": datetime.utcnow().isoformat()
        }
        
        if not cleanup_complete:
            logger.warning(f"⚠️ Cleanup verification failed: {result}")
        else:
            logger.info("✅ Cleanup verification passed - all resources cleaned up")
        
        return result
