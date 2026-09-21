#!/usr/bin/env bash
# ==============================================================================
# Living Ink — One-Line Installer
# Turn handwritten reMarkable notebooks into Markdown in your Obsidian vault
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/EmilioFC99/living-ink/main/install.sh | bash
# ==============================================================================

set -euo pipefail

# Reconnect stdin to terminal if piped via curl ... | bash
if [ ! -t 0 ] && (exec < /dev/tty) 2>/dev/null; then
    exec < /dev/tty
fi

# Colors
if [ -t 1 ]; then
    BOLD="\033[1m"
    GREEN="\033[92m"
    YELLOW="\033[93m"
    CYAN="\033[96m"
    RED="\033[91m"
    DIM="\033[2m"
    RESET="\033[0m"
else
    BOLD=""
    GREEN=""
    YELLOW=""
    CYAN=""
    RED=""
    DIM=""
    RESET=""
fi

echo -e "${BOLD}${CYAN}"
echo "============================================================"
echo "              🖋️   Living Ink Installer  🖋️                "
echo "     Handwritten reMarkable notebooks → your Obsidian vault "
echo "============================================================"
echo -e "${RESET}"

# ------------------------------------------------------------------------------
# 1. System checks
# ------------------------------------------------------------------------------
OS="$(uname -s)"
if [ "$OS" != "Darwin" ] && [ "$OS" != "Linux" ]; then
    echo -e "${RED}Error: Living Ink currently supports macOS and Linux.${RESET}"
    exit 1
fi

# Check git
if ! command -v git &> /dev/null; then
    echo -e "${RED}Git is required but was not found.${RESET}"
    if [ "$OS" = "Darwin" ]; then
        echo -e "Please install Xcode Command Line Tools by running: ${BOLD}xcode-select --install${RESET}"
    else
        echo -e "Please install Git (e.g. ${BOLD}sudo apt install git${RESET} or ${BOLD}sudo dnf install git${RESET})."
    fi
    exit 1
fi

# No system libraries to check for. Rendering used to go through cairosvg,
# which needed libcairo2 on Linux; it does not any more, and a notice telling
# people to apt-install a library nothing loads is worse than no notice at all.

# ------------------------------------------------------------------------------
# 2. Check / Install uv (Fast Python package manager)
# ------------------------------------------------------------------------------
export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"

if ! command -v uv &> /dev/null; then
    echo -e "${CYAN}→ Installing uv (fast Python package manager)...${RESET}"
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
    echo -e "${GREEN}✓ uv installed successfully.${RESET}"
else
    echo -e "${GREEN}✓ uv is already installed.${RESET}"
fi

# ------------------------------------------------------------------------------
# 3. Install Living Ink CLI via uv tool
# ------------------------------------------------------------------------------
REPO_URL="https://github.com/EmilioFC99/living-ink.git"
echo -e "${CYAN}→ Installing Living Ink CLI via uv tool...${RESET}"
uv tool install --force "git+${REPO_URL}"
uv tool update-shell || true

# Ensure ~/.local/bin is in PATH for this session
export PATH="$HOME/.local/bin:$PATH"

# Check if ~/.local/bin is in PATH configuration
SHELL_NAME="$(basename "${SHELL:-bash}")"
RC_FILE=""
if [ "$SHELL_NAME" = "zsh" ]; then
    RC_FILE="$HOME/.zshrc"
elif [ "$SHELL_NAME" = "bash" ]; then
    RC_FILE="$HOME/.bashrc"
fi

if [ -n "$RC_FILE" ] && [ -f "$RC_FILE" ]; then
    if ! grep -q '\.local/bin' "$RC_FILE"; then
        echo '' >> "$RC_FILE"
        echo '# Added by Living Ink installer' >> "$RC_FILE"
        echo 'export PATH="$HOME/.local/bin:$PATH"' >> "$RC_FILE"
        echo -e "${DIM}Added ~/.local/bin to your ${RC_FILE}.${RESET}"
    fi
fi

echo -e "${GREEN}✓ Global 'living-ink' command installed successfully.${RESET}"
echo ""

# ------------------------------------------------------------------------------
# 4. Launch Setup Wizard
# ------------------------------------------------------------------------------
if [ -t 0 ]; then
    echo -e "${BOLD}${GREEN}Starting the interactive setup wizard...${RESET}"
    echo ""
    living-ink setup
else
    echo -e "${GREEN}Installation complete!${RESET}"
    echo -e "Run the setup wizard with: ${BOLD}living-ink setup${RESET}"
fi
