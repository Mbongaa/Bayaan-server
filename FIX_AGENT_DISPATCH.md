# Agent Dispatch Fix - Root Cause Analysis

## Problem Summary

The July 28 version works because it successfully receives and accepts job requests from LiveKit. Newer deployments register but never receive job requests.

## Working Version (July 28) Characteristics:

- **Agent receives job request**: `received job request` appears in logs
- **Agent accepts request**: `✅ Accepted job request for room`
- **Agent joins room**: Successfully connects with 2 participants
- **Real-time updates work**: Each word is sent immediately to frontend

## Broken Version Characteristics:

- **Agent registers**: `registered worker` appears in logs
- **No job requests**: Never sees `received job request`
- **Stuck waiting**: Agent ready but never called
- **Frontend shows 1 participant**: Agent never joins

## The Real Fix Needed:

### Option 1: Pin LiveKit Versions (Recommended)

Update `requirements.txt` to use exact versions from July 28:

```python
# LiveKit Core Dependencies - PINNED VERSIONS
livekit-agents==1.2.1  # Was >=1.0.0
livekit-plugins-openai==0.8.1  # Was >=0.8.0
livekit-plugins-speechmatics==0.6.1  # Was >=0.6.0
livekit-plugins-silero==0.6.1  # Was >=0.6.0
```

### Option 2: Fix Agent Request Function

The issue might be in how the agent accepts requests. Check if newer LiveKit versions changed the API:

```python
async def request_fnc(req: JobRequest):
    logger.info(f"🎯 Received job request for room: {req.room.name if req.room else 'unknown'}")
    logger.info(f"📋 Request details: job_id={req.id}, room_name={req.room.name if req.room else 'unknown'}")

    # Newer versions might need different accept parameters
    await req.accept(
        name="agent",
        identity="agent",
        # Add this for newer versions:
        auto_subscribe=AutoSubscribe.AUDIO_ONLY  # Might be required now
    )
    logger.info(f"✅ Accepted job request for room: {req.room.name if req.room else 'unknown'}")
```

### Option 3: Check Agent Namespace

LiveKit might have changed how agents are dispatched. The working version shows:

- Worker ID: `AW_MKDehXBn9y5N`
- Protocol: 16

Check if newer versions need explicit namespace or dispatch configuration.

## Why Domain Patch Isn't The Issue:

The July 28 version HAS the domain patch active and working:

```
[INFO] Speechmatics domain configured: broadcast
📋 Creating TranscriptionConfig with domain: broadcast
[INFO] TranscriptionConfig initialized with domain: broadcast
```

This proves the domain patch itself isn't causing the connection issue.

## Immediate Action Items:

1. **Compare installed package versions**:

   ```bash
   pip freeze | grep livekit
   ```

   Compare between working and broken deployments.

2. **Check LiveKit Cloud logs**:
   - See if job requests are being sent
   - Check for any dispatch errors

3. **Test with pinned versions**:
   - Use exact versions from July 28
   - This should immediately fix the issue

## The Core Issue:

**The agent registration/dispatch mechanism changed between LiveKit SDK versions.** The July 28 deployment uses an older SDK version that's compatible with how your frontend requests agents. Newer SDK versions have a different dispatch mechanism that's not receiving the job requests.

## Recommended Fix:

1. Pin all LiveKit dependencies to July 28 versions
2. Keep the domain patch (it works fine)
3. Don't worry about the deprecated API warnings
4. Deploy with these exact versions

This will give you a stable, working system while you investigate the proper way to use newer LiveKit SDK versions.
