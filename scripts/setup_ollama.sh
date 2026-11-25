#!/usr/bin/env bash
set -Eeuo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
model='hf.co/OBLITERATUS/Qwen3.8-27B-OBLITERATED:Q3_K_M'
install=false
pull=false
for option in "$@"; do
    case "$option" in
        --install) install=true ;;
        --pull-model) pull=true ;;
        *) echo "Usage: $0 [--install] [--pull-model]" >&2; exit 2 ;;
    esac
done
if ! command -v ollama >/dev/null; then
    if ! "$install"; then
        echo "Ollama executable is unavailable. Use --install to run the official installer." >&2
        exit 1
    fi
    installer=$(mktemp)
    trap 'rm -f -- "$installer"' EXIT
    curl --proto '=https' --fail --show-error --silent --location https://ollama.com/install.sh -o "$installer"
    /bin/sh "$installer"
fi
if "$pull"; then
    if ! ollama show "$model" >/dev/null 2>&1; then
        ollama pull "$model"
    fi
fi
./budai model
