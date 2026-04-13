"""
Claude LLM Recommendations source.

Uses claude-opus-4-6 with adaptive thinking and prompt caching to generate
curated deep house recommendations based on the user's taste profile and
catalog. The system prompt (taste profile + catalog summary) is cached as
an ephemeral prefix to avoid re-tokenizing on repeated runs.
"""

import json
from datetime import datetime, timezone

import anthropic

from modules.db import get_connection

_SYSTEM_TEMPLATE = """\
You are an expert deep house music curator with deep knowledge of underground,
Bandcamp-tier, and club-circuit artists. Your job is to recommend tracks that
fit a listener's taste profile but are NOT already in their catalog.

## Listener Taste Profile
{taste_summary}

## Their Catalog (top-rated tracks — do NOT recommend these)
{catalog_summary}

## Your Task
Recommend exactly {count} tracks the listener would love that are NOT in their
catalog. Prioritize lesser-known artists, underground labels, and tracks that
match the BPM range, energy level, and Camelot key signature of the listener's
favorites. Avoid mainstream chart tracks unless they are genuinely a great fit.

Respond ONLY with a JSON array — no preamble, no trailing text. Each element:
{{
  "title": "Track Title",
  "artist": "Artist Name",
  "reason": "One sentence why this fits the taste profile"
}}
"""


def _build_taste_summary(taste_profile: dict) -> str:
    lines = []
    if taste_profile.get("avg_bpm"):
        bpm = taste_profile["avg_bpm"]
        std = taste_profile.get("bpm_std") or 0
        lines.append(f"- Average BPM: {bpm:.1f} (±{std:.1f})")
    if taste_profile.get("top_keys"):
        lines.append(f"- Favourite Camelot keys: {', '.join(taste_profile['top_keys'])}")
    if taste_profile.get("genre_weights"):
        gw = taste_profile["genre_weights"]
        top = ", ".join(f"{g} ({w:.0%})" for g, w in list(gw.items())[:4])
        lines.append(f"- Genre breakdown: {top}")
    if taste_profile.get("top_labels"):
        lines.append(f"- Favourite labels: {', '.join(taste_profile['top_labels'][:6])}")
    if taste_profile.get("top_artists"):
        lines.append(f"- Known artists: {', '.join(taste_profile['top_artists'][:10])}")
    return "\n".join(lines) if lines else "No profile data available."


def _build_catalog_summary(db_path: str, top_n: int = 60) -> str:
    """Return the top N rated tracks as a compact text list."""
    conn = get_connection(db_path)
    rows = conn.execute(
        """
        SELECT title, artist, bpm, key_camelot,
               CAST(COALESCE(NULLIF(rating, ''), '0') AS INTEGER) as play_count
        FROM catalog
        WHERE play_count > 0
        ORDER BY play_count DESC
        LIMIT ?
        """,
        (top_n,),
    ).fetchall()
    conn.close()

    if not rows:
        return "Catalog is empty."

    lines = []
    for r in rows:
        parts = [f'"{r["title"]}" by {r["artist"]}']
        meta = []
        if r["bpm"]:
            meta.append(f"{r['bpm']} BPM")
        if r["key_camelot"]:
            meta.append(r["key_camelot"])
        if meta:
            parts.append(f"({', '.join(meta)})")
        lines.append(" ".join(parts))
    return "\n".join(lines)


def get_llm_recommendations(
    config: dict,
    db_path: str,
    run_id: str,
    taste_profile: dict,
    count: int = 15,
) -> list[dict]:
    """
    Ask Claude to recommend tracks based on taste profile and catalog.

    Uses prompt caching on the system prompt so repeated runs within
    the 5-minute cache TTL avoid re-tokenizing the full catalog.
    """
    api_key = config.get("anthropic", {}).get("api_key")
    if not api_key:
        print("  [llm] No Anthropic API key configured — skipping.")
        return []

    client = anthropic.Anthropic(api_key=api_key)

    taste_summary = _build_taste_summary(taste_profile)
    catalog_summary = _build_catalog_summary(db_path)

    system_text = _SYSTEM_TEMPLATE.format(
        taste_summary=taste_summary,
        catalog_summary=catalog_summary,
        count=count,
    )

    try:
        # Stream with adaptive thinking; cache the large system prompt
        with client.messages.stream(
            model="claude-opus-4-6",
            max_tokens=4096,
            thinking={"type": "adaptive"},
            system=[
                {
                    "type": "text",
                    "text": system_text,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[
                {
                    "role": "user",
                    "content": (
                        f"Please recommend exactly {count} deep house tracks "
                        "that would be perfect for my collection. "
                        "Return only the JSON array as specified."
                    ),
                }
            ],
        ) as stream:
            response = stream.get_final_message()
    except anthropic.APIError as e:
        print(f"  [llm] API error: {e}")
        return []

    # Extract the text block from the response (skip thinking blocks)
    raw_text = ""
    for block in response.content:
        if block.type == "text":
            raw_text = block.text
            break

    candidates = _parse_recommendations(raw_text, run_id)

    # Deduplicate against existing catalog
    conn = get_connection(db_path)
    existing = {
        (r["title"].lower(), r["artist"].lower())
        for r in conn.execute("SELECT title, artist FROM catalog").fetchall()
    }
    conn.close()

    candidates = [
        c for c in candidates
        if (c["title"].lower(), c["artist"].lower()) not in existing
    ][:count]

    _save_candidates(candidates, db_path)
    print(f"  [llm] {len(candidates)} candidates generated")
    return candidates


def _parse_recommendations(raw: str, run_id: str) -> list[dict]:
    """Extract JSON array from Claude's response text."""
    raw = raw.strip()

    # Strip markdown code fence if present
    if raw.startswith("```"):
        lines = raw.splitlines()
        raw = "\n".join(
            line for line in lines
            if not line.startswith("```")
        ).strip()

    try:
        items = json.loads(raw)
        if not isinstance(items, list):
            raise ValueError("Expected a JSON array")
    except (json.JSONDecodeError, ValueError) as e:
        print(f"  [llm] Failed to parse response: {e}")
        return []

    candidates = []
    for item in items:
        title = item.get("title", "").strip()
        artist = item.get("artist", "").strip()
        reason = item.get("reason", "").strip()
        if title and artist:
            candidates.append(
                {
                    "run_id": run_id,
                    "source": "llm",
                    "title": title,
                    "artist": artist,
                    "spotify_id": None,
                    "reason": reason,
                }
            )
    return candidates


def _save_candidates(candidates: list[dict], db_path: str) -> None:
    if not candidates:
        return
    conn = get_connection(db_path)
    now = datetime.now(timezone.utc).isoformat()
    for c in candidates:
        conn.execute(
            """
            INSERT INTO recommendations (run_id, source, title, artist, spotify_id, reason, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                c["run_id"],
                c["source"],
                c["title"],
                c["artist"],
                c.get("spotify_id"),
                c.get("reason"),
                now,
            ),
        )
    conn.commit()
    conn.close()
