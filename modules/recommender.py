"""
Multi-source recommendation orchestrator.

Runs all enabled recommendation sources (Beatport, Last.fm, Claude LLM),
merges the results, and deduplicates by normalised (title, artist).
"""

from pathlib import Path

from modules.recommender_lastfm import get_lastfm_recommendations
from modules.recommender_llm import get_llm_recommendations


def run_recommenders(
    config: dict,
    db_path: str,
    run_id: str,
    taste_profile: dict,
    per_source: int = 15,
) -> list[dict]:
    """
    Run all configured recommendation sources and return a merged,
    deduplicated candidate list.

    When only the LLM source is active, its count is scaled up to
    compensate for the missing sources.
    """
    all_candidates: list[dict] = []
    seen: set[tuple] = set()

    def _add(batch: list[dict]) -> None:
        for c in batch:
            key = (" ".join(c["title"].lower().split()), " ".join(c["artist"].lower().split()))
            if key not in seen:
                seen.add(key)
                all_candidates.append(c)

    beatport_token = config.get("beatport", {}).get(
        "token_file", "beatport_token.json"
    )
    beatport_enabled = Path(beatport_token).exists()
    lastfm_enabled = bool(config.get("lastfm", {}).get("api_key"))
    llm_enabled = bool(config.get("anthropic", {}).get("api_key"))

    active_sources = sum([beatport_enabled, lastfm_enabled, llm_enabled])
    llm_count = per_source * max(1, (3 // active_sources)) if active_sources else per_source

    # Instantiate Beatport client once — shared by all three sources
    bp = None
    if beatport_enabled:
        try:
            from modules.beatport_client import BeatportClient
            bp = BeatportClient(token_file=beatport_token)
        except Exception as e:
            print(f"[recommender] Could not load Beatport client: {e}")

    # --- Beatport ---
    # Beatport gets 2× the per-source cap — it's the primary chart source and
    # pulling a larger pool gives the ranker more real, verified candidates.
    if bp is not None:
        from modules.recommender_beatport import get_beatport_recommendations
        print("[recommender] Running Beatport source…")
        try:
            _add(get_beatport_recommendations(bp, db_path, run_id, count=per_source * 2))
        except Exception as e:
            print(f"  [beatport] Source failed: {e}")
    else:
        print("[recommender] No Beatport token — skipping. Run setup_beatport_auth.py to enable.")

    # --- Last.fm ---
    if lastfm_enabled:
        print("[recommender] Running Last.fm source…")
        try:
            _add(
                get_lastfm_recommendations(
                    config["lastfm"]["api_key"], db_path, run_id, count=per_source, bp=bp
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
                    config, db_path, run_id, taste_profile, count=llm_count, bp=bp
                )
            )
        except Exception as e:
            print(f"  [llm] Source failed: {e}")
    else:
        print("[recommender] Anthropic API key not configured — skipping.")

    print(f"[recommender] {len(all_candidates)} total candidates (merged, deduped)")
    return all_candidates
