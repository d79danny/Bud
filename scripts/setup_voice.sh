#!/usr/bin/env bash
set -Eeuo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
bud_data="${BUDAI_DATA_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/budai}"
name=sherpa-onnx-whisper-tiny.en
target="$bud_data/voice/$name"
if [[ ! -x .venv/bin/python ]]; then
    echo "Run scripts/install.sh before setting up voice." >&2
    exit 1
fi
./scripts/budai init
.venv/bin/python -m pip install --disable-pip-version-check -r requirements-voice.txt
if [[ ! -d "$target" ]]; then
    mkdir -p -m 700 -- "$bud_data/voice"
    tmp=$(mktemp -d "$bud_data/voice/.download.XXXXXX")
    trap 'rm -rf -- "$tmp"' EXIT
    curl --proto '=https' --fail --show-error --silent --location         "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/$name.tar.bz2" -o "$tmp/model.tar.bz2"
    printf '%s  %s\n' '2bd6cf965c8bb3e068ef9fa2191387ee63a9dfa2a4e37582a8109641c20005dd' "$tmp/model.tar.bz2" |
        sha256sum --check --status
    tar --extract --bzip2 --file "$tmp/model.tar.bz2" --directory "$tmp" --no-same-owner --no-same-permissions
    mv -T -- "$tmp/$name" "$target"
fi
for file in tiny.en-encoder.int8.onnx tiny.en-decoder.int8.onnx tiny.en-tokens.txt; do
    if [[ ! -s "$target/$file" ]]; then
        echo "The installed model is missing $file" >&2
        exit 1
    fi
done
printf 'Configure the local English speech model before starting BudAI:\nexport BUDAI_SHERPA_MODEL=%q\n' "$target"
