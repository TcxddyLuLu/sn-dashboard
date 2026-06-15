#!/bin/bash
# ============================================================
# Nike Tools (Jamf Self Service) Batch Installer
# Usage: ./niketools-batch-install.sh
#
# Automates installing multiple apps from Nike Tools.
# Each app is searched, and the Install button is clicked.
# Already-installed apps (showing Reinstall/打开) are skipped.
# ============================================================

APPS=(
    "Box|Box (Drive)"
    "CrashPlan|CrashPlan"
    "Office 365|Microsoft Office 365"
    "Huddle|Nike Huddle Powered by Zoom"
    "Okta Verify|Okta Verify"
    "GlobalProtect|Palo Alto GlobalProtect"
    "Slack|Slack"
    "Chrome|Google Chrome"
    "Pages|Pages"
    "Numbers|Numbers"
    "Keynote|Keynote"
    "NikePrint|NikePrint"
    "Keka|Keka"
)

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
BLUE='\033[0;34m'
NC='\033[0m'

echo "========================================"
echo "  Nike Tools Batch Installer"
echo "  Apps to install: ${#APPS[@]}"
echo "========================================"
echo ""

open -a "Nike Tools"
sleep 3

# Ensure we're on the main view
osascript -e '
tell application "Nike Tools" to activate
delay 1
tell application "System Events"
    tell process "Self Service"
        -- Click search field to confirm main view is ready
        try
            set focused of text field 1 of window 1 to true
        end try
        delay 0.5
        key code 53
    end tell
end tell
' 2>/dev/null
sleep 1

installed=0
skipped=0
failed=0

for entry in "${APPS[@]}"; do
    IFS='|' read -r searchTerm appDesc <<< "$entry"
    idx=$((installed+skipped+failed+1))

    printf "${BLUE}[%2d/${#APPS[@]}]${NC} %-40s" "$idx" "$appDesc"

    result=$(osascript 2>&1 << ENDSCRIPT
tell application "Nike Tools" to activate
delay 0.5

tell application "System Events"
    tell process "Self Service"
        -- Press Escape to ensure we are on the main view
        key code 53
        delay 0.5

        set w to window 1

        -- Find and focus the search field
        set searchField to missing value
        try
            set searchField to text field 1 of w
        on error
            -- If not found, press Escape again
            key code 53
            delay 1
            try
                set searchField to text field 1 of w
            end try
        end try

        if searchField is missing value then
            return "ERROR_NO_SEARCH"
        end if

        -- Clear search field
        set focused of searchField to true
        delay 0.2
        set value of searchField to ""
        delay 1

        -- Type search term and trigger search
        set value of searchField to "$searchTerm"
        keystroke return
        delay 3

        -- Collect all buttons from the results
        set sa1 to scroll area 1 of w
        set allElems to entire contents of sa1
        set btnList to {}
        repeat with elem in allElems
            try
                if class of elem is button then
                    set end of btnList to elem
                end if
            end try
        end repeat

        -- Find matching app and its action button
        repeat with i from 1 to ((count of btnList) - 1)
            try
                set bDesc to description of (item i of btnList) as text
                if bDesc is "$appDesc" then
                    set actionBtn to item (i + 1) of btnList
                    set actionName to name of actionBtn as text

                    if actionName is "Install" or actionName is "安装" then
                        click actionBtn
                        delay 1
                        -- Press Escape to go back to main view for next search
                        key code 53
                        delay 0.5
                        return "INSTALLED"
                    else if actionName is "Reinstall" or actionName is "重新安装" or actionName is "打开" then
                        return "SKIP"
                    else
                        return "SKIP_" & actionName
                    end if
                end if
            end try
        end repeat

        return "NOT_FOUND"
    end tell
end tell
ENDSCRIPT
    )

    case "$result" in
        INSTALLED)
            printf "${GREEN}✓ Installing${NC}\n"
            ((installed++))
            sleep 2
            ;;
        SKIP)
            printf "${YELLOW}⊘ Already installed${NC}\n"
            ((skipped++))
            ;;
        SKIP_*)
            action="${result#SKIP_}"
            printf "${YELLOW}⊘ $action (skipped)${NC}\n"
            ((skipped++))
            ;;
        NOT_FOUND)
            printf "${RED}✗ Not found${NC}\n"
            ((failed++))
            ;;
        *)
            printf "${RED}✗ Error: $result${NC}\n"
            ((failed++))
            ;;
    esac
done

# Clear search field
osascript -e '
tell application "System Events"
    tell process "Self Service"
        key code 53
        delay 0.5
        try
            set value of text field 1 of window 1 to ""
        end try
    end tell
end tell
' 2>/dev/null

echo ""
echo "========================================"
printf "  ${GREEN}Installed: $installed${NC}\n"
printf "  ${YELLOW}Skipped:   $skipped${NC} (already installed)\n"
printf "  ${RED}Failed:    $failed${NC}\n"
echo "========================================"
