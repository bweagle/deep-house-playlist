"""
Catalog Analyzer — Phase 1 of the pipeline.

Loads songs.csv into the database, matches tracks to Spotify for
enrichment, and computes a taste profile from your highest-rated songs.

CLI: python app.py analyze
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import spotipy
from spotipy.oauth2 import SpotifyClientCredentials

# songs.py lives one directory up
sys.path.insert(0, str(Path(__file__).parent.parent))
from songs import Song, load_songs, filter_songs
from modules.db import get_connection, init_db


# ---------------------------------------------------------------------------
# Spotify helpers
# ---------------------------------------------------------------------------

def build_spotify_client(config: dict) -> spotipy.Spotify:
    return spotipy.Spotify(
        auth_manager=SpotifyClientCredentials(
            client_id=config["spotify"]["client_id"],
            client_secret=config["spotify"]["client_secret"],
        ),
        requests_timeout=10,
    )


def match_song_to_spotify(sp: spotipy.Spotify, song: Song) -> str | None:
    """Search Spotify for a track and return its ID, or None."""
    query = f"track:{song.title}"
    if song.artist:
        query += f" artist:{song.artist}"
    try:
        results = sp.search(q=query, type="track", limit=1, market="US")
        items = results.get("tracks", {}).get("items", [])
        if items:
            return items[0]["id"]
    except spotipy.exceptions.SpotifyException:
        pass
    return None


def fetch_audio_features_safe(
    sp: spotipy.Spotify, spotify_ids: list[str]
) -> dict[str, dict]:
    """
    Fetch audio features in batches of 50.
    Returns empty dict silently if the endpoint is unavailable (deprecated).
    """
    features: dict[str, dict] = {}
    for i in range(0, len(spotify_ids), 50):
        batch = spotify_ids[i : i + 50]
        try:
            result = sp.audio_features(batch) or []
            for f in result:
                if f:
                    features[f["id"]] = f
        except spotipy.exceptions.SpotifyException as e:
            if e.http_status in (403, 404):
                break  # endpoint deprecated — fall back to CSV data
            raise
    return features


# ---------------------------------------------------------------------------
# Database operations
# ---------------------------------------------------------------------------

def load_catalog_to_db(songs: list[Song], db_path: str) -> int:
    """Insert/update songs into the catalog table. Returns count upserted."""
    conn = get_connection(db_path)
    count = 0
    for song in songs:
        conn.execute(
            """
            INSERT INTO catalog (title, artist, bpm, key_camelot, duration, rating, notes, local)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT DO NOTHING
            """,
            (
                song.title,
                song.artist,
                song.bpm,
                song.key,
                song.duration,
                song.rating,
                song.notes,
                song.local,
            ),
        )
        count += conn.execute(
            "SELECT changes()"
        ).fetchone()[0]
    conn.commit()
    conn.close()
    return count


def match_catalog_to_spotify(sp: spotipy.Spotify, db_path: str) -> tuple[int, int]:
    """Match unmatched catalog rows to Spotify. Returns (matched, skipped)."""
    conn = get_connection(db_path)
    rows = conn.execute(
        "SELECT id, title, artist FROM catalog WHERE spotify_id IS NULL"
    ).fetchall()

    matched = skipped = 0
    now = datetime.now(timezone.utc).isoformat()

    for row in rows:
        # Build a minimal Song for the helper
        song = Song(
            rating="",
            title=row["title"],
            artist=row["artist"] or "",
            bpm=None,
            key=None,
            duration=None,
            notes=None,
            local=None,
        )
        spotify_id = match_song_to_spotify(sp, song)
        if spotify_id:
            conn.execute(
                "UPDATE catalog SET spotify_id = ?, fetched_at = ? WHERE id = ?",
                (spotify_id, now, row["id"]),
            )
            matched += 1
        else:
            skipped += 1

    conn.commit()
    conn.close()
    return matched, skipped


def store_audio_features(features: dict[str, dict], db_path: str) -> int:
    """Upsert audio features into the database. Returns count stored."""
    conn = get_connection(db_path)
    now = datetime.now(timezone.utc).isoformat()
    count = 0
    for spotify_id, f in features.items():
        conn.execute(
            """
            INSERT OR REPLACE INTO audio_features
                (spotify_id, danceability, energy, valence, tempo, loudness,
                 acousticness, instrumentalness, speechiness,
                 spotify_key, mode, time_signature, fetched_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                spotify_id,
                f.get("danceability"),
                f.get("energy"),
                f.get("valence"),
                f.get("tempo"),
                f.get("loudness"),
                f.get("acousticness"),
                f.get("instrumentalness"),
                f.get("speechiness"),
                f.get("key"),
                f.get("mode"),
                f.get("time_signature"),
                now,
            ),
        )
        count += 1
    conn.commit()
    conn.close()
    return count


# ---------------------------------------------------------------------------
# Taste profile
# ---------------------------------------------------------------------------

def compute_taste_profile(db_path: str, min_rating: int = 3) -> dict:
    """
    Compute a taste profile from songs with numeric rating >= min_rating.

    Uses Spotify audio features when available, falls back to CSV bpm/key.
    Returns the profile dict and writes it to the taste_profile table.
    """
    conn = get_connection(db_path)

    # Pull rated songs with any available data
    rows = conn.execute(
        """
        SELECT c.bpm, c.key_camelot, c.rating,
               af.energy, af.danceability, af.valence,
               af.acousticness, af.instrumentalness
        FROM catalog c
        LEFT JOIN audio_features af ON c.spotify_id = af.spotify_id
        WHERE CAST(c.rating AS INTEGER) >= ?
        """,
        (min_rating,),
    ).fetchall()

    if not rows:
        conn.close()
        raise ValueError(f"No songs found with rating >= {min_rating}")

    # Aggregate — weight higher-rated songs more heavily
    def weighted_avg(values_weights: list[tuple]) -> float | None:
        pairs = [(v, w) for v, w in values_weights if v is not None]
        if not pairs:
            return None
        total_weight = sum(w for _, w in pairs)
        return sum(v * w for v, w in pairs) / total_weight

    key_counts: dict[str, int] = {}
    bpms, energies, danceabilities, valences, acousticnesses, instrumentalnesses = (
        [], [], [], [], [], []
    )

    for row in rows:
        try:
            weight = int(row["rating"])
        except (TypeError, ValueError):
            weight = 1

        bpm = row["bpm"]
        if bpm:
            bpms.append((bpm, weight))

        key = row["key_camelot"]
        if key:
            key_counts[key] = key_counts.get(key, 0) + weight

        for lst, col in [
            (energies, "energy"),
            (danceabilities, "danceability"),
            (valences, "valence"),
            (acousticnesses, "acousticness"),
            (instrumentalnesses, "instrumentalness"),
        ]:
            val = row[col]
            if val is not None:
                lst.append((val, weight))

    top_keys = sorted(key_counts, key=key_counts.get, reverse=True)[:5]

    profile = {
        "avg_bpm": weighted_avg(bpms),
        "avg_energy": weighted_avg(energies),
        "avg_danceability": weighted_avg(danceabilities),
        "avg_valence": weighted_avg(valences),
        "avg_acousticness": weighted_avg(acousticnesses),
        "avg_instrumentalness": weighted_avg(instrumentalnesses),
        "top_keys": top_keys,
        "computed_at": datetime.now(timezone.utc).isoformat(),
    }

    conn.execute(
        """
        INSERT OR REPLACE INTO taste_profile
            (id, avg_bpm, avg_energy, avg_danceability, avg_valence,
             avg_acousticness, avg_instrumentalness, top_keys, computed_at)
        VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            profile["avg_bpm"],
            profile["avg_energy"],
            profile["avg_danceability"],
            profile["avg_valence"],
            profile["avg_acousticness"],
            profile["avg_instrumentalness"],
            json.dumps(profile["top_keys"]),
            profile["computed_at"],
        ),
    )
    conn.commit()
    conn.close()
    return profile


def load_taste_profile(db_path: str) -> dict:
    """Load the most recently computed taste profile from the database."""
    conn = get_connection(db_path)
    row = conn.execute("SELECT * FROM taste_profile WHERE id = 1").fetchone()
    conn.close()
    if not row:
        raise ValueError("No taste profile found. Run `python app.py analyze` first.")
    profile = dict(row)
    if profile.get("top_keys"):
        profile["top_keys"] = json.loads(profile["top_keys"])
    return profile


# ---------------------------------------------------------------------------
# Top-level entry point
# ---------------------------------------------------------------------------

def run_analyze(config: dict, verbose: bool = True) -> dict:
    """
    Full analyze pipeline:
      1. Load CSV into DB
      2. Match to Spotify
      3. Fetch audio features
      4. Compute taste profile
    """
    db_path = config["pipeline"]["database"]
    csv_path = config["pipeline"]["catalog_csv"]
    min_rating = config["pipeline"].get("min_rating_for_taste_profile", 3)

    init_db(db_path)

    # 1. Load CSV
    songs = load_songs(csv_path)
    inserted = load_catalog_to_db(songs, db_path)
    if verbose:
        print(f"Catalog: {len(songs)} songs loaded ({inserted} new rows)")

    # 2. Match to Spotify
    sp = build_spotify_client(config)
    matched, skipped = match_catalog_to_spotify(sp, db_path)
    if verbose:
        print(f"Spotify match: {matched} matched, {skipped} not found")

    # 3. Fetch audio features for matched songs
    conn = get_connection(db_path)
    ids = [
        r["spotify_id"]
        for r in conn.execute(
            "SELECT spotify_id FROM catalog WHERE spotify_id IS NOT NULL"
        ).fetchall()
    ]
    conn.close()

    features = fetch_audio_features_safe(sp, ids)
    stored = store_audio_features(features, db_path)
    if verbose:
        if features:
            print(f"Audio features: {stored} tracks enriched")
        else:
            print("Audio features: endpoint unavailable — using CSV bpm/key for taste profile")

    # 4. Taste profile
    profile = compute_taste_profile(db_path, min_rating=min_rating)
    if verbose:
        print(f"\nTaste profile (from songs rated {min_rating}+):")
        if profile["avg_bpm"]:
            print(f"  BPM:             {profile['avg_bpm']:.1f}")
        if profile["avg_energy"]:
            print(f"  Energy:          {profile['avg_energy']:.2f}")
        if profile["avg_danceability"]:
            print(f"  Danceability:    {profile['avg_danceability']:.2f}")
        print(f"  Top Camelot keys: {', '.join(profile['top_keys'])}")

    return profile
