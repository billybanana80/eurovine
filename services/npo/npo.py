import base64
import binascii
import html
import json
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin, urlparse

import requests
import urllib3
from beaupy.spinners import Spinner
from pywidevine.cdm import Cdm
from pywidevine.device import Device
from pywidevine.pssh import PSSH
import icons
from colors import bcolors
from services.proxy import current_proxy_url, mask_proxy_command

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

SERVICE_NAME = "npo"
SCRIPT_DIR = Path(__file__).resolve().parent
NPO_DRM_TYPE = "widevine"
config = {}
SAVE_PATH = None
WVD_PATH = ""
N_M3U8DL = "N_m3u8DL-RE"
TRANSLATE_URL = "https://translate.googleapis.com/translate_a/single"
TRANSLATE_BATCH_MARKER = "@@NPOBREAK@@"
TRANSLATE_BATCH_SIZE = 40
TRANSLATE_BATCH_CHAR_LIMIT = 4500

# NPO specific endpoints
NPO_ENDPOINTS = {
    "player_token": "https://npo.nl/start/api/domain/player-token?productId={product_id}",
    "streams": "https://prod.npoplayer.nl/stream-link",
    "license": "https://npo-drm-gateway.samgcloud.nepworldwide.nl/authentication",
    "search": "https://npo.nl/start/api/domain/search-collection-items",
    "user_profiles": "https://npo.nl/start/api/domain/user-profiles",
}

# NPO URL patterns
URL_PATTERNS = {
    "video": r"^(?:https?://(?:www\.)?npo\.nl/start/)?(?:video|afspelen)/(?P<slug>[^/]+)",
    "serie": r"^(?:https?://(?:www\.)?npo\.nl/start/)?serie/(?P<slug>[^/]+)",
    "episode": r"^(?:https?://(?:www\.)?npo\.nl/start/)?serie/(?P<series_slug>[^/]+)/seizoen-(?P<season>[^/]+)/(?P<episode_slug>[^/]+)/afspelen",
}

BASE_URL = "https://npo.nl"
START_URL = f"{BASE_URL}/start"


session = requests.Session()
SERVICE_PROXY = None


def configure_service(downloads_path, wvd_device_path):
    global config, SAVE_PATH, WVD_PATH, SERVICE_PROXY
    SAVE_PATH = Path(downloads_path)
    WVD_PATH = wvd_device_path
    config = {"npo_drm_type": NPO_DRM_TYPE}
    SERVICE_PROXY = current_proxy_url()
    session.proxies.clear()
    session.verify = True
    if SERVICE_PROXY:
        session.proxies.update({"http": SERVICE_PROXY, "https": SERVICE_PROXY})
        session.verify = False

DEFAULT_HEADERS = {
    "Accept": "*/*",
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
    series_title: Optional[str] = None
    series_slug: Optional[str] = None
    product_id: Optional[str] = None
    guid: Optional[str] = None
    slug: Optional[str] = None


@dataclass
class PlaybackInfo:
    manifest_url: str
    manifest_type: str
    license_url: Optional[str] = None
    pssh: Optional[str] = None
    drm_token: Optional[str] = None
    metadata: Metadata = field(default_factory=Metadata)
    is_encrypted: bool = True
    subtitles: list = field(default_factory=list)
    streams: list = field(default_factory=list)


def clean_text(value):
    return re.sub(r"\s+", " ", html.unescape(str(value or ""))).strip()


def int_value(value):
    match = re.search(r"\d+", clean_text(value))
    return int(match.group(0)) if match else None


def timestamp_date(value):
    if value in (None, ""):
        return "Unknown"
    try:
        return datetime.fromtimestamp(int(value), tz=timezone.utc).strftime("%Y-%m-%d")
    except (TypeError, ValueError, OSError):
        return clean_text(value).split("T", 1)[0] or "Unknown"


def synopsis_text(item):
    synopsis = (item or {}).get("synopsis")
    if isinstance(synopsis, dict):
        return (
            clean_text(synopsis.get("long"))
            or clean_text(synopsis.get("short"))
            or clean_text(synopsis.get("brief"))
            or "No Description"
        )
    return clean_text(synopsis) or "No Description"


def extract_video_info(url: str):
    """Extract video/series info from NPO URL."""
    url = url.strip()
    
    # Try episode pattern first (most specific)
    m = re.match(URL_PATTERNS["episode"], url)
    if m:
        return {
            "type": "episode",
            "series_slug": m.group("series_slug"),
            "season": m.group("season"),
            "episode_slug": m.group("episode_slug"),
            "slug": m.group("episode_slug"),
        }
    
    # Try video pattern
    m = re.match(URL_PATTERNS["video"], url)
    if m:
        return {
            "type": "video",
            "slug": m.group("slug"),
        }
    
    # Try serie pattern
    m = re.match(URL_PATTERNS["serie"], url)
    if m:
        return {
            "type": "serie",
            "slug": m.group("slug"),
        }
    
    raise ValueError(f"Could not extract video info from URL: {url}")


def fetch_next_data(slug: str, url_type: str = "video"):
    """Fetch and parse __NEXT_DATA__ from NPO page."""
    if url_type == "serie":
        page_url = f"https://npo.nl/start/serie/{slug}"
    else:
        page_url = f"https://npo.nl/start/afspelen/{slug}"
    
    try:
        response = session.get(page_url, headers={
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) Firefox/143.0",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }, timeout=35)
        response.raise_for_status()
    except requests.RequestException as exc:
        raise ConnectionError(f"Failed to fetch NPO page metadata: {exc}") from exc
    
    match = re.search(r'<script id="__NEXT_DATA__" type="application/json">({.*?})</script>', response.text, re.DOTALL)
    if not match:
        raise RuntimeError("Failed to extract __NEXT_DATA__")
    
    return json.loads(match.group(1))


def canonical_url(value):
    value = clean_text(value)
    if re.match(r"^https?://", value, re.IGNORECASE):
        return value
    if value.startswith("/"):
        return urljoin(BASE_URL, value)
    return f"{START_URL}/serie/{value.strip('/')}"


def is_episode_url(url):
    path = urlparse(url).path
    return "/start/afspelen/" in path or re.search(r"/start/serie/[^/]+/seizoen-[^/]+/[^/]+/afspelen/?$", path)


def is_series_url(url):
    return "/start/serie/" in urlparse(url).path and not is_episode_url(url)


def fetch_page_payload(url):
    source_url = canonical_url(url)
    response = session.get(
        source_url,
        headers={
            **DEFAULT_HEADERS,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        },
        timeout=35,
    )
    response.raise_for_status()
    match = re.search(
        r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
        response.text,
        re.S,
    )
    if not match:
        raise RuntimeError("Could not find NPO page state in __NEXT_DATA__.")
    try:
        return json.loads(html.unescape(match.group(1)))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Could not parse NPO page state: {exc}") from exc


def page_props(payload):
    return (payload.get("props") or {}).get("pageProps") or {}


def dehydrated_queries(payload):
    return ((page_props(payload).get("dehydratedState") or {}).get("queries") or [])


def query_key_text(query):
    return " ".join(str(part) for part in (query.get("queryKey") or []))


def query_data(query):
    return (query.get("state") or {}).get("data")


def walk(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk(child)


def looks_like_program(item):
    return (
        isinstance(item, dict)
        and clean_text(item.get("productId"))
        and clean_text(item.get("slug"))
        and ("synopsis" in item or "programKey" in item)
    )


def collect_series_info(payload):
    for query in dehydrated_queries(payload):
        data = query_data(query)
        if isinstance(data, dict) and query_key_text(query).startswith("series:detail"):
            return data

    for node in walk(page_props(payload)):
        if isinstance(node, dict) and node.get("type") in ("timeless_series", "series") and node.get("title"):
            return node

    return {}


def collect_seasons(payload):
    seasons = []
    seen = set()
    for query in dehydrated_queries(payload):
        data = query_data(query)
        if not isinstance(data, list) or not query_key_text(query).startswith("series:seasons"):
            continue
        for season in data:
            if not isinstance(season, dict):
                continue
            season_id = clean_text(season.get("guid") or season.get("slug") or season.get("seasonKey"))
            if not season_id or season_id in seen:
                continue
            seen.add(season_id)
            seasons.append(season)
    return seasons


def collect_program_items(payload):
    episodes = []
    seen = set()

    for query in dehydrated_queries(payload):
        data = query_data(query)
        key = query_key_text(query)
        candidates = []
        if isinstance(data, list) and key.startswith("programs:"):
            candidates = data
        elif isinstance(data, dict) and key.startswith("program:detail"):
            candidates = [data]

        for item in candidates:
            if not looks_like_program(item):
                continue
            item_id = clean_text(item.get("productId") or item.get("guid") or item.get("slug"))
            if item_id and item_id not in seen:
                seen.add(item_id)
                episodes.append(item)

    if episodes:
        return episodes

    for node in walk(page_props(payload)):
        if not looks_like_program(node):
            continue
        item_id = clean_text(node.get("productId") or node.get("guid") or node.get("slug"))
        if item_id and item_id not in seen:
            seen.add(item_id)
            episodes.append(node)

    return episodes


def merge_program_lists(payloads):
    episodes = []
    seen = set()
    for payload in payloads:
        for item in collect_program_items(payload):
            item_id = clean_text(item.get("productId") or item.get("guid") or item.get("slug"))
            if item_id and item_id not in seen:
                seen.add(item_id)
                episodes.append(item)
    return episodes


def list_show_title(item, series_info=None):
    series = item.get("series") or {}
    return (
        clean_text(series.get("title"))
        or clean_text((series_info or {}).get("title"))
        or "Unknown Show"
    )


def list_season_number(item):
    season = item.get("season") or {}
    value = season.get("seasonKey") or season.get("slug")
    match = re.search(r"\d+", clean_text(value))
    return int(match.group(0)) if match else 1


def list_episode_number(item):
    value = clean_text(item.get("programKey"))
    match = re.search(r"\d+", value)
    return int(match.group(0)) if match else 1


def list_video_url(item):
    slug = clean_text(item.get("slug"))
    return f"{START_URL}/afspelen/{slug}" if slug else ""


def episode_sort_key(item):
    return (
        list_season_number(item),
        list_episode_number(item),
        clean_text(item.get("title")).lower(),
        clean_text(item.get("productId")),
    )


def collect_episode_items(series_url):
    source_url = canonical_url(series_url)
    payload = fetch_page_payload(source_url)
    series_info = collect_series_info(payload)
    payloads = [payload]

    series_slug_match = re.search(r"/start/serie/(?P<slug>[^/?#]+)", urlparse(source_url).path)
    series_slug = series_slug_match.group("slug") if series_slug_match else ""
    for season in collect_seasons(payload):
        season_slug = clean_text(season.get("slug"))
        if not season_slug:
            continue
        season_url = f"{START_URL}/serie/{series_slug}/afleveringen/{season_slug}"
        try:
            payloads.append(fetch_page_payload(season_url))
        except Exception as exc:
            print(f"{bcolors.WARNING}{icons.ICON_WARNING} Could not fetch NPO season {season_slug}: {exc}{bcolors.ENDC}")

    episodes = sorted(merge_program_lists(payloads), key=episode_sort_key)
    if not episodes:
        raise RuntimeError("No NPO episodes found for this URL.")

    return [
        {
            "show_title": list_show_title(item, series_info),
            "season": list_season_number(item),
            "episode": list_episode_number(item),
            "title": clean_text(item.get("title")) or f"Episode {list_episode_number(item)}",
            "url": list_video_url(item),
        }
        for item in episodes
        if list_video_url(item)
    ]


def get_metadata_from_next_data(next_data: dict, url_info: dict) -> Metadata:
    """Extract metadata from __NEXT_DATA__."""
    page_props = next_data.get("props", {}).get("pageProps", {})
    queries = page_props.get("dehydratedState", {}).get("queries", [])
    
    def get_data(fragment: str):
        for q in queries:
            if fragment in str(q.get("queryKey", "")):
                return q.get("state", {}).get("data")
        return None
    
    metadata = Metadata()
    
    if url_info["type"] == "serie":
        series_data = get_data("series:detail-")
        if series_data:
            metadata.title = series_data.get("title", "Unknown")
            metadata.series_title = series_data.get("title")
            metadata.series_slug = url_info["slug"]
            metadata.slug = url_info["slug"]
            metadata.description = synopsis_text(series_data)
        
        # Get first episode from first season for series
        seasons = get_data("series:seasons-") or []
        if seasons:
            first_season = seasons[0]
            eps = get_data(f"programs:season-{first_season['guid']}") or []
            if eps:
                first_ep = eps[0]
                metadata.season = int(first_season.get("seasonKey", 1))
                metadata.episode = int(first_ep.get("programKey", 1))
                metadata.episode_title = first_ep.get("title", "")
                metadata.guid = first_ep.get("guid")
                metadata.product_id = first_ep.get("productId")
                metadata.video_id = first_ep.get("guid")
                metadata.aired_date = timestamp_date(first_ep.get("firstBroadcastDate") or first_ep.get("publishedDateTime"))
                description = synopsis_text(first_ep)
                metadata.description = translate_text(description) if description != "No Description" else description
    
    elif url_info["type"] == "video" or url_info["type"] == "episode":
        # Try to get program data
        program_data = get_data("program:detail-")
        if not program_data:
            # Try the first query if program data not found
            program_data = queries[0].get("state", {}).get("data") if queries else None
        
        if program_data:
            metadata.title = program_data.get("title", "Unknown")
            metadata.guid = program_data.get("guid")
            metadata.product_id = program_data.get("productId")
            metadata.video_id = program_data.get("guid")
            metadata.slug = url_info.get("slug")
            metadata.episode_title = program_data.get("title", "")
            metadata.aired_date = timestamp_date(program_data.get("firstBroadcastDate") or program_data.get("publishedDateTime"))
            description = synopsis_text(program_data)
            metadata.description = translate_text(description) if description != "No Description" else description

            series = program_data.get("series") or {}
            season = program_data.get("season") or {}
            metadata.series_title = (
                series.get("title")
                or program_data.get("seriesTitle")
                or metadata.series_title
            )
            metadata.series_slug = series.get("slug") or url_info.get("series_slug") or metadata.series_slug
            metadata.season = int_value(season.get("seasonKey") or season.get("slug")) or metadata.season
            metadata.episode = int_value(program_data.get("programKey")) or metadata.episode
            
            # For episodes
            if url_info["type"] == "episode":
                metadata.season = int_value(url_info.get("season")) or metadata.season
                metadata.episode = int_value(url_info.get("episode_slug")) or metadata.episode
    
    return metadata


def get_player_token(product_id: str) -> str:
    """Get player token for the content."""
    token_url = NPO_ENDPOINTS["player_token"].format(product_id=product_id)
    try:
        response = session.get(token_url, headers={
            "Referer": "https://npo.nl/start/video/",
            "User-Agent": DEFAULT_HEADERS["User-Agent"],
        }, timeout=30)
        response.raise_for_status()
    except requests.RequestException as exc:
        raise ConnectionError(f"Failed to get NPO player token: {exc}") from exc
    return response.json().get("jwt")


def get_stream_info(product_id: str, jwt: str, drm_type: str = "widevine") -> dict:
    """Get stream information from NPO."""
    try:
        response = session.post(
            NPO_ENDPOINTS["streams"],
            json={
                "profileName": "dash",
                "drmType": drm_type,
                "referrerUrl": "https://npo.nl/start/video/",
                "ster": {
                    "identifier": "npo-app-desktop",
                    "deviceType": 4,
                    "player": "web"
                },
            },
            headers={
                "Authorization": jwt,
                "Content-Type": "application/json",
                "Origin": "https://npo.nl",
                "Referer": "https://npo.nl/start/video/",
                "User-Agent": DEFAULT_HEADERS["User-Agent"],
            },
            timeout=30,
        )

        if response.status_code == 450:
            raise PermissionError(
                "NPO refused the stream with HTTP 450. This is usually the age-restricted viewing window: "
                "try again between 20:00 and 06:00 Netherlands time."
            )
        response.raise_for_status()
    except requests.RequestException as exc:
        raise ConnectionError(f"Failed to get NPO stream information: {exc}") from exc
    data = response.json()   


    if "error" in data:
        raise PermissionError(f"Stream error: {data['error']}")
    
    return data


def get_playback_info(video_url: str, metadata: Metadata) -> PlaybackInfo:
    """Get playback information for the video."""
    url_info = extract_video_info(video_url)
    
    # For series, we need to handle differently
    if url_info["type"] == "serie":
        # Get first episode from series
        next_data = fetch_next_data(url_info["slug"], "serie")
        metadata = get_metadata_from_next_data(next_data, url_info)
        if not metadata.product_id:
            raise ValueError("Could not find product ID for series")
    
    # For video/episode, get metadata if not already provided
    if not metadata.product_id:
        next_data = fetch_next_data(url_info["slug"], "video")
        metadata = get_metadata_from_next_data(next_data, url_info)
    
    if not metadata.product_id:
        raise ValueError("Could not find product ID for content")
    
    # print(f"{icons.ICON_INFO} Product ID: {metadata.product_id}")
    
    # Get player token
    jwt = get_player_token(metadata.product_id)
    if not jwt:
        raise ValueError("Could not get player token")
    
    # Get stream info
    drm_type = config.get("npo_drm_type", "widevine")
    stream_data = get_stream_info(metadata.product_id, jwt, drm_type)
    
    stream = stream_data.get("stream", {})
    manifest_url = stream.get("streamURL") or stream.get("url")
    if not manifest_url:
        raise ValueError("No stream URL in response")
    
    # Check if encrypted
    is_encrypted = not ("unencrypted" in manifest_url.lower())
    
    # Get DRM token if encrypted
    drm_token = None
    license_url = None
    pssh = None
    
    if is_encrypted:
        drm_token = stream.get("drmToken") or stream.get("token") or stream.get("drm_token")
        license_url = NPO_ENDPOINTS["license"]
    
    manifest_type = "hls" if ".m3u8" in manifest_url.lower() else "dash"

    return PlaybackInfo(
        manifest_url=manifest_url,
        manifest_type=manifest_type,
        license_url=license_url,
        pssh=pssh,
        drm_token=drm_token,
        metadata=metadata,
        is_encrypted=is_encrypted,
        subtitles=(stream_data.get("assets") or {}).get("subtitles") or [],
    )


def get_pssh_from_manifest(manifest_url: str) -> str:
    """Extract PSSH from DASH manifest."""
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


def build_license_headers(metadata: Metadata, drm_token: str):
    """Build license request headers."""
    return {
        "Content-Type": "application/octet-stream",
        "Origin": "https://npo.nl",
        "Referer": "https://npo.nl/",
        "User-Agent": DEFAULT_HEADERS["User-Agent"],
    }


def post_license_challenge(license_url: str, challenge: bytes, metadata: Metadata, drm_token: str) -> bytes:
    """Post license challenge to NPO DRM server."""
    response = session.post(
        license_url,
        params={"custom_data": drm_token},
        data=challenge,
        headers=build_license_headers(metadata, drm_token),
        timeout=30
    )
    try:
        response.raise_for_status()
    except requests.exceptions.HTTPError as exc:
        raise RuntimeError(f"NPO license request failed: {exc}. Response: {response.text[:300]}") from exc
    return response.content


def get_keys(pssh: str, license_url: str, metadata: Metadata, drm_token: str):
    """Get decryption keys using Widevine."""
    try:
        pssh = PSSH(pssh)
    except (binascii.Error, ValueError) as exc:
        raise ValueError(f"Could not parse PSSH: {exc}") from exc

    device = Device.load(WVD_PATH)
    cdm = Cdm.from_device(device)
    session_id = cdm.open()

    try:
        challenge = cdm.get_license_challenge(session_id, pssh)
        licence = post_license_challenge(license_url, challenge, metadata, drm_token)
        cdm.parse_license(session_id, licence)
        return [f"{key.kid.hex}:{key.key.hex()}" for key in cdm.get_keys(session_id) if key.type == "CONTENT"]
    finally:
        cdm.close(session_id)


def get_dash_resolution(mpd_url: str) -> str:
    """Get max resolution from DASH manifest."""
    response = session.get(mpd_url, headers=DEFAULT_HEADERS, timeout=30)
    response.raise_for_status()
    root = ET.fromstring(response.content)
    heights = [
        int(rep.get("height"))
        for rep in root.findall(".//{urn:mpeg:dash:schema:mpd:2011}Representation")
        if rep.get("height")
    ]
    return f"{max(heights)}p" if heights else "Unknown"


def fetch_manifest(manifest_url):
    try:
        response = session.get(manifest_url, headers=DEFAULT_HEADERS, timeout=30)
        response.raise_for_status()
        return response.text
    except requests.RequestException as exc:
        raise ConnectionError(f"Failed to fetch NPO manifest: {exc}") from exc


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


def parse_manifest_attributes(line):
    _, _, payload = line.partition(":")
    attrs = {}
    for match in re.finditer(r'([A-Z0-9-]+)=("[^"]*"|[^,]*)', payload, re.I):
        value = match.group(2).strip()
        if value.startswith('"') and value.endswith('"'):
            value = value[1:-1]
        attrs[match.group(1).lower()] = html.unescape(value)
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


def parse_manifest_streams(manifest_text):
    if manifest_text.lstrip().startswith("#EXTM3U"):
        return parse_hls_streams(manifest_text), "HLS"
    return parse_dash_streams(manifest_text), "DASH"


def subtitle_info_streams(subtitles):
    streams = []
    seen = set()
    for subtitle in subtitles or []:
        if not isinstance(subtitle, dict):
            continue
        location = clean_text(subtitle.get("location"))
        lang = clean_text(subtitle.get("iso")) or clean_text(subtitle.get("language")) or "-"
        name = clean_text(subtitle.get("name"))
        key = (location, lang, name)
        if key in seen:
            continue
        seen.add(key)
        codec = urlparse(location).path.rsplit(".", 1)[-1].lower() if "." in urlparse(location).path else "-"
        streams.append({
            "type": "Sub",
            "resolution": "-",
            "bitrate": "-",
            "codec": codec,
            "lang": lang,
            "channels": "-",
        })
    return streams


def highest_stream_resolution(streams, default="Unknown"):
    heights = []
    for stream in streams or []:
        if stream.get("type") != "Vid":
            continue
        match = re.search(r"x(\d+)", stream.get("resolution") or "")
        if match:
            heights.append(int(match.group(1)))
    return f"{max(heights)}p" if heights else default


def strip_vtt_tags(text):
    text = re.sub(r"<\d{2}:\d{2}:\d{2}\.\d{3}>", "", text)
    text = re.sub(r"</?[^>]+>", "", text)
    return clean_text(text)


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

        time_line = lines[time_index]
        start, _, end = time_line.partition("-->")
        start = start.strip()
        end = end.strip().split(" ", 1)[0]
        text = strip_vtt_tags(" ".join(lines[time_index + 1:]))
        if text:
            cues.append({
                "start": vtt_time_to_srt(start),
                "end": vtt_time_to_srt(end),
                "text": text,
            })

    return cues


def translate_text(text):
    text = clean_text(text)
    if not text:
        return ""

    response = session.get(
        TRANSLATE_URL,
        params={
            "client": "gtx",
            "sl": "nl",
            "tl": "en",
            "dt": "t",
            "q": text,
        },
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


def subtitle_preference_score(subtitle):
    iso = clean_text(subtitle.get("iso")).lower()
    name = clean_text(subtitle.get("name")).lower()
    location = clean_text(subtitle.get("location")).lower()
    score = 0
    if iso.startswith("en") or "english" in name or "/subtitles/en/" in location:
        score += 200
    if iso == "nl" or "nederlands" in name or "/subtitles/nl/" in location:
        score += 100
    if clean_text(subtitle.get("location")):
        score += 10
    return score


def get_subtitle(playback):
    subtitles = [subtitle for subtitle in playback.subtitles or [] if isinstance(subtitle, dict)]
    subtitles = [subtitle for subtitle in subtitles if clean_text(subtitle.get("location"))]
    if not subtitles:
        return None
    subtitles.sort(key=subtitle_preference_score, reverse=True)
    return subtitles[0]


def save_english_subtitles(playback, filename):
    subtitle = get_subtitle(playback)
    if not subtitle:
        print(f"{bcolors.WARNING}{icons.ICON_WARNING} No NPO subtitle URL found in stream-link response.{bcolors.ENDC}")
        return None

    subtitle_url = clean_text(subtitle.get("location"))
    subtitle_iso = clean_text(subtitle.get("iso")).lower()
    response = session.get(subtitle_url, headers=DEFAULT_HEADERS, timeout=30)
    response.raise_for_status()
    vtt_text = response.content.decode("utf-8", "replace")
    cues = parse_vtt(vtt_text)
    if not cues:
        print(f"{bcolors.WARNING}{icons.ICON_WARNING} No subtitle cues found in NPO VTT response.{bcolors.ENDC}")
        return None

    output_path = SAVE_PATH / f"{filename}.en.srt"
    if subtitle_iso.startswith("en"):
        print(f"{bcolors.LIGHTBLUE}{icons.ICON_WAITING} Saving English subtitles as SRT...{bcolors.ENDC}")
        write_srt(cues, output_path)
    else:
        print(f"{bcolors.LIGHTBLUE}{icons.ICON_WAITING} Translating Dutch subtitles to English SRT...{bcolors.ENDC}")
        write_srt(translate_cues(cues), output_path)

    print(f"{bcolors.OKGREEN}{icons.ICON_SUCCESS} English subtitles saved: {bcolors.ENDC}{output_path}")
    return output_path


def maybe_save_english_subtitles(playback, filename):
    try:
        user_input = input("Do you wish to save English subtitles? Y or N: ").strip().lower()
    except EOFError:
        user_input = "n"

    if user_input != "y":
        return None

    return save_english_subtitles(playback, filename)


def safe_name(value):
    value = clean_text(value).replace("'", "")
    value = re.sub(r"[\\/:*?\"<>|]", " ", value)
    value = re.sub(r"\s+", ".", value)
    return value.strip(".") or "Unknown"


def titlecase_slug(value):
    return "-".join(part.capitalize() for part in clean_text(value).split("-") if part)


def show_filename_name(metadata: Metadata):
    title = clean_text(metadata.series_title or metadata.title)
    slug = clean_text(metadata.series_slug)
    if slug and re.sub(r"[^a-z0-9]", "", slug.lower()) == re.sub(r"[^a-z0-9]", "", title.lower()):
        return titlecase_slug(slug)
    return safe_name(title)


def format_filename(metadata: Metadata, resolution: str) -> str:
    """Format filename for download."""
    title = show_filename_name(metadata)
    parts = [title]
    
    if metadata.season is not None and metadata.episode is not None:
        season_episode = f"S{int(metadata.season):02}E{int(metadata.episode):02}"
        parts.append(season_episode)
    
    parts.extend([resolution, "NPO", "WEB-DL", "AAC2.0", "H.264"])
    return ".".join(part for part in parts if part and part != "Unknown")


def episode_series_number(item):
    return int(item.get("season") or 1)


def episode_number(item):
    return int(item.get("episode") or 1)


def episode_tree_label(item):
    return str(episode_number(item)), clean_text(item.get("title")) or f"Episode {episode_number(item)}"


def group_episode_items_by_series(episode_items):
    grouped = {}
    for item in episode_items:
        label = f"Series {episode_series_number(item)}"
        grouped.setdefault(label, []).append(item)
    return grouped


def series_group_sort_key(label):
    match = re.search(r"\d+", label)
    return int(match.group(0)) if match else 9999


def print_series_rule(prefix, title, width=120):
    label = f" {prefix}: {title} "
    if len(label) >= width:
        print(f"{bcolors.GRAY}{label}{bcolors.ENDC}")
        return
    left = (width - len(label)) // 2
    right = width - len(label) - left
    print(f"{bcolors.GRAY}{'─' * left}{label}{'─' * right}{bcolors.ENDC}")


def list_episode_items(episode_items):
    if not episode_items:
        print(f"{icons.ICON_FAILURE} {bcolors.FAIL}No NPO episodes found.{bcolors.ENDC}")
        return
    show = episode_items[0].get("show_title", "NPO")
    grouped_items = group_episode_items_by_series(episode_items)
    group_labels = sorted(grouped_items, key=series_group_sort_key)
    series_summary = ",  ".join(f"{label}({len(grouped_items[label])})" for label in group_labels)
    print(f"{icons.ICON_SUCCESS} {bcolors.OKGREEN}Found {len(episode_items)} NPO episodes{bcolors.ENDC}")
    print()
    print_series_rule("NPO Series", show)
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
            print(f"{bcolors.GRAY}{group_child_prefix}{branch} {episode_number_label}. {title}{bcolors.ENDC}")
            print(f"{bcolors.GRAY}{group_child_prefix}{url_branch} {bcolors.OKBLUE}{item['url']}{bcolors.ENDC}")


def parse_selector_part(value):
    match = re.fullmatch(r"s(\d{1,4})(?:e(\d{1,4}))?", clean_text(value).lower())
    if not match:
        raise ValueError(f"Invalid selector '{value}'. Use s01e01, s01, or a range like s01e01-s01e03.")
    return {
        "season": int(match.group(1)),
        "episode": int(match.group(2)) if match.group(2) is not None else None,
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
    if part["episode"] is None:
        return f"s{part['season']:02d}"
    return f"s{part['season']:02d}e{part['episode']:02d}"


def format_download_selector(parsed):
    if parsed["type"] in ("single_episode", "single_season"):
        return format_selector_part(parsed["start"])
    return f"{format_selector_part(parsed['start'])}-{format_selector_part(parsed['end'])}"


def format_queue_selector(item):
    return f"S{episode_series_number(item):02d}E{episode_number(item):02d}"


def select_episode_items(series_url, selector):
    parsed = parse_download_selector(selector)
    episode_items = collect_episode_items(series_url)
    selected = []
    for item in episode_items:
        season = episode_series_number(item)
        episode = episode_number(item)
        if parsed["type"] == "single_episode":
            keep = season == parsed["start"]["season"] and episode == parsed["start"]["episode"]
        elif parsed["type"] == "single_season":
            keep = season == parsed["start"]["season"]
        elif parsed["type"] == "episode_range":
            keep = (parsed["start"]["season"], parsed["start"]["episode"]) <= (season, episode) <= (
                parsed["end"]["season"],
                parsed["end"]["episode"],
            )
        else:
            keep = parsed["start"]["season"] <= season <= parsed["end"]["season"]
        if keep:
            selected.append(item)

    if not selected:
        raise ValueError(f"No NPO episodes matched selector {format_download_selector(parsed)}.")
    return selected


def print_download_queue(episode_items):
    print()
    print(f"{bcolors.GRAY}Download queue:{bcolors.ENDC}")
    for item in episode_items:
        print(f"{bcolors.GRAY}{format_queue_selector(item)} {item.get('title') or ''}{bcolors.ENDC}".rstrip())


def build_download_command(playback: PlaybackInfo, filename: str, keys=None, interactive=False) -> str:
    """Build download command for N_m3u8DL-RE."""
    selectors = "" if interactive else "--select-video best --select-audio best --select-subtitle all "
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


def print_playback_details(playback: PlaybackInfo, keys, command):
    """Print playback details to console."""
    manifest_label = "HLS URL" if playback.manifest_type == "hls" else "MPD URL"
    print(f"{bcolors.LIGHTBLUE}{manifest_label}: {bcolors.ENDC}{playback.manifest_url}")

    if playback.license_url:
        print(f"{bcolors.RED}License URL: {bcolors.ENDC}{playback.license_url}")
    if playback.pssh:
        print(f"{bcolors.LIGHTBLUE}PSSH: {bcolors.ENDC}{playback.pssh}")
    if keys:
        print(f"{bcolors.OKGREEN}KEYS: {bcolors.ENDC}" + " ".join(f"--key {key}" for key in keys))
    else:
        print(f"{icons.ICON_WARNING} {bcolors.WARNING}No keys available - content may be encrypted{bcolors.ENDC}")

    print(f"{bcolors.YELLOW}DOWNLOAD COMMAND: {bcolors.ENDC}")
    print(mask_proxy_command(command))


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


def resolve_video(video_url, interactive=False):
    url_info = extract_video_info(video_url)
    next_data = fetch_next_data(url_info["slug"], url_info["type"])
    metadata = get_metadata_from_next_data(next_data, url_info)
    playback = get_playback_info(video_url, metadata)

    keys = []
    if playback.is_encrypted and playback.manifest_type == "dash":
        playback.pssh = playback.pssh or get_pssh_from_manifest(playback.manifest_url)
        if playback.license_url and playback.pssh and playback.drm_token:
            keys = get_keys(playback.pssh, playback.license_url, metadata, playback.drm_token)
        else:
            raise RuntimeError("Missing PSSH, license URL, or DRM token; cannot get decryption keys.")
    elif playback.is_encrypted and playback.manifest_type != "hls":
        raise ValueError(f"Unsupported manifest type: {playback.manifest_type}")

    manifest_text = fetch_manifest(playback.manifest_url)
    streams, detected_manifest_type = parse_manifest_streams(manifest_text)
    if detected_manifest_type == "HLS":
        playback.manifest_type = "hls"
    elif detected_manifest_type == "DASH":
        playback.manifest_type = "dash"

    existing_subtitle_keys = {
        (stream.get("codec"), stream.get("lang"))
        for stream in streams
        if stream.get("type") == "Sub"
    }
    for subtitle_stream in subtitle_info_streams(playback.subtitles):
        key = (subtitle_stream.get("codec"), subtitle_stream.get("lang"))
        if key not in existing_subtitle_keys:
            streams.append(subtitle_stream)
            existing_subtitle_keys.add(key)
    playback.streams = sorted(streams, key=stream_table_sort_key)

    resolution = highest_stream_resolution(playback.streams)
    if resolution == "Unknown" and playback.manifest_type == "dash":
        resolution = get_dash_resolution(playback.manifest_url)
    filename = format_filename(metadata, resolution)
    command = build_download_command(playback, filename, keys, interactive=interactive)
    return playback, keys, resolution, filename, command


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
    print(f"{bcolors.LIGHTBLUE}Show: {bcolors.ENDC}{metadata.series_title or metadata.title or 'Unknown'}")
    print(f"{bcolors.LIGHTBLUE}Title: {bcolors.ENDC}{metadata.episode_title or metadata.title or 'Unknown'}")
    print(f"{bcolors.LIGHTBLUE}Date Aired: {bcolors.ENDC}{metadata.aired_date or 'Unknown'}")
    print(f"{bcolors.LIGHTBLUE}Description: {bcolors.ENDC}{metadata.description or 'No Description'}")


def print_info_mode(video_url):
    if not is_episode_url(video_url):
        raise ValueError("Info mode requires an NPO episode/video URL.")

    playback, keys, _resolution, filename, _command = run_with_spinner(lambda: resolve_video(video_url))
    manifest_label = "DASH Manifest URL" if playback.manifest_type == "dash" else "HLS Manifest URL"
    print(f"{bcolors.LIGHTBLUE}{manifest_label}: {bcolors.ENDC}{playback.manifest_url}")
    if keys:
        print(f"{bcolors.OKGREEN}KEYS: {bcolors.ENDC}" + " ".join(f"--key {key}" for key in keys))
    print_streams(playback.streams)
    print_episode_metadata(playback.metadata)
    print(f"\n{bcolors.YELLOW}Suggested filename: {bcolors.ENDC}{filename}.mkv")


def maybe_download(command, auto_download=False):
    """Ask user if they want to download."""
    if auto_download:
        user_input = "y"
    else:
        user_input = input("Do you wish to download? Y or N: ").strip().lower()
    if user_input == "y":
        print(f"{icons.ICON_WAITING} {bcolors.OKBLUE}Downloading video...{bcolors.ENDC}")
        subprocess.run(command, shell=True)
    else:
        print(f"{icons.ICON_FAILURE} {bcolors.RED}Download cancelled{bcolors.ENDC}")


def process_video(video_url: str, auto_download=False, interactive=False):
    """Main processing function for NPO videos."""
    print(f"{icons.ICON_INFO} {bcolors.LIGHTBLUE}Processing: {bcolors.ENDC}{video_url}")

    playback, keys, _resolution, filename, command = run_with_spinner(lambda: resolve_video(video_url, interactive=interactive))
    metadata = playback.metadata
    
    if metadata.title != "Unknown":
        episode_str = f"S{metadata.season:02d}E{metadata.episode:02d}" if metadata.season and metadata.episode else ""
        episode_title = f" - {metadata.episode_title}" if metadata.episode_title else ""
        print(f"{icons.ICON_SUCCESS}{bcolors.OKGREEN} Episode Title:{bcolors.ENDC} {metadata.title} {episode_str}{episode_title}".strip())

    print_playback_details(playback, keys, command)
    if auto_download:
        save_english_subtitles(playback, filename)
    else:
        maybe_save_english_subtitles(playback, filename)
    maybe_download(command, auto_download=auto_download)


def download_selected_episodes(series_url, selector):
    episode_items = select_episode_items(series_url, selector)
    print_download_queue(episode_items)
    episode_word = "episode" if len(episode_items) == 1 else "episodes"
    this_or_these = "this" if len(episode_items) == 1 else "these"
    user_input = input(f"Do you wish to download {this_or_these} {len(episode_items)} {episode_word}? Y or N: ").strip().lower()
    if user_input != "y":
        print(f"{icons.ICON_FAILURE} {bcolors.RED}Download cancelled{bcolors.ENDC}")
        return

    for index, item in enumerate(episode_items, 1):
        print()
        print(f"{icons.ICON_WAITING} {bcolors.LIGHTBLUE}Downloading {index}/{len(episode_items)}: {bcolors.ENDC}{item['url']}")
        process_video(item["url"], auto_download=True)


def export_episode_urls(episode_items):
    if not episode_items:
        print(f"{icons.ICON_FAILURE} {bcolors.FAIL}No NPO episodes found to export.{bcolors.ENDC}")
        return

    export_dir = Path(__file__).resolve().parents[2] / "export"
    export_dir.mkdir(parents=True, exist_ok=True)
    show_title = episode_items[0].get("show_title", "NPO")
    output_path = export_dir / f"npo_{safe_name(show_title)}_episodes.txt"
    output_path.write_text("\n".join(item["url"] for item in episode_items) + "\n", encoding="utf-8")
    print(f"{icons.ICON_SUCCESS} {bcolors.OKGREEN}Exported list: {output_path}{bcolors.ENDC}")


def main(video_url, downloads_path, wvd_device_path, mode="auto", export_list=False, download_selector=None):
    """Eurovine entry point for NPO (Widevine)."""
    try:
        if not video_url:
            raise ValueError("No NPO URL provided.")
        if not downloads_path or not wvd_device_path:
            raise ValueError("Eurovine config requires downloads_path and wvd_device_path for NPO.")

        configure_service(downloads_path, wvd_device_path)
        video_url = video_url.strip()

        if mode == "list":
            if is_episode_url(video_url):
                print(f"{icons.ICON_FAILURE} {bcolors.FAIL}List mode requires an NPO series URL, not an episode URL.{bcolors.ENDC}")
                return
            print(f"{icons.ICON_WAITING} {bcolors.LIGHTBLUE}Retrieving series information.....{bcolors.ENDC}")
            episode_items = collect_episode_items(video_url)
            list_episode_items(episode_items)
            if export_list:
                export_episode_urls(episode_items)
            return

        if mode == "download":
            if is_episode_url(video_url):
                print(f"{icons.ICON_FAILURE} {bcolors.FAIL}Download selector mode requires an NPO series URL, not an episode URL.{bcolors.ENDC}")
                return
            print(f"{icons.ICON_WAITING} {bcolors.LIGHTBLUE}Retrieving series information.....{bcolors.ENDC}")
            download_selected_episodes(video_url, download_selector)
            return

        if mode == "info":
            print_info_mode(video_url)
            return

        if export_list:
            if is_episode_url(video_url):
                print(f"{icons.ICON_FAILURE} {bcolors.FAIL}Export mode requires an NPO series URL, not an episode URL.{bcolors.ENDC}")
                return
            print(f"{icons.ICON_WAITING} {bcolors.LIGHTBLUE}Retrieving series information.....{bcolors.ENDC}")
            export_episode_urls(collect_episode_items(video_url))
            return

        if is_series_url(video_url):
            print(f"{icons.ICON_WARNING} {bcolors.WARNING}Series URLs require a flag. Use --list/-l to list episodes, --export/-x to export episode URLs, or --download/-d SELECTOR to download selected episodes.{bcolors.ENDC}")
            return

        process_video(video_url, interactive=(mode == "interactive"))
    except Exception as exc:
        print(f"{icons.ICON_FAILURE} {bcolors.FAIL}Error: {exc}{bcolors.ENDC}")


if __name__ == "__main__":
    print("Run NPO through eurovine.py so it can use the shared Eurovine configuration.")
