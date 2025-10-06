# Ghost Session Fix Deployment Checklist

## Pre-Deployment:

- [ ] Run SQL migration in Supabase
- [ ] Backup production database.py
- [ ] Review database_integration.patch
- [ ] Test in development environment

## Integration Steps:

1. [ ] Apply database.py integration patch
2. [ ] Update main.py with health monitoring
3. [ ] Verify Python syntax: `python3 -m py_compile *.py`
4. [ ] Run local tests if available

## Deployment:

1. [ ] Commit changes to git
2. [ ] Push to production branch
3. [ ] Monitor Render deployment logs
4. [ ] Check for startup errors

## Post-Deployment Verification:

1. [ ] Check application logs for:
   - "✅ Active session ensured" messages
   - "🧹 Cleaned X stale sessions" messages
   - "💓 Heartbeat updated" messages

2. [ ] Monitor database for ghost sessions:

   ```sql
   SELECT * FROM ghost_session_monitor;
   ```

3. [ ] Test session creation and cleanup:
   - Connect to a room
   - Verify session created
   - Disconnect
   - Verify session cleaned up

## Rollback Plan:

If issues occur:

1. Restore from backup: `backup_ghost_fix_*/`
2. Redeploy previous version on Render
3. Investigate logs for root cause
