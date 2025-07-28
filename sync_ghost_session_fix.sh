#!/bin/bash

# Ghost Session Fix Sync Script for Bayaan Production Server
# This script syncs the ghost session fixes from server to production
# Author: SuperClaude DevOps
# Date: $(date +%Y-%m-%d)

set -euo pipefail

# Configuration
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVER_DIR="${SCRIPT_DIR}/../server"
PROD_DIR="${SCRIPT_DIR}"
BACKUP_DIR="${PROD_DIR}/backup_ghost_fix_$(date +%Y%m%d_%H%M%S)"
LOG_FILE="${PROD_DIR}/ghost_fix_sync_$(date +%Y%m%d_%H%M%S).log"

# Color codes
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m'

# Logging function
log() {
    echo -e "$1" | tee -a "$LOG_FILE"
}

# Create backup
create_backup() {
    log "${CYAN}=== Creating Backup ===${NC}"
    mkdir -p "$BACKUP_DIR"
    
    # Backup critical files
    for file in database.py main.py requirements.txt; do
        if [ -f "$PROD_DIR/$file" ]; then
            cp -p "$PROD_DIR/$file" "$BACKUP_DIR/"
            log "Backed up: $file"
        fi
    done
    
    log "${GREEN}Backup created at: $BACKUP_DIR${NC}"
}

# Main sync function
main() {
    log "${GREEN}=== Ghost Session Fix Sync Script ===${NC}"
    log "Started at: $(date)"
    log ""
    
    # Create backup first
    create_backup
    
    # Step 1: Check if database_enhanced.py exists in production
    if [ -f "$PROD_DIR/database_enhanced.py" ]; then
        log "${GREEN}✅ database_enhanced.py already in production${NC}"
    else
        log "${RED}❌ database_enhanced.py missing - please run the main fix script first${NC}"
        exit 1
    fi
    
    # Step 2: Create integration patch for database.py
    log "${CYAN}=== Creating Database Integration Patch ===${NC}"
    
    cat > "$PROD_DIR/database_integration.patch" << 'EOF'
# Add these imports at the top of database.py after existing imports:
from database_enhanced import (
    ensure_active_session_atomic as _ensure_active_session_atomic,
    SessionHealthMonitor,
    update_session_heartbeat_enhanced
)

# Initialize health monitor (add after _pool initialization)
_health_monitor = SessionHealthMonitor()

# Replace the existing ensure_active_session function with:
async def ensure_active_session(room_id: int, mosque_id: int) -> Optional[str]:
    """
    Enhanced version with atomic session creation and ghost prevention.
    """
    try:
        session = await _pool.get_session()
        async with get_db_headers() as headers:
            return await _ensure_active_session_atomic(
                room_id, mosque_id, session, headers
            )
    except Exception as e:
        logger.error(f"Failed to ensure active session: {e}")
        return None

# Add this new function for enhanced heartbeat:
async def update_session_heartbeat_with_monitor(session_id: str) -> bool:
    """
    Update heartbeat with health monitoring.
    """
    if not session_id:
        return False
        
    try:
        session = await _pool.get_session()
        async with get_db_headers() as headers:
            # Use health monitor
            healthy = await _health_monitor.monitor_heartbeat(
                session_id, session, headers
            )
            
            if not healthy:
                if _health_monitor.should_force_cleanup(session_id):
                    logger.error(f"Session {session_id} needs force cleanup")
                    return False
                else:
                    _health_monitor.increment_recovery_attempt(session_id)
                    
            return healthy
    except Exception as e:
        logger.error(f"Heartbeat monitor error: {e}")
        return False

# Export health monitor for use in main.py
def get_health_monitor():
    return _health_monitor
EOF
    
    log "${GREEN}✅ Integration patch created${NC}"
    
    # Step 3: Create main.py integration instructions
    log "${CYAN}=== Creating Main.py Integration Instructions ===${NC}"
    
    cat > "$PROD_DIR/main_integration.md" << 'EOF'
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
EOF
    
    log "${GREEN}✅ Integration instructions created${NC}"
    
    # Step 4: Check for SQL migration status
    log "${CYAN}=== Checking SQL Migration Status ===${NC}"
    
    log "${YELLOW}⚠️  IMPORTANT: Make sure you've run the SQL migration:${NC}"
    log "  ${CYAN}20250128_fix_ghost_sessions_comprehensive.sql${NC}"
    log ""
    log "The migration adds:"
    log "  - ensure_room_session_atomic() function"
    log "  - update_session_heartbeat_enhanced() function"
    log "  - cleanup_ghost_sessions() function"
    log "  - ghost_session_monitor view"
    log ""
    
    # Step 5: Create deployment checklist
    log "${CYAN}=== Creating Deployment Checklist ===${NC}"
    
    cat > "$PROD_DIR/GHOST_FIX_DEPLOYMENT.md" << 'EOF'
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
EOF
    
    log "${GREEN}✅ Deployment checklist created${NC}"
    
    # Step 6: Summary
    log ""
    log "${GREEN}=== Sync Complete ===${NC}"
    log ""
    log "${CYAN}Files Created:${NC}"
    log "  📄 database_integration.patch - Integration code for database.py"
    log "  📄 main_integration.md - Instructions for main.py updates"
    log "  📄 GHOST_FIX_DEPLOYMENT.md - Deployment checklist"
    log ""
    log "${YELLOW}Next Steps:${NC}"
    log "  1. Review the integration files"
    log "  2. Apply patches to database.py and main.py"
    log "  3. Test the changes locally"
    log "  4. Follow GHOST_FIX_DEPLOYMENT.md for deployment"
    log ""
    log "${CYAN}Backup Location:${NC} $BACKUP_DIR"
    log "${CYAN}Log File:${NC} $LOG_FILE"
    log ""
    log "Completed at: $(date)"
}

# Run main function
main "$@"