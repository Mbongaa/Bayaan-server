#!/bin/bash

# Dev Server to Production Server Update Script
# This script syncs changes from the development server to production
# while preserving production-specific optimizations
# 
# Author: Bayaan DevOps Team
# Date: $(date +%Y-%m-%d)
# Last Updated: 2025-01-28

set -euo pipefail  # Exit on error, undefined variables, pipe failures

# Configuration
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEV_DIR="${SCRIPT_DIR}/../server"
PROD_DIR="${SCRIPT_DIR}"
BACKUP_DIR="${PROD_DIR}/backup_$(date +%Y%m%d_%H%M%S)"
LOG_FILE="${PROD_DIR}/sync_dev_production_$(date +%Y%m%d_%H%M%S).log"
REPORT_FILE="${PROD_DIR}/sync_report_$(date +%Y%m%d_%H%M%S).md"

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

# Initialize sync report
init_report() {
    cat > "$REPORT_FILE" << EOF
# Dev Server → Production Server Sync Report
**Date:** $(date)
**Source:** $DEV_DIR
**Target:** $PROD_DIR

## Summary of Changes

EOF
}

# Add to report
report() {
    echo "$1" >> "$REPORT_FILE"
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
    
    # Check if dev server directory exists
    if [ ! -d "$DEV_DIR" ]; then
        error_exit "Dev server directory not found: $DEV_DIR"
    fi
    
    # Check if we're in the production directory
    if [ ! -d "$PROD_DIR" ]; then
        error_exit "Production directory not found: $PROD_DIR"
    fi
    
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
    report "### Backup Location"
    report "\`$BACKUP_DIR\`"
    report ""
}

# Update core files that have been modified
update_core_files() {
    log "${CYAN}=== Updating Core Files ===${NC}"
    report "### Updated Files"
    report ""
    
    # Files that need to be updated (modified in both)
    local core_files=(
        "broadcasting.py"
        "config.py"
        "database.py"
        "database_enhanced.py"
        "main.py"
        "prompt_builder.py"
        "resource_management.py"
        "text_processing.py"
        "translation_helpers.py"
        "translator.py"
        "webhook_handler.py"
    )
    
    for file in "${core_files[@]}"; do
        if [ -f "$DEV_DIR/$file" ]; then
            if [ -f "$PROD_DIR/$file" ]; then
                # Check if files are different
                if ! diff -q "$DEV_DIR/$file" "$PROD_DIR/$file" > /dev/null 2>&1; then
                    cp -f "$DEV_DIR/$file" "$PROD_DIR/$file"
                    log "${GREEN}Updated: $file${NC}"
                    report "- **Updated:** \`$file\`"
                else
                    log "${BLUE}No changes: $file${NC}"
                fi
            else
                # File doesn't exist in production, copy it
                cp -f "$DEV_DIR/$file" "$PROD_DIR/$file"
                log "${GREEN}Added: $file${NC}"
                report "- **Added:** \`$file\` (was missing in production)"
            fi
        fi
    done
}

# Add new files from dev server
add_new_files() {
    log "${CYAN}=== Adding New Files from Dev Server ===${NC}"
    report ""
    report "### New Files Added"
    report ""
    
    # New files to add (exist in dev but not in production)
    local new_files=(
        "speechmatics_advanced.py"
        "speechmatics_domain_patch.py"
    )
    
    # Test/utility files to optionally add
    local optional_files=(
        "check_stt_params.py"
        "database_cleanup_fix.py"
        "simple_domain_test.py"
        "test_domain_patch.py"
        "test_domain_support.py"
        "test_room_domain.py"
        "verify_domain_config.py"
    )
    
    # Add essential new files
    for file in "${new_files[@]}"; do
        if [ -f "$DEV_DIR/$file" ] && [ ! -f "$PROD_DIR/$file" ]; then
            cp -f "$DEV_DIR/$file" "$PROD_DIR/$file"
            log "${GREEN}Added new file: $file${NC}"
            report "- **Added:** \`$file\` (Speechmatics domain support)"
        fi
    done
    
    # Report optional files (but don't copy automatically)
    report ""
    report "### Optional Files (Not Copied)"
    report "These files exist in dev but are test/utility files:"
    report ""
    
    for file in "${optional_files[@]}"; do
        if [ -f "$DEV_DIR/$file" ] && [ ! -f "$PROD_DIR/$file" ]; then
            log "${YELLOW}Optional file available: $file${NC}"
            report "- \`$file\` - $(get_file_purpose "$file")"
        fi
    done
}

# Get file purpose for documentation
get_file_purpose() {
    local file=$1
    case $file in
        "check_stt_params.py") echo "STT parameter verification utility" ;;
        "database_cleanup_fix.py") echo "Database cleanup script" ;;
        "simple_domain_test.py") echo "Domain testing utility" ;;
        "test_domain_patch.py") echo "Domain patch testing" ;;
        "test_domain_support.py") echo "Domain support testing" ;;
        "test_room_domain.py") echo "Room domain configuration test" ;;
        "verify_domain_config.py") echo "Domain configuration verification" ;;
        *) echo "Utility/test file" ;;
    esac
}

# Handle production-specific files
handle_production_files() {
    log "${CYAN}=== Checking Production-Specific Files ===${NC}"
    report ""
    report "### Production-Specific Files"
    report ""
    
    # main_production.py exists only in production
    if [ -f "$PROD_DIR/main_production.py" ]; then
        log "${YELLOW}Note: main_production.py is production-specific and was not modified${NC}"
        report "- \`main_production.py\` - Production entry point (preserved)"
    fi
    
    # Check if render.yaml or other deployment configs need updates
    if [ -f "$PROD_DIR/render.yaml" ]; then
        report "- \`render.yaml\` - Deployment configuration (preserved)"
    fi
}

# Update requirements.txt if needed
check_requirements() {
    log "${CYAN}=== Checking Requirements ===${NC}"
    report ""
    report "### Dependencies"
    report ""
    
    if [ -f "$DEV_DIR/requirements.txt" ] && [ -f "$PROD_DIR/requirements.txt" ]; then
        # Create sorted unique lists
        sort "$DEV_DIR/requirements.txt" | grep -v "^#" | grep -v "^$" > /tmp/dev_reqs_sorted.txt
        sort "$PROD_DIR/requirements.txt" | grep -v "^#" | grep -v "^$" > /tmp/prod_reqs_sorted.txt
        
        # Find differences
        if ! diff -q /tmp/dev_reqs_sorted.txt /tmp/prod_reqs_sorted.txt > /dev/null 2>&1; then
            log "${YELLOW}Requirements differ between dev and production${NC}"
            report "**Note:** requirements.txt files differ. Manual review recommended."
            
            # Show new requirements in dev
            comm -23 /tmp/dev_reqs_sorted.txt /tmp/prod_reqs_sorted.txt > /tmp/new_reqs.txt
            if [ -s /tmp/new_reqs.txt ]; then
                report ""
                report "New dependencies in dev:"
                while IFS= read -r req; do
                    report "- \`$req\`"
                done < /tmp/new_reqs.txt
            fi
        else
            log "${GREEN}Requirements are in sync${NC}"
            report "Requirements files are identical."
        fi
        
        # Cleanup
        rm -f /tmp/dev_reqs_sorted.txt /tmp/prod_reqs_sorted.txt /tmp/new_reqs.txt
    fi
}

# Verify Python syntax
verify_syntax() {
    log "${CYAN}=== Verifying Python Syntax ===${NC}"
    
    local all_good=true
    
    for file in "$PROD_DIR"/*.py; do
        if [ -f "$file" ]; then
            filename=$(basename "$file")
            if python3 -m py_compile "$file" 2>/dev/null; then
                log "${GREEN}✓ Syntax OK: $filename${NC}"
            else
                log "${RED}✗ Syntax error in: $filename${NC}"
                all_good=false
            fi
        fi
    done
    
    # Clean up __pycache__
    rm -rf "$PROD_DIR/__pycache__" 2>/dev/null || true
    
    if [ "$all_good" = false ]; then
        error_exit "Python syntax verification failed"
    fi
    
    log "${GREEN}All Python files passed syntax check${NC}"
}

# Generate final recommendations
generate_recommendations() {
    log "${CYAN}=== Generating Recommendations ===${NC}"
    
    report ""
    report "## Post-Sync Recommendations"
    report ""
    report "### Important Notes"
    report ""
    report "1. **STT Stream Fix**: The recent STT stream reconnection fix has been applied to both environments"
    report "2. **Domain Support**: New Speechmatics domain support files have been added"
    report "3. **Resource Management**: Enhanced resource cleanup and debouncing implemented"
    report ""
    report "### Testing Checklist"
    report ""
    report "- [ ] Test STT stream reconnection scenarios"
    report "- [ ] Verify duplicate transcription prevention"
    report "- [ ] Test participant disconnect/reconnect within 3 seconds"
    report "- [ ] Verify resource cleanup on disconnect"
    report "- [ ] Test Speechmatics domain configuration (when enabled)"
    report ""
    report "### Deployment Steps"
    report ""
    report "1. Review this report and the sync log"
    report "2. Run local tests if possible"
    report "3. Commit changes: \`git add . && git commit -m \"Sync dev changes: STT fixes and domain support\"\`"
    report "4. Deploy to Render: \`git push\`"
    report "5. Monitor logs after deployment"
    report ""
    report "### Rollback Instructions"
    report ""
    report "If issues occur, run: \`bash $0 --rollback\`"
    report ""
    report "**Backup Location:** \`$BACKUP_DIR\`"
    report "**Log File:** \`$LOG_FILE\`"
}

# Main execution
main() {
    log "${GREEN}=== Dev Server → Production Server Sync Script ===${NC}"
    log "Started at: $(date)"
    log ""
    
    # Initialize report
    init_report
    
    # Execute sync steps
    preflight_checks
    create_backup
    update_core_files
    add_new_files
    handle_production_files
    check_requirements
    verify_syntax
    generate_recommendations
    
    # Final summary
    log ""
    log "${GREEN}=== Sync Completed Successfully ===${NC}"
    log ""
    log "${CYAN}Important Files:${NC}"
    log "  📄 Sync Report: ${YELLOW}$REPORT_FILE${NC}"
    log "  📋 Log File: ${YELLOW}$LOG_FILE${NC}"
    log "  💾 Backup: ${YELLOW}$BACKUP_DIR${NC}"
    log ""
    log "Review the sync report for detailed changes and recommendations."
    
    # Show the report
    echo ""
    echo "Opening sync report..."
    cat "$REPORT_FILE"
}

# Handle rollback option
if [ "${1:-}" == "--rollback" ]; then
    # Find the most recent backup
    LATEST_BACKUP=$(ls -td ${PROD_DIR}/backup_* 2>/dev/null | head -1)
    if [ -n "$LATEST_BACKUP" ]; then
        BACKUP_DIR="$LATEST_BACKUP"
        LOG_FILE="${PROD_DIR}/rollback_$(date +%Y%m%d_%H%M%S).log"
        log "${YELLOW}Rolling back to: $BACKUP_DIR${NC}"
        rollback
        log "${GREEN}Rollback completed successfully${NC}"
        exit 0
    else
        echo "${RED}No backup found for rollback${NC}"
        exit 1
    fi
fi

# Run main function
main "$@"