import base64
import binascii
import html
import contextlib
import io
import json
import re
import shutil
import subprocess
import sys
import urllib.parse
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
from urllib.request import Request, urlopen

import requests
import icons
import urllib3
import yaml
from beaupy.spinners import Spinner
from colors import bcolors
from pywidevine.cdm import Cdm
from pywidevine.device import Device
from pywidevine.pssh import PSSH
from services.proxy import current_proxy_url, mask_proxy_command

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

SERVICE_NAME = "tf1"
SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = Path(__file__).resolve().parents[2] / "config.yaml"

# TF1 Endpoints
TF1_BASE = "https://www.tf1.fr"
TF1_LOGIN = "https://compte.tf1.fr/accounts.login"
TF1_TOKEN = "https://www.tf1.fr/token/gigya/web"
TF1_MEDIA = "https://mediainfo.tf1.fr/mediainfocombo/{media_id}"
TF1_PLAYER = "https://prod-player.tf1.fr"
TF1_LICENSE = "https://drm-wide.tf1.fr/proxy"
TRANSLATE_URL = "https://translate.googleapis.com/translate_a/single"
TRANSLATE_BATCH_MARKER = "@@TF1BREAK@@"
TRANSLATE_BATCH_SIZE = 40
TRANSLATE_BATCH_CHAR_LIMIT = 4500

def read_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as file:
        return yaml.safe_load(file) or {}

def write_config(data):
    with open(CONFIG_PATH, "w", encoding="utf-8") as file:
        yaml.safe_dump(data, file, sort_keys=False, allow_unicode=True)

config = {}
SAVE_PATH = None
WVD_PATH = ""
N_M3U8DL = "N_m3u8DL-RE"

def load_tf1_cache():
    return dict(((config.get("tf1") or {}).get("cache") or {}))

def save_tf1_cache(cache_data):
    global config
    config = read_config()
    tf1_config = config.get("tf1")
    if not isinstance(tf1_config, dict):
        tf1_config = {}
        config["tf1"] = tf1_config
    tf1_config["cache"] = cache_data or {}
    write_config(config)

def jwt_expiry(token):
    try:
        parts = str(token or "").split(".")
        if len(parts) < 2:
            return None
        payload = parts[1]
        payload += "=" * (-len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")).decode("utf-8"))
        expiry = data.get("exp")
        return int(expiry) if expiry is not None else None
    except Exception:
        return None

def cached_bearer_token():
    cache = load_tf1_cache()
    token = clean_text(cache.get("bearer_token") or cache.get("token"))
    expiry = cache.get("expiry")
    try:
        expiry = int(expiry)
    except (TypeError, ValueError):
        expiry = jwt_expiry(token)
    if token and expiry and expiry > int(datetime.now().timestamp()) + 120:
        print(f"{icons.ICON_SUCCESS} {bcolors.OKGREEN}Using cached TF1 bearer token{bcolors.ENDC}")
        return token
    return None

session = requests.Session()
SERVICE_PROXY = None

def configure_service(downloads_path, wvd_device_path, tf1_credentials=None, tf1_config=None):
    global config, SAVE_PATH, WVD_PATH, SERVICE_PROXY
    SAVE_PATH = Path(downloads_path)
    WVD_PATH = wvd_device_path
    try:
        config = read_config()
    except Exception:
        config = {}
    if tf1_config:
        config["tf1"] = dict(tf1_config)
    if tf1_credentials:
        config.setdefault("credentials", {})["tf1"] = tf1_credentials
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

def fetch_page_text(url, attempts=3):
    last_error = None
    for attempt in range(1, attempts + 1):
        try:
            response = session.get(url, headers=DEFAULT_HEADERS, timeout=30)
            response.raise_for_status()
            return response.text
        except requests.RequestException as exc:
            last_error = exc
            if attempt < attempts:
                continue

    if SERVICE_PROXY:
        raise last_error

    request = Request(url, headers=DEFAULT_HEADERS)
    with urlopen(request, timeout=35) as response:
        return response.read().decode("utf-8", "replace")

@dataclass
class Metadata:
    title: str = "Unknown"
    season: Optional[int] = None
    episode: Optional[int] = None
    episode_title: Optional[str] = None
    aired_date: str = "Unknown"
    description: str = "No Description"
    video_id: Optional[str] = None
    channel: Optional[str] = None
    content_title: Optional[str] = None

@dataclass
class PlaybackInfo:
    manifest_url: str
    manifest_type: str
    license_url: Optional[str] = None
    pssh: Optional[str] = None
    subtitles: list = field(default_factory=list)
    streams: list = field(default_factory=list)
    metadata: Metadata = field(default_factory=Metadata)

def clean_text(value):
    return re.sub(r"\s+", " ", html.unescape(str(value or ""))).strip()

def date_value(value):
    value = clean_text(value)
    if not value:
        return "Unknown"
    normalised = value.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(normalised).strftime("%Y-%m-%d")
    except ValueError:
        return value.split("T", 1)[0] or "Unknown"

def canonical_url(value):
    value = clean_text(value)
    if re.match(r"^https?://", value, re.IGNORECASE):
        return value
    if value.startswith("/"):
        return urllib.parse.urljoin(TF1_BASE, value)
    return urllib.parse.urljoin(TF1_BASE, f"/tf1/{value.strip('/')}")

def is_episode_url(video_url):
    return "/videos/" in urllib.parse.urlparse(canonical_url(video_url)).path

def walk(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk(child)

def flatten_strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for child in value.values():
            yield from flatten_strings(child)
    elif isinstance(value, list):
        for child in value:
            yield from flatten_strings(child)

def collect_flight_text(html_text):
    chunks = []
    decoder = json.JSONDecoder()
    for match in re.finditer(r"self\.__next_f\.push\(", html_text):
        start = match.end()
        try:
            payload, end = decoder.raw_decode(html_text[start:])
        except json.JSONDecodeError:
            continue
        if html_text[start + end:start + end + 1] != ")":
            continue
        chunks.extend(flatten_strings(payload))
    return "\n".join(chunks)

def normalise_card_video(item):
    video = item.get("video") if isinstance(item.get("video"), dict) else None
    if not video:
        return item

    merged = dict(video)
    for key, value in item.items():
        if key == "video":
            continue
        if key not in merged or merged.get(key) in (None, "", [], {}):
            merged[key] = value
    return merged

def extract_wrapping_json_objects(text, marker):
    decoder = json.JSONDecoder()
    objects = []
    seen = set()

    for match in re.finditer(re.escape(marker), text):
        window_start = max(0, match.start() - 20000)
        brace_positions = [pos for pos in range(window_start, match.start()) if text[pos] == "{"]
        candidates = []
        for start in reversed(brace_positions):
            try:
                item, end = decoder.raw_decode(text[start:])
            except json.JSONDecodeError:
                continue
            if not isinstance(item, dict) or start + end < match.end():
                continue
            video = item.get("video") if isinstance(item.get("video"), dict) else {}
            if item.get("__typename") == "Video" or video.get("__typename") == "Video":
                candidates.append(item)

        if not candidates:
            continue

        item = next(
            (
                candidate for candidate in candidates
                if isinstance(candidate.get("video"), dict)
                and (candidate.get("image") or candidate.get("description"))
            ),
            candidates[0],
        )
        item = normalise_card_video(item)
        item_id = clean_text(item.get("id") or item.get("slug"))
        key = item_id or json.dumps(item, sort_keys=True)
        if key not in seen:
            seen.add(key)
            objects.append(item)

    return objects

def collect_flight_videos(html_text):
    flight_text = collect_flight_text(html_text)
    videos = extract_wrapping_json_objects(flight_text, '"__typename":"Video"')
    if videos:
        return videos
    return extract_wrapping_json_objects(html_text.replace(r"\"", '"'), '"__typename":"Video"')

def collect_json_ld_videos(html_text, source_url):
    videos = []
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
            node_type = node.get("@type") if isinstance(node, dict) else None
            if node_type == "VideoObject" or (isinstance(node_type, list) and "VideoObject" in node_type):
                item = dict(node)
                item["_source_url"] = source_url
                videos.append(item)
    return videos

def list_video_url(item):
    source_url = clean_text(item.get("_source_url") or item.get("url") or item.get("contentUrl"))
    if source_url:
        return canonical_url(source_url)

    slug = clean_text(item.get("slug"))
    program = item.get("program") or {}
    program_slug = clean_text(program.get("slug"))
    if slug and program_slug:
        return f"{TF1_BASE}/tf1/{program_slug}/videos/{slug}.html"
    if slug:
        return f"{TF1_BASE}/videos/{slug}.html"
    return ""

def list_video_id(item):
    item_id = clean_text(item.get("id"))
    if item_id:
        return item_id
    embed_url = clean_text(item.get("embedUrl"))
    match = re.search(r"/player/([^/?#]+)", embed_url)
    if match:
        return match.group(1)
    slug = clean_text(item.get("slug") or item.get("_source_url"))
    match = re.search(r"-(\d+)(?:\.html)?$", slug)
    return match.group(1) if match else ""

def merge_episode_metadata(base_item, full_item):
    merged = dict(base_item)
    for key, value in (full_item or {}).items():
        if value in (None, "", [], {}):
            continue
        if key in ("description", "thumbnailUrl", "duration", "uploadDate", "datePublished"):
            if clean_text(merged.get(key)) and key != "description":
                continue
            if key == "description" and len(clean_text(merged.get(key))) >= len(clean_text(value)):
                continue
        if key not in merged or merged.get(key) in (None, "", [], {}) or key in ("description", "thumbnailUrl"):
            merged[key] = value
    return merged

def list_description_text(item):
    for key in ("description", "synopsis", "summary"):
        value = item.get(key)
        if isinstance(value, dict):
            value = value.get("long") or value.get("short") or value.get("text")
        value = clean_text(value)
        if value:
            return value
    return "No Description"

def hydrate_series_episode(item):
    if list_description_text(item) != "No Description":
        return item

    url = list_video_url(item)
    if not url:
        return item

    try:
        response = session.get(url, headers=DEFAULT_HEADERS, timeout=30)
        response.raise_for_status()
        json_ld_items = collect_json_ld_videos(response.text, url)
    except Exception as exc:
        print(f"{icons.ICON_WARNING} {bcolors.WARNING}Could not hydrate TF1 episode page {url}: {exc}{bcolors.ENDC}")
        return item

    full_item = next(
        (
            candidate for candidate in json_ld_items
            if list_video_id(item) == list_video_id(candidate)
            or clean_text(item.get("slug")).rsplit("-", 1)[-1] in clean_text(candidate.get("_source_url"))
        ),
        json_ld_items[0] if json_ld_items else None,
    )
    return merge_episode_metadata(item, full_item) if full_item else item

def collect_series_info(html_text, videos):
    flight_text = collect_flight_text(html_text)
    for node in walk({"videos": videos}):
        program = node.get("program") if isinstance(node, dict) else None
        if isinstance(program, dict) and clean_text(program.get("name")):
            return program

    for match in re.finditer(r'"seoData":\{.*?"tags":\{"title":"(.*?)".*?"h1":"(.*?)"', flight_text):
        title = clean_text(match.group(2) or match.group(1))
        if title:
            return {"name": title}

    title_match = re.search(r"<title>(.*?)</title>", html_text, re.I | re.S)
    if title_match:
        return {"name": clean_text(title_match.group(1)).split("|", 1)[0].strip()}
    return {}

def dedupe_videos(items):
    deduped = []
    seen = {}
    for item in items:
        embed_url = clean_text(item.get("embedUrl"))
        embed_match = re.search(r"/player/([^/?#]+)", embed_url)
        item_id = clean_text(item.get("id") or (embed_match.group(1) if embed_match else ""))
        if not item_id:
            item_id = clean_text(item.get("slug") or item.get("url") or item.get("contentUrl"))
        if not item_id:
            item_id = json.dumps(item, sort_keys=True)
        if item_id in seen:
            index = seen[item_id]
            deduped[index] = merge_episode_metadata(deduped[index], item)
            continue
        seen[item_id] = len(deduped)
        deduped.append(item)
    return deduped

def is_episode_item(item):
    if item.get("@type") == "VideoObject":
        return True
    slug = clean_text(item.get("slug") or item.get("_source_url"))
    return bool(re.search(r"s\d+\s*[-_ ]?\s*e\d+", slug, re.I))

def list_show_title(item, series_info=None):
    program = item.get("program") or {}
    series = item.get("partOfSeries") if isinstance(item.get("partOfSeries"), dict) else {}
    return (
        clean_text(program.get("name"))
        or clean_text((series_info or {}).get("name"))
        or clean_text(series.get("name"))
        or clean_text(item.get("name")).split(" - ", 1)[0]
        or "TF1"
    )

def list_season_number(item):
    value = item.get("season")
    if value not in (None, ""):
        return int(value)
    for key in ("slug", "name", "headline"):
        match = re.search(r"s(?:aison)?\s*0*(\d+)", clean_text(item.get(key)), re.I)
        if match:
            return int(match.group(1))
    match = re.search(r"s(\d+)[\s-]*e\d+", list_video_url(item), re.I)
    return int(match.group(1)) if match else 1

def list_episode_number(item):
    value = item.get("episode")
    if value not in (None, ""):
        return int(value)
    for key in ("slug", "name", "headline"):
        match = re.search(r"e(?:pisode)?\s*0*(\d+)", clean_text(item.get(key)), re.I)
        if match:
            return int(match.group(1))
    match = re.search(r"s\d+[\s-]*e(\d+)", list_video_url(item), re.I)
    return int(match.group(1)) if match else 1

def list_episode_title(item):
    title = clean_text(item.get("title") or item.get("name") or item.get("headline"))
    season = str(list_season_number(item)).zfill(2)
    episode = str(list_episode_number(item)).zfill(2)
    if not title:
        return f"S{season} E{episode}"
    return title

def build_series_episode_item(item, series_info=None):
    return {
        "id": list_video_id(item),
        "show_title": list_show_title(item, series_info=series_info),
        "season": list_season_number(item),
        "episode": list_episode_number(item),
        "title": list_episode_title(item),
        "url": list_video_url(item),
    }

def collect_episode_items(series_url):
    source_url = canonical_url(series_url)
    if is_episode_url(source_url):
        raise ValueError("List/download mode requires a TF1 series URL, not an episode URL.")

    html_text = fetch_page_text(source_url)
    videos = dedupe_videos(collect_flight_videos(html_text))
    videos = [item for item in videos if is_episode_item(item)]
    if not videos:
        raise RuntimeError("No TF1 episodes found for this URL.")

    hydrated = [hydrate_series_episode(item) for item in videos]
    series_info = collect_series_info(html_text, hydrated)
    episode_items = []
    seen = set()
    for item in sorted(hydrated, key=lambda item: (list_season_number(item), list_episode_number(item), clean_text(item.get("slug") or item.get("name")).lower())):
        episode_item = build_series_episode_item(item, series_info=series_info)
        key = episode_item["id"] or episode_item["url"]
        if not key or key in seen or not episode_item["url"]:
            continue
        seen.add(key)
        episode_items.append(episode_item)
    return episode_items

def parse_season_episode(value):
    match = re.search(r"S\s*(\d+)\s*[Ee]\s*(\d+)", clean_text(value), re.IGNORECASE)
    if not match:
        return None, None
    return int(match.group(1)), int(match.group(2))

def extract_video_id(video_url):
    # TF1's mediainfo endpoint expects the player UUID, not the numeric slug id.
    match = re.search(r"/player/([^/?#]+)", video_url)
    if match:
        return match.group(1)

    try:
        response = session.get(video_url, headers=DEFAULT_HEADERS, timeout=30)
        response.raise_for_status()
        content = response.text
        patterns = [
            r'"embedUrl"\s*:\s*"https://www\.tf1\.fr/player/([^"]+)"',
            r'"embedUrl"\s*:\s*"[^"]*/player/([^"]+)"',
            r'https://www\.tf1\.fr/player/([0-9a-fA-F-]{36})',
            r'/player/([0-9a-fA-F-]{36})',
        ]
        for pattern in patterns:
            match = re.search(pattern, content)
            if match:
                return html.unescape(match.group(1))
    except Exception as exc:
        print(f"{icons.ICON_WARNING} {bcolors.WARNING}Could not resolve TF1 player UUID from page: {exc}{bcolors.ENDC}")

    # Keep the numeric slug id as a last resort for older pages/scripts.
    match = re.search(r"/videos/[^/]+-(\d+)\.html", video_url)
    if match:
        return match.group(1)
    raise ValueError("Could not extract video ID from URL.")

def get_api_key_from_page():
    """Get API key from TF1 page"""
    try:
        response = session.get(TF1_BASE, headers=DEFAULT_HEADERS, timeout=30)
        response.raise_for_status()
        content = response.text
        
        # Try multiple patterns to find API key
        patterns = [
            r'apiKey=([^"&\'<>\s]+)',
            r'gig_bootstrap_([a-zA-Z0-9_]+)',
            r'"apiKey"\s*:\s*"([^"]+)"',
            r'apiKey\s*=\s*"([^"]+)"',
            r'eu1.gigya.com",key:"([^"]+)"',
            r'API_KEY\s*=\s*"([^"]+)"'
        ]
        
        for pattern in patterns:
            match = re.search(pattern, content)
            if match:
                api_key = html.unescape(match.group(1))
                print(f"{icons.ICON_INFO} {bcolors.LIGHTBLUE}Found API Key: {bcolors.ENDC}{api_key[:20]}...")
                return api_key
        
        # Try to find in script tags
        script_pattern = r'<script[^>]*>.*?(?:gig_bootstrap_|apiKey)\s*[:=]\s*"([^"]+)".*?</script>'
        match = re.search(script_pattern, content, re.DOTALL)
        if match:
            api_key = html.unescape(match.group(1))
            print(f"{icons.ICON_INFO} {bcolors.LIGHTBLUE}Found API Key in script: {bcolors.ENDC}{api_key[:20]}...")
            return api_key
        
        raise Exception("Could not find API key")
    except Exception as e:
        print(f"{icons.ICON_WARNING} {bcolors.WARNING}Error getting API key: {e}{bcolors.ENDC}")
        raise

def get_consent_ids():
    """Get consent IDs from TF1 page"""
    try:
        response = session.get(TF1_BASE, headers=DEFAULT_HEADERS, timeout=30)
        response.raise_for_status()
        content = response.text
        
        # Try to find consent IDs
        patterns = [
            r'neededConsentIds"\s*:\s*\[(.*?)\]',
            r'neededConsentIds\s*=\s*\[(.*?)\]'
        ]
        
        for pattern in patterns:
            match = re.search(pattern, content)
            if match:
                consent_raw = match.group(1)
                consent_ids = [c.strip().strip('"') for c in consent_raw.split(',') if c.strip()]
                if consent_ids:
                    print(f"{icons.ICON_INFO} {bcolors.LIGHTBLUE}Found {len(consent_ids)} consent IDs{bcolors.ENDC}")
                    return consent_ids

    except Exception as e:
        print(f"{icons.ICON_WARNING} {bcolors.WARNING}Error getting consent IDs: {e}{bcolors.ENDC}")

    return ["4", "10001", "10003", "10005", "10007", "10009", "10011", "10013", "10015", "10017", "10019"]

def get_player_version():
    """Get player version from TF1 page"""
    try:
        response = session.get(TF1_BASE, headers=DEFAULT_HEADERS, timeout=30)
        response.raise_for_status()
        content = response.text
        
        # Try to find player version
        patterns = [
            r'"playerEndpoint"\s*:\s*"[^"]*",\s*"version"\s*:\s*"([^"]+)"',
            r'playerEndpoint.*?version.*?"([^"]+)"',
            r'playerVersion\s*=\s*"([^"]+)"',
            r'main-(.*?)\.bundle\.js'
        ]
        
        for pattern in patterns:
            match = re.search(pattern, content, re.DOTALL)
            if match:
                player_version = match.group(1)
                # Format version
                try:
                    major, minor, patch = map(int, player_version.split('.'))
                    player_version_formatted = str(major * 1000000 + minor * 1000 + patch)
                    print(f"{icons.ICON_INFO} {bcolors.LIGHTBLUE}Player Version: {bcolors.ENDC}{player_version_formatted}")
                    return player_version_formatted
                except:
                    print(f"{icons.ICON_INFO} {bcolors.LIGHTBLUE}Player Version: {bcolors.ENDC}{player_version}")
                    return "5029001"
        
         # Fallback player version known to work with TF1 mediainfo.
        return "5029001"
    except Exception as e:
        print(f"{icons.ICON_WARNING} {bcolors.WARNING}Error getting player version: {e}{bcolors.ENDC}")
        return "5029001"

def authenticate(username, password):
    """Authenticate with TF1 and get bearer token"""
    try:
        cached_token = cached_bearer_token()
        if cached_token:
            return cached_token

        # Get API key
        api_key = get_api_key_from_page()
        
        # Get consent IDs
        consent_ids = get_consent_ids()
        
        # Login
        login_data = {
            "loginID": username,
            "password": password,
            "apiKey": api_key,
            "format": "json"
        }
        
        print(f"{icons.ICON_WAITING} {bcolors.LIGHTBLUE}Logging in...{bcolors.ENDC}")
        login_response = session.post(
            TF1_LOGIN,
            data=login_data,
            headers={'Content-Type': 'application/x-www-form-urlencoded'},
            timeout=30,
        )
        login_response.raise_for_status()
        login_json = login_response.json()
        
        if "UID" not in login_json:
            print(f"{icons.ICON_FAILURE} {bcolors.FAIL}Login failed: {login_json}{bcolors.ENDC}")
            raise Exception("Login failed - check your credentials")
        
        print(f"{icons.ICON_SUCCESS} {bcolors.OKGREEN}Login successful{bcolors.ENDC}")
        
        # Get token
        token_data = {
            'uid': login_json["UID"],
            'signature': login_json["UIDSignature"],
            'timestamp': int(login_json["signatureTimestamp"]),
            'consent_ids': consent_ids
        }
        
        print(f"{icons.ICON_WAITING} {bcolors.LIGHTBLUE}Getting bearer token...{bcolors.ENDC}")
        token_response = session.post(TF1_TOKEN, json=token_data, timeout=30)
        token_response.raise_for_status()
        token_json = token_response.json()
        
        if "token" not in token_json:
            print(f"{icons.ICON_FAILURE} {bcolors.FAIL}Failed to get token: {token_json}{bcolors.ENDC}")
            raise Exception("Failed to get bearer token")
        
        token = token_json["token"]
        expiry = jwt_expiry(token)
        cache_data = {"bearer_token": token}
        if expiry:
            cache_data["expiry"] = expiry
        save_tf1_cache(cache_data)
        print(f"{icons.ICON_SUCCESS} {bcolors.OKGREEN}Bearer token obtained{bcolors.ENDC}")
        
        return token
        
    except Exception as e:
        raise Exception(f"Authentication failed: {e}")

def search_metadata(video_url, video_id):
    # Extract channel and title from URL
    channel_match = re.search(r"https://www\.tf1\.fr/([^/]+)/", video_url)
    channel = channel_match.group(1) if channel_match else "tf1"
    
    # Get page title for metadata
    try:
        response = session.get(video_url, headers=DEFAULT_HEADERS, timeout=30)
        response.raise_for_status()
        content = response.content.decode()
        page_items = collect_json_ld_videos(content, video_url)
        page_item = page_items[0] if page_items else {}
        description = list_description_text(page_item)
        aired_date = date_value(
            page_item.get("date")
            or page_item.get("published")
            or page_item.get("datePublished")
            or page_item.get("uploadDate")
        )
        
        # Try to get title from page
        title_match = re.search(r'<h1[^>]*>(.*?)</h1>', content)
        if title_match:
            page_title = clean_text(title_match.group(1))
            # Extract season and episode from title if present
            season, episode = parse_season_episode(page_title)
            if season is not None and episode is not None:
                title = clean_text(re.split(r"\s+-\s+S\s*\d+\s*E\s*\d+", page_title, maxsplit=1, flags=re.IGNORECASE)[0])
                episode_title = page_title
            else:
                # Try to find season/episode in URL or content
                season = None
                episode = None
                title = page_title
                episode_title = None
        else:
            title = f"TF1_{video_id}"
            season = None
            episode = None
            episode_title = None
            
        return Metadata(
            title=title,
            season=season,
            episode=episode,
            episode_title=episode_title,
            aired_date=aired_date,
            description=translate_to_english(description),
            video_id=video_id,
            channel=channel,
            content_title=episode_title,
        )
    except Exception as e:
        print(f"{icons.ICON_WARNING} {bcolors.WARNING}Error getting metadata: {e}{bcolors.ENDC}")
        return Metadata(
            title=f"TF1_{video_id}",
            season=None,
            episode=None,
            episode_title=None,
            aired_date="Unknown",
            description="No Description",
            video_id=video_id,
            channel=channel
        )

def update_metadata_from_media(metadata, media_data):
    media = media_data.get("media") or {}
    content = media_data.get("content") or {}

    program_name = clean_text(media.get("programName"))
    media_title = clean_text(media.get("title"))
    content_title = clean_text(content.get("title"))
    short_title = clean_text(media.get("shortTitle"))
    description = list_description_text(content) if isinstance(content, dict) else "No Description"
    if description == "No Description":
        description = list_description_text(media) if isinstance(media, dict) else "No Description"
    aired_date = date_value(
        content.get("date")
        or content.get("published")
        or content.get("datePublished")
        or content.get("uploadDate")
        or media.get("date")
        or media.get("published")
        or media.get("datePublished")
        or media.get("uploadDate")
    )

    season, episode = parse_season_episode(short_title or media_title or content_title)
    if season is not None and episode is not None:
        metadata.season = season
        metadata.episode = episode
        metadata.title = program_name or metadata.title
        metadata.episode_title = short_title or media_title or metadata.episode_title
        metadata.content_title = media_title or metadata.episode_title
    else:
        metadata.title = media_title or program_name or metadata.title
        metadata.episode_title = None
        metadata.content_title = media_title or content_title or metadata.title

    if not metadata.title:
        metadata.title = "Unknown"
    if metadata.aired_date == "Unknown" and aired_date != "Unknown":
        metadata.aired_date = aired_date
    if metadata.description == "No Description" and description != "No Description":
        metadata.description = translate_to_english(description)
    return metadata

def get_playback_info(video_url, metadata):
    try:
        # Get credentials from config
        credentials = config.get('credentials', {})
        tf1_creds = credentials.get('tf1', '')
        if ':' not in tf1_creds:
            raise ValueError("Invalid TF1 credentials format in config. Expected: username:password")
        
        username, password = tf1_creds.split(':', 1)
        
        # Authenticate and get token
        bearer_token = authenticate(username, password)
        
        # Get player version
        player_version = get_player_version()
        
        # Get media info
        media_response = session.get(
            TF1_MEDIA.format(media_id=metadata.video_id),
            params={'pver': player_version, 'context': 'MYTF1'},
            headers={'Authorization': f'Bearer {bearer_token}'},
            timeout=30,
        )
        media_response.raise_for_status()
        media_data = json.loads(media_response.content.decode())
        update_metadata_from_media(metadata, media_data)
        
        # Check for geoblocking
        if "GEOBLOCKED" in str(media_data):
            raise Exception("Content is geoblocked. Please use a French proxy.")
        
        # Get manifest URL
        manifest_url = media_data["delivery"]["url"]
        
        # Determine manifest type
        manifest_type = "mpd" if ".mpd" in manifest_url else "m3u8"
        
        # Get PSSH from manifest if DASH
        pssh = None
        subtitles = []
        streams = []
        manifest_content = ""
        manifest_response = session.get(manifest_url, headers=DEFAULT_HEADERS, timeout=30)
        manifest_response.raise_for_status()
        manifest_content = manifest_response.content.decode("utf-8", "replace")
        if manifest_type == "mpd":
            try:
                pssh_values = re.findall(r'<cenc:pssh>(.+?)</cenc:pssh>', manifest_content)
                if pssh_values:
                    pssh = min(pssh_values, key=len)  # Use shortest PSSH
                subtitles = extract_subtitles_from_mpd(manifest_url, manifest_content)
            except Exception as e:
                print(f"{icons.ICON_WARNING} {bcolors.WARNING}Could not extract PSSH from manifest: {e}{bcolors.ENDC}")
        streams = parse_manifest_streams(manifest_content, manifest_type)
        streams.extend(subtitle_info_streams(subtitles))
        
        return PlaybackInfo(
            manifest_url=manifest_url,
            manifest_type=manifest_type,
            license_url=TF1_LICENSE,
            pssh=pssh,
            subtitles=subtitles,
            streams=streams,
            metadata=metadata
        )
    except Exception as e:
        raise Exception(f"Error getting playback info: {e}")

def extract_subtitles_from_mpd(manifest_url, manifest_content):
    root = ET.fromstring(manifest_content.encode("utf-8") if isinstance(manifest_content, str) else manifest_content)
    ns = "{urn:mpeg:dash:schema:mpd:2011}"
    subtitles = []

    for adaptation in root.findall(".//" + ns + "AdaptationSet"):
        content_type = (adaptation.get("contentType") or "").lower()
        mime_type = (adaptation.get("mimeType") or "").lower()
        lang = (adaptation.get("lang") or "").lower()
        if content_type != "text" and "text/vtt" not in mime_type:
            continue
        if "text/vtt" not in mime_type:
            continue

        adaptation_base = adaptation.find(ns + "BaseURL")
        for representation in adaptation.findall(ns + "Representation"):
            base_url = None
            rep_base = representation.find(ns + "BaseURL")
            if rep_base is not None and rep_base.text:
                base_url = clean_text(rep_base.text)
            elif adaptation_base is not None and adaptation_base.text:
                base_url = clean_text(adaptation_base.text)
            if not base_url:
                continue
            subtitles.append({
                "lang": lang or "fr",
                "mime_type": mime_type,
                "url": urllib.parse.urljoin(manifest_url, base_url),
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
        "Origin": "https://www.tf1.fr",
        "Referer": "https://www.tf1.fr/",
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

def parse_m3u8_attributes(line):
    attrs = {}
    for match in re.finditer(r'([A-Z0-9-]+)=("[^"]*"|[^,]*)', line):
        value = match.group(2)
        attrs[match.group(1)] = value[1:-1] if value.startswith('"') and value.endswith('"') else value
    return attrs

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
    rows = []
    seen = set()
    for subtitle in subtitles or []:
        if not isinstance(subtitle, dict):
            continue
        key = clean_text(subtitle.get("url") or subtitle.get("lang"))
        if not key or key in seen:
            continue
        seen.add(key)
        mime_type = clean_text(subtitle.get("mime_type"))
        rows.append({
            "type": "Sub",
            "resolution": "-",
            "bitrate": "-",
            "codec": "vtt" if "vtt" in mime_type.lower() else mime_type or "-",
            "lang": clean_text(subtitle.get("lang")) or "fr",
            "channels": "-",
        })
    return rows

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
        start, _, end = lines[time_index].partition("-->")
        start = start.strip()
        end = end.strip().split(" ", 1)[0]
        text = strip_vtt_tags(" ".join(lines[time_index + 1:]))
        if text:
            cues.append({"start": vtt_time_to_srt(start), "end": vtt_time_to_srt(end), "text": text})
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

def translate_to_english(text):
    text = clean_text(text)
    if not text or text == "No Description":
        return "No Description"
    try:
        return translate_text(text) or text
    except Exception as exc:
        print(f"{icons.ICON_WARNING} {bcolors.WARNING}Could not translate description: {exc}{bcolors.ENDC}")
        return text

def translate_texts_batch(texts):
    clean_texts = [clean_text(text) for text in texts]
    if not clean_texts:
        return []
    if len(clean_texts) == 1:
        return [translate_text(clean_texts[0])]
    translated = translate_text(f" {TRANSLATE_BATCH_MARKER} ".join(clean_texts))
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

def translate_cues(cues):
    translated = []
    batches = list(cue_batches(cues))
    total_batches = len(batches)
    for batch_index, batch in enumerate(batches, 1):
        start = len(translated) + 1
        end = start + len(batch) - 1
        print(f"\r{bcolors.LIGHTBLUE}{progress_bar(batch_index - 1, total_batches)}{bcolors.ENDC}", end="", flush=True)
        try:
            translated_texts = translate_texts_batch([cue["text"] for cue in batch])
        except Exception as exc:
            print()
            print(
                f"{icons.ICON_WARNING} {bcolors.WARNING}Subtitle batch translation failed at cues {start}-{end}: "
                f"{exc}; keeping French text for this batch.{bcolors.ENDC}"
            )
            translated_texts = [cue["text"] for cue in batch]
        for cue, text in zip(batch, translated_texts):
            translated.append({**cue, "text": text})
        print(f"\r{bcolors.LIGHTBLUE}{progress_bar(batch_index, total_batches)}{bcolors.ENDC}", end="", flush=True)
    print()
    return translated

def write_srt(cues, output_path):
    lines = []
    for index, cue in enumerate(cues, 1):
        lines.extend([str(index), f"{cue['start']} --> {cue['end']}", cue["text"], ""])
    output_path.write_text("\n".join(lines), encoding="utf-8")

def get_subtitle_url(playback):
    for subtitle in playback.subtitles or []:
        if clean_text(subtitle.get("lang")).lower().startswith("fr"):
            return subtitle.get("url")
    if playback.subtitles:
        return playback.subtitles[0].get("url")
    return None

def save_translated_subtitles(playback, filename):
    subtitle_url = get_subtitle_url(playback)
    if not subtitle_url:
        print(f"{icons.ICON_WARNING} {bcolors.WARNING}No French subtitle URL found in TF1 manifest.{bcolors.ENDC}")
        return None

    response = session.get(subtitle_url, headers=DEFAULT_HEADERS, timeout=30)
    response.raise_for_status()
    cues = parse_vtt(response.text)
    if not cues:
        print(f"{icons.ICON_WARNING} {bcolors.WARNING}No subtitle cues found in TF1 VTT response.{bcolors.ENDC}")
        return None

    output_path = SAVE_PATH / f"{filename}.en.srt"
    print(f"{icons.ICON_WAITING} {bcolors.LIGHTBLUE}Translating French subtitles to English SRT...{bcolors.ENDC}")
    write_srt(translate_cues(cues), output_path)
    print(f"{icons.ICON_SUCCESS} {bcolors.OKGREEN}English subtitles saved: {bcolors.ENDC}{output_path}")
    return output_path

def maybe_save_translated_subtitles(playback, filename, auto_download=False):
    if auto_download:
        return save_translated_subtitles(playback, filename)

    try:
        user_input = input("Do you wish to save translated English subtitles? Y or N: ").strip().lower()
    except EOFError:
        user_input = "n"

    if user_input != "y":
        return None
    return save_translated_subtitles(playback, filename)

def safe_name(value):
    value = clean_text(value).replace("'", "")
    value = re.sub(r"[\\/:*?\"<>|]", " ", value)
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
    parts.extend([resolution, "TF1", "WEB-DL", "AAC2.0", "H.264"])
    return ".".join(part for part in parts if part and part != "Unknown")

def build_download_command(playback, filename, keys=None, interactive=False):
    selectors = "" if interactive else "--select-video best --select-audio best --drop-subtitle all "
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

def resolve_video(video_url, interactive=False):
    video_url = canonical_url(video_url)
    video_id = extract_video_id(video_url)
    metadata = search_metadata(video_url, video_id)
    playback = get_playback_info(video_url, metadata)

    keys = []
    if playback.manifest_type == "mpd":
        if not playback.pssh:
            try:
                playback.pssh = get_pssh_from_manifest(playback.manifest_url)
            except Exception as exc:
                print(f"{icons.ICON_WARNING} {bcolors.WARNING}Could not extract PSSH: {exc}{bcolors.ENDC}")
        if playback.license_url and playback.pssh:
            keys = get_keys(playback.pssh, playback.license_url, metadata)
    elif playback.manifest_type != "m3u8":
        raise ValueError(f"Unsupported manifest type: {playback.manifest_type}")

    resolution = highest_stream_resolution(playback.streams, get_resolution(playback))
    filename = format_filename(metadata, resolution)
    command = build_download_command(playback, filename, keys, interactive=interactive)
    return playback, keys, resolution, filename, command

def run_with_spinner(callback, quiet=False):
    spinner = Spinner()
    spinner.start()
    try:
        if quiet:
            with contextlib.redirect_stdout(io.StringIO()):
                result = callback()
        else:
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
    print(f"{bcolors.LIGHTBLUE}Title: {bcolors.ENDC}{metadata.episode_title or metadata.content_title or 'Unknown'}")
    print(f"{bcolors.LIGHTBLUE}Date Aired: {bcolors.ENDC}{metadata.aired_date or 'Unknown'}")
    print(f"{bcolors.LIGHTBLUE}Description: {bcolors.ENDC}{metadata.description or 'No Description'}")

def print_info_mode(video_url):
    if not is_episode_url(video_url):
        raise ValueError("Info mode requires a TF1 episode/video URL.")
    playback, keys, resolution, filename, command = run_with_spinner(lambda: resolve_video(video_url), quiet=True)
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
        print(f"{icons.ICON_FAILURE} {bcolors.FAIL}No TF1 episodes found.{bcolors.ENDC}")
        return
    show_title = episode_items[0].get("show_title", "TF1")
    grouped_items = group_episode_items_by_series(episode_items)
    group_labels = sorted(grouped_items, key=series_group_sort_key)
    series_summary = ",  ".join(f"{label}({len(grouped_items[label])})" for label in group_labels)
    print(f"{icons.ICON_SUCCESS} {bcolors.OKGREEN}Found {len(episode_items)} TF1 episodes{bcolors.ENDC}")
    print()
    print_series_rule("TF1 Series", show_title)
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
            "Examples: s01e01, s01, s01e01-s01e04"
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
        raise ValueError(f"No TF1 episodes found for selector {format_download_selector(parsed_selector)}.")
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

    if playback.license_url:
        print(f"{bcolors.RED}License URL: {bcolors.ENDC}{playback.license_url}")
    subtitle_url = get_subtitle_url(playback)
    if subtitle_url:
        print(f"{bcolors.LIGHTBLUE}French subtitles: {bcolors.ENDC}{subtitle_url}")
    if playback.pssh:
        print(f"{bcolors.LIGHTBLUE}PSSH: {bcolors.ENDC}{playback.pssh}")
    if keys:
        for key in keys:
            print(f"{bcolors.OKGREEN}KEYS: {bcolors.ENDC}--key {key}")
    else:
        print(f"{icons.ICON_WARNING} {bcolors.WARNING}No keys available - content may be encrypted{bcolors.ENDC}")

    print(f"{bcolors.YELLOW}DOWNLOAD COMMAND: {bcolors.ENDC}")
    print(mask_proxy_command(command))

def maybe_download(command, auto_download=False):
    if auto_download:
        print(f"{icons.ICON_WAITING} {bcolors.OKBLUE}Downloading video...{bcolors.ENDC}")
        subprocess.run(command, shell=True)
        return

    user_input = input("Do you wish to download? Y or N: ").strip().lower()
    if user_input == "y":
        print(f"{icons.ICON_WAITING} {bcolors.OKBLUE}Downloading video...{bcolors.ENDC}")
        subprocess.run(command, shell=True)
    else:
        print(f"{icons.ICON_FAILURE} {bcolors.RED}Download cancelled{bcolors.ENDC}")

def process_video(video_url, auto_download=False, interactive=False):
    video_url = canonical_url(video_url)
    print(f"{icons.ICON_INFO} {bcolors.LIGHTBLUE}Processing: {bcolors.ENDC}{video_url}")
    playback, keys, resolution, filename, command = run_with_spinner(lambda: resolve_video(video_url, interactive=interactive))
    metadata = playback.metadata
    episode_str = f"S{metadata.season:02d}E{metadata.episode:02d}" if metadata.season and metadata.episode else ""
    print_playback_details(playback, keys, command)
    maybe_save_translated_subtitles(playback, filename, auto_download=auto_download)
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
        print(f"{icons.ICON_FAILURE} {bcolors.FAIL}No TF1 episodes found to export.{bcolors.ENDC}")
        return

    export_dir = Path(__file__).resolve().parents[2] / "export"
    export_dir.mkdir(parents=True, exist_ok=True)
    show_title = episode_items[0].get("show_title", "TF1")
    output_path = export_dir / f"tf1_{safe_name(show_title)}_episodes.txt"
    output_path.write_text("\n".join(item["url"] for item in episode_items) + "\n", encoding="utf-8")
    print(f"{icons.ICON_SUCCESS} {bcolors.OKGREEN}Exported list: {output_path}{bcolors.ENDC}")

def main(video_url, downloads_path, wvd_device_path, tf1_credentials=None, tf1_config=None, mode="auto", export_list=False, download_selector=None):
    """Eurovine entry point for TF1 (Widevine)."""
    try:
        if not video_url:
            raise ValueError("No TF1 URL provided.")
        if not downloads_path or not wvd_device_path:
            raise ValueError("Eurovine config requires downloads_path and wvd_device_path for TF1.")

        configure_service(downloads_path, wvd_device_path, tf1_credentials, tf1_config)
        video_url = video_url.strip()
        print(f"{icons.ICON_INFO} {bcolors.LIGHTBLUE}TF1 URL: {bcolors.ENDC}{video_url}")

        if mode == "list":
            if is_episode_url(video_url):
                print(f"{icons.ICON_FAILURE} {bcolors.FAIL}List mode requires a TF1 series URL, not an episode URL.{bcolors.ENDC}")
                return
            print(f"{icons.ICON_WAITING} {bcolors.LIGHTBLUE}Retrieving series information.....{bcolors.ENDC}")
            episode_items = collect_episode_items(video_url)
            list_episode_items(episode_items)
            if export_list:
                export_episode_urls(episode_items)
            return

        if mode == "download":
            if is_episode_url(video_url):
                print(f"{icons.ICON_FAILURE} {bcolors.FAIL}Download selector mode requires a TF1 series URL, not an episode URL.{bcolors.ENDC}")
                return
            print(f"{icons.ICON_WAITING} {bcolors.LIGHTBLUE}Retrieving series information.....{bcolors.ENDC}")
            download_selected_episodes(video_url, download_selector)
            return

        if mode == "info":
            if not is_episode_url(video_url):
                print(f"{icons.ICON_FAILURE} {bcolors.FAIL}Info mode requires a TF1 episode URL, not a series URL.{bcolors.ENDC}")
                return
            print_info_mode(video_url)
            return

        if export_list:
            if is_episode_url(video_url):
                print(f"{icons.ICON_FAILURE} {bcolors.FAIL}Export mode requires a TF1 series URL, not an episode URL.{bcolors.ENDC}")
                return
            print(f"{icons.ICON_WAITING} {bcolors.LIGHTBLUE}Retrieving series information.....{bcolors.ENDC}")
            export_episode_urls(collect_episode_items(video_url))
            return

        if is_episode_url(video_url):
            process_video(video_url, interactive=(mode == "interactive"))
            return

        print(f"{icons.ICON_WARNING} {bcolors.WARNING}Series URLs require a flag. Use --list/-l to list episodes, --export/-x to export episode URLs, or --download/-d SELECTOR to download selected episodes.{bcolors.ENDC}")
    except Exception as exc:
        print(f"{icons.ICON_FAILURE} {bcolors.FAIL}Error: {exc}{bcolors.ENDC}")

if __name__ == "__main__":
    print("Run TF1 through eurovine.py so it can use the shared Eurovine configuration.")

