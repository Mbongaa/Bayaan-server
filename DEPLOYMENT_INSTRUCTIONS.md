# Deployment Instructions - Bayaan Server

## Quick Deploy (Production-Ready)

### Step 1: Use the Correct Requirements File
```bash
# IMPORTANT: Use the pinned versions, NOT requirements.txt
pip install -r requirements-pinned.txt
```

### Step 2: Deploy Your July 28 Code
Use the code from commit `2ec438247866c62bc2c0d259767e9e5bd089de8f` or your backup:
- Keep the domain patch imports
- Keep the TranscriptionConfig wrapper
- Don't change anything - it works perfectly

### Step 3: Verify Deployment
After deployment, check logs for:
```
✅ "registered worker, id=AW_..." 
✅ "received job request"
✅ "Accepted job request for room"
✅ "Speechmatics domain configured: broadcast"
```

## What's Actually Happening

### Why Deployments Break
When you use `requirements.txt` with `>=1.0.0`, pip installs the latest versions:
- Gets LiveKit 1.2.6+ instead of 1.2.1
- New version doesn't receive job requests with current frontend
- Agent registers but never connects to rooms

### Why July 28 Works
Your July 28 deployment has:
- LiveKit 1.2.1 frozen in the container
- Compatible job dispatch mechanism
- Domain patch working correctly
- Perfect integration with your frontend

## Files to Use

### requirements-pinned.txt (USE THIS)
```python
livekit-agents==1.2.1
livekit-plugins-openai==0.8.1
livekit-plugins-speechmatics==0.6.1
livekit-plugins-silero==0.6.1
# ... rest of dependencies
```

### main.py (July 28 Version)
- Keep ALL domain patch code
- Keep TranscriptionConfig wrapper style
- Don't remove any imports
- The deprecation warnings don't matter

## Render/Railway Deployment

### Environment Variables
No changes needed - use your existing:
```
LIVEKIT_API_KEY=your_key
LIVEKIT_API_SECRET=your_secret
LIVEKIT_URL=wss://your-url
OPENAI_API_KEY=your_key
SPEECHMATICS_API_KEY=your_key
SUPABASE_URL=your_url
SUPABASE_SERVICE_ROLE_KEY=your_key
```

### Build Command
```bash
pip install -r requirements-pinned.txt
```

### Start Command
```bash
python main.py
```

## Common Mistakes to Avoid

❌ **DON'T** use `requirements.txt` with `>=` operators
❌ **DON'T** remove the domain patch - it works fine
❌ **DON'T** change to direct parameters (that was for 1.2.6+)
❌ **DON'T** update LiveKit versions "just because"

✅ **DO** use exact version pins
✅ **DO** keep your July 28 code as-is
✅ **DO** verify job requests in logs
✅ **DO** trust that 1.2.1 is stable

## Troubleshooting

### If Agent Doesn't Connect
1. Check you're using `requirements-pinned.txt`
2. Verify logs show "received job request"
3. Ensure frontend hasn't changed

### If Domain Patch Error Appears
1. You're accidentally using newer LiveKit version
2. Redeploy with `requirements-pinned.txt`

### If Transcripts Lag
1. Keep the TranscriptionConfig wrapper (July 28 style)
2. Don't use direct parameters (that's for 1.2.6+)

## Summary

Your July 28 code + LiveKit 1.2.1 = Perfect Working System

Just pin your versions and deploy. No code changes needed!