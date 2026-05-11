"""
Catalog Analyzer — Phase 1 of the pipeline.

Loads songs.csv into the database, matches tracks to Beatport for
enrichment (BPM, key, genre, label), and computes a taste profile
from the highest-rated songs.

CLI: python3 app.py analyze
"""

import json
import math
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

# songs.py lives one directory up
sys.path.insert(0, str(Path(__file__).parent.parent))
from songs import Song, load_songs
from modules.db import get_connection, init_db
from modules.beatport_client import BeatportClient


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
            ON CONFLICT(title, artist) DO NOTHING
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
        count += conn.execute("SELECT changes()").fetchone()[0]
    conn.commit()
    conn.close()
    return count


def match_catalog_to_beatport(bp: BeatportClient, db_path: str) -> tuple[int, int]:
    """
    Match unmatched catalog rows to Beatport.
    Updates bpm, key_camelot, genre, label, beatport_id, beatport_artist_ids.
    Returns (matched, skipped).
    """
    import sqlite3

    conn = get_connection(db_path)
    rows = conn.execute(
        """
        SELECT id, title, artist FROM catalog
        WHERE beatport_id IS NULL
          AND (beatport_searched_at IS NULL
               OR beatport_searched_at < datetime('now', '-30 days'))
        """
    ).fetchall()

    matched = skipped = 0
    consecutive_errors = 0
    now = datetime.now(timezone.utc).isoformat()

    try:
        for row in rows:
            try:
                track = bp.search_track(row["title"], row["artist"])
                consecutive_errors = 0  # reset on success
            except Exception as e:
                consecutive_errors += 1
                print(f"  [beatport] Search error for '{row['title']}': {e}")
                # Hard stop on auth errors
                if hasattr(e, "response") and getattr(e.response, "status_code", 0) in (401, 403):
                    print("  Beatport auth failed — re-run setup_beatport_auth.py.")
                    return matched, skipped + len(rows)
                # Stop after 3 consecutive errors — something is wrong
                if consecutive_errors >= 3:
                    print(f"  Stopping Beatport matching after {consecutive_errors} consecutive errors.")
                    return matched, skipped + len(rows)
                skipped += 1
                continue

            if track:
                artist_ids_json = json.dumps(track.get("artist_ids") or [])
                try:
                    conn.execute(
                        """
                        UPDATE catalog SET
                            beatport_id = ?,
                            beatport_artist_ids = ?,
                            bpm = COALESCE(bpm, ?),
                            key_camelot = COALESCE(key_camelot, ?),
                            genre = ?,
                            sub_genre = ?,
                            label = ?,
                            release_date = ?,
                            fetched_at = ?,
                            beatport_searched_at = ?
                        WHERE id = ?
                        """,
                        (
                            track["beatport_id"],
                            artist_ids_json,
                            track.get("bpm"),
                            track.get("camelot_key"),
                            track.get("genre"),
                            track.get("sub_genre"),
                            track.get("label"),
                            track.get("release_date"),
                            now,
                            now,
                            row["id"],
                        ),
                    )
                    matched += 1
                except sqlite3.IntegrityError:
                    # Two catalog tracks matched the same Beatport ID — skip this one
                    conn.execute(
                        "UPDATE catalog SET beatport_searched_at = ? WHERE id = ?",
                        (now, row["id"]),
                    )
                    skipped += 1
            else:
                # Not found — stamp so we don't retry for 30 days
                conn.execute(
                    "UPDATE catalog SET beatport_searched_at = ? WHERE id = ?",
                    (now, row["id"]),
                )
                skipped += 1
    finally:
        conn.commit()
        conn.close()

    return matched, skipped


# ---------------------------------------------------------------------------
# Taste profile
# ---------------------------------------------------------------------------

def _weighted_avg(values_weights: list[tuple]) -> float | None:
    pairs = [(v, w) for v, w in values_weights if v is not None]
    if not pairs:
        return None
    total = sum(w for _, w in pairs)
    return sum(v * w for v, w in pairs) / total


def _weighted_std(values_weights: list[tuple], mean: float) -> float | None:
    pairs = [(v, w) for v, w in values_weights if v is not None]
    if len(pairs) < 2:
        return None
    total = sum(w for _, w in pairs)
    variance = sum(w * (v - mean) ** 2 for v, w in pairs) / total
    return math.sqrt(variance)


def compute_taste_profile(db_path: str, min_rating: int = 3) -> dict:
    """
    Compute a taste profile from songs with numeric rating >= min_rating.

    Uses Beatport metadata (BPM, key, genre, label) when available,
    falls back to CSV bpm/key for unmatched tracks.
    Returns the profile dict and writes it to the taste_profile table.
    """
    conn = get_connection(db_path)
    rows = conn.execute(
        """
        SELECT bpm, key_camelot, rating, genre, label, artist
        FROM catalog
        WHERE CAST(COALESCE(NULLIF(rating, ''), '0') AS INTEGER) >= ?
        """,
        (min_rating,),
    ).fetchall()
    conn.close()

    if not rows:
        raise ValueError(f"No songs found with rating >= {min_rating}")

    bpms: list[tuple] = []
    key_counts: Counter = Counter()
    genre_counts: Counter = Counter()
    label_counts: Counter = Counter()
    artist_counts: Counter = Counter()

    for row in rows:
        try:
            weight = int(row["rating"])
        except (TypeError, ValueError):
            weight = 1

        if row["bpm"]:
            bpms.append((float(row["bpm"]), weight))

        if row["key_camelot"]:
            key_counts[row["key_camelot"]] += weight

        if row["genre"]:
            genre_counts[row["genre"]] += weight

        if row["label"]:
            label_counts[row["label"]] += weight

        if row["artist"]:
            for a in row["artist"].split(","):
                a = a.strip()
                if a:
                    artist_counts[a] += weight

    avg_bpm = _weighted_avg(bpms)
    bpm_std = _weighted_std(bpms, avg_bpm) if avg_bpm else None

    top_keys = [k for k, _ in key_counts.most_common(5)]

    # Genre weights as proportions
    total_genre = sum(genre_counts.values()) or 1
    genre_weights = {g: round(c / total_genre, 3) for g, c in genre_counts.most_common(8)}

    top_labels = [l for l, _ in label_counts.most_common(10)]
    top_artists = [a for a, _ in artist_counts.most_common(20)]

    profile = {
        "avg_bpm": avg_bpm,
        "bpm_std": bpm_std,
        "top_keys": top_keys,
        "genre_weights": genre_weights,
        "top_labels": top_labels,
        "top_artists": top_artists,
        "computed_at": datetime.now(timezone.utc).isoformat(),
    }

    conn = get_connection(db_path)
    conn.execute(
        """
        INSERT OR REPLACE INTO taste_profile
            (id, avg_bpm, bpm_std, top_keys, genre_weights, top_labels, top_artists, computed_at)
        VALUES (1, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            profile["avg_bpm"],
            profile["bpm_std"],
            json.dumps(profile["top_keys"]),
            json.dumps(profile["genre_weights"]),
            json.dumps(profile["top_labels"]),
            json.dumps(profile["top_artists"]),
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
        raise ValueError("No taste profile found. Run `python3 app.py analyze` first.")
    profile = dict(row)
    for key in ("top_keys", "genre_weights", "top_labels", "top_artists"):
        if profile.get(key):
            profile[key] = json.loads(profile[key])
    return profile


# ---------------------------------------------------------------------------
# Top-level entry point
# ---------------------------------------------------------------------------

def run_analyze(config: dict, verbose: bool = True, limit: int | None = None) -> dict:
    """
    Full analyze pipeline:
      1. Load CSV into DB
      2. Match to Beatport (if token available)
      3. Resolve YouTube video IDs (if OAuth file available)
      4. Compute taste profile
    """
    db_path = config["pipeline"]["database"]
    csv_path = config["pipeline"]["catalog_csv"]
    min_rating = config["pipeline"].get("min_rating_for_taste_profile", 3)

    init_db(db_path)

    # 1. Load CSV
    songs = load_songs(csv_path)
    if limit:
        songs = songs[:limit]
    inserted = load_catalog_to_db(songs, db_path)
    if verbose:
        total_msg = f" (limited to {limit})" if limit else ""
        print(f"Catalog: {len(songs)} songs loaded ({inserted} new rows){total_msg}")

    # 2. Match to Beatport (optional — skip if no token)
    beatport_token = config.get("beatport", {}).get("token_file", "beatport_token.json")
    if Path(beatport_token).exists():
        try:
            bp = BeatportClient(token_file=beatport_token)
            matched, skipped = match_catalog_to_beatport(bp, db_path)
            if verbose:
                print(f"Beatport match: {matched} matched, {skipped} not found")
        except FileNotFoundError:
            if verbose:
                print("Beatport token not found — skipping enrichment.")
        except Exception as e:
            if verbose:
                print(f"Beatport matching failed: {e} — skipping enrichment.")
    else:
        if verbose:
            print("No Beatport token — using CSV data only for taste profile.")

    # 3. Resolve YouTube video IDs — skip in CI (only needed for mixtape, run locally)
    import os
    auth_file = config.get("youtube", {}).get("oauth_file", "oauth.json")
    if os.environ.get("CI"):
        if verbose:
            print("CI environment — skipping YouTube ID resolution (run `analyze` locally for mixtape).")
    elif Path(auth_file).exists():
        try:
            from modules.library_sync import sync_youtube
            if verbose:
                print()
            sync_youtube(db_path, auth_file=auth_file, verbose=verbose)
        except Exception as e:
            if verbose:
                print(f"YouTube sync failed: {e} — skipping.")
    else:
        if verbose:
            print("No YouTube OAuth file — skipping YouTube ID resolution.")

    # 4. Taste profile
    profile = compute_taste_profile(db_path, min_rating=min_rating)
    if verbose:
        print(f"\nTaste profile (from songs rated {min_rating}+):")
        if profile["avg_bpm"]:
            std = profile.get("bpm_std") or 0
            print(f"  BPM:              {profile['avg_bpm']:.1f} ± {std:.1f}")
        if profile["top_keys"]:
            print(f"  Top Camelot keys: {', '.join(profile['top_keys'])}")
        if profile.get("genre_weights"):
            top_genres = list(profile["genre_weights"].items())[:4]
            genre_str = ", ".join(f"{g} ({w:.0%})" for g, w in top_genres)
            print(f"  Top genres:       {genre_str}")
        if profile.get("top_labels"):
            print(f"  Top labels:       {', '.join(profile['top_labels'][:5])}")

    return profile
