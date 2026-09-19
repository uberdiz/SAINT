import time
import torch
import sounddevice as sd
from kokoro import KPipeline

TEXT = "Hello! This is a performance test for Kokoro text to speech."

print("=" * 50)
print("KOKORO SPEED TEST")
print("=" * 50)

device = "cuda" if torch.cuda.is_available() else "cpu"
print(f"Device: {device}")

if device == "cuda":
    print(f"GPU: {torch.cuda.get_device_name(0)}")
    print(f"CUDA: {torch.version.cuda}")

# -----------------------------
# Load
# -----------------------------

print("\nLoading model...")

start = time.perf_counter()

pipeline = KPipeline(
    lang_code="a",
    device=device
)

if device == "cuda":
    torch.cuda.synchronize()

print(f"Model loaded in {time.perf_counter() - start:.3f}s")


# -----------------------------
# CUDA warmup
# -----------------------------

print("\nWarming up CUDA...")

start = time.perf_counter()

for _, _, audio in pipeline(
    "Testing.",
    voice="af_heart"
):
    pass

if device == "cuda":
    torch.cuda.synchronize()

print(f"Warmup: {time.perf_counter() - start:.3f}s")


# -----------------------------
# Actual tests
# -----------------------------

print("\nRunning generation tests...\n")

for i in range(3):

    start = time.perf_counter()

    audio_chunks = []

    for _, _, audio in pipeline(
        TEXT,
        voice="af_heart"
    ):
        audio_chunks.append(audio)

    if device == "cuda":
        torch.cuda.synchronize()

    generation_time = time.perf_counter() - start

    audio = torch.cat(audio_chunks).cpu().numpy()

    audio_length = len(audio) / 24000
    realtime = audio_length / generation_time

    print(f"Test {i + 1}")
    print(f"  Generation: {generation_time:.3f}s")
    print(f"  Audio:      {audio_length:.3f}s")
    print(f"  Speed:      {realtime:.2f}x realtime")
    print()


# -----------------------------
# Play final result
# -----------------------------

print("Playing final result...")

sd.play(audio, 24000)
sd.wait()

print("Done.")