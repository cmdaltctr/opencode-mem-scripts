#!/usr/bin/env python3
"""Build user profile (preferences + patterns + workflows) by analysing prompts.

Reads prompts from opencode-mem's user-prompts.db, calls the configured AI provider
in batches, and writes the result to user-profiles.db. Uses the plugin's expected
field names so the web UI renders correctly:

  preferences: {category, description, confidence, evidence[], lastUpdated}
  patterns:    {category, description, frequency, lastSeen}
  workflows:   {description, steps[], frequency}
"""

import sqlite3, json, os, time, hashlib, argparse
from urllib.request import Request, urlopen

USER_PROMPTS_DB = os.path.expanduser("~/.opencode-mem/data/user-prompts.db")
PROFILES_DB    = os.path.expanduser("~/.opencode-mem/data/user-profiles.db")
API_URL        = os.environ.get("PROFILE_API_URL", "https://api.deepseek.com/v1/chat/completions")
MODEL          = os.environ.get("PROFILE_MODEL", "deepseek-v4-flash")
BATCH_SIZE     = 100  # prompts per API call

PREFERENCES_PROMPT = """You are a user profile analyser. Analyse the conversation
prompts and extract the user's stable PREFERENCES — code style, communication style,
tool choices, frameworks, architecture choices.

Return ONLY valid JSON — no markdown:
{
  "preferences": [
    {
      "category": "code-style|communication|tools|languages|frameworks|architecture|workflow|process",
      "description": "specific, observable preference (e.g. 'prefers TypeScript over JavaScript')",
      "confidence": 0.5-1.0
    }
  ]
}

Rules:
- Extract 5-10 preferences
- Confidence based on how clearly the preference is expressed
- Skip vague/generic observations
- description should be 1 sentence, under 100 chars
"""

PATTERNS_PROMPT = """You are a user profile analyser. Analyse the conversation prompts
and extract the user's PATTERNS — recurring topics, problem domains, and technical
interests they engage with frequently.

Return ONLY valid JSON — no markdown:
{
  "patterns": [
    {
      "category": "topic|domain|technology|skill",
      "description": "name of the recurring pattern (e.g. 'MCP server development and configuration')",
      "frequency": 0.0-1.0
    }
  ]
}

Rules:
- Extract 5-10 patterns
- frequency = how often this topic appears across the prompts
- description is the pattern name, 2-6 words
"""

WORKFLOWS_PROMPT = """You are a user profile analyser. Analyse the conversation prompts
to extract the user's development WORKFLOWS — their habits, sequences, and
repeating behaviours.

Return ONLY valid JSON — no markdown:
{
  "workflows": [
    {
      "description": "short name of the workflow",
      "steps": ["step 1", "step 2", "step 3"],
      "frequency": 0.0-1.0
    }
  ]
}

Field rules:
- description: 3-8 word summary
- steps: 2-5 concrete actions in order, each under 80 chars
- frequency: how consistently observed (0.0 = rarely, 1.0 = always)

Example:
- {"description": "Parallel subagent delegation", "steps": ["Identify independent subtasks", "Launch subagents simultaneously", "Merge findings"], "frequency": 0.95}

Extract 5-10 workflows."""


def _load_dotenv():
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


def _resolve_api_key(flag_key: str) -> str:
    if flag_key:
        return flag_key
    for var in ("DEEPSEEK_API_KEY", "OPENAI_API_KEY"):
        val = os.environ.get(var, "")
        if val:
            return val
    auth_path = os.path.expanduser("~/.local/share/opencode/auth.json")
    try:
        with open(auth_path) as f:
            auth = json.load(f)
            return auth.get("deepseek", {}).get("key", "")
    except Exception:
        pass
    return ""


def call_api(batch, system_prompt, api_key):
    payload = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": "Analyse these prompts:\n\n"
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


def get_prompts():
    conn = sqlite3.connect(USER_PROMPTS_DB)
    c = conn.cursor()
    c.execute("SELECT id, content FROM user_prompts WHERE user_learning_captured = 0")
    rows = c.fetchall()
    conn.close()
    return rows


def get_active_profile():
    conn = sqlite3.connect(PROFILES_DB)
    c = conn.cursor()
    c.execute("SELECT id, profile_data FROM user_profiles WHERE is_active = 1 ORDER BY created_at DESC LIMIT 1")
    row = c.fetchone()
    conn.close()
    if not row:
        return None, {}
    return row[0], json.loads(row[1])


def write_profile(profile_id, data, n_analyzed):
    conn = sqlite3.connect(PROFILES_DB)
    c = conn.cursor()
    now = int(time.time() * 1000)
    if profile_id is None:
        profile_id = hashlib.sha256(str(now).encode()).hexdigest()[:16]
        c.execute("""INSERT INTO user_profiles
            (id, user_id, display_name, user_name, user_email, profile_data,
             version, created_at, last_analyzed_at, total_prompts_analyzed, is_active)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (profile_id, "aizat.hawari@gmail.com", "Dr Aizat", "aizat",
             "aizat.hawari@gmail.com", json.dumps(data),
             1, now, now, n_analyzed, 1))
    else:
        c.execute("UPDATE user_profiles SET profile_data = ?, last_analyzed_at = ?, "
                  "total_prompts_analyzed = ?, version = version + 1 WHERE id = ?",
                  (json.dumps(data), now, n_analyzed, profile_id))
    conn.commit()
    conn.close()
    return profile_id


def merge_results(merged, new, key, dedupe_field="description"):
    if not new or key not in new:
        return merged
    merged.setdefault(key, [])
    seen = {item.get(dedupe_field, "").lower() for item in merged[key]}
    for item in new[key]:
        d = item.get(dedupe_field, "").lower()
        if d and d not in seen:
            seen.add(d)
            merged[key].append(item)
    return merged


def run_aspect(name, prompts, system_prompt, api_key, batch_size):
    print(f"\n── {name} ──")
    merged = {}
    n_batches = (len(prompts) - 1) // batch_size + 1
    for i in range(0, len(prompts), batch_size):
        batch = prompts[i:i + batch_size]
        print(f"  Batch {i//batch_size + 1}/{n_batches} ({len(batch)})...", end=" ", flush=True)
        result = call_api(batch, system_prompt, api_key)
        if result:
            merged = merge_results(merged, result, list(result.keys())[0])
            n = len(list(result.values())[0])
            print(f"✓ {n} items")
        else:
            print("✗ failed")
        time.sleep(1)
    return merged


def main():
    _load_dotenv()
    parser = argparse.ArgumentParser(description="Build user profile")
    parser.add_argument("--api-key", default="")
    parser.add_argument("--aspect", choices=["preferences", "patterns", "workflows", "all"],
                        default="all", help="Which aspect to build")
    args = parser.parse_args()

    api_key = _resolve_api_key(args.api_key)
    if not api_key:
        print("✗ No API key. Set DEEPSEEK_API_KEY in scripts/.env or use --api-key.")
        return

    profile_id, existing = get_active_profile()
    print(f"Active profile: {profile_id or '(none — will create new)'}")

    all_prompts = get_prompts()
    print(f"Available prompts: {len(all_prompts)}")
    if not all_prompts:
        print("Run backfill-profile.py first to populate prompts")
        return

    step = max(1, len(all_prompts) // 300)
    sampled = all_prompts[::step]
    print(f"Sampled: {len(sampled)} (every {step}th)\n")

    aspects = (["preferences", "patterns", "workflows"] if args.aspect == "all"
               else [args.aspect])
    data = {k: existing.get(k, []) for k in aspects}

    for aspect in aspects:
        sys_prompt = {
            "preferences": PREFERENCES_PROMPT,
            "patterns":    PATTERNS_PROMPT,
            "workflows":   WORKFLOWS_PROMPT,
        }[aspect]
        result = run_aspect(aspect, sampled, sys_prompt, api_key, BATCH_SIZE)
        if result and aspect in result:
            data[aspect] = result[aspect]

    print(f"\nFinal counts:")
    for k in ["preferences", "patterns", "workflows"]:
        print(f"  {k:12} {len(data.get(k, []))}")

    profile_id = write_profile(profile_id, data, len(all_prompts))
    print(f"\n✓ Profile {profile_id} written")
    print(f"  Refresh http://127.0.0.1:4747 → User Profile tab")


if __name__ == "__main__":
    main()
