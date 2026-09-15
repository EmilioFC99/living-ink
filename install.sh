#!/usr/bin/env bash
# ==============================================================================
# Living Ink — One-Line Installer
# Sync your reMarkable notebooks to Obsidian & Apple Notes
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/EmilioFC99/living-ink/main/install.sh | bash
# ==============================================================================

set -euo pipefail

# Reconnect stdin to terminal if piped via curl ... | bash
if [ ! -t 0 ] && [ -e /dev/tty ]; then
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
echo "   Sync your reMarkable notebooks to Obsidian & Apple Notes "
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
# 3. Clone or update repository
# ------------------------------------------------------------------------------
INSTALL_DIR="$HOME/.living-ink"
REPO_URL="https://github.com/EmilioFC99/living-ink.git"

if [ -d "$INSTALL_DIR/.git" ]; then
    echo -e "${CYAN}→ Updating existing installation in ${INSTALL_DIR}...${RESET}"
    git -C "$INSTALL_DIR" pull --ff-only || true
else
    echo -e "${CYAN}→ Downloading Living Ink to ${INSTALL_DIR}...${RESET}"
    git clone "$REPO_URL" "$INSTALL_DIR"
fi

# ------------------------------------------------------------------------------
# 4. Install dependencies
# ------------------------------------------------------------------------------
echo -e "${CYAN}→ Installing Python dependencies...${RESET}"
cd "$INSTALL_DIR"
uv sync --all-extras

# ------------------------------------------------------------------------------
# 5. Install global 'living-ink' CLI command
# ------------------------------------------------------------------------------
BIN_DIR="$HOME/.local/bin"
mkdir -p "$BIN_DIR"
WRAPPER="$BIN_DIR/living-ink"

cat <<'EOF' > "$WRAPPER"
#!/usr/bin/env bash
export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
INSTALL_DIR="$HOME/.living-ink"
exec uv run --directory "$INSTALL_DIR" living-ink "$@"
EOF

chmod +x "$WRAPPER"

# Check if ~/.local/bin is in PATH
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

echo -e "${GREEN}✓ Global 'living-ink' command installed to ${WRAPPER}.${RESET}"
echo ""

# ------------------------------------------------------------------------------
# 6. Launch Setup Wizard
# ------------------------------------------------------------------------------
if [ -t 0 ]; then
    echo -e "${BOLD}${GREEN}Starting the interactive setup wizard...${RESET}"
    echo ""
    uv run --directory "$INSTALL_DIR" living-ink setup
else
    echo -e "${GREEN}Installation complete!${RESET}"
    echo -e "Run the setup wizard with: ${BOLD}living-ink setup${RESET}"
fi
