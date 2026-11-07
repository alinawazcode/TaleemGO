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
