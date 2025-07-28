# Main.py Integration for Ghost Session Fix

## Required Changes:

### 1. Import the health monitor (at top of file):
```python
from database import get_health_monitor, update_session_heartbeat_with_monitor
```

### 2. Initialize health monitor (in entrypoint function):
```python
# After initializing resource_manager
health_monitor = get_health_monitor()
```

### 3. Update the heartbeat periodic function:
Replace the existing `update_session_heartbeat_periodic` function with:

```python
async def update_session_heartbeat_periodic():
    """Periodically update session heartbeat with health monitoring."""
    while not stop_heartbeat:
        try:
            if tenant_context and tenant_context.get('session_id'):
                session_id = tenant_context['session_id']
                
                # Use enhanced heartbeat with monitoring
                success = await update_session_heartbeat_with_monitor(session_id)
                
                if not success:
                    logger.error(f"Session {session_id} health check failed - may need cleanup")
                    # The monitor will handle cleanup if needed
                    
            await asyncio.sleep(30)
        except Exception as e:
            logger.error(f"Heartbeat task error: {e}")
            await asyncio.sleep(30)
```

### 4. Add cleanup on connect (in entrypoint, after room join):
```python
# Clean any stale sessions for this room on connect
if tenant_context.get('room_id'):
    logger.info("Checking for stale sessions on connect...")
    # The atomic session creation will handle cleanup automatically
```

## Testing the Integration:

1. Check logs for "🧹 Cleaned X stale sessions" messages
2. Monitor for "Session health check failed" warnings
3. Verify no ghost sessions in database after disconnects
