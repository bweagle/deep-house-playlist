"""
Filter, score, and sort candidates for the weekly mix.

Scoring uses:
  1. Camelot wheel compatibility with the taste profile's top keys
  2. Cosine similarity of audio features against the taste profile
     (when Spotify features are available on the candidate)

Final ordering uses a greedy nearest-neighbour walk on the Camelot wheel
to produce a harmonically mixed tracklist.
"""

import math
from modules.db import get_connection

# ---------------------------------------------------------------------------
# Camelot wheel — keys use m/d suffix (minor/dominant = minor/major)
# ---------------------------------------------------------------------------

# Adjacent keys on the wheel (same number ± 1, and opposite mode)
# Distance 0 = same key, 1 = adjacent, 2 = two steps, etc.
# We compute a compatibility score 0–1 from distance.

_CAMELOT_NUMBERS = list(range(1, 13))  # 1..12


def _parse_camelot(key: str) -> tuple[int, str] | None:
    """Parse '10m', '12d', etc. → (number, mode). Returns None on failure."""
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
    """
    Returns the minimum step distance between two Camelot keys.
    Same key → 0.  Wrap-around (1→12) counts as 1 step.
    Returns None if either key is unparseable.
    """
    a = _parse_camelot(key_a)
    b = _parse_camelot(key_b)
    if a is None or b is None:
        return None

    num_a, mode_a = a
    num_b, mode_b = b

    if mode_a != mode_b:
        # Crossing modes costs 1 extra step (same number, different mode)
        mode_penalty = 1
    else:
        mode_penalty = 0

    # Circular distance on 1..12
    diff = abs(num_a - num_b)
    circular = min(diff, 12 - diff)
    return circular + mode_penalty


def camelot_score(candidate_key: str, top_keys: list[str]) -> float:
    """
    Score 0–1 for how well candidate_key fits the listener's top keys.
    Uses the minimum distance to any of the top_keys.
    """
    if not top_keys or not candidate_key:
        return 0.5  # neutral when no data

    distances = [
        camelot_distance(candidate_key, k)
        for k in top_keys
        if camelot_distance(candidate_key, k) is not None
    ]
    if not distances:
        return 0.5

    min_dist = min(distances)
    # Distance 0 → 1.0, distance 1 → 0.8, 2 → 0.5, 3+ → 0.2
    score_map = {0: 1.0, 1: 0.8, 2: 0.5}
    return score_map.get(min_dist, 0.2)


# ---------------------------------------------------------------------------
# Feature cosine similarity
# ---------------------------------------------------------------------------

def _feature_vector(d: dict, keys: list[str]) -> list[float]:
    return [float(d.get(k) or 0.0) for k in keys]


def cosine_similarity(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    mag_a = math.sqrt(sum(x * x for x in a))
    mag_b = math.sqrt(sum(y * y for y in b))
    if mag_a == 0 or mag_b == 0:
        return 0.0
    return dot / (mag_a * mag_b)


_FEATURE_KEYS = ["avg_energy", "avg_danceability", "avg_valence",
                 "avg_acousticness", "avg_instrumentalness"]
_CANDIDATE_FEATURE_KEYS = ["energy", "danceability", "valence",
                            "acousticness", "instrumentalness"]


def feature_score(candidate_features: dict | None, taste_profile: dict) -> float:
    """Cosine similarity between candidate and taste profile feature vectors."""
    if not candidate_features:
        return 0.5  # neutral

    profile_vec = _feature_vector(taste_profile, _FEATURE_KEYS)
    cand_vec = _feature_vector(candidate_features, _CANDIDATE_FEATURE_KEYS)

    if all(v == 0.0 for v in profile_vec):
        return 0.5

    sim = cosine_similarity(profile_vec, cand_vec)
    # Shift from [-1,1] to [0,1]
    return (sim + 1.0) / 2.0


# ---------------------------------------------------------------------------
# Fetch Spotify features for candidates
# ---------------------------------------------------------------------------

def _fetch_candidate_features(spotify_ids: list[str], db_path: str) -> dict[str, dict]:
    """Load audio features for the given IDs from the local DB."""
    if not spotify_ids:
        return {}
    conn = get_connection(db_path)
    placeholders = ",".join("?" * len(spotify_ids))
    rows = conn.execute(
        f"SELECT * FROM audio_features WHERE spotify_id IN ({placeholders})",
        spotify_ids,
    ).fetchall()
    conn.close()
    return {r["spotify_id"]: dict(r) for r in rows}


# ---------------------------------------------------------------------------
# Score all candidates
# ---------------------------------------------------------------------------

def score_candidates(
    candidates: list[dict],
    taste_profile: dict,
    db_path: str,
) -> list[dict]:
    """
    Attach a composite `score` to each candidate.
    Weights: Camelot 40%, feature similarity 40%, source diversity bonus 20%.
    """
    # Gather Spotify IDs for feature lookup
    spotify_ids = [c["spotify_id"] for c in candidates if c.get("spotify_id")]
    features_map = _fetch_candidate_features(spotify_ids, db_path)

    top_keys = taste_profile.get("top_keys") or []

    source_counts: dict[str, int] = {}
    for c in candidates:
        source_counts[c["source"]] = source_counts.get(c["source"], 0) + 1

    scored = []
    for c in candidates:
        ckey = c.get("camelot_key") or c.get("key_camelot") or ""
        c_score = camelot_score(ckey, top_keys)

        feat = features_map.get(c.get("spotify_id", ""))
        f_score = feature_score(feat, taste_profile)

        # Diversity bonus: favour sources that appear less often
        n_src = source_counts.get(c["source"], 1)
        d_score = 1.0 / n_src

        composite = 0.4 * c_score + 0.4 * f_score + 0.2 * d_score

        scored.append({**c, "score": round(composite, 4)})

    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored


# ---------------------------------------------------------------------------
# Greedy nearest-neighbour Camelot ordering
# ---------------------------------------------------------------------------

def _best_next(current_key: str, remaining: list[dict]) -> int:
    """Index of the track in `remaining` closest to current_key on Camelot wheel."""
    best_idx = 0
    best_dist = float("inf")
    for i, track in enumerate(remaining):
        key = track.get("camelot_key") or track.get("key_camelot") or ""
        dist = camelot_distance(current_key, key)
        if dist is None:
            dist = 6  # unknown: treat as far
        if dist < best_dist:
            best_dist = dist
            best_idx = i
    return best_idx


def harmonic_sort(tracks: list[dict]) -> list[dict]:
    """
    Reorder tracks using greedy nearest-neighbour walk on Camelot wheel.
    Tracks without a key are appended at the end.
    """
    keyed = [t for t in tracks if t.get("camelot_key") or t.get("key_camelot")]
    keyless = [t for t in tracks if not (t.get("camelot_key") or t.get("key_camelot"))]

    if not keyed:
        return tracks

    ordered = [keyed.pop(0)]
    while keyed:
        current = ordered[-1].get("camelot_key") or ordered[-1].get("key_camelot") or ""
        idx = _best_next(current, keyed)
        ordered.append(keyed.pop(idx))

    return ordered + keyless


# ---------------------------------------------------------------------------
# Top-level entry point
# ---------------------------------------------------------------------------

def rank_and_filter(
    candidates: list[dict],
    taste_profile: dict,
    db_path: str,
    final_count: int = 20,
) -> list[dict]:
    """
    Score, trim to final_count, then harmonically sort.
    Returns ranked tracks with `rank` and `score` fields set.
    """
    if not candidates:
        return []

    scored = score_candidates(candidates, taste_profile, db_path)
    top = scored[:final_count]
    ordered = harmonic_sort(top)

    for i, track in enumerate(ordered, start=1):
        track["rank"] = i

    return ordered
