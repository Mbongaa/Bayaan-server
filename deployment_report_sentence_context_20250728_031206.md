# Sentence Context Update Deployment Report

**Date:** Mon Jul 28 03:12:06 CEST 2025
**Feature:** Sentence context tracking for improved session replay quality
**Source:** /mnt/c/Users/hassa/OneDrive/Desktop/0.2 Bayan/Dev/bayan-platform-admin-login/Backend/LiveKit-ai-translation/server
**Target:** /mnt/c/Users/hassa/OneDrive/Desktop/0.2 Bayan/Dev/bayan-platform-admin-login/Backend/LiveKit-ai-translation/bayaan-server-production

## Deployment Summary

### Changes Being Deployed:

1. **database.py** - Added sentence_context parameter to store_transcript_in_database
2. **broadcasting.py** - Updated to pass sentence_context through to storage

### Database Changes Required:

- Migration: 20250128_add_sentence_context_to_transcripts.sql
- Adds: sentence_id, is_complete, is_fragment columns to transcripts table

### Backup Location\n`/mnt/c/Users/hassa/OneDrive/Desktop/0.2 Bayan/Dev/bayan-platform-admin-login/Backend/LiveKit-ai-translation/bayaan-server-production/backup_sentence_context_20250728_031206`\n

\n### File Changes Analysis\n

#### database.py Changes:

```diff
     message_type: str,
     language: str,
     text: str,
-    tenant_context: Dict[str, Any]
+    tenant_context: Dict[str, Any],
+    sentence_context: Optional[Dict[str, Any]] = None
 ) -> bool:
     """
     Store transcription/translation in Supabase database.
@@ -162,6 +163,7 @@
         language: Language code (e.g., "ar", "nl")
         text: The text to store
         tenant_context: Context containing room_id, mosque_id, session_id
+        sentence_context: Optional context containing sentence_id, is_complete, is_fragment

     Returns:
         bool: True if successful, False otherwise
@@ -195,6 +197,12 @@
             "timestamp": datetime.utcnow().isoformat() + "Z",
         }

+        # Add sentence context if provided
+        if sentence_context:
+            transcript_data["sentence_id"] = sentence_context.get("sentence_id")
+            transcript_data["is_complete"] = sentence_context.get("is_complete", False)
+            transcript_data["is_fragment"] = sentence_context.get("is_fragment", True)
+
         # Set appropriate field based on message type
         if message_type == "transcription":
             transcript_data["transcription_segment"] = text
@@ -417,6 +425,98 @@
```

#### broadcasting.py Changes:

```diff
@@ -107,7 +107,7 @@
             # Store directly in database using existing function
             # Use create_task to avoid blocking the broadcast with proper error handling
             task = asyncio.create_task(
-                _store_with_error_handling(message_type, language, text, tenant_context)
+                _store_with_error_handling(message_type, language, text, tenant_context, sentence_context)
             )
             task.add_done_callback(lambda t: None if not t.exception() else logger.error(f"Storage task failed: {t.exception()}"))
             logger.debug(
@@ -126,7 +126,8 @@
     message_type: str,
     language: str,
     text: str,
-    tenant_context: Dict[str, Any]
+    tenant_context: Dict[str, Any],
+    sentence_context: Optional[Dict[str, Any]] = None
 ) -> None:
     """
     Store transcript with proper error handling.
@@ -139,10 +140,11 @@
         language: Language code
         text: The text content to store
         tenant_context: Context containing room_id, mosque_id, etc.
+        sentence_context: Optional context containing sentence_id, is_complete, is_fragment
     """
     try:
         success = await store_transcript_in_database(
-            message_type, language, text, tenant_context
+            message_type, language, text, tenant_context, sentence_context
         )
         if not success:
             logger.warning(
```

\n### Deployment Actions\n

- ✅ Updated database.py with sentence_context support
- ✅ Updated broadcasting.py to pass sentence_context to storage
  \n## Post-Deployment Steps\n

### 1. Database Migration

Apply the following migration to your Supabase database:

```sql
-- Add sentence context columns to transcripts table
ALTER TABLE transcripts
ADD COLUMN IF NOT EXISTS sentence_id UUID;

ALTER TABLE transcripts
ADD COLUMN IF NOT EXISTS is_complete BOOLEAN DEFAULT false;

ALTER TABLE transcripts
ADD COLUMN IF NOT EXISTS is_fragment BOOLEAN DEFAULT true;

-- Add indexes for efficient querying
CREATE INDEX IF NOT EXISTS idx_transcripts_sentence_id
ON transcripts(sentence_id)
WHERE sentence_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_transcripts_session_sentence
ON transcripts(session_id, sentence_id)
WHERE sentence_id IS NOT NULL;
```

\n### 2. Frontend Deployment
The frontend changes are already in the Dev folder. Deploy these files:

- `src/integrations/supabase/types.ts` - Updated TypeScript types
- `src/components/SessionReplay.tsx` - Enhanced sentence processing
  \n### 3. Server Restart
  After deploying:

1. Commit and push the changes to your production repository
2. Render will automatically redeploy the service
3. Monitor logs for any errors during startup
   \n### 4. Verification
   After deployment:

- [ ] Start a new live monitoring session
- [ ] Check database for new columns in transcript entries
- [ ] Verify session replay shows same quality as live monitoring
      \n### 5. Rollback Instructions
      If issues occur:

1. Run: `bash deploy-sentence-context-update.sh --rollback`
2. Or manually restore from: `/mnt/c/Users/hassa/OneDrive/Desktop/0.2 Bayan/Dev/bayan-platform-admin-login/Backend/LiveKit-ai-translation/bayaan-server-production/backup_sentence_context_20250728_031206`
