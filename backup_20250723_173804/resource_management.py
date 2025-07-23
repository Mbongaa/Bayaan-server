"""
Resource management module for LiveKit AI Translation Server.
Handles tracking and cleanup of async tasks, STT streams, and other resources.
"""
import asyncio
import logging
from typing import Set, List, Dict, Any, Optional, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime
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
        self._stats = ResourceStats()
        logger.info("🎤 STTStreamManager initialized")
    
    @asynccontextmanager
    async def create_stream(self, stt_provider, participant_id: str):
        """
        Create and manage an STT stream.
        
        Args:
            stt_provider: STT provider instance
            participant_id: ID of the participant
            
        Yields:
            STT stream instance
        """
        stream = None
        try:
            # Create stream
            stream = stt_provider.stream()
            self._streams.add(stream)
            self._stream_metadata[stream] = {
                "participant_id": participant_id,
                "created_at": datetime.utcnow()
            }
            self._stats.streams_opened += 1
            self._stats.active_streams = len(self._streams)
            
            logger.info(f"🎤 Created STT stream for {participant_id}")
            yield stream
            
        finally:
            # Cleanup stream
            if stream:
                try:
                    await stream.aclose()
                    logger.info(f"✅ STT stream closed for {participant_id}")
                except Exception as e:
                    logger.error(f"Error closing STT stream: {e}")
                finally:
                    self._streams.discard(stream)
                    self._stream_metadata.pop(stream, None)
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
        
        self._stats.active_streams = 0
        logger.info("✅ All STT streams closed")
    
    def get_stats(self) -> ResourceStats:
        """Get current statistics."""
        return self._stats


class ResourceManager:
    """
    Central resource manager for the application.
    Coordinates TaskManager and STTStreamManager.
    """
    
    def __init__(self):
        self.task_manager = TaskManager("main")
        self.stt_manager = STTStreamManager()
        self._shutdown_handlers: List[Callable] = []
        logger.info("🏗️ ResourceManager initialized")
    
    async def __aenter__(self):
        """Context manager entry."""
        await self.task_manager.__aenter__()
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit with cleanup."""
        await self.shutdown()
    
    def add_shutdown_handler(self, handler: Callable):
        """Add a handler to be called on shutdown."""
        self._shutdown_handlers.append(handler)
    
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
        await self.task_manager.shutdown()
        await self.stt_manager.close_all()
        
        logger.info("✅ ResourceManager shutdown complete")
    
    def get_all_stats(self) -> Dict[str, ResourceStats]:
        """Get statistics from all managers."""
        return {
            "tasks": self.task_manager.get_stats(),
            "stt_streams": self.stt_manager.get_stats()
        }
    
    def log_stats(self):
        """Log current resource statistics."""
        stats = self.get_all_stats()
        logger.info(
            f"📊 Resource Stats - "
            f"Tasks: {stats['tasks'].active_tasks} active "
            f"({stats['tasks'].tasks_completed} completed, "
            f"{stats['tasks'].tasks_failed} failed, "
            f"{stats['tasks'].tasks_cancelled} cancelled), "
            f"STT Streams: {stats['stt_streams'].active_streams} active"
        )