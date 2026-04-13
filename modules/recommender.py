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

    When only the LLM source is active, its count is scaled up to
    compensate for the missing sources.
    """
    all_candidates: list[dict] = []
    seen: set[tuple] = set()

    def _add(batch: list[dict]) -> None:
        for c in batch:
            key = (c["title"].lower(), c["artist"].lower())
            if key not in seen:
                seen.add(key)
                all_candidates.append(c)

    spotify_cfg = config.get("spotify", {})
    spotify_enabled = bool(spotify_cfg.get("client_id") and spotify_cfg.get("client_secret"))
    lastfm_enabled = bool(config.get("lastfm", {}).get("api_key"))
    llm_enabled = bool(config.get("anthropic", {}).get("api_key"))

    # Scale up LLM count when it's carrying more (or all) of the load
    active_sources = sum([spotify_enabled, lastfm_enabled, llm_enabled])
    llm_count = per_source * max(1, (3 // active_sources)) if active_sources else per_source

    # --- Spotify ---
    if spotify_enabled:
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
        print("[recommender] Spotify not configured — skipping.")

    # --- Last.fm ---
    if lastfm_enabled:
        print("[recommender] Running Last.fm source…")
        try:
            _add(
                get_lastfm_recommendations(
                    config["lastfm"]["api_key"], db_path, run_id, count=per_source
                )
            )
        except Exception as e:
            print(f"  [lastfm] Source failed: {e}")
    else:
        print("[recommender] Last.fm not configured — skipping.")

    # --- Claude LLM ---
    if llm_enabled:
        print(f"[recommender] Running Claude LLM source (count={llm_count})…")
        try:
            _add(
                get_llm_recommendations(
                    config, db_path, run_id, taste_profile, count=llm_count
                )
            )
        except Exception as e:
            print(f"  [llm] Source failed: {e}")
    else:
        print("[recommender] Anthropic API key not configured — skipping.")

    print(f"[recommender] {len(all_candidates)} total candidates (merged, deduped)")
    return all_candidates
