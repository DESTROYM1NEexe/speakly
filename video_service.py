"""Video URL validation, metadata, and subtitle extraction."""

import asyncio
import html
import logging
import re
from typing import Any, Dict, List, Optional, cast

import yt_dlp

import config


logger = logging.getLogger(__name__)


async def is_valid_url(text: str) -> bool:
    return bool(re.match(r"^https?://[^\s]+$", text.strip()))


async def get_subtitles(url: str) -> Optional[str]:
    """Extract the best available English subtitle track."""

    try:
        options: Dict[str, Any] = {
            "quiet": True,
            "no_warnings": True,
            "socket_timeout": config.YT_DLP_TIMEOUT,
            "skip_download": True,
            "writesubtitles": True,
            "writeautomaticsub": True,
            "subtitleslangs": ["en", "en-US", "en-GB", "en.*"],
            "subtitlesformat": "vtt",
        }
        with yt_dlp.YoutubeDL(cast(Any, options)) as ydl:
            info = await asyncio.to_thread(ydl.extract_info, url, download=False)
            if not info:
                logger.error("yt-dlp returned no information")
                return None

            for key in ("subtitles", "automatic_captions"):
                subtitle_tracks = info.get(key) or {}
                transcript = await extract_best_subtitle(ydl, subtitle_tracks)
                if transcript:
                    logger.info("Successfully extracted %s", key)
                    return transcript

            logger.warning("No English subtitles found")
            return None
    except Exception:
        logger.exception("Error getting subtitles")
        return None


async def extract_best_subtitle(
    ydl: yt_dlp.YoutubeDL,
    subtitle_languages: Dict[str, Any],
) -> Optional[str]:
    if not subtitle_languages:
        return None

    preferred_languages = ("en", "en-US", "en-GB")
    ordered_tracks = []
    for language in preferred_languages:
        if language in subtitle_languages:
            ordered_tracks.append(subtitle_languages[language])
    ordered_tracks.extend(
        tracks
        for language, tracks in subtitle_languages.items()
        if language.lower().startswith("en") and language not in preferred_languages
    )

    for tracks in ordered_tracks:
        if not tracks:
            continue
        transcript = await try_subtitle_tracks(ydl, tracks)
        if transcript:
            return transcript
    return None


async def try_subtitle_tracks(
    ydl: yt_dlp.YoutubeDL,
    tracks: List[Dict[str, Any]],
) -> Optional[str]:
    sorted_tracks = sorted(tracks, key=lambda track: 0 if track.get("ext") == "vtt" else 1)
    for subtitle in sorted_tracks:
        try:
            raw_text = await asyncio.to_thread(_read_subtitle, ydl, subtitle)
            transcript = parse_vtt(raw_text) if raw_text else ""
            if transcript:
                return transcript
        except Exception:
            logger.warning("Failed to read subtitle track", exc_info=True)
    return None


def _read_subtitle(ydl: yt_dlp.YoutubeDL, subtitle: Dict[str, Any]) -> str:
    data = subtitle.get("data")
    if data:
        return data
    subtitle_url = subtitle.get("url")
    if not subtitle_url:
        return ""
    raw_data = ydl.urlopen(subtitle_url).read()
    return raw_data.decode("utf-8", errors="replace") if isinstance(raw_data, bytes) else str(raw_data)


def parse_vtt(vtt_content: str) -> str:
    """Strip WebVTT headers, cue times, tags, and repeated captions."""

    if not vtt_content:
        return ""

    metadata_keys = ("kind", "language", "style", "region", "x-timestamp-map")
    transcript_lines = []
    previous_line = ""
    for raw_line in vtt_content.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith(("WEBVTT", "NOTE", "STYLE", "REGION")):
            continue
        if re.match(
            rf"^(?:{'|'.join(metadata_keys)})\s*(?:=|:)",
            line,
            flags=re.IGNORECASE,
        ):
            continue
        if re.fullmatch(r"\d+", line):
            continue
        if re.fullmatch(
            r"^(?:\d{2}:)?\d{2}:\d{2}\.\d{3}\s+-->\s+"
            r"(?:\d{2}:)?\d{2}:\d{2}\.\d{3}(?:\s+.*)?$",
            line,
        ):
            continue

        line = html.unescape(re.sub(r"<[^>]+>", "", line)).strip()
        if line and line != previous_line:
            transcript_lines.append(line)
            previous_line = line
    return " ".join(transcript_lines)


async def get_video_title(url: str) -> Optional[str]:
    """Get video title using yt-dlp without downloading the video."""

    try:
        options: Dict[str, Any] = {
            "quiet": True,
            "no_warnings": True,
            "socket_timeout": config.YT_DLP_TIMEOUT,
            "skip_download": True,
        }
        with yt_dlp.YoutubeDL(cast(Any, options)) as ydl:
            info = await asyncio.to_thread(ydl.extract_info, url, download=False)
            return info.get("title", "Unknown") if info else None
    except Exception:
        logger.exception("Error getting video title")
        return None