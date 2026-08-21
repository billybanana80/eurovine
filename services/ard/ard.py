import base64
import binascii
import html
import json
import os
import shutil
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlencode, urljoin, urlparse

import requests
import urllib3
from pywidevine.cdm import Cdm
from pywidevine.device import Device
from pywidevine.pssh import PSSH

import icons
from colors import bcolors
from download_confirm import confirm_download
from quality_utils import apply_quality_to_filename, normalize_quality, video_selector
from services.proxy import append_downloader_proxy, mask_proxy_command


for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


SERVICE_KEY = "ard"
SERVICE_NAME = "ARD Mediathek"
SERVICE_TAG = "ARD"
SERVICE_DISPLAY_NAME = SERVICE_NAME
SERVICE_URL_PREFIXES = ("https://www.ardmediathek.de", "https://ardmediathek.de")
BASE_URL = "https://www.ardmediathek.de"
API_BASE_URL = "https://api.ardmediathek.de/page-gateway"
HBBTV_URL = "https://tv.ardmediathek.de/dyn/get?id=video:{video_id}"

N_M3U8DL = "N_m3u8DL-RE"
DEFAULT_VIDEO_SELECTOR = "best"
DEFAULT_AUDIO_SELECTOR = "best"
DEFAULT_SUBTITLE_SELECTOR = "all"

ENABLE_TRANSLATION = True
SOURCE_LANGUAGE_CODE = "de"
SOURCE_LANGUAGE_NAME = "German"
TARGET_LANGUAGE_CODE = "en"
TARGET_LANGUAGE_NAME = "English"
TRANSLATE_URL = "https://translate.googleapis.com/translate_a/single"
TRANSLATE_BATCH_MARKER = " [[[EUROVINE_OZIVINE_TRANSLATE_SPLIT]]] "
TRANSLATE_BATCH_SIZE = 25

SAVE_PATH = Path(".")
WVD_PATH = None
session = requests.Session()

DEFAULT_HEADERS = {
    "Accept": "text/html,application/json,text/plain,*/*",
    "Accept-Language": "de-DE,de;q=0.9,en-US;q=0.8,en;q=0.7",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36"
    ),
}


@dataclass
class Metadata:
    title: str = "Unknown"
    season: Optional[int] = None
    episode: Optional[int] = None
    episode_title: Optional[str] = None
    description: Optional[str] = None
    aired_date: str = "Unknown"
    video_id: Optional[str] = None
    year: Optional[int] = None
    content_type: Optional[str] = None
    source_language: Optional[str] = None


@dataclass
class PlaybackInfo:
    manifest_url: str
    manifest_type: str
    license_url: Optional[str] = None
    pssh: Optional[str] = None
    metadata: Metadata = field(default_factory=Metadata)
    subtitles: list[dict[str, Any]] = field(default_factory=list)
    license_headers: dict[str, str] = field(default_factory=dict)
    license_json: Optional[dict[str, Any]] = None


@dataclass
class EpisodeItem:
    url: str
    title: str = "Unknown"
    season: Optional[int] = None
    episode: Optional[int] = None
    episode_title: Optional[str] = None
    video_id: Optional[str] = None
    description: Optional[str] = None
    air_date: Optional[str] = None


def clean_text(value):
    text = html.unescape(str(value or "")).replace("\\n", " ")
    return re.sub(r"\s+", " ", text).strip()


def fetch_json(url, method="GET", headers=None, params=None, data=None, json_body=None, timeout=30):
    response = session.request(
        method,
        url,
        headers=headers or DEFAULT_HEADERS,
        params=params,
        data=data,
        json=json_body,
        timeout=timeout,
    )
    response.raise_for_status()
    return response.json()


def fetch_text(url, headers=None, timeout=30):
    response = session.get(url, headers=headers or DEFAULT_HEADERS, timeout=timeout)
    response.raise_for_status()
    return response.text


def extract_json_script(page_text, script_id=None):
    if script_id:
        pattern = rf'<script[^>]+id=["\']{re.escape(script_id)}["\'][^>]*>(.*?)</script>'
    else:
        pattern = r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>'

    match = re.search(pattern, page_text, flags=re.DOTALL | re.IGNORECASE)
    if not match:
        return None

    return json.loads(html.unescape(match.group(1)).strip())


def canonical_url(value):
    value = clean_text(value)
    if re.match(r"^https?://", value, re.IGNORECASE):
        return value
    if value.startswith("/"):
        return urljoin(BASE_URL, value)
    return urljoin(BASE_URL, f"/{value.strip('/')}")


def looks_like_ard_id(value):
    return bool(re.match(r"^[A-Za-z0-9_-]{16,}$", clean_text(value)))


def parse_url(input_url):
    source_url = canonical_url(input_url)
    parsed = urlparse(source_url)
    host = parsed.netloc.lower()
    if not host.endswith("ardmediathek.de"):
        raise ValueError("Expected an ardmediathek.de URL.")

    parts = [part for part in parsed.path.split("/") if part]
    ard_id = next((part for part in reversed(parts) if looks_like_ard_id(part)), "")
    if not ard_id:
        raise ValueError("Could not extract an ARD ID from the URL.")

    season = None
    for part in parts:
        match = re.fullmatch(r"staffel-(\d+)(?:-[^/]*)?", part, re.IGNORECASE)
        if match:
            season = int(match.group(1))
            break
    if season is None and parts[-1].isdigit() and len(parts) >= 2 and looks_like_ard_id(parts[-2]):
        season = int(parts[-1])

    lowered_path = parsed.path.lower()
    return {
        "source_url": source_url,
        "ard_id": ard_id,
        "season": season,
        "is_episode": "/video/" in lowered_path or "/player/" in lowered_path,
        "is_collection": any(f"/{kind}/" in lowered_path for kind in ("serie", "sendung", "sammlung")),
    }


def extract_video_id(video_url):
    return parse_url(video_url)["ard_id"]


def item_page_url(video_id):
    params = urlencode({"devicetype": "pc", "embedded": "true", "mcV6": "true"})
    return f"{API_BASE_URL}/pages/ard/item/{video_id}?{params}"


def grouping_url(show_id):
    params = urlencode({"seasoned": "true", "embedded": "true"})
    return f"{API_BASE_URL}/pages/ard/grouping/{show_id}?{params}"


def season_widget_url(show_id, season, page_number=0, page_size=100):
    params = urlencode(
        {
            "pageNumber": page_number,
            "pageSize": page_size,
            "embedded": "true",
            "seasoned": "true",
            "seasonNumber": season,
            "withAudiodescription": "false",
            "withOriginalWithSubtitle": "false",
            "withOriginalversion": "false",
            "single": "false",
        }
    )
    return f"{API_BASE_URL}/widgets/ard/asset/{show_id}?{params}"


def show_widget_url(show_id, page_number=0, page_size=100):
    params = urlencode(
        {
            "pageNumber": page_number,
            "pageSize": page_size,
            "embedded": "true",
            "seasoned": "false",
            "withAudiodescription": "false",
            "withOriginalWithSubtitle": "false",
            "withOriginalversion": "false",
            "single": "false",
        }
    )
    return f"{API_BASE_URL}/widgets/ard/asset/{show_id}?{params}"


def is_episode_url(video_url):
    return parse_url(video_url)["is_episode"]


def strip_accessibility_suffix(value):
    return clean_text(re.sub(r"\s*\((?:Audiodeskription|AD|H\u00f6rfassung|Geb\u00e4rdensprache)\)\s*$", "", clean_text(value), flags=re.IGNORECASE))


def is_alternate_accessibility_version(item):
    item_id_value = clean_text(item.get("id"))
    title = clean_text(item.get("longTitle") or item.get("title") or item.get("mediumTitle") or item.get("shortTitle"))
    target = ((item.get("links") or {}).get("target") or {})
    target_id = clean_text(target.get("id") or target.get("urlId"))
    lowered = " ".join([item_id_value, target_id, title]).lower()
    return bool(
        lowered.endswith("/audiodeskription")
        or lowered.endswith("/gebaerdensprache")
        or "audiodeskription" in lowered
        or "h\u00f6rfassung" in lowered
        or "hoerfassung" in lowered
        or "geb\u00e4rdensprache" in lowered
        or "gebaerdensprache" in lowered
    )


def player_widget(page):
    for widget in page.get("widgets") or []:
        if widget.get("type") in {"player_ondemand", "player_live"}:
            return widget
    for widget in page.get("widgets") or []:
        if widget.get("mediaCollection"):
            return widget
    raise ValueError("Could not find the ARD player payload for this item.")


def fetch_single_episode(video_id):
    return player_widget(fetch_json(item_page_url(video_id)))


def embedded_media(widget):
    return ((widget.get("mediaCollection") or {}).get("embedded") or {})


def media_meta(widget):
    return embedded_media(widget).get("meta") or {}


def target_item_id(item):
    target = ((item.get("links") or {}).get("target") or {})
    return clean_text(target.get("urlId") or target.get("id") or item.get("id"))


def item_id(item):
    return clean_text(item.get("id") or target_item_id(item))


def video_url(video_id):
    return f"{BASE_URL}/video/{video_id}"


def parse_date(value):
    value = clean_text(value)
    if not value:
        return None
    for date_format in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, date_format).replace(tzinfo=None)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


def date_value(value):
    parsed = parse_date(value)
    if parsed:
        return parsed.strftime("%Y-%m-%d")
    return clean_text(value) or "Unknown"


def episode_numbers_from_date(value):
    parsed = parse_date(value)
    if not parsed:
        return None, None
    return parsed.year, int(parsed.strftime("%m%d%H%M"))


def episode_numbers_from_title(title):
    title = clean_text(title)
    patterns = (
        (r"\(S\s*(\d+)\s*/\s*E\s*(\d+)\)", False),
        (r"\bS(?:taffel)?\s*(\d+)\s*(?:/|,|-|\s)+\s*E(?:pisode|p\.?|)\s*(\d+)\b", False),
        (r"staffel\s*(\d+).*?folge\s*(\d+)", False),
        (r"folge\s*(\d+).*?staffel\s*(\d+)", True),
    )
    for pattern, swapped in patterns:
        match = re.search(pattern, title, re.IGNORECASE)
        if not match:
            continue
        first, second = int(match.group(1)), int(match.group(2))
        return (second, first) if swapped else (first, second)

    match = re.search(r"\b(?:folge|episode|teil)\s*0*(\d+)\b", title, re.IGNORECASE)
    if match:
        return None, int(match.group(1))
    return None, None


def source_description(item, hydrated=None):
    hydrated = hydrated or {}
    meta = media_meta(hydrated) if hydrated else {}
    description = (
        hydrated.get("synopsis")
        or meta.get("synopsis")
        or hydrated.get("descriptionSeo")
        or item.get("synopsis")
        or item.get("longSynopsis")
        or item.get("shortSynopsis")
    )
    return clean_text(description) or "No Description"


def show_title(item, hydrated=None):
    hydrated = hydrated or {}
    show = hydrated.get("show") or item.get("show") or {}
    meta = media_meta(hydrated) if hydrated else {}
    return clean_text(
        meta.get("seriesTitle")
        or show.get("title")
        or (item.get("show") or {}).get("title")
        or item.get("showTitle")
        or "Unknown Show"
    )


def episode_title_from_item(item, hydrated=None):
    hydrated = hydrated or {}
    meta = media_meta(hydrated) if hydrated else {}
    title = (
        meta.get("title")
        or hydrated.get("title")
        or item.get("longTitle")
        or item.get("mediumTitle")
        or item.get("shortTitle")
        or item.get("title")
    )
    return strip_accessibility_suffix(title) or "Unknown Title"


def normalize_episode_title(title, show=None, season=None, episode=None):
    title = strip_accessibility_suffix(title)
    if not title:
        return None

    show = clean_text(show)
    parts = [clean_text(part) for part in re.split(r"\s*[|]\s*", title) if clean_text(part)]
    filtered_parts = []

    for part in parts or [title]:
        candidate = clean_text(part)
        if not candidate:
            continue
        if show and candidate.casefold() == show.casefold():
            continue

        without_markers = candidate
        without_markers = re.sub(r"(?i)\bfolge\s*0*" + re.escape(str(episode)) + r"\b", "", without_markers) if episode is not None else without_markers
        without_markers = re.sub(r"(?i)\bstaffel\s*0*" + re.escape(str(season)) + r"\b", "", without_markers) if season is not None else without_markers
        if without_markers != candidate:
            without_markers = re.sub(r"^\s*[·:;,/-]+\s*|\s*[·:;,/-]+\s*$", "", without_markers)
            without_markers = re.sub(r"\s*[·:;,/-]\s*", " ", without_markers)
        without_markers = clean_text(without_markers)
        if not without_markers:
            continue

        if show and without_markers.casefold() == show.casefold():
            continue
        if candidate.casefold() in {"folge", "staffel"}:
            continue

        filtered_parts.append(without_markers)

    normalized = " | ".join(filtered_parts)
    if show and normalized.casefold() == show.casefold():
        return None
    return normalized or None


def build_metadata(item, hydrated=None, default_season=None, default_episode=None, translate=True):
    hydrated = hydrated or {}
    title = episode_title_from_item(item, hydrated)
    season, episode = episode_numbers_from_title(title)
    if season is None or episode is None:
        season2, episode2 = episode_numbers_from_title(item.get("longTitle") or item.get("mediumTitle") or "")
        season = season if season is not None else season2
        episode = episode if episode is not None else episode2
    if season is None:
        season = default_season or item.get("_season_number")
    if episode is None:
        episode = default_episode or item.get("_episode_number")
    aired_raw = hydrated.get("broadcastedOn") or item.get("broadcastedOn") or media_meta(hydrated).get("broadcastedOnDateTime")
    if season is None or episode is None:
        date_season, date_episode = episode_numbers_from_date(aired_raw)
        season = season if season is not None else date_season
        episode = episode if episode is not None else date_episode

    description = source_description(item, hydrated)
    if translate and description != "No Description":
        description = translate_to_target_language(description)

    show = show_title(item, hydrated)
    show_data = hydrated.get("show") or {}
    is_single = show_data.get("coreAssetType") == "SINGLE" or not show_data.get("availableSeasons")
    episode_title = normalize_episode_title(title, show=show, season=season, episode=episode)
    return Metadata(
        title=show if not is_single else title,
        season=None if is_single else season,
        episode=None if is_single else episode,
        episode_title=None if is_single or clean_text(title).lower() == clean_text(show).lower() else episode_title,
        description=description,
        aired_date=date_value(aired_raw),
        video_id=item_id(item) or target_item_id(item),
        year=(parse_date(aired_raw) or datetime.min).year if aired_raw else None,
        content_type="movie" if is_single else "episode",
        source_language=SOURCE_LANGUAGE_CODE,
    )


def search_metadata(video_url, video_id):
    widget = fetch_single_episode(video_id)
    return build_metadata({"id": video_id}, widget, translate=False)


def media_url(media):
    stream_value = media.get("_stream")
    if isinstance(stream_value, list):
        stream_value = stream_value[0] if stream_value else ""
    url = clean_text(media.get("url") or media.get("href") or stream_value)
    if url.startswith("//"):
        return "https:" + url
    return url


def iter_media_entries(media_root):
    for stream in media_root.get("streams") or []:
        if stream.get("kind") and stream.get("kind") != "main":
            continue
        for media in stream.get("media") or []:
            if isinstance(media, dict):
                yield media

    for media_array in media_root.get("_mediaArray") or []:
        for stream in media_array.get("_mediaStreamArray") or []:
            stream_url = stream.get("_stream")
            urls = stream_url if isinstance(stream_url, list) else [stream_url]
            for url in urls:
                if url:
                    yield {"url": url, "mimeType": stream.get("_mimeType") or ""}


def select_manifest(media_root):
    candidates = []
    for media in iter_media_entries(media_root):
        url = media_url(media)
        if not url:
            continue
        mime_type = clean_text(media.get("mimeType") or media.get("_mimeType")).lower()
        audio_kind = clean_text(((media.get("audios") or [{}])[0] or {}).get("kind")).lower()
        score = 0
        if audio_kind in ("", "standard"):
            score += 10
        if ".m3u8" in url or "mpegurl" in mime_type:
            score += 8
            manifest_type = "m3u8"
        elif ".mpd" in url or "dash" in mime_type:
            score += 6
            manifest_type = "mpd"
        else:
            continue
        candidates.append((score, manifest_type, url))
    if not candidates:
        return None, None
    candidates.sort(reverse=True)
    return candidates[0][1], candidates[0][2]


def hbbtv_dash_manifest(video_id):
    try:
        payload = fetch_json(HBBTV_URL.format(video_id=video_id))
    except Exception:
        return None
    for stream in ((payload.get("video") or {}).get("streams") or []):
        for media in stream.get("media") or []:
            url = media_url(media)
            mime_type = clean_text(media.get("mimeType")).lower()
            audio_kind = clean_text(((media.get("audios") or [{}])[0] or {}).get("kind")).lower()
            if audio_kind in ("", "standard") and (".mpd" in url or "dash" in mime_type):
                return url
    return None


def collect_subtitles(media_root):
    subtitles = []
    seen = set()
    for subtitle in media_root.get("subtitles") or []:
        language = clean_text(subtitle.get("languageCode") or subtitle.get("language") or "de") or "de"
        label = clean_text(subtitle.get("label") or subtitle.get("title") or "German")
        for source in subtitle.get("sources") or []:
            url = clean_text(source.get("url"))
            if not url or url in seen:
                continue
            seen.add(url)
            kind = clean_text(source.get("kind") or source.get("format") or "subtitle")
            subtitles.append({"url": url, "language": language, "label": label, "kind": kind})
            if kind == "ebutt" and "/ebutt/" in url:
                vtt_url = url.replace("/ebutt/", "/webvtt/")
                if not vtt_url.endswith(".vtt"):
                    vtt_url += ".vtt"
                if vtt_url not in seen:
                    seen.add(vtt_url)
                    subtitles.append({"url": vtt_url, "language": language, "label": label, "kind": "webvtt"})
    subtitle_url = clean_text(media_root.get("_subtitleUrl"))
    if subtitle_url and subtitle_url not in seen:
        subtitles.append({"url": subtitle_url, "language": "de", "label": "German", "kind": "ebutt"})
    return subtitles


def get_playback_info(video_url, metadata):
    video_id = metadata.video_id or extract_video_id(video_url)
    widget = fetch_single_episode(video_id)
    if widget.get("blockedByFsk"):
        raise ValueError("This ARD item is age-restricted and is only available during the German watershed window.")

    playback_metadata = build_metadata({"id": video_id}, widget, translate=False)
    media_root = embedded_media(widget)
    manifest_type, manifest_url = select_manifest(media_root)
    dash_url = hbbtv_dash_manifest(video_id)
    if dash_url:
        manifest_type, manifest_url = "mpd", dash_url
    if not manifest_url:
        if widget.get("geoblocked") or media_root.get("_geoblocked"):
            raise ValueError("No ARD stream URL found. This item may require a German proxy/VPN.")
        raise ValueError("No ARD HLS or DASH manifest URL found.")

    return PlaybackInfo(
        manifest_url=manifest_url,
        manifest_type=manifest_type,
        metadata=playback_metadata,
        subtitles=collect_subtitles(media_root),
    )


def collect_episode_item(video_url):
    video_id = extract_video_id(video_url)
    metadata = search_metadata(video_url, video_id)
    return EpisodeItem(
        url=canonical_url(video_url),
        title=metadata.title,
        season=metadata.season,
        episode=metadata.episode,
        episode_title=metadata.episode_title,
        video_id=metadata.video_id or video_id,
        description=metadata.description,
    )


def discover_seasons(show_id):
    page = fetch_json(grouping_url(show_id))
    seasons = []
    for widget in page.get("widgets") or []:
        title = clean_text(widget.get("title"))
        season = clean_text(widget.get("seasonNumber"))
        if not season.isdigit() or "audiodeskription" in title.lower():
            continue
        number = int(season)
        if number not in seasons:
            seasons.append(number)
    return sorted(seasons)


def fetch_paged_widget(url_builder):
    items = []
    page_number = 0
    total = None
    while total is None or len(items) < total:
        payload = fetch_json(url_builder(page_number))
        page_items = payload.get("teasers") or []
        if not page_items:
            break
        items.extend(page_items)
        pagination = payload.get("pagination") or {}
        total = int(pagination.get("totalElements") or len(items))
        page_number += 1
    return items


def fetch_season_episodes(show_id, season):
    return fetch_paged_widget(lambda page_number: season_widget_url(show_id, season, page_number=page_number))


def fetch_show_episodes(show_id):
    return fetch_paged_widget(lambda page_number: show_widget_url(show_id, page_number=page_number))


def hydrate_episode(item):
    video_id = target_item_id(item)
    return fetch_single_episode(video_id) if video_id else {}


def build_episode_list_item(item, show_name, season_hint=None, episode_hint=None):
    hydrated = {}
    try:
        hydrated = hydrate_episode(item)
    except Exception as exc:
        print(f"{icons.ICON_WARNING} {bcolors.WARNING}Could not hydrate ARD episode {item_id(item)}: {exc}{bcolors.ENDC}")
    metadata = build_metadata(item, hydrated, default_season=season_hint, default_episode=episode_hint, translate=False)
    metadata.title = show_name or metadata.title
    return EpisodeItem(
        url=video_url(metadata.video_id or target_item_id(item)),
        title=metadata.title,
        season=metadata.season,
        episode=metadata.episode,
        episode_title=metadata.episode_title,
        video_id=metadata.video_id or target_item_id(item),
        description=metadata.description,
        air_date=clean_text(hydrated.get("broadcastedOn") or item.get("broadcastedOn")),
    )


def collect_episode_items(series_url, show_progress=True):
    url_info = parse_url(series_url)
    if url_info["is_episode"]:
        return [collect_episode_item(series_url)]

    show_id = url_info["ard_id"]
    seasons = [url_info["season"]] if url_info["season"] else discover_seasons(show_id)
    if show_progress and seasons:
        print(f"{bcolors.OKGREEN}Seasons: {', '.join(str(season) for season in seasons)}{bcolors.ENDC}")

    raw_items = []
    if seasons:
        for season in seasons:
            season_items = fetch_season_episodes(show_id, season)
            if show_progress:
                print(f"Fetched season {season}: {len(season_items)} rows")
            for index, item in enumerate(season_items, start=1):
                item["_season_number"] = season
                item["_episode_number"] = index
            raw_items.extend(season_items)
    else:
        raw_items = fetch_show_episodes(show_id)
        for index, item in enumerate(raw_items, start=1):
            item["_episode_number"] = index

    episode_items = []
    seen = set()
    show_name = ""
    for item in raw_items:
        if item.get("coreAssetType") not in (None, "EPISODE", "SINGLE", "MOVIE"):
            continue
        if is_alternate_accessibility_version(item):
            continue
        video_id = target_item_id(item)
        dedupe_key = re.sub(r"/(?:audiodeskription|gebaerdensprache)$", "", video_id, flags=re.IGNORECASE)
        if not dedupe_key or dedupe_key in seen or dedupe_key == show_id:
            continue
        seen.add(dedupe_key)
        episode_item = build_episode_list_item(
            item,
            show_name,
            season_hint=item.get("_season_number"),
            episode_hint=item.get("_episode_number"),
        )
        show_name = show_name or episode_item.title
        episode_items.append(episode_item)

    if not episode_items:
        raise ValueError("No standard ARD episodes were found for this series URL.")
    return sorted(episode_items, key=episode_sort_key)

def episode_sort_key(item):
    return (
        item.season if item.season is not None else 9999,
        item.episode if item.episode is not None else 9999,
        item.video_id or item.url,
    )


def episode_series_number(item):
    return item.season


def episode_number(item):
    return item.episode


def episode_tree_label(item):
    number = episode_number(item)
    title = item.episode_title
    if not title and (item.season is None or item.episode is None):
        title = item.title or item.url
    return str(number).zfill(2) if number is not None else "-", title


def group_episode_items_by_series(episode_items):
    grouped = {}
    for item in sorted(episode_items, key=episode_sort_key):
        season = episode_series_number(item)
        series_label = f"Series {season}" if season is not None else "Episodes"
        grouped.setdefault(series_label, []).append(item)
    return grouped


def series_group_sort_key(label):
    match = re.search(r"\d+", label)
    return int(match.group(0)) if match else 0


def print_series_rule(service_label, series_title):
    terminal_width = shutil.get_terminal_size((88, 20)).columns
    title = f" {service_label}: {series_title} "
    rule_width = max(terminal_width, len(title) + 4)
    left_width = max((rule_width - len(title)) // 2, 0)
    right_width = max(rule_width - len(title) - left_width, 0)
    print(
        f"{bcolors.LIGHTBLUE}{'─' * left_width}{bcolors.ENDC} "
        f"{bcolors.LIGHTBLUE}{service_label}: {bcolors.ENDC}{bcolors.WHITE}{series_title}{bcolors.ENDC} "
        f"{bcolors.LIGHTBLUE}{'─' * right_width}{bcolors.ENDC}"
    )


def parse_selector_part(selector_part):
    match = re.fullmatch(r"s(?P<season>\d{2}|\d{4})(?:e(?P<episode>\d{2}))?", selector_part)
    if not match:
        raise ValueError(
            "Download selector must be sXXeXX, sXXXXeXX, sXX, sXXXX, or a matching range. "
            "Examples: s01e01, s2026e01, s01, s2026, s01e03-s02e02, s01-s03"
        )
    return {
        "season": int(match.group("season")),
        "episode": int(match.group("episode")) if match.group("episode") else None,
    }


def parse_download_selector(selector):
    selector = str(selector or "").strip().lower()
    if "-" not in selector:
        part = parse_selector_part(selector)
        return {
            "type": "single_episode" if part["episode"] is not None else "single_season",
            "start": part,
            "end": part,
        }

    range_parts = selector.split("-", 1)
    if not range_parts[0] or not range_parts[1]:
        raise ValueError("Download range must include both start and end selectors.")

    start = parse_selector_part(range_parts[0])
    end = parse_selector_part(range_parts[1])
    start_has_episode = start["episode"] is not None
    end_has_episode = end["episode"] is not None
    if start_has_episode != end_has_episode:
        raise ValueError("Download range must use two episode selectors or two season selectors.")

    if start_has_episode:
        if (start["season"], start["episode"]) > (end["season"], end["episode"]):
            raise ValueError("Download episode range start must be before the end selector.")
        return {"type": "episode_range", "start": start, "end": end}

    if start["season"] > end["season"]:
        raise ValueError("Download season range start must be before the end selector.")
    return {"type": "season_range", "start": start, "end": end}


def format_selector_part(part):
    season = part["season"]
    season_label = f"s{season:04d}" if season >= 1000 else f"s{season:02d}"
    if part["episode"] is not None:
        return f"{season_label}e{part['episode']:02d}"
    return season_label


def format_download_selector(parsed_selector):
    if parsed_selector["start"] == parsed_selector["end"]:
        return format_selector_part(parsed_selector["start"])
    return f"{format_selector_part(parsed_selector['start'])}-{format_selector_part(parsed_selector['end'])}"


def format_queue_selector(season, episode=None):
    season_label = f"S{season:04d}" if season >= 1000 else f"S{season:02d}"
    if episode is not None:
        return f"{season_label}E{episode:02d}"
    return season_label


def warn_if_partial_range_match(parsed_selector, selected):
    if parsed_selector["type"] == "episode_range":
        requested_start = (parsed_selector["start"]["season"], parsed_selector["start"]["episode"])
        requested_end = (parsed_selector["end"]["season"], parsed_selector["end"]["episode"])
        matched_start = (episode_series_number(selected[0]), episode_number(selected[0]))
        matched_end = (episode_series_number(selected[-1]), episode_number(selected[-1]))
        if matched_start > requested_start or matched_end < requested_end:
            matched_label = f"{format_queue_selector(*matched_start)}-{format_queue_selector(*matched_end)}"
            print(
                f"{icons.ICON_WARNING} {bcolors.WARNING}Requested range "
                f"{format_download_selector(parsed_selector)} only matched {matched_label}.{bcolors.ENDC}"
            )

    if parsed_selector["type"] == "season_range":
        requested_start = parsed_selector["start"]["season"]
        requested_end = parsed_selector["end"]["season"]
        matched_seasons = sorted({episode_series_number(item) for item in selected if episode_series_number(item) is not None})
        if matched_seasons and (matched_seasons[0] > requested_start or matched_seasons[-1] < requested_end):
            matched_label = f"{format_queue_selector(matched_seasons[0])}-{format_queue_selector(matched_seasons[-1])}"
            print(
                f"{icons.ICON_WARNING} {bcolors.WARNING}Requested range "
                f"{format_download_selector(parsed_selector)} only matched seasons {matched_label}.{bcolors.ENDC}"
            )


def select_episode_items(series_url, selector):
    parsed_selector = parse_download_selector(selector)
    episode_items = collect_episode_items(series_url, show_progress=False)
    selected = []
    for item in episode_items:
        season = episode_series_number(item)
        episode = episode_number(item)
        if season is None or episode is None:
            continue

        if parsed_selector["type"] == "single_episode":
            keep = season == parsed_selector["start"]["season"] and episode == parsed_selector["start"]["episode"]
        elif parsed_selector["type"] == "single_season":
            keep = season == parsed_selector["start"]["season"]
        elif parsed_selector["type"] == "episode_range":
            keep = (
                (parsed_selector["start"]["season"], parsed_selector["start"]["episode"])
                <= (season, episode)
                <= (parsed_selector["end"]["season"], parsed_selector["end"]["episode"])
            )
        else:
            keep = parsed_selector["start"]["season"] <= season <= parsed_selector["end"]["season"]

        if keep:
            selected.append(item)

    if not selected:
        raise ValueError(f"No {SERVICE_NAME} episodes found for selector {format_download_selector(parsed_selector)}.")

    selected.sort(key=episode_sort_key)
    warn_if_partial_range_match(parsed_selector, selected)
    return selected


def print_download_queue(episode_items):
    print()
    print(f"{bcolors.LIGHTBLUE}Download queue:{bcolors.ENDC}")
    for item in episode_items:
        season = episode_series_number(item)
        episode = episode_number(item)
        selector = format_queue_selector(season, episode) if season is not None and episode is not None else item.video_id or item.url
        _, title = episode_tree_label(item)
        print(f"{selector} {title or ''}".rstrip())


def safe_windows_filename(value):
    value = clean_text(value).replace("'", "")
    value = re.sub(r'[\\/:*?"<>|]', " ", value)
    value = re.sub(r"\s+", ".", value)
    return value.strip(".") or "Unknown"


def export_episode_list_text(series_url, episode_items):
    export_dir = Path(__file__).resolve().parents[2] / "export"
    export_dir.mkdir(parents=True, exist_ok=True)
    slug = safe_windows_filename(Path(series_url.rstrip("/")).name or SERVICE_KEY)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_path = export_dir / f"{SERVICE_KEY}_{slug}_export_{timestamp}.txt"

    lines = []
    for item in sorted(episode_items, key=episode_sort_key):
        season = episode_series_number(item)
        episode = episode_number(item)
        selector = format_queue_selector(season, episode) if season is not None and episode is not None else "-"
        _, title = episode_tree_label(item)
        lines.append(f"{selector}\t{title}\t{item.url}")

    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return output_path


def list_episode_items(episode_items, export_list=False, series_url=None):
    if not episode_items:
        print(f"{icons.ICON_FAILURE} {bcolors.FAIL}No {SERVICE_NAME} episodes found.{bcolors.ENDC}")
        return

    series_title = episode_items[0].title or SERVICE_NAME
    grouped_items = group_episode_items_by_series(episode_items)
    group_labels = sorted(grouped_items, key=series_group_sort_key)
    series_summary = ",  ".join(f"{label}({len(grouped_items[label])})" for label in group_labels)

    print(f"{icons.ICON_SUCCESS} {bcolors.OKGREEN}Found {len(episode_items)} {SERVICE_NAME} episodes{bcolors.ENDC}")
    print()
    print_series_rule(f"{SERVICE_NAME} Series", series_title)
    print()
    print(f"{bcolors.GRAY}{len(group_labels)} Series" + (f",  {series_summary}" if series_summary else "") + f"{bcolors.ENDC}")

    for series_index, series_label in enumerate(group_labels):
        series_items = grouped_items[series_label]
        if series_index > 0:
            print(f"{bcolors.GRAY}│{bcolors.ENDC}")

        group_is_last = series_index == len(group_labels) - 1
        group_branch = "└─" if group_is_last else "├─"
        group_child_prefix = "   " if group_is_last else "│  "
        print(f"{bcolors.GRAY}{group_branch} {series_label}: {bcolors.ENDC}{len(series_items)} episodes")

        for episode_index, item in enumerate(series_items):
            is_last = episode_index == len(series_items) - 1
            branch = "└─" if is_last else "├─"
            url_branch = "  " if is_last else "│ "
            number_label, title = episode_tree_label(item)
            title_suffix = f" {title}" if title else ""
            print(f"{bcolors.GRAY}{group_child_prefix}{branch} {number_label}.{bcolors.ENDC}{title_suffix}")
            print(f"{bcolors.GRAY}{group_child_prefix}{url_branch} {bcolors.ENDC}{bcolors.LIGHTBLUE}{item.url}{bcolors.ENDC}")

    if export_list:
        export_path = export_episode_list_text(series_url or SERVICE_KEY, episode_items)
        print(f"{bcolors.OKGREEN}{icons.ICON_SUCCESS} Exported list: {export_path}{bcolors.ENDC}")


def get_pssh_from_manifest(manifest_url):
    response = session.get(manifest_url, headers=DEFAULT_HEADERS, timeout=30)
    response.raise_for_status()

    root = ET.fromstring(response.content)
    ns_mpd = "{urn:mpeg:dash:schema:mpd:2011}"
    ns_cenc = "{urn:mpeg:cenc:2013}"
    widevine_uuid = "edef8ba9-79d6-4ace-a3c8-27dcd51d21ed"

    for content_protection in root.findall(".//" + ns_mpd + "ContentProtection"):
        scheme = (content_protection.attrib.get("schemeIdUri") or "").lower()
        if widevine_uuid in scheme:
            pssh_el = content_protection.find(ns_cenc + "pssh")
            if pssh_el is not None and pssh_el.text:
                pssh_data = pssh_el.text.strip()
                base64.b64decode(pssh_data)
                return pssh_data

    for pssh_el in root.findall(".//" + ns_cenc + "pssh"):
        if pssh_el.text:
            pssh_data = pssh_el.text.strip()
            base64.b64decode(pssh_data)
            return pssh_data

    raise ValueError("PSSH not found in the manifest.")


def build_license_headers(playback, metadata):
    if playback.license_headers:
        return playback.license_headers

    # TODO: add service-specific licence request headers, tokens, custom data, etc.
    return {
        "Content-Type": "application/octet-stream",
        "Origin": SERVICE_URL_PREFIXES[0],
        "Referer": f"{SERVICE_URL_PREFIXES[0]}/",
        "User-Agent": DEFAULT_HEADERS["User-Agent"],
    }


def post_license_challenge(playback, challenge):
    headers = build_license_headers(playback, playback.metadata)
    if playback.license_json is not None:
        payload = dict(playback.license_json)
        payload["challenge"] = base64.b64encode(challenge).decode("ascii")
        response = session.post(playback.license_url, headers=headers, json=payload, timeout=30)
    else:
        response = session.post(playback.license_url, headers=headers, data=challenge, timeout=30)

    try:
        response.raise_for_status()
    except requests.exceptions.HTTPError as exc:
        print(f"{icons.ICON_FAILURE} {bcolors.FAIL}HTTPError: {exc}{bcolors.ENDC}")
        print(f"{icons.ICON_INFO} Response Headers: {response.headers}")
        print(f"{icons.ICON_INFO} Response Text: {response.text[:2000]}")
        raise

    # TODO: if the service wraps the licence in JSON, decode and return the raw licence bytes here.
    return response.content


def get_keys(playback):
    if not WVD_PATH:
        print(f"{icons.ICON_WARNING} {bcolors.WARNING}No WVD device path configured; skipping keys.{bcolors.ENDC}")
        return []

    try:
        pssh = PSSH(playback.pssh)
    except (binascii.Error, ValueError) as exc:
        print(f"{icons.ICON_FAILURE} {bcolors.FAIL}Could not parse PSSH: {exc}{bcolors.ENDC}")
        return []

    device = Device.load(WVD_PATH)
    cdm = Cdm.from_device(device)
    session_id = cdm.open()

    try:
        challenge = cdm.get_license_challenge(session_id, pssh)
        licence = post_license_challenge(playback, challenge)
        cdm.parse_license(session_id, licence)
        return [f"{key.kid.hex}:{key.key.hex()}" for key in cdm.get_keys(session_id) if key.type == "CONTENT"]
    finally:
        cdm.close(session_id)


def get_dash_resolution(mpd_url):
    response = session.get(mpd_url, headers=DEFAULT_HEADERS, timeout=30)
    response.raise_for_status()
    root = ET.fromstring(response.content)
    heights = [
        int(rep.get("height"))
        for rep in root.findall(".//{urn:mpeg:dash:schema:mpd:2011}Representation")
        if rep.get("height")
    ]
    return f"{max(heights)}p" if heights else "Unknown"


def get_hls_resolution(m3u8_url):
    response = session.get(m3u8_url, headers=DEFAULT_HEADERS, timeout=30)
    response.raise_for_status()
    resolutions = re.findall(r"RESOLUTION=\d+x(\d+)", response.text)
    if not resolutions:
        return "Unknown"
    return f"{max(int(height) for height in resolutions)}p"


def get_resolution(playback):
    if playback.manifest_type == "mpd":
        return get_dash_resolution(playback.manifest_url)
    if playback.manifest_type == "m3u8":
        return get_hls_resolution(playback.manifest_url)
    return "Unknown"


def format_filename(metadata, resolution):
    title = safe_windows_filename(metadata.title)
    season_episode = ""
    if metadata.season is not None and metadata.episode is not None:
        season_episode = f"S{int(metadata.season):02}E{int(metadata.episode):02}"
    elif metadata.season is not None:
        season_episode = f"S{int(metadata.season):02}"

    parts = [title]
    if season_episode:
        parts.append(season_episode)
    if metadata.episode_title:
        parts.append(safe_windows_filename(metadata.episode_title))
    parts.extend([resolution, SERVICE_TAG, "WEB-DL", "AAC2.0", "H.264"])
    return ".".join(part for part in parts if part and part != "Unknown")


def build_download_command(playback, filename, keys=None, mode="auto", quality=None, include_subtitles=True):
    if mode == "interactive":
        selectors = ""
    else:
        subtitle_selector = f"--select-subtitle {DEFAULT_SUBTITLE_SELECTOR}" if include_subtitles else "--drop-subtitle all"
        selectors = f'{video_selector(quality, default=DEFAULT_VIDEO_SELECTOR)} --select-audio {DEFAULT_AUDIO_SELECTOR} {subtitle_selector} '

    command = (
        f'{N_M3U8DL} "{playback.manifest_url}" '
        f'{selectors}'
        f'-mt -M format=mkv --save-dir "{SAVE_PATH}" --save-name "{filename}"'
    )

    if keys:
        command += " " + " ".join(f"--key {key}" for key in keys)

    return append_downloader_proxy(command)


def fetch_manifest_text(playback):
    response = session.get(playback.manifest_url, headers=DEFAULT_HEADERS, timeout=30)
    response.raise_for_status()
    response.encoding = response.encoding or "utf-8"
    return response.text


def format_bitrate(value):
    try:
        bitrate = int(float(value))
    except (TypeError, ValueError):
        return "-"
    return f"{bitrate / 1000000:.2f} Mbps" if bitrate >= 1000000 else f"{bitrate // 1000} Kbps"


def parse_manifest_attributes(line):
    _, _, payload = line.partition(":")
    attrs = {}
    for match in re.finditer(r'([A-Z0-9-]+)=("[^"]*"|[^,]*)', payload, re.I):
        value = match.group(2).strip()
        if value.startswith('"') and value.endswith('"'):
            value = value[1:-1]
        attrs[match.group(1).lower()] = html.unescape(value)
    return attrs


def stream_table_sort_key(stream):
    type_order = {"Vid": 0, "Aud": 1, "Sub": 2}
    height_match = re.search(r"x(\d+)", stream.get("resolution") or "")
    height = int(height_match.group(1)) if height_match else 0
    bitrate_text = stream.get("bitrate") or ""
    bitrate_match = re.search(r"[\d.]+", bitrate_text)
    bitrate = float(bitrate_match.group()) if bitrate_match else 0
    if "Mbps" in bitrate_text:
        bitrate *= 1000
    return (type_order.get(stream.get("type"), 9), -height, -bitrate, stream.get("lang") or "")


def parse_hls_streams(manifest_text):
    streams = []
    pending_variant = None
    for raw_line in manifest_text.splitlines():
        line = raw_line.strip()
        if line.startswith("#EXT-X-STREAM-INF:"):
            pending_variant = parse_manifest_attributes(line)
            continue
        if pending_variant is not None and line and not line.startswith("#"):
            attrs = pending_variant
            streams.append({
                "type": "Vid",
                "resolution": attrs.get("resolution") or "-",
                "bitrate": format_bitrate(attrs.get("average-bandwidth") or attrs.get("bandwidth")),
                "codec": attrs.get("codecs") or "-",
                "lang": "-",
                "channels": "-",
            })
            pending_variant = None
            continue
        if not line.startswith("#EXT-X-MEDIA:"):
            continue
        attrs = parse_manifest_attributes(line)
        media_type = (attrs.get("type") or "").upper()
        if media_type == "AUDIO":
            stream_type = "Aud"
        elif media_type in {"SUBTITLES", "CLOSED-CAPTIONS"}:
            stream_type = "Sub"
        else:
            continue
        streams.append({
            "type": stream_type,
            "resolution": "-",
            "bitrate": "-",
            "codec": "-",
            "lang": attrs.get("language") or "-",
            "channels": attrs.get("channels") or "-",
        })
    return sorted(streams, key=stream_table_sort_key)


def parse_dash_streams(manifest_text):
    root = ET.fromstring(manifest_text.encode("utf-8") if isinstance(manifest_text, str) else manifest_text)
    streams = []
    for adaptation in root.findall(".//{*}AdaptationSet"):
        adaptation_mime = clean_text(adaptation.get("mimeType"))
        adaptation_type = clean_text(adaptation.get("contentType"))
        adaptation_codec = clean_text(adaptation.get("codecs"))
        adaptation_lang = clean_text(adaptation.get("lang")) or "-"
        adaptation_channels = next(
            (
                clean_text(node.get("value"))
                for node in adaptation.findall("{*}AudioChannelConfiguration")
                if node.get("value")
            ),
            "-",
        )
        for rep in adaptation.findall("{*}Representation"):
            mime = clean_text(rep.get("mimeType")) or adaptation_mime
            content_type = adaptation_type or (mime.split("/", 1)[0] if "/" in mime else "")
            if content_type == "video":
                stream_type = "Vid"
            elif content_type == "audio":
                stream_type = "Aud"
            elif content_type in {"text", "subtitle", "subtitles"} or "ttml" in mime or "vtt" in mime:
                stream_type = "Sub"
            else:
                continue
            width = clean_text(rep.get("width"))
            height = clean_text(rep.get("height"))
            streams.append({
                "type": stream_type,
                "resolution": f"{width}x{height}" if width and height else "-",
                "bitrate": format_bitrate(rep.get("bandwidth")),
                "codec": clean_text(rep.get("codecs")) or adaptation_codec or "-",
                "lang": adaptation_lang,
                "channels": adaptation_channels if stream_type == "Aud" else "-",
            })
    return sorted(streams, key=stream_table_sort_key)


def playback_streams(playback):
    manifest_text = fetch_manifest_text(playback)
    if playback.manifest_type == "mpd":
        return parse_dash_streams(manifest_text)
    if playback.manifest_type == "m3u8":
        return parse_hls_streams(manifest_text)
    return []


def print_streams(streams):
    print(f"\n{bcolors.YELLOW}Available streams:{bcolors.ENDC}")
    if not streams:
        print("No video, audio, or subtitle streams were found in the manifest.")
        return

    headings = ("#", "Type", "Resolution", "Bitrate", "Codec", "Lang", "Channels")
    rows = [
        (
            str(index),
            stream["type"],
            stream["resolution"],
            stream["bitrate"],
            stream["codec"],
            stream["lang"],
            stream["channels"],
        )
        for index, stream in enumerate(streams, 1)
    ]
    widths = [
        min(max(len(headings[column]), *(len(row[column]) for row in rows)), 52)
        for column in range(len(headings))
    ]
    widths[0] = 3
    print("  ".join(f"{heading:<{widths[index]}}" for index, heading in enumerate(headings)))
    print("  ".join("-" * width for width in widths))
    for row in rows:
        print("  ".join(f"{value[:widths[index]]:<{widths[index]}}" for index, value in enumerate(row)))


def print_episode_metadata(metadata):
    print(f"\n{bcolors.YELLOW}Episode metadata:{bcolors.ENDC}")
    print(f"{bcolors.LIGHTBLUE}Show: {bcolors.ENDC}{metadata.title or 'Unknown'}")
    if metadata.season is not None or metadata.episode is not None:
        print(f"{bcolors.LIGHTBLUE}Episode: {bcolors.ENDC}{format_queue_selector(metadata.season or 0, metadata.episode)}")
    print(f"{bcolors.LIGHTBLUE}Title: {bcolors.ENDC}{metadata.episode_title or metadata.title or 'Unknown'}")
    print(f"{bcolors.LIGHTBLUE}Date Aired: {bcolors.ENDC}{metadata.aired_date or 'Unknown'}")
    description = metadata.description or 'No Description'
    if ENABLE_TRANSLATION and description != 'No Description':
        description = translate_to_target_language(description)
    print(f"{bcolors.LIGHTBLUE}Description: {bcolors.ENDC}{description}")


def print_external_subtitles(subtitles):
    if not subtitles:
        return

    print(f"\n{bcolors.YELLOW}External subtitles:{bcolors.ENDC}")
    for index, subtitle in enumerate(subtitles, start=1):
        print(
            f"  {index:02d}. "
            f"{clean_text(subtitle.get('language') or '-'):<6} "
            f"{clean_text(subtitle.get('kind') or subtitle.get('type') or 'subtitle'):<10} "
            f"{clean_text(subtitle.get('label') or subtitle.get('name') or '-')}"
        )


def print_metadata(metadata):
    print(f"{icons.ICON_SUCCESS} {bcolors.OKGREEN}{metadata.title}{bcolors.ENDC}")
    if metadata.season is not None or metadata.episode is not None:
        print(f"{bcolors.LIGHTBLUE}Episode: {bcolors.ENDC}{format_queue_selector(metadata.season or 0, metadata.episode)}")
    if metadata.episode_title:
        print(f"{bcolors.LIGHTBLUE}Episode title: {bcolors.ENDC}{metadata.episode_title}")
    if metadata.description:
        description = metadata.description
        if ENABLE_TRANSLATION:
            description = translate_to_target_language(description)
        print(f"{bcolors.LIGHTBLUE}Description: {bcolors.ENDC}{description}")


def print_playback_details(playback, keys, command, filename, info=False):
    if info:
        label = "DASH Manifest URL" if playback.manifest_type == "mpd" else "HLS Manifest URL"
        print(f"{bcolors.LIGHTBLUE}{label}: {bcolors.ENDC}{playback.manifest_url}")

        if playback.license_url:
            print(f"{bcolors.RED}License URL: {bcolors.ENDC}{playback.license_url}")
        if playback.pssh:
            print(f"{bcolors.LIGHTBLUE}PSSH: {bcolors.ENDC}{playback.pssh}")
        for key in keys:
            print(f"{bcolors.OKGREEN}KEYS: {bcolors.ENDC}--key {key}")

        try:
            print_streams(playback_streams(playback))
        except Exception as exc:
            print(f"{bcolors.WARNING}{icons.ICON_WARNING} Could not inspect manifest streams: {exc}{bcolors.ENDC}")

        print_external_subtitles(playback.subtitles)
        print_episode_metadata(playback.metadata)
        print(f"\n{bcolors.YELLOW}Suggested filename: {bcolors.ENDC}{filename}.mkv")
        return

    if playback.manifest_type == "mpd":
        print(f"{bcolors.LIGHTBLUE}DASH URL: {bcolors.ENDC}{playback.manifest_url}")
        if playback.pssh:
            print(f"{bcolors.LIGHTBLUE}PSSH: {bcolors.ENDC}{playback.pssh}")
        if playback.license_url:
            print(f"{bcolors.RED}Licence URL: {bcolors.ENDC}{playback.license_url}")
        for key in keys:
            print(f"{bcolors.OKGREEN}KEYS: {bcolors.ENDC}--key {key}")
    else:
        print(f"{bcolors.LIGHTBLUE}M3U8 URL: {bcolors.ENDC}{playback.manifest_url}")

    print(f"{bcolors.YELLOW}DOWNLOAD COMMAND: {bcolors.ENDC}")
    print(mask_proxy_command(command))


def maybe_download(command, auto_download=False, auto_confirm=False):
    if confirm_download("Do you wish to download? Y or N: ", auto_confirm=auto_confirm, auto_download=auto_download):
        print(f"{icons.ICON_WAITING} {bcolors.OKBLUE}Downloading video...{bcolors.ENDC}")
        subprocess.run(command, shell=True)
    else:
        print(f"{icons.ICON_FAILURE} {bcolors.RED}Download cancelled{bcolors.ENDC}")


def resolve_video(video_url, mode="auto", quality=None):
    print(f"{icons.ICON_INFO} {bcolors.LIGHTBLUE}Processing: {bcolors.ENDC}{video_url}")
    video_id = extract_video_id(video_url)
    metadata = search_metadata(video_url, video_id)

    print(f"{icons.ICON_WAITING} {bcolors.LIGHTBLUE}Fetching playback info...{bcolors.ENDC}")
    playback = get_playback_info(video_url, metadata)
    playback_metadata = playback.metadata
    if playback_metadata and (playback_metadata.title != "Unknown" or playback_metadata.video_id):
        metadata = playback_metadata
    else:
        playback.metadata = metadata

    keys = []
    if playback.manifest_type == "mpd":
        if not playback.pssh:
            print(f"{icons.ICON_WAITING} {bcolors.LIGHTBLUE}Extracting PSSH from manifest...{bcolors.ENDC}")
            try:
                playback.pssh = get_pssh_from_manifest(playback.manifest_url)
                print(f"{icons.ICON_SUCCESS} {bcolors.OKGREEN}PSSH extracted{bcolors.ENDC}")
            except Exception as exc:
                print(f"{icons.ICON_WARNING} {bcolors.WARNING}Could not extract PSSH: {exc}{bcolors.ENDC}")
        if playback.license_url and playback.pssh:
            print(f"{icons.ICON_WAITING} {bcolors.LIGHTBLUE}Getting decryption keys...{bcolors.ENDC}")
            keys = get_keys(playback)
            if keys:
                print(f"{icons.ICON_SUCCESS} {bcolors.OKGREEN}Retrieved {len(keys)} keys{bcolors.ENDC}")
            else:
                print(f"{icons.ICON_WARNING} {bcolors.WARNING}No keys retrieved{bcolors.ENDC}")
        else:
            print(f"{icons.ICON_WARNING} {bcolors.WARNING}Missing PSSH or License URL - cannot get keys{bcolors.ENDC}")
    elif playback.manifest_type != "m3u8":
        raise ValueError(f"Unsupported manifest type: {playback.manifest_type}")

    resolution = get_resolution(playback)
    filename = apply_quality_to_filename(format_filename(metadata, resolution), quality)
    include_subtitles = not ENABLE_TRANSLATION
    command = build_download_command(playback, filename, keys, mode=mode, quality=quality, include_subtitles=include_subtitles)
    return playback, keys, command, filename


def process_video(video_url, mode="auto", auto_download=False, info=False, quality=None, auto_confirm=False, save_native_subs=False):
    playback, keys, command, filename = resolve_video(video_url, mode=mode, quality=quality)
    print_playback_details(playback, keys, command, filename, info=info)

    if info:
        return

    if save_native_subs:
        save_native_subtitles(playback, filename)

    if ENABLE_TRANSLATION:
        maybe_save_translated_subtitles(playback, filename, auto_download=auto_download)

    maybe_download(command, auto_download=auto_download, auto_confirm=auto_confirm)


def info(video_url, quality=None):
    if not is_episode_url(video_url):
        raise ValueError(f"Info mode requires a {SERVICE_NAME} episode/video URL, not a series URL.")
    process_video(video_url, mode="info", info=True, quality=quality)


def download_selected_episodes(series_url, selector, quality=None, auto_confirm=False, save_native_subs=False):
    print(f"{icons.ICON_WAITING} {bcolors.LIGHTBLUE}Retrieving series information.....{bcolors.ENDC}")
    episode_items = select_episode_items(series_url, selector)
    print_download_queue(episode_items)

    if not confirm_download(f"\nDownload {len(episode_items)} episode(s)? Y or N: ", auto_confirm=auto_confirm):
        print(f"{icons.ICON_FAILURE} {bcolors.RED}Download cancelled{bcolors.ENDC}")
        return

    for index, item in enumerate(episode_items, start=1):
        _, title = episode_tree_label(item)
        title = title or format_queue_selector(item.season, item.episode)
        print(f"\n{icons.ICON_INFO} {bcolors.LIGHTBLUE}Downloading {index}/{len(episode_items)}: {title}{bcolors.ENDC}")
        process_video(item.url, mode="auto", auto_download=True, quality=quality, auto_confirm=auto_confirm, save_native_subs=save_native_subs)


def translate_to_target_language(text):
    text = clean_text(text)
    if not text:
        return ""
    try:
        return translate_text(text) or text
    except Exception as exc:
        print(f"{icons.ICON_WARNING} {bcolors.WARNING}Translation failed, keeping source text: {exc}{bcolors.ENDC}")
        return text


def translate_text(text):
    params = {
        "client": "gtx",
        "sl": SOURCE_LANGUAGE_CODE,
        "tl": TARGET_LANGUAGE_CODE,
        "dt": "t",
        "q": text,
    }
    response = session.get(TRANSLATE_URL, params=params, headers=DEFAULT_HEADERS, timeout=30)
    response.raise_for_status()
    payload = response.json()
    return clean_text("".join(part[0] for part in payload[0] if part and part[0]))


def translate_texts_batch(texts):
    clean_texts = [clean_text(text) for text in texts]
    if not clean_texts:
        return []
    if len(clean_texts) == 1:
        return [translate_text(clean_texts[0])]

    joined = TRANSLATE_BATCH_MARKER.join(clean_texts)
    try:
        translated = translate_text(joined)
        parts = [clean_text(part) for part in translated.split(TRANSLATE_BATCH_MARKER)]
        if len(parts) == len(clean_texts):
            return parts
    except Exception:
        if len(clean_texts) <= 2:
            raise

    midpoint = len(clean_texts) // 2
    return translate_texts_batch(clean_texts[:midpoint]) + translate_texts_batch(clean_texts[midpoint:])


def strip_subtitle_tags(text):
    text = re.sub(r"<[^>]+>", "", text)
    return html.unescape(clean_text(text))


def vtt_time_to_srt(value):
    value = value.strip()
    if value.count(":") == 1:
        value = "00:" + value
    return value.replace(".", ",")


def ttml_time_to_srt(value):
    value = value.strip()
    if value.endswith("s"):
        total = float(value[:-1])
        hours = int(total // 3600)
        minutes = int((total % 3600) // 60)
        seconds = int(total % 60)
        millis = int(round((total - int(total)) * 1000))
        return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"
    return vtt_time_to_srt(value)


def parse_vtt(vtt_text):
    vtt_text = vtt_text.replace("\ufeff", "").replace("\r\n", "\n").replace("\r", "\n")
    cues = []
    for block in re.split(r"\n{2,}", vtt_text.strip()):
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        if not lines or lines[0].upper().startswith("WEBVTT"):
            continue
        time_index = next((index for index, line in enumerate(lines) if "-->" in line), None)
        if time_index is None:
            continue
        start, end = [part.strip().split()[0] for part in lines[time_index].split("-->", 1)]
        text = strip_subtitle_tags(" ".join(lines[time_index + 1:]))
        if text:
            cues.append({"start": vtt_time_to_srt(start), "end": vtt_time_to_srt(end), "text": text})
    return cues


def parse_srt(srt_text):
    srt_text = srt_text.replace("\ufeff", "").replace("\r\n", "\n").replace("\r", "\n")
    cues = []
    for block in re.split(r"\n{2,}", srt_text.strip()):
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        time_line = next((line for line in lines if "-->" in line), None)
        if not time_line:
            continue
        start, end = [part.strip().split()[0] for part in time_line.split("-->", 1)]
        text_lines = lines[lines.index(time_line) + 1:]
        text = strip_subtitle_tags(" ".join(text_lines))
        if text:
            cues.append({"start": vtt_time_to_srt(start), "end": vtt_time_to_srt(end), "text": text})
    return cues


def parse_ttml(ttml_text):
    ttml_text = ttml_text.replace("\ufeff", "")
    cues = []
    pattern = r"<p\b[^>]*\bbegin=[\"']([^\"']+)[\"'][^>]*\bend=[\"']([^\"']+)[\"'][^>]*>(.*?)</p>"
    for match in re.finditer(pattern, ttml_text, flags=re.DOTALL | re.IGNORECASE):
        text = strip_subtitle_tags(match.group(3))
        if text:
            cues.append({"start": ttml_time_to_srt(match.group(1)), "end": ttml_time_to_srt(match.group(2)), "text": text})
    return cues


def parse_subtitle_text(subtitle_text, source_url=""):
    head = subtitle_text[:300].upper()
    if "WEBVTT" in head or source_url.lower().endswith(".vtt"):
        return parse_vtt(subtitle_text)
    if "<TT" in head or "<TT:" in head or "<P " in head:
        return parse_ttml(subtitle_text)
    return parse_srt(subtitle_text)


def vtt_segment_urls(playlist_url, playlist_text):
    urls = []
    for line in playlist_text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if ".vtt" in line.lower() or ".webvtt" in line.lower():
            urls.append(urljoin(playlist_url, line))
    return urls


def fetch_subtitle_text(subtitle):
    url = subtitle_url(subtitle)
    if not url:
        return ""

    text = fetch_text(url, headers={"Accept": "text/vtt,application/ttml+xml,application/x-subrip,*/*"})
    if ".m3u8" in url.lower() or "#EXTM3U" in text[:100].upper():
        segments = vtt_segment_urls(url, text)
        if segments:
            print(f"{bcolors.LIGHTBLUE}{icons.ICON_WAITING} Fetching {len(segments)} subtitle segments...{bcolors.ENDC}")
            return "\n\n".join(fetch_text(segment, headers={"Accept": "text/vtt,text/plain,*/*"}) for segment in segments)
    return text


def progress_bar(done, total, width=30):
    total = max(total, 1)
    filled = int(width * done / total)
    return "[" + "#" * filled + "-" * (width - filled) + f"] {done}/{total}"


def translate_cues(cues, show_progress=True):
    translated = []
    batches = [cues[index:index + TRANSLATE_BATCH_SIZE] for index in range(0, len(cues), TRANSLATE_BATCH_SIZE)]
    total_batches = len(batches)
    if show_progress:
        print(f"\r{bcolors.LIGHTBLUE}{progress_bar(0, total_batches)}{bcolors.ENDC}", end="", flush=True)

    for batch_index, batch in enumerate(batches, start=1):
        start = len(translated) + 1
        end = start + len(batch) - 1
        try:
            translated_texts = translate_texts_batch([cue["text"] for cue in batch])
        except Exception as exc:
            if show_progress:
                print()
            print(
                f"{bcolors.WARNING}{icons.ICON_WARNING} Subtitle batch translation failed at cues "
                f"{start}-{end}: {exc}{bcolors.ENDC}"
            )
            translated_texts = [cue["text"] for cue in batch]

        for cue, text in zip(batch, translated_texts):
            translated.append({**cue, "text": text})
        if show_progress:
            print(f"\r{bcolors.LIGHTBLUE}{progress_bar(batch_index, total_batches)}{bcolors.ENDC}", end="", flush=True)

    if show_progress:
        print()
    return translated


def write_srt(cues, output_path):
    lines = []
    for index, cue in enumerate(cues, start=1):
        lines.extend([str(index), f"{cue['start']} --> {cue['end']}", cue["text"], ""])
    Path(output_path).write_text("\n".join(lines), encoding="utf-8")


def subtitle_url(subtitle):
    if isinstance(subtitle, str):
        return subtitle
    if not isinstance(subtitle, dict):
        return ""
    for key in ("url", "href", "uri", "src", "file", "link"):
        value = clean_text(subtitle.get(key))
        if value:
            return value
    return ""


def subtitle_preference_score(subtitle):
    if isinstance(subtitle, str):
        text = subtitle.lower()
    else:
        text = json.dumps(subtitle, ensure_ascii=False).lower()

    score = 0
    if SOURCE_LANGUAGE_CODE and SOURCE_LANGUAGE_CODE != "auto" and SOURCE_LANGUAGE_CODE.lower() in text:
        score += 50
    if SOURCE_LANGUAGE_NAME and SOURCE_LANGUAGE_NAME.lower() in text:
        score += 40
    if ".vtt" in text or "webvtt" in text:
        score += 20
    if ".ttml" in text or ".dfxp" in text or "ttml" in text:
        score += 15
    if subtitle_url(subtitle):
        score += 10
    return score


def get_subtitle(playback):
    subtitles = [subtitle for subtitle in playback.subtitles or [] if subtitle_url(subtitle)]
    if not subtitles:
        return None
    subtitles.sort(key=subtitle_preference_score, reverse=True)
    return subtitles[0]


def subtitle_language_suffix(subtitle):
    if isinstance(subtitle, dict):
        language = clean_text(subtitle.get("language") or subtitle.get("languageCode")).lower()
        if language in {"de", "deu", "ger", "german", "deutsch"}:
            return "de"
        if language:
            suffix = re.sub(r"[^a-z0-9-]", "", language.replace("_", "-")).split("-", 1)[0]
            if suffix:
                return suffix
    return SOURCE_LANGUAGE_CODE or "native"


def save_native_subtitles(playback, filename):
    subtitle = get_subtitle(playback)
    if not subtitle:
        print(
            f"{bcolors.WARNING}{icons.ICON_WARNING} No external {SOURCE_LANGUAGE_NAME} subtitle URL found "
            f"for {SERVICE_NAME}.{bcolors.ENDC}"
        )
        return None

    subtitle_text = fetch_subtitle_text(subtitle)
    cues = parse_subtitle_text(subtitle_text, subtitle_url(subtitle))
    if not cues:
        print(f"{bcolors.WARNING}{icons.ICON_WARNING} No subtitle cues found for native subtitle save.{bcolors.ENDC}")
        return None

    output_path = SAVE_PATH / f"{filename}.{subtitle_language_suffix(subtitle)}.srt"
    print(f"{bcolors.LIGHTBLUE}{icons.ICON_WAITING} Saving {SOURCE_LANGUAGE_NAME} subtitles as SRT...{bcolors.ENDC}")
    write_srt(cues, output_path)
    print(f"{bcolors.OKGREEN}{icons.ICON_SUCCESS} Native subtitles saved: {bcolors.ENDC}{output_path}")
    return output_path


def save_translated_subtitles(playback, filename):
    subtitle = get_subtitle(playback)
    if not subtitle:
        print(
            f"{bcolors.WARNING}{icons.ICON_WARNING} No external {SOURCE_LANGUAGE_NAME} subtitle URL found "
            f"for {SERVICE_NAME}.{bcolors.ENDC}"
        )
        return None

    subtitle_text = fetch_subtitle_text(subtitle)
    cues = parse_subtitle_text(subtitle_text, subtitle_url(subtitle))
    if not cues:
        print(f"{bcolors.WARNING}{icons.ICON_WARNING} No subtitle cues found for translation.{bcolors.ENDC}")
        return None

    output_path = SAVE_PATH / f"{filename}.{TARGET_LANGUAGE_CODE}.srt"
    print(
        f"{bcolors.LIGHTBLUE}{icons.ICON_WAITING} Translating {SOURCE_LANGUAGE_NAME} subtitles "
        f"to {TARGET_LANGUAGE_NAME} SRT...{bcolors.ENDC}"
    )
    write_srt(translate_cues(cues), output_path)
    print(f"{bcolors.OKGREEN}{icons.ICON_SUCCESS} {TARGET_LANGUAGE_NAME} subtitles saved: {bcolors.ENDC}{output_path}")
    return output_path


def maybe_save_translated_subtitles(playback, filename, auto_download=False):
    if not ENABLE_TRANSLATION:
        return None

    if auto_download:
        return save_translated_subtitles(playback, filename)

    try:
        user_input = input(f"Do you wish to save translated {TARGET_LANGUAGE_NAME} subtitles? Y or N: ").strip().lower()
    except EOFError:
        user_input = "n"

    if user_input != "y":
        print(f"{icons.ICON_FAILURE} {bcolors.RED}Subtitle translation skipped{bcolors.ENDC}")
        return None

    return save_translated_subtitles(playback, filename)


def normalize_main_options(optional_args, quality=None, auto_confirm=False, auto_download=False, save_native_subs=False):
    if len(optional_args) == 2:
        quality, auto_confirm = optional_args
    elif len(optional_args) == 3:
        first, second, third = optional_args
        if isinstance(first, bool) and not isinstance(second, bool):
            auto_download, quality, auto_confirm = optional_args
        else:
            quality, auto_confirm, save_native_subs = optional_args
    elif len(optional_args) == 4:
        auto_download, quality, auto_confirm, save_native_subs = optional_args
    elif len(optional_args) > 4:
        raise TypeError(f"Unexpected trailing service arguments: {optional_args!r}")

    return normalize_quality(quality) if quality else None, bool(auto_confirm), bool(auto_download), bool(save_native_subs)


def main(
    video_url,
    downloads_path,
    wvd_device_path=None,
    mode="auto",
    export_list=False,
    download_selector=None,
    *optional_args,
    quality=None,
    auto_confirm=False,
    auto_download=False,
):
    global SAVE_PATH, WVD_PATH
    SAVE_PATH = Path(downloads_path)
    WVD_PATH = wvd_device_path
    quality, auto_confirm, auto_download, save_native_subs = normalize_main_options(optional_args, quality, auto_confirm, auto_download)

    if mode == "list":
        if is_episode_url(video_url):
            list_episode_items([collect_episode_item(video_url)], export_list=export_list, series_url=video_url)
        else:
            print(f"{icons.ICON_WAITING} {bcolors.LIGHTBLUE}Retrieving series information.....{bcolors.ENDC}")
            list_episode_items(collect_episode_items(video_url, show_progress=False), export_list=export_list, series_url=video_url)
        return

    if mode == "download":
        if is_episode_url(video_url):
            print(
                f"{icons.ICON_FAILURE} {bcolors.FAIL}Download selector mode requires a "
                f"{SERVICE_NAME} series URL, not an episode URL.{bcolors.ENDC}"
            )
            return
        download_selected_episodes(video_url, download_selector, quality=quality, auto_confirm=auto_confirm, save_native_subs=save_native_subs)
        return

    if mode == "info":
        info(video_url, quality=quality)
        return

    if is_episode_url(video_url):
        process_video(
            video_url,
            mode=mode,
            auto_download=auto_download or auto_confirm,
            quality=quality,
            auto_confirm=auto_confirm,
            save_native_subs=save_native_subs,
        )
        return

    print(
        f"{icons.ICON_WARNING} {bcolors.WARNING}Series URLs require a flag. Use --list/-l "
        f"to list episodes or --download/-d SELECTOR to download selected episodes.{bcolors.ENDC}"
    )
