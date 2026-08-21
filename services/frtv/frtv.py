import base64
import binascii
import html
import json
import re
import shutil
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin, urlparse

import icons
from download_confirm import confirm_download
import requests
import urllib3
from beaupy.spinners import Spinner
from colors import bcolors
from pywidevine.cdm import Cdm
from pywidevine.device import Device
from pywidevine.pssh import PSSH
from quality_utils import apply_quality_to_filename, video_selector
from services.proxy import current_proxy_url, mask_proxy_command


urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


BASE_URL = "https://www.france.tv"
DEEP_PAGE_URL = f"{BASE_URL}/api/deep-page/"
K7_VIDEO_URL = "https://k7.ftven.fr/videos/{media_id}"
TRANSLATE_URL = "https://translate.googleapis.com/translate_a/single"
TRANSLATE_BATCH_MARKER = "@@FRTVBREAK@@"
TRANSLATE_BATCH_SIZE = 40
TRANSLATE_BATCH_CHAR_LIMIT = 4500

SCRIPT_DIR = Path(__file__).resolve().parent
N_M3U8DL = "N_m3u8DL-RE"


session = requests.Session()
SAVE_PATH = None
WVD_PATH = None
SERVICE_PROXY = None


def configure_service(downloads_path, wvd_device_path):
    global SAVE_PATH, WVD_PATH, SERVICE_PROXY
    SAVE_PATH = Path(downloads_path)
    WVD_PATH = wvd_device_path
    SERVICE_PROXY = current_proxy_url()
    session.proxies.clear()
    session.verify = True
    if SERVICE_PROXY:
        session.proxies.update({"http": SERVICE_PROXY, "https": SERVICE_PROXY})
        session.verify = False


DEFAULT_HEADERS = {
    "Accept": "text/html,application/json,text/plain,*/*",
    "Accept-Language": "fr-FR,fr;q=0.9,en-US;q=0.8,en;q=0.7",
    "Origin": BASE_URL,
    "Referer": f"{BASE_URL}/",
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
    aired_date: str = "Unknown"
    description: str = "No Description"
    video_id: Optional[str] = None
    content_id: Optional[str] = None


@dataclass
class PlaybackInfo:
    manifest_url: str
    manifest_type: str
    license_url: Optional[str] = None
    pssh: Optional[str] = None
    subtitles: list = field(default_factory=list)
    streams: list = field(default_factory=list)
    metadata: Metadata = field(default_factory=Metadata)
    drm: bool = False


def clean_text(value):
    return re.sub(r"\s+", " ", html.unescape(str(value or ""))).strip()


def clean_title(value):
    value = clean_text(value)
    value = re.sub(r"\s+en replay$", "", value, flags=re.I)
    return clean_text(value)


def date_value(value):
    value = clean_text(value)
    if not value:
        return "Unknown"

    normalised = value.replace("Z", "+00:00")
    try:
        from datetime import datetime
        return datetime.fromisoformat(normalised).strftime("%Y-%m-%d")
    except ValueError:
        pass

    match = re.search(r"(\d{2})/(\d{2})/(\d{4})", value)
    if match:
        day, month, year = match.groups()
        return f"{year}-{month}-{day}"
    return value.split("T", 1)[0]


def translate_description_to_english(text):
    text = clean_text(text)
    if not text or text == "No Description":
        return "No Description"
    try:
        return translate_text(text) or text
    except Exception as exc:
        print(f"{icons.ICON_WARNING} {bcolors.WARNING}Could not translate description: {exc}{bcolors.ENDC}")
        return text


def canonical_url(value):
    value = clean_text(value)
    if re.match(r"^https?://", value, re.IGNORECASE):
        return value
    if value.startswith("/"):
        return urljoin(BASE_URL, value)
    return urljoin(BASE_URL, value.strip("/"))


def extract_video_id(video_url):
    match = re.search(r"/(\d+)[^/]*\.html(?:[?#].*)?$", urlparse(video_url).path)
    if match:
        return match.group(1)
    raise ValueError("Could not extract France TV content ID from URL.")


def unescape_page_state(value):
    return value.replace(r"\"", '"').replace(r"\/", "/")


def find_player_src(html_text, content_id):
    text = unescape_page_state(html_text)

    content_pattern = re.compile(
        r'"contentId":"(?P<content_id>\d+)".{0,1200}?"src":"(?P<src>[0-9a-f-]{36})"',
        re.S,
    )
    for match in content_pattern.finditer(text):
        if match.group("content_id") == content_id:
            return match.group("src")

    src_pattern = re.compile(
        r'"src":"(?P<src>[0-9a-f-]{36})".{0,1200}?"contentId":"(?P<content_id>\d+)"',
        re.S,
    )
    for match in src_pattern.finditer(text):
        if match.group("content_id") == content_id:
            return match.group("src")

    all_srcs = re.findall(r'"src":"([0-9a-f-]{36})"', text)
    if all_srcs:
        return all_srcs[0]

    # Single-item pages sometimes do not expose playlistData; the media UUID is still repeated in page state.
    ignored = {"cda7ee37-4865-4ed0-96a1-1bd385a21725"}
    for uuid in re.findall(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", text):
        if uuid not in ignored:
            return uuid

    raise ValueError("Could not find France TV player media ID in page state.")


def meta_content(html_text, name):
    pattern = rf'<meta\s+(?:name|property)=["\']{re.escape(name)}["\']\s+content=["\']([^"\']*)["\']'
    match = re.search(pattern, html_text, re.I)
    return clean_text(match.group(1)) if match else ""


def parse_json_ld_metadata(html_text):
    metadata = {}
    for match in re.finditer(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html_text,
        re.I | re.S,
    ):
        body = html.unescape(match.group(1)).strip()
        if not body:
            continue
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            continue
        for node in walk(payload):
            if not isinstance(node, dict):
                continue
            node_type = node.get("@type")
            if node_type in ("TVSeries", "Movie") and not metadata.get("title"):
                metadata["title"] = clean_title(node.get("name"))
            if node_type == "TVEpisode":
                metadata["episode"] = node.get("episodeNumber")
                metadata["episode_title"] = clean_title(node.get("name"))
                description = clean_text(node.get("description"))
                aired_date = clean_text(node.get("datePublished") or node.get("uploadDate"))
                if description and not metadata.get("description"):
                    metadata["description"] = description
                if aired_date and not metadata.get("aired_date"):
                    metadata["aired_date"] = aired_date
            if node_type == "VideoObject":
                metadata.setdefault("episode_title", clean_title(node.get("name")))
                description = clean_text(node.get("description"))
                aired_date = clean_text(node.get("uploadDate") or node.get("datePublished"))
                if description and not metadata.get("description"):
                    metadata["description"] = description
                if aired_date and not metadata.get("aired_date"):
                    metadata["aired_date"] = aired_date
    return metadata


def walk(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk(child)


def is_episode_url(video_url):
    return re.search(r"/\d+[^/]*\.html(?:[?#].*)?$", urlparse(canonical_url(video_url)).path) is not None


def is_all_videos_url(video_url):
    return "/toutes-les-videos" in urlparse(canonical_url(video_url)).path


def series_url_from_url(video_url):
    parts = [part for part in urlparse(canonical_url(video_url)).path.split("/") if part]
    if len(parts) < 2:
        return BASE_URL
    return f"{BASE_URL}/{parts[0]}/{parts[1]}/"


def series_slug_from_url(video_url):
    parts = [part for part in urlparse(canonical_url(video_url)).path.split("/") if part]
    if len(parts) < 2:
        return ""
    return f"{parts[0]}_{parts[1]}"


def fetch_json(url, params=None, headers=None):
    response = session.get(
        url,
        params=params or {},
        headers={**DEFAULT_HEADERS, "Accept": "application/json,text/plain,*/*", **(headers or {})},
        timeout=30,
    )
    response.raise_for_status()
    return response.json()


def list_video_url(item):
    source_url = clean_text(item.get("_source_url") or item.get("url"))
    return canonical_url(source_url) if source_url else ""


def list_video_id(item):
    item_id = clean_text(item.get("id"))
    if item_id:
        return item_id
    match = re.search(r"/(\d+)[^/]*\.html", list_video_url(item))
    return match.group(1) if match else ""


def extract_episode_links(html_text):
    links = []
    seen = set()
    for match in re.finditer(r'href=["\']([^"\']+/\d+[^"\']*?\.html)["\']', html_text):
        url = canonical_url(match.group(1))
        if url in seen:
            continue
        seen.add(url)
        links.append({"url": url, "_source_url": url})
    return links


def collect_deep_page_items(source_url):
    slug = series_slug_from_url(source_url)
    if not slug:
        return []

    referer = source_url if is_all_videos_url(source_url) else urljoin(series_url_from_url(source_url), "toutes-les-videos/")
    page = 0
    items = []
    seen = set()

    while True:
        payload = fetch_json(DEEP_PAGE_URL, params={"slug": slug, "page": page}, headers={"Referer": referer})
        for wrapper in payload.get("result") or []:
            content = wrapper.get("content") if isinstance(wrapper, dict) else None
            if not isinstance(content, dict) or content.get("type") != "video":
                continue
            url = list_video_url(content)
            item_id = clean_text(content.get("id") or url)
            if not item_id or item_id in seen:
                continue
            seen.add(item_id)
            items.append(content)

        cursor = payload.get("cursor") or {}
        next_page = cursor.get("next")
        if next_page is None:
            break
        page = int(next_page)

    return items


def collect_series_info(source_url):
    response = session.get(series_url_from_url(source_url), headers=DEFAULT_HEADERS, timeout=30)
    response.raise_for_status()
    metadata = parse_json_ld_metadata(response.text)
    if metadata.get("title"):
        return {"name": metadata["title"]}
    title_match = re.search(r"<h1[^>]*>(.*?)</h1>", response.text, re.I | re.S)
    return {"name": clean_text(re.sub(r"<[^>]+>", " ", title_match.group(1))) if title_match else "France TV"}


def hydrate_series_episode(item):
    url = list_video_url(item)
    if not url:
        return item
    try:
        content_id = extract_video_id(url)
        response = session.get(url, headers=DEFAULT_HEADERS, timeout=30)
        response.raise_for_status()
        metadata = parse_page_metadata(response.text, url, content_id, translate_description=False)
        item = dict(item)
        item["_frtv_metadata"] = metadata
    except Exception as exc:
        print(f"{icons.ICON_WARNING} {bcolors.WARNING}Could not hydrate France TV episode {url}: {exc}{bcolors.ENDC}")
    return item


def list_season_number(item):
    metadata = item.get("_frtv_metadata")
    if metadata and metadata.season is not None:
        return metadata.season
    title = clean_text(item.get("title") or item.get("name"))
    title_match = re.search(r"\bS(?:aison)?\s*0*(\d+)\b", title, re.I)
    if title_match:
        return int(title_match.group(1))
    source_url = list_video_url(item)
    match = re.search(r"/(?:saison|opj-saison)-(\d+)/", source_url, re.I)
    return int(match.group(1)) if match else 1


def list_episode_number(item):
    metadata = item.get("_frtv_metadata")
    if metadata and metadata.episode is not None:
        return metadata.episode
    title = clean_text(item.get("title") or item.get("name"))
    match = re.search(r"\bE(?:pisode)?\s*0*(\d+)\b", title, re.I)
    return int(match.group(1)) if match else 1


def list_episode_title(item):
    metadata = item.get("_frtv_metadata")
    if metadata and metadata.episode_title:
        return clean_text(metadata.episode_title)
    title = clean_text(item.get("title") or item.get("name"))
    match = re.match(r"S\d+\s+E\d+\s+-\s+(.+)$", title, re.I)
    return clean_text(match.group(1) if match else title) or f"Episode {list_episode_number(item)}"


def build_series_episode_item(item, series_info=None):
    metadata = item.get("_frtv_metadata")
    return {
        "id": list_video_id(item),
        "show_title": clean_text((metadata.title if metadata else "") or (series_info or {}).get("name") or "France TV"),
        "season": list_season_number(item),
        "episode": list_episode_number(item),
        "title": list_episode_title(item),
        "url": list_video_url(item),
    }


def has_list_numbers_without_hydration(item):
    title = clean_text(item.get("title") or item.get("name"))
    source_url = list_video_url(item)
    has_season = bool(
        re.search(r"\bS(?:aison)?\s*0*\d+\b", title, re.I)
        or re.search(r"/(?:saison|opj-saison)-\d+/", source_url, re.I)
    )
    has_episode = bool(re.search(r"\bE(?:pisode)?\s*0*\d+\b", title, re.I))
    return has_season and has_episode


def collect_episode_items(series_url):
    source_url = canonical_url(series_url)
    if is_episode_url(source_url):
        raise ValueError("List/download mode requires a France TV series URL, not an episode URL.")

    items = collect_deep_page_items(source_url)
    if not items:
        response = session.get(source_url, headers=DEFAULT_HEADERS, timeout=30)
        response.raise_for_status()
        items = extract_episode_links(response.text)
    if not items:
        raise RuntimeError("No France TV episodes found for this URL.")

    series_info = collect_series_info(source_url)
    hydrated = [
        item if has_list_numbers_without_hydration(item) else hydrate_series_episode(item)
        for item in items
    ]
    episode_items = []
    seen = set()
    for item in hydrated:
        episode_item = build_series_episode_item(item, series_info=series_info)
        key = episode_item["id"] or episode_item["url"]
        if not key or key in seen or not episode_item["url"]:
            continue
        seen.add(key)
        episode_items.append(episode_item)
    episode_items.sort(key=lambda item: (item.get("season") or 0, item.get("episode") or 0, item.get("id") or ""))
    return episode_items


def parse_page_metadata(html_text, video_url, content_id, translate_description=True):
    text = unescape_page_state(html_text)
    parsed = parse_json_ld_metadata(html_text)

    title = parsed.get("title")
    episode_title = None
    season = None
    episode = parsed.get("episode")

    item_pattern = re.compile(
        r'"cardInfo":\{"image":"[^"]*","origin":[^,]*,"subTitle":"(?P<subtitle>[^"]*)","title":"(?P<title>[^"]*)".{0,800}?'
        r'"preTitle":"(?P<pretitle>[^"]*)","title":"(?P<episode_title>[^"]*)","contentId":"(?P<content_id>\d+)".{0,400}?"src":"(?P<src>[0-9a-f-]{36})"',
        re.S,
    )
    for match in item_pattern.finditer(text):
        if match.group("content_id") != content_id:
            continue
        title = clean_text(match.group("title")) or title
        episode_title = clean_text(match.group("episode_title"))
        se_match = re.search(r"S\s*(\d+)\s*E\s*(\d+)", clean_text(match.group("pretitle")), re.I)
        if se_match:
            season = int(se_match.group(1))
            episode = int(se_match.group(2))
        break

    if not episode_title:
        h1_match = re.search(r"<h1[^>]*>(.*?)</h1>", html_text, re.I | re.S)
        if h1_match:
            h1 = clean_text(re.sub(r"<[^>]+>", " ", h1_match.group(1)))
            h1_match = re.match(r"(?P<title>.+?)\s+S(?P<season>\d+)\s+E(?P<episode>\d+)\s+-\s+(?P<episode_title>.+)", h1, re.I)
            if h1_match:
                title = clean_title(h1_match.group("title")) or title
                season = int(h1_match.group("season"))
                episode = int(h1_match.group("episode"))
                episode_title = clean_title(h1_match.group("episode_title"))
            else:
                episode_title = clean_title(h1)

    if season is None:
        season_match = re.search(r"/saison-(\d+)/", video_url, re.I)
        if season_match:
            season = int(season_match.group(1))

    if episode is None:
        episode_match = re.search(r"S\s*\d+\s*E\s*(\d+)", text, re.I)
        if episode_match:
            episode = int(episode_match.group(1))

    if not title:
        og_title = meta_content(html_text, "og:title")
        title = clean_title(og_title.split(" - ", 1)[0]) if og_title else "Unknown"

    description = (
        parsed.get("description")
        or meta_content(html_text, "description")
        or meta_content(html_text, "og:description")
        or "No Description"
    )
    aired_date = date_value(parsed.get("aired_date"))

    if episode_title and title and episode_title.lower().startswith(title.lower()):
        episode_title = clean_text(episode_title[len(title):])

    return Metadata(
        title=title or "Unknown",
        season=season,
        episode=int(episode) if str(episode or "").isdigit() else None,
        episode_title=episode_title or parsed.get("episode_title"),
        aired_date=aired_date,
        description=translate_description_to_english(description) if translate_description else clean_text(description),
        video_id=None,
        content_id=content_id,
    )


def search_metadata(video_url, video_id):
    response = session.get(video_url, headers=DEFAULT_HEADERS, timeout=30)
    response.raise_for_status()
    metadata = parse_page_metadata(response.text, video_url, video_id)
    metadata.video_id = find_player_src(response.text, video_id)
    return metadata


def sign_manifest_url(manifest_url, token_url):
    if not token_url:
        return manifest_url
    response = session.get(
        token_url,
        params={"url": manifest_url},
        headers={**DEFAULT_HEADERS, "Accept": "application/json,text/plain,*/*"},
        timeout=30,
    )
    response.raise_for_status()
    signed_url = clean_text((response.json() or {}).get("url"))
    return signed_url or manifest_url


def get_playback_info(video_url, metadata):
    if not metadata.video_id:
        raise ValueError("Missing France TV player media ID.")

    response = session.get(
        K7_VIDEO_URL.format(media_id=metadata.video_id),
        params={"device_type": "desktop", "browser": "chrome", "domain": "www.france.tv"},
        headers={**DEFAULT_HEADERS, "Accept": "application/json,text/plain,*/*"},
        timeout=30,
    )
    response.raise_for_status()
    payload = response.json()
    video = payload.get("video") or {}
    meta = payload.get("meta") or {}

    manifest_url = clean_text(video.get("url"))
    if not manifest_url:
        raise ValueError("France TV playback response did not include a manifest URL.")

    manifest_url = sign_manifest_url(manifest_url, (video.get("token") or {}).get("akamai"))
    manifest_type = "m3u8" if ".m3u8" in manifest_url.lower() or video.get("format") == "hls" else "mpd"

    if meta.get("title") and metadata.title == "Unknown":
        metadata.title = clean_title(meta.get("title"))
    if meta.get("additional_title") and (
        not metadata.episode_title
        or re.match(r"^(?:episode|épisode)\s+\d+$", metadata.episode_title, re.I)
    ):
        metadata.episode_title = clean_text(meta.get("additional_title"))
    if meta.get("description") and metadata.description == "No Description":
        metadata.description = translate_description_to_english(meta.get("description"))
    if meta.get("uploadDate") and metadata.aired_date == "Unknown":
        metadata.aired_date = date_value(meta.get("uploadDate"))
    if meta.get("pre_title"):
        match = re.search(r"S\s*(\d+)\s*E\s*(\d+)", clean_text(meta.get("pre_title")), re.I)
        if match:
            metadata.season = int(match.group(1))
            metadata.episode = int(match.group(2))

    subtitles = extract_subtitles(manifest_url, manifest_type)
    streams = parse_manifest_streams(fetch_manifest(manifest_url), manifest_type)
    streams.extend(subtitle_info_streams(subtitles))
    return PlaybackInfo(
        manifest_url=manifest_url,
        manifest_type=manifest_type,
        license_url=None,
        pssh=None,
        subtitles=subtitles,
        streams=streams,
        metadata=metadata,
        drm=bool(video.get("drm")),
    )


def extract_subtitles(manifest_url, manifest_type):
    response = session.get(manifest_url, headers=DEFAULT_HEADERS, timeout=30)
    response.raise_for_status()
    if manifest_type == "m3u8":
        return extract_subtitles_from_m3u8(manifest_url, response.text)
    return extract_subtitles_from_mpd(manifest_url, response.content)


def fetch_manifest(manifest_url):
    response = session.get(manifest_url, headers=DEFAULT_HEADERS, timeout=30)
    response.raise_for_status()
    return response.text


def format_bitrate(value):
    try:
        bitrate = int(float(value))
    except (TypeError, ValueError):
        return "-"
    return f"{bitrate / 1000000:.2f} Mbps" if bitrate >= 1000000 else f"{bitrate // 1000} Kbps"


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
            pending_variant = parse_m3u8_attributes(line.split(":", 1)[1])
            continue
        if pending_variant is not None and line and not line.startswith("#"):
            attrs = pending_variant
            streams.append({
                "type": "Vid",
                "resolution": attrs.get("RESOLUTION") or "-",
                "bitrate": format_bitrate(attrs.get("AVERAGE-BANDWIDTH") or attrs.get("BANDWIDTH")),
                "codec": attrs.get("CODECS") or "-",
                "lang": "-",
                "channels": "-",
            })
            pending_variant = None
            continue
        if not line.startswith("#EXT-X-MEDIA:"):
            continue
        attrs = parse_m3u8_attributes(line.split(":", 1)[1])
        media_type = attrs.get("TYPE", "").upper()
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
            "lang": attrs.get("LANGUAGE") or "-",
            "channels": attrs.get("CHANNELS") or "-",
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
            "",
        )
        for representation in adaptation.findall("{*}Representation"):
            rep_id = clean_text(representation.get("id"))
            codec = clean_text(representation.get("codecs")) or adaptation_codec or "-"
            if "thumb" in rep_id.lower() or "thumb" in codec.lower():
                continue
            mime_type = clean_text(representation.get("mimeType")) or adaptation_mime
            content_type = clean_text(representation.get("contentType")) or adaptation_type
            lang = clean_text(representation.get("lang")) or adaptation_lang
            width = clean_text(representation.get("width"))
            height = clean_text(representation.get("height"))
            channels = next(
                (
                    clean_text(node.get("value"))
                    for node in representation.findall("{*}AudioChannelConfiguration")
                    if node.get("value")
                ),
                adaptation_channels,
            )
            type_hint = f"{content_type} {mime_type} {codec}".lower()
            if "video" in type_hint:
                stream_type = "Vid"
            elif "audio" in type_hint:
                stream_type = "Aud"
            elif any(value in type_hint for value in ("text", "subtitle", "vtt", "ttml", "stpp", "wvtt")):
                stream_type = "Sub"
            else:
                continue
            streams.append({
                "type": stream_type,
                "resolution": f"{width}x{height}" if width and height else "-",
                "bitrate": format_bitrate(representation.get("bandwidth")),
                "codec": codec,
                "lang": lang,
                "channels": channels or "-",
            })
    return sorted(streams, key=stream_table_sort_key)


def parse_manifest_streams(manifest_text, manifest_type):
    if manifest_type == "m3u8" or str(manifest_text).lstrip().startswith("#EXTM3U"):
        return parse_hls_streams(manifest_text)
    return parse_dash_streams(manifest_text)


def subtitle_info_streams(subtitles):
    streams = []
    seen = set()
    for subtitle in subtitles or []:
        if not isinstance(subtitle, dict):
            continue
        key = clean_text(subtitle.get("url") or subtitle.get("name") or subtitle.get("lang"))
        if not key or key in seen:
            continue
        seen.add(key)
        kind = clean_text(subtitle.get("kind"))
        streams.append({
            "type": "Sub",
            "resolution": "-",
            "bitrate": "-",
            "codec": "vtt" if "vtt" in kind.lower() else clean_text(subtitle.get("mime_type")) or "-",
            "lang": clean_text(subtitle.get("lang")) or "fr",
            "channels": "-",
        })
    return streams


def parse_m3u8_attributes(line):
    attrs = {}
    for match in re.finditer(r'([A-Z0-9-]+)=("[^"]*"|[^,]*)', line):
        value = match.group(2)
        attrs[match.group(1)] = value[1:-1] if value.startswith('"') and value.endswith('"') else value
    return attrs


def extract_subtitles_from_m3u8(manifest_url, manifest_text):
    subtitles = []
    for line in manifest_text.splitlines():
        if not line.startswith("#EXT-X-MEDIA") or "TYPE=SUBTITLES" not in line.upper():
            continue
        attrs = parse_m3u8_attributes(line)
        uri = clean_text(attrs.get("URI"))
        if not uri:
            continue
        subtitles.append({
            "kind": "hls-vtt",
            "lang": clean_text(attrs.get("LANGUAGE") or "fr"),
            "name": clean_text(attrs.get("NAME")),
            "url": urljoin(manifest_url, uri),
        })
    return subtitles


def extract_subtitles_from_mpd(manifest_url, manifest_content):
    subtitles = []
    root = ET.fromstring(manifest_content)
    ns = "{urn:mpeg:dash:schema:mpd:2011}"

    for adaptation in root.findall(".//" + ns + "AdaptationSet"):
        content_type = (adaptation.get("contentType") or "").lower()
        mime_type = (adaptation.get("mimeType") or "").lower()
        lang = clean_text(adaptation.get("lang") or "fr")
        if content_type != "text" and "vtt" not in mime_type and "ttml" not in mime_type:
            continue

        label_el = adaptation.find(ns + "Label")
        label = clean_text(label_el.text if label_el is not None else "")
        subtitles.append({
            "kind": "dash-fragmented" if "mp4" in mime_type else "dash-text",
            "lang": lang,
            "name": label,
            "mime_type": mime_type,
            "url": manifest_url,
        })

    return subtitles


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


def build_license_headers(metadata):
    return {
        "Content-Type": "application/octet-stream",
        "Origin": BASE_URL,
        "Referer": f"{BASE_URL}/",
        "User-Agent": DEFAULT_HEADERS["User-Agent"],
    }


def post_license_challenge(license_url, challenge, metadata):
    headers = build_license_headers(metadata)
    response = session.post(license_url, headers=headers, data=challenge, timeout=30)
    try:
        response.raise_for_status()
    except requests.exceptions.HTTPError as exc:
        print(f"{icons.ICON_FAILURE} {bcolors.FAIL}HTTPError: {exc}{bcolors.ENDC}")
        print(f"{icons.ICON_INFO} Response Headers: {response.headers}")
        print(f"{icons.ICON_INFO} Response Text: {response.text[:2000]}")
        raise
    return response.content


def get_keys(pssh, license_url, metadata):
    try:
        pssh = PSSH(pssh)
    except (binascii.Error, ValueError) as exc:
        print(f"{icons.ICON_FAILURE} {bcolors.FAIL}Could not parse PSSH: {exc}{bcolors.ENDC}")
        return []

    device = Device.load(WVD_PATH)
    cdm = Cdm.from_device(device)
    session_id = cdm.open()

    try:
        challenge = cdm.get_license_challenge(session_id, pssh)
        licence = post_license_challenge(license_url, challenge, metadata)
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


def strip_vtt_tags(text):
    text = re.sub(r"<\d{2}:\d{2}:\d{2}\.\d{3}>", "", text)
    text = re.sub(r"</?[^>]+>", "", text)
    return html.unescape(clean_text(text))


def vtt_time_to_srt(value):
    value = value.strip()
    if re.match(r"^\d{2}:\d{2}\.\d{3}$", value):
        value = f"00:{value}"
    return value.replace(".", ",")


def parse_vtt(vtt_text):
    vtt_text = vtt_text.replace("\ufeff", "").replace("\r\n", "\n").replace("\r", "\n")
    blocks = re.split(r"\n{2,}", vtt_text.strip())
    cues = []

    for block in blocks:
        lines = [line.strip() for line in block.split("\n") if line.strip()]
        if not lines:
            continue
        if lines[0].upper().startswith(("WEBVTT", "NOTE", "STYLE", "REGION")):
            continue

        time_index = next((index for index, line in enumerate(lines) if "-->" in line), None)
        if time_index is None:
            continue

        start, _, end = lines[time_index].partition("-->")
        end = end.split(" ", 1)[0]
        text = strip_vtt_tags(" ".join(lines[time_index + 1:]))
        if text:
            cues.append({
                "start": vtt_time_to_srt(start),
                "end": vtt_time_to_srt(end),
                "text": text,
            })

    return cues


def parse_srt(srt_text):
    srt_text = srt_text.replace("\ufeff", "").replace("\r\n", "\n").replace("\r", "\n")
    blocks = re.split(r"\n{2,}", srt_text.strip())
    cues = []

    for block in blocks:
        lines = [line.strip() for line in block.split("\n") if line.strip()]
        if not lines:
            continue

        time_index = next((index for index, line in enumerate(lines) if "-->" in line), None)
        if time_index is None:
            continue

        start, _, end = lines[time_index].partition("-->")
        text = strip_vtt_tags(" ".join(lines[time_index + 1:]))
        if text:
            cues.append({
                "start": start.strip(),
                "end": end.strip().split(" ", 1)[0],
                "text": text,
            })

    return cues


def translate_text(text):
    text = clean_text(text)
    if not text:
        return ""

    response = session.get(
        TRANSLATE_URL,
        params={"client": "gtx", "sl": "fr", "tl": "en", "dt": "t", "q": text},
        headers={**DEFAULT_HEADERS, "Accept": "application/json,text/plain,*/*"},
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

    joined = f" {TRANSLATE_BATCH_MARKER} ".join(clean_texts)
    translated = translate_text(joined)
    parts = [clean_text(part) for part in translated.split(TRANSLATE_BATCH_MARKER)]
    if len(parts) == len(clean_texts):
        return parts

    midpoint = len(clean_texts) // 2
    return translate_texts_batch(clean_texts[:midpoint]) + translate_texts_batch(clean_texts[midpoint:])


def cue_batches(cues, batch_size=TRANSLATE_BATCH_SIZE, char_limit=TRANSLATE_BATCH_CHAR_LIMIT):
    batch = []
    batch_chars = 0
    for cue in cues:
        text = clean_text(cue["text"])
        projected_chars = batch_chars + len(text) + len(TRANSLATE_BATCH_MARKER) + 2
        if batch and (len(batch) >= batch_size or projected_chars > char_limit):
            yield batch
            batch = []
            batch_chars = 0
        batch.append(cue)
        batch_chars += len(text) + len(TRANSLATE_BATCH_MARKER) + 2
    if batch:
        yield batch


def progress_bar(done, total, width=30):
    total = max(total, 1)
    filled = int(width * done / total)
    return "[" + "#" * filled + "-" * (width - filled) + f"] {done}/{total}"


def translate_cues(cues, show_progress=True):
    translated = []
    batches = list(cue_batches(cues))
    total_batches = len(batches)
    for batch_index, batch in enumerate(batches, 1):
        start = len(translated) + 1
        end = start + len(batch) - 1
        if show_progress:
            print(f"\r{bcolors.LIGHTBLUE}{progress_bar(batch_index - 1, total_batches)}{bcolors.ENDC}", end="", flush=True)
        try:
            translated_texts = translate_texts_batch([cue["text"] for cue in batch])
        except Exception as exc:
            if show_progress:
                print()
            print(
                f"{bcolors.WARNING}{icons.ICON_WARNING} Subtitle batch translation failed at cues {start}-{end}: "
                f"{exc}{bcolors.ENDC}"
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
    for index, cue in enumerate(cues, 1):
        lines.extend([
            str(index),
            f"{cue['start']} --> {cue['end']}",
            cue["text"],
            "",
        ])
    output_path.write_text("\n".join(lines), encoding="utf-8")


def delete_temp_subtitle_file(path, show_success=True):
    try:
        path.unlink()
        if show_success:
            print(f"{bcolors.OKGREEN}{icons.ICON_SUCCESS} Deleted temporary French subtitles: {bcolors.ENDC}{path}")
    except FileNotFoundError:
        pass
    except Exception as exc:
        print(f"{bcolors.WARNING}{icons.ICON_WARNING} Could not delete temporary French subtitles {path}: {exc}{bcolors.ENDC}")


def find_created_subtitle_file(temp_name, existing_paths):
    candidates = []
    for pattern in (f"{temp_name}*.srt", f"{temp_name}*.vtt", f"{temp_name}*.ass"):
        candidates.extend(SAVE_PATH.glob(pattern))

    candidates = [path for path in candidates if path not in existing_paths and path.exists()]
    if not candidates:
        return None

    candidates.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    return candidates[0]


def extract_dash_subtitles_with_n_m3u8dl(playback, filename):
    temp_name = f"{filename}.fr.{int(time.time())}"
    existing_paths = set(SAVE_PATH.glob(f"{temp_name}*"))
    command = (
        f'{N_M3U8DL} "{playback.manifest_url}" '
        f'--sub-only --save-dir "{SAVE_PATH}" --save-name "{temp_name}"'
    )
    if SERVICE_PROXY:
        command += f' --custom-proxy "{SERVICE_PROXY}"'

    print(f"{bcolors.LIGHTBLUE}{icons.ICON_WAITING} Extracting French DASH subtitles with N_m3u8DL-RE...{bcolors.ENDC}", flush=True)
    result = subprocess.run(command, shell=True)
    if result.returncode != 0:
        print(f"{bcolors.WARNING}{icons.ICON_WARNING} Subtitle extraction command exited with code {result.returncode}.{bcolors.ENDC}")
        return None

    subtitle_path = find_created_subtitle_file(temp_name, existing_paths)
    if not subtitle_path:
        print(f"{bcolors.WARNING}{icons.ICON_WARNING} N_m3u8DL-RE completed, but no subtitle file was found for {temp_name}.{bcolors.ENDC}")
        return None

    return subtitle_path


def get_subtitle_url(playback):
    subtitles = [subtitle for subtitle in playback.subtitles or [] if isinstance(subtitle, dict)]
    hls_subtitles = [subtitle for subtitle in subtitles if subtitle.get("kind") == "hls-vtt" and subtitle.get("url")]
    if not hls_subtitles:
        return None

    hls_subtitles.sort(key=lambda item: clean_text(item.get("lang")).lower().startswith("fr"), reverse=True)
    return hls_subtitles[0].get("url")


def save_translated_subtitles(playback, filename, show_temp_delete=True):
    subtitle_url = get_subtitle_url(playback)
    if not subtitle_url:
        dash_subtitles = [subtitle for subtitle in playback.subtitles or [] if str(subtitle.get("kind", "")).startswith("dash")]
        if dash_subtitles:
            subtitle_path = extract_dash_subtitles_with_n_m3u8dl(playback, filename)
            if not subtitle_path:
                return None

            if subtitle_path.suffix.lower() == ".srt":
                cues = parse_srt(subtitle_path.read_text(encoding="utf-8-sig", errors="replace"))
            elif subtitle_path.suffix.lower() == ".vtt":
                cues = parse_vtt(subtitle_path.read_text(encoding="utf-8-sig", errors="replace"))
            else:
                print(f"{bcolors.WARNING}{icons.ICON_WARNING} Unsupported subtitle file format: {subtitle_path}{bcolors.ENDC}")
                return None

            if not cues:
                print(f"{bcolors.WARNING}{icons.ICON_WARNING} No subtitle cues found in extracted France TV subtitle file.{bcolors.ENDC}")
                return None

            output_path = SAVE_PATH / f"{filename}.en.srt"
            print(f"{bcolors.LIGHTBLUE}{icons.ICON_WAITING} Translating French subtitles to English SRT...{bcolors.ENDC}")
            write_srt(translate_cues(cues), output_path)
            print(f"{bcolors.OKGREEN}{icons.ICON_SUCCESS} English subtitles saved: {bcolors.ENDC}{output_path}")
            delete_temp_subtitle_file(subtitle_path, show_success=show_temp_delete)
            return output_path
        else:
            print(f"{bcolors.WARNING}{icons.ICON_WARNING} No French subtitle URL found in France TV manifest.{bcolors.ENDC}")
        return None

    response = session.get(subtitle_url, headers=DEFAULT_HEADERS, timeout=30)
    response.raise_for_status()
    cues = parse_vtt(response.text)
    if not cues:
        print(f"{bcolors.WARNING}{icons.ICON_WARNING} No subtitle cues found in France TV VTT response.{bcolors.ENDC}")
        return None

    output_path = SAVE_PATH / f"{filename}.en.srt"
    print(f"{bcolors.LIGHTBLUE}{icons.ICON_WAITING} Translating French subtitles to English SRT...{bcolors.ENDC}")
    write_srt(translate_cues(cues), output_path)
    print(f"{bcolors.OKGREEN}{icons.ICON_SUCCESS} English subtitles saved: {bcolors.ENDC}{output_path}")
    return output_path


def maybe_save_translated_subtitles(playback, filename, auto_download=False, show_temp_delete=True):
    if auto_download:
        return save_translated_subtitles(playback, filename, show_temp_delete=show_temp_delete)

    try:
        user_input = input("Do you wish to save translated English subtitles? Y or N: ").strip().lower()
    except EOFError:
        user_input = "n"

    if user_input != "y":
        return None

    return save_translated_subtitles(playback, filename, show_temp_delete=show_temp_delete)


def safe_name(value):
    value = clean_text(value).replace("'", "")
    value = re.sub(r'[\\/:*?"<>|]', " ", value)
    value = re.sub(r"\s+", ".", value)
    return value.strip(".") or "Unknown"


def format_filename(metadata, resolution):
    title = safe_name(metadata.title)
    season_episode = ""
    if metadata.season is not None and metadata.episode is not None:
        season_episode = f"S{int(metadata.season):02}E{int(metadata.episode):02}"
    elif metadata.season is not None:
        season_episode = f"S{int(metadata.season):02}"

    parts = [title]
    if season_episode:
        parts.append(season_episode)
    parts.extend([resolution, "FRTV", "WEB-DL", "AAC2.0", "H.264"])
    return ".".join(part for part in parts if part and part != "Unknown")


def build_download_command(playback, filename, keys=None, interactive=False, quality=None, include_subtitles=False):
    if interactive:
        selectors = ""
    else:
        subtitle_selector = "--select-subtitle all" if include_subtitles else "--drop-subtitle all"
        selectors = f"{video_selector(quality)} --select-audio best {subtitle_selector} "
    command = (
        f'{N_M3U8DL} "{playback.manifest_url}" '
        f'{selectors}'
        f'-mt -M format=mkv --save-dir "{SAVE_PATH}" --save-name "{filename}"'
    )

    if keys:
        command += " " + " ".join(f"--key {key}" for key in keys)

    if SERVICE_PROXY:
        command += f' --custom-proxy "{SERVICE_PROXY}"'

    return command


def highest_stream_resolution(streams, default="Unknown"):
    heights = []
    for stream in streams or []:
        if stream.get("type") != "Vid":
            continue
        match = re.search(r"x(\d+)", stream.get("resolution") or "")
        if match:
            heights.append(int(match.group(1)))
    return f"{max(heights)}p" if heights else default


def resolve_video(video_url, interactive=False, quality=None, include_subtitles=False):
    video_url = canonical_url(video_url)
    video_id = extract_video_id(video_url)
    metadata = search_metadata(video_url, video_id)
    playback = get_playback_info(video_url, metadata)

    keys = []
    if playback.drm and playback.manifest_type == "mpd":
        if not playback.pssh:
            try:
                playback.pssh = get_pssh_from_manifest(playback.manifest_url)
            except Exception as exc:
                print(f"{icons.ICON_WARNING} {bcolors.WARNING}Could not extract PSSH: {exc}{bcolors.ENDC}")
        if playback.license_url and playback.pssh:
            keys = get_keys(playback.pssh, playback.license_url, metadata)

    resolution = highest_stream_resolution(playback.streams, get_resolution(playback))
    filename = format_filename(metadata, resolution)
    filename = apply_quality_to_filename(filename, quality)
    command = build_download_command(playback, filename, keys, interactive=interactive, quality=quality, include_subtitles=include_subtitles)
    return playback, keys, resolution, filename, command


def run_with_spinner(callback):
    spinner = Spinner()
    spinner.start()
    try:
        result = callback()
    except Exception:
        spinner.stop()
        raise
    spinner.stop()
    return result


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
    print(f"{bcolors.LIGHTBLUE}Title: {bcolors.ENDC}{metadata.episode_title or 'Unknown'}")
    print(f"{bcolors.LIGHTBLUE}Date Aired: {bcolors.ENDC}{metadata.aired_date or 'Unknown'}")
    print(f"{bcolors.LIGHTBLUE}Description: {bcolors.ENDC}{metadata.description or 'No Description'}")


def print_info_mode(video_url):
    if not is_episode_url(video_url):
        raise ValueError("Info mode requires a France TV episode/video URL.")
    playback, keys, resolution, filename, command = run_with_spinner(lambda: resolve_video(video_url))
    manifest_label = "DASH Manifest URL" if playback.manifest_type == "mpd" else "HLS Manifest URL"
    print(f"{bcolors.LIGHTBLUE}{manifest_label}: {bcolors.ENDC}{playback.manifest_url}")
    for key in keys:
        print(f"{bcolors.OKGREEN}KEYS: {bcolors.ENDC}--key {key}")
    print_streams(playback.streams)
    print_episode_metadata(playback.metadata)
    print(f"\n{bcolors.YELLOW}Suggested filename: {bcolors.ENDC}{filename}.mkv")


def episode_series_number(item):
    try:
        return int(item.get("season"))
    except (TypeError, ValueError):
        return None


def episode_number(item):
    try:
        return int(item.get("episode"))
    except (TypeError, ValueError):
        return None


def episode_tree_label(item):
    number = episode_number(item)
    title = clean_text(item.get("title")) or item.get("id") or "Untitled"
    return str(number) if number is not None else "-", title


def group_episode_items_by_series(episode_items):
    grouped = {}
    for item in episode_items:
        season = episode_series_number(item)
        label = f"Series {season}" if season is not None else "Episodes"
        grouped.setdefault(label, []).append(item)
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


def list_episode_items(episode_items):
    if not episode_items:
        print(f"{icons.ICON_FAILURE} {bcolors.FAIL}No France TV episodes found.{bcolors.ENDC}")
        return
    show_title = episode_items[0].get("show_title", "France TV")
    grouped_items = group_episode_items_by_series(episode_items)
    group_labels = sorted(grouped_items, key=series_group_sort_key)
    series_summary = ",  ".join(f"{label}({len(grouped_items[label])})" for label in group_labels)
    print(f"{icons.ICON_SUCCESS} {bcolors.OKGREEN}Found {len(episode_items)} France TV episodes{bcolors.ENDC}")
    print()
    print_series_rule("France TV Series", show_title)
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
            print(f"{bcolors.GRAY}{group_child_prefix}{url_branch} {bcolors.ENDC}{bcolors.LIGHTBLUE}{item['url']}{bcolors.ENDC}")


def parse_selector_part(selector_part):
    match = re.fullmatch(r"s(?P<season>\d{2}|\d{4})(?:e(?P<episode>\d{2,3}))?", selector_part)
    if not match:
        raise ValueError(
            "Download selector must be sXXeXX, sXXXXeXX, sXX, sXXXX, or a matching range. "
            "Examples: s07e01, s07, s07e01-s07e02"
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
    season_label = f"S{season:04d}" if season >= 1000 else f"S{season:02d}"
    if episode is not None:
        return f"{season_label}E{episode:02d}"
    return season_label


def select_episode_items(series_url, selector):
    parsed_selector = parse_download_selector(selector)
    episode_items = collect_episode_items(series_url)
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
        raise ValueError(f"No France TV episodes found for selector {format_download_selector(parsed_selector)}.")
    selected.sort(key=lambda item: (episode_series_number(item) or 0, episode_number(item) or 0, item.get("id") or ""))
    return selected


def print_download_queue(episode_items):
    print()
    print(f"{bcolors.LIGHTBLUE}Download queue:{bcolors.ENDC}")
    for item in episode_items:
        season = episode_series_number(item)
        episode = episode_number(item)
        selector = format_queue_selector(season, episode) if season is not None and episode is not None else item["id"]
        _, title = episode_tree_label(item)
        print(f"{selector} {title}")


def print_playback_details(playback, keys, command):
    label = "MPD URL" if playback.manifest_type == "mpd" else "M3U8 URL"
    print(f"{bcolors.LIGHTBLUE}{label}: {bcolors.ENDC}{playback.manifest_url}")
    print(f"{bcolors.LIGHTBLUE}DRM: {bcolors.ENDC}{'Yes' if playback.drm else 'No'}")

    if playback.subtitles:
        print(f"{bcolors.LIGHTBLUE}Subtitle tracks: {bcolors.ENDC}{len(playback.subtitles)}")
    if playback.license_url:
        print(f"{bcolors.RED}License URL: {bcolors.ENDC}{playback.license_url}")
    if playback.pssh:
        print(f"{bcolors.LIGHTBLUE}PSSH: {bcolors.ENDC}{playback.pssh}")
    if keys:
        print(f"{bcolors.OKGREEN}KEYS: {bcolors.ENDC}{keys}")

    print(f"{bcolors.YELLOW}DOWNLOAD COMMAND: {bcolors.ENDC}")
    print(mask_proxy_command(command))


def maybe_download(command, auto_download=False):
    if auto_download:
        print(f"{icons.ICON_WAITING} {bcolors.OKBLUE}Downloading video...{bcolors.ENDC}")
        subprocess.run(command, shell=True)
        return

    try:
        user_input = input("Do you wish to download? Y or N: ").strip().lower()
    except EOFError:
        user_input = "n"
    if user_input == "y":
        print(f"{icons.ICON_WAITING} {bcolors.OKBLUE}Downloading video...{bcolors.ENDC}")
        subprocess.run(command, shell=True)
    else:
        print(f"{icons.ICON_FAILURE} {bcolors.RED}Download cancelled{bcolors.ENDC}")


def process_video(video_url, auto_download=False, interactive=False, quality=None, save_native_subs=False):
    video_url = canonical_url(video_url)
    print(f"{icons.ICON_INFO} {bcolors.LIGHTBLUE}Processing: {bcolors.ENDC}{video_url}")
    playback, keys, resolution, filename, command = run_with_spinner(lambda: resolve_video(video_url, interactive=interactive, quality=quality, include_subtitles=save_native_subs))
    metadata = playback.metadata
    episode_str = f"S{metadata.season:02d}E{metadata.episode:02d}" if metadata.season and metadata.episode else ""
    if metadata.title != "Unknown" or episode_str or metadata.episode_title:
        print(f"{icons.ICON_SUCCESS} {bcolors.OKGREEN}{metadata.title}{bcolors.ENDC} {episode_str} - {metadata.episode_title or ''}".rstrip())
    print(f"{icons.ICON_SUCCESS} {bcolors.OKGREEN}Player media ID: {bcolors.ENDC}{metadata.video_id}")
    if playback.drm and playback.manifest_type != "mpd":
        print(f"{icons.ICON_WARNING} {bcolors.WARNING}France TV reports DRM, but this script has no licence endpoint for this item yet.{bcolors.ENDC}")
    print_playback_details(playback, keys, command)
    maybe_save_translated_subtitles(playback, filename, auto_download=auto_download, show_temp_delete=not save_native_subs)
    maybe_download(command, auto_download=auto_download)


def download_selected_episodes(series_url, selector, quality=None, auto_confirm=False, save_native_subs=False):
    episode_items = select_episode_items(series_url, selector)
    print_download_queue(episode_items)
    episode_word = "episode" if len(episode_items) == 1 else "episodes"
    this_or_these = "this" if len(episode_items) == 1 else "these"
    if not confirm_download(f"Do you wish to download {this_or_these} {len(episode_items)} {episode_word}? Y or N: ", auto_confirm=auto_confirm):
        print(f"{icons.ICON_FAILURE} {bcolors.RED}Download cancelled{bcolors.ENDC}")
        return

    for index, item in enumerate(episode_items, 1):
        print()
        print(f"{icons.ICON_WAITING} {bcolors.LIGHTBLUE}Downloading {index}/{len(episode_items)}: {bcolors.ENDC}{item['url']}")
        process_video(item["url"], auto_download=True, quality=quality, save_native_subs=save_native_subs)


def export_episode_urls(episode_items):
    if not episode_items:
        print(f"{icons.ICON_FAILURE} {bcolors.FAIL}No France TV episodes found to export.{bcolors.ENDC}")
        return

    export_dir = Path(__file__).resolve().parents[2] / "export"
    export_dir.mkdir(parents=True, exist_ok=True)
    show_title = episode_items[0].get("show_title", "France TV")
    output_path = export_dir / f"frtv_{safe_name(show_title)}_episodes.txt"
    output_path.write_text("\n".join(item["url"] for item in episode_items) + "\n", encoding="utf-8")
    print(f"{icons.ICON_SUCCESS} {bcolors.OKGREEN}Exported list: {output_path}{bcolors.ENDC}")


def main(video_url, downloads_path, wvd_device_path, mode="auto", export_list=False, download_selector=None, quality=None, auto_confirm=False, save_native_subs=False):
    """Eurovine entry point for France TV (Widevine where available)."""
    try:
        if not video_url:
            raise ValueError("No France TV URL provided.")
        if not downloads_path or not wvd_device_path:
            raise ValueError("Eurovine config requires downloads_path and wvd_device_path for France TV.")

        configure_service(downloads_path, wvd_device_path)
        video_url = video_url.strip()
        print(f"{icons.ICON_INFO} {bcolors.LIGHTBLUE}France TV URL: {bcolors.ENDC}{video_url}")

        if mode == "list":
            if is_episode_url(video_url):
                print(f"{icons.ICON_FAILURE} {bcolors.FAIL}List mode requires a France TV series URL, not an episode URL.{bcolors.ENDC}")
                return
            print(f"{icons.ICON_WAITING} {bcolors.LIGHTBLUE}Retrieving series information.....{bcolors.ENDC}")
            episode_items = collect_episode_items(video_url)
            list_episode_items(episode_items)
            if export_list:
                export_episode_urls(episode_items)
            return

        if mode == "download":
            if is_episode_url(video_url):
                print(f"{icons.ICON_FAILURE} {bcolors.FAIL}Download selector mode requires a France TV series URL, not an episode URL.{bcolors.ENDC}")
                return
            print(f"{icons.ICON_WAITING} {bcolors.LIGHTBLUE}Retrieving series information.....{bcolors.ENDC}")
            download_selected_episodes(video_url, download_selector, quality, auto_confirm, save_native_subs=save_native_subs)
            return

        if mode == "info":
            if not is_episode_url(video_url):
                print(f"{icons.ICON_FAILURE} {bcolors.FAIL}Info mode requires a France TV episode URL, not a series URL.{bcolors.ENDC}")
                return
            print_info_mode(video_url)
            return

        if export_list:
            if is_episode_url(video_url):
                print(f"{icons.ICON_FAILURE} {bcolors.FAIL}Export mode requires a France TV series URL, not an episode URL.{bcolors.ENDC}")
                return
            print(f"{icons.ICON_WAITING} {bcolors.LIGHTBLUE}Retrieving series information.....{bcolors.ENDC}")
            export_episode_urls(collect_episode_items(video_url))
            return

        if is_episode_url(video_url):
            process_video(video_url, auto_download=auto_confirm, interactive=(mode == "interactive"), quality=quality, save_native_subs=save_native_subs)
            return

        print(f"{icons.ICON_WARNING} {bcolors.WARNING}Series URLs require a flag. Use --list/-l to list episodes, --export/-x to export episode URLs, or --download/-d SELECTOR to download selected episodes.{bcolors.ENDC}")
    except Exception as exc:
        print(f"{icons.ICON_FAILURE} {bcolors.FAIL}Error: {exc}{bcolors.ENDC}")
