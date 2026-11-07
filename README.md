# Saathi

> [A short tagline describing your project.]

## Overview

[Describe what the project does, who it is for, and the problem it solves.]

## Features

- [Feature one]
- [Feature two]
- [Feature three]

## Getting Started

### Prerequisites

- [Required tool or runtime and version]
- [Other prerequisite]

### Installation

1. Clone the repository:

   ```bash
   git clone [repository-url]
   cd [repository-directory]
   ```

2. Install dependencies:

   ```bash
   [installation-command]
   ```

3. Configure the project:

   ```bash
   [configuration-command]
   ```

## Usage

```bash
[command-to-run-the-project]
```

[Add examples or instructions for common use cases.]

## Configuration

| Name | Description | Required |
|------|-------------|----------|
| `[SETTING_NAME]` | [What this setting controls] | [Yes/No] |

## Tests

```bash
[command-to-run-tests]
```

## Contributing

Contributions are welcome. [Add instructions for reporting issues, proposing changes, and submitting pull requests.]

## License

[Specify the license for this project.]


## WhatsApp Multi-User Bot

Saathi serves multiple students at once. Each WhatsApp number gets its own private LMS account, session, and monitor.

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

1. Send any message to the bot's WhatsApp number.
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
