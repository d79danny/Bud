# BudAI

BudAI is a local Ubuntu application. Bud uses the exact configured Ollama model to interpret requests, choose tools, inspect their results, and answer. Python runs the tools under the Linux account that starts BudAI. The Qt desktop window and installable mobile web app use the same authenticated backend.

## Step 1. System requirements

Ubuntu 24.04 LTS on x86-64 is the installation target. Use Python 3.11 or newer, SQLite with FTS5, and a Linux user account. BudAI itself runs on CPU. Allow about 2 GiB for the database, Python environment, and desktop dependencies at installation, plus space for browser files, attachments, and generated media. Ollama and ComfyUI have their own storage and memory requirements.

For local inference, size RAM and VRAM to the actual model files, context window, and desired GPU offloading. The requested 27B quantization's file size and runtime requirements remain unverified. A supported GPU is recommended for diffusion image and video workloads. ComfyUI workflow requirements vary with the model, resolution, duration, batching, and supported data types. Plan for an Internet connection during initial package and model setup. Installed local tools work without external inference APIs.

## Step 2. Hardware detection

From the Ubuntu terminal, run the available system tools.

```bash
lscpu
free -h
lsblk
df -h
lspci
uname -a
printf '%s\n' "${XDG_SESSION_TYPE:-unknown}"
```

After installing BudAI, run `./scripts/detect_hardware.sh` from its checkout. It saves `hardware.json` under the BudAI data directory. Hardware → Detect hardware reruns the same collection. The Hardware tab reports measured CPU load, RAM, swap, storage, GPU sensors when available, network throughput, and Ollama inference timings when supplied. Missing sensors appear as unavailable.

## Step 3. Ubuntu preparation

Run these commands in the Ubuntu terminal, then continue in the checkout.

```bash
sudo apt-get update
sudo apt-get install -y python3 python3-venv git curl ffmpeg espeak-ng imagemagick \
  tesseract-ocr xdotool wmctrl x11-xserver-utils xclip wl-clipboard at-spi2-core \
  xdg-desktop-portal pulseaudio-utils pciutils libxcb-cursor0 libnss3 libxkbcommon-x11-0
```

The installer can perform the same package installation with `--system`. Install a supported Chromium browser through Ubuntu or use Playwright's packaged Chromium in Step 7.

## Step 4. GPU configuration

On an NVIDIA host, run `nvidia-smi` and check the device and driver. Ubuntu's supported driver recommendations are listed by `ubuntu-drivers devices`. Use Ubuntu's driver installation tools for the selected supported driver and reboot if required. Start with Ollama's GPU detection and the [PyTorch installation selector](https://pytorch.org/get-started/locally/) for ComfyUI. A separate system CUDA toolkit is optional when the selected PyTorch and Ollama distributions already supply runtime libraries.

After loading the model, run `ollama ps` and check Settings → Check model for actual placement when reported. In Settings you can lower the context or set GPU layers to reduce VRAM use. An empty GPU-layer field delegates placement to Ollama.

## Step 5. Ollama installation

Follow [Ollama's official Linux instructions](https://docs.ollama.com/linux). You can fetch and run its official installer from a terminal.

```bash
installer=$(mktemp)
curl --proto '=https' --fail --show-error --silent --location https://ollama.com/install.sh -o "$installer"
sh "$installer"
rm -- "$installer"
systemctl status ollama
curl --fail --show-error http://127.0.0.1:11434/api/tags
```

If you run Ollama without its system service, leave `ollama serve` running in a separate terminal. Keep the listener on loopback. The BudAI installer can run the official installer with `--install-ollama`.

## Step 6. Language model installation

Run the exact pull command and check the installed quantization.

```bash
ollama pull hf.co/OBLITERATUS/Qwen3.8-27B-OBLITERATED:Q3_K_M
ollama show hf.co/OBLITERATUS/Qwen3.8-27B-OBLITERATED:Q3_K_M
ollama run hf.co/OBLITERATUS/Qwen3.8-27B-OBLITERATED:Q3_K_M 'Reply with a short introduction.'
ollama ps
```

BudAI uses that identifier for native calls and structured JSON fallback. The Ollama API detects installed model capabilities. Settings exposes the server's model metadata and load, unload, context, keep-alive, and inference controls.

The cloud verification machine had no Ollama listener. Its network proxy returned HTTP 403 for the exact Hugging Face repository metadata and the Ollama distribution domain. That leaves repository availability, quantization compatibility, and live inference **unverified**. If `ollama pull` fails on Ubuntu, its original error is the source of truth. BudAI keeps the exact identifier and records errors returned by its local Ollama API.

## Step 7. BudAI installation

Open an Ubuntu terminal in your Bud checkout. For a new checkout, the repository is [d79danny/Bud](https://github.com/d79danny/Bud).

```bash
cd ~/Bud
./scripts/install.sh --launchers
./scripts/budai add-user bud
./scripts/start.sh
```

Use `./scripts/budai serve` for the API-only process, then open [BudAI on this computer](http://127.0.0.1:8765) in your browser. `--core-only` installs the API and responsive interface without the optional Qt and Playwright packages. The installer verifies the Linux distribution, checks storage, initializes the private data directory, records hardware, and runs the automated tests. It accepts `--pull-model`, `--setup-voice`, and `--comfyui=/absolute/path` for the optional service setup steps. Non-Ubuntu Linux development machines can use `--development`.

For a manual installation, run these from the checkout.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt -r requirements-desktop.txt -r requirements-browser.txt
python -m budai init
python -m budai hardware
python -m budai add-user bud
python -m budai desktop
```

For Playwright's managed Chromium, run `.venv/bin/python -m playwright install chromium`. Ubuntu dependencies can be installed through `.venv/bin/python -m playwright install-deps chromium` if needed. BudAI automatically finds system Chromium or Chrome, or falls back to the Playwright browser. Set `BUDAI_CHROMIUM` to a particular installed executable if needed. The controlled browser starts a dedicated profile with Chromium's sandbox enabled.

Copy the desired environment variables from [.env.example](.env.example) into the launching shell or the systemd environment file in Step 13. The defaults keep BudAI, Ollama, and ComfyUI on loopback. The default data directory is `${XDG_DATA_HOME:-$HOME/.local/share}/budai` with mode `0700`. Read the install log at `${XDG_STATE_HOME:-$HOME/.local/state}/budai/install.log`.

## Step 8. ComfyUI installation

ComfyUI runs in an independent checkout and Python environment. The included setup script pins the upstream ComfyUI release used by the API verification. For an NVIDIA or AMD host with a supported PyTorch build, configure the PyTorch environment using the official selector before installing ComfyUI requirements. For the verified CPU setup, use the following commands from Bud.

```bash
./scripts/setup_comfyui.sh "$HOME/ComfyUI" --cpu
cd "$HOME/ComfyUI"
.venv/bin/python main.py --cpu --listen 127.0.0.1 --port 8188 --disable-all-custom-nodes
```

On a host with a GPU and compatible PyTorch packages, omit both `--cpu` flags. Check the service in BudAI at Studio → Check ComfyUI, or request `http://127.0.0.1:8188/system_stats` from the host.

Install weights and workflows using a recipe from the official ComfyUI examples. For an image workflow, start with the [official SDXL example](https://comfyanonymous.github.io/ComfyUI_examples/sdxl/) and put the selected checkpoint in `ComfyUI/models/checkpoints/`. For Wan 2.1 video, the [upstream workflow and model links](https://comfyanonymous.github.io/ComfyUI_examples/wan/) put the VAE under `models/vae/`, the text encoder under `models/text_encoders/`, and the selected diffusion weights under `models/diffusion_models/`. Image-to-video may also need `models/clip_vision/`. These references come from upstream. Their weight downloads and GPU requirements were not verified in the cloud. Review custom nodes before installing them. BudAI supports any installed compatible workflow exposed through its descriptor, including workflows with a reference image, inpainting, upscaling, or video input parameters supported by the installed graph.

In the ComfyUI editor, run the workflow with actual inputs and inspect the generated files. Export it in **API format**. Put the graph and a reviewed descriptor under `$BUDAI_DATA_DIR/workflows/`, using the default data directory from Step 7 when no environment override is set. For example, if the exported graph is `portrait.json`, save `portrait.workflow.json` alongside it. Replace the node ID and input names with the exact entries in the exported graph.

```json
{
  "title": "Portrait workflow",
  "kind": "image",
  "graph": "portrait.json",
  "inputs": {
    "prompt": {"label": "Prompt", "node": "6", "name": "text", "type": "string"},
    "seed": {"label": "Seed", "node": "3", "name": "seed", "type": "integer"}
  }
}
```

Supported field types are `string`, `integer`, `number`, `boolean`, `image`, and `video`. Image and video fields upload reference media to the selected ComfyUI input. Leave a field blank to use the exported graph's value. Use `kind: "video"` with a graph that saves an MP4 or WebM. Studio → Verify workflow checks node and model options against the running server. Queue the workflow and inspect the real progress and outputs in Studio. ComfyUI versions that return an error from `SaveVideo` for an explicit container may work with the node's `auto` container value. Preserve the verified exported graph.

BudAI serializes Ollama inference and ComfyUI generation. The GPU sharing setting unloads the idle Ollama model before ComfyUI jobs. BudAI verifies local media headers before making saved files available to the authenticated client. The integration tests executed stock image and video graphs against real CPU ComfyUI. Diffusion generation requires your selected models and hardware.

## Step 9. Voice assistant installation

The Ubuntu packages in Step 3 provide FFmpeg and eSpeak NG. Install the local English recognizer from the Bud checkout.

```bash
./scripts/setup_voice.sh
export BUDAI_SHERPA_MODEL="${BUDAI_DATA_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/budai}/voice/sherpa-onnx-whisper-tiny.en"
```

The script fetches the official Sherpa-ONNX release asset, verifies its SHA-256, and configures the English tiny Whisper model. Alternatively, install `whisper-cli` from [whisper.cpp](https://github.com/ggml-org/whisper.cpp), download the intended speech model through its model tools, and set `BUDAI_WHISPER_MODEL` to the local model file. Restart BudAI after changing environment variables.

In Voice, list available microphones and speakers, allow the client to use the chosen microphone, and try a push-to-talk recording. Play a sample to verify local synthesis. Continuous mode and the optional `Hey Bud` wake phrase are active only after you start microphone capture in the client. BudAI transcribes the segmented recording on the host. Replies for a voice conversation come from Ollama, then eSpeak NG turns the answer into audio. Use Stop or Emergency stop to end listening and playback.

## Step 10. Computer control configuration

Run `printf '%s\n' "$XDG_SESSION_TYPE"` in the Ubuntu desktop terminal. Launch BudAI from the desktop session so it inherits the display and D-Bus environment. X11 supports live capture, screenshots, OCR, pointer and keyboard input, window operations, and clipboard tools. Accessibility inspection uses the session's AT-SPI service when available. The X11 integration was exercised with a disposable display, an actual window manager, and a Qt text input.

On a Wayland session, screenshots use the interactive XDG portal. The compositor asks for consent and lets you choose what to share. For X11 automation on Ubuntu, select **Ubuntu on Xorg** on the sign-in screen when your installed Ubuntu session offers it. Wayland pointer and keyboard injection and continuous screencasting are currently unavailable. Clipboard operations can use `wl-clipboard` when the compositor allows them.

To verify safely, open an empty text editor, take a window screenshot from Computer and check its OCR output, move the pointer to a chosen coordinate, and type a test line into the editor. Run `printf 'BudAI terminal test\\n'` from the Computer terminal and check its PTY output. Create a test text file in a temporary directory and verify it from the file explorer. File changes and shell commands requested by Bud require approval. Terminal execution runs as the host Linux user. Authorize administration through the operating system and review exact commands before running them.

## Step 11. First launch

Start Ollama, ComfyUI if using Studio, and BudAI. Create your account with `./scripts/budai add-user bud` and sign in. Check the exact local model in Settings. In Chat, start a conversation and ask Bud to report the CPU. To test the full tool loop, authorize a test directory in **Tool access for this message**, ask Bud to read the GPU name and write it to an explicit new file path, and review the file action before approving it. Inspect the saved contents and tool output.

Save and search an entry in Memory. Configure an installed local embedding model with `BUDAI_EMBEDDING_MODEL`, then index recent records in Memory to add semantic retrieval. Browser supports direct HTTP document reads and a dedicated Chromium session for visible or background pages, tabs, form interaction, downloads, and source inspection. Open a known page and verify the returned source. Use the Studio and Voice checks in Steps 8 and 9. Queue a Hardware report in Tasks and inspect the completed readings and execution history. Tasks can also schedule reviewed ComfyUI graphs with explicit inputs and detect changes to the graph before execution.

Bud handles each model-directed action as an individually validated request. Tool results and failures are recorded. Scheduled assistant requests can perform authorized read-only work and must cite verified tool evidence before completion. Work requiring live approval can be performed interactively. Optional specialists use the same model sequentially. Settings supports read-only workflow proposals, tests against actual results, version activation, and rollback. Python source proposals can be reviewed and syntax-checked. Deploying application changes requires external code review and isolated tests.

## Step 12. Mobile configuration

Find the host's addresses with `ip -brief address`. Use an HTTPS origin reachable over your Wi-Fi network or private VPN. Keep BudAI itself on loopback and proxy only its authenticated API and interface. Configure private DNS for the Ubuntu host's address and install a trusted certificate on the mobile device. [configuration/Caddyfile.example](configuration/Caddyfile.example) shows a Caddy reverse proxy using a private hostname and Caddy's internal certificate authority. Trust that authority on the device if you use it.

Set `BUDAI_ORIGIN=https://budai.home.arpa` in the BudAI service environment, using your actual origin. Restart the service. From a signed-in BudAI client, open Settings → Mobile access → Create pairing QR. Scan it with the phone, name the device, and confirm pairing. The code is single-use and expires after two minutes. Revoke devices from the same panel. The Android Chrome menu offers **Install app** where supported. In iOS Safari use **Share → Add to Home Screen**. The app shell can reopen offline and shows host connection failures. Logs, model calls, files, and computer controls require the authenticated connection. Notifications are available from the open Tasks view on supported clients.

For use outside the LAN, connect the device to your private VPN and use the same trusted HTTPS origin. The configured host must be reachable over the VPN. Ollama and ComfyUI stay on the Ubuntu machine. Verify account ownership and revocation before relying on remote control.

## Step 13. Automatic startup

Run `./scripts/install_launchers.sh` from the checkout to install a BudAI desktop entry and user service. To set environment variables for the user service, create `${XDG_CONFIG_HOME:-$HOME/.config}/budai/environment` with the assignments you need from `.env.example`.

```bash
systemctl --user daemon-reload
systemctl --user start budai.service
systemctl --user status budai.service
systemctl --user enable budai.service
journalctl --user -u budai.service -f
```

Use `systemctl --user stop budai.service` to stop it and `systemctl --user disable --now budai.service` to disable startup. The desktop launcher starts the API if a compatible local one is absent. If the user service does not have the graphical environment, import display environment values from the desktop terminal and restart it.

```bash
systemctl --user import-environment DISPLAY XAUTHORITY XDG_SESSION_TYPE
systemctl --user restart budai.service
```

On a Wayland session, import `WAYLAND_DISPLAY` and `XDG_SESSION_TYPE` when present. Systemd startup and the graphical session are distinct. If a portal or X11 authorization check fails, launch the desktop application directly from the intended session.

## Step 14. Updating

Stop BudAI and back up its data. From a clean checkout run `./scripts/update.sh` or pull fast-forward changes and rerun `./scripts/install.sh`. The script preserves your application data and reruns the tests. Update Ollama using its official Linux installer and run the exact model pull from Step 6 if you want to refresh it. Update ComfyUI in its own environment and revalidate the workflows after changing its version or weights.

## Step 15. Troubleshooting

| Symptom | Check |
| --- | --- |
| Ollama refuses a connection | Verify `systemctl status ollama` or `ollama serve`, then call `/api/tags` on the configured local port. |
| Pull fails or model is missing | Use the exact command from Step 6. Check the repository and quantization from your own network. Save the underlying Ollama error. |
| VRAM, CUDA, or slow inference | Check `nvidia-smi`, `ollama ps`, RAM, swap, CPU affinity, and the selected PyTorch build. Reduce output or context size and try partial CPU offload. |
| X11 or Wayland control fails | Check the session type, inherited display variables, and the documented session permissions. See Step 10. |
| Controlled browser fails to launch | Verify the configured executable and Chromium sandbox support under the Ubuntu user. Check the Error tab for Chromium's startup log. |
| ComfyUI fails to connect or validate | Check `/system_stats`, `/object_info`, graph export format, installed nodes, and checkpoint names. |
| Microphone or speech fails | Confirm HTTPS or localhost and client microphone permission. Check `ffmpeg`, the configured recognizer and model path, and `espeak-ng --voices`. |
| Mobile cannot connect | Verify private DNS or VPN reachability, trusted HTTPS, the proxy's forwarded host and scheme, and the configured origin. |
| Port is in use | Choose a local port with `./scripts/budai serve --port PORT`. If using Qt or Caddy, give the desktop command or proxy the same port. |
| Database or dependencies fail | Stop BudAI. Check space, directory permissions, the install log and `python -m pip check`. Restore a verified database snapshot if integrity checks fail. |
| API fails at startup | Start it from the terminal, inspect its underlying exception and the user service journal, then rerun the failing check. |

The Errors tab shows actual component errors and available traceback details. It supports filtering, copying, exporting, retrying service checks, and sending the selected record to Bud for diagnosis. The app's rotating text log is `logs/budai.log` in the data directory.

## Step 16. Backup and restoration

Stop BudAI and run these commands from the Bud checkout. Pick new destination directories.

```bash
./scripts/backup.sh "$HOME/budai-backup"
./scripts/restore.sh "$HOME/budai-backup" "$HOME/budai-restored"
export BUDAI_DATA_DIR="$HOME/budai-restored"
./scripts/budai serve
```

Backup copies the SQLite database, attachments, generated media, workflows, software settings, and authentication records, then runs an integrity check. Restore invalidates old sessions and pairing links. Browser profiles, desktop web caches, external models, and the systemd environment file live separately. Back up the environment file and external workflows independently if you need them. Keep the snapshot private.

## Step 17. Uninstallation

Close the desktop window, stop the user service if enabled, and run `./scripts/uninstall.sh --remove-launchers` from the checkout. It removes the entries generated for that checkout. You can remove the checkout and its virtual environment separately. Local data and external services stay on disk. To remove user data permanently, verify the data-directory path and its backup before manually deleting that directory.

## Verification

Run `.venv/bin/python -m unittest discover -s tests -v`. Regular tests cover authentication, secure pairing, conversation and memory persistence, structured model messages, actual tool results, approval and rejection, the PTY, file operations, backup and restore, agents, tasks, and emergency stop. The Ollama protocol tests use isolated fixtures and actual host tools.

Set `BUDAI_TEST_OLLAMA=1` on a GPU host with the exact installed model to run the GPU-name file example through live inference. Set `BUDAI_TEST_X11=1` only on a disposable desktop with a window manager, because the test sends real pointer and keyboard input. Set `BUDAI_TEST_COMFYUI=1` with a running compatible local ComfyUI instance to execute stock image and video workflows and verify the output files. Set `BUDAI_TEST_WHISPER=1` and `BUDAI_TEST_WAV=/absolute/path.wav` with a configured speech engine to run the local recognition check.

In the cloud, the main browser UI was exercised at desktop and mobile widths. Host integrations verified the Qt window, actual X11 capture, OCR, pointer and keyboard events, speech synthesis and recognition, ComfyUI PNG and MP4 production, and secure pairing through a real TLS reverse proxy. Exact-model inference, GPU diffusion, Wayland portal behavior, the sandboxed Chromium research session, and physical Android and iOS pairing remain unverified in that environment.
