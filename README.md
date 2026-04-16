# Weekly Deep House Mix

Automatically generates a weekly deep house playlist on YouTube Music based on your personal music catalog. Every Sunday, the pipeline analyzes your taste profile, pulls fresh tracks from Beatport charts and similar-artist recommendations, scores them against your preferences, and publishes a harmonically-mixed 20-track playlist — plus updates a pinned "My Weekly Mix" playlist you can always play on demand.

## How it works

1. **Analyze** — Loads your catalog CSV, matches tracks to Beatport to enrich them with BPM, Camelot key, genre, and label, then computes a taste profile from your highest-rated songs.

2. **Recommend** — Pulls candidates from three sources:
   - **Beatport** — Top tracks from Deep House, Melodic House & Techno, and Organic House charts (last 3 months)
   - **Last.fm** — Top tracks by artists similar to those in your catalog
   - **Claude AI** — Suggests artists to explore based on your taste profile, then finds real tracks on Beatport

3. **Rank** — Scores every candidate against your taste profile (BPM fit, Camelot key compatibility, genre match, recency, familiarity, novelty). Tracks from the last 4 weeks are excluded to keep mixes fresh. Final tracklist is ordered using Camelot wheel harmonic mixing.

4. **Publish** — Searches YouTube for each track, creates a dated archive playlist (e.g. `Weekly Deep House Mix 2026-W16`), and updates a pinned `My Weekly Mix` playlist.

Runs automatically every Sunday via GitHub Actions.

---

## Prerequisites

- Python 3.11+
- A [Beatport](https://www.beatport.com) account
- A [Last.fm](https://www.last.fm) account (optional but recommended)
- An [Anthropic API key](https://console.anthropic.com) (optional but recommended)
- A Google account with YouTube Data API v3 enabled

---

## Setup

### 1. Clone and install

```bash
git clone https://github.com/bweagle/deep-house-playlist.git
cd deep-house-playlist
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 2. Create your music catalog

Copy `songs.example.csv` to `songs.csv` and fill it in with your tracks:

```
rating, Title, Artist, BPM, Key, Duration, Notes, Local
```

| Column | Values | Notes |
|--------|--------|-------|
| rating | blank, `X`, or `1`–`5` | Numeric ratings are used to build your taste profile. `X` = backlog. |
| Title | track title | Required |
| Artist | artist name | |
| BPM | e.g. `124` | Optional — Beatport fills this in if missing |
| Key | Camelot notation e.g. `8m`, `10d` | Optional — Beatport fills this in if missing |
| Duration | `H:MM:SS` | Optional |
| Notes | free text | Optional |
| Local | date or flag | Optional |

The more songs you have with numeric ratings (1–5), the better your taste profile will be.

### 3. Set up Beatport authentication

First, find your Beatport OAuth `client_id`:
1. Open `https://api.beatport.com/v4/docs/` in your browser
2. Open DevTools → Sources and search JS bundles for `CLIENT_ID` or `clientId`
3. Look for a ~40-character alphanumeric string near OAuth references

Create a `.env` file in the project root:

```
BEATPORT_CLIENT_ID=your_client_id_here
```

Then authenticate:

```bash
python3 setup_beatport_auth.py
```

This saves a `beatport_token.json` locally. Tokens refresh automatically.

### 4. Set up YouTube authentication

Enable the [YouTube Data API v3](https://console.developers.google.com) in Google Cloud Console, create OAuth 2.0 credentials (Desktop app), and download the credentials JSON. Then:

```bash
python3 setup_auth.py
```

This saves an `oauth.json` locally.

### 5. Configure the app

```bash
cp config.example.yaml config.yaml
```

Edit `config.yaml` and fill in your API keys:

```yaml
beatport:
  token_file: "beatport_token.json"

lastfm:
  api_key: "YOUR_LASTFM_API_KEY"     # get one free at last.fm/api

anthropic:
  api_key: "YOUR_ANTHROPIC_API_KEY"  # console.anthropic.com

youtube:
  oauth_file: "oauth.json"
  playlist_name_template: "Weekly Deep House Mix {week}"
  pinned_playlist_name: "My Weekly Mix"

pipeline:
  catalog_csv: "songs.csv"
  database: "deephouse.db"
  recommendations_per_source: 15
  final_playlist_size: 20
  min_rating_for_taste_profile: 3
  exclude_recent_weeks: 4
```

---

## Running the pipeline

```bash
source venv/bin/activate

# First time (or when your catalog changes):
python3 app.py analyze

# Generate and publish a new mix:
python3 app.py recommend
python3 app.py publish
```

You can also run each step individually:

```bash
python3 app.py rank     # score and preview the tracklist before publishing
python3 app.py publish --dry-run  # test YouTube search without creating a playlist
```

---

## Automated weekly runs (GitHub Actions)

Add the following secrets to your GitHub repository (`Settings → Secrets → Actions`):

| Secret | Description |
|--------|-------------|
| `BEATPORT_TOKEN_JSON` | Contents of your `beatport_token.json` |
| `YOUTUBE_OAUTH_JSON` | Contents of your `oauth.json` |
| `ANTHROPIC_API_KEY` | Your Anthropic API key |
| `LASTFM_API_KEY` | Your Last.fm API key |

The workflow in `.github/workflows/weekly-mix.yml` runs every Sunday at 08:00 UTC. You can also trigger it manually from the Actions tab with an optional dry-run flag.

---

## YouTube API quota

The YouTube Data API v3 has a free quota of 10,000 units/day.

| Operation | Cost |
|-----------|------|
| Search (per track) | 100 units |
| Playlist insert (per track) | 50 units |
| Playlist item delete (per track) | 50 units |

A typical weekly publish costs ~5,000 units. Previously found tracks are cached in the local database and served for free on future runs, so costs decrease over time.

---

## License

MIT — see [LICENSE](LICENSE).
