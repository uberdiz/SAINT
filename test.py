import sounddevice as sd
from scipy.io import wavfile
import numpy as np
import keyboard
import time

### Audio configuration
SAMPLE_RATE = 24000 
FILENAME = "reference.wav"
CHUNK_SIZE = 1024  # Size of each audio chunk to read

print("Get ready to speak...")
for i in range(3, 0, -1):
    print(f"{i}...")
    time.sleep(1)

print("\n🔴 RECORDING STARTED - Press SPACEBAR to stop recording...")

# Initialize an empty list to store all audio chunks
audio_chunks = []

# Start an input stream to read audio continuously
with sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype='int16') as stream:
    while True:
        # Read a chunk of data from the microphone
        data, overflowed = stream.read(CHUNK_SIZE)
        audio_chunks.append(data)
        
        # Check if the spacebar has been pressed
        if keyboard.is_pressed('space'):
            break

print("\n⏹️ RECORDING FINISHED.")

# Combine all chunks into a single numpy array
audio_data = np.concatenate(audio_chunks, axis=0)

### Save the recording as a WAV file
wavfile.write(FILENAME, SAMPLE_RATE, audio_data)
print(f"✅ File successfully saved as: {FILENAME}")