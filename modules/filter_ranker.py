"""
Filter, score, and sort candidates for the weekly mix.

Scoring weights (from architecture doc):
  BPM & Key Fit       0.25  — BPM within taste profile range + Camelot key fit
  Genre Match         0.15  — matches preferred genres
  Multi-Source Bonus  0.15  — recommended by 2+ sources
  Recency             0.20  — released within last 6 months; decays over 2 years
  Artist Familiarity  0.10  — known artists/labels
  Novelty             0.15  — not in any previous published mix

Final ordering uses a greedy nearest-neighbour walk on the Camelot wheel
to produce a harmonically mixed tracklist.
"""

import math
from datetime import datetime, timedelta, timezone
from modules.db import get_connection

# ---------------------------------------------------------------------------
# Camelot wheel helpers  (keys use m/d suffix: 10m, 12d, etc.)
# ---------------------------------------------------------------------------

def _parse_camelot(key: str) -> tuple[int, str] | None:
    if not key or len(key) < 2:
        return None
    mode = key[-1].lower()
    if mode not in ("m", "d"):
        return None
    try:
        num = int(key[:-1])
    except ValueError:
        return None
    if num < 1 or num > 12:
        return None
    return (num, mode)


def camelot_distance(key_a: str, key_b: str) -> int | None:
    a = _parse_camelot(key_a)
    b = _parse_camelot(key_b)
    if a is None or b is None:
        return None
    num_a, mode_a = a
    num_b, mode_b = b
    mode_penalty = 0 if mode_a == mode_b else 1
    diff = abs(num_a - num_b)
    circular = min(diff, 12 - diff)
    return circular + mode_penalty


def camelot_score(candidate_key: str, top_keys: list[str]) -> float:
    if not top_keys or not candidate_key:
        return 0.5
    distances = [
        camelot_distance(candidate_key, k)
        for k in top_keys
        if camelot_distance(candidate_key, k) is not None
    ]
    if not distances:
        return 0.5
    score_map = {0: 1.0, 1: 0.8, 2: 0.5}
    return score_map.get(min(distances), 0.2)


# ---------------------------------------------------------------------------
# BPM fit
# ---------------------------------------------------------------------------

def bpm_score(candidate_bpm: float | None, avg_bpm: float | None, bpm_std: float | None) -> float:
    if not candidate_bpm or not avg_bpm:
        return 0.5
    std = bpm_std or 5.0
    diff = abs(candidate_bpm - avg_bpm)
    # Score 1.0 within 1 std, decays to 0.0 at 3 std
    return max(0.0, 1.0 - (diff / (3 * std)))


# ---------------------------------------------------------------------------
# Genre match
# ---------------------------------------------------------------------------

def genre_score(candidate_genre: str | None, genre_weights: dict) -> float:
    if not candidate_genre or not genre_weights:
        return 0.5
    # Direct match: return the genre's weight normalised to 0-1
    for genre, weight in genre_weights.items():
        if genre.lower() in candidate_genre.lower() or candidate_genre.lower() in genre.lower():
            # Scale: if this genre is 50% of catalog it scores 1.0; 10% scores 0.2
            max_weight = max(genre_weights.values()) or 1
            return weight / max_weight
    return 0.2  # unknown genre


# ---------------------------------------------------------------------------
# Recency
# ---------------------------------------------------------------------------

def recency_score(release_date: str | None) -> float:
    """
    1.0 for tracks released in the last 6 months.
    Decays linearly to 0.0 over 2 years.
    Neutral (0.5) for unknown release date.
    """
    if not release_date:
        return 0.5
    try:
        released = datetime.fromisoformat(release_date.split("T")[0])
        if released.tzinfo is None:
            released = released.replace(tzinfo=timezone.utc)
    except (ValueError, AttributeError):
        return 0.5

    now = datetime.now(timezone.utc)
    age_days = (now - released).days
    six_months = 180
    two_years = 730

    if age_days <= six_months:
        return 1.0
    if age_days >= two_years:
        return 0.0
    return 1.0 - (age_days - six_months) / (two_years - six_months)


# ---------------------------------------------------------------------------
# Artist / label familiarity
# ---------------------------------------------------------------------------

def familiarity_score(
    candidate_artist: str,
    candidate_label: str | None,
    top_artists: list[str],
    top_labels: list[str],
) -> float:
    artist_names = [a.strip().lower() for a in candidate_artist.split(",")]
    known_artist = any(a in (x.lower() for x in top_artists) for a in artist_names)
    known_label = (
        candidate_label
        and candidate_label.lower() in (l.lower() for l in top_labels)
    )
    if known_artist:
        return 0.7   # cap — don't over-index on same names
    if known_label:
        return 0.5
    return 0.3


# ---------------------------------------------------------------------------
# Novelty (not in previous mixes)
# ---------------------------------------------------------------------------

def _get_published_titles(db_path: str) -> set[tuple]:
    """All tracks ever published — used for the soft novelty score."""
    conn = get_connection(db_path)
    rows = conn.execute("SELECT LOWER(title) as t, LOWER(artist) as a FROM mix_tracks").fetchall()
    conn.close()
    return {(r["t"], r["a"]) for r in rows}


def _get_recent_titles(db_path: str, weeks: int) -> set[tuple]:
    """Tracks published in the last N weeks — hard-excluded from selection."""
    conn = get_connection(db_path)
    rows = conn.execute(
        """
        SELECT LOWER(mt.title) AS t, LOWER(mt.artist) AS a
        FROM mix_tracks mt
        JOIN weekly_mixes wm ON mt.run_id = wm.run_id
        WHERE wm.published_at >= datetime('now', ?)
        """,
        (f"-{weeks * 7} days",),
    ).fetchall()
    conn.close()
    return {(r["t"], r["a"]) for r in rows}


def novelty_score(title: str, artist: str, published: set[tuple]) -> float:
    key = (title.lower(), artist.lower())
    return 0.0 if key in published else 1.0


# ---------------------------------------------------------------------------
# Multi-source bonus
# ---------------------------------------------------------------------------

def _count_sources(candidates: list[dict]) -> dict[tuple, int]:
    """Count how many sources recommended each (title, artist)."""
    counts: dict[tuple, int] = {}
    for c in candidates:
        key = (c["title"].lower(), c["artist"].lower())
        counts[key] = counts.get(key, 0) + 1
    return counts


# ---------------------------------------------------------------------------
# Score all candidates
# ---------------------------------------------------------------------------

def score_candidates(
    candidates: list[dict],
    taste_profile: dict,
    db_path: str,
) -> list[dict]:
    avg_bpm = taste_profile.get("avg_bpm")
    bpm_std = taste_profile.get("bpm_std")
    top_keys = taste_profile.get("top_keys") or []
    genre_weights = taste_profile.get("genre_weights") or {}
    top_labels = taste_profile.get("top_labels") or []
    top_artists = taste_profile.get("top_artists") or []

    published = _get_published_titles(db_path)
    source_counts = _count_sources(candidates)

    scored = []
    for c in candidates:
        key = (c["title"].lower(), c["artist"].lower())
        n_sources = source_counts.get(key, 1)

        # BPM + Camelot key (combined weight 0.25)
        ckey = c.get("camelot_key") or c.get("key_camelot") or ""
        k_score = camelot_score(ckey, top_keys)
        b_score = bpm_score(c.get("bpm"), avg_bpm, bpm_std)
        bpm_key_score = (k_score + b_score) / 2

        # Genre (0.15)
        g_score = genre_score(c.get("genre"), genre_weights)

        # Multi-source bonus (0.15)
        ms_score = 1.0 if n_sources >= 2 else 0.65

        # Recency (0.20)
        r_score = recency_score(c.get("release_date"))

        # Familiarity (0.10)
        f_score = familiarity_score(
            c.get("artist", ""),
            c.get("label"),
            top_artists,
            top_labels,
        )

        # Novelty (0.15)
        n_score = novelty_score(c["title"], c["artist"], published)

        composite = (
            0.25 * bpm_key_score
            + 0.15 * g_score
            + 0.15 * ms_score
            + 0.20 * r_score
            + 0.10 * f_score
            + 0.15 * n_score
        )

        scored.append({**c, "score": round(composite, 4)})

    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored


# ---------------------------------------------------------------------------
# Greedy nearest-neighbour Camelot ordering
# ---------------------------------------------------------------------------

def _best_next(current_key: str, remaining: list[dict]) -> int:
    best_idx, best_dist = 0, float("inf")
    for i, track in enumerate(remaining):
        key = track.get("camelot_key") or track.get("key_camelot") or ""
        dist = camelot_distance(current_key, key)
        if dist is None:
            dist = 6
        if dist < best_dist:
            best_dist, best_idx = dist, i
    return best_idx


def harmonic_sort(tracks: list[dict]) -> list[dict]:
    keyed = [t for t in tracks if t.get("camelot_key") or t.get("key_camelot")]
    keyless = [t for t in tracks if not (t.get("camelot_key") or t.get("key_camelot"))]
    if not keyed:
        return tracks
    ordered = [keyed.pop(0)]
    while keyed:
        current = ordered[-1].get("camelot_key") or ordered[-1].get("key_camelot") or ""
        ordered.append(keyed.pop(_best_next(current, keyed)))
    return ordered + keyless


# ---------------------------------------------------------------------------
# Top-level entry point
# ---------------------------------------------------------------------------

def _artist_key(track: dict) -> str:
    return track.get("artist", "").lower()


def _apply_artist_cap(scored: list[dict], final_count: int, max_per_artist: int = 2) -> list[dict]:
    """
    Greedily select up to final_count tracks, capping any single artist
    at max_per_artist entries. Candidates are already sorted by score desc.
    """
    counts: dict[str, int] = {}
    selected = []
    for track in scored:
        artist = _artist_key(track)
        if counts.get(artist, 0) < max_per_artist:
            selected.append(track)
            counts[artist] = counts.get(artist, 0) + 1
        if len(selected) >= final_count:
            break
    return selected


def _spread_artists(tracks: list[dict]) -> list[dict]:
    """
    Ensure no two adjacent tracks share the same artist.
    When a collision is found, swap the second track with the nearest
    non-colliding track later in the list.  Repeats until stable or
    no more swaps are possible.
    """
    result = list(tracks)
    for _ in range(len(result)):  # at most len passes
        swapped = False
        for i in range(len(result) - 1):
            if _artist_key(result[i]) == _artist_key(result[i + 1]):
                # Find nearest track after i+1 with a different artist
                for j in range(i + 2, len(result)):
                    if _artist_key(result[j]) != _artist_key(result[i]):
                        result[i + 1], result[j] = result[j], result[i + 1]
                        swapped = True
                        break
        if not swapped:
            break
    return result


def rank_and_filter(
    candidates: list[dict],
    taste_profile: dict,
    db_path: str,
    final_count: int = 20,
    max_per_artist: int = 2,
    exclude_recent_weeks: int = 4,
) -> list[dict]:
    """
    Score, enforce per-artist cap, harmonically sort, then spread artists.

    Tracks published in the last `exclude_recent_weeks` weeks are hard-excluded
    before scoring so the mix stays fresh week to week. Older repeated tracks
    still receive a soft novelty penalty in the score.
    """
    if not candidates:
        return []

    # Hard-exclude recently played tracks
    if exclude_recent_weeks > 0:
        recent = _get_recent_titles(db_path, exclude_recent_weeks)
        if recent:
            before = len(candidates)
            candidates = [
                c for c in candidates
                if (c["title"].lower(), c["artist"].lower()) not in recent
            ]
            excluded = before - len(candidates)
            if excluded:
                print(f"[ranker] Excluded {excluded} tracks played in the last {exclude_recent_weeks} weeks")

    scored = score_candidates(candidates, taste_profile, db_path)
    top = _apply_artist_cap(scored, final_count, max_per_artist)
    ordered = harmonic_sort(top)
    ordered = _spread_artists(ordered)
    for i, track in enumerate(ordered, start=1):
        track["rank"] = i
    return ordered
