import os
import re
import subprocess
import gspread
from google.oauth2.service_account import Credentials
from openai import OpenAI
from dotenv import load_dotenv

# ── Load .env ─────────────────────────────────────────────────────────────────
load_dotenv()

# ── Config ────────────────────────────────────────────────────────────────────
BASE_DIR          = os.path.dirname(os.path.abspath(__file__))
RAW_DIR           = os.path.join(BASE_DIR, "01_raw")
PROCESSING_DIR    = os.path.join(BASE_DIR, "02_processing")
CLIPS_DIR         = os.path.join(BASE_DIR, "03_clips")
DONE_DIR          = os.path.join(BASE_DIR, "04_done")
CREDS_FILE        = os.path.join(BASE_DIR, "credentials.json")
SHEET_ID          = "1nuXqst7Az550sjU5vfa1v9YjeJG-6H5xEy7GHwiR-kg"
WHISPER_MODEL     = "base"
OPENROUTER_KEY    = os.getenv("OPENROUTER_API_KEY", "")
OPENROUTER_MODEL  = os.getenv("OPENROUTER_MODEL", "google/gemini-flash-1.5")  # fast + cheap

# ── Helpers ───────────────────────────────────────────────────────────────────

def srt_time_to_seconds(t: str) -> float:
    """Convert SRT timestamp (HH:MM:SS,mmm) to seconds."""
    t = t.replace(",", ".")
    h, m, s = t.split(":")
    return int(h) * 3600 + int(m) * 60 + float(s)


def parse_srt(srt_path: str) -> list[dict]:
    """Return list of {index, start, end, text} dicts from an SRT file."""
    with open(srt_path, "r", encoding="utf-8") as f:
        content = f.read()

    blocks = re.split(r"\n\n+", content.strip())
    segments = []
    for block in blocks:
        lines = block.strip().splitlines()
        if len(lines) < 3:
            continue
        try:
            idx = int(lines[0].strip())
            times = lines[1].strip().split(" --> ")
            start = srt_time_to_seconds(times[0])
            end   = srt_time_to_seconds(times[1])
            text  = " ".join(lines[2:]).strip()
            segments.append({"index": idx, "start": start, "end": end, "text": text})
        except Exception:
            continue
    return segments


def detect_exercise_boundaries(segments: list[dict]) -> list[dict]:
    """
    Detect where new exercises begin based on transition phrases.
    Returns list of {start, end, transcript} dicts — one per exercise section.
    """
    transition_keywords = [
        r"moving on to",
        r"next exercise",
        r"next we",
        r"let'?s move",
        r"now we('re going to| will)",
        r"exercise \d+",
        r"begin with",
        r"start with",
        r"for the (next|final|last)",
        r"switching to",
        r"on to the",
    ]
    pattern = re.compile("|".join(transition_keywords), re.IGNORECASE)

    boundary_indices = [0]  # always start from beginning
    for i, seg in enumerate(segments):
        if pattern.search(seg["text"]):
            boundary_indices.append(i)

    # Deduplicate and sort
    boundary_indices = sorted(set(boundary_indices))

    # Build exercise chunks
    exercises = []
    for i, start_idx in enumerate(boundary_indices):
        end_idx = boundary_indices[i + 1] if i + 1 < len(boundary_indices) else len(segments) - 1
        chunk = segments[start_idx:end_idx + 1]
        if not chunk:
            continue
        exercises.append({
            "start": chunk[0]["start"],
            "end":   chunk[-1]["end"],
            "transcript": " ".join(s["text"] for s in chunk),
        })

    return exercises


def generate_clip_name(transcript: str, client: OpenAI) -> str:
    """Ask LLM via OpenRouter to generate a short exercise clip name."""
    prompt = (
        "You are naming exercise video clips. Based on this transcript snippet, "
        "give a SHORT exercise name (3-6 words max, no punctuation, use Title Case). "
        "Only output the name, nothing else.\n\n"
        f"Transcript: {transcript[:400]}"
    )
    response = client.chat.completions.create(
        model=OPENROUTER_MODEL,
        max_tokens=50,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.choices[0].message.content.strip().replace("/", "-").replace(":", "")


def split_clip(video_path: str, start: float, end: float, output_path: str):
    """Use ffmpeg to cut a clip from start to end seconds."""
    duration = end - start
    subprocess.run([
        "ffmpeg", "-y",
        "-ss", str(start),
        "-i", video_path,
        "-t", str(duration),
        "-c", "copy",
        output_path
    ], check=True, capture_output=True)


def connect_sheet():
    """Return the first worksheet of the target Google Sheet."""
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds  = Credentials.from_service_account_file(CREDS_FILE, scopes=scopes)
    client = gspread.authorize(creds)
    sheet  = client.open_by_key(SHEET_ID)
    return sheet.sheet1


def append_to_sheet(worksheet, exercise_name: str, clip_path: str, source_file: str):
    """Append a new row to the Google Sheet."""
    worksheet.append_row([
        "",                  # Body Part (filled later by Grok ranking)
        exercise_name,       # Exercise Name
        clip_path,           # Clip 1 Link (local path for now)
        "",                  # Difficulty rank (filled later)
        source_file,         # Clip Source
        "",                  # PDF Link
        "",                  # YT Link
    ])


# ── Main pipeline ─────────────────────────────────────────────────────────────

def process_video(video_path: str, worksheet, ai_client: OpenAI):
    filename = os.path.basename(video_path)
    name_no_ext = os.path.splitext(filename)[0]

    print(f"\n{'='*60}")
    print(f"📹 Processing: {filename}")
    print(f"{'='*60}")

    # Step 1 — Transcribe with WhisperX
    print("🎙️  Transcribing with WhisperX...")
    subprocess.run([
        "whisperx", video_path,
        "--model", WHISPER_MODEL,
        "--language", "en",
        "--output_dir", PROCESSING_DIR,
    ], check=True)

    # Find the generated SRT file
    srt_path = os.path.join(PROCESSING_DIR, name_no_ext + ".srt")
    if not os.path.exists(srt_path):
        print(f"❌ SRT not found at {srt_path}, skipping.")
        return

    # Step 2 — Parse SRT and detect exercise boundaries
    print("🔍 Detecting exercise boundaries...")
    segments  = parse_srt(srt_path)
    exercises = detect_exercise_boundaries(segments)
    print(f"   Found {len(exercises)} exercise section(s)")

    # Step 3 — For each exercise: name it, cut clip, write to sheet
    for i, ex in enumerate(exercises, 1):
        print(f"\n   ✂️  Exercise {i}/{len(exercises)}")
        print(f"      Time: {ex['start']:.1f}s → {ex['end']:.1f}s")

        # Generate name via Claude
        clip_name = generate_clip_name(ex["transcript"], ai_client)
        print(f"      Name: {clip_name}")

        # Build output filename: SourceFile_01_ClipName.mp4
        safe_name   = re.sub(r"[^\w\s-]", "", clip_name).strip().replace(" ", "_")
        output_name = f"{name_no_ext}_{i:02d}_{safe_name}.mp4"
        output_path = os.path.join(CLIPS_DIR, output_name)

        # Cut the clip
        split_clip(video_path, ex["start"], ex["end"], output_path)
        print(f"      Saved: {output_name}")

        # Write to Google Sheet
        append_to_sheet(worksheet, clip_name, output_path, name_no_ext)
        print(f"      ✅ Written to sheet")

    # Step 4 — Move original to 04_done
    done_path = os.path.join(DONE_DIR, filename)
    os.rename(video_path, done_path)
    print(f"\n📦 Moved original to 04_done/")


def main():
    # Validate API key
    if not OPENROUTER_KEY:
        print("❌ OPENROUTER_API_KEY not set in .env file")
        return

    # Connect to OpenRouter via OpenAI-compatible client
    ai_client = OpenAI(
        api_key=OPENROUTER_KEY,
        base_url="https://openrouter.ai/api/v1",
    )

    # Connect to Google Sheet
    print("🔗 Connecting to Google Sheet...")
    worksheet = connect_sheet()
    print("✅ Sheet connected")

    # Get all videos in 01_raw
    video_extensions = (".mp4", ".mov", ".avi", ".mkv")
    videos = [
        os.path.join(RAW_DIR, f)
        for f in sorted(os.listdir(RAW_DIR))
        if f.lower().endswith(video_extensions) and not f.startswith(".")
    ]

    if not videos:
        print("⚠️  No videos found in 01_raw/")
        return

    print(f"\n🎬 Found {len(videos)} video(s) to process")

    for video_path in videos:
        try:
            process_video(video_path, worksheet, ai_client)
        except Exception as e:
            print(f"❌ Error processing {video_path}: {e}")
            continue

    print(f"\n🎉 Pipeline complete!")


if __name__ == "__main__":
    main()