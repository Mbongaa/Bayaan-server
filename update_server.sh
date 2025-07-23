#!/bin/bash

# Bayaan Server Update Script
# Updates production server with development files

set -e  # Exit on error

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Paths
PROD_DIR="/mnt/c/Users/hassa/OneDrive/Desktop/0.2 Bayan/bayaan-server-production"
DEV_DIR="/mnt/c/Users/hassa/OneDrive/Desktop/0.2 Bayan/Dev/bayan-platform-admin-login/Backend/LiveKit-ai-translation/server"
BACKUP_DIR="$PROD_DIR/backup_$(date +%Y%m%d_%H%M%S)"

echo -e "${GREEN}=== Bayaan Server Update Script ===${NC}"
echo "Production Dir: $PROD_DIR"
echo "Development Dir: $DEV_DIR"
echo ""

# Check if DEV directory exists
if [ ! -d "$DEV_DIR" ]; then
    echo -e "${RED}Error: Development directory not found!${NC}"
    exit 1
fi

# Step 1: Create backup
echo -e "${YELLOW}Step 1: Creating backup...${NC}"
mkdir -p "$BACKUP_DIR"

# Backup files that will be updated
cp "$PROD_DIR/main.py" "$BACKUP_DIR/" 2>/dev/null || true
cp "$PROD_DIR/resource_management.py" "$BACKUP_DIR/" 2>/dev/null || true
cp "$PROD_DIR/translation_helpers.py" "$BACKUP_DIR/" 2>/dev/null || true
cp "$PROD_DIR/broadcasting.py" "$BACKUP_DIR/" 2>/dev/null || true

echo -e "${GREEN}✓ Backup created at: $BACKUP_DIR${NC}"

# Step 2: Copy development files to production
echo -e "${YELLOW}Step 2: Copying development files...${NC}"

# Copy the main files
cp "$DEV_DIR/main.py" "$PROD_DIR/"
echo "  ✓ Copied main.py"

cp "$DEV_DIR/resource_management.py" "$PROD_DIR/"
echo "  ✓ Copied resource_management.py"

cp "$DEV_DIR/translation_helpers.py" "$PROD_DIR/"
echo "  ✓ Copied translation_helpers.py"

cp "$DEV_DIR/broadcasting.py" "$PROD_DIR/"
echo "  ✓ Copied broadcasting.py"

# Check if other files need updating
echo -e "${YELLOW}Step 3: Checking other files...${NC}"

# List of other files that might need updating
OTHER_FILES=("config.py" "database.py" "text_processing.py" "translator.py" "webhook_handler.py" "prompt_builder.py")

for file in "${OTHER_FILES[@]}"; do
    if [ -f "$DEV_DIR/$file" ]; then
        # Check if files are different
        if ! cmp -s "$PROD_DIR/$file" "$DEV_DIR/$file"; then
            echo -e "  ${YELLOW}! $file differs between DEV and PROD${NC}"
            cp "$PROD_DIR/$file" "$BACKUP_DIR/" 2>/dev/null || true
            cp "$DEV_DIR/$file" "$PROD_DIR/"
            echo "    ✓ Updated $file"
        else
            echo "  - $file is identical (no update needed)"
        fi
    fi
done

# Step 4: Log the update
echo -e "${YELLOW}Step 4: Creating update log...${NC}"
cat > "$PROD_DIR/update_$(date +%Y%m%d_%H%M%S).log" << EOF
Update performed at: $(date)
Files updated:
- main.py (added heartbeat monitoring and sentence tracking)
- resource_management.py (added HeartbeatMonitor class)
- translation_helpers.py (added sentence_id parameter)
- broadcasting.py (added sentence context support)

Key improvements:
1. Heartbeat monitoring - Detects stuck participant sessions (45s timeout)
2. Sentence tracking - Unique IDs for better UI synchronization
3. Fragment handling - Improved real-time display
4. Resource management - Better cleanup and monitoring

Backup location: $BACKUP_DIR
EOF

echo -e "${GREEN}✓ Update log created${NC}"

# Step 5: Verify installation
echo -e "${YELLOW}Step 5: Verifying installation...${NC}"

# Check if key imports work
python3 -c "
import sys
sys.path.insert(0, '$PROD_DIR')
try:
    from resource_management import HeartbeatMonitor
    print('  ✓ HeartbeatMonitor class imported successfully')
except ImportError as e:
    print('  ✗ Failed to import HeartbeatMonitor:', e)
    sys.exit(1)
"

if [ $? -eq 0 ]; then
    echo -e "${GREEN}✓ Verification passed${NC}"
else
    echo -e "${RED}✗ Verification failed${NC}"
    echo -e "${YELLOW}Rolling back...${NC}"
    # Rollback
    for file in "$BACKUP_DIR"/*.py; do
        if [ -f "$file" ]; then
            filename=$(basename "$file")
            cp "$file" "$PROD_DIR/$filename"
        fi
    done
    echo -e "${GREEN}✓ Rollback completed${NC}"
    exit 1
fi

echo ""
echo -e "${GREEN}=== Update Complete ===${NC}"
echo ""
echo "Next steps:"
echo "1. Review the changes in your version control system"
echo "2. Restart the Bayaan server:"
echo "   - If using systemd: sudo systemctl restart bayaan"
echo "   - If using PM2: pm2 restart bayaan"
echo "   - If running directly: restart the Python process"
echo "3. Monitor logs for any errors"
echo "4. Test the heartbeat monitoring feature"
echo ""
echo "To rollback if needed:"
echo "  cp $BACKUP_DIR/*.py $PROD_DIR/"
echo ""