import {
  replace,   api, button, element, field, grid, muted, notice, panel, renderArtifact, row, select, stack,
  submitForm, textArea, pre, when,
} from "./common.js";


class Mic {
  constructor(media, level, transcribe, status, mode) {
    this.media = media;
    this.mode = mode;
    this.transcribe = transcribe;
    this.status = status;
    this.level = level;
    this.controller = new AbortController();
    this.context = new AudioContext();
    this.analyser = this.context.createAnalyser();
    this.analyser.fftSize = 1024;
    this.context.createMediaStreamSource(media).connect(this.analyser);
    this.samples = new Float32Array(this.analyser.fftSize);
    this.recorder = new MediaRecorder(media);
    this.parts = [];
    this.speech = false;
    this.quiet = 0;
    this.recorder.ondataavailable = event => {
      if (event.data.size) this.parts.push(event.data);
    };
    this.recorder.onstop = () => this.finished();
    this.recorder.start();
    this.started = performance.now();
    this.timer = setInterval(() => this.sample(), 100);
  }

  sample() {
    this.analyser.getFloatTimeDomainData(this.samples);
    const rms = Math.sqrt(this.samples.reduce((total, value) => total + value * value, 0) / this.samples.length);
    if (this.recorder.state !== "recording") return;
    if (rms >= this.level) {
      this.quiet = 0;
      this.speech = true;
    } else if (this.speech) {
      this.quiet += 1;
    }
    const elapsed = performance.now() - this.started;
    if ((this.mode !== "push" && (this.quiet >= 8 || (!this.speech && elapsed > 15000))) || elapsed > 58000) {
      this.recorder.stop();
    }
  }

  async finished() {
    const parts = this.parts.splice(0);
    const active = !this.controller.signal.aborted;
    const speech = this.speech;
    this.speech = false;
    this.quiet = 0;
    if (!active) return;
    const clip = new Blob(parts, {type: this.recorder.mimeType});
    if (this.mode === "push") this.release();
    try {
      if (clip.size && (this.mode === "push" || speech)) {
        this.status("Transcribing on host");
        await this.transcribe(clip, this.controller.signal);
      }
    } catch (error) {
      if (error.name !== "AbortError") notice(error.message);
      this.stop(false);
    } finally {
      if (this.mode !== "push" && !this.controller.signal.aborted && this.recorder.state === "inactive") {
        this.status("Microphone active");
        this.started = performance.now();
        this.recorder.start();
      } else if (this.mode === "push") {
        this.status("");
      }
    }
  }

  release() {
    clearInterval(this.timer);
    this.media.getTracks().forEach(track => track.stop());
    if (this.context.state !== "closed") this.context.close().catch(() => {});
  }

  stop(finalize = false) {
    if (!finalize) this.controller.abort();
    if (this.recorder.state !== "inactive") this.recorder.stop();
    this.release();
    this.status("");
  }
}

export function installSpeech(state) {
  let mic = null;
  let audio = null;
  state.speaker = "";

  state.stopVoice = () => {
    mic?.stop(false);
    mic = null;
    audio?.pause();
    audio = null;
  };

  state.playSpeech = async (text) => {
    const result = await api("/voice/speak", "POST", {text});
    audio?.pause();
    const next = new Audio("/api/artifacts/" + result.id);
    audio = next;
    if (state.speaker && next.setSinkId) await next.setSinkId(state.speaker);
    try {
      await next.play();
      await new Promise(resolve => {
        next.addEventListener("ended", resolve, {once: true});
        next.addEventListener("pause", resolve, {once: true});
        next.addEventListener("error", resolve, {once: true});
      });
    } catch (error) {
      notice("Speech file created. Open Voice history to play it: " + error.message);
    }
    return result;
  };

  state.recordVoice = async ({mode, microphone, language, onText}) => {
    if (!navigator.mediaDevices?.getUserMedia || !window.MediaRecorder) {
      throw new Error("Microphone capture requires a compatible browser on HTTPS or localhost");
    }
    state.stopVoice();
    const constraints = microphone ? {deviceId: {exact: microphone}, echoCancellation: true} : {echoCancellation: true};
    const media = await navigator.mediaDevices.getUserMedia({audio: constraints});
    const prefs = state.settings.preferences;
    const wake = prefs.wake_phrase.normalize().toLocaleLowerCase();
    try {
      mic = new Mic(media, prefs.microphone_level, async (clip, signal) => {
        const result = await api("/voice/transcribe?language=" + encodeURIComponent(language), "POST", clip, signal);
        if (!result.text) return;
        let text = result.text;
        if (mode === "wake") {
          const start = text.normalize().toLocaleLowerCase().indexOf(wake);
          if (start < 0) return;
          text = text.slice(start + prefs.wake_phrase.length).replace(/^[.,:!?\s]+/, "");
          if (!text) return;
        }
        await onText(text);
      }, value => {
        state.recording = value;
        state.markActive();
        document.dispatchEvent(new CustomEvent("bud-voice-status", {detail: value}));
      }, mode);
      state.recording = "Microphone active";
      state.markActive();
      document.dispatchEvent(new CustomEvent("bud-voice-status", {detail: state.recording}));
    } catch (error) {
      media.getTracks().forEach(track => track.stop());
      throw error;
    }
  };

  state.finishVoice = () => mic?.stop(true);
}

export async function voicePage(state, signal) {
  const profile = await api("/voice");
  const microphone = select("microphone", [["", "Default microphone"]], state.microphone);
  const speaker = select("speaker", [["", "Default speaker"]], state.speaker);
  const mode = select("mode", [["push", "Push-to-talk"], ["continuous", "Continuous conversation"], ["wake", "Wake phrase"]]);
  const language = select("language", profile.recognizer === "sherpa-onnx" ? [["en", "English"]] :
    [["auto", "Detect automatically"], ["en", "English"], ["es", "Spanish"], ["fr", "French"],
      ["de", "German"], ["it", "Italian"], ["pt", "Portuguese"]]);
  const text = textArea("transcript", {maxlength: 24000, value: state.transcript || ""});
  const status = element("span", {class: "alert", role: "status"}, state.recording || "");
  const history = stack();
  let activeMode = "push";

  async function devices(request = false) {
    if (!navigator.mediaDevices?.enumerateDevices) return;
    if (request) {
      const grant = await navigator.mediaDevices.getUserMedia({audio: true});
      grant.getTracks().forEach(track => track.stop());
    }
    const available = await navigator.mediaDevices.enumerateDevices();
    const fill = (control, type, name, value) => {
      const items = available.filter(item => item.kind === type && item.deviceId && item.deviceId !== "default");
      replace(control, element("option", {value: ""}, "Default " + name),
        items.map((item, i) => element("option", {value: item.deviceId}, item.label || name + " " + (i + 1))));
      control.value = value || "";
    };
    fill(microphone, "audioinput", "microphone", state.microphone);
    fill(speaker, "audiooutput", "speaker", state.speaker);
  }

  async function refreshHistory() {
    const [list, transcripts] = await Promise.all([api("/artifacts?kind=speech"), api("/voice/history")]);
    replace(history,
      list.slice(0, 20).map(item => stack(muted(when(item.created)), renderArtifact(item))),
      transcripts.map(item => stack(muted(when(item.created)), pre(item.content))),
      list.length || transcripts.length ? null : muted("No speech history"));
  }

  const start = button("Start microphone", async () => {
    activeMode = mode.value;
    state.microphone = microphone.value;
    await state.recordVoice({mode: activeMode, microphone: microphone.value, language: language.value,
      onText: async value => {
        text.value = state.transcript = value;
        if (activeMode !== "push") await state.chat.send(value, "voice", true);
      }});
    await devices();
  }, {disabled: !profile.model_exists || !profile.recognizer || !profile.ffmpeg});
  const finish = button("Transcribe recording", () => state.finishVoice(), {disabled: true});
  speaker.addEventListener("change", () => state.speaker = speaker.value);
  document.addEventListener("bud-voice-status", event => {
    status.textContent = event.detail;
    finish.disabled = activeMode !== "push" || event.detail !== "Microphone active";
  }, {signal});
  const send = submitForm(field("Transcript", text), "Send to Bud and speak reply", async values => {
    if (!values.transcript.value.trim()) throw new Error("Record or enter a message");
    state.transcript = values.transcript.value;
    await state.chat.send(values.transcript.value, "voice", true);
  });
  replace(state.page, stack(grid(
    panel("Microphone", stack(
      field("Input device", microphone), field("Listening mode", mode), field("Language", language),
      muted("Wake phrase: " + state.settings.preferences.wake_phrase),
      row(button("List devices", () => devices(true)), start, finish,
        button("Stop microphone and playback", () => state.stopVoice())), status,
      profile.model_exists && profile.recognizer ? null : muted("Install a local speech engine and configure its model."),
      send)),
    panel("Speaker", stack(field("Output device", speaker),
      submitForm(field("Text to speak", textArea("text", {required: "", maxlength: 6000})), "Play sample", async values => {
        await state.playSpeech(values.text.value);
        await refreshHistory();
      }),
      button("List local voices", async () => notice(await api("/voice/voices"), true)),
      profile.speaker ? null : muted("Install espeak-ng for speech output.")))),
  panel("Speech history", stack(button("Refresh", refreshHistory), history))));
  await devices();
  await refreshHistory();
}
