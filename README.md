# TaleemGo AI

> Your LMS assistant on WhatsApp.

## Overview

TaleemGo AI connects students with their university LMS over WhatsApp. It tracks courses, attendance, upcoming assignment deadlines, and allows downloading lecture materials directly within WhatsApp.

## Features

- Multi-user support with isolated sessions and encrypted token storage.
- Course registration, attendance tracking, and lecture file downloads.
- Real-time automated deadline reminders and lecture upload notifications.
- Bilingual and natural language understanding support.

## Getting Started

### Prerequisites

- Node.js (v18+)
- Python (3.10+)
- Google Chrome

### Installation

1. Clone the repository:

   ```bash
   git clone https://github.com/alinawazcode/Saathi.git
   cd Saathi
   ```

2. Install dependencies:

   ```bash
   pip install -r requirements.txt
   cd whatsapp_bridge
   npm install
   cd ..
   ```

3. Configure the project:
   Copy `.env.example` to `.env` and fill in your values.
   > **Note for existing users:** If you have an existing `.env` file, please update `ACTIVATION_PHRASES` to `taleemgo ai,taleem go ai,taleemgo` (or remove that line to use the new default). You should also rename `SATHI_SECRET_KEY` to `TALEEMGO_SECRET_KEY` (the bot will still read `SATHI_SECRET_KEY` for backwards compatibility).
   > **WhatsApp Profile:** The WhatsApp profile name and avatar for the bot's number must be configured manually in the WhatsApp app on the linked device.

## WhatsApp Multi-User Bot

TaleemGo AI serves multiple students at once. Each WhatsApp number gets its own private LMS account, session, and monitor.

### How to run

**Terminal 1 — Python backend:**
```bash
uvicorn app.wa_bot:api --port 8000
```

**Terminal 2 — WhatsApp bridge:**
```bash
cd whatsapp_bridge
npm start
```

Scan the QR code with WhatsApp on the linked phone → Linked Devices → Link a Device.

### First-time user flow

1. Send an activation phrase (e.g. `Hi TaleemGo AI`) to the bot's WhatsApp number.
2. The bot asks for your LMS email, then your password (used once, never stored).
3. On success you receive "Connected as …" and the full help text.
4. From then on, all commands and automatic alerts are private to your account.

### Commands

| Command | Description |
|---|---|
| `courses` | List your enrolled courses |
| `upcoming` | Pending assignments with deadlines |
| `assignments` | All assignments with status |
| `attendance [course]` | Overall or per-course attendance |
| `resources <course>` | Lecture resources by week |
| `clos <course>` | Course Learning Outcomes |
| `download <course> <title>` | Download and receive a lecture file |
| `logout` | Disconnect your account and delete all stored data |
| `login` | Re-authenticate after session expiry |

## Natural Language Front-End (TaleemGo AI)

Students can communicate in everyday language instead of typing exact commands. Supported languages include **English**, **Sindhi** (Sindhi script and Roman Sindhi), **Urdu** (Urdu script and Roman Urdu), and **Hindi**.

### Examples

- **Sindhi Script**:
  - `منهنجي NLP جي حاضري ڪيتري آهي؟` → Checks attendance for NLP
  - `HCI جي پهرين هفتي جا سلائيڊس موڪليو` → Downloads Week 1 slides for HCI
  - `ڪو اسائنمينٽ ڏيڻو آهي؟` → Lists pending/upcoming assignments
  - `مون کي منهنجا ڪورس ڏيکاريو` → Lists enrolled courses
- **Roman Sindhi**:
  - `munhinji NLP jo haziri ketri aahe`
  - `HCI jo pehrio hafto moklyo`
  - `madad` → Displays help
- **Urdu Script**:
  - `پہلے ہفتے کی سلائیڈز بھیجو`
  - `میرا NLP کا اٹینڈنس کتنا ہے؟`
- **Roman Urdu / Hindi**:
  - `mera NLP ka attendance kitna hai`
  - `HCI ke pehle hafte ki slides bhej do`
  - `kal koi deadline hai?`

### Free LLM Providers & Setup

TaleemGo AI uses free OpenAI-compatible APIs in an automatic failover chain:
1. **Google Gemini** (via Google AI Studio OpenAI-compatible endpoint):
   - Key: Obtain a free API key at [Google AI Studio](https://aistudio.google.com/).
   - Env: `GEMINI_API_KEY`, default model: `gemini-2.0-flash`.
2. **Groq**:
   - Key: Free fast inference key at [Groq Console](https://console.groq.com/).
   - Env: `GROQ_API_KEY`, default model: `llama-3.3-70b-versatile`.
3. **OpenRouter**:
   - Key: Free key at [OpenRouter](https://openrouter.ai/).
   - Env: `OPENROUTER_API_KEY`, default model: `meta-llama/llama-3.3-70b-instruct:free`.
4. **Cerebras**:
   - Key: Free high-speed key at [Cerebras Cloud](https://cloud.cerebras.ai/).
   - Env: `CEREBRAS_API_KEY`, default model: `llama3.1-8b`.

### How Fallback & Offline Resilience Work

- **Time Budget**: Per-message budget of 12 seconds total (6s per-request timeout).
- **Failover Chain**: Providers are attempted in the order listed in `LLM_PROVIDERS`. If provider 1 returns 429 (rate limit) or 5xx (server error), it is placed on thread-safe cooldown and the next provider answers immediately.
- **Offline Rule Fallback**: If all providers fail, keys are omitted, or a user exceeds their hourly limit (`LLM_USER_LIMIT_PER_HOUR=20`), the bot seamlessly falls back to the built-in offline rule matcher (`app/llm/rules.py`).
- **Zero Hallucination / Zero Jailbreak Risk**: The bot never sends any text generated by the model to students. The model only outputs strict JSON actions from a whitelisted enum. All student-facing replies are either LMS data outputs or fixed localized templates.

### Off-Topic Policy & Lockout

TaleemGo AI is an academic LMS assistant, not a general-purpose chatbot.
- **Silence on Off-Topic**: Unrelated requests (jokes, general knowledge, coding help, role-play) receive silence (no response).
- **Lockout Mechanism**: If a student sends 2 off-topic messages within 10 minutes (`LLM_STRIKES=2`, `LLM_STRIKE_WINDOW_MIN=10`), a 2-hour commands-only lockout is engaged (`LLM_LOCK_HOURS=2`). A jailbreak attempt triggers an instant 2-strike lockout.
- **During Lockout**: Standard commands (`attendance NLP`, `download HCI week 1`, `courses`, `help`, `logout`) continue to function normally. Natural language processing is paused.
- **Admin Exemption**: The administrator WhatsApp number (`ADMIN_NUMBER`) is exempt from lockouts.
- **Admin Monitoring**: Sending `llm` from the administrator chat displays provider health, calls today, recent error classes, and currently locked user count.

### Privacy Note

- Passwords and emails during sign-up never reach any LLM provider.
- No LMS data (grades, course titles, assignment details) is ever sent to any LLM provider.
- *Notice on Free Tiers*: Free tiers of third-party LLM providers may retain anonymized logs according to their platform terms. Students are informed on first sign-up never to send their password except during the initial login prompt.

### Automatic alerts (no action needed)

- 📚 New lecture uploaded
- 📝 New assignment posted
- ✅ Attendance marked
- ⏰ Deadline reminder — 3 days before and 1 day before

### What is stored per user

- `data/users.db` — email, status, timestamps (NO password)
- `data/users/<hash>/tokens.enc` — Fernet-encrypted Supabase session tokens
- `data/users/<hash>/monitor_state.json` — IDs of already-seen items

Everything under `data/` is git-ignored.
