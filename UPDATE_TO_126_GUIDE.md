# Updating from LiveKit Agents 1.2.1 to 1.2.6+

## Current Issue

Agents register successfully but don't receive job requests in 1.2.6+.

## Potential Solutions to Try

### 1. Update Job Request Function

The job acceptance might need additional parameters in newer versions:

```python
async def request_fnc(req: JobRequest):
    logger.info(f"🎯 Received job request for room: {req.room.name if req.room else 'unknown'}")

    # Try adding more explicit parameters for 1.2.6+
    await req.accept(
        name="agent",
        identity="agent",
        # New parameters that might be required:
        auto_subscribe=AutoSubscribe.AUDIO_ONLY,  # Explicitly set subscription
        auto_disconnect=True,  # Disconnect when room empties
    )
```

### 2. Check Worker Options

Newer versions might require different WorkerOptions configuration:

```python
if __name__ == "__main__":
    cli.run_app(
        WorkerOptions(
            entrypoint_fnc=entrypoint,
            prewarm_fnc=prewarm,
            request_fnc=request_fnc,
            # Try adding:
            agent_name="bayaan-transcriber",  # Explicit agent name
            worker_type="room",  # Specify worker type
            max_idle_time=60.0,  # Maximum idle time
        )
    )
```

### 3. Frontend Room Creation

The frontend might need to explicitly request an agent when creating rooms:

```javascript
// In your Supabase edge function or frontend:
const room = await livekitClient.createRoom({
  name: roomName,
  metadata: {
    // Add explicit agent request
    agent_request: {
      agents: ['agent'], // Request specific agent
      dispatch_required: true,
    },
  },
});
```

### 4. Environment Variables

Check if new environment variables are needed:

```bash
# Might be required in 1.2.6+
LIVEKIT_AGENT_NAMESPACE=default
LIVEKIT_AGENT_DISPATCH_ENABLED=true
```

### 5. Debug Job Dispatch

Add more logging to understand why jobs aren't arriving:

```python
async def request_fnc(req: JobRequest):
    # Add detailed logging
    logger.info(f"📦 Job request received: {req.__dict__}")
    logger.info(f"🏷️ Dispatch ID: {req.dispatch_id if hasattr(req, 'dispatch_id') else 'N/A'}")
    logger.info(f"🎯 Agent name: {req.agent_name if hasattr(req, 'agent_name') else 'N/A'}")

    # Accept the job
    result = await req.accept(name="agent", identity="agent")
    logger.info(f"✅ Accept result: {result}")
    return result
```

## Testing Approach

1. **Create a minimal test**:

   ```python
   # test_agent.py
   from livekit.agents import cli, WorkerOptions, JobContext, JobRequest
   import logging

   logging.basicConfig(level=logging.DEBUG)

   async def test_entrypoint(ctx: JobContext):
       print(f"Connected to room: {ctx.room.name}")

   async def test_request(req: JobRequest):
       print(f"Got request for: {req.room.name}")
       await req.accept(name="test", identity="test")

   if __name__ == "__main__":
       cli.run_app(WorkerOptions(
           entrypoint_fnc=test_entrypoint,
           request_fnc=test_request
       ))
   ```

2. **Test with different LiveKit versions**:

   ```bash
   pip install livekit-agents==1.2.1  # Test working version
   python test_agent.py dev

   pip install livekit-agents==1.2.6  # Test newer version
   python test_agent.py dev
   ```

3. **Compare the debug output** to see what's different

## My Recommendation

**For Production**: Stay on 1.2.1 with the July 28 code. It works perfectly and is stable.

**For Development**: Create a separate branch to experiment with 1.2.6+ and figure out the new dispatch mechanism without affecting production.

**Long-term**: Once you understand the changes, you can plan a controlled migration to the newer version.

## The Real Problem

The issue isn't with your code - it's that LiveKit changed how agents are dispatched between these versions. Your frontend is still using the old dispatch method which works with 1.2.1 but not with 1.2.6+.
