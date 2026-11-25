#!/usr/bin/env bash
set -Eeuo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."

core_only=false
development=false
system_deps=false
launchers=false
install_ollama=false
pull_model=false
voice=false
comfy_checkout=""
comfy_cpu=false
for option in "$@"; do
    case "$option" in
        --core-only) core_only=true ;;
        --development) development=true ;;
        --system) system_deps=true ;;
        --launchers) launchers=true ;;
        --install-ollama) install_ollama=true ;;
        --pull-model) pull_model=true ;;
        --setup-voice) voice=true ;;
        --comfyui=/*) comfy_checkout=${option#--comfyui=} ;;
        --comfy-cpu) comfy_cpu=true ;;
        *) echo "Usage: $0 [--core-only] [--development] [--system] [--launchers] [--install-ollama] [--pull-model] [--setup-voice] [--comfyui=/absolute/path] [--comfy-cpu]" >&2; exit 2 ;;
    esac
done

os_id=$(sed -n 's/^ID=//p' /etc/os-release | tr -d '"')
if [[ "$os_id" != ubuntu ]] && ! "$development"; then
    echo "BudAI targets Ubuntu. Use --development to install dependencies on another Linux distribution." >&2
    exit 1
fi
state_directory="${XDG_STATE_HOME:-$HOME/.local/state}/budai"
mkdir -p -m 700 -- "$state_directory"
exec > >(tee -a "$state_directory/install.log") 2>&1
trap 'printf "Installation failed at line %s. Log: %s\n" "$LINENO" "$state_directory/install.log" >&2' ERR

if "$system_deps"; then
    if [[ "$os_id" != ubuntu ]]; then
        echo "System package installation is specified for Ubuntu." >&2
        exit 1
    fi
    sudo apt-get update
    sudo apt-get install -y python3 python3-venv git curl ffmpeg espeak-ng imagemagick          tesseract-ocr xdotool wmctrl x11-xserver-utils xclip wl-clipboard at-spi2-core          xdg-desktop-portal pulseaudio-utils pciutils libxcb-cursor0 libnss3 libxkbcommon-x11-0
fi
python3 -c 'import sys; sys.exit("BudAI requires Python 3.11 or newer") if sys.version_info < (3, 11) else None'
df -h -- .
if [[ ! -x .venv/bin/python ]]; then
    python3 -m venv .venv
fi
requirements=(-r requirements.txt)
if ! "$core_only"; then
    requirements+=(-r requirements-browser.txt -r requirements-desktop.txt)
fi
.venv/bin/python -m pip install --disable-pip-version-check "${requirements[@]}"
./scripts/budai init
.venv/bin/python -m unittest discover -s tests
./scripts/detect_hardware.sh

if "$launchers"; then
    ./scripts/install_launchers.sh
fi
if "$voice"; then
    ./scripts/setup_voice.sh
fi
if [[ -n "$comfy_checkout" ]]; then
    comfy_flags=()
    if "$comfy_cpu"; then comfy_flags+=(--cpu); fi
    ./scripts/setup_comfyui.sh "$comfy_checkout" "${comfy_flags[@]}"
fi
ollama_flags=()
if "$install_ollama"; then ollama_flags+=(--install); fi
if "$pull_model"; then ollama_flags+=(--pull-model); fi
if (( ${#ollama_flags[@]} > 0 )); then
    ./scripts/setup_ollama.sh "${ollama_flags[@]}"
elif command -v ollama >/dev/null; then
    if ! ./scripts/budai model; then
        echo "Install finished. The model check above requires attention before inference." >&2
    fi
else
    echo "Install finished. Ollama was not detected. See README step 5."
fi
printf 'Local prerequisites\n'
for executable in whisper-cli ffmpeg espeak-ng chromium xdotool wmctrl; do
    if command -v "$executable"; then continue; fi
    printf '  Unavailable: %s\n' "$executable"
done
