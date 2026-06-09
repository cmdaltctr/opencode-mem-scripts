# OpenCode Memory Backfill & Profile Scripts

Scripts to bootstrap [opencode-mem](https://github.com/tickernelz/opencode-mem) with data
from your existing OpenCode session history. These read your OpenCode SQLite database
(`~/.local/share/opencode/opencode.db`) and populate opencode-mem's vector store and
user profile.

## Prerequisites

- **OpenCode** running with the `opencode-mem` plugin loaded (port 4747 reachable)
- **Python 3.9+** (no extra packages needed — stdlib only)
- **Ollama** running (for the embedding model; only needed by the plugin, not these scripts)

> **Platform note:** These scripts have only been tested on macOS (Apple Silicon).
> They use standard Python stdlib only, so they should work on Linux/Windows too,
> but we have not verified this. Pull requests welcome.

## Scripts

### 1. `backfill-memories.py` — Import session memories

Reads every session from your OpenCode database, extracts user prompts and AI responses,
and posts structured summaries to opencode-mem's REST API (`POST /api/memories`).

```bash
# Dry run — preview what will be imported
python3 backfill-memories.py --dry-run

# Real run — import all sessions
python3 backfill-memories.py

# Limit to first N sessions
python3 backfill-memories.py --limit 20
```

**What it does:**
- Extracts text prompts from `part` and `message` tables in opencode.db
- Groups by session, builds a markdown summary
- Posts to `http://127.0.0.1:4747/api/memories` with project-scoped container tags
- Each memory is tagged `backfill` + `session` — safe to delete later

### 2. `backfill-profile.py` — Import prompts for profile learning

Extracts user and assistant prompts from OpenCode sessions and inserts them into
opencode-mem's `user-prompts.db`. This feeds the profile learning system, which
analyses prompts to build your user profile (preferences, patterns, workflows).

```bash
# Dry run — shows schema and counts without modifying
python3 backfill-profile.py --dry-run

# Real run — inserts prompts and triggers profile refresh
python3 backfill-profile.py

# Limit to first N prompts
python3 backfill-profile.py --limit 500
```

**What it does:**
- Extracts user + assistant text prompts from the OpenCode database
- Inserts them into `~/.opencode-mem/data/user-prompts.db`
- Calls `POST /api/user-profile/refresh` to queue profile learning
- The plugin then uses your configured AI provider to analyse prompts

### 3. `build-profile.py` — Direct profile construction

Calls an AI provider directly (bypassing the plugin's scheduler) to analyse your
prompts and build a user profile. Useful when the plugin's `performUserProfileLearning`
hasn't triggered yet, or to rebuild specific aspects of the profile.

**Setup (one-time):** copy `.env.example` → `.env` and fill in your key + model:

```bash
cp .env.example .env
# edit .env:
#   DEEPSEEK_API_KEY=sk-...
#   PROFILE_API_URL=https://api.deepseek.com/v1/chat/completions
#   PROFILE_MODEL=deepseek-v4-flash
```

**Run:**

```bash
# Build all three aspects (preferences + patterns + workflows)
python3 build-profile.py

# Or build just one
python3 build-profile.py --aspect preferences
python3 build-profile.py --aspect patterns
python3 build-profile.py --aspect workflows
```

**Override at runtime (without touching `.env`):**

```bash
python3 build-profile.py --api-key "sk-..."   # key only
PROFILE_API_URL=https://api.openai.com/v1 PROFILE_MODEL=gpt-4o-mini python3 build-profile.py
```

**What it does:**
- Reads all unanalysed prompts from user-prompts.db
- Samples across the dataset for diversity (every Nth prompt)
- Batches prompts and sends them to the AI for extraction
- Writes results in the plugin's expected format so the web UI renders properly:
  - `preferences`: `{category, description, confidence}`
  - `patterns`:    `{category, description, frequency}`
  - `workflows`:   `{description, steps[], frequency}`
- Deduplicates by `description` and writes to user-profiles.db
- Uses DeepSeek V4 Flash by default (1M context); configurable via env vars

## Typical Workflow

```bash
# 1. Ensure OpenCode is running with opencode-mem loaded
curl -s http://127.0.0.1:4747/api/stats

# 2. Import all session memories (~2 minutes for 150 sessions)
python3 backfill-memories.py

# 3. Import prompts for profile learning (~1 minute)
python3 backfill-profile.py

# 4. Build/update the profile (~3 minutes)
export DEEPSEEK_API_KEY="sk-..."
python3 build-profile.py

# 5. Verify
open http://127.0.0.1:4747
```

## Changing the AI Provider / Model

`build-profile.py` reads its API config from `scripts/.env`. To use a different
provider, edit:

```bash
# scripts/.env
PROFILE_API_URL=https://api.deepseek.com/v1/chat/completions
PROFILE_MODEL=deepseek-v4-flash
DEEPSEEK_API_KEY=sk-...
```

Any OpenAI-compatible chat completions endpoint works. Examples:

| Provider      | `PROFILE_API_URL`                                  | `PROFILE_MODEL`    |
| ------------- | -------------------------------------------------- | ------------------ |
| DeepSeek      | `https://api.deepseek.com/v1/chat/completions`     | `deepseek-v4-flash`|
| OpenAI        | `https://api.openai.com/v1/chat/completions`       | `gpt-4o-mini`      |

The `.env.example` file in this directory shows all available options.

## Configuration

Default paths (override by editing the constants at the top of each script):

- OpenCode DB: `~/.local/share/opencode/opencode.db`
- OpenCode-mem data: `~/.opencode-mem/data/`
- OpenCode-mem API: `http://127.0.0.1:4747`

## Security

- `build-profile.py` reads its API key from `scripts/.env` (gitignored), the
  `--api-key` flag, or the `DEEPSEEK_API_KEY` environment variable.
- `backfill-memories.py` and `backfill-profile.py` need no keys — they talk to the
  locally running opencode-mem plugin.
- No credentials are hardcoded. `.env` is in `.gitignore`.

## License

MIT © 2026 Dr. Muhammad Aizat Md Hawari — see [LICENSE](LICENSE).
