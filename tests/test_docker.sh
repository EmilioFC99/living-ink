#!/usr/bin/env bash
# ==============================================================================
# Living Ink — Docker Integration & Smoke Test Script
# Tests the full container lifecycle and sync pipeline from a clean slate
# ==============================================================================

set -euo pipefail

BOLD="\033[1m"
GREEN="\033[92m"
CYAN="\033[96m"
RED="\033[91m"
RESET="\033[0m"

echo -e "${BOLD}${CYAN}=== Living Ink Docker Test Suite ===${RESET}"

# 1. Check Docker daemon
if ! docker info >/dev/null 2>&1; then
    echo -e "${RED}Error: Docker daemon is not running. Start Docker Desktop and try again.${RESET}"
    exit 1
fi

# 2. Build production Docker image
echo -e "\n${CYAN}1. Building Docker image (living-ink:test)...${RESET}"
docker build -t living-ink:test .
echo -e "${GREEN}✓ Image built successfully.${RESET}"

# 3. Test CLI help
echo -e "\n${CYAN}2. Testing CLI entrypoint in container...${RESET}"
docker run --rm living-ink:test --help >/dev/null
echo -e "${GREEN}✓ CLI entrypoint is working.${RESET}"

# 4. Test unconfigured status (clean handling)
echo -e "\n${CYAN}3. Testing unconfigured status...${RESET}"
STATUS_OUTPUT=$(docker run --rm living-ink:test status)
if echo "$STATUS_OUTPUT" | grep -q "Configuration: Not found"; then
    echo -e "${GREEN}✓ Status cleanly reports 'Configuration: Not found' in fresh state.${RESET}"
else
    echo -e "${RED}❌ Unexpected status output:${RESET}\n$STATUS_OUTPUT"
    exit 1
fi

# 5. Test with mounted configuration and test vault
TMP_VAULT=$(mktemp -d /tmp/living-ink-vault.XXXXXX)
TMP_CONFIG=$(mktemp -d /tmp/living-ink-config.XXXXXX)
TMP_DATA=$(mktemp -d /tmp/living-ink-data.XXXXXX)

trap 'rm -rf "$TMP_VAULT" "$TMP_CONFIG" "$TMP_DATA"' EXIT

HOST_CONFIG="$HOME/.config/living-ink/config.yml"
[ -f "config/config.yml" ] && HOST_CONFIG="config/config.yml"

if [ -f "$HOST_CONFIG" ]; then
    echo -e "\n${CYAN}4. Testing live sync with host config ($HOST_CONFIG) against temporary vault...${RESET}"
    sed 's|vault_path: .*|vault_path: /vault|' "$HOST_CONFIG" > "$TMP_CONFIG/config.yml"

    docker run --rm \
        -v "$TMP_CONFIG":/app/config:ro \
        -v "$TMP_VAULT":/vault \
        -v "$TMP_DATA":/app/data \
        living-ink:test sync --limit 1

    MD_COUNT=$(find "$TMP_VAULT" -name "*.md" | wc -l | tr -d ' ')
    if [ "$MD_COUNT" -gt 0 ]; then
        echo -e "${GREEN}✓ Successfully synced $MD_COUNT notebook(s) to Obsidian vault in Docker.${RESET}"
    else
        echo -e "${RED}❌ No Markdown notes found in test vault.${RESET}"
        exit 1
    fi
else
    echo -e "\n${CYAN}4. Skipping live sync test (no config found at $HOST_CONFIG).${RESET}"
fi

echo -e "\n${BOLD}${GREEN}============================================================${RESET}"
echo -e "${BOLD}${GREEN}  ✓ All Docker integration tests passed successfully!     ${RESET}"
echo -e "${BOLD}${GREEN}============================================================${RESET}"
