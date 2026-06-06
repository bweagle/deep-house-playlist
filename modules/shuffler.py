"""
Shuffle the pinned playlists on YouTube and SoundCloud.

Run nightly so the "White Eagle Deephouse" playlist plays in a different
order every day without re-generating the mix.
"""

import random
from pathlib import Path


def shuffle_pinned_playlists(config: dict, verbose: bool = True) -> None:
    yt_pinned = config.get("youtube", {}).get("pinned_playlist_name")
    sc_pinned = (
        config.get("soundcloud", {}).get("pinned_playlist_name")
        or config.get("youtube", {}).get("pinned_playlist_name")
    )

    # YouTube
    auth_file = config.get("youtube", {}).get("oauth_file", "oauth.json")
    if yt_pinned and Path(auth_file).exists():
        from ytmusic_client import YTMusicClient
        yt = YTMusicClient(auth_file=auth_file)
        playlist_id = yt.find_playlist(yt_pinned)
        if playlist_id:
            video_ids = yt.get_playlist_video_ids(playlist_id)
            if video_ids:
                random.shuffle(video_ids)
                yt.clear_playlist(playlist_id)
                yt.add_to_playlist(playlist_id, video_ids)
                if verbose:
                    print(f"[shuffle] YouTube '{yt_pinned}' reshuffled ({len(video_ids)} tracks)")
            else:
                if verbose:
                    print(f"[shuffle] YouTube '{yt_pinned}' is empty — skipping")
        else:
            if verbose:
                print(f"[shuffle] YouTube playlist '{yt_pinned}' not found — skipping")

    # SoundCloud
    sc_token = config.get("soundcloud", {}).get("token_file", "soundcloud_token.json")
    if sc_pinned and Path(sc_token).exists():
        from soundcloud_client import SoundCloudClient
        sc = SoundCloudClient(token_file=sc_token)
        playlist_id = sc.find_playlist(sc_pinned)
        if playlist_id:
            track_ids = sc.get_playlist_track_ids(playlist_id)
            if track_ids:
                random.shuffle(track_ids)
                sc.update_playlist(playlist_id, track_ids)
                if verbose:
                    print(f"[shuffle] SoundCloud '{sc_pinned}' reshuffled ({len(track_ids)} tracks)")
            else:
                if verbose:
                    print(f"[shuffle] SoundCloud playlist '{sc_pinned}' is empty — skipping")
        else:
            if verbose:
                print(f"[shuffle] SoundCloud playlist '{sc_pinned}' not found — skipping")
