# SAINT onboarding (first run)

Figma: <https://www.figma.com/design/XCpQUI3TpVjhwUtq4SoZzH> — dark theme, built from SAINT's own tokens
(`ui/theme.py`: near-black surfaces, hairline borders, one orange accent `#feaa34`, 12 px cards, 8 px
controls) and icons (`ui/icons.py`). Inter stands in for Segoe UI, which Figma can't load here.

The file has the variables (collection **SAINT**), text styles (`SAINT/*`), a component library
(Button, Chip, Switch, Step, Choice card, Setting row, Service row, 21 icons) and screens 1–3.
Screens 4–7 are scripted in [`onboarding_screens_4-7.figma.js`](onboarding_screens_4-7.figma.js) —
the Starter plan's MCP call limit stopped them being built. The same script fixes the green/amber chips,
whose tint rendered solid. Run it either way:

- **Figma desktop** (no call limit): Plugins → Development → Import plugin from manifest… →
  [`figma-plugin/manifest.json`](figma-plugin/manifest.json), open the file's onboarding page, run
  *SAINT onboarding screens 4-7* once.
- **Figma MCP**: the script's contents as the `use_figma` code on that file.

Every screen: a 340 px rail with the seven steps and a "Stays on this PC" note, then title, one-paragraph
explanation, the step's controls, and Back / Skip for now / Continue. Everything but the microphone is
skippable and can be done later in Settings.

| # | Step | What the user does | Wired to |
|---|------|--------------------|----------|
| 1 | Welcome | Meet SAINT; *Start setup* or *Use my data from another PC* | `core/setup.py` first-run check, SAINT Link pairing |
| 2 | Microphone | Pick the mic, watch the level, say “Hey SAINT” 3× | `modules/voice/wake_word.py` score, `voice.auto_start`, `voice.music_hotwords` |
| 3 | Voice | Pick a Kokoro voice, set **voice volume**, *Hear it*; download progress, Windows voice meanwhile | `voice.tts_voice`, `voice.volume` (0–150 %), `modules/voice/tts_service.py` fallback + `core/model_assets.ensure_kokoro` |
| 4 | Your apps | See the apps/games SAINT found and names it has learned to hear (“clad” → Claude) | `modules/desktop/apps.py` catalog, `modules/desktop/vocabulary.py` |
| 5 | Connect | Spotify, AI model, iPhone, another PC — all optional | `modules/spotify/auth.py`, Ollama check, `modules/link` |
| 6 | Safety | Choose what SAINT asks before doing; example of a scene asking first | permission policies (`core/permissions.py`), `modules/automation/scene_plan.py` |
| 7 | Ready | Six commands to try; *Open SAINT* or *Watch the demo* | `ui/demo.py` |

Testing it without your data: `run.bat --profile clean` starts SAINT as a fresh install
(see README → *Testing without your real data*).
