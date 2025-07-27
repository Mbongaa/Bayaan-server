#!/bin/bash

# Production Update Script for Bayaan Server
# This script safely updates production files from development while preserving production-specific configurations
# Author: Senior DevOps Engineer
# Date: $(date +%Y-%m-%d)

set -euo pipefail  # Exit on error, undefined variables, pipe failures

# Configuration
DEV_DIR="/mnt/c/Users/hassa/OneDrive/Desktop/0.2 Bayan/bayan-platform-admin-login/Backend/LiveKit-ai-translation/server"
PROD_DIR="/mnt/c/Users/hassa/OneDrive/Desktop/0.2 Bayan/bayaan-server-production"
BACKUP_DIR="${PROD_DIR}/backup_$(date +%Y%m%d_%H%M%S)"
LOG_FILE="${PROD_DIR}/update_$(date +%Y%m%d_%H%M%S).log"

# Color codes for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
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

# Rollback function
rollback() {
    if [ -d "$BACKUP_DIR" ]; then
        log "${YELLOW}Restoring from backup: $BACKUP_DIR${NC}"
        
        # Restore Python files
        for file in "$BACKUP_DIR"/*.py; do
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
    log "${YELLOW}=== Starting Pre-flight Checks ===${NC}"
    
    # Check if dev directory exists
    if [ ! -d "$DEV_DIR" ]; then
        error_exit "Development directory not found: $DEV_DIR"
    fi
    
    # Check if we're in the production directory
    if [ "$(pwd)" != "$PROD_DIR" ]; then
        log "${YELLOW}Changing to production directory${NC}"
        cd "$PROD_DIR" || error_exit "Cannot change to production directory"
    fi
    
    # Check for critical production files
    local critical_files=("main_production.py" "render.yaml" "requirements.txt" "Dockerfile" ".env")
    for file in "${critical_files[@]}"; do
        if [ ! -f "$file" ]; then
            log "${RED}WARNING: Critical production file missing: $file${NC}"
        fi
    done
    
    log "${GREEN}Pre-flight checks passed${NC}"
}

# Create backup
create_backup() {
    log "${YELLOW}=== Creating Backup ===${NC}"
    
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
}

# Update files from development
update_files() {
    log "${YELLOW}=== Updating Files from Development ===${NC}"
    
    # Core Python modules to update (excluding dev-specific files)
    local python_files=(
        "main.py"
        "prompt_builder.py"
        "broadcasting.py"
        "config.py"
        "database.py"
        "resource_management.py"
        "text_processing.py"
        "translation_helpers.py"
        "translator.py"
        "webhook_handler.py"
    )
    
    # Files to explicitly exclude
    local exclude_patterns=(
        "*_backup.py"
        "*_fixed.py"
        "*_cleanup*.py"
        "production_deployment.md"
    )
    
    # Update each Python file
    for file in "${python_files[@]}"; do
        local src_file="$DEV_DIR/$file"
        
        if [ -f "$src_file" ]; then
            # Check if file exists in exclude patterns
            local skip=false
            for pattern in "${exclude_patterns[@]}"; do
                if [[ "$file" == $pattern ]]; then
                    skip=true
                    break
                fi
            done
            
            if [ "$skip" = false ]; then
                cp -f "$src_file" "$PROD_DIR/$file" || error_exit "Failed to copy $file"
                log "${GREEN}Updated: $file${NC}"
            else
                log "${YELLOW}Skipped (excluded): $file${NC}"
            fi
        else
            log "${YELLOW}Not found in dev (skipping): $file${NC}"
        fi
    done
    
    # Handle .env.example if it exists and production doesn't have it
    if [ -f "$DEV_DIR/.env.example" ] && [ ! -f "$PROD_DIR/.env.example" ]; then
        cp -f "$DEV_DIR/.env.example" "$PROD_DIR/.env.example"
        log "${GREEN}Added: .env.example${NC}"
    fi
    
    # Update .gitignore if needed
    if [ -f "$DEV_DIR/.gitignore" ]; then
        cp -f "$DEV_DIR/.gitignore" "$PROD_DIR/.gitignore"
        log "${GREEN}Updated: .gitignore${NC}"
    fi
}

# Verify production integrity
verify_production() {
    log "${YELLOW}=== Verifying Production Integrity ===${NC}"
    
    # Check that production-specific files are still present
    local prod_files=("main_production.py" "render.yaml" "requirements.txt" "Dockerfile" ".env")
    local all_good=true
    
    for file in "${prod_files[@]}"; do
        if [ -f "$file" ]; then
            log "${GREEN}✓ Production file intact: $file${NC}"
        else
            log "${RED}✗ Production file missing: $file${NC}"
            all_good=false
        fi
    done
    
    # Check Python syntax for all .py files
    log "${YELLOW}Checking Python syntax...${NC}"
    for file in *.py; do
        if [ -f "$file" ]; then
            if python3 -m py_compile "$file" 2>/dev/null; then
                log "${GREEN}✓ Syntax OK: $file${NC}"
                rm -f "__pycache__/${file%.py}.cpython-*.pyc" 2>/dev/null
            else
                log "${RED}✗ Syntax error in: $file${NC}"
                all_good=false
            fi
        fi
    done
    
    # Clean up __pycache__
    rmdir __pycache__ 2>/dev/null || true
    
    if [ "$all_good" = false ]; then
        error_exit "Production integrity check failed"
    fi
    
    log "${GREEN}Production integrity verified${NC}"
}

# Post-update recommendations
post_update_recommendations() {
    log "${YELLOW}=== Post-Update Recommendations ===${NC}"
    log ""
    log "1. ${YELLOW}Test locally:${NC} Test the updated code in a staging environment if available"
    log "2. ${YELLOW}Review logs:${NC} Check $LOG_FILE for any warnings"
    log "3. ${YELLOW}Git status:${NC} Run 'git status' to review changes before committing"
    log "4. ${YELLOW}Deploy:${NC} Follow your standard Render deployment process"
    log ""
    log "${GREEN}Update completed successfully!${NC}"
    log ""
    log "Backup location: $BACKUP_DIR"
    log "To rollback if needed, run: ${YELLOW}bash $0 --rollback${NC}"
}

# Standalone rollback option
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
    log "${GREEN}=== Bayaan Production Update Script ===${NC}"
    log "Started at: $(date)"
    log "Dev source: $DEV_DIR"
    log "Production: $PROD_DIR"
    log ""
    
    # Execute update steps
    preflight_checks
    create_backup
    update_files
    verify_production
    post_update_recommendations
    
    log "Completed at: $(date)"
}

# Run main function
main "$@"