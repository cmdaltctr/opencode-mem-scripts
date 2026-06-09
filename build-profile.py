#!/usr/bin/env python3
"""Build user profile workflows by analysing prompts with DeepSeek V4 Flash."""

import sqlite3, json, os, time, hashlib, argparse
from urllib.request import Request, urlopen

USER_PROMPTS_DB = os.path.expanduser("~/.opencode-mem/data/user-prompts.db")
PROFILES_DB    = os.path.expanduser("~/.opencode-mem/data/user-profiles.db")
API_URL        = os.environ.get("PROFILE_API_URL", "https://api.deepseek.com/v1/chat/completions")
MODEL          = os.environ.get("PROFILE_MODEL", "deepseek-v4-flash")
BATCH_SIZE     = 100  # prompts per API call

SYSTEM_PROMPT = """You are a user profile analyser. Analyse the conversation prompts
to extract the user's development WORKFLOWS — their habits, sequences, and
repeating behaviours.

A workflow is a pattern of HOW they work, not WHAT they work on. Examples:
- "delegates research to parallel subagents before coding"
- "always writes tests before implementation (TDD)"
- "commits after every incremental step, not in bulk"
- "reads entire codebase structure before making edits"
- "validates config changes with grep/diff before applying"
- "writes documentation alongside code, never after"
- "starts with dry-run/plan before executing changes"
- "uses structured back-and-forth debugging (hypothesis → test → fix)"
- "always specifies model and provider explicitly for agents"
- "inspects generated code before accepting it"

Return ONLY valid JSON — no markdown:
{
  "workflows": [
    {"habit": "description of workflow",
     "frequency": "low|medium|high",
     "evidence": "short excerpt from prompts showing this habit"}
  ]
}

Rules:
- Extract 5-10 workflows
- Look for ACTION SEQUENCES, not topics
- frequency = how consistently observed
- evidence = one-line quote from the prompts
"""


def call_api(batch, api_key):
    payload = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": "Extract workflows from these:\n\n"
             + "\n---\n".join(p[1][:800] for p in batch)}
        ],
        "temperature": 0.1,
        "max_tokens": 4000
    }
    req = Request(API_URL, data=json.dumps(payload).encode(),
                  headers={"Content-Type": "application/json",
                           "Authorization": f"Bearer {api_key}"})
    try:
        with urlopen(req, timeout=120) as resp:
            result = json.loads(resp.read())
            content = result["choices"][0]["message"]["content"]
            start = content.find("{")
            end = content.rfind("}") + 1
            if start >= 0 and end > start:
                return json.loads(content[start:end])
    except Exception as e:
        print(f"  API error: {e}")
    return None


def get_existing_profile():
    conn = sqlite3.connect(PROFILES_DB)
    c = conn.cursor()
    c.execute("SELECT profile_data, id FROM user_profiles WHERE is_active = 1 ORDER BY created_at DESC LIMIT 1")
    row = c.fetchone()
    conn.close()
    if row:
        return json.loads(row[0]), row[1]
    return {}, None


def update_profile_workflows(profile_id, workflows):
    conn = sqlite3.connect(PROFILES_DB)
    c = conn.cursor()
    now = int(time.time() * 1000)
    # Get current profile data
    c.execute("SELECT profile_data FROM user_profiles WHERE id = ?", (profile_id,))
    row = c.fetchone()
    if not row:
        conn.close()
        return
    data = json.loads(row[0])
    data["workflows"] = workflows
    c.execute("UPDATE user_profiles SET profile_data = ?, last_analyzed_at = ?, version = version + 1 WHERE id = ?",
              (json.dumps(data), now, profile_id))
    conn.commit()
    conn.close()


def _load_dotenv():
    """Load .env from the script's directory if present (gitignored)."""
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.exists(env_path):
        return
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key, val = key.strip(), val.strip().strip("'\"")
            if key and key not in os.environ:
                os.environ[key] = val


def main():
    _load_dotenv()

    parser = argparse.ArgumentParser(description="Build user profile workflows")
    parser.add_argument("--api-key", default="",
                        help="DeepSeek API key (or set DEEPSEEK_API_KEY in scripts/.env)")
    args = parser.parse_args()

    # Resolve API key: flag > env var (incl .env) > OpenCode auth.json > error
    api_key = args.api_key
    if not api_key:
        api_key = os.environ.get("DEEPSEEK_API_KEY", os.environ.get("OPENAI_API_KEY", ""))
    if not api_key:
        auth_path = os.path.expanduser("~/.local/share/opencode/auth.json")
        try:
            with open(auth_path) as f:
                auth = json.load(f)
                api_key = auth.get("deepseek", {}).get("key", "")
        except Exception:
            pass
    if not api_key:
        print("✗ No DeepSeek API key found.")
        print("  Create scripts/.env with: DEEPSEEK_API_KEY=sk-...")
        print("  See scripts/.env.example for the template.")
        return
        return

    prompts_rows = []
    conn = sqlite3.connect(USER_PROMPTS_DB)
    c = conn.cursor()
    c.execute("SELECT id, content FROM user_prompts WHERE user_learning_captured = 0")
    prompts_rows = c.fetchall()
    conn.close()

    print(f"Loading {len(prompts_rows)} prompts...")

    # Sample across the dataset
    step = max(1, len(prompts_rows) // 300)
    sampled = prompts_rows[::step]
    print(f"Sampled {len(sampled)} prompts (every {step}th)")

    # Get existing profile
    existing, profile_id = get_existing_profile()
    print(f"Existing profile id: {profile_id}, workflows: {len(existing.get('workflows',[]))}")

    all_workflows = []
    for i in range(0, len(sampled), BATCH_SIZE):
        batch = sampled[i:i + BATCH_SIZE]
        print(f"  Batch {i//BATCH_SIZE + 1}/{(len(sampled)-1)//BATCH_SIZE + 1} "
              f"({len(batch)} prompts)...", end=" ", flush=True)
        result = call_api(batch, api_key)
        if result and "workflows" in result:
            wfs = result["workflows"]
            print(f"✓ {len(wfs)} workflows")
            all_workflows.extend(wfs)
        else:
            print("✗ failed")
        time.sleep(1)

    # Deduplicate by habit name
    seen = set()
    unique = []
    for w in all_workflows:
        key = w.get("habit", "").lower()[:50]
        if key not in seen:
            seen.add(key)
            unique.append(w)

    print(f"\nFinal workflows: {len(unique)}")
    for w in unique:
        print(f"  - {w.get('habit')} ({w.get('frequency')})")

    if unique and profile_id:
        update_profile_workflows(profile_id, unique)
        print(f"\n✓ Updated profile {profile_id} with {len(unique)} workflows")
    elif unique:
        print("\n✗ No existing profile to update")
    else:
        print("\n✗ No workflows extracted")


if __name__ == "__main__":
    main()
