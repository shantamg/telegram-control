"""Transcribe a mono PCM WAV file with the server's Parakeet V3 model."""

import sys
import wave
from pathlib import Path

import numpy as np
import sherpa_onnx


def main() -> None:
    model = Path(sys.argv[1])
    recognizer = sherpa_onnx.OfflineRecognizer.from_transducer(
        encoder=str(model / "encoder.int8.onnx"),
        decoder=str(model / "decoder.int8.onnx"),
        joiner=str(model / "joiner.int8.onnx"),
        tokens=str(model / "tokens.txt"),
        num_threads=2,
        provider="cpu",
        model_type="nemo_transducer",
    )
    with wave.open(sys.argv[2]) as audio:
        samples = np.frombuffer(audio.readframes(audio.getnframes()), dtype="<i2")
        stream = recognizer.create_stream()
        stream.accept_waveform(audio.getframerate(), samples.astype(np.float32) / 32768)
    recognizer.decode_stream(stream)
    print(stream.result.text)


if __name__ == "__main__":
    main()
