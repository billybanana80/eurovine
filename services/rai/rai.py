import argparse
import base64
import binascii
import html
import json
import os
import re
import shlex
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs, quote, urlencode, urljoin, urlparse, urlsplit, urlunsplit

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

SERVICE_KEY = "rai"
SERVICE_NAME = "RaiPlay"
SERVICE_TAG = "RaiPlay"
SERVICE_DISPLAY_NAME = SERVICE_NAME
SERVICE_URL_PREFIXES = ("https://www.raiplay.it", "https://raiplay.it")
BASE_URL = "https://www.raiplay.it"
RELINKER_URL = "https://mediapolisvod.rai.it/relinker/relinkerServlet.htm"

N_M3U8DL = "N_m3u8DL-RE"
DEFAULT_VIDEO_SELECTOR = "best"
DEFAULT_AUDIO_SELECTOR = "best"
DEFAULT_SUBTITLE_SELECTOR = "all"

ENABLE_TRANSLATION = True
SOURCE_LANGUAGE_CODE = "it"
SOURCE_LANGUAGE_NAME = "Italian"
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
    "Accept-Language": "it-IT,it;q=0.9,en-US;q=0.8,en;q=0.7",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36"
    ),
}

session.headers.update(DEFAULT_HEADERS)
session.verify = False


@dataclass
class Metadata:
    title: str = "Unknown"
    season: Optional[int] = None
    episode: Optional[int] = None
    episode_title: Optional[str] = None
    description: Optional[str] = None
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
    streams: list[dict[str, str]] = field(default_factory=list)
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
    text = html.unescape(str(value or ""))
    text = re.sub(r"<script\b[^>]*>.*?</script>", " ", text, flags=re.I | re.S)
    text = re.sub(r"<style\b[^>]*>.*?</style>", " ", text, flags=re.I | re.S)
    text = re.sub(r"<br\s*/?>", " ", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def parse_int(value, default=None):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def fetch_json(url, method="GET", headers=None, params=None, data=None, json_body=None, timeout=30, attempts=4):
    request_headers = dict(DEFAULT_HEADERS)
    if headers:
        request_headers.update(headers)

    last_error = None
    for attempt in range(1, attempts + 1):
        try:
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
            try:
                return response.json()
            except ValueError:
                return json.loads(response.content.decode("latin-1"))
        except Exception as exc:
            last_error = exc
            if attempt < attempts:
                time.sleep(0.75 * attempt)
    raise last_error


def fetch_text(url, headers=None, timeout=30, attempts=4):
    request_headers = dict(DEFAULT_HEADERS)
    if headers:
        request_headers.update(headers)
    last_error = None
    for attempt in range(1, attempts + 1):
        try:
            response = session.get(url, headers=request_headers, timeout=timeout)
            response.raise_for_status()
            return response.text
        except Exception as exc:
            last_error = exc
            if attempt < attempts:
                time.sleep(0.75 * attempt)
    raise last_error


def canonical_url(value):
    value = clean_text(value)
    if not value:
        return ""
    if value.startswith("//"):
        return "https:" + value
    if re.match(r"^https?://", value, re.IGNORECASE):
        return value
    return urljoin(BASE_URL, value if value.startswith("/") else f"/{value.strip('/')}")


def base_url_without_query(url):
    parts = urlsplit(clean_text(url))
    return urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/"), "", ""))


def json_url_from_url(input_url):
    source_url = canonical_url(input_url)
    parsed = urlparse(source_url)
    if not parsed.netloc.lower().endswith("raiplay.it"):
        raise ValueError("Expected a raiplay.it URL.")

    path = parsed.path.rstrip("/")
    if path.endswith(".json"):
        json_path = path
    elif path.endswith(".html"):
        json_path = re.sub(r"\.html$", ".json", path)
    elif "/programmi/" in path:
        json_path = f"{path}.json"
    else:
        raise ValueError("Expected a RaiPlay programme or video URL.")

    return urljoin(BASE_URL, json_path)


def is_video_json_url(url):
    return "/video/" in urlparse(url).path


def rai_item_url(item):
    return canonical_url(item.get("weblink") or item.get("path_id") or item.get("url"))


def programme_sets(programme):
    sets = []
    for block in programme.get("blocks") or []:
        block_type = clean_text(block.get("type"))
        block_name = clean_text(block.get("name"))
        if block_name.lower() in {"clip", "extra"}:
            continue
        if block_type != "RaiPlay Multimedia Block":
            continue
        for content_set in block.get("sets") or []:
            if clean_text(content_set.get("path_id")) or clean_text(content_set.get("id")):
                sets.append({"block": block, "set": content_set})
    return sets


def is_playable_programme(programme):
    program_info = programme.get("program_info") or {}
    typology = clean_text(program_info.get("typology") or programme.get("typology")).lower()
    if typology == "film":
        return True
    return bool(clean_text(programme.get("first_item_path"))) and not programme_sets(programme)


def load_video_item(video_url):
    json_url = json_url_from_url(video_url)
    item = fetch_json(json_url, headers={"Accept": "application/json,text/plain,*/*"})
    if is_video_json_url(json_url):
        return item, base_url_without_query(canonical_url(video_url))

    first_item_path = clean_text(item.get("first_item_path"))
    if not first_item_path:
        raise ValueError("RaiPlay programme URL does not expose a playable first item.")
    video_item = fetch_json(canonical_url(first_item_path), headers={"Accept": "application/json,text/plain,*/*"})
    return video_item, canonical_url(first_item_path).replace(".json", ".html")


def extract_uuid(value):
    match = re.search(
        r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
        clean_text(value),
        re.IGNORECASE,
    )
    return match.group(1) if match else ""


def video_id_from_item(item):
    item_id = clean_text(item.get("id"))
    if item_id.startswith("ContentItem-"):
        return item_id.replace("ContentItem-", "", 1)

    for value in (item_id, item.get("path_id"), item.get("weblink"), item.get("info_url")):
        item_uuid = extract_uuid(value)
        if item_uuid:
            return item_uuid
    return item_id


def programme_title(item, fallback="RaiPlay"):
    program_info = item.get("program_info") or {}
    parent_page = item.get("parent_page") or {}
    return clean_text(
        item.get("program_name")
        or program_info.get("name")
        or program_info.get("program_title")
        or program_info.get("title")
        or parent_page.get("name")
        or fallback
    )


def episode_title_from_item(item):
    return clean_text(item.get("episode_title") or item.get("name") or item.get("toptitle") or "Unknown Title")


def source_description(item):
    return clean_text(item.get("description") or item.get("vanity") or "No Description")


def season_number_from_item(item):
    season = parse_int(item.get("season"))
    if season is not None:
        return season
    for value in (item.get("subtitle"), item.get("aria_label"), item.get("toptitle"), item.get("name"), item.get("weblink")):
        match = re.search(r"(?:Stagione|St|S)\s*0*(\d+)", clean_text(value), re.IGNORECASE)
        if match:
            return int(match.group(1))
    return None


def episode_number_from_item(item):
    episode = parse_int(item.get("episode"))
    if episode is not None:
        return episode
    for value in (item.get("subtitle"), item.get("aria_label"), item.get("toptitle"), item.get("name"), item.get("weblink")):
        match = re.search(r"(?:Episodio|Ep|E)\s*0*(\d+)", clean_text(value), re.IGNORECASE)
        if match:
            return int(match.group(1))
    return None


def metadata_from_item(item, fallback_title="RaiPlay"):
    program_info = item.get("program_info") or {}
    year = parse_int(program_info.get("year") or item.get("year"))
    return Metadata(
        title=programme_title(item, fallback=fallback_title),
        season=season_number_from_item(item),
        episode=episode_number_from_item(item),
        episode_title=episode_title_from_item(item),
        description=source_description(item),
        video_id=video_id_from_item(item),
        year=year,
        content_type=clean_text(program_info.get("typology") or item.get("type")),
        source_language=SOURCE_LANGUAGE_CODE,
    )


def relinker_cont_from_url(content_url):
    content_url = clean_text(content_url)
    if not content_url:
        return ""
    query = parse_qs(urlparse(content_url).query)
    cont = clean_text((query.get("cont") or [""])[0])
    if cont:
        return cont
    if "=" in content_url:
        return clean_text(content_url.rsplit("=", 1)[-1])
    return ""


def fetch_relinker(cont):
    variants = (
        {"cont": cont, "output": "62", "forceUserAgent": "raiplayappletv"},
        {"cont": cont, "output": "62"},
    )
    last_error = None
    for params in variants:
        try:
            payload = fetch_json(
                RELINKER_URL,
                params=params,
                headers={"Accept": "application/json,text/plain,*/*", "User-Agent": DEFAULT_HEADERS["User-Agent"]},
            )
            if relinker_manifest_url(payload):
                return payload
        except Exception as exc:
            last_error = exc

    if last_error:
        raise last_error
    raise ValueError("RaiPlay relinker did not return JSON playback data.")


def relinker_manifest_url(payload):
    videos = payload.get("video") or []
    if videos and clean_text(videos[0]):
        return clean_text(videos[0])

    for item in payload.get("playlist") or []:
        if clean_text(item.get("type")).lower() == "main" and clean_text(item.get("url")):
            return clean_text(item.get("url"))
    return ""


def fixed_manifest_url(manifest_url):
    manifest_url = clean_text(manifest_url)
    if not manifest_url or ".m3u8" not in manifest_url.lower():
        return manifest_url

    standard_qualities = "1200,1800,2400,3600,5000"
    fixed = re.sub(r"(_,[\d,]+)(/playlist\.m3u8)", f"_,{standard_qualities}\\2", manifest_url)
    fixed = re.sub(
        r"(baseuri=[^&]*?_,)[\d,%]+(%2F)",
        lambda match: f"{match.group(1)}{quote(standard_qualities)}{match.group(2)}",
        fixed,
    )
    return fixed


def relinker_license(payload):
    values = ((payload.get("licence_server_map") or {}).get("drmLicenseUrlValues") or [])
    if not values:
        return None, {}

    license_url = clean_text(values[0].get("licenceUrl"))
    if not license_url:
        return None, {}

    parsed = urlparse(license_url)
    query = parse_qs(parsed.query)
    auth = clean_text((query.get("authorization") or query.get("Authorization") or [""])[0])
    clean_license_url = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
    headers = {
        "Content-Type": "application/octet-stream",
        "Origin": BASE_URL,
        "Referer": f"{BASE_URL}/",
        "User-Agent": DEFAULT_HEADERS["User-Agent"],
    }
    if auth:
        headers["nv-authorizations"] = auth
    return clean_license_url or license_url, headers


def subtitle_tracks_from_item(item):
    video = item.get("video") or {}
    tracks = []

    for source in (video.get("subtitlesArray"), video.get("subtitleList"), item.get("subtitles")):
        if not source:
            continue
        if isinstance(source, dict):
            source = [source]
        if not isinstance(source, list):
            continue
        for track in source:
            if isinstance(track, str):
                subtitle_path = track
                language = SOURCE_LANGUAGE_CODE
                label = SOURCE_LANGUAGE_NAME
            else:
                subtitle_path = clean_text(track.get("url") or track.get("href") or track.get("src") or track.get("file"))
                language = clean_text(track.get("language") or track.get("lang") or track.get("locale")) or SOURCE_LANGUAGE_CODE
                label = clean_text(track.get("label") or track.get("name")) or language
            if not subtitle_path:
                continue
            tracks.append(
                {
                    "url": canonical_subtitle_url(subtitle_path),
                    "language": language.lower().split("-")[0],
                    "label": label,
                    "kind": "subtitle",
                }
            )

    if clean_text(video.get("subtitles")) and not tracks:
        tracks.append(
            {
                "url": canonical_subtitle_url(video.get("subtitles")),
                "language": SOURCE_LANGUAGE_CODE,
                "label": SOURCE_LANGUAGE_NAME,
                "kind": "subtitle",
            }
        )

    seen = set()
    unique = []
    for track in tracks:
        key = track["url"]
        if key not in seen:
            seen.add(key)
            unique.append(track)
    return unique


def canonical_subtitle_url(subtitle_path):
    subtitle_path = clean_text(subtitle_path)
    if not subtitle_path:
        return ""
    if re.match(r"^https?://", subtitle_path, re.I):
        return subtitle_path
    return urljoin(BASE_URL, quote(subtitle_path, safe="/:?&=%"))


def cards_from_season_payload(payload):
    cards = []
    for season in payload.get("seasons") or []:
        for episode_group in season.get("episodes") or []:
            cards.extend(episode_group.get("cards") or [])
    if cards:
        return cards
    if payload.get("cards"):
        return payload.get("cards") or []
    if payload.get("items"):
        return payload.get("items") or []
    return []


def stream_row(stream_type, resolution="-", bitrate="-", codec="-", lang="-", channels="-"):
    return {
        "type": clean_text(stream_type) or "-",
        "resolution": clean_text(resolution) or "-",
        "bitrate": clean_text(bitrate) or "-",
        "codec": clean_text(codec) or "-",
        "lang": clean_text(lang) or "-",
        "channels": clean_text(channels) or "-",
    }


def stream_height(stream):
    match = re.search(r"x(\d+)$", clean_text(stream.get("resolution")))
    return int(match.group(1)) if match else -1


def stream_sort_key(stream):
    stream_type = clean_text(stream.get("type")).lower()
    type_order = {"video": 0, "audio": 1, "subtitle": 2}.get(stream_type, 9)
    height_key = -stream_height(stream) if stream_type == "video" else 0
    bitrate = parse_int(stream.get("bitrate"), 0) or 0
    bitrate_key = -bitrate if stream_type == "video" else bitrate
    return type_order, height_key, bitrate_key, clean_text(stream.get("lang")).lower()


def collect_hls_streams(manifest_url):
    text = fetch_text(manifest_url, headers={"Accept": "application/vnd.apple.mpegurl,*/*"})
    streams = []
    lines = text.splitlines()
    for line in lines:
        if line.startswith("#EXT-X-STREAM-INF"):
            resolution = "-"
            bitrate = "-"
            codec = "-"
            match = re.search(r"RESOLUTION=(\d+x\d+)", line)
            if match:
                resolution = match.group(1)
            match = re.search(r"BANDWIDTH=(\d+)", line)
            if match:
                bitrate = str(int(match.group(1)) // 1000)
            match = re.search(r'CODECS="([^"]+)"', line)
            if match:
                codec = match.group(1)
            streams.append(stream_row("video", resolution, bitrate, codec))

        if line.startswith("#EXT-X-MEDIA"):
            media_type = "-"
            language = "-"
            channels = "-"
            match = re.search(r'TYPE=([^,]+)', line)
            if match:
                media_type = match.group(1).lower()
            match = re.search(r'LANGUAGE="([^"]+)"', line)
            if match:
                language = match.group(1)
            match = re.search(r'CHANNELS="([^"]+)"', line)
            if match:
                channels = match.group(1)
            if media_type in {"audio", "subtitles"}:
                streams.append(stream_row("subtitle" if media_type == "subtitles" else "audio", lang=language, channels=channels))
    return streams


def collect_mpd_streams(manifest_url):
    response = session.get(manifest_url, headers={"Accept": "application/dash+xml,*/*"}, timeout=30)
    response.raise_for_status()
    root = ET.fromstring(response.content)
    ns = "{urn:mpeg:dash:schema:mpd:2011}"
    streams = []
    for adaptation in root.findall(f".//{ns}AdaptationSet"):
        mime = clean_text(adaptation.get("mimeType") or adaptation.get("contentType")).lower()
        if "video" in mime:
            stream_type = "video"
        elif "audio" in mime:
            stream_type = "audio"
        elif "text" in mime or "subtitle" in mime:
            stream_type = "subtitle"
        else:
            stream_type = clean_text(adaptation.get("contentType") or "-")
        lang = clean_text(adaptation.get("lang") or "-")
        for rep in adaptation.findall(f"{ns}Representation"):
            width = clean_text(rep.get("width"))
            height = clean_text(rep.get("height"))
            resolution = f"{width}x{height}" if width and height else "-"
            bitrate = clean_text(rep.get("bandwidth"))
            if bitrate.isdigit():
                bitrate = str(int(bitrate) // 1000)
            codec = clean_text(rep.get("codecs") or adaptation.get("codecs") or "-")
            channels = "-"
            streams.append(stream_row(stream_type, resolution, bitrate, codec, lang, channels))
    return streams


def collect_manifest_streams(manifest_url, manifest_type):
    try:
        if manifest_type == "mpd":
            return collect_mpd_streams(manifest_url)
        return collect_hls_streams(manifest_url)
    except Exception as exc:
        print(f"{icons.ICON_WARNING} {bcolors.WARNING}Could not inspect RaiPlay manifest streams: {exc}{bcolors.ENDC}")
        return []


def extract_json_script(page_text, script_id=None):
    if script_id:
        pattern = rf'<script[^>]+id=["\']{re.escape(script_id)}["\'][^>]*>(.*?)</script>'
    else:
        pattern = r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>'

    match = re.search(pattern, page_text, flags=re.DOTALL | re.IGNORECASE)
    if not match:
        return None

    return json.loads(html.unescape(match.group(1)).strip())


def extract_video_id(video_url):
    item, _ = load_video_item(video_url)
    video_id = video_id_from_item(item)
    if video_id:
        return video_id
    raise ValueError("Could not extract RaiPlay video ID from URL.")


def search_metadata(video_url, video_id):
    item, _ = load_video_item(video_url)
    metadata = metadata_from_item(item)
    metadata.video_id = metadata.video_id or video_id
    return metadata


def get_playback_info(video_url, metadata):
    item, _ = load_video_item(video_url)
    item_metadata = metadata_from_item(item)
    if item_metadata.video_id:
        metadata = item_metadata

    video = item.get("video") or {}
    content_url = clean_text(video.get("content_url"))
    cont = relinker_cont_from_url(content_url)
    if not cont:
        raise ValueError("RaiPlay video JSON does not contain a relinker content id.")

    relinker_payload = fetch_relinker(cont)
    manifest_url = fixed_manifest_url(relinker_manifest_url(relinker_payload))
    if not manifest_url:
        raise ValueError("RaiPlay relinker did not return a playable manifest URL.")

    license_url, license_headers = relinker_license(relinker_payload)
    manifest_type = "mpd" if ".mpd" in manifest_url.lower() else "m3u8"
    subtitles = subtitle_tracks_from_item(item)
    streams = collect_manifest_streams(manifest_url, manifest_type)

    return PlaybackInfo(
        manifest_url=manifest_url,
        manifest_type=manifest_type,
        license_url=license_url,
        metadata=metadata,
        subtitles=subtitles,
        streams=streams,
        license_headers=license_headers,
    )


def is_episode_url(video_url):
    try:
        json_url = json_url_from_url(video_url)
    except ValueError:
        return False
    if is_video_json_url(json_url):
        return True
    if "/programmi/" not in urlparse(json_url).path:
        return False
    try:
        return is_playable_programme(fetch_json(json_url, headers={"Accept": "application/json,text/plain,*/*"}))
    except Exception:
        return False


def collect_episode_item(video_url):
    item, page_url = load_video_item(video_url)
    metadata = metadata_from_item(item)
    video_id = metadata.video_id or video_id_from_item(item)
    return EpisodeItem(
        url=page_url,
        title=metadata.title,
        season=metadata.season,
        episode=metadata.episode,
        episode_title=metadata.episode_title,
        video_id=metadata.video_id or video_id,
        description=metadata.description,
    )


def collect_episode_items(series_url, show_progress=True):
    json_url = json_url_from_url(series_url)
    if is_video_json_url(json_url):
        raise ValueError("List/download mode requires a RaiPlay series URL, not an episode URL.")

    programme = fetch_json(json_url, headers={"Accept": "application/json,text/plain,*/*"})
    if is_playable_programme(programme):
        raise ValueError("List/download mode requires a RaiPlay series URL, not a movie URL.")

    show_title = clean_text((programme.get("program_info") or {}).get("name") or programme.get("name") or "RaiPlay")
    items = []
    seen = set()
    season_sets = programme_sets(programme)
    if not season_sets:
        raise ValueError("Could not find any RaiPlay season sets in the programme JSON.")

    for set_index, set_info in enumerate(season_sets, start=1):
        block = set_info["block"]
        content_set = set_info["set"]
        path_id = clean_text(content_set.get("path_id"))
        if path_id:
            season_payload = fetch_json(canonical_url(path_id), headers={"Accept": "application/json,text/plain,*/*"})
            cards = cards_from_season_payload(season_payload)
        else:
            block_id = clean_text(block.get("id"))
            set_id = clean_text(content_set.get("id"))
            base_path = urlparse(json_url).path.lstrip("/").removesuffix(".json")
            episodes_url = f"{BASE_URL}/{base_path}/{block_id}/{set_id}/episodes.json"
            season_payload = fetch_json(episodes_url, headers={"Accept": "application/json,text/plain,*/*"})
            cards = cards_from_season_payload(season_payload)

        if show_progress:
            season_name = clean_text(content_set.get("name") or f"Set {set_index}")
            print(f"{icons.ICON_INFO} {bcolors.LIGHTBLUE}{season_name}: {bcolors.ENDC}{len(cards)} episode(s)")

        for item in cards:
            item.setdefault("program_name", show_title)
            url = rai_item_url(item)
            key = video_id_from_item(item) or url
            if not url or key in seen:
                continue
            seen.add(key)
            metadata = metadata_from_item(item, fallback_title=show_title)
            items.append(
                EpisodeItem(
                    url=url,
                    title=metadata.title or show_title,
                    season=metadata.season,
                    episode=metadata.episode,
                    episode_title=metadata.episode_title,
                    video_id=metadata.video_id,
                    description=metadata.description,
                    air_date=clean_text(item.get("date_published")),
                )
            )

    if not items:
        raise ValueError("No RaiPlay episodes found for this URL.")
    return sorted(items, key=episode_sort_key)


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
    season = episode_series_number(item)
    if season is not None and number is not None:
        return format_queue_selector(season, number), title
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
    terminal_width = os.get_terminal_size().columns if sys.stdout.isatty() else 88
    title = f" {service_label}: {series_title} "
    rule_width = max(terminal_width, len(title) + 4)
    left_width = max((rule_width - len(title)) // 2, 0)
    right_width = max(rule_width - len(title) - left_width, 0)
    print(
        f"{bcolors.LIGHTBLUE}{'─' * left_width}{bcolors.ENDC} "
        f"{bcolors.LIGHTBLUE}{service_label}: {bcolors.ENDC}{series_title} "
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
        print(f"{selector} {title}")


def safe_windows_filename(value):
    value = clean_text(value).replace("'", "")
    value = re.sub(r'[\\/:*?"<>|]', " ", value)
    value = re.sub(r"\s+", ".", value)
    return value.strip(".") or "Unknown"


def filename_show_title(value):
    value = clean_text(value)
    value = re.sub(r"\s*\((?:Serie\s*tv|Serie TV|Doc|Film)\)\s*$", "", value, flags=re.IGNORECASE)
    return safe_windows_filename(value)


def normalized_title_key(value):
    value = clean_text(value).casefold()
    value = re.sub(r"\s*\((?:serie\s*tv|doc|film)\)\s*$", "", value, flags=re.IGNORECASE)
    return re.sub(r"[^a-z0-9]+", "", value)


def filename_episode_title(metadata):
    title = clean_text(metadata.episode_title)
    if not title:
        return ""

    episode = int(metadata.episode) if metadata.episode is not None else None
    if episode is not None and re.fullmatch(rf"Episodio\s*0*{episode}", title, flags=re.IGNORECASE):
        return ""

    show = clean_text(metadata.title)
    if normalized_title_key(title) == normalized_title_key(show):
        return ""

    show_pattern = re.escape(re.sub(r"\s*\((?:Serie\s*tv|Serie TV|Doc|Film)\)\s*$", "", show, flags=re.IGNORECASE))
    if re.fullmatch(rf"{show_pattern}\s*[-:]\s*S\s*\d+\s*E\s*\d+", title, flags=re.IGNORECASE):
        return ""

    return safe_windows_filename(title)


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

    for group_index, label in enumerate(group_labels):
        series_items = grouped_items[label]
        if group_index > 0:
            print(f"{bcolors.GRAY}│{bcolors.ENDC}")
        group_is_last = group_index == len(group_labels) - 1
        group_branch = "└─" if group_is_last else "├─"
        group_child_prefix = "   " if group_is_last else "│  "
        print(f"{bcolors.GRAY}{group_branch} {label}: {bcolors.ENDC}{len(series_items)} episodes")
        for episode_index, item in enumerate(series_items):
            is_last = episode_index == len(series_items) - 1
            branch = "└─" if is_last else "├─"
            url_branch = "  " if is_last else "│ "
            number_label, title = episode_tree_label(item)
            print(f"{bcolors.GRAY}{group_child_prefix}{branch} {number_label}. {bcolors.ENDC}{title}")
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
    title = filename_show_title(metadata.title)
    season_episode = ""
    if metadata.season is not None and metadata.episode is not None:
        season_episode = f"S{int(metadata.season):02}E{int(metadata.episode):02}"
    elif metadata.season is not None:
        season_episode = f"S{int(metadata.season):02}"

    parts = [title]
    if season_episode:
        parts.append(season_episode)
    episode_title = filename_episode_title(metadata)
    if episode_title:
        parts.append(episode_title)
    parts.extend([resolution, SERVICE_TAG, "WEB-DL", "AAC2.0", "H.264"])
    return ".".join(part for part in parts if part and part != "Unknown")


def build_download_command(playback, filename, keys=None, mode="auto", quality=None, save_subs=False):
    if mode == "interactive":
        selectors = ""
    else:
        subtitle_selector = f"--select-subtitle {DEFAULT_SUBTITLE_SELECTOR}" if save_subs else "--drop-subtitle all"
        selectors = f'{video_selector(quality, default=DEFAULT_VIDEO_SELECTOR)} --select-audio {DEFAULT_AUDIO_SELECTOR} {subtitle_selector} '

    command = (
        f'{N_M3U8DL} "{playback.manifest_url}" '
        f'{selectors}'
        f'-mt -M format=mkv --save-dir "{SAVE_PATH}" --save-name "{filename}"'
    )

    if keys:
        command += " " + " ".join(f"--key {key}" for key in keys)

    return append_downloader_proxy(command)


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


def print_streams(streams):
    print(f"\n{bcolors.YELLOW}Available streams:{bcolors.ENDC}")
    if not streams:
        print("No video, audio, or subtitle streams were found in the manifest.")
        return

    streams = sorted(streams, key=stream_sort_key)
    headings = ("#", "Type", "Resolution", "Bitrate", "Codec", "Lang", "Channels")
    rows = [
        (
            str(index),
            stream.get("type", "-"),
            stream.get("resolution", "-"),
            stream.get("bitrate", "-"),
            stream.get("codec", "-"),
            stream.get("lang", "-"),
            stream.get("channels", "-"),
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
    print(f"{bcolors.LIGHTBLUE}Title: {bcolors.ENDC}{metadata.episode_title or 'Unknown'}")
    if metadata.season is not None or metadata.episode is not None:
        print(f"{bcolors.LIGHTBLUE}Episode: {bcolors.ENDC}{format_queue_selector(metadata.season or 0, metadata.episode)}")
    if metadata.description:
        print(f"{bcolors.LIGHTBLUE}Description: {bcolors.ENDC}{translate_to_target_language(metadata.description)}")


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


def print_playback_details(playback, keys, command, filename):
    label = "MPD URL" if playback.manifest_type == "mpd" else "M3U8 URL"
    print(f"{bcolors.LIGHTBLUE}{label}: {bcolors.ENDC}{playback.manifest_url}")

    if playback.license_url:
        print(f"{bcolors.RED}License URL: {bcolors.ENDC}{playback.license_url}")
    if playback.pssh:
        print(f"{bcolors.LIGHTBLUE}PSSH: {bcolors.ENDC}{playback.pssh}")
    if keys:
        print(f"{bcolors.OKGREEN}KEYS: {bcolors.ENDC}{keys}")
    elif playback.manifest_type == "mpd":
        print(f"{icons.ICON_WARNING} {bcolors.WARNING}No keys available - content may be encrypted{bcolors.ENDC}")

    print(f"{bcolors.YELLOW}DOWNLOAD COMMAND: {bcolors.ENDC}")
    print(mask_proxy_command(command))


def maybe_download(command, auto_download=False, auto_confirm=False):
    if confirm_download("Do you wish to download? Y or N: ", auto_confirm=auto_confirm, auto_download=auto_download):
        print(f"{icons.ICON_WAITING} {bcolors.OKBLUE}Downloading video...{bcolors.ENDC}")
        subprocess.run(command, shell=True)
    else:
        print(f"{icons.ICON_FAILURE} {bcolors.RED}Download cancelled{bcolors.ENDC}")


def resolve_video(video_url, mode="auto", quality=None, save_subs=False, show_metadata=False):
    print(f"{icons.ICON_INFO} {bcolors.LIGHTBLUE}Processing: {bcolors.ENDC}{video_url}")
    video_id = extract_video_id(video_url)
    metadata = search_metadata(video_url, video_id)
    if show_metadata:
        print_metadata(metadata)

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
    command = build_download_command(playback, filename, keys, mode=mode, quality=quality, save_subs=save_subs)
    return playback, keys, command, filename


def process_video(video_url, mode="auto", auto_download=False, info=False, quality=None, auto_confirm=False, save_subs=False):
    playback, keys, command, filename = resolve_video(video_url, mode=mode, quality=quality, save_subs=save_subs)
    print_playback_details(playback, keys, command, filename)

    if info:
        return

    if ENABLE_TRANSLATION:
        maybe_save_translated_subtitles(playback, filename, auto_download=auto_download)

    if save_subs and playback.subtitles:
        save_native_subtitles(playback, filename)

    maybe_download(command, auto_download=auto_download, auto_confirm=auto_confirm)


def info(video_url, quality=None, save_subs=False):
    if not is_episode_url(video_url):
        raise ValueError(f"Info mode requires a {SERVICE_NAME} episode/video URL, not a series URL.")
    playback, keys, command, filename = resolve_video(video_url, mode="info", quality=quality, save_subs=save_subs)
    manifest_label = "DASH Manifest URL" if playback.manifest_type == "mpd" else "HLS Manifest URL"
    print(f"{bcolors.LIGHTBLUE}{manifest_label}: {bcolors.ENDC}{playback.manifest_url}")
    if playback.license_url:
        print(f"{bcolors.RED}License URL: {bcolors.ENDC}{playback.license_url}")
    if playback.pssh:
        print(f"{bcolors.LIGHTBLUE}PSSH: {bcolors.ENDC}{playback.pssh}")
    for key in keys:
        print(f"{bcolors.OKGREEN}KEYS: {bcolors.ENDC}--key {key}")
    print_streams(playback.streams)
    print_external_subtitles(playback.subtitles)
    print_episode_metadata(playback.metadata)
    print(f"\n{bcolors.YELLOW}Suggested filename: {bcolors.ENDC}{filename}.mkv")


def download_selected_episodes(series_url, selector, quality=None, auto_confirm=False, save_subs=False):
    print(f"{icons.ICON_WAITING} {bcolors.LIGHTBLUE}Retrieving series information.....{bcolors.ENDC}")
    episode_items = select_episode_items(series_url, selector)
    print_download_queue(episode_items)

    if not confirm_download(f"\nDownload {len(episode_items)} episode(s)? Y or N: ", auto_confirm=auto_confirm):
        print(f"{icons.ICON_FAILURE} {bcolors.RED}Download cancelled{bcolors.ENDC}")
        return

    for index, item in enumerate(episode_items, start=1):
        _, title = episode_tree_label(item)
        print(f"\n{icons.ICON_INFO} {bcolors.LIGHTBLUE}Downloading {index}/{len(episode_items)}: {title}{bcolors.ENDC}")
        process_video(item.url, mode="auto", auto_download=True, quality=quality, auto_confirm=auto_confirm, save_subs=save_subs)


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


def progress_bar(done, total, width=28):
    if total <= 0:
        return "[" + "-" * width + "] 0/0"
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


def subtitle_language_code(subtitle, fallback="sub"):
    if isinstance(subtitle, dict):
        value = clean_text(subtitle.get("language") or subtitle.get("lang") or subtitle.get("srclang") or subtitle.get("locale"))
    else:
        value = ""

    if not value:
        value = SOURCE_LANGUAGE_CODE if SOURCE_LANGUAGE_CODE and SOURCE_LANGUAGE_CODE != "auto" else fallback

    value = value.lower().split("-")[0]
    value = re.sub(r"[^a-z0-9]+", "", value)
    return value or fallback


def save_native_subtitles(playback, filename):
    subtitle = get_subtitle(playback)
    if not subtitle:
        print(
            f"{bcolors.WARNING}{icons.ICON_WARNING} No external native/default subtitle URL found "
            f"for {SERVICE_NAME}.{bcolors.ENDC}"
        )
        return None

    subtitle_text = fetch_subtitle_text(subtitle)
    cues = parse_subtitle_text(subtitle_text, subtitle_url(subtitle))
    if not cues:
        print(f"{bcolors.WARNING}{icons.ICON_WARNING} No native/default subtitle cues found.{bcolors.ENDC}")
        return None

    language_code = subtitle_language_code(subtitle, fallback=SOURCE_LANGUAGE_CODE if SOURCE_LANGUAGE_CODE != "auto" else "sub")
    output_path = SAVE_PATH / f"{filename}.{language_code}.srt"
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


def normalize_main_options(optional_args, quality=None, auto_confirm=False, auto_download=False, save_subs=False):
    if len(optional_args) == 2:
        quality, auto_confirm = optional_args
    elif len(optional_args) == 3:
        if isinstance(optional_args[0], bool) and not isinstance(optional_args[1], bool):
            auto_download, quality, auto_confirm = optional_args
        else:
            quality, auto_confirm, save_subs = optional_args
    elif len(optional_args) == 4:
        auto_download, quality, auto_confirm, save_subs = optional_args
    elif len(optional_args) > 4:
        raise TypeError(f"Unexpected trailing service arguments: {optional_args!r}")

    return normalize_quality(quality) if quality else None, bool(auto_confirm), bool(auto_download), bool(save_subs)


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
    save_subs=False,
):
    global SAVE_PATH, WVD_PATH
    SAVE_PATH = Path(downloads_path)
    WVD_PATH = wvd_device_path
    quality, auto_confirm, auto_download, save_subs = normalize_main_options(
        optional_args,
        quality,
        auto_confirm,
        auto_download,
        save_subs,
    )

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
        download_selected_episodes(video_url, download_selector, quality=quality, auto_confirm=auto_confirm, save_subs=save_subs)
        return

    if mode == "info":
        info(video_url, quality=quality, save_subs=save_subs)
        return

    if is_episode_url(video_url):
        process_video(
            video_url,
            mode=mode,
            auto_download=auto_download or auto_confirm,
            quality=quality,
            auto_confirm=auto_confirm,
            save_subs=save_subs,
        )
        return

    print(
        f"{icons.ICON_WARNING} {bcolors.WARNING}Series URLs require a flag. Use --list/-l "
        f"to list episodes or --download/-d SELECTOR to download selected episodes.{bcolors.ENDC}"
    )


def parse_local_args(argv=None):
    parser = argparse.ArgumentParser(
        description=f"Resolve {SERVICE_NAME} episode/video or series URLs.",
        usage=f"{Path(__file__).name} [url] [-i | -a | -l | -d SELECTOR] [-q HEIGHT] [-s] [-y]",
    )
    parser.add_argument("url", nargs="?", help=f"{SERVICE_NAME} episode/video or series URL")
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument("-i", "--info", action="store_true", help="Show metadata and available streams without downloading")
    mode_group.add_argument("-a", "--action", action="store_true", help="Let N_m3u8DL-RE prompt for stream choices")
    mode_group.add_argument("-l", "--list", action="store_true", help="List episodes found on a series URL")
    mode_group.add_argument(
        "-d",
        "--download",
        metavar="SELECTOR",
        help="Download from a series URL using sXXeXX, sXXXXeXX, sXX, sXXXX, or a range",
    )
    parser.add_argument("-x", "--export", action="store_true", help="Export list-mode episode URLs to a text file")
    parser.add_argument("-q", "--quality", type=normalize_quality, help="Select video height for downloads, e.g. 720 or 1080")
    parser.add_argument("-s", "--subs", action="store_true", help="Keep native/default service subtitles where implemented")
    parser.add_argument("-y", "--yes", action="store_true", help="Automatically answer yes to download prompts")
    parser.add_argument("--downloads-path", default=".", help="Local smoke-test downloads path")
    parser.add_argument("--wvd-device-path", default=None, help="Local smoke-test WVD device path")
    return parser.parse_args(argv)


def local_main(argv=None):
    args = parse_local_args(argv)
    if args.url:
        video_url = args.url.strip()
    else:
        prompt_input = input(f"{icons.ICON_INFO} {bcolors.LIGHTBLUE}Enter the {SERVICE_NAME} URL: {bcolors.ENDC}").strip()
        prompt_args = parse_local_args(shlex.split(prompt_input))
        args = prompt_args
        video_url = (args.url or "").strip()
        if not video_url:
            print(f"{icons.ICON_FAILURE} {bcolors.FAIL}No {SERVICE_NAME} URL provided.{bcolors.ENDC}")
            return

    mode = "auto"
    if args.info:
        mode = "info"
    elif args.action:
        mode = "interactive"
    elif args.list:
        mode = "list"
    elif args.download:
        mode = "download"

    try:
        main(
            video_url,
            args.downloads_path,
            args.wvd_device_path,
            mode=mode,
            export_list=args.export,
            download_selector=args.download,
            quality=args.quality,
            auto_confirm=args.yes,
            save_subs=args.subs,
        )
    except Exception as exc:
        print(f"{icons.ICON_FAILURE} {bcolors.FAIL}Error: {exc}{bcolors.ENDC}")
        sys.exit(1)


if __name__ == "__main__":
    local_main()
