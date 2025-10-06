# Backend Connection Troubleshooting Guide

## Current Status ✅

The backend server is **running correctly** and registered with LiveKit:

- Worker ID: `AW_mKa3Fyz7iQQE`
- LiveKit URL: `wss://jamaa-app-4bix2j1v.livekit.cloud`
- Region: Germany
- All plugins loaded successfully (Speechmatics, OpenAI, Silero)

## The Issue

The server is waiting for room connections but none are being created. This is a **client-side issue**, not a server problem.

## What Should Happen

1. Frontend creates a LiveKit room via Supabase edge function
2. LiveKit sends a webhook to request an agent
3. Agent accepts the request and joins the room
4. Translation begins

## Check These Things

### 1. Frontend Room Creation

Verify the frontend is calling the Supabase edge function to create LiveKit rooms:

- Check browser console for errors when clicking "Go Live"
- Look for network requests to `/create-livekit-room`

### 2. LiveKit Webhook Configuration

In your LiveKit Cloud dashboard:

- Go to Settings → Webhooks
- Ensure webhook URL points to your agent's deployment
- The URL should be: `https://your-render-service.onrender.com/webhook` (if using webhook)
- OR ensure "Agents" are enabled for automatic dispatch

### 3. Agent Request Pattern

The agent is configured to accept ALL room requests:

```python
async def request_fnc(req: JobRequest):
    await req.accept(
        name="agent",
        identity="agent",
    )
```

### 4. Test Room Creation

Try creating a test room directly:

1. Use LiveKit playground or CLI
2. Create a room with any name
3. Check if agent receives the request

### 5. Check Render Logs

Look for these log messages:

- ✅ `"registered worker"` - Agent connected to LiveKit
- ⏳ Waiting for: `"🎯 Received job request for room"` - Room request received
- ⏳ Waiting for: `"✅ Accepted job request"` - Agent accepted room

## Quick Test

Use LiveKit CLI to create a test room:

```bash
livekit-cli create-room --api-key YOUR_KEY --api-secret YOUR_SECRET --url wss://jamaa-app-4bix2j1v.livekit.cloud test-room
```

Then check Render logs to see if the agent receives the request.

## Frontend Fix Checklist

- [ ] Verify VITE_LIVEKIT_URL is set correctly in frontend
- [ ] Check Supabase edge function `create-livekit-room` is deployed
- [ ] Ensure LiveKit API keys match between frontend and backend
- [ ] Verify room creation API call succeeds (check Network tab)
- [ ] Check browser console for WebSocket connection errors

## Summary

**The backend is working correctly!** The issue is that no rooms are being created for it to join. Focus on:

1. Frontend room creation process
2. LiveKit webhook/agent configuration
3. API key consistency between services
