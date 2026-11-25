#!/usr/bin/env bash
set -Eeuo pipefail
if [[ $# -lt 1 || $# -gt 2 || (${2:-} != "" && ${2:-} != --cpu) ]]; then
    echo "Usage: $0 /absolute/path/to/ComfyUI [--cpu]" >&2
    exit 2
fi
checkout=$1
release=v0.3.76
expected=30c259cac8c08ff8d015f9aff3151cb525c9b702
if [[ "$checkout" != /* ]]; then
    echo "Provide an absolute path for the separate ComfyUI checkout." >&2
    exit 2
fi
mkdir -p -- "$(dirname -- "$checkout")"
if [[ ! -e "$checkout" ]]; then
    git clone --depth 1 --branch "$release" https://github.com/Comfy-Org/ComfyUI.git "$checkout"
fi
if [[ "$(git -C "$checkout" rev-parse HEAD)" != "$expected" ]]; then
    echo "The existing checkout does not match ComfyUI $release. Select a new location or manage that checkout manually." >&2
    exit 1
fi
if [[ ! -x "$checkout/.venv/bin/python" ]]; then
    python3 -m venv "$checkout/.venv"
fi
arg=""
if [[ ${2:-} == --cpu ]]; then
    "$checkout/.venv/bin/python" -m pip install --disable-pip-version-check \
        --index-url https://download.pytorch.org/whl/cpu torch==2.8.0 torchvision==0.23.0 torchaudio==2.8.0
    arg=" --cpu"
fi
"$checkout/.venv/bin/python" -m pip install --disable-pip-version-check 'transformers==4.57.6' -r "$checkout/requirements.txt"
printf 'Start ComfyUI on this computer with:\n  cd %q && .venv/bin/python main.py --listen 127.0.0.1 --port 8188%s\n' "$checkout" "$arg"
