import base64
import binascii
import html
import json
import re
import shutil
import subprocess
import sys
import time
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.append(str(SCRIPT_DIR))

for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import requests
import urllib3
import icons
from colors import bcolors
from services.proxy import current_proxy_url, mask_proxy_command
try:
    from beaupy.spinners import Spinner
except Exception:
    class Spinner:
        def start(self):
            return None

        def stop(self):
            return None
from pywidevine.cdm import Cdm
from pywidevine.device import Device
from pywidevine.pssh import PSSH

# PlayReady imports
try:
    from pyplayready.cdm import Cdm as PrCdm
    from pyplayready.device import Device as PrDevice
    from pyplayready.system.pssh import PSSH as PrPSSH
    from pyplayready.misc.revocation_list import RevocationList
    PLAYREADY_AVAILABLE = True
except ImportError:
    PLAYREADY_AVAILABLE = False
    print(f"{icons.ICON_WARNING} {bcolors.WARNING}pyplayready not installed. PlayReady support disabled.{bcolors.ENDC}")

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

#   Ozivine: M6+ Video Downloader
#   Usage: enter an M6+ episode URL to retrieve manifest, PSSH, keys, subtitles, and download command.
#   Authentication: credentials are read from config.yaml but playback is probed anonymously first.
#   Geo-Locking: France proxy is supported via config.yaml service_proxies.
#   Quality: handles DASH/Widevine/PlayReady and HLS when exposed by the page.
#   Subtitles: external French DASH/HLS subtitles are translated to an English SRT sidecar.


SERVICE_NAME = "m6"
SERVICE_LABEL = "M6+"
BASE_URL = "https://www.m6.fr"
TRANSLATE_URL = "https://translate.googleapis.com/translate_a/single"
TRANSLATE_BATCH_MARKER = "@@M6BREAK@@"
TRANSLATE_BATCH_SIZE = 35
TRANSLATE_BATCH_CHAR_LIMIT = 4500
DEFAULT_LICENSE_URL = "https://lic.drmtoday.com/license-proxy-widevine/cenc/"
PLAYREADY_LICENSE_URL = "https://lic.drmtoday.com/license-proxy-headerauth/drmtoday/RightsManager.asmx"
GIGYA_API_KEY = "3_hH5KBv25qZTd_sURpixbQW6a4OsiIzIEF2Ei_2H7TXTGLJb_1Hr4THKZianCQhWK"
FRONT_AUTH_URL = "https://front-auth.6cloud.fr/v2/platforms/m6group_web/getJwt"
LAYOUT_URL = "https://layout.6cloud.fr/front/v1/m6web/m6group_web/main/token-web-32/video/{content_id}/layout"
DRM_TOKEN_URL = (
    "https://drm.6cloud.fr/v1/customers/{customer}/platforms/{platform}/services/{service}"
    "/users/{uid}/videos/{content_id}/upfront-token"
)
M6_CLIENT_RELEASE = "6.41.17"
N_M3U8DL = "N_m3u8DL-RE"


session = requests.Session()
config: dict[str, Any] = {}
SAVE_PATH = None
WVD_PATH = ""
PRD_PATH = ""
SERVICE_PROXY = None


def configure_service(downloads_path: str, wvd_device_path: str, prd_device_path: str = "", m6_credentials: Optional[str] = None, m6_config: Optional[dict[str, Any]] = None):
    global config, SAVE_PATH, WVD_PATH, PRD_PATH, SERVICE_PROXY, _M6_DEVICE_ID, _M6_FRONT_AUTH_JWT
    SAVE_PATH = Path(downloads_path)
    WVD_PATH = wvd_device_path
    PRD_PATH = prd_device_path or ""
    config = {"m6": dict(m6_config or {})}
    if m6_credentials:
        config["credentials"] = {"m6": m6_credentials}
    _M6_DEVICE_ID = None
    _M6_FRONT_AUTH_JWT = None
    SERVICE_PROXY = current_proxy_url()
    session.proxies.clear()
    session.verify = True
    if SERVICE_PROXY:
        session.proxies.update({"http": SERVICE_PROXY, "https": SERVICE_PROXY})
        session.verify = False

_M6_DEVICE_ID: Optional[str] = None
_M6_FRONT_AUTH_JWT: Optional[str] = None


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
    air_date: Optional[str] = None
    description: Optional[str] = None
    duration: Optional[str] = None
    video_id: Optional[str] = None
    program_id: Optional[str] = None
    source_url: Optional[str] = None


@dataclass
class PlaybackInfo:
    manifest_url: str
    manifest_type: str
    license_url: Optional[str] = None
    drm_token: Optional[str] = None
    layout_asset: Optional[dict[str, Any]] = None
    pssh: Optional[str] = None
    metadata: Metadata = field(default_factory=Metadata)
    subtitles: list[dict[str, Any]] = field(default_factory=list)
    psshs: list[str] = field(default_factory=list)
    is_playready: bool = False


@dataclass
class EpisodeItem:
    url: str
    title: str = "Unknown"
    season: Optional[int] = None
    episode: Optional[int] = None
    episode_title: Optional[str] = None
    air_date: Optional[str] = None
    description: Optional[str] = None
    duration: Optional[str] = None
    video_id: Optional[str] = None


class EpisodeLinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links = []
        self._current = None
        self._depth = 0

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "a" and not self._current and "c_" in attrs.get("href", ""):
            self._current = {"url": canonical_url(attrs.get("href", "")), "title": clean_text(attrs.get("aria-label")), "_text": []}
            self._depth = 1
            return
        if self._current:
            self._depth += 1
            if tag == "img" and not self._current.get("title") and attrs.get("alt"):
                self._current["title"] = clean_text(attrs.get("alt"))

    def handle_endtag(self, tag):
        if not self._current:
            return
        self._depth -= 1
        if tag == "a" and self._depth <= 0:
            text = clean_text(" ".join(self._current["_text"]))
            if text and not self._current.get("title"):
                self._current["title"] = text
            self.links.append(self._current)
            self._current = None
            self._depth = 0

    def handle_data(self, data):
        if self._current:
            text = clean_text(data)
            if text:
                self._current["_text"].append(text)


def clean_text(value: Any) -> str:
    return re.sub(r"\s+", " ", html.unescape(str(value or ""))).strip()


def strip_tags(value: str) -> str:
    value = re.sub(r"<script\b[^>]*>.*?</script>", " ", value, flags=re.I | re.S)
    value = re.sub(r"<style\b[^>]*>.*?</style>", " ", value, flags=re.I | re.S)
    return clean_text(re.sub(r"<[^>]+>", " ", value))


def canonical_url(value: str) -> str:
    value = clean_text(value)
    if re.match(r"^https?://", value, re.I):
        return value
    return urljoin(BASE_URL, value if value.startswith("/") else f"/{value.strip('/')}")


def fetch_text(url: str, headers: Optional[dict[str, str]] = None, attempts: int = 3) -> str:
    request_headers = dict(DEFAULT_HEADERS)
    if headers:
        request_headers.update(headers)
    last_error = None
    for attempt in range(1, attempts + 1):
        try:
            response = session.get(url, headers=request_headers, timeout=35)
            response.raise_for_status()
            return response.text
        except requests.RequestException as exc:
            last_error = exc
            if attempt < attempts:
                time.sleep(0.75 * attempt)
    raise RuntimeError(f"M6+ request failed: {last_error}")


def get_m6_credentials() -> Optional[tuple[str, str]]:
    value = (config.get("credentials") or {}).get("m6") or (config.get("m6") or {}).get("credentials")
    if not value or ":" not in str(value):
        return None
    username, password = str(value).split(":", 1)
    if not username or not password:
        return None
    return username, password


def m6_api_headers(extra: Optional[dict[str, str]] = None) -> dict[str, str]:
    headers = {
        "Accept": "*/*",
        "Accept-Language": DEFAULT_HEADERS["Accept-Language"],
        "Origin": BASE_URL,
        "Referer": f"{BASE_URL}/",
        "User-Agent": DEFAULT_HEADERS["User-Agent"],
        "X-Customer-Name": "m6web",
        "X-Client-Release": M6_CLIENT_RELEASE,
    }
    if extra:
        headers.update(extra)
    return headers


def get_m6_device_id() -> str:
    global _M6_DEVICE_ID
    if _M6_DEVICE_ID:
        return _M6_DEVICE_ID
    m6_config = config.get("m6") or {}
    _M6_DEVICE_ID = str(m6_config.get("device_id") or m6_config.get("auth_device_id") or f"_luid_{uuid.uuid4()}")
    return _M6_DEVICE_ID


def gigya_login() -> Optional[dict[str, Any]]:
    credentials = get_m6_credentials()
    if not credentials:
        return None
    username, password = credentials
    response = session.post(
        "https://login-gigya.m6.fr/accounts.login",
        headers=m6_api_headers({"Content-Type": "application/x-www-form-urlencoded"}),
        data={
            "loginID": username,
            "password": password,
            "APIKey": GIGYA_API_KEY,
            "include": "profile,data",
            "lang": "fr",
            "format": "json",
        },
        timeout=30,
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get("errorCode"):
        raise RuntimeError(f"M6+ login failed: {payload.get('errorMessage') or payload.get('errorDetails')}")
    if not payload.get("UID") or not payload.get("UIDSignature") or not payload.get("signatureTimestamp"):
        raise RuntimeError("M6+ login did not return UID signature data.")
    return payload


def get_front_auth_jwt() -> tuple[Optional[str], Optional[dict[str, Any]]]:
    global _M6_FRONT_AUTH_JWT
    if _M6_FRONT_AUTH_JWT:
        return _M6_FRONT_AUTH_JWT, None
    login = gigya_login()
    if not login:
        return None, None
    uid = str(login["UID"])
    response = session.get(
        FRONT_AUTH_URL,
        headers=m6_api_headers(
            {
                "x-auth-device-name": "Windows - Firefox",
                "x-auth-device-id": get_m6_device_id(),
                "X-Auth-profile-id": f"_puid_{uid}_DEFAULT0",
                "X-Auth-gigya-uid": uid,
                "X-Auth-gigya-signature": str(login["UIDSignature"]),
                "X-Auth-gigya-signature-timestamp": str(login["signatureTimestamp"]),
            }
        ),
        timeout=30,
    )
    response.raise_for_status()
    token = response.json().get("token")
    if not token:
        raise RuntimeError("M6+ front-auth did not return a JWT.")
    _M6_FRONT_AUTH_JWT = token
    return token, login


def reset_front_auth_jwt(device_id: Optional[str] = None):
    global _M6_DEVICE_ID, _M6_FRONT_AUTH_JWT
    if device_id:
        _M6_DEVICE_ID = device_id.split("|", 1)[-1]
    _M6_FRONT_AUTH_JWT = None


def is_device_gate_layout(layout: Optional[dict[str, Any]]) -> bool:
    if not isinstance(layout, dict):
        return False
    analytics = layout.get("analytics") or {}
    tealium = analytics.get("tealium") or {}
    google = analytics.get("googleAnalytics") or {}
    page_name = clean_text(tealium.get("page_name") or google.get("pageName")).lower()
    if "devicesgate" in page_name:
        return True
    return any(
        clean_text(item.get("reason")).lower() == "deletedevice"
        for item in iter_dicts(layout)
        if isinstance(item, dict)
    )


def device_ids_from_gate(layout: Optional[dict[str, Any]]) -> list[str]:
    ids = []
    seen = set()
    for item in iter_dicts(layout or {}):
        reason_attrs = item.get("reasonAttributes") if isinstance(item, dict) else None
        if not isinstance(reason_attrs, dict):
            continue
        device_id = clean_text(reason_attrs.get("deviceId"))
        if not device_id:
            continue
        device_id = device_id.split("|", 1)[-1]
        if device_id and device_id not in seen:
            seen.add(device_id)
            ids.append(device_id)
    return ids


def request_layout(video_url: str, content_id: str, auth_jwt: str) -> dict[str, Any]:
    params = {"blockPage": "1", "nbPages": "2"}
    response = session.get(
        LAYOUT_URL.format(content_id=content_id),
        headers=m6_api_headers(
            {
                "Authorization": f"Bearer {auth_jwt}",
                "X-Location": canonical_url(video_url),
            }
        ),
        params=params,
        timeout=30,
    )
    response.raise_for_status()
    return response.json()


def fetch_layout(video_url: str, metadata: Metadata) -> Optional[dict[str, Any]]:
    auth_jwt, _login = get_front_auth_jwt()
    if not auth_jwt:
        return None
    content_id = f"clip_{metadata.video_id or extract_video_id(video_url)}"
    layout = request_layout(video_url, content_id, auth_jwt)
    if not is_device_gate_layout(layout):
        return layout

    for device_id in device_ids_from_gate(layout):
        reset_front_auth_jwt(device_id)
        auth_jwt, _login = get_front_auth_jwt()
        if not auth_jwt:
            break
        retry_layout = request_layout(video_url, content_id, auth_jwt)
        if not is_device_gate_layout(retry_layout):
            print(f"{icons.ICON_INFO} {bcolors.LIGHTBLUE}Reused existing M6+ device session for playback.{bcolors.ENDC}")
            return retry_layout
    return layout


def iter_dicts(value: Any):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from iter_dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from iter_dicts(child)


def layout_assets(layout: dict[str, Any]) -> list[dict[str, Any]]:
    assets = []
    for item in iter_dicts(layout):
        path = item.get("path")
        if path and item.get("drm") and (".mpd" in path or ".m3u8" in path):
            assets.append(item)
    return assets


def asset_height(asset: dict[str, Any]) -> int:
    text = " ".join(str(asset.get(key) or "") for key in ("path", "quality", "video_quality", "format"))
    heights = [int(value) for value in re.findall(r"(?:upTo|_)(\d{3,4})p", text, re.I)]
    return max(heights) if heights else 0


def choose_layout_asset(assets: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
    m6_config = config.get("m6") or {}
    prefer_hardware = bool(m6_config.get("prefer_hardware"))
    prefer_hls = bool(m6_config.get("prefer_hls"))

    def score(asset: dict[str, Any]) -> tuple[int, int, int, int]:
        path = str(asset.get("path") or "").lower()
        fmt = str(asset.get("format") or "").lower()
        drm_type = str(((asset.get("drm") or {}).get("type") or "")).lower()
        is_preferred_container = 1 if (".m3u8" in path or "hls" in fmt) else 0
        if not prefer_hls:
            is_preferred_container = 1 if (".mpd" in path or "dash" in fmt) else 0
        is_preferred_drm = 1 if drm_type == "hardware" else 0
        if not prefer_hardware:
            is_preferred_drm = 1 if drm_type == "software" else 0
        return is_preferred_container, is_preferred_drm, asset_height(asset), 1 if asset.get("video_quality") == "hd" else 0

    return max(assets, key=score) if assets else None


def get_drm_token(asset: Optional[dict[str, Any]], auth_jwt: Optional[str] = None) -> Optional[str]:
    config_data = (((asset or {}).get("drm") or {}).get("config") or {})
    if not config_data:
        return ((config.get("m6") or {}).get("drm_token") or (config.get("m6") or {}).get("x_dt_auth_token"))
    if not auth_jwt:
        auth_jwt, _login = get_front_auth_jwt()
    url = DRM_TOKEN_URL.format(
        customer=config_data.get("customerCode") or "m6web",
        platform=config_data.get("platform") or "m6group_web",
        service=config_data.get("serviceCode") or asset.get("service") or "m6replay",
        uid=config_data.get("uid"),
        content_id=config_data.get("contentId"),
    )
    response = session.get(
        url,
        headers=m6_api_headers({"Authorization": f"Bearer {auth_jwt}"}),
        timeout=30,
    )
    response.raise_for_status()
    token = response.json().get("token")
    if not token:
        raise RuntimeError("M6+ DRM endpoint did not return an upfront token.")
    return token


def extract_video_id(video_url: str) -> str:
    match = re.search(r"(?:-|/)c_(\d+)(?:[/?#]|$)", urlparse(canonical_url(video_url)).path)
    if match:
        return match.group(1)
    raise ValueError("Could not extract M6+ video ID from URL.")


def extract_program_id(video_url: str) -> Optional[str]:
    match = re.search(r"-p_(\d+)(?:/|$)", urlparse(canonical_url(video_url)).path)
    return match.group(1) if match else None


def is_episode_url(video_url: str) -> bool:
    return bool(re.search(r"(?:-|/)c_\d+(?:[/?#]|$)", urlparse(canonical_url(video_url)).path))


def parse_season_episode_from_url(video_url: str) -> tuple[Optional[int], Optional[int]]:
    path = urlparse(canonical_url(video_url)).path
    match = re.search(r"/saison-(\d+)-(?:e|Ã©|pisode-|pisode-)?(?:pisode-)?(\d+)-c_\d+", path, re.I)
    if not match:
        match = re.search(r"/s(\d+)-e(\d+)-c_\d+", path, re.I)
    if match:
        return int(match.group(1)), int(match.group(2))
    return None, None


def meta_content(html_text: str, *names: str) -> str:
    for name in names:
        pattern = (
            r'<meta\b(?=[^>]*(?:name|property)=["\']'
            + re.escape(name)
            + r'["\'])(?=[^>]*content=["\']([^"\']*)["\'])[^>]*>'
        )
        match = re.search(pattern, html_text, re.I | re.S)
        if match:
            return clean_text(match.group(1))
    return ""


def page_title(html_text: str) -> str:
    match = re.search(r"<title[^>]*>(.*?)</title>", html_text, re.I | re.S)
    return clean_text(match.group(1)) if match else ""


def show_title_from_html(html_text: str) -> str:
    og_title = meta_content(html_text, "og:title", "title")
    if og_title:
        return clean_text(og_title.split(" : ", 1)[0].split(" - ", 1)[0].split(" sur M6+", 1)[0])
    match = re.search(r"<h1[^>]*>(.*?)</h1>", html_text, re.I | re.S)
    return strip_tags(match.group(1)) if match else "Unknown"


def episode_title_from_html(html_text: str, fallback: str = "") -> str:
    title = meta_content(html_text, "og:title", "title") or page_title(html_text) or fallback
    match = re.search(r":\s*([^:-]+?)(?:\s+\d{2}[-/]\d{2}[-/]\d{4}|[- ]+M6\+|$)", title)
    if match:
        return clean_text(match.group(1))
    match = re.search(r"(S\d+\s*E\d+\s*-\s*[^-]+)", title, re.I)
    return clean_text(match.group(1)) if match else clean_text(fallback)


def parse_info_date(value: str) -> str:
    value = clean_text(value)
    if not value:
        return ""
    for fmt in ("%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.strftime("%Y-%m-%d")
    except ValueError:
        return value


def duration_from_text(value: str) -> str:
    match = re.search(r"\b(\d{1,2}:\d{2}(?::\d{2})?)\b", clean_text(value))
    return match.group(1) if match else ""


def hydrate_metadata_from_html(metadata: Metadata, html_text: str) -> Metadata:
    text = strip_tags(html_text)
    title = meta_content(html_text, "og:title") or page_title(html_text)
    title_match = re.search(r":\s*([^:-]+?)(?:\s+\d{2}[-/]\d{2}[-/]\d{4}|[- ]+M6\+|$)", title)
    if title_match:
        metadata.episode_title = clean_text(title_match.group(1)) or metadata.episode_title

    date_match = re.search(r"\bDiffus\S*\s+le\s+(\d{2}/\d{2}/\d{4})", text, re.I)
    if date_match:
        metadata.air_date = parse_info_date(date_match.group(1))

    desc_match = re.search(
        r"\bDiffus\S*\s+le\s+\d{2}/\d{2}/\d{4}\s+(.*?)(?:\s+En savoir plus|\s+Retour\s+.\s+la navigation|$)",
        text,
        re.I,
    )
    if desc_match:
        metadata.description = clean_text(desc_match.group(1))

    if not metadata.description:
        metadata.description = meta_content(html_text, "og:description", "description", "twitter:description") or None

    metadata.duration = duration_from_text(text) or metadata.duration
    return metadata

def search_metadata(video_url: str, video_id: str) -> Metadata:
    source_url = canonical_url(video_url)
    html_text = fetch_text(source_url)
    season, episode = parse_season_episode_from_url(source_url)
    title = show_title_from_html(html_text)
    ep_title = episode_title_from_html(html_text)
    metadata = Metadata(
        title=title or "Unknown",
        season=season,
        episode=episode,
        episode_title=ep_title or None,
        video_id=video_id,
        program_id=extract_program_id(source_url),
        source_url=source_url,
    )
    return hydrate_metadata_from_html(metadata, html_text)


def collect_episode_item(video_url: str) -> EpisodeItem:
    video_id = extract_video_id(video_url)
    metadata = search_metadata(video_url, video_id)
    return EpisodeItem(
        url=canonical_url(video_url),
        title=metadata.title,
        season=metadata.season,
        episode=metadata.episode,
        episode_title=metadata.episode_title,
        air_date=metadata.air_date,
        description=metadata.description,
        duration=metadata.duration,
        video_id=video_id,
    )


def collect_episode_items(series_url: str, show_progress: bool = True) -> list[EpisodeItem]:
    html_text = fetch_text(canonical_url(series_url))
    parser = EpisodeLinkParser()
    parser.feed(html_text)
    seen = set()
    items = []
    show_title = show_title_from_html(html_text)
    for link in parser.links:
        video_id = extract_video_id(link["url"]) if is_episode_url(link["url"]) else ""
        if not video_id or video_id in seen:
            continue
        seen.add(video_id)
        season, episode = parse_season_episode_from_url(link["url"])
        ep_title = clean_text(link.get("title")) or (f"Episode {episode}" if episode else show_title)
        items.append(EpisodeItem(link["url"], show_title, season, episode, ep_title, None, None, None, video_id))
    if not items:
        raise RuntimeError("Could not find any M6+ episode links on the series page.")
    return sorted(items, key=episode_sort_key)


def resolve_single_playable_url(page_url: str) -> Optional[str]:
    if is_episode_url(page_url):
        return canonical_url(page_url)
    try:
        items = collect_episode_items(page_url, show_progress=False)
    except Exception:
        return None
    if len(items) == 1:
        return items[0].url
    return None


def episode_sort_key(item: EpisodeItem):
    return (item.season if item.season is not None else 9999, item.episode if item.episode is not None else 9999, item.video_id or item.url)


def episode_series_number(item: EpisodeItem):
    return item.season


def episode_number(item: EpisodeItem):
    return item.episode


def episode_tree_label(item: EpisodeItem):
    number = episode_number(item)
    title = item.episode_title or item.title or item.url
    return str(number).zfill(2) if number is not None else "-", title


def group_episode_items_by_series(episode_items):
    grouped = {}
    for item in sorted(episode_items, key=episode_sort_key):
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
        f"{bcolors.LIGHTBLUE}{'-' * left_width}{bcolors.ENDC} "
        f"{bcolors.LIGHTBLUE}{service_label}: {bcolors.ENDC}{bcolors.WHITE}{series_title}{bcolors.ENDC} "
        f"{bcolors.LIGHTBLUE}{'-' * right_width}{bcolors.ENDC}"
    )


def parse_selector_part(selector_part):
    match = re.fullmatch(r"s(?P<season>\d{2}|\d{4})(?:e(?P<episode>\d{2}))?", selector_part)
    if not match:
        raise ValueError("Download selector must be sXXeXX, sXXXXeXX, sXX, sXXXX, or a matching range.")
    return {"season": int(match.group("season")), "episode": int(match.group("episode")) if match.group("episode") else None}


def parse_download_selector(selector):
    selector = str(selector or "").strip().lower()
    if "-" not in selector:
        part = parse_selector_part(selector)
        return {"type": "single_episode" if part["episode"] is not None else "single_season", "start": part, "end": part}
    start, end = selector.split("-", 1)
    start_part = parse_selector_part(start)
    end_part = parse_selector_part(end)
    if (start_part["episode"] is None) != (end_part["episode"] is None):
        raise ValueError("Download range must use two episode selectors or two season selectors.")
    if start_part["episode"] is not None:
        if (start_part["season"], start_part["episode"]) > (end_part["season"], end_part["episode"]):
            raise ValueError("Download episode range start must be before the end selector.")
        return {"type": "episode_range", "start": start_part, "end": end_part}
    if start_part["season"] > end_part["season"]:
        raise ValueError("Download season range start must be before the end selector.")
    return {"type": "season_range", "start": start_part, "end": end_part}


def format_selector_part(part):
    season = part["season"]
    season_label = f"s{season:04d}" if season >= 1000 else f"s{season:02d}"
    return f"{season_label}e{part['episode']:02d}" if part["episode"] is not None else season_label


def format_download_selector(parsed_selector):
    if parsed_selector["start"] == parsed_selector["end"]:
        return format_selector_part(parsed_selector["start"])
    return f"{format_selector_part(parsed_selector['start'])}-{format_selector_part(parsed_selector['end'])}"


def format_queue_selector(season, episode=None):
    season_label = f"S{season:04d}" if season >= 1000 else f"S{season:02d}"
    return f"{season_label}E{episode:02d}" if episode is not None else season_label


def select_episode_items(series_url, selector):
    parsed_selector = parse_download_selector(selector)
    selected = []
    for item in collect_episode_items(series_url, show_progress=False):
        season = episode_series_number(item)
        episode = episode_number(item)
        if season is None or episode is None:
            continue
        if parsed_selector["type"] == "single_episode":
            keep = season == parsed_selector["start"]["season"] and episode == parsed_selector["start"]["episode"]
        elif parsed_selector["type"] == "single_season":
            keep = season == parsed_selector["start"]["season"]
        elif parsed_selector["type"] == "episode_range":
            keep = (parsed_selector["start"]["season"], parsed_selector["start"]["episode"]) <= (season, episode) <= (parsed_selector["end"]["season"], parsed_selector["end"]["episode"])
        else:
            keep = parsed_selector["start"]["season"] <= season <= parsed_selector["end"]["season"]
        if keep:
            selected.append(item)
    if not selected:
        raise ValueError(f"No {SERVICE_LABEL} episodes found for selector {format_download_selector(parsed_selector)}.")
    return sorted(selected, key=episode_sort_key)


def print_download_queue(episode_items):
    print()
    print(f"{bcolors.LIGHTBLUE}Download queue:{bcolors.ENDC}")
    for item in episode_items:
        selector = format_queue_selector(item.season, item.episode) if item.season is not None and item.episode is not None else item.video_id or item.url
        _, title = episode_tree_label(item)
        print(f"{selector} {title}")


def list_episode_items(episode_items):
    if not episode_items:
        print(f"{icons.ICON_FAILURE} {bcolors.FAIL}No {SERVICE_LABEL} episodes found.{bcolors.ENDC}")
        return
    show_title = episode_items[0].title or SERVICE_LABEL
    grouped_items = group_episode_items_by_series(episode_items)
    group_labels = sorted(grouped_items, key=series_group_sort_key)
    series_summary = ",  ".join(f"{label}({len(grouped_items[label])})" for label in group_labels)

    print(f"{icons.ICON_SUCCESS} {bcolors.OKGREEN}Found {len(episode_items)} {SERVICE_LABEL} episodes{bcolors.ENDC}")
    print()
    print_series_rule(f"{SERVICE_LABEL} Series", show_title)
    print()
    print(f"{bcolors.GRAY}{len(group_labels)} Series" + (f",  {series_summary}" if series_summary else "") + f"{bcolors.ENDC}")

    for series_index, series_label in enumerate(group_labels):
        series_items = grouped_items[series_label]
        if series_index > 0:
            print(f"{bcolors.GRAY}|{bcolors.ENDC}")

        group_is_last = series_index == len(group_labels) - 1
        group_branch = "`-" if group_is_last else "+-"
        group_child_prefix = "   " if group_is_last else "|  "
        print(f"{bcolors.GRAY}{group_branch} {series_label}: {bcolors.ENDC}{len(series_items)} episodes")

        for episode_index, item in enumerate(series_items):
            is_last = episode_index == len(series_items) - 1
            branch = "`-" if is_last else "+-"
            url_branch = "  " if is_last else "| "
            number_label, title = episode_tree_label(item)
            print(f"{bcolors.GRAY}{group_child_prefix}{branch} {number_label}. {bcolors.ENDC}{title}")
            print(f"{bcolors.GRAY}{group_child_prefix}{url_branch} {bcolors.ENDC}{bcolors.LIGHTBLUE}{item.url}{bcolors.ENDC}")


def extract_manifest_urls(html_text: str) -> list[str]:
    urls = []
    for match in re.finditer(r'<video\b[^>]+src=["\']([^"\']+\.(?:mpd|m3u8)[^"\']*)["\']', html_text, re.I | re.S):
        urls.append(html.unescape(match.group(1)))
    for match in re.finditer(r'https?:\\?/\\?/[^"\'<> ]+\.(?:mpd|m3u8)[^"\'<> ]*', html_text, re.I):
        url = html.unescape(match.group(0)).replace("\\/", "/")
        urls.append(url)
    seen = set()
    deduped = []
    for url in urls:
        if url not in seen:
            seen.add(url)
            deduped.append(url)
    return deduped


def manifest_type_from_url(url: str) -> str:
    path = urlparse(url).path.lower()
    if path.endswith(".mpd"):
        return "mpd"
    if path.endswith(".m3u8"):
        return "m3u8"
    raise ValueError(f"Unsupported manifest URL: {url}")


def choose_manifest_url(manifests: list[str], layout_asset: Optional[dict[str, Any]] = None) -> str:
    m6_config = config.get("m6") or {}
    prefer_hardware = bool(m6_config.get("prefer_hardware"))
    prefer_hls = bool(m6_config.get("prefer_hls"))

    deduped = list(dict.fromkeys(manifests))
    if not layout_asset and not prefer_hardware:
        software_or_clear = [url for url in deduped if "drm_hardware" not in url.lower()]
        if software_or_clear:
            deduped = software_or_clear
        elif deduped:
            raise RuntimeError(
                "Only hardware DRM manifests were exposed by M6+. Set m6.prefer_hardware: true "
                "to inspect them, or free an M6+ web device slot so the authenticated software assets are available."
            )

    def score(url: str) -> tuple[int, int, int]:
        lower = url.lower()
        is_preferred_container = 1 if ".m3u8" in lower else 0
        if not prefer_hls:
            is_preferred_container = 1 if ".mpd" in lower else 0
        is_preferred_drm = 1 if "drm_hardware" in lower else 0
        if not prefer_hardware:
            is_preferred_drm = 1 if "drm_software" in lower else 0
        heights = [int(value) for value in re.findall(r"(?:upto|_)(\d{3,4})p", lower)]
        return is_preferred_container, is_preferred_drm, max(heights) if heights else 0

    return max(deduped, key=score)


def get_license_url() -> str:
    m6_cfg = config.get("m6") or {}
    return clean_text(m6_cfg.get("license_url") or m6_cfg.get("widevine_license_url")) or DEFAULT_LICENSE_URL


def get_playback_info(video_url: str, metadata: Metadata) -> PlaybackInfo:
    layout_asset = None
    drm_token = None
    manifests = []
    try:
        layout = fetch_layout(video_url, metadata)
        layout_asset = choose_layout_asset(layout_assets(layout or {}))
        if layout_asset:
            manifests.append(str(layout_asset.get("path")))
            drm_token = get_drm_token(layout_asset)
    except Exception as exc:
        print(f"{icons.ICON_WARNING} {bcolors.WARNING}M6+ authenticated playback probe failed, falling back to page manifest: {exc}{bcolors.ENDC}")

    html_text = fetch_text(canonical_url(video_url))
    manifests.extend(extract_manifest_urls(html_text))
    if not manifests:
        raise RuntimeError("Could not find DASH/HLS playback URL in the M6+ page. The title may require login, consent, or may be unavailable.")
    manifest_url = choose_manifest_url(manifests, layout_asset)
    manifest_type = manifest_type_from_url(manifest_url)
    
    # Determine if this is PlayReady or Widevine
    is_playready = "hardware.mpd" in manifest_url.lower()
    is_widevine = "software.mpd" in manifest_url.lower()
    
    # Choose appropriate license URL
    license_url = get_license_url()
    if is_playready:
        license_url = PLAYREADY_LICENSE_URL
    elif is_widevine:
        print(f"{icons.ICON_INFO} {bcolors.LIGHTBLUE}Detected software.mpd - using Widevine DRM{bcolors.ENDC}")
    else:
        print(f"{icons.ICON_WARNING} {bcolors.WARNING}Unknown manifest type, defaulting to Widevine{bcolors.ENDC}")
    
    playback = PlaybackInfo(
        manifest_url=manifest_url,
        manifest_type=manifest_type,
        license_url=license_url if manifest_type == "mpd" else None,
        drm_token=drm_token,
        layout_asset=layout_asset,
        metadata=metadata,
        is_playready=is_playready,
    )
    if manifest_type == "mpd":
        try:
            playback.psshs = get_pssh_values_from_manifest(manifest_url, is_playready=playback.is_playready)
            playback.pssh = playback.psshs[0] if playback.psshs else None
            playback.subtitles = subtitles_from_dash_manifest(manifest_url)
        except Exception as exc:
            print(f"{icons.ICON_WARNING} {bcolors.WARNING}Could not inspect DASH manifest yet: {exc}{bcolors.ENDC}")
    elif manifest_type == "m3u8":
        playback.subtitles = subtitles_from_hls_manifest(manifest_url)
    return playback


def get_pssh_values_from_manifest(manifest_url: str, is_playready: Optional[bool] = None) -> list[str]:
    response = session.get(manifest_url, headers=DEFAULT_HEADERS, timeout=30)
    response.raise_for_status()
    root = ET.fromstring(response.content)
    ns_mpd = "{urn:mpeg:dash:schema:mpd:2011}"
    ns_cenc = "{urn:mpeg:cenc:2013}"
    widevine_uuid = "edef8ba9-79d6-4ace-a3c8-27dcd51d21ed"
    playready_uuid = "9a04f079-9840-4286-ab92-e65be0885f95"
    values = []
    skipped_invalid = 0
    for cp in root.findall(".//" + ns_mpd + "ContentProtection"):
        scheme = (cp.attrib.get("schemeIdUri") or "").lower()
        is_widevine_scheme = widevine_uuid in scheme
        is_playready_scheme = playready_uuid in scheme
        if is_playready is True and not is_playready_scheme:
            continue
        if is_playready is False and not is_widevine_scheme:
            continue
        if is_playready is None and not (is_widevine_scheme or is_playready_scheme):
            continue
        pssh_el = cp.find(ns_cenc + "pssh")
        if pssh_el is not None and pssh_el.text:
            pssh_data = pssh_el.text.strip()
            try:
                base64.b64decode(pssh_data, validate=True)
            except (binascii.Error, ValueError):
                skipped_invalid += 1
                continue
            if pssh_data not in values:
                values.append(pssh_data)
    if not values:
        drm_label = "PlayReady" if is_playready else "Widevine" if is_playready is False else "DRM"
        details = f" ({skipped_invalid} invalid value(s) skipped)" if skipped_invalid else ""
        raise ValueError(f"{drm_label} PSSH not found in the manifest{details}.")
    return values

def build_license_headers(metadata: Metadata, drm_token: Optional[str] = None, is_playready: bool = False) -> dict[str, str]:
    headers = {
        "Accept": "*/*",
        "Origin": BASE_URL,
        "Referer": metadata.source_url or f"{BASE_URL}/",
        "User-Agent": DEFAULT_HEADERS["User-Agent"],
    }
    if drm_token:
        headers["x-dt-auth-token"] = drm_token
    if is_playready:
        headers["Content-Type"] = "text/xml; charset=UTF-8"
    custom = ((config.get("m6") or {}).get("license_headers") or {})
    headers.update({str(k): str(v) for k, v in custom.items()})
    return headers


def post_license_challenge(license_url: str, challenge: bytes, metadata: Metadata, drm_token: Optional[str] = None, is_playready: bool = False) -> bytes:
    headers = build_license_headers(metadata, drm_token, is_playready)
    response = session.post(license_url, headers=headers, data=challenge, timeout=30)
    try:
        response.raise_for_status()
    except requests.exceptions.HTTPError as exc:
        print(f"{icons.ICON_FAILURE} {bcolors.FAIL}HTTPError: {exc}{bcolors.ENDC}")
        print(f"{icons.ICON_INFO} Response Headers: {response.headers}")
        print(f"{icons.ICON_INFO} Response Text: {response.text[:1000]}")
        raise
    
    # Handle PlayReady XML response 
    if is_playready:
        # Parse XML response directly (PlayReady returns XML)
        try:
            # For PlayReady, the response.text is the license XML
            return response.text.encode('utf-8') if isinstance(response.text, str) else response.content
        except Exception:
            return response.content
    
    # Handle Widevine JSON response
    content_type = response.headers.get("content-type", "")
    if "json" in content_type or response.content[:1] == b"{":
        payload = response.json()
        if payload.get("status") and payload.get("status") != "OK":
            raise RuntimeError(f"M6+ license request failed: {payload}")
        license_b64 = payload.get("license")
        if not license_b64:
            raise RuntimeError(f"M6+ license response did not contain a license: {payload}")
        return base64.b64decode(license_b64)
    return response.content


def get_playready_keys(pssh_value: str, license_url: str, metadata: Metadata, drm_token: Optional[str] = None, quiet: bool = False) -> list[str]:
    """Get PlayReady keys using the pyplayready library."""
    if not PLAYREADY_AVAILABLE:
        print(f"{icons.ICON_FAILURE} {bcolors.FAIL}pyplayready not installed. Cannot retrieve PlayReady keys.{bcolors.ENDC}")
        return []

    try:
        prd_path = Path(PRD_PATH)
        if not prd_path.exists():
            print(f"{icons.ICON_WARNING} {bcolors.WARNING}PlayReady device file not found at {prd_path}{bcolors.ENDC}")
            return []

        if not quiet:
            print(f"{icons.ICON_INFO} {bcolors.LIGHTBLUE}Loading PlayReady device from {prd_path}{bcolors.ENDC}")
        device = PrDevice.load(str(prd_path))
        cdm = PrCdm.from_device(device)
        session_id = cdm.open()

        try:
            pssh = PrPSSH(pssh_value)
            if not pssh.wrm_headers:
                raise ValueError("PlayReady PSSH did not contain a WRMHEADER.")
            if not quiet:
                print(f"{icons.ICON_INFO} {bcolors.LIGHTBLUE}Getting PlayReady license challenge...{bcolors.ENDC}")

            licence_challenge = cdm.get_license_challenge(
                session_id,
                pssh.wrm_headers[0],
                rev_lists=RevocationList.SupportedListIds,
            )

            if not quiet:
                print(f"{icons.ICON_INFO} {bcolors.LIGHTBLUE}Posting PlayReady license challenge to {license_url}{bcolors.ENDC}")
            response = session.post(
                url=license_url,
                headers={
                    "Content-Type": "text/xml; charset=UTF-8",
                    "x-dt-auth-token": drm_token if drm_token else "",
                },
                data=licence_challenge,
                timeout=30,
            )
            response.raise_for_status()

            if not quiet:
                print(f"{icons.ICON_INFO} {bcolors.LIGHTBLUE}Parsing PlayReady license...{bcolors.ENDC}")
            cdm.parse_license(session_id, response.text)

            keys = []
            for key in cdm.get_keys(session_id):
                key_str = f"{key.key_id.hex}:{key.key.hex()}"
                keys.append(key_str)

            return keys

        finally:
            cdm.close(session_id)

    except Exception as exc:
        if not quiet:
            print(f"{icons.ICON_WARNING} {bcolors.WARNING}PlayReady key retrieval failed: {exc}{bcolors.ENDC}")
            import traceback
            traceback.print_exc()
        return []

def get_keys_for_pssh(pssh_value: str, license_url: str, metadata: Metadata, drm_token: Optional[str] = None, is_playready: bool = False, quiet: bool = False) -> list[str]:
    if is_playready:
        return get_playready_keys(pssh_value, license_url, metadata, drm_token, quiet=quiet)
    else:
        try:
            pssh = PSSH(pssh_value)
        except (binascii.Error, ValueError) as exc:
            print(f"{icons.ICON_FAILURE} {bcolors.FAIL}Could not parse PSSH: {exc}{bcolors.ENDC}")
            return []
        device = Device.load(WVD_PATH)
        cdm = Cdm.from_device(device)
        session_id = cdm.open()
        try:
            challenge = cdm.get_license_challenge(session_id, pssh)
            licence = post_license_challenge(license_url, challenge, metadata, drm_token, is_playready=False)
            cdm.parse_license(session_id, licence)
            return [f"{key.kid.hex}:{key.key.hex()}" for key in cdm.get_keys(session_id) if key.type == "CONTENT"]
        finally:
            cdm.close(session_id)

def get_keys(playback: PlaybackInfo) -> list[str]:
    keys = []
    seen = set()

    if not playback.is_playready:
        print(f"{icons.ICON_INFO} {bcolors.LIGHTBLUE}Using Widevine path (software.mpd detected){bcolors.ENDC}")

    pssh_values = playback.psshs or ([playback.pssh] if playback.pssh else [])
    candidate_failures = 0
    for pssh in pssh_values:
        candidate_keys = get_keys_for_pssh(
            pssh,
            playback.license_url,
            playback.metadata,
            playback.drm_token,
            playback.is_playready,
            quiet=playback.is_playready,
        )
        if playback.is_playready and not candidate_keys:
            candidate_failures += 1
            continue
        for key in candidate_keys:
            if key not in seen:
                seen.add(key)
                keys.append(key)
        if playback.is_playready and keys:
            break
    if playback.is_playready and not keys and candidate_failures:
        print(f"{icons.ICON_WARNING} {bcolors.WARNING}No usable PlayReady PSSH found after trying {candidate_failures} candidate(s).{bcolors.ENDC}")
    return keys

def format_bitrate(value: Any) -> str:
    try:
        bitrate = int(float(value))
    except (TypeError, ValueError):
        return "-"
    return f"{bitrate / 1000000:.2f} Mbps" if bitrate >= 1000000 else f"{bitrate // 1000} Kbps"


def format_frame_rate(value: Any) -> str:
    value = clean_text(value)
    if "/" in value:
        try:
            numerator, denominator = value.split("/", 1)
            return f"{float(numerator) / float(denominator):.3g} fps"
        except (ValueError, ZeroDivisionError):
            pass
    return f"{value} fps" if value else ""


def parse_hls_attribute_string(value: str) -> dict[str, str]:
    return {
        match.group(1): match.group(2).strip().strip('"')
        for match in re.finditer(r'([A-Z0-9-]+)=("[^"]*"|[^,]*)', value)
    }


def stream_sort_key(stream: dict[str, str]):
    type_order = {"Vid": 0, "Aud": 1, "Sub": 2}
    height_match = re.search(r"x(\d+)", stream.get("resolution") or "")
    height = int(height_match.group(1)) if height_match else 0
    bitrate_text = stream.get("bitrate") or ""
    bitrate_match = re.search(r"[\d.]+", bitrate_text)
    bitrate = float(bitrate_match.group()) if bitrate_match else 0
    if "Mbps" in bitrate_text:
        bitrate *= 1000
    return (type_order.get(stream.get("type"), 9), -height, -bitrate, stream.get("lang") or "")


def parse_dash_streams(manifest_text: str) -> list[dict[str, str]]:
    try:
        root = ET.fromstring(manifest_text)
    except ET.ParseError as exc:
        raise ValueError(f"Unable to parse the DASH manifest: {exc}") from exc

    streams = []
    for adaptation in root.findall(".//{*}AdaptationSet"):
        adaptation_mime = clean_text(adaptation.get("mimeType"))
        adaptation_type = clean_text(adaptation.get("contentType"))
        adaptation_codec = clean_text(adaptation.get("codecs"))
        adaptation_lang = clean_text(adaptation.get("lang")) or "-"
        roles = [clean_text(node.get("value")) for node in adaptation.findall("{*}Role") if node.get("value")]
        role = ", ".join(roles)
        adaptation_channels = next(
            (clean_text(node.get("value")) for node in adaptation.findall("{*}AudioChannelConfiguration") if node.get("value")),
            "",
        )

        for representation in adaptation.findall("{*}Representation"):
            mime_type = clean_text(representation.get("mimeType")) or adaptation_mime
            content_type = clean_text(representation.get("contentType")) or adaptation_type
            codec = clean_text(representation.get("codecs")) or adaptation_codec or "-"
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

            extra = []
            frame_rate = format_frame_rate(representation.get("frameRate") or adaptation.get("frameRate"))
            sample_rate = clean_text(representation.get("audioSamplingRate") or adaptation.get("audioSamplingRate"))
            if frame_rate:
                extra.append(frame_rate)
            if sample_rate:
                extra.append(f"{sample_rate} Hz")
            if role:
                extra.append(role)
            if representation.get("id"):
                extra.append(f"id={representation.get('id')}")

            streams.append(
                {
                    "type": stream_type,
                    "resolution": f"{width}x{height}" if width and height else "-",
                    "bitrate": format_bitrate(representation.get("bandwidth")),
                    "codec": codec,
                    "lang": lang,
                    "channels": channels or "-",
                    "extra": ", ".join(extra) or "-",
                }
            )
    return sorted(streams, key=stream_sort_key)


def parse_hls_streams(manifest_text: str) -> list[dict[str, str]]:
    streams = []
    pending_variant = None
    for raw_line in manifest_text.splitlines():
        line = raw_line.strip()
        if line.startswith("#EXT-X-STREAM-INF:"):
            pending_variant = parse_hls_attribute_string(line.split(":", 1)[1])
            continue
        if pending_variant is not None and line and not line.startswith("#"):
            attrs = pending_variant
            streams.append(
                {
                    "type": "Vid",
                    "resolution": attrs.get("RESOLUTION") or "-",
                    "bitrate": format_bitrate(attrs.get("AVERAGE-BANDWIDTH") or attrs.get("BANDWIDTH")),
                    "codec": attrs.get("CODECS") or "-",
                    "lang": "-",
                    "channels": "-",
                    "extra": ", ".join(
                        value
                        for value in (
                            f"fps={attrs.get('FRAME-RATE')}" if attrs.get("FRAME-RATE") else "",
                            f"video={attrs.get('VIDEO')}" if attrs.get("VIDEO") else "",
                            f"audio={attrs.get('AUDIO')}" if attrs.get("AUDIO") else "",
                        )
                        if value
                    )
                    or "-",
                }
            )
            pending_variant = None
            continue
        if not line.startswith("#EXT-X-MEDIA:"):
            continue

        attrs = parse_hls_attribute_string(line.split(":", 1)[1])
        media_type = attrs.get("TYPE", "").upper()
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
                "lang": attrs.get("LANGUAGE") or "-",
                "channels": attrs.get("CHANNELS") or "-",
                "extra": ", ".join(
                    value
                    for value in (
                        attrs.get("NAME"),
                        "default" if attrs.get("DEFAULT") == "YES" else "",
                        "forced" if attrs.get("FORCED") == "YES" else "",
                        f"uri={attrs.get('URI')}" if attrs.get("URI") else "",
                    )
                    if value
                )
                or "-",
            }
        )
    return sorted(streams, key=stream_sort_key)


def parse_manifest_streams(manifest_text: str) -> tuple[list[dict[str, str]], str]:
    if manifest_text.lstrip().startswith("#EXTM3U"):
        return parse_hls_streams(manifest_text), "HLS"
    return parse_dash_streams(manifest_text), "DASH"


def fetch_manifest(manifest_url: str) -> str:
    try:
        response = session.get(manifest_url, headers=DEFAULT_HEADERS, timeout=30)
        response.raise_for_status()
        return response.text
    except requests.RequestException as exc:
        raise RuntimeError(f"Failed to fetch manifest: {exc}") from exc


def print_streams(streams: list[dict[str, str]]) -> None:
    print(f"\n{bcolors.YELLOW}Available streams:{bcolors.ENDC}")
    if not streams:
        print("No video, audio, or subtitle streams were found in the manifest.")
        return

    headings = ("#", "Type", "Resolution", "Bitrate", "Codec", "Lang", "Channels", "Other")
    rows = [
        (
            str(index),
            stream["type"],
            stream["resolution"],
            stream["bitrate"],
            stream["codec"],
            stream["lang"],
            stream["channels"],
            stream["extra"],
        )
        for index, stream in enumerate(streams, start=1)
    ]
    widths = [min(max(len(headings[column]), *(len(row[column]) for row in rows)), 52) for column in range(len(headings))]
    widths[0] = 3
    print("  ".join(f"{heading:<{widths[index]}}" for index, heading in enumerate(headings)))
    print("  ".join("-" * width for width in widths))
    for row in rows:
        print("  ".join(f"{value[:widths[index]]:<{widths[index]}}" for index, value in enumerate(row)))


def max_height_from_streams(streams: list[dict[str, str]], default: str = "1080") -> str:
    heights = []
    for stream in streams:
        if stream.get("type") != "Vid":
            continue
        match = re.search(r"x(\d+)", stream.get("resolution") or "")
        if match:
            heights.append(int(match.group(1)))
    return str(max(heights)) if heights else str(default)


def metadata_description(metadata: Metadata, translate_description: bool = False) -> str:
    description = clean_text(metadata.description)
    if not description or not translate_description:
        return description
    try:
        return translate_text(description) or description
    except Exception as exc:
        print(f"{icons.ICON_WARNING} {bcolors.WARNING}Could not translate description, keeping French text: {exc}{bcolors.ENDC}")
        return description


def print_episode_metadata(metadata: Metadata, translate_description: bool = False) -> None:
    rows = [
        ("Show", metadata.title),
        ("Title", metadata.episode_title),
        ("Date Aired", metadata.air_date),
        ("Description", metadata_description(metadata, translate_description)),
    ]
    print(f"\n{bcolors.YELLOW}Episode metadata:{bcolors.ENDC}")
    for label, value in rows:
        value = clean_text(value)
        if value:
            print(f"{bcolors.LIGHTBLUE}{label}: {bcolors.ENDC}{value}")


def get_dash_resolution(mpd_url: str) -> str:
    response = session.get(mpd_url, headers=DEFAULT_HEADERS, timeout=30)
    response.raise_for_status()
    root = ET.fromstring(response.content)
    heights = [int(rep.get("height")) for rep in root.findall(".//{urn:mpeg:dash:schema:mpd:2011}Representation") if rep.get("height")]
    return f"{max(heights)}p" if heights else "Unknown"


def get_hls_resolution(m3u8_url: str) -> str:
    response = session.get(m3u8_url, headers=DEFAULT_HEADERS, timeout=30)
    response.raise_for_status()
    resolutions = re.findall(r"RESOLUTION=\d+x(\d+)", response.text)
    return f"{max(int(height) for height in resolutions)}p" if resolutions else "Unknown"


def get_resolution(playback: PlaybackInfo) -> str:
    if playback.manifest_type == "mpd":
        return get_dash_resolution(playback.manifest_url)
    if playback.manifest_type == "m3u8":
        return get_hls_resolution(playback.manifest_url)
    return "Unknown"


def safe_name(value: Any) -> str:
    value = clean_text(value).replace("'", "")
    value = re.sub(r"[\\/:*?\"<>|]", " ", value)
    value = re.sub(r"\s+", ".", value)
    return value.strip(".") or "Unknown"


def format_filename(metadata: Metadata, resolution: str) -> str:
    parts = [safe_name(metadata.title)]
    if metadata.season is not None and metadata.episode is not None:
        parts.append(f"S{int(metadata.season):02d}E{int(metadata.episode):02d}")
    elif metadata.season is not None:
        parts.append(f"S{int(metadata.season):02d}")
    parts.extend([resolution, "M6PLUS", "WEB-DL", "AAC2.0", "H.264"])
    return ".".join(part for part in parts if part and part != "Unknown")


def build_download_command(playback: PlaybackInfo, filename: str, keys: Optional[list[str]] = None, interactive: bool = False) -> str:
    selectors = "" if interactive else "--select-video best --select-audio best --select-subtitle none "
    command = (
        f'{N_M3U8DL} "{playback.manifest_url}" '
        f'{selectors}'
        f'-mt -M format=mkv --save-dir "{SAVE_PATH}" --save-name "{filename}"'
    )
    if keys:
        # Keys should already be in the format "kid:key"
        command += " " + " ".join(f"--key {key}" for key in keys)
    if SERVICE_PROXY:
        command += f' --custom-proxy "{SERVICE_PROXY}"'
    return command


def parse_hls_attribute_list(line: str) -> dict[str, str]:
    _, _, payload = line.partition(":")
    attrs = {}
    for match in re.finditer(r'([A-Z0-9-]+)=("[^"]*"|[^,]*)', payload, re.I):
        value = match.group(2).strip()
        if value.startswith('"') and value.endswith('"'):
            value = value[1:-1]
        attrs[match.group(1).lower()] = html.unescape(value)
    return attrs


def subtitles_from_hls_manifest(manifest_url: str) -> list[dict[str, Any]]:
    text = fetch_text(manifest_url, headers={"Accept": "application/vnd.apple.mpegurl,*/*"})
    subtitles = []
    for line in text.splitlines():
        if "#EXT-X-MEDIA" not in line or "TYPE=SUBTITLES" not in line.upper():
            continue
        attrs = parse_hls_attribute_list(line)
        uri = attrs.get("uri")
        if uri:
            subtitles.append({"url": urljoin(manifest_url, uri), "manifest_line": line, **attrs})
    return subtitles


def add_manifest_query(manifest_url: str, segment_url: str) -> str:
    parsed_segment = urlparse(segment_url)
    if parsed_segment.query:
        return segment_url
    manifest_query = urlparse(manifest_url).query
    if not manifest_query:
        return segment_url
    return urlunparse(parsed_segment._replace(query=manifest_query))


def resolve_dash_segment_url(manifest_url: str, segment_path: str) -> str:
    segment_path = clean_text(segment_path)
    if re.match(r"^https?://", segment_path, re.I):
        return segment_path

    parsed_manifest = urlparse(manifest_url)
    if segment_path.startswith("/") and "/resource/" in parsed_manifest.path:
        return urlunparse((parsed_manifest.scheme, parsed_manifest.netloc, "/resource" + segment_path, "", "", ""))

    return urljoin(manifest_url, segment_path)


def dash_segment_url(manifest_url: str, segment_base_url: str, segment_path: str) -> str:
    segment_url = resolve_dash_segment_url(segment_base_url, segment_path)
    if urlparse(segment_base_url).netloc != urlparse(manifest_url).netloc:
        return segment_url
    return add_manifest_query(manifest_url, segment_url)


def expand_segment_timeline(segment_template, ns: str) -> list[int]:
    times = []
    current_time = None
    timeline = segment_template.find(ns + "SegmentTimeline")
    if timeline is None:
        return times
    for s_el in timeline.findall(ns + "S"):
        duration = int(s_el.get("d", "0"))
        repeat = int(s_el.get("r", "0"))
        if s_el.get("t") is not None:
            current_time = int(s_el.get("t"))
        elif current_time is None:
            current_time = 0
        for _ in range(repeat + 1):
            times.append(current_time)
            current_time += duration
    return times


def subtitles_from_dash_manifest(manifest_url: str) -> list[dict[str, Any]]:
    response = session.get(manifest_url, headers=DEFAULT_HEADERS, timeout=30)
    response.raise_for_status()
    segment_base_url = response.url or manifest_url
    root = ET.fromstring(response.content)
    ns = "{urn:mpeg:dash:schema:mpd:2011}"
    subtitles = []
    for adaptation in root.findall(".//" + ns + "AdaptationSet"):
        content_type = clean_text(adaptation.get("contentType")).lower()
        mime_type = clean_text(adaptation.get("mimeType")).lower()
        if content_type != "text" and "ttml" not in mime_type and "vtt" not in mime_type:
            continue
        roles = [clean_text(role.get("value")).lower() for role in adaptation.findall(ns + "Role")]
        for representation in adaptation.findall(ns + "Representation"):
            template = representation.find(ns + "SegmentTemplate")
            if template is None or not template.get("media"):
                continue
            media = template.get("media").replace("$RepresentationID$", representation.get("id", ""))
            init = template.get("initialization", "").replace("$RepresentationID$", representation.get("id", ""))
            segment_times = expand_segment_timeline(template, ns)
            urls = [
                dash_segment_url(manifest_url, segment_base_url, media.replace("$Time$", str(value)))
                for value in segment_times
            ]
            subtitles.append(
                {
                    "manifest_url": manifest_url,
                    "lang": clean_text(adaptation.get("lang")),
                    "mimeType": mime_type,
                    "roles": roles,
                    "initialization": dash_segment_url(manifest_url, segment_base_url, init) if init else "",
                    "segments": urls,
                }
            )
    return subtitles


def subtitle_url(subtitle: Any) -> str:
    if isinstance(subtitle, str):
        return subtitle
    if not isinstance(subtitle, dict):
        return ""
    return clean_text(subtitle.get("url") or subtitle.get("href") or subtitle.get("uri"))


def subtitle_preference_score(subtitle: dict[str, Any]) -> int:
    text = json.dumps(subtitle, ensure_ascii=False).lower()
    score = 0
    if "fra" in text or "fr" in text or "french" in text:
        score += 100
    if "subtitle" in text:
        score += 60
    if "caption" in text or "sdh" in text:
        score -= 50
    if subtitle.get("segments"):
        score += min(len(subtitle["segments"]), 50)
    if subtitle_url(subtitle):
        score += 20
    return score


def get_subtitle(playback: PlaybackInfo) -> Optional[dict[str, Any]]:
    subtitles = list(playback.subtitles or [])
    if not subtitles:
        if playback.manifest_type == "mpd":
            subtitles = subtitles_from_dash_manifest(playback.manifest_url)
        elif playback.manifest_type == "m3u8":
            subtitles = subtitles_from_hls_manifest(playback.manifest_url)
    subtitles = [subtitle for subtitle in subtitles if subtitle.get("segments") or subtitle_url(subtitle)]
    if not subtitles:
        return None
    subtitles.sort(key=subtitle_preference_score, reverse=True)
    return subtitles[0]


def extract_ttml_from_bytes(data: bytes) -> str:
    text = data.decode("utf-8", "ignore")
    match = re.search(r"(<tt[\s>].*?</tt>)", text, re.I | re.S)
    return match.group(1) if match else text


def fetch_dash_subtitle_text(subtitle: dict[str, Any]) -> str:
    segments = subtitle.get("segments") or []
    parts = []
    total = len(segments)
    if total:
        print(f"{bcolors.LIGHTBLUE}{icons.ICON_WAITING} Fetching {total} M6+ subtitle segments...{bcolors.ENDC}")
    for index, segment_url in enumerate(segments, 1):
        if total and (index == 1 or index == total or index % 100 == 0):
            print(f"\r{bcolors.LIGHTBLUE}{progress_bar(index, total)}{bcolors.ENDC}", end="", flush=True)
        response = session.get(segment_url, headers=DEFAULT_HEADERS, timeout=30)
        response.raise_for_status()
        parts.append(extract_ttml_from_bytes(response.content))
    if total:
        print()
    return "\n".join(parts)


def vtt_segment_urls(playlist_url: str, playlist_text: str) -> list[str]:
    urls = []
    for line in playlist_text.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            urls.append(urljoin(playlist_url, line))
    return urls


def fetch_hls_subtitle_text(url: str) -> str:
    text = fetch_text(url, headers={"Accept": "application/vnd.apple.mpegurl,text/vtt,text/plain,*/*"})
    if "#EXTM3U" not in text[:200].upper():
        return text
    segments = vtt_segment_urls(url, text)
    return "\n\n".join(fetch_text(segment, headers={"Accept": "text/vtt,text/plain,*/*"}) for segment in segments)


def vtt_time_to_srt(value: str) -> str:
    value = value.strip()
    if re.match(r"^\d{2}:\d{2}\.\d{3}$", value):
        value = f"00:{value}"
    return value.replace(".", ",")


def ttml_time_to_srt(value: str) -> str:
    value = clean_text(value)
    if not value:
        return "00:00:00,000"
    if value.endswith("s") and re.fullmatch(r"\d+(?:\.\d+)?s", value):
        seconds = float(value[:-1])
        hours = int(seconds // 3600)
        minutes = int((seconds % 3600) // 60)
        secs = int(seconds % 60)
        millis = int(round((seconds - int(seconds)) * 1000))
        return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"
    if "." in value:
        return value.replace(".", ",")
    if re.fullmatch(r"\d{2}:\d{2}:\d{2}", value):
        return value + ",000"
    return value


def strip_subtitle_tags(text: str) -> str:
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    return clean_text(text)


def parse_vtt(vtt_text: str) -> list[dict[str, str]]:
    vtt_text = vtt_text.replace("\ufeff", "").replace("\r\n", "\n").replace("\r", "\n")
    cues = []
    for block in re.split(r"\n{2,}", vtt_text.strip()):
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        if not lines or lines[0].upper().startswith(("WEBVTT", "NOTE", "STYLE", "REGION")):
            continue
        time_index = next((index for index, line in enumerate(lines) if "-->" in line), None)
        if time_index is None:
            continue
        start, end = [part.strip().split(" ", 1)[0] for part in lines[time_index].split("-->", 1)]
        text = strip_subtitle_tags(" ".join(lines[time_index + 1:]))
        if text:
            cues.append({"start": vtt_time_to_srt(start), "end": vtt_time_to_srt(end), "text": text})
    return cues


def parse_ttml(ttml_text: str) -> list[dict[str, str]]:
    ttml_text = re.sub(r"\s+xmlns(:\w+)?=\"[^\"]+\"", "", ttml_text)
    cues = []
    for match in re.finditer(r"<p\b([^>]*)>(.*?)</p>", ttml_text, re.I | re.S):
        attrs = match.group(1)
        begin = re.search(r'\bbegin=["\']([^"\']+)', attrs, re.I)
        end = re.search(r'\bend=["\']([^"\']+)', attrs, re.I)
        if not begin or not end:
            continue
        text = strip_subtitle_tags(match.group(2))
        if text:
            cues.append({"start": ttml_time_to_srt(begin.group(1)), "end": ttml_time_to_srt(end.group(1)), "text": text})
    return cues


def translate_text(text: str) -> str:
    text = clean_text(text)
    if not text:
        return ""
    response = session.get(
        TRANSLATE_URL,
        params={"client": "gtx", "sl": "fr", "tl": "en", "dt": "t", "q": text},
        headers={"Accept": "application/json,text/plain,*/*", "User-Agent": DEFAULT_HEADERS["User-Agent"]},
        timeout=30,
    )
    response.raise_for_status()
    payload = response.json()
    return clean_text("".join(part[0] for part in payload[0] if part and part[0]))


def translate_texts_batch(texts: list[str]) -> list[str]:
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


def progress_bar(current, total, width=30):
    if total <= 0:
        return "[" + "-" * width + "]"
    filled = int(width * current / total)
    return "[" + "#" * filled + "-" * (width - filled) + f"] {current}/{total}"


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
            print(f"{bcolors.WARNING}{icons.ICON_WARNING} Subtitle batch translation failed at cues {start}-{end}: {exc}{bcolors.ENDC}")
            translated_texts = [cue["text"] for cue in batch]
        for cue, text in zip(batch, translated_texts):
            translated.append({**cue, "text": text})
        if show_progress:
            print(f"\r{bcolors.LIGHTBLUE}{progress_bar(batch_index, total_batches)}{bcolors.ENDC}", end="", flush=True)
    if show_progress:
        print()
    return translated


def write_srt(cues, output_path: Path):
    with output_path.open("w", encoding="utf-8") as file:
        for index, cue in enumerate(cues, 1):
            file.write(f"{index}\n{cue['start']} --> {cue['end']}\n{cue['text']}\n\n")


def save_translated_subtitles(playback: PlaybackInfo, filename: str):
    subtitle = get_subtitle(playback)
    if not subtitle:
        print(f"{bcolors.WARNING}{icons.ICON_WARNING} No external French subtitle stream found.{bcolors.ENDC}")
        return None
    if subtitle.get("segments"):
        subtitle_text = fetch_dash_subtitle_text(subtitle)
        cues = parse_ttml(subtitle_text)
    else:
        url = subtitle_url(subtitle)
        subtitle_text = fetch_hls_subtitle_text(url)
        cues = parse_vtt(subtitle_text) if "WEBVTT" in subtitle_text[:200].upper() or ".vtt" in url.lower() else parse_ttml(subtitle_text)
    if not cues:
        print(f"{bcolors.WARNING}{icons.ICON_WARNING} No subtitle cues found in M6+ subtitle response.{bcolors.ENDC}")
        return None
    output_path = SAVE_PATH / f"{filename}.en.srt"
    print(f"{bcolors.LIGHTBLUE}{icons.ICON_WAITING} Translating French subtitles to English SRT...{bcolors.ENDC}")
    write_srt(translate_cues(cues), output_path)
    print(f"{bcolors.OKGREEN}{icons.ICON_SUCCESS} English subtitles saved: {bcolors.ENDC}{output_path}")
    return output_path


def maybe_save_translated_subtitles(playback: PlaybackInfo, filename: str):
    try:
        user_input = input("Do you wish to save translated English subtitles? Y or N: ").strip().lower()
    except EOFError:
        user_input = "n"
    if user_input != "y":
        return None
    try:
        return save_translated_subtitles(playback, filename)
    except Exception as exc:
        print(f"{icons.ICON_WARNING} {bcolors.WARNING}Could not save translated subtitles: {exc}{bcolors.ENDC}")
        return None


def print_playback_details(playback: PlaybackInfo, keys: list[str], command: str):
    drm_type = "PlayReady" if playback.is_playready else "Widevine"
    label = "DASH Manifest URL" if playback.manifest_type == "mpd" else "HLS Manifest URL"
    print(f"{bcolors.LIGHTBLUE}DRM Type: {bcolors.ENDC}{drm_type}")
    print(f"{bcolors.LIGHTBLUE}{label}: {bcolors.ENDC}{playback.manifest_url}")
    if keys:
        for key in keys:
            print(f"{bcolors.OKGREEN}KEYS: {bcolors.ENDC}--key {key}")
    elif playback.manifest_type == "mpd":
        print(f"{icons.ICON_WARNING} {bcolors.WARNING}No keys available - content may be encrypted{bcolors.ENDC}")
    if playback.layout_asset:
        drm_type = ((playback.layout_asset.get("drm") or {}).get("type") or "unknown")
        height = asset_height(playback.layout_asset)
        print(f"{bcolors.LIGHTBLUE}Selected asset: {bcolors.ENDC}{drm_type} {height}p")
    if playback.license_url:
        print(f"{bcolors.RED}License URL: {bcolors.ENDC}{playback.license_url}")
    if playback.pssh:
        print(f"{bcolors.LIGHTBLUE}PSSH: {bcolors.ENDC}{playback.pssh}")
    print(f"{bcolors.YELLOW}DOWNLOAD COMMAND: {bcolors.ENDC}")
    print(mask_proxy_command(command))


def print_info_details(playback: PlaybackInfo, keys: list[str], streams: list[dict[str, str]], metadata: Metadata, filename: str):
    drm_type = "PlayReady" if playback.is_playready else "Widevine"
    label = "DASH Manifest URL" if playback.manifest_type == "mpd" else "HLS Manifest URL"
    print(f"{bcolors.LIGHTBLUE}DRM Type: {bcolors.ENDC}{drm_type}")
    print(f"{bcolors.LIGHTBLUE}{label}: {bcolors.ENDC}{playback.manifest_url}")
    if keys:
        for key in keys:
            print(f"{bcolors.OKGREEN}KEYS: {bcolors.ENDC}--key {key}")
    elif playback.manifest_type == "mpd":
        print(f"{icons.ICON_WARNING} {bcolors.WARNING}No keys available - content may be encrypted{bcolors.ENDC}")
    if playback.layout_asset:
        drm_type = ((playback.layout_asset.get("drm") or {}).get("type") or "unknown")
        height = asset_height(playback.layout_asset)
        print(f"{bcolors.LIGHTBLUE}Selected asset: {bcolors.ENDC}{drm_type} {height}p")
    print_streams(streams)
    print_episode_metadata(metadata, translate_description=True)
    print(f"\n{bcolors.YELLOW}Suggested filename: {bcolors.ENDC}{filename}.mkv")


def maybe_download(command: str, auto_download: bool = False):
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


def resolve_video_details(video_url: str, interactive: bool = False) -> dict[str, Any]:
    video_id = extract_video_id(video_url)
    metadata = search_metadata(video_url, video_id)
    playback = get_playback_info(video_url, metadata)
    keys = []
    if playback.manifest_type == "mpd":
        if not playback.psshs:
            playback.psshs = get_pssh_values_from_manifest(playback.manifest_url, is_playready=playback.is_playready)
            playback.pssh = playback.psshs[0] if playback.psshs else None
        if playback.license_url and playback.psshs:
            try:
                keys = get_keys(playback)
            except Exception as exc:
                print(f"{icons.ICON_WARNING} {bcolors.WARNING}Could not retrieve keys: {exc}{bcolors.ENDC}")
                import traceback
                traceback.print_exc()
    manifest_text = fetch_manifest(playback.manifest_url)
    streams, manifest_label = parse_manifest_streams(manifest_text)
    resolution = f"{max_height_from_streams(streams, default=str(asset_height(playback.layout_asset or {}) or 1080))}p"
    filename = format_filename(metadata, resolution)
    command = build_download_command(playback, filename, keys, interactive=interactive)
    return {
        "metadata": metadata,
        "playback": playback,
        "keys": keys,
        "streams": streams,
        "manifest_label": manifest_label,
        "resolution": resolution,
        "filename": filename,
        "command": command,
    }


def resolve_video_details_with_spinner(video_url: str, interactive: bool = False) -> dict[str, Any]:
    spinner = Spinner()
    spinner.start()
    try:
        details = resolve_video_details(video_url, interactive=interactive)
    except Exception:
        spinner.stop()
        raise
    spinner.stop()
    return details


def print_resolved_title(metadata: Metadata):
    episode_str = f"S{metadata.season:02d}E{metadata.episode:02d}" if metadata.season and metadata.episode else ""
    if metadata.title != "Unknown" or episode_str or metadata.episode_title:
        print(f"{icons.ICON_SUCCESS} {bcolors.OKGREEN}{metadata.title}{bcolors.ENDC} {episode_str} - {metadata.episode_title or ''}".rstrip())


def process_video(video_url: str, auto_download: bool = False, info: bool = False, interactive: bool = False):
    print(f"{icons.ICON_INFO} {bcolors.LIGHTBLUE}Processing: {bcolors.ENDC}{video_url}")
    details = resolve_video_details_with_spinner(video_url, interactive=interactive)
    metadata = details["metadata"]
    playback = details["playback"]
    keys = details["keys"]
    filename = details["filename"]
    command = details["command"]
    print_resolved_title(metadata)
    if keys:
        print(f"{icons.ICON_SUCCESS} {bcolors.OKGREEN}Retrieved {len(keys)} keys{bcolors.ENDC}")
    elif playback.manifest_type == "mpd":
        print(f"{icons.ICON_WARNING} {bcolors.WARNING}No keys retrieved{bcolors.ENDC}")
    print_playback_details(playback, keys, command)
    print(f"{bcolors.YELLOW}Suggested filename: {bcolors.ENDC}{filename}.mkv")
    if info:
        return
    maybe_save_translated_subtitles(playback, filename)
    maybe_download(command, auto_download=auto_download)


def info(video_url: str):
    playable_url = resolve_single_playable_url(video_url)
    if not playable_url:
        raise ValueError(f"Info mode requires an {SERVICE_LABEL} episode/video URL, or a page with one playable video.")
    print(f"{icons.ICON_INFO} {bcolors.LIGHTBLUE}Processing: {bcolors.ENDC}{playable_url}")
    details = resolve_video_details_with_spinner(playable_url)
    print_resolved_title(details["metadata"])
    print_info_details(
        details["playback"],
        details["keys"],
        details["streams"],
        details["metadata"],
        details["filename"],
    )


def download_selected_episodes(series_url: str, selector: str):
    print(f"{icons.ICON_WAITING} {bcolors.LIGHTBLUE}Retrieving series information.....{bcolors.ENDC}")
    episode_items = select_episode_items(series_url, selector)
    print_download_queue(episode_items)
    episode_word = "episode" if len(episode_items) == 1 else "episodes"
    this_or_these = "this" if len(episode_items) == 1 else "these"
    user_input = input(f"Do you wish to download {this_or_these} {len(episode_items)} {episode_word}? Y or N: ").strip().lower()
    if user_input != "y":
        print(f"{icons.ICON_FAILURE} {bcolors.RED}Download cancelled{bcolors.ENDC}")
        return
    for index, item in enumerate(episode_items, start=1):
        _, title = episode_tree_label(item)
        print(f"\n{icons.ICON_INFO} {bcolors.LIGHTBLUE}Downloading {index}/{len(episode_items)}: {title}{bcolors.ENDC}")
        process_video(item.url, auto_download=True)


def export_episode_urls(episode_items):
    if not episode_items:
        print(f"{icons.ICON_FAILURE} {bcolors.FAIL}No {SERVICE_LABEL} episodes found to export.{bcolors.ENDC}")
        return

    export_dir = Path(__file__).resolve().parents[2] / "export"
    export_dir.mkdir(parents=True, exist_ok=True)
    show_title = episode_items[0].title or SERVICE_LABEL
    output_path = export_dir / f"m6_{safe_name(show_title)}_episodes.txt"
    output_path.write_text("\n".join(item.url for item in episode_items) + "\n", encoding="utf-8")
    print(f"{icons.ICON_SUCCESS} {bcolors.OKGREEN}Exported list: {output_path}{bcolors.ENDC}")


def main(video_url, downloads_path, wvd_device_path, prd_device_path="", m6_credentials=None, m6_config=None, mode="auto", export_list=False, download_selector=None):
    """Eurovine entry point for M6+ (Widevine/PlayReady)."""
    try:
        if not video_url:
            raise ValueError(f"No {SERVICE_LABEL} URL provided.")
        if not downloads_path or not wvd_device_path:
            raise ValueError(f"Eurovine config requires downloads_path and wvd_device_path for {SERVICE_LABEL}.")

        configure_service(downloads_path, wvd_device_path, prd_device_path, m6_credentials, m6_config)
        video_url = video_url.strip()
        print(f"{icons.ICON_INFO} {bcolors.LIGHTBLUE}{SERVICE_LABEL} URL: {bcolors.ENDC}{video_url}")

        if mode == "list":
            if is_episode_url(video_url):
                episode_items = [collect_episode_item(video_url)]
            else:
                print(f"{icons.ICON_WAITING} {bcolors.LIGHTBLUE}Retrieving series information.....{bcolors.ENDC}")
                episode_items = collect_episode_items(video_url, show_progress=False)
            list_episode_items(episode_items)
            if export_list:
                export_episode_urls(episode_items)
            return

        if mode == "info":
            info(video_url)
            return

        if mode == "download":
            if is_episode_url(video_url):
                print(f"{icons.ICON_FAILURE} {bcolors.FAIL}Download selector mode requires an {SERVICE_LABEL} series URL, not an episode URL.{bcolors.ENDC}")
                return
            download_selected_episodes(video_url, download_selector)
            return

        if export_list:
            if is_episode_url(video_url):
                print(f"{icons.ICON_FAILURE} {bcolors.FAIL}Export mode requires an {SERVICE_LABEL} series URL, not an episode URL.{bcolors.ENDC}")
                return
            print(f"{icons.ICON_WAITING} {bcolors.LIGHTBLUE}Retrieving series information.....{bcolors.ENDC}")
            export_episode_urls(collect_episode_items(video_url, show_progress=False))
            return

        playable_url = resolve_single_playable_url(video_url)
        if playable_url:
            if canonical_url(video_url) != playable_url:
                print(f"{icons.ICON_INFO} {bcolors.LIGHTBLUE}Resolved playable URL: {bcolors.ENDC}{playable_url}")
            process_video(playable_url, interactive=(mode == "interactive"))
            return
        print(f"{icons.ICON_WARNING} {bcolors.WARNING}Series URLs require a flag. Use --list/-l to list episodes, --export/-x to export episode URLs, or --download/-d SELECTOR to download selected episodes.{bcolors.ENDC}")
    except Exception as exc:
        print(f"{icons.ICON_FAILURE} {bcolors.FAIL}Error: {exc}{bcolors.ENDC}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    print("Run M6+ through eurovine.py so it can use the shared Eurovine configuration.")











