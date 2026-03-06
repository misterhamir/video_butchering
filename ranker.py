import os
import re
import json
import gspread
from google.oauth2.service_account import Credentials
from openai import OpenAI
from dotenv import load_dotenv

# ── Load .env ─────────────────────────────────────────────────────────────────
load_dotenv()

# ── Config ────────────────────────────────────────────────────────────────────
BASE_DIR         = os.path.dirname(os.path.abspath(__file__))
CLIPS_DIR        = os.path.join(BASE_DIR, "03_clips")
CREDS_FILE       = os.path.join(BASE_DIR, "credentials.json")
SHEET_ID         = "1nuXqst7Az550sjU5vfa1v9YjeJG-6H5xEy7GHwiR-kg"
OPENROUTER_KEY   = os.getenv("OPENROUTER_API_KEY", "")
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "google/gemini-flash-1.5")

# ── Column indices (1-based) ───────────────────────────────────────────────────
COL_BODY_PART     = 1   # A
COL_EXERCISE_NAME = 2   # B
COL_CLIP_LINK     = 3   # C
COL_DIFFICULTY    = 4   # D
COL_CLIP_SOURCE   = 5   # E


# ── Google Sheet ──────────────────────────────────────────────────────────────

def connect_sheet():
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds  = Credentials.from_service_account_file(CREDS_FILE, scopes=scopes)
    client = gspread.authorize(creds)
    return client.open_by_key(SHEET_ID).sheet1


def get_unranked_rows(worksheet) -> list[dict]:
    """Return rows where Body Part or Difficulty is empty."""
    rows = worksheet.get_all_values()
    header = rows[0] if rows else []
    unranked = []

    for i, row in enumerate(rows[1:], start=2):  # start=2 = actual sheet row number
        # Pad short rows
        while len(row) < 5:
            row.append("")

        body_part  = row[COL_BODY_PART - 1].strip()
        exercise   = row[COL_EXERCISE_NAME - 1].strip()
        clip_path  = row[COL_CLIP_LINK - 1].strip()
        difficulty = row[COL_DIFFICULTY - 1].strip()

        if exercise and (not body_part or not difficulty):
            unranked.append({
                "row":       i,
                "exercise":  exercise,
                "clip_path": clip_path,
            })

    return unranked


# ── OpenRouter Ranking ────────────────────────────────────────────────────────

def rank_exercises(exercises: list[str], client: OpenAI) -> list[dict]:
    """
    Send all exercise names to OpenRouter in one batch.
    Returns list of {exercise, body_part, rank} dicts.
    """
    exercise_list = "\n".join(f"{i+1}. {e}" for i, e in enumerate(exercises))

    prompt = f"""Rank & categorize all these movement names.

The output should be a JSON array where each item has:
- "exercise": the original exercise name (exact match)
- "body_part": ONE single word category (e.g. "Lower", "Upper", "Core", "Full", "Glutes", "Back", "Shoulders"). 
  If it targets Glute, Butt and Thigh → use "Lower". Each movement must belong to only 1 body part category.
- "rank": difficulty from 1 (easiest) to 10 (hardest)

Rules:
- Use only 1 word for body_part
- No movement should be in more than 1 body part category
- Rank relative to each other within the full list
- Output ONLY the JSON array, no explanation, no markdown

Exercises:
{exercise_list}"""

    response = client.chat.completions.create(
        model=OPENROUTER_MODEL,
        max_tokens=2000,
        messages=[{"role": "user", "content": prompt}],
    )

    raw = response.choices[0].message.content.strip()

    # Strip markdown code fences if present
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)

    try:
        ranked = json.loads(raw)
        return ranked
    except json.JSONDecodeError as e:
        print(f"❌ Failed to parse JSON from OpenRouter: {e}")
        print(f"Raw response:\n{raw}")
        return []


# ── File Renaming ─────────────────────────────────────────────────────────────

def rename_clip(old_path: str, body_part: str, rank: int) -> str:
    """
    Rename clip file to include body_part and rank.
    Example: Lower_07_Straight_Leg_Kicks.mp4
    Returns new path.
    """
    if not old_path or not os.path.exists(old_path):
        return old_path

    directory  = os.path.dirname(old_path)
    filename   = os.path.basename(old_path)
    name, ext  = os.path.splitext(filename)

    # Remove any existing body_part/rank prefix (in case of re-run)
    name = re.sub(r"^[A-Za-z]+_\d+_", "", name)

    new_name = f"{body_part}_{rank:02d}_{name}{ext}"
    new_path = os.path.join(directory, new_name)

    try:
        os.rename(old_path, new_path)
        print(f"   📁 Renamed: {filename} → {new_name}")
        return new_path
    except Exception as e:
        print(f"   ⚠️  Could not rename {filename}: {e}")
        return old_path


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    if not OPENROUTER_KEY:
        print("❌ OPENROUTER_API_KEY not set in .env file")
        return

    # Connect
    print("🔗 Connecting to Google Sheet...")
    worksheet = connect_sheet()
    print("✅ Sheet connected")

    # Get unranked rows
    unranked = get_unranked_rows(worksheet)
    if not unranked:
        print("✅ No unranked rows found — everything is already ranked!")
        return

    print(f"\n📋 Found {len(unranked)} unranked exercise(s):")
    for r in unranked:
        print(f"   Row {r['row']}: {r['exercise']}")

    # Connect to OpenRouter
    ai_client = OpenAI(
        api_key=OPENROUTER_KEY,
        base_url="https://openrouter.ai/api/v1",
    )

    # Send all exercises to OpenRouter in one batch
    print(f"\n🤖 Sending to OpenRouter ({OPENROUTER_MODEL}) for ranking...")
    exercise_names = [r["exercise"] for r in unranked]
    ranked = rank_exercises(exercise_names, ai_client)

    if not ranked:
        print("❌ No ranking results returned. Aborting.")
        return

    # Build lookup: exercise name → {body_part, rank}
    rank_lookup = {}
    for item in ranked:
        name = item.get("exercise", "").strip()
        rank_lookup[name] = {
            "body_part": item.get("body_part", "").strip(),
            "rank":      int(item.get("rank", 0)),
        }

    print(f"✅ Received rankings for {len(rank_lookup)} exercise(s)")

    # Update sheet + rename files
    print("\n✏️  Updating sheet and renaming files...")
    for row_data in unranked:
        exercise  = row_data["exercise"]
        row_num   = row_data["row"]
        clip_path = row_data["clip_path"]

        result = rank_lookup.get(exercise)
        if not result:
            # Try case-insensitive match
            for key, val in rank_lookup.items():
                if key.lower() == exercise.lower():
                    result = val
                    break

        if not result:
            print(f"   ⚠️  No ranking found for: {exercise}")
            continue

        body_part = result["body_part"]
        rank      = result["rank"]

        print(f"\n   Row {row_num}: {exercise}")
        print(f"   Body Part: {body_part} | Rank: {rank}")

        # Update sheet columns A (body_part) and D (difficulty)
        worksheet.update_cell(row_num, COL_BODY_PART, body_part)
        worksheet.update_cell(row_num, COL_DIFFICULTY, rank)

        # Rename the clip file and update sheet column C
        new_path = rename_clip(clip_path, body_part, rank)
        if new_path != clip_path:
            worksheet.update_cell(row_num, COL_CLIP_LINK, new_path)

    print("\n🎉 Ranking complete! Sheet updated and files renamed.")


if __name__ == "__main__":
    main()