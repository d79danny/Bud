import json
import os
import sys
import wave
from pathlib import Path


def main():
    import sherpa_onnx

    model, audio, language = sys.argv[1:]
    model = Path(model)
    if language not in {"auto", "en"}:
        raise ValueError("The configured tiny.en speech model supports English")
    recognizer = sherpa_onnx.OfflineRecognizer.from_whisper(
        str(model / "tiny.en-encoder.int8.onnx"), str(model / "tiny.en-decoder.int8.onnx"),
        str(model / "tiny.en-tokens.txt"), language="en",
        num_threads=max(1, min(4, len(os.sched_getaffinity(0)))),
    )
    with wave.open(audio, "rb") as source:
        if source.getnchannels() != 1 or source.getsampwidth() != 2 or source.getframerate() != 16000:
            raise ValueError("Speech input must be 16 kHz, mono, signed 16-bit PCM")
        frames = memoryview(source.readframes(source.getnframes())).cast("h")
        samples = [sample / 32768 for sample in frames]
    stream = recognizer.create_stream()
    stream.accept_waveform(16000, samples)
    recognizer.decode_stream(stream)
    text = stream.result.text.strip()
    print(json.dumps({"text": text, "language": "en", "segments": 1 if text else 0}))


if __name__ == "__main__":
    main()
