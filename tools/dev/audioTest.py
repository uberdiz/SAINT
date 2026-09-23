import os
import sys
import time
import torch
import torchaudio

# ============================================================
# COSYVOICE 300M — CUDA ZERO-SHOT VOICE CLONING TEST
# ============================================================

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

MODEL_DIR = os.path.join(
    BASE_DIR,
    "pretrained_models",
    "CosyVoice-300M"
)

REFERENCE_AUDIO = os.path.join(
    BASE_DIR,
    "reference.wav"
)

REFERENCE_TEXT = (
    "This is the exact transcript of the reference audio. "
    "Replace this with exactly what is spoken in reference.wav."
)

TEST_TEXT = (
    "Hello. I am SAINT, your local artificial intelligence assistant. "
    "This is a test of my cloned voice running locally on your computer."
)

print("=" * 70)
print("SAINT — COSYVOICE 300M CUDA TTS")
print("=" * 70)

# ------------------------------------------------------------
# GPU CHECK
# ------------------------------------------------------------

if not torch.cuda.is_available():
    raise RuntimeError(
        "CUDA is not available. Install a CUDA-enabled PyTorch build."
    )

device = torch.device("cuda")

print(f"GPU:       {torch.cuda.get_device_name(0)}")
print(f"CUDA:      {torch.version.cuda}")
print(f"PyTorch:   {torch.__version__}")
print(
    f"VRAM:      "
    f"{torch.cuda.get_device_properties(0).total_memory / 1024**3:.2f} GB"
)

# ------------------------------------------------------------
# PATH CHECKS
# ------------------------------------------------------------

if not os.path.exists(MODEL_DIR):
    raise FileNotFoundError(
        f"\nCosyVoice model not found:\n{MODEL_DIR}\n\n"
        "Download CosyVoice-300M into:\n"
        "pretrained_models/CosyVoice-300M"
    )

if not os.path.exists(REFERENCE_AUDIO):
    raise FileNotFoundError(
        f"\nReference audio not found:\n{REFERENCE_AUDIO}"
    )

# ------------------------------------------------------------
# COSYVOICE IMPORT PATHS
# ------------------------------------------------------------

# CosyVoice depends on Matcha-TTS being available.
matcha_path = os.path.join(
    BASE_DIR,
    "third_party",
    "Matcha-TTS"
)

if os.path.exists(matcha_path):
    sys.path.insert(0, matcha_path)

sys.path.insert(0, BASE_DIR)

from cosyvoice.cli.cosyvoice import AutoModel

# ------------------------------------------------------------
# LOAD MODEL
# ------------------------------------------------------------

print("\nLoading CosyVoice-300M...")

load_start = time.perf_counter()

cosyvoice = AutoModel(
    model_dir=MODEL_DIR,
    load_jit=True,
    load_onnx=False,
    load_trt=False,
    fp16=True,
)

load_time = time.perf_counter() - load_start

print(f"Model loaded in {load_time:.2f}s")

if torch.cuda.is_available():
    print(
        f"GPU memory allocated: "
        f"{torch.cuda.memory_allocated() / 1024**3:.2f} GB"
    )

# ------------------------------------------------------------
# GENERATE VOICE
# ------------------------------------------------------------

print("\nGenerating speech...")
print(f"Text: {TEST_TEXT}")

start = time.perf_counter()

audio_chunks = []

with torch.inference_mode():

    results = cosyvoice.inference_zero_shot(
        TEST_TEXT,
        REFERENCE_TEXT,
        REFERENCE_AUDIO,
        stream=True,
    )

    for result in results:

        if "tts_speech" not in result:
            continue

        audio = result["tts_speech"]

        if audio is None:
            continue

        audio_chunks.append(
            audio.detach().cpu()
        )

# ------------------------------------------------------------
# COMBINE AUDIO
# ------------------------------------------------------------

if not audio_chunks:
    raise RuntimeError(
        "CosyVoice returned no audio."
    )

audio = torch.cat(audio_chunks, dim=-1)

generation_time = time.perf_counter() - start

# ------------------------------------------------------------
# SAVE
# ------------------------------------------------------------

output_path = os.path.join(
    BASE_DIR,
    "cosyvoice_output.wav"
)

sample_rate = 22050

torchaudio.save(
    output_path,
    audio,
    sample_rate,
)

# ------------------------------------------------------------
# PERFORMANCE
# ------------------------------------------------------------

audio_seconds = audio.shape[-1] / sample_rate

rtf = generation_time / audio_seconds

print("\n" + "=" * 70)
print("DONE")
print("=" * 70)

print(f"Audio length:     {audio_seconds:.2f}s")
print(f"Generation time:  {generation_time:.2f}s")
print(f"RTF:              {rtf:.2f}x")
print(f"Output:           {output_path}")

if rtf < 1:
    print("Status: REAL-TIME")
elif rtf < 2:
    print("Status: NEAR REAL-TIME")
else:
    print("Status: SLOWER THAN REAL-TIME")

print("=" * 70)