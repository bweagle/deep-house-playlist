"""
Parse the Deep House backlog CSV into Song objects.

CSV columns (0-indexed):
  0: rating   — blank, X/x (backlog), or 1–4 (numeric priority)
  1: title    — song title
  2: artist
  3: bpm
  4: key      — Camelot notation (e.g. "10m", "12d")
  5: duration — H:MM:SS
  6: notes
  7: local    — 0/1 flag or date added
"""

import csv
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class Song:
    rating: str
    title: str
    artist: str
    bpm: Optional[float]
    key: Optional[str]
    duration: Optional[str]
    notes: Optional[str]
    local: Optional[str]

    @property
    def search_query(self) -> str:
        """Best query for YouTube Music search."""
        if self.artist:
            return f"{self.title} {self.artist}"
        return self.title

    @property
    def numeric_rating(self) -> Optional[int]:
        """Returns 1–4 for rated songs, None for X/backlog/unrated."""
        try:
            return int(self.rating.strip())
        except (ValueError, AttributeError):
            return None

    @property
    def is_backlog(self) -> bool:
        return self.rating.strip().upper() == "X"

    def __str__(self) -> str:
        artist = f" — {self.artist}" if self.artist else ""
        rating = f"[{self.rating}] " if self.rating.strip() else ""
        return f"{rating}{self.title}{artist}"


def load_songs(csv_path: str | Path, encoding: str = "utf-8") -> list[Song]:
    """
    Load songs from the CSV export of the Google Sheet.
    Skips the header row and any row without a title.
    """
    songs: list[Song] = []
    path = Path(csv_path)

    with path.open(encoding=encoding, errors="replace") as f:
        reader = csv.reader(f)
        next(reader)  # skip header row

        for row in reader:
            # Pad to at least 8 columns
            while len(row) < 8:
                row.append("")

            title = row[1].strip()
            if not title:
                continue

            bpm: Optional[float] = None
            if row[3].strip():
                try:
                    bpm = float(row[3].strip())
                except ValueError:
                    pass

            songs.append(Song(
                rating=row[0].strip(),
                title=title,
                artist=row[2].strip(),
                bpm=bpm,
                key=row[4].strip() or None,
                duration=row[5].strip() or None,
                notes=row[6].strip() or None,
                local=row[7].strip() or None,
            ))

    return songs


def filter_songs(
    songs: list[Song],
    min_rating: Optional[int] = None,
    include_backlog: bool = False,
) -> list[Song]:
    """
    Filter songs by rating.

    Args:
        songs: full song list
        min_rating: only include songs with numeric_rating >= this value (1–4)
        include_backlog: if True, also include X-rated backlog songs
    """
    result = []
    for song in songs:
        if song.numeric_rating is not None:
            if min_rating is None or song.numeric_rating >= min_rating:
                result.append(song)
        elif include_backlog and song.is_backlog:
            result.append(song)
    return result
