# Wake-word models

| File | Size | What it is |
|---|---|---|
| `hey_saint.onnx` | 214 KB | SAINT's custom wake-word classifier ("Hey SAINT"), trained with openWakeWord on synthetic Piper TTS speech. Input `x` `[1, 16, 96]` (16 speech embeddings) → output `sigmoid` `[1, 1]`. |
| `melspectrogram.onnx` | 1.1 MB | openWakeWord's shared mel-spectrogram front end (Apache-2.0, from [dscripka/openWakeWord](https://github.com/dscripka/openWakeWord)). |
| `embedding_model.onnx` | 1.3 MB | openWakeWord's shared speech-embedding model (Apache-2.0, same source; derived from Google's `speech_embedding`). |

SAINT runs all three with **onnxruntime on the CPU** (`modules/voice/wake_word.py`). It does not
import the `openwakeword` Python package at runtime; that package imports scikit-learn, whose DLLs can be
blocked by Windows Application Control.

`hey_saint.onnx` is a single self-contained file: the weights that the PyTorch exporter originally
wrote to a separate `hey_saint.onnx.data` file were merged into it.

## Using a different model

Put any openWakeWord-style classifier `.onnx` here (or anywhere) and set
**Settings → Wake Word → Model file** (config key `voice.wake_word_model_path`). Relative paths are
resolved from the SAINT folder. If the file is missing or invalid, SAINT shows the exact error on the
dashboard and falls back to transcript-gated listening.

## Retraining

The training config used for `hey_saint.onnx` is in `tools/wake_word/hey_saint.yaml`. See the README
section *Wake word → Retraining*.

SHA-256:

```
6e893f893ababf7d26d6e27c0c67da63b5861822611e107be7c811ed9773c01f  hey_saint.onnx
ba2b0e0f8b7b875369a2c89cb13360ff53bac436f2895cced9f479fa65eb176f  melspectrogram.onnx
70d164290c1d095d1d4ee149bc5e00543250a7316b59f31d056cff7bd3075c1f  embedding_model.onnx
```
