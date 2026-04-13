"""
Multi-source recommendation orchestrator.

Runs all enabled recommendation sources (Spotify, Last.fm, Claude LLM),
merges the results, and deduplicates by normalised (title, artist).
"""

import spotipy
from spotipy.oauth2 import SpotifyClientCredentials

from modules.recommender_spotify import get_spotify_recommendations
from modules.recommender_lastfm import get_lastfm_recommendations
from modules.recommender_llm import get_llm_recommendations


def build_spotify_client(config: dict) -> spotipy.Spotify:
    return spotipy.Spotify(
        auth_manager=SpotifyClientCredentials(
            client_id=config["spotify"]["client_id"],
            client_secret=config["spotify"]["client_secret"],
        ),
        requests_timeout=10,
    )


def run_recommenders(
    config: dict,
    db_path: str,
    run_id: str,
    taste_profile: dict,
    per_source: int = 15,
) -> list[dict]:
    """
    Run all configured recommendation sources and return a merged,
    deduplicated candidate list. Each candidate dict contains at minimum:
      run_id, source, title, artist, spotify_id (may be None), reason.
    """
    all_candidates: list[dict] = []
    seen: set[tuple] = set()

    def _add(batch: list[dict]) -> None:
        for c in batch:
            key = (c["title"].lower(), c["artist"].lower())
            if key not in seen:
                seen.add(key)
                all_candidates.append(c)

    # --- Spotify ---
    spotify_cfg = config.get("spotify", {})
    if spotify_cfg.get("client_id") and spotify_cfg.get("client_secret"):
        print("[recommender] Running Spotify source…")
        try:
            sp = build_spotify_client(config)
            _add(
                get_spotify_recommendations(
                    sp, db_path, run_id, taste_profile, count=per_source
                )
            )
        except Exception as e:
            print(f"  [spotify] Source failed: {e}")
    else:
        print("[recommender] Spotify credentials not configured — skipping.")

    # --- Last.fm ---
    lastfm_key = config.get("lastfm", {}).get("api_key")
    if lastfm_key:
        print("[recommender] Running Last.fm source…")
        try:
            _add(
                get_lastfm_recommendations(
                    lastfm_key, db_path, run_id, count=per_source
                )
            )
        except Exception as e:
            print(f"  [lastfm] Source failed: {e}")
    else:
        print("[recommender] Last.fm API key not configured — skipping.")

    # --- Claude LLM ---
    anthropic_key = config.get("anthropic", {}).get("api_key")
    if anthropic_key:
        print("[recommender] Running Claude LLM source…")
        try:
            _add(
                get_llm_recommendations(
                    config, db_path, run_id, taste_profile, count=per_source
                )
            )
        except Exception as e:
            print(f"  [llm] Source failed: {e}")
    else:
        print("[recommender] Anthropic API key not configured — skipping.")

    print(f"[recommender] {len(all_candidates)} total candidates (merged, deduped)")
    return all_candidates
