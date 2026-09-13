# SAINT Desktop AI Assistant

SAINT is a desktop AI assistant with voice interaction, memory, automation, and vision capabilities.

## Features

- **Voice Interface**: Wake-word detection, STT, and TTS
- **AI Integration**: Multi-provider support (Ollama, OpenAI, mock)
- **Memory System**: Persistent conversation context
- **Automation**: Desktop control via PyAutoGUI
- **Vision**: OCR and image analysis
- **Analytics**: Detailed performance tracking

## Installation

```bash
pip install -r requirements.txt
```

## Running

```bash
python app.py
```

## TTS Setup

SAINT uses the `kokoro` package for text-to-speech. Models are automatically downloaded from HuggingFace on first use.

### Configuration

Edit `data/config.json`:
- `voice.tts_backend`: `"kokoro"` (default), `"qwen"`, or `"mock"`
- `voice.tts_voice`: Voice name (e.g., `"af_heart"`)
- `voice.tts_device`: `"cuda"` or `"cpu"`

## Voice Pipeline

The voice pipeline consists of:
1. VAD (Voice Activity Detection) - detects when you start/stopspeaking
2. STT (Speech-to-Text) - converts speech to text
3. AI Processing - generates response
4. TTS (Text-to-Speech) - converts response to speech

## Analytics

SAINT tracks detailed analytics including:
- Response times
- Voice latencies
- Tool usage
- Error rates
- Token statistics
