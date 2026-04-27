"""
Claude LLM Recommendations source.

Two-step approach that eliminates hallucination:
  1. Ask Claude to suggest artist names to explore (not specific track titles).
  2. Search Beatport for real tracks by those artists — full BPM/key/genre metadata.

This means every LLM candidate is a real, verified Beatport track that scores
properly in the ranker. Claude provides curation intelligence; Beatport provides
ground truth.

Falls back to direct track suggestions (no Beatport lookup) if no bp client
is passed, but those candidates will lack metadata and score lower.
"""

import json
import math
from datetime import datetime, timezone

import anthropic

from modules.db import get_connection

# ---------------------------------------------------------------------------
# Prompt: ask for artist names, not track titles
# ---------------------------------------------------------------------------

_ARTIST_SUGGESTION_PROMPT = """\
You are an expert deep house music curator with deep knowledge of underground,
boutique-label, and club-circuit artists.

## Listener Taste Profile
{taste_summary}

## Artists Already in Their Catalog (do NOT suggest these)
{known_artists}

## Your Task
Suggest exactly {count} artists the listener would love that are NOT in their catalog.
Focus on underground, boutique-label, and festival-circuit artists in the deep house,
organic house, melodic house, and afro house spaces.

CRITICAL: Only suggest real artists you are highly confident exist and actively
release music in these genres. Do not invent names or guess.

Respond ONLY with a JSON array — no preamble, no trailing text:
[{{"artist": "Artist Name", "reason": "One sentence why they fit the taste profile"}}]
"""

# Fallback prompt used when Beatport is not available
_DIRECT_TRACK_PROMPT = """\
You are an expert deep house music curator.

## Listener Taste Profile
{taste_summary}

## Their Catalog (do NOT recommend these)
{catalog_summary}

Recommend exactly {count} tracks the listener would love that are NOT in their catalog.
IMPORTANT: Only recommend real tracks by real artists you are certain exist.
Do not invent or fabricate any track titles or artist names.

Respond ONLY with a JSON array:
[{{"title": "Track Title", "artist": "Artist Name", "reason": "One sentence why this fits"}}]
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

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


def _get_existing_keys(db_path: str) -> set[tuple]:
    conn = get_connection(db_path)
    rows = conn.execute("SELECT title, artist FROM catalog").fetchall()
    conn.close()
    return {(r["title"].lower(), r["artist"].lower()) for r in rows}


def _parse_json_response(raw: str) -> list[dict]:
    raw = raw.strip()
    if raw.startswith("```"):
        lines = raw.splitlines()
        raw = "\n".join(l for l in lines if not l.startswith("```")).strip()
    items = json.loads(raw)
    if not isinstance(items, list):
        raise ValueError("Expected a JSON array")
    return items


def _call_claude(client: anthropic.Anthropic, system_text: str, user_text: str) -> str:
    with client.messages.stream(
        model="claude-opus-4-6",
        max_tokens=4096,
        system=[
            {
                "type": "text",
                "text": system_text,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        messages=[{"role": "user", "content": user_text}],
    ) as stream:
        response = stream.get_final_message()
    return next((b.text for b in response.content if b.type == "text"), "")


# ---------------------------------------------------------------------------
# Primary path: Claude suggests artists → Beatport finds real tracks
# ---------------------------------------------------------------------------

def _via_beatport(
    client: anthropic.Anthropic,
    bp,
    db_path: str,
    run_id: str,
    taste_profile: dict,
    count: int,
) -> list[dict]:
    n_artists = max(count, math.ceil(count * 4 / 3))  # ask for more artists than tracks needed
    known_artists = "\n".join(
        f"- {a}" for a in (taste_profile.get("top_artists") or [])[:30]
    ) or "(none yet)"

    prompt_text = _ARTIST_SUGGESTION_PROMPT.format(
        taste_summary=_build_taste_summary(taste_profile),
        known_artists=known_artists,
        count=n_artists,
    )

    try:
        raw = _call_claude(client, prompt_text, f"Suggest {n_artists} deep house artists for this listener.")
    except anthropic.APIError as e:
        print(f"  [llm] API error: {e}")
        return []

    try:
        suggestions = _parse_json_response(raw)
    except (json.JSONDecodeError, ValueError) as e:
        print(f"  [llm] Failed to parse artist suggestions: {e}")
        return []

    existing = _get_existing_keys(db_path)
    seen: set[tuple] = set()
    candidates: list[dict] = []
    tracks_per_artist = max(2, math.ceil(count / len(suggestions))) if suggestions else 2
    consecutive_errors = 0

    for suggestion in suggestions:
        if len(candidates) >= count:
            break
        artist_name = (suggestion.get("artist") or "").strip()
        reason = (suggestion.get("reason") or "").strip()
        if not artist_name:
            continue

        try:
            tracks = bp.search_artist_tracks(artist_name, per_page=tracks_per_artist + 1)
            consecutive_errors = 0
        except Exception:
            consecutive_errors += 1
            if consecutive_errors >= 3:
                print(f"  [llm] Stopping Beatport search after {consecutive_errors} consecutive errors.")
                break
            continue

        for track in tracks:
            key = (track["title"].lower(), track["artist"].lower())
            if key in seen or key in existing:
                continue
            seen.add(key)
            candidates.append({
                "run_id": run_id,
                "source": "llm",
                "title": track["title"],
                "artist": track["artist"],
                "beatport_id": track.get("beatport_id"),
                "bpm": track.get("bpm"),
                "camelot_key": track.get("camelot_key"),
                "genre": track.get("genre", ""),
                "sub_genre": track.get("sub_genre", ""),
                "label": track.get("label", ""),
                "release_date": track.get("release_date"),
                "duration_ms": track.get("duration_ms"),
                "reason": f"[LLM→BP] {reason}",
            })
            if len(candidates) >= count:
                break

    return candidates[:count]


# ---------------------------------------------------------------------------
# Fallback path: Claude suggests specific tracks directly (no Beatport)
# ---------------------------------------------------------------------------

def _direct(
    client: anthropic.Anthropic,
    db_path: str,
    run_id: str,
    taste_profile: dict,
    count: int,
) -> list[dict]:
    system_text = _DIRECT_TRACK_PROMPT.format(
        taste_summary=_build_taste_summary(taste_profile),
        catalog_summary=_build_catalog_summary(db_path),
        count=count,
    )
    try:
        raw = _call_claude(
            client, system_text,
            f"Recommend {count} deep house tracks for this listener. JSON array only.",
        )
    except anthropic.APIError as e:
        print(f"  [llm] API error: {e}")
        return []

    try:
        items = _parse_json_response(raw)
    except (json.JSONDecodeError, ValueError) as e:
        print(f"  [llm] Failed to parse response: {e}")
        return []

    existing = _get_existing_keys(db_path)
    candidates = []
    for item in items:
        title = (item.get("title") or "").strip()
        artist = (item.get("artist") or "").strip()
        reason = (item.get("reason") or "").strip()
        if not title or not artist:
            continue
        if (title.lower(), artist.lower()) in existing:
            continue
        candidates.append({
            "run_id": run_id,
            "source": "llm",
            "title": title,
            "artist": artist,
            "reason": reason,
        })
    return candidates[:count]


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def get_llm_recommendations(
    config: dict,
    db_path: str,
    run_id: str,
    taste_profile: dict,
    count: int = 15,
    bp=None,
) -> list[dict]:
    """
    Ask Claude to recommend tracks based on taste profile and catalog.

    When a BeatportClient (bp) is supplied, uses the two-step approach:
    Claude suggests artists → Beatport fetches real tracks with full metadata.
    Otherwise falls back to direct track suggestions (lower quality).
    """
    api_key = config.get("anthropic", {}).get("api_key")
    if not api_key:
        print("  [llm] No Anthropic API key configured — skipping.")
        return []

    client = anthropic.Anthropic(api_key=api_key)

    if bp is not None:
        print("  [llm] Using artist-suggestion → Beatport lookup (no hallucination)")
        candidates = _via_beatport(client, bp, db_path, run_id, taste_profile, count)
    else:
        print("  [llm] No Beatport client — using direct track suggestions (fallback)")
        candidates = _direct(client, db_path, run_id, taste_profile, count)

    _save_candidates(candidates, db_path)
    print(f"  [llm] {len(candidates)} candidates generated")
    return candidates


# ---------------------------------------------------------------------------
# DB persistence
# ---------------------------------------------------------------------------

def _save_candidates(candidates: list[dict], db_path: str) -> None:
    if not candidates:
        return
    conn = get_connection(db_path)
    now = datetime.now(timezone.utc).isoformat()
    for c in candidates:
        conn.execute(
            """
            INSERT INTO recommendations
                (run_id, source, title, artist, spotify_id, beatport_id,
                 bpm, camelot_key, genre, sub_genre, label, release_date,
                 reason, duration_ms, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                c["run_id"], c["source"], c["title"], c["artist"],
                None, c.get("beatport_id"),
                c.get("bpm"), c.get("camelot_key"),
                c.get("genre"), c.get("sub_genre"),
                c.get("label"), c.get("release_date"),
                c.get("reason"), c.get("duration_ms"), now,
            ),
        )
    conn.commit()
    conn.close()
