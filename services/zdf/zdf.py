import argparse
import html
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs, urljoin, urlparse

import requests
import urllib3

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


SERVICE_KEY = "zdf"
SERVICE_NAME = "ZDF"
SERVICE_TAG = "ZDF"
SERVICE_DISPLAY_NAME = SERVICE_NAME
SERVICE_URL_PREFIXES = ("https://www.zdf.de", "https://zdf.de")
BASE_URL = "https://www.zdf.de"
API_BASE_URL = "https://api.zdf.de"
GRAPHQL_URL = "https://api.zdf.de/graphql"
TOKEN_URL = "https://zdf-prod-futura.zdf.de/mediathekV2/token"

COLLECTION_HASH = "cb49420e133bd668ad895a8cea0e65cba6aa11ac1cacb02341ff5cf32a17cd02"
SEASON_HASH = "9412a0f4ac55dc37d46975d461ec64bfd14380d815df843a1492348f77b5c99a"
PLAYER_TYPES = ("android_native_5", "smarttv_6", "ngplayer_2_5")

N_M3U8DL = "N_m3u8DL-RE"
DEFAULT_VIDEO_SELECTOR = "best"
DEFAULT_SUBTITLE_SELECTOR = "all"
DEFAULT_MUX_OPTIONS = "format=mkv:muxer=mkvmerge"

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

VIDEO_BY_CANONICAL_QUERY = """
query VideoByCanonical($canonical: String!) {
  videoByCanonical(canonical: $canonical) {
    id
    canonical
    title
    contentType
    sharingUrl
    leadParagraph
    editorialDate
    currentMediaType
    availability {
      fskBlocked
      vod {
        visibleFrom
        visibleTo
        endDate
        fsk
      }
    }
    teaser {
      title
      description
      imageWithoutLogo {
        layouts {
          original
          dim1920X1080
          dim1280X720
          dim768X432
          dim384X216
        }
      }
    }
    episodeInfo {
      seasonNumber
      episodeNumber
    }
    smartCollection {
      canonical
      title
    }
    streamingOptions {
      ad
      dgs
      fsk
      ks
      ov
      uhd
      ut
    }
    structuralMetadata {
      publicationFormInfo {
        original
        transformed
      }
    }
    currentMedia {
      nodes {
        id
        ... on VodMedia {
          duration
          visible
          geoLocation
          highestVerticalResolution
          vodMediaType
          ptmdTemplate
          contentType
          label
        }
      }
    }
  }
}
"""


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
    streams: list[dict[str, Any]] = field(default_factory=list)
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
    text = re.sub(r"<script\b[^>]*>.*?</script>", " ", text, flags=re.I | re.S)
    text = re.sub(r"<style\b[^>]*>.*?</style>", " ", text, flags=re.I | re.S)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def fetch_json(url, method="GET", headers=None, params=None, data=None, json_body=None, timeout=35):
    request_headers = dict(DEFAULT_HEADERS)
    if headers:
        request_headers.update(headers)
    response = session.request(
        method,
        url,
        headers=request_headers,
        params=params,
        data=data,
        json=json_body,
        timeout=timeout,
    )
    response.raise_for_status()
    return response.json()


def fetch_text(url, headers=None, timeout=35):
    request_headers = dict(DEFAULT_HEADERS)
    if headers:
        request_headers.update(headers)
    response = session.get(url, headers=request_headers, timeout=timeout)
    response.raise_for_status()
    response.encoding = response.encoding or "utf-8"
    return response.text


def get_api_auth():
    token = fetch_json(TOKEN_URL)
    token_type = token.get("type")
    token_value = token.get("token")
    if not token_type or not token_value:
        raise ValueError("Could not retrieve a ZDF API token.")
    return f"{token_type} {token_value}"


def graphql_headers(api_auth):
    return {
        "Accept": "application/json,text/plain,*/*",
        "Api-Auth": api_auth,
        "Apollo-Require-Preflight": "true",
    }


def fetch_graphql_get(api_auth, operation_name, variables, sha256_hash):
    params = {
        "operationName": operation_name,
        "variables": json.dumps(variables, separators=(",", ":")),
        "extensions": json.dumps(
            {"persistedQuery": {"version": 1, "sha256Hash": sha256_hash}},
            separators=(",", ":"),
        ),
    }
    payload = fetch_json(GRAPHQL_URL, headers=graphql_headers(api_auth), params=params)
    if payload.get("errors"):
        raise RuntimeError(f"ZDF GraphQL returned errors: {payload['errors']}")
    return payload.get("data") or {}


def fetch_graphql_post(api_auth, operation_name, query, variables):
    payload = fetch_json(
        GRAPHQL_URL,
        method="POST",
        headers=graphql_headers(api_auth),
        json_body={"operationName": operation_name, "query": query, "variables": variables},
    )
    if payload.get("errors"):
        raise RuntimeError(f"ZDF GraphQL returned errors: {payload['errors']}")
    return payload.get("data") or {}


def canonical_url(value):
    value = clean_text(value)
    if re.match(r"^https?://", value, re.IGNORECASE):
        return value
    if value.startswith("/"):
        return urljoin(BASE_URL, value)
    return urljoin(BASE_URL, f"/{value.strip('/')}")


def parse_url(input_url):
    source_url = canonical_url(input_url)
    parsed = urlparse(source_url)
    host = parsed.netloc.lower()
    if not (host == "zdf.de" or host.endswith(".zdf.de")):
        raise ValueError("Expected a zdf.de URL.")

    parts = [part for part in parsed.path.split("/") if part]
    if not parts:
        raise ValueError("Could not extract a ZDF canonical slug from the URL.")

    query = parse_qs(parsed.query)
    requested_season = None
    for key in ("staffel", "season"):
        value = (query.get(key) or [""])[-1]
        if str(value).isdigit():
            requested_season = int(value)
            break

    first = parts[0].lower()
    if first in {"video", "play"}:
        canonical = re.sub(r"\.html$", "", parts[-1], flags=re.IGNORECASE)
        is_video = True
    elif first == "serien" and len(parts) == 2:
        canonical = re.sub(r"\.html$", "", parts[1], flags=re.IGNORECASE)
        is_video = False
    else:
        canonical = re.sub(r"\.html$", "", parts[-1], flags=re.IGNORECASE)
        is_video = True

    if not canonical:
        raise ValueError("Could not extract a ZDF canonical slug from the URL.")

    return {
        "source_url": source_url,
        "canonical": canonical,
        "requested_season": requested_season,
        "is_video": is_video,
    }


def extract_video_id(video_url):
    return parse_url(video_url)["canonical"]


def is_episode_url(video_url):
    return parse_url(video_url)["is_video"]


def format_date(value):
    value = clean_text(value)
    if not value:
        return "Unknown"
    for date_format in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d"):
        try:
            parsed = datetime.strptime(value, date_format)
            return parsed.strftime("%d %B %Y %I:%M %p")
        except ValueError:
            continue
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.strftime("%d %B %Y %I:%M %p")
    except ValueError:
        return value


def sort_date(value):
    value = clean_text(value)
    if not value:
        return datetime.min
    for date_format in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, date_format).replace(tzinfo=None)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return datetime.min


def duration_seconds(video):
    for node in current_media_nodes(video):
        duration = node.get("duration")
        if duration:
            return duration
    return 0


def current_media_nodes(video):
    return ((video.get("currentMedia") or {}).get("nodes") or [])


def video_title(video):
    teaser = video.get("teaser") or {}
    return clean_text(teaser.get("title") or video.get("title")) or "Unknown Title"


def show_title(video, collection_title=""):
    collection = video.get("smartCollection") or {}
    return clean_text(collection.get("title") or collection_title) or video_title(video)


def source_description(video):
    teaser = video.get("teaser") or {}
    return clean_text(video.get("leadParagraph") or teaser.get("description")) or "No Description"


def episode_numbers(video):
    info = video.get("episodeInfo") or {}
    season = info.get("seasonNumber")
    episode = info.get("episodeNumber")
    try:
        season = int(season) if season is not None else None
    except (TypeError, ValueError):
        season = None
    try:
        episode = int(episode) if episode is not None else None
    except (TypeError, ValueError):
        episode = None
    return season, episode


def is_movie_video(video):
    content_type = clean_text(video.get("contentType") or video.get("currentMediaType")).upper()
    publication = ((video.get("structuralMetadata") or {}).get("publicationFormInfo") or {})
    return content_type in {"MOVIE", "FILM"} or clean_text(publication.get("original")).lower() == "film"


def zdf_video_id(video):
    return clean_text(video.get("canonical") or video.get("id"))


def video_url(video, collection_canonical=""):
    url = clean_text(video.get("sharingUrl"))
    if url:
        return canonical_url(url)
    canonical = zdf_video_id(video)
    if collection_canonical:
        return f"{BASE_URL}/video/serien/{collection_canonical}/{canonical}"
    return f"{BASE_URL}/video/{canonical}"


def fetch_single_video(api_auth, canonical):
    data = fetch_graphql_post(api_auth, "VideoByCanonical", VIDEO_BY_CANONICAL_QUERY, {"canonical": canonical})
    video = data.get("videoByCanonical")
    if not video:
        raise ValueError("Could not find a ZDF video for this URL.")
    return video


def extract_page_video_canonical(page_url):
    try:
        page_text = fetch_text(page_url, headers={"Accept": "text/html,*/*"})
    except Exception:
        return ""

    normalized = page_text.replace('\\"', '"')
    for pattern in (
        r'"heroVideo"\s*:\s*\{.*?"canonical"\s*:\s*"([^"]+)"',
        r'"video"\s*:\s*\{.*?"canonical"\s*:\s*"([^"]+)"',
    ):
        match = re.search(pattern, normalized, flags=re.DOTALL)
        if match:
            return clean_text(match.group(1))
    return ""


def fetch_video_for_url(api_auth, url_info):
    try:
        return fetch_single_video(api_auth, url_info["canonical"])
    except ValueError as original_error:
        if not url_info.get("is_video"):
            raise
        fallback_canonical = extract_page_video_canonical(url_info["source_url"])
        if fallback_canonical and fallback_canonical != url_info["canonical"]:
            return fetch_single_video(api_auth, fallback_canonical)
        raise original_error


def build_metadata(video, collection_title="", collection_canonical="", translate=False):
    season, episode = episode_numbers(video)
    title = show_title(video, collection_title)
    episode_title = video_title(video)
    if is_movie_video(video) or title == episode_title:
        title = episode_title
        episode_title = None

    description = source_description(video)
    if translate and description != "No Description":
        description = translate_to_target_language(description)

    aired_raw = video.get("editorialDate")
    year = None
    if clean_text(aired_raw)[:4].isdigit():
        year = int(clean_text(aired_raw)[:4])

    return Metadata(
        title=title,
        season=season,
        episode=episode,
        episode_title=episode_title,
        description=description,
        aired_date=format_date(aired_raw),
        video_id=zdf_video_id(video),
        year=year,
        content_type=clean_text(video.get("contentType") or video.get("currentMediaType")),
        source_language=SOURCE_LANGUAGE_CODE,
    )


def search_metadata(video_url, video_id):
    api_auth = get_api_auth()
    url_info = parse_url(video_url)
    video = fetch_video_for_url(api_auth, url_info)
    return build_metadata(video, translate=False)


def fetch_collection(api_auth, canonical, page_size):
    data = fetch_graphql_get(
        api_auth,
        "GetSmartCollectionByCanonical",
        {"canonical": canonical, "videoPageSize": page_size},
        COLLECTION_HASH,
    )
    collection = data.get("smartCollectionByCanonical")
    if not collection:
        raise ValueError("Could not find a ZDF series/show for this URL.")
    return collection


def fetch_season_page(api_auth, canonical, season_index, page_size, cursor=None):
    data = fetch_graphql_get(
        api_auth,
        "seasonByCanonical",
        {
            "seasonIndex": season_index,
            "canonical": canonical,
            "episodesPageSize": page_size,
            "episodesAfter": cursor,
        },
        SEASON_HASH,
    )
    collection = data.get("smartCollectionByCanonical") or {}
    nodes = ((collection.get("seasons") or {}).get("nodes") or [])
    return nodes[0] if nodes else {}


def videos_from_episode_connection(connection):
    return connection.get("videos") or connection.get("nodes") or []


def fetch_all_season_videos(api_auth, canonical, season_index, season_number, page_size):
    videos = []
    cursor = None
    while True:
        season = fetch_season_page(api_auth, canonical, season_index, page_size, cursor)
        if season_number and season.get("number") != season_number:
            break
        episodes = season.get("episodes") or {}
        page_videos = videos_from_episode_connection(episodes)
        if not page_videos:
            break
        videos.extend(page_videos)
        page_info = episodes.get("pageInfo") or {}
        if not page_info.get("hasNextPage") or not page_info.get("endCursor"):
            break
        cursor = page_info.get("endCursor")
    return videos


def collect_show_videos(api_auth, canonical, requested_season=None, page_size=100, show_progress=True):
    collection = fetch_collection(api_auth, canonical, page_size)
    collection_title = clean_text(collection.get("title")) or "Unknown Show"
    collection_canonical = clean_text(collection.get("canonical")) or canonical
    seasons_root = collection.get("seasons") or {}
    seasons = seasons_root.get("seasons") or seasons_root.get("nodes") or []

    if show_progress:
        season_labels = [str(season.get("number")) for season in seasons if season.get("number") is not None]
        print(f"{bcolors.OKGREEN}Show: {collection_title}{bcolors.ENDC}")
        if season_labels:
            print(f"{bcolors.OKGREEN}Seasons: {', '.join(season_labels)}{bcolors.ENDC}")

    videos = []
    for season_index, season in enumerate(seasons):
        season_number = season.get("number")
        if requested_season is not None and season_number != requested_season:
            continue
        episodes = season.get("episodes") or {}
        season_videos = videos_from_episode_connection(episodes)
        page_info = episodes.get("pageInfo") or {}
        count = int(season.get("countEpisodes") or len(season_videos) or 0)
        if page_info.get("hasNextPage") or (count and len(season_videos) < count):
            season_videos = fetch_all_season_videos(api_auth, canonical, season_index, season_number, page_size)
        if show_progress:
            print(f"Fetched season {season_number}: {len(season_videos)} rows")
        videos.extend(season_videos)

    if requested_season is not None and not videos:
        raise ValueError(f"Season {requested_season} was not found for this ZDF show.")
    return videos, collection_title, collection_canonical


def build_episode_item(video, collection_title="", collection_canonical=""):
    metadata = build_metadata(video, collection_title=collection_title, collection_canonical=collection_canonical)
    return EpisodeItem(
        url=video_url(video, collection_canonical),
        title=metadata.title,
        season=metadata.season,
        episode=metadata.episode,
        episode_title=metadata.episode_title,
        video_id=metadata.video_id,
        description=metadata.description,
        air_date=metadata.aired_date,
    )


def collect_episode_item(video_url_value):
    api_auth = get_api_auth()
    video = fetch_video_for_url(api_auth, parse_url(video_url_value))
    return build_episode_item(video, collection_title=show_title(video))


def collect_episode_items(series_url, show_progress=True):
    url_info = parse_url(series_url)
    api_auth = get_api_auth()
    if url_info["is_video"]:
        video = fetch_video_for_url(api_auth, url_info)
        return [build_episode_item(video, collection_title=show_title(video))]

    videos, collection_title, collection_canonical = collect_show_videos(
        api_auth,
        url_info["canonical"],
        requested_season=url_info["requested_season"],
        page_size=100,
        show_progress=show_progress,
    )

    episode_items = []
    seen = set()
    for video in videos:
        content_type = clean_text(video.get("contentType") or video.get("currentMediaType")).upper()
        if content_type not in {"", "EPISODE"}:
            continue
        video_id = zdf_video_id(video)
        if not video_id or video_id in seen:
            continue
        seen.add(video_id)
        episode_items.append(build_episode_item(video, collection_title, collection_canonical))

    if not episode_items:
        raise ValueError("No ZDF episodes were found.")
    return sorted(episode_items, key=episode_sort_key)


def ptmd_url(template, player_type):
    return urljoin(API_BASE_URL, template.format(playerId=player_type))


def fetch_ptmd(api_auth, template, player_type):
    return fetch_json(ptmd_url(template, player_type), headers=graphql_headers(api_auth))


def stream_type_from_url(url, mime_type):
    lowered = f"{url} {mime_type}".lower()
    if ".m3u8" in lowered or "mpegurl" in lowered:
        return "m3u8"
    if ".mp4" in lowered or "video/mp4" in lowered:
        return "mp4"
    if ".webm" in lowered or "video/webm" in lowered:
        return "webm"
    return "direct"


def collect_ptmd_streams(ptmd, player_type):
    streams = []
    for priority in ptmd.get("priorityList") or []:
        for media_format in priority.get("formitaeten") or []:
            facets = media_format.get("facets") or []
            if "restriction_useragent" in facets:
                continue
            mime_type = clean_text(media_format.get("mimeType"))
            for quality in media_format.get("qualities") or []:
                height = quality.get("highestVerticalResolution")
                audio = quality.get("audio") or {}
                for track in audio.get("tracks") or []:
                    track_class = clean_text(track.get("class") or "main")
                    if track_class not in {"main", "ot"}:
                        continue
                    url = clean_text(track.get("uri"))
                    if not url:
                        continue
                    manifest_type = stream_type_from_url(url, mime_type)
                    streams.append(
                        {
                            "url": url,
                            "manifest_type": manifest_type,
                            "height": int(height or 0),
                            "quality": clean_text(quality.get("quality")),
                            "codec": clean_text(quality.get("mimeCodec")) or "-",
                            "mime_type": mime_type,
                            "language": clean_text(track.get("language") or "-"),
                            "audio_class": track_class,
                            "filesize": track.get("filesize"),
                            "facets": ", ".join(str(facet) for facet in facets),
                            "player_type": player_type,
                        }
                    )
    return streams


def stream_score(stream):
    score = stream.get("height") or 0
    if stream.get("manifest_type") == "m3u8":
        score += 10000
    elif stream.get("manifest_type") == "mp4":
        score += 5000
    elif stream.get("manifest_type") == "webm":
        score += 2500
    if stream.get("audio_class") == "main":
        score += 500
    if stream.get("player_type") == "android_native_5":
        score += 100
    return score


def select_playback_stream(streams):
    candidates = [stream for stream in streams if stream.get("manifest_type") == "m3u8"]
    if not candidates:
        candidates = [stream for stream in streams if stream.get("manifest_type") in {"mp4", "webm", "direct"}]
    if not candidates:
        return None
    candidates.sort(key=stream_score, reverse=True)
    return candidates[0]


def collect_subtitles(ptmd):
    subtitles = []
    seen = set()
    for caption in ptmd.get("captions") or []:
        url = clean_text(caption.get("uri"))
        if not url or url in seen:
            continue
        seen.add(url)
        language = clean_text(caption.get("language") or "de")
        if language == "deu":
            language = "de"
        subtitles.append(
            {
                "url": url,
                "language": language,
                "label": "German",
                "kind": clean_text(caption.get("format") or "subtitle"),
                "class": clean_text(caption.get("class")),
            }
        )
    return subtitles


def get_playback_info(video_url_value, metadata):
    api_auth = get_api_auth()
    video = fetch_video_for_url(api_auth, parse_url(video_url_value))
    if ((video.get("availability") or {}).get("fskBlocked")):
        raise ValueError("This ZDF item is age-restricted and may only be available during the German watershed window.")

    playback_metadata = build_metadata(video, translate=False)
    streams = []
    subtitles = []
    ptmd_errors = []
    for node in current_media_nodes(video):
        if node.get("vodMediaType") not in (None, "DEFAULT"):
            continue
        template = clean_text(node.get("ptmdTemplate"))
        if not template:
            continue
        for player_type in PLAYER_TYPES:
            try:
                ptmd = fetch_ptmd(api_auth, template, player_type)
            except Exception as exc:
                ptmd_errors.append(f"{player_type}: {exc}")
                continue
            streams.extend(collect_ptmd_streams(ptmd, player_type))
            if not subtitles:
                subtitles = collect_subtitles(ptmd)

    selected = select_playback_stream(streams)
    if not selected:
        detail = f" Last PTMD errors: {'; '.join(ptmd_errors[-2:])}" if ptmd_errors else ""
        raise ValueError(f"No ZDF HLS/progressive stream URL found. This item may require a German proxy/VPN.{detail}")

    manifest_type = selected["manifest_type"]
    if manifest_type in {"mp4", "webm", "direct"}:
        manifest_type = "direct"

    return PlaybackInfo(
        manifest_url=selected["url"],
        manifest_type=manifest_type,
        metadata=playback_metadata,
        subtitles=subtitles,
        streams=streams,
    )


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
    title = item.episode_title or item.title or item.url
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
            episode_number_label, title = episode_tree_label(item)
            print(f"{bcolors.GRAY}{group_child_prefix}{branch} {episode_number_label}. {bcolors.ENDC}{title}")
            print(f"{bcolors.GRAY}{group_child_prefix}{url_branch} {bcolors.ENDC}{bcolors.LIGHTBLUE}{item.url}{bcolors.ENDC}")

    if export_list:
        output_path = export_episode_list_text(series_url or "", episode_items)
        print()
        print(f"{icons.ICON_SUCCESS} {bcolors.OKGREEN}Exported list: {output_path}{bcolors.ENDC}")


def parse_selector_part(selector_part):
    match = re.fullmatch(r"s(?P<season>\d{2}|\d{4})(?:e(?P<episode>\d{2,3}))?", selector_part)
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

    start_text, end_text = selector.split("-", 1)
    if not start_text or not end_text:
        raise ValueError("Download range must include both start and end selectors.")
    start = parse_selector_part(start_text)
    end = parse_selector_part(end_text)
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
    season_label = f"S{season:04d}" if season and season >= 1000 else f"S{int(season or 0):02d}"
    if episode is not None:
        return f"{season_label}E{int(episode):02d}"
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
        print(f"{selector} {title}")


def safe_windows_filename(value):
    value = clean_text(value).replace("'", "")
    value = re.sub(r'[\\/:*?"<>|]', " ", value)
    value = re.sub(r"\s+", ".", value)
    return value.strip(".") or "Unknown"


def export_episode_list_text(series_url, episode_items):
    export_dir = Path(__file__).resolve().parents[2] / "export"
    export_dir.mkdir(parents=True, exist_ok=True)
    slug = safe_windows_filename(Path((series_url or SERVICE_KEY).rstrip("/")).name or SERVICE_KEY)
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


def format_filename(metadata, resolution):
    title = safe_windows_filename(metadata.title)
    season_episode = ""
    if metadata.season is not None and metadata.episode is not None:
        season_episode = f"S{int(metadata.season):02d}E{int(metadata.episode):02d}"
    elif metadata.season is not None:
        season_episode = f"S{int(metadata.season):02d}"

    parts = [title]
    if season_episode:
        parts.append(season_episode)
    if metadata.episode_title:
        parts.append(safe_windows_filename(metadata.episode_title))
    if not season_episode and metadata.year:
        parts.append(str(metadata.year))
    parts.extend([resolution, SERVICE_TAG, "WEB-DL", "AAC2.0", "H.264"])
    return ".".join(part for part in parts if part and part != "Unknown")


def build_download_command(playback, filename, keys=None, mode="auto", quality=None, include_subtitles=True):
    if mode == "interactive":
        selectors = ""
        mux_options = "format=mkv"
    else:
        subtitle_selector = f"--select-subtitle {DEFAULT_SUBTITLE_SELECTOR}" if include_subtitles else "--drop-subtitle all"
        selectors = f'{video_selector(quality, default=DEFAULT_VIDEO_SELECTOR)} --drop-audio all {subtitle_selector} '
        mux_options = DEFAULT_MUX_OPTIONS

    command = (
        f'{N_M3U8DL} "{playback.manifest_url}" '
        f'{selectors}'
        f'-mt -M {mux_options} --save-dir "{SAVE_PATH}" --save-name "{filename}"'
    )

    if keys:
        command += " " + " ".join(f"--key {key}" for key in keys)
    return append_downloader_proxy(command)


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
        attrs[match.group(1).lower()] = value
    return attrs


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
            streams.append(
                {
                    "type": "Vid",
                    "resolution": attrs.get("resolution") or "-",
                    "bitrate": format_bitrate(attrs.get("average-bandwidth") or attrs.get("bandwidth")),
                    "codec": attrs.get("codecs") or "-",
                    "lang": "-",
                    "channels": "-",
                }
            )
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
        streams.append(
            {
                "type": stream_type,
                "resolution": "-",
                "bitrate": "-",
                "codec": "-",
                "lang": attrs.get("language") or "-",
                "channels": attrs.get("channels") or "-",
            }
        )
    return sorted(streams, key=stream_table_sort_key)


def stream_table_sort_key(stream):
    type_order = {"Vid": 0, "Aud": 1, "Sub": 2}
    resolution = stream.get("resolution") or ""
    match = re.search(r"x(\d+)", resolution)
    height = int(match.group(1)) if match else 0
    return (type_order.get(stream.get("type"), 9), -height, stream.get("lang") or "")


def fetch_manifest_text(playback):
    return fetch_text(playback.manifest_url, headers={"Accept": "application/x-mpegURL,text/plain,*/*"})


def playback_streams(playback):
    if playback.manifest_type == "m3u8":
        return parse_hls_streams(fetch_manifest_text(playback))

    rows = []
    for stream in playback.streams:
        rows.append(
            {
                "type": "Vid",
                "resolution": f"{stream.get('height')}p" if stream.get("height") else "-",
                "bitrate": filesize_text(stream.get("filesize")),
                "codec": stream.get("codec") or "-",
                "lang": stream.get("language") or "-",
                "channels": stream.get("manifest_type") or "-",
            }
        )
    return rows


def filesize_text(value):
    try:
        size = int(value)
    except (TypeError, ValueError):
        return "-"
    if size >= 1024 ** 3:
        return f"{size / (1024 ** 3):.2f} GB"
    if size >= 1024 ** 2:
        return f"{size / (1024 ** 2):.1f} MB"
    return f"{size // 1024} KB"


def get_hls_resolution(manifest_url):
    heights = []
    for line in fetch_text(manifest_url, headers={"Accept": "application/x-mpegURL,text/plain,*/*"}).splitlines():
        match = re.search(r"RESOLUTION=\d+x(\d+)", line, flags=re.IGNORECASE)
        if match:
            heights.append(int(match.group(1)))
    return f"{max(heights)}p" if heights else "best"


def get_resolution(playback):
    if playback.manifest_type == "m3u8":
        try:
            return get_hls_resolution(playback.manifest_url)
        except Exception:
            pass
    heights = [stream.get("height") for stream in playback.streams if stream.get("height")]
    return f"{max(heights)}p" if heights else "best"


def print_streams(streams):
    print(f"\n{bcolors.YELLOW}Available streams:{bcolors.ENDC}")
    if not streams:
        print("No video, audio, or subtitle streams were found.")
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
    widths = [min(max(len(headings[column]), *(len(row[column]) for row in rows)), 52) for column in range(len(headings))]
    widths[0] = 3
    print("  ".join(f"{heading:<{widths[index]}}" for index, heading in enumerate(headings)))
    print("  ".join("-" * width for width in widths))
    for row in rows:
        print("  ".join(f"{value[:widths[index]]:<{widths[index]}}" for index, value in enumerate(row)))


def print_external_subtitles(subtitles):
    if not subtitles:
        return
    print(f"\n{bcolors.YELLOW}External subtitles:{bcolors.ENDC}")
    for index, subtitle in enumerate(subtitles, start=1):
        print(
            f"  {index:02d}. "
            f"{clean_text(subtitle.get('language') or '-'):<6} "
            f"{clean_text(subtitle.get('kind') or subtitle.get('type') or 'subtitle'):<18} "
            f"{clean_text(subtitle.get('label') or subtitle.get('name') or '-')}"
        )


def print_episode_metadata(metadata):
    print(f"\n{bcolors.YELLOW}Episode metadata:{bcolors.ENDC}")
    print(f"{bcolors.LIGHTBLUE}Show: {bcolors.ENDC}{metadata.title or 'Unknown'}")
    if metadata.season is not None or metadata.episode is not None:
        print(f"{bcolors.LIGHTBLUE}Episode: {bcolors.ENDC}{format_queue_selector(metadata.season or 0, metadata.episode)}")
    print(f"{bcolors.LIGHTBLUE}Title: {bcolors.ENDC}{metadata.episode_title or metadata.title or 'Unknown'}")
    print(f"{bcolors.LIGHTBLUE}Date Aired: {bcolors.ENDC}{metadata.aired_date or 'Unknown'}")
    description = metadata.description or "No Description"
    if ENABLE_TRANSLATION and description != "No Description":
        description = translate_to_target_language(description)
    print(f"{bcolors.LIGHTBLUE}Description: {bcolors.ENDC}{description}")


def print_playback_details(playback, keys, command, filename, info=False):
    if info:
        label = "HLS Manifest URL" if playback.manifest_type == "m3u8" else "Direct Media URL"
        print(f"{bcolors.LIGHTBLUE}{label}: {bcolors.ENDC}{playback.manifest_url}")
        print_streams(playback_streams(playback))
        print_external_subtitles(playback.subtitles)
        print_episode_metadata(playback.metadata)
        print(f"\n{bcolors.YELLOW}Suggested filename: {bcolors.ENDC}{filename}.mkv")
        return

    label = "M3U8 URL" if playback.manifest_type == "m3u8" else "Direct Media URL"
    print(f"{bcolors.LIGHTBLUE}{label}: {bcolors.ENDC}{playback.manifest_url}")
    if playback.manifest_type == "direct":
        print(f"{icons.ICON_WARNING} {bcolors.WARNING}ZDF returned a progressive file URL. N_m3u8DL-RE may not handle direct files on every setup.{bcolors.ENDC}")

    print(f"{bcolors.YELLOW}DOWNLOAD COMMAND: {bcolors.ENDC}")
    print(mask_proxy_command(command))


def resolve_video(video_url_value, mode="auto", quality=None):
    print(f"{icons.ICON_INFO} {bcolors.LIGHTBLUE}Processing: {bcolors.ENDC}{video_url_value}")
    video_id = extract_video_id(video_url_value)
    metadata = search_metadata(video_url_value, video_id)

    print(f"{icons.ICON_WAITING} {bcolors.LIGHTBLUE}Fetching playback info...{bcolors.ENDC}")
    playback = get_playback_info(video_url_value, metadata)
    if playback.metadata and (playback.metadata.title != "Unknown" or playback.metadata.video_id):
        metadata = playback.metadata
    else:
        playback.metadata = metadata

    resolution = get_resolution(playback)
    filename = apply_quality_to_filename(format_filename(metadata, resolution), quality)
    include_subtitles = not ENABLE_TRANSLATION
    command = build_download_command(playback, filename, [], mode=mode, quality=quality, include_subtitles=include_subtitles)
    return playback, [], command, filename


def process_video(video_url_value, mode="auto", auto_download=False, info=False, quality=None, auto_confirm=False, save_native_subs=False):
    playback, keys, command, filename = resolve_video(video_url_value, mode=mode, quality=quality)
    print_playback_details(playback, keys, command, filename, info=info)
    if info:
        return

    if save_native_subs:
        save_native_subtitles(playback, filename)

    if ENABLE_TRANSLATION:
        maybe_save_translated_subtitles(playback, filename, auto_download=auto_download)

    maybe_download(command, auto_download=auto_download, auto_confirm=auto_confirm)


def info(video_url_value, quality=None):
    if not is_episode_url(video_url_value):
        raise ValueError(f"Info mode requires a {SERVICE_NAME} episode/video URL, not a series URL.")
    process_video(video_url_value, mode="info", info=True, quality=quality)


def download_selected_episodes(series_url, selector, quality=None, auto_confirm=False, save_native_subs=False):
    print(f"{icons.ICON_WAITING} {bcolors.LIGHTBLUE}Retrieving series information.....{bcolors.ENDC}")
    episode_items = select_episode_items(series_url, selector)
    print_download_queue(episode_items)

    if not confirm_download(f"\nDownload {len(episode_items)} episode(s)? Y or N: ", auto_confirm=auto_confirm):
        print(f"{icons.ICON_FAILURE} {bcolors.RED}Download cancelled{bcolors.ENDC}")
        return

    for index, item in enumerate(episode_items, start=1):
        _, title = episode_tree_label(item)
        print(f"\n{icons.ICON_INFO} {bcolors.LIGHTBLUE}Downloading {index}/{len(episode_items)}: {title}{bcolors.ENDC}")
        process_video(item.url, mode="auto", auto_download=True, quality=quality, auto_confirm=auto_confirm, save_native_subs=save_native_subs)


def maybe_download(command, auto_download=False, auto_confirm=False):
    if confirm_download("Do you wish to download? Y or N: ", auto_confirm=auto_confirm, auto_download=auto_download):
        print(f"{icons.ICON_WAITING} {bcolors.OKBLUE}Downloading video...{bcolors.ENDC}")
        subprocess.run(command, shell=True)
    else:
        print(f"{icons.ICON_FAILURE} {bcolors.RED}Download cancelled{bcolors.ENDC}")


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
    response = session.get(
        TRANSLATE_URL,
        params={"client": "gtx", "sl": SOURCE_LANGUAGE_CODE, "tl": TARGET_LANGUAGE_CODE, "dt": "t", "q": text},
        headers=DEFAULT_HEADERS,
        timeout=30,
    )
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


def fetch_subtitle_text(subtitle):
    return fetch_text(subtitle_url(subtitle), headers={"Accept": "text/vtt,application/ttml+xml,application/x-subrip,*/*"})


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
    if SOURCE_LANGUAGE_CODE and SOURCE_LANGUAGE_CODE.lower() in text:
        score += 50
    if "deu" in text:
        score += 45
    if SOURCE_LANGUAGE_NAME.lower() in text:
        score += 40
    if ".vtt" in text or "webvtt" in text:
        score += 20
    if ".ttml" in text or ".xml" in text or "ebu-tt" in text:
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
        print(f"{bcolors.WARNING}{icons.ICON_WARNING} No {SOURCE_LANGUAGE_NAME} subtitle cues found.{bcolors.ENDC}")
        return None

    output_path = SAVE_PATH / f"{filename}.{SOURCE_LANGUAGE_CODE}.srt"
    write_srt(cues, output_path)
    print(f"{bcolors.OKGREEN}{icons.ICON_SUCCESS} {SOURCE_LANGUAGE_NAME} subtitles saved: {bcolors.ENDC}{output_path}")
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
            auto_download, quality, auto_confirm = first, second, third
        else:
            quality, auto_confirm, save_native_subs = first, second, third
    elif len(optional_args) == 4:
        auto_download, quality, auto_confirm, save_native_subs = optional_args
    elif len(optional_args) > 3:
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
    save_native_subs=False,
):
    global SAVE_PATH, WVD_PATH
    SAVE_PATH = Path(downloads_path)
    WVD_PATH = wvd_device_path
    quality, auto_confirm, auto_download, save_native_subs = normalize_main_options(optional_args, quality, auto_confirm, auto_download, save_native_subs)

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
            mode="interactive" if mode == "interactive" else "auto",
            auto_download=auto_download or auto_confirm,
            quality=quality,
            auto_confirm=auto_confirm,
            save_native_subs=save_native_subs,
        )
        return

    print(
        f"{icons.ICON_WARNING} {bcolors.WARNING}Series URLs require a flag. Use --list/-l to list episodes, "
        f"--export/-x to export episode URLs, or --download/-d SELECTOR to download selected episodes.{bcolors.ENDC}"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Eurovine ZDF service")
    parser.add_argument("url")
    parser.add_argument("--info", "-i", action="store_true")
    parser.add_argument("--list", "-l", action="store_true")
    parser.add_argument("--download", "-d")
    parser.add_argument("--quality", "-q", type=normalize_quality)
    parser.add_argument("--yes", "-y", action="store_true")
    parser.add_argument("--subs", "-s", action="store_true")
    args = parser.parse_args()
    mode = "info" if args.info else "list" if args.list else "download" if args.download else "auto"
    main(args.url, os.getcwd(), None, mode, False, args.download, args.quality, args.yes, args.subs)
