#!/bin/bash

# Server_Dev to Production Update Script for Bayaan Server
# This script intelligently syncs changes from server_dev while preserving production optimizations
# Author: Bayaan DevOps Team
# Date: $(date +%Y-%m-%d)

set -euo pipefail  # Exit on error, undefined variables, pipe failures

# Configuration
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEV_DIR="${SCRIPT_DIR}/server_dev"
PROD_DIR="${SCRIPT_DIR}"
BACKUP_DIR="${PROD_DIR}/backup_$(date +%Y%m%d_%H%M%S)"
LOG_FILE="${PROD_DIR}/update_server_dev_$(date +%Y%m%d_%H%M%S).log"
MERGE_REPORT="${PROD_DIR}/merge_report_$(date +%Y%m%d_%H%M%S).md"

# Color codes for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

# Logging function
log() {
    echo -e "$1" | tee -a "$LOG_FILE"
}

# Error handling
error_exit() {
    log "${RED}ERROR: $1${NC}"
    log "${YELLOW}Rolling back changes...${NC}"
    rollback
    exit 1
}

# Initialize merge report
init_merge_report() {
    cat > "$MERGE_REPORT" << EOF
# Server_Dev → Production Update Report
**Date:** $(date)
**Source:** $DEV_DIR
**Target:** $PROD_DIR

## Update Summary

EOF
}

# Add to merge report
report() {
    echo "$1" >> "$MERGE_REPORT"
}

# Rollback function
rollback() {
    if [ -d "$BACKUP_DIR" ]; then
        log "${YELLOW}Restoring from backup: $BACKUP_DIR${NC}"
        
        # Restore all backed up files
        for file in "$BACKUP_DIR"/*; do
            if [ -f "$file" ]; then
                filename=$(basename "$file")
                cp -f "$file" "$PROD_DIR/$filename" 2>/dev/null || true
                log "Restored: $filename"
            fi
        done
        
        log "${GREEN}Rollback completed${NC}"
    else
        log "${RED}No backup found for rollback${NC}"
    fi
}

# Pre-flight checks
preflight_checks() {
    log "${CYAN}=== Starting Pre-flight Checks ===${NC}"
    
    # Check if server_dev directory exists
    if [ ! -d "$DEV_DIR" ]; then
        error_exit "Server_dev directory not found: $DEV_DIR"
    fi
    
    # Check if we're in the production directory
    if [ "$(pwd)" != "$PROD_DIR" ]; then
        log "${YELLOW}Changing to production directory${NC}"
        cd "$PROD_DIR" || error_exit "Cannot change to production directory"
    fi
    
    # Check for critical production files
    local critical_files=("main_production.py" "render.yaml" "requirements.txt" "Dockerfile")
    for file in "${critical_files[@]}"; do
        if [ ! -f "$file" ]; then
            log "${RED}WARNING: Critical production file missing: $file${NC}"
        fi
    done
    
    # Verify Python is available
    if ! command -v python3 &> /dev/null; then
        error_exit "Python3 is required but not installed"
    fi
    
    log "${GREEN}Pre-flight checks passed${NC}"
}

# Create backup
create_backup() {
    log "${CYAN}=== Creating Backup ===${NC}"
    
    # Create backup directory
    mkdir -p "$BACKUP_DIR" || error_exit "Cannot create backup directory"
    
    # Backup all Python files and critical configs
    local files_to_backup=(
        "*.py"
        ".env"
        "requirements.txt"
        "Dockerfile"
        "render.yaml"
        ".gitignore"
        "*.sh"
    )
    
    for pattern in "${files_to_backup[@]}"; do
        for file in $pattern; do
            if [ -f "$file" ]; then
                cp -p "$file" "$BACKUP_DIR/" 2>/dev/null || true
                log "Backed up: $file"
            fi
        done
    done
    
    log "${GREEN}Backup created at: $BACKUP_DIR${NC}"
    report "### Backup Location\n\`$BACKUP_DIR\`\n"
}

# Compare files and determine update strategy
compare_and_update() {
    local file=$1
    local src_file="$DEV_DIR/$file"
    local dst_file="$PROD_DIR/$file"
    
    if [ ! -f "$src_file" ]; then
        log "${YELLOW}Source file not found, skipping: $file${NC}"
        return 1
    fi
    
    # If destination doesn't exist, it's a simple copy
    if [ ! -f "$dst_file" ]; then
        cp -f "$src_file" "$dst_file"
        log "${GREEN}Added new file: $file${NC}"
        report "- **Added:** \`$file\` (new file from server_dev)"
        return 0
    fi
    
    # Check if files are different
    if ! diff -q "$src_file" "$dst_file" > /dev/null 2>&1; then
        return 0  # Files are different, needs update
    else
        return 1  # Files are identical
    fi
}

# Update simple files (direct replacement)
update_simple_files() {
    log "${CYAN}=== Updating Simple Files ===${NC}"
    report "\n### Simple File Updates\n"
    
    # Files that can be safely replaced without merging
    local simple_files=(
        "config.py"
        "prompt_builder.py"
        "text_processing.py"
        "translation_helpers.py"
        "translator.py"
        "webhook_handler.py"
    )
    
    for file in "${simple_files[@]}"; do
        if compare_and_update "$file"; then
            cp -f "$DEV_DIR/$file" "$PROD_DIR/$file"
            log "${GREEN}Updated: $file${NC}"
            report "- **Updated:** \`$file\`"
        else
            log "${BLUE}No changes needed: $file${NC}"
        fi
    done
}

# Handle complex files with merge logic
update_complex_files() {
    log "${CYAN}=== Handling Complex Files ===${NC}"
    report "\n### Complex File Handling\n"
    
    # main.py - Preserve production optimizations
    if [ -f "$DEV_DIR/main.py" ]; then
        log "${YELLOW}Analyzing main.py differences...${NC}"
        
        # Create a comparison report
        if diff -u "$PROD_DIR/main.py" "$DEV_DIR/main.py" > /tmp/main_diff.txt 2>&1; then
            log "${BLUE}main.py is identical in both versions${NC}"
        else
            log "${YELLOW}main.py has differences - preserving production optimizations${NC}"
            report "- **main.py:** Differences detected - production optimizations preserved"
            report "  - Kept production's interim transcript handling"
            report "  - Kept production's simplified cleanup approach"
            report "  - Review \`/tmp/main_diff.txt\` for detailed differences"
            
            # Don't update main.py automatically - requires manual review
            log "${YELLOW}⚠️  main.py requires manual review due to production-specific optimizations${NC}"
        fi
    fi
    
    # database.py - Check for schema changes
    if [ -f "$DEV_DIR/database.py" ]; then
        if compare_and_update "database.py"; then
            log "${YELLOW}database.py has changes - reviewing for compatibility...${NC}"
            
            # Check if new functions are added that don't exist in production
            if grep -q "close_room_session\|update_session_heartbeat" "$DEV_DIR/database.py"; then
                log "${YELLOW}New database functions detected (heartbeat/session management)${NC}"
                report "- **database.py:** New session management functions detected"
                report "  - Contains heartbeat monitoring functions not used in production"
                report "  - Manual review recommended"
            fi
            
            # For now, skip automatic update of database.py
            log "${YELLOW}⚠️  database.py update skipped - manual review required${NC}"
        fi
    fi
    
    # broadcasting.py
    if compare_and_update "broadcasting.py"; then
        cp -f "$DEV_DIR/broadcasting.py" "$PROD_DIR/broadcasting.py"
        log "${GREEN}Updated: broadcasting.py${NC}"
        report "- **Updated:** \`broadcasting.py\`"
    fi
    
    # resource_management.py
    if compare_and_update "resource_management.py"; then
        cp -f "$DEV_DIR/resource_management.py" "$PROD_DIR/resource_management.py"
        log "${GREEN}Updated: resource_management.py${NC}"
        report "- **Updated:** \`resource_management.py\`"
    fi
}

# Handle new files from server_dev
handle_new_files() {
    log "${CYAN}=== Checking for New Files ===${NC}"
    report "\n### New Files Analysis\n"
    
    # Files to explicitly exclude
    local exclude_patterns=(
        "*_cleanup*.py"
        "*_fix.py"
        "*.sql"
        "start_server.sh"
        "production_deployment.md"
        "DEPLOYMENT.md"
        "CLEANUP_SUMMARY.md"
    )
    
    # Check for new Python files
    for file in "$DEV_DIR"/*.py; do
        if [ -f "$file" ]; then
            filename=$(basename "$file")
            
            # Skip if file exists in production
            if [ -f "$PROD_DIR/$filename" ]; then
                continue
            fi
            
            # Check exclusion patterns
            local skip=false
            for pattern in "${exclude_patterns[@]}"; do
                if [[ "$filename" == $pattern ]]; then
                    skip=true
                    break
                fi
            done
            
            if [ "$skip" = true ]; then
                log "${YELLOW}Excluded new file: $filename${NC}"
                report "- **Excluded:** \`$filename\` (development/cleanup file)"
            else
                log "${CYAN}Found new file: $filename - requires review${NC}"
                report "- **New file found:** \`$filename\` - manual review required"
            fi
        fi
    done
}

# Update requirements.txt intelligently
update_requirements() {
    log "${CYAN}=== Checking requirements.txt ===${NC}"
    report "\n### Dependencies Update\n"
    
    if [ -f "$DEV_DIR/requirements.txt" ] && [ -f "$PROD_DIR/requirements.txt" ]; then
        # Create sorted unique lists
        sort "$DEV_DIR/requirements.txt" | grep -v "^#" | grep -v "^$" > /tmp/dev_reqs.txt
        sort "$PROD_DIR/requirements.txt" | grep -v "^#" | grep -v "^$" > /tmp/prod_reqs.txt
        
        # Find new requirements in dev
        comm -23 /tmp/dev_reqs.txt /tmp/prod_reqs.txt > /tmp/new_reqs.txt
        
        if [ -s /tmp/new_reqs.txt ]; then
            log "${YELLOW}New dependencies found in server_dev:${NC}"
            cat /tmp/new_reqs.txt | while read -r req; do
                log "  + $req"
                report "- New dependency: \`$req\`"
            done
            report "\n⚠️  **Action Required:** Review and add new dependencies to production requirements.txt"
        else
            log "${GREEN}No new dependencies found${NC}"
            report "- No new dependencies detected"
        fi
        
        # Cleanup temp files
        rm -f /tmp/dev_reqs.txt /tmp/prod_reqs.txt /tmp/new_reqs.txt
    fi
}

# Verify Python syntax
verify_python_syntax() {
    log "${CYAN}=== Verifying Python Syntax ===${NC}"
    
    local all_good=true
    
    for file in *.py; do
        if [ -f "$file" ]; then
            if python3 -m py_compile "$file" 2>/dev/null; then
                log "${GREEN}✓ Syntax OK: $file${NC}"
            else
                log "${RED}✗ Syntax error in: $file${NC}"
                all_good=false
            fi
        fi
    done
    
    # Clean up __pycache__
    rm -rf __pycache__ 2>/dev/null || true
    
    if [ "$all_good" = false ]; then
        error_exit "Python syntax verification failed"
    fi
    
    log "${GREEN}All Python files passed syntax check${NC}"
}

# Generate final recommendations
generate_recommendations() {
    log "${CYAN}=== Generating Recommendations ===${NC}"
    
    report "\n## Post-Update Recommendations\n"
    report "### Manual Review Required:"
    report "1. **main.py** - Review differences between dev and production versions"
    report "2. **database.py** - Check if new session management functions are needed"
    report "3. **New files** - Evaluate any new files from server_dev for inclusion"
    report "4. **Dependencies** - Review and update requirements.txt if needed"
    report ""
    report "### Testing Checklist:"
    report "- [ ] Run local tests with updated code"
    report "- [ ] Verify WebSocket connections work correctly"
    report "- [ ] Test transcript handling (both final and interim)"
    report "- [ ] Confirm database operations function properly"
    report "- [ ] Check resource cleanup on disconnection"
    report ""
    report "### Deployment Steps:"
    report "1. Review this report and the update log"
    report "2. Manually review complex files if needed"
    report "3. Run \`git status\` to see all changes"
    report "4. Test locally if possible"
    report "5. Commit changes with descriptive message"
    report "6. Deploy to Render following standard procedure"
    report ""
    report "### Rollback Instructions:"
    report "If issues occur, run: \`bash $0 --rollback\`"
    report ""
    report "**Backup Location:** \`$BACKUP_DIR\`"
    report "**Log File:** \`$LOG_FILE\`"
}

# Post-update summary
post_update_summary() {
    log ""
    log "${GREEN}=== Update Completed Successfully ===${NC}"
    log ""
    log "${CYAN}Important Files:${NC}"
    log "  📄 Merge Report: ${YELLOW}$MERGE_REPORT${NC}"
    log "  📋 Log File: ${YELLOW}$LOG_FILE${NC}"
    log "  💾 Backup: ${YELLOW}$BACKUP_DIR${NC}"
    log ""
    log "${YELLOW}Next Steps:${NC}"
    log "  1. Review the merge report for detailed changes"
    log "  2. Manually review files marked for attention"
    log "  3. Run tests before deploying"
    log ""
    log "To rollback if needed: ${CYAN}bash $0 --rollback${NC}"
}

# Rollback functionality
if [ "${1:-}" == "--rollback" ]; then
    # Find the most recent backup
    LATEST_BACKUP=$(ls -td ${PROD_DIR}/backup_* 2>/dev/null | head -1)
    if [ -n "$LATEST_BACKUP" ]; then
        BACKUP_DIR="$LATEST_BACKUP"
        log "${YELLOW}Rolling back to: $BACKUP_DIR${NC}"
        rollback
        exit 0
    else
        log "${RED}No backup found for rollback${NC}"
        exit 1
    fi
fi

# Main execution
main() {
    log "${GREEN}=== Bayaan Server_Dev → Production Update Script ===${NC}"
    log "Started at: $(date)"
    log "Source: $DEV_DIR"
    log "Target: $PROD_DIR"
    log ""
    
    # Initialize merge report
    init_merge_report
    
    # Execute update steps
    preflight_checks
    create_backup
    update_simple_files
    update_complex_files
    handle_new_files
    update_requirements
    verify_python_syntax
    generate_recommendations
    post_update_summary
    
    log "Completed at: $(date)"
}

# Run main function
main "$@"