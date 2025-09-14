#!/usr/bin/env bash
set -euo pipefail

# Adds this repo's bin/ to your PATH in ~/.bashrc (idempotent),
# then reloads your shell config.

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")"/.. && pwd)"
BIN_DIR="$REPO_DIR/bin"
BASHRC="$HOME/.bashrc"
MARK_START="# >>> x-cli PATH (auto-added) >>>"
MARK_END="# <<< x-cli PATH (auto-added) <<<"

mkdir -p "$HOME/.local/bin" >/dev/null 2>&1 || true

append_block() {
  {
    echo "$MARK_START"
    echo "# Add x-cli bin to PATH"
    echo "export PATH=\"$BIN_DIR:\$PATH\""
    echo "$MARK_END"
  } >> "$BASHRC"
}

# Remove any previous block to keep it clean (optional but nice)
if grep -q "$MARK_START" "$BASHRC" 2>/dev/null; then
  tmpfile="$(mktemp)"
  awk -v s="$MARK_START" -v e="$MARK_END" '
    $0 ~ s {skip=1; next} $0 ~ e {skip=0; next} !skip {print}
  ' "$BASHRC" > "$tmpfile"
  mv "$tmpfile" "$BASHRC"
fi

append_block

echo "[x-cli] Appended PATH export for: $BIN_DIR"
echo "[x-cli] Reloading ~/.bashrc ..."
source "$BASHRC"

echo "[x-cli] Done. Try: x --help"

