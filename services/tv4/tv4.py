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
import requests
import urllib3
import yaml
from beaupy.spinners import Spinner
from pywidevine.cdm import Cdm
from pywidevine.device import Device
from pywidevine.pssh import PSSH
from colors import bcolors
from quality_utils import apply_quality_to_filename, video_selector
from services.proxy import current_proxy_url, mask_proxy_command


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

SERVICE_NAME = "tv4"
BASE_URL = "https://www.tv4play.se"
REFRESH_URL = "https://avod-auth-alb.a2d.tv/oauth/refresh"
AUTH_TOKEN_URL = "https://auth.tv4.a2d.tv/v2/auth/token"
CONTENT_URL = "https://client-gateway.tv4.a2d.tv/graphql"
PLAYBACK_URL = (
    "https://playback2.a2d.tv/play/{id}"
    "?service=tv4play&device=browser&protocol=hls%2Cdash&drm=widevine"
    "&browser=GoogleChrome&capabilities=live-drm-adstitch-2%2Cyospace3"
)
MEDIA_QUERY = """
query TV4MediaDetails($id: ID!) {
  media(id: $id) {
    __typename
    ... on Episode {
      id
      slug
      title
      extendedTitle
      episodeNumber
      seasonId
      synopsis { brief medium long }
      playableFrom { isoString readableDate }
      series { id title slug }
    }
    ... on Movie {
      id
      slug
      title
      productionYear
      isDrmProtected
    }
  }
}
"""
SERIES_QUERY = """
query TV4SeriesBasics($id: ID!) {
  media(id: $id) {
    __typename
    ... on Series {
      id
      slug
      title
      synopsis { brief medium long }
      allSeasonLinks { title seasonId }
      suggestedSeason { id title }
      numberOfAvailableSeasons
    }
  }
}
"""
SEASON_EPISODES_QUERY = """
query TV4SeasonEpisodes($seasonId: ID!, $input: SeasonEpisodesInput!) {
  season(id: $seasonId) {
    id
    title
    numberOfEpisodes
    episodes(input: $input) {
      initialSortOrder
      pageInfo { totalCount hasNextPage nextPageOffset }
      items {
        __typename
        id
        slug
        title
        extendedTitle
        episodeNumber
        seasonId
        synopsis { brief medium long }
        duration { readableShort seconds }
        playableFrom { isoString readableDate }
        playableUntil { isoString readableDate }
        images { main16x9 { sourceEncoded isFallback } }
      }
    }
  }
}
"""
TRANSLATE_URL = "https://translate.googleapis.com/translate_a/single"
TRANSLATE_BATCH_MARKER = "@@TV4BREAK@@"
TRANSLATE_BATCH_SIZE = 40
TRANSLATE_BATCH_CHAR_LIMIT = 4500
SERVICE_DIR = Path(__file__).resolve().parent
EUROVINE_DIR = SERVICE_DIR.parents[1]
EUROVINE_TEMP_DIR = EUROVINE_DIR / "temp"
CONFIG_PATH = EUROVINE_DIR / "config.yaml"
N_M3U8DL = "N_m3u8DL-RE"
config = {}
SAVE_PATH = None
WVD_PATH = None
SERVICE_PROXY = None


session = requests.Session()


def configure_service(downloads_path, wvd_device_path, cookies_path=None, tv4_credentials=None, tv4_config=None):
    global config, SAVE_PATH, WVD_PATH, SERVICE_PROXY
    SAVE_PATH = Path(downloads_path)
    WVD_PATH = wvd_device_path

    service_config = tv4_config if isinstance(tv4_config, dict) else {}
    if isinstance(tv4_config, str):
        service_config = tv4_config
    config = {
        SERVICE_NAME: service_config,
        "credentials": {SERVICE_NAME: tv4_credentials} if tv4_credentials else {},
        "cookies_path": cookies_path,
    }

    SERVICE_PROXY = current_proxy_url()
    session.proxies.clear()
    session.verify = True
    if SERVICE_PROXY:
        session.proxies.update({"http": SERVICE_PROXY, "https": SERVICE_PROXY})
        session.verify = False


DEFAULT_HEADERS = {
    "Accept": "text/html,application/json,text/plain,*/*",
    "Accept-Language": "sv-SE,sv;q=0.9,en-US;q=0.8,en;q=0.7",
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
    season_id: Optional[str] = None


@dataclass
class PlaybackInfo:
    manifest_url: str
    manifest_type: str
    license_url: Optional[str] = None
    license_token: Optional[str] = None
    pssh: Optional[str] = None
    metadata: Metadata = field(default_factory=Metadata)
    is_encrypted: bool = False
    subtitles: list = field(default_factory=list)
    playback_json: dict = field(default_factory=dict)
    streams: list = field(default_factory=list)


def clean_text(value):
    return re.sub(r"\s+", " ", html.unescape(str(value or ""))).strip()


def date_value(value):
    value = clean_text(value)
    if not value:
        return "Unknown"
    normalised = value.replace("Z", "+00:00")
    try:
        from datetime import datetime
        return datetime.fromisoformat(normalised).strftime("%Y-%m-%d")
    except ValueError:
        return value.split("T", 1)[0]


def synopsis_text(item):
    synopsis = item.get("synopsis") or {}
    return (
        clean_text(synopsis.get("medium"))
        or clean_text(synopsis.get("long"))
        or clean_text(synopsis.get("brief"))
        or "No Description"
    )


def episode_season_number(item, season_map=None):
    season_id = clean_text(item.get("seasonId"))
    if season_map and season_id in season_map:
        return season_map[season_id]

    title = clean_text(item.get("extendedTitle"))
    match = re.search(r"Säsong\s*0*(\d+)", title, re.IGNORECASE)
    return match.group(1) if match else "1"


def episode_number(item):
    value = item.get("episodeNumber")
    if value not in (None, ""):
        return str(value)

    for key in ("title", "extendedTitle", "slug"):
        match = re.search(r"(?:Avsnitt|Episode|avsnitt)[\s-]*0*(\d+)", clean_text(item.get(key)), re.IGNORECASE)
        if match:
            return match.group(1)
    return "1"


def episode_title(item):
    return clean_text(item.get("title") or item.get("extendedTitle")) or f"Avsnitt {episode_number(item)}"


def video_url(item):
    item_id = clean_text(item.get("id"))
    slug = clean_text(item.get("slug"))
    if item_id and slug:
        return f"{BASE_URL}/video/{item_id}/{slug}"
    if item_id:
        return f"{BASE_URL}/video/{item_id}"
    return ""


def playback_manifest(playback, wanted):
    if not isinstance(playback, dict):
        return ""

    for key in (wanted, wanted.upper(), f"{wanted}Url", f"{wanted.upper()}Url"):
        value = playback.get(key)
        if isinstance(value, str) and value.startswith("http"):
            return clean_text(value)

    for container_key in ("sources", "manifests", "playbackItems", "items"):
        values = playback.get(container_key)
        if not isinstance(values, list):
            continue
        for item in values:
            if not isinstance(item, dict):
                continue
            haystack = " ".join(clean_text(item.get(k)).lower() for k in ("type", "format", "protocol", "mimeType"))
            if wanted in haystack:
                for url_key in ("url", "src", "href", "manifestUrl"):
                    url = clean_text(item.get(url_key))
                    if url.startswith("http"):
                        return url
    return ""


def canonical_url(value):
    value = clean_text(value)
    if re.match(r"^https?://", value, re.IGNORECASE):
        return value
    if value.startswith("/"):
        return urljoin(BASE_URL, value)
    return urljoin(BASE_URL, f"/{value.strip('/')}")


def extract_video_id(video_url):
    match = re.search(r"/(?:video|program)/([^/?#]+)", urlparse(canonical_url(video_url)).path)
    if match:
        return match.group(1)
    raise ValueError("Could not extract TV4 video ID from URL.")


def is_episode_url(video_url):
    return "/video/" in urlparse(canonical_url(video_url)).path


def is_series_url(video_url):
    if "/program/" not in urlparse(canonical_url(video_url)).path:
        return False
    try:
        return fetch_media(extract_video_id(video_url)).get("__typename") == "Series"
    except Exception:
        return False


def int_value(value):
    match = re.search(r"\d+", clean_text(value))
    return int(match.group(0)) if match else None


def post_graphql(query, variables, operation_name):
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Origin": BASE_URL,
        "Referer": f"{BASE_URL}/",
        "client-name": "tv4-web",
        "client-version": "5.5.0",
        "User-Agent": DEFAULT_HEADERS["User-Agent"],
    }
    response = session.post(
        CONTENT_URL,
        headers=headers,
        json={"operationName": operation_name, "query": query, "variables": variables},
        timeout=30,
    )
    response.raise_for_status()
    data = response.json()
    if data.get("errors"):
        message = "; ".join(clean_text(error.get("message")) for error in data["errors"])
        raise RuntimeError(f"TV4 GraphQL error: {message}")
    return data.get("data") or {}


def fetch_media(video_id):
    media = (post_graphql(MEDIA_QUERY, {"id": video_id}, "TV4MediaDetails").get("media") or {})
    if not media:
        raise RuntimeError("The TV4 URL did not resolve to media.")
    return media


def fetch_series(series_id):
    media = (post_graphql(SERIES_QUERY, {"id": series_id}, "TV4SeriesBasics").get("media") or {})
    if media.get("__typename") != "Series":
        raise RuntimeError("The TV4 URL did not resolve to a series.")
    return media


def fetch_season_episodes(season_id):
    episodes = []
    offset = 0
    while True:
        variables = {"seasonId": season_id, "input": {"limit": 100, "offset": offset}}
        data = post_graphql(SEASON_EPISODES_QUERY, variables, "TV4SeasonEpisodes")
        season = data.get("season") or {}
        page = ((season.get("episodes") or {}).get("pageInfo") or {})
        episodes.extend((season.get("episodes") or {}).get("items") or [])
        if not page.get("hasNextPage"):
            break
        next_offset = page.get("nextPageOffset")
        if next_offset in (None, offset):
            break
        offset = next_offset
    return season, episodes


def season_number_from_title(value):
    match = re.search(r"\d+", clean_text(value))
    return int(match.group(0)) if match else 1


def episode_item_sort_key(item, season_map=None):
    return (
        int_value(episode_season_number(item, season_map=season_map)) or 9999,
        int_value(episode_number(item)) or 9999,
        clean_text(episode_title(item)).lower(),
        clean_text(item.get("id")),
    )


def collect_episode_items(series_url):
    series = fetch_series(extract_video_id(series_url))
    show = clean_text(series.get("title")) or "Unknown Show"
    season_map = {
        clean_text(link.get("seasonId")): season_number_from_title(link.get("title"))
        for link in series.get("allSeasonLinks") or []
        if clean_text(link.get("seasonId"))
    }

    episodes = []
    seen = set()
    for season_link in series.get("allSeasonLinks") or []:
        season_id = clean_text(season_link.get("seasonId"))
        if not season_id:
            continue
        _, season_episodes = fetch_season_episodes(season_id)
        for item in season_episodes:
            item_id = clean_text(item.get("id"))
            if item_id and item_id not in seen:
                seen.add(item_id)
                episodes.append(item)

    episodes = sorted(episodes, key=lambda item: episode_item_sort_key(item, season_map=season_map))
    if not episodes:
        raise RuntimeError("No TV4 episodes found for this URL.")

    return [
        {
            "show_title": show,
            "season": int_value(episode_season_number(item, season_map=season_map)) or 1,
            "episode": int_value(episode_number(item)) or 1,
            "title": clean_text(episode_title(item)) or f"Avsnitt {episode_number(item)}",
            "url": video_url(item),
            "video_id": clean_text(item.get("id")),
        }
        for item in episodes
        if video_url(item)
    ]


def search_metadata(video_url, video_id):
    item = fetch_media(video_id)
    if item.get("__typename") == "Movie":
        return Metadata(
            title=clean_text(item.get("title")) or "Unknown",
            episode_title=None,
            description=translate_metadata_description(item),
            video_id=clean_text(item.get("id")) or video_id,
        )

    series = item.get("series") or {}
    description = translate_metadata_description(item)
    return Metadata(
        title=clean_text(series.get("title")) or "Unknown",
        season=int_value(episode_season_number(item)),
        episode=int_value(episode_number(item)),
        episode_title=clean_text(episode_title(item)) or None,
        aired_date=date_value((item.get("playableFrom") or {}).get("isoString")),
        description=description,
        video_id=clean_text(item.get("id")) or video_id,
        season_id=clean_text(item.get("seasonId")) or None,
    )


def translate_metadata_description(item):
    description = synopsis_text(item)
    if description == "No Description":
        return description
    try:
        return translate_text(description)
    except Exception:
        return description


def config_section():
    value = config.get(SERVICE_NAME) or {}
    return value if isinstance(value, dict) else {}


def read_config():
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as file:
            return yaml.safe_load(file) or {}
    except OSError:
        return {}


def write_config(data):
    with open(CONFIG_PATH, "w", encoding="utf-8") as file:
        yaml.safe_dump(data, file, sort_keys=False, allow_unicode=True)


def config_cookie_path():
    tv4_config = config_section()
    path = clean_text(tv4_config.get("cookies") or tv4_config.get("cookies_path") or config.get("cookies_path"))
    return Path(path) if path else None


def config_credentials():
    credentials = (config.get("credentials") or {}).get(SERVICE_NAME)
    service_config = config.get(SERVICE_NAME)
    if not credentials and isinstance(service_config, str):
        credentials = service_config
    if isinstance(credentials, str) and ":" in credentials:
        username, password = credentials.split(":", 1)
        return clean_text(username), clean_text(password)
    if isinstance(credentials, dict):
        username = credentials.get("username") or credentials.get("email") or credentials.get("login")
        password = credentials.get("password")
        if username and password:
            return clean_text(username), clean_text(password)
    if isinstance(service_config, dict):
        username = service_config.get("username") or service_config.get("email") or service_config.get("login")
        password = service_config.get("password")
        if username and password:
            return clean_text(username), clean_text(password)
    return "", ""


def jwt_exp(token):
    token = clean_text(token)
    parts = token.split(".")
    if len(parts) < 2:
        return None
    try:
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        return int(json.loads(base64.urlsafe_b64decode(payload)).get("exp") or 0)
    except Exception:
        return None


def save_tv4_cache(token_data):
    global config

    access_token = clean_text((token_data or {}).get("access_token"))
    if not access_token:
        return

    cache_data = {"access_token": access_token}
    expiry = jwt_exp(access_token)
    if expiry:
        cache_data["expiry"] = expiry

    file_config = read_config()
    tv4_config = file_config.get(SERVICE_NAME)
    if not isinstance(tv4_config, dict):
        tv4_config = {"value": tv4_config} if tv4_config else {}
        file_config[SERVICE_NAME] = tv4_config
    tv4_config["cache"] = cache_data
    write_config(file_config)

    memory_config = config.get(SERVICE_NAME)
    if not isinstance(memory_config, dict):
        memory_config = {"value": memory_config} if memory_config else {}
        config[SERVICE_NAME] = memory_config
    memory_config["cache"] = cache_data


def cached_token():
    tv4_config = config_section()
    candidates = [
        tv4_config.get("access_token"),
        tv4_config.get("token"),
        tv4_config.get("bearer_token"),
        (tv4_config.get("cache") or {}).get("access_token"),
        (tv4_config.get("cache") or {}).get("token"),
    ]
    for token in candidates:
        token = clean_text(token)
        if not token:
            continue
        exp = jwt_exp(token)
        if not exp or exp > int(time.time()) + 60:
            return token
    return ""


def refresh_token_value():
    tv4_config = config_section()
    cache = tv4_config.get("cache") or {}
    return clean_text(
        tv4_config.get("refresh_token")
        or tv4_config.get("tv4_refresh_token")
        or cache.get("refresh_token")
        or cache.get("refresh")
        or cookie_value("tv4-refresh-token")
    )


def cookie_value(name):
    cookie_path = config_cookie_path()
    if not cookie_path or not cookie_path.exists():
        return ""

    try:
        for line in cookie_path.read_text(encoding="utf-8", errors="replace").splitlines():
            if not line or line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) >= 7 and parts[5] == name:
                try:
                    if int(parts[4]) and int(parts[4]) < int(time.time()):
                        continue
                except ValueError:
                    pass
                return clean_text(parts[6])
    except OSError:
        return ""
    return ""


def refresh_access_token(refresh):
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Client-Name": "tv4-web",
        "client-name": "tv4-web",
        "client-version": "4.0.0",
        "Origin": BASE_URL,
        "Referer": f"{BASE_URL}/",
        "User-Agent": DEFAULT_HEADERS["User-Agent"],
    }
    legacy_payload = {
        "refresh_token": refresh,
        "client_id": "tv4-web",
        "profile_id": "default",
    }
    current_payload = {
        "grant_type": "refresh_token",
        "refresh_token": refresh,
        "profile_id": "default",
    }
    errors = []
    for url, payload in ((REFRESH_URL, legacy_payload), (AUTH_TOKEN_URL, current_payload)):
        try:
            response = session.post(url, headers=headers, json=payload, timeout=30)
        except requests.RequestException as exc:
            errors.append(f"{url}: {exc}")
            continue
        if response.ok:
            data = response.json()
            token = clean_text(data.get("access_token"))
            if token:
                return data
            errors.append(f"{url}: response did not include access_token: {data}")
            continue
        errors.append(f"{url}: HTTP {response.status_code}: {response.text[:500]}")
    raise RuntimeError("TV4 refresh failed. " + " | ".join(errors))


def get_access_token(verbose=True):
    token = cached_token()
    if token:
        if verbose:
            print(f"{icons.ICON_SUCCESS} {bcolors.OKGREEN}Using cached TV4 bearer token{bcolors.ENDC}")
        return token

    refresh = refresh_token_value()
    if refresh:
        try:
            data = refresh_access_token(refresh)
            token = clean_text(data.get("access_token"))
            try:
                save_tv4_cache(data)
            except Exception as cache_exc:
                if verbose:
                    print(f"{icons.ICON_WARNING} {bcolors.WARNING}TV4 token cache could not be saved: {cache_exc}{bcolors.ENDC}")
            if verbose:
                print(f"{icons.ICON_SUCCESS} {bcolors.OKGREEN}Refreshed TV4 bearer token{bcolors.ENDC}")
            return token
        except Exception as exc:
            if verbose:
                print(f"{icons.ICON_WARNING} {bcolors.WARNING}TV4 refresh token failed: {exc}{bcolors.ENDC}")

    username, password = config_credentials()
    if username and password:
        raise RuntimeError(
            "TV4 web playback uses the tv4-refresh-token browser cookie for this flow. "
            "Export fresh TV4 cookies to cookies_path, or add tv4.refresh_token / tv4.access_token to the Eurovine config."
        )

    raise RuntimeError("No TV4 access token or TV4 browser cookie found in the Eurovine config.")


def fetch_json(url, headers=None):
    request_headers = dict(DEFAULT_HEADERS)
    request_headers["Accept"] = "application/json,*/*"
    if headers:
        request_headers.update(headers)
    response = session.get(url, headers=request_headers, timeout=30)
    response.raise_for_status()
    return response.json()


def find_urls(value, wanted):
    found = []
    if isinstance(value, dict):
        keys = " ".join(str(key).lower() for key in value)
        values = " ".join(clean_text(value.get(key)).lower() for key in value if not isinstance(value.get(key), (dict, list)))
        haystack = f"{keys} {values}"
        for key, item in value.items():
            if isinstance(item, str) and item.startswith("http") and wanted in f"{key} {item}".lower():
                found.append(item)
            else:
                found.extend(find_urls(item, wanted))
        if wanted in haystack:
            for key in ("url", "src", "href", "manifestUrl", "licenseUrl"):
                item = clean_text(value.get(key))
                if item.startswith("http"):
                    found.append(item)
    elif isinstance(value, list):
        for item in value:
            found.extend(find_urls(item, wanted))
    return found


def first_url(value, wanted):
    for url in find_urls(value, wanted):
        return clean_text(url)
    return ""


def playback_license(playback):
    playback_item = playback.get("playbackItem") or {}
    license_info = playback_item.get("license") or {}
    return (
        clean_text(license_info.get("castlabsServer"))
        or clean_text(playback.get("licenseUrl"))
        or clean_text(playback.get("license_url"))
        or first_url(playback, "license")
        or first_url(playback, "widevine")
    )


def playback_license_token(playback):
    playback_item = playback.get("playbackItem") or {}
    license_info = playback_item.get("license") or {}
    return clean_text(
        license_info.get("castlabsToken")
        or license_info.get("token")
        or playback.get("licenseToken")
        or playback.get("castlabsToken")
    )


def get_playback_info(video_url, metadata, verbose=True):
    access_token = get_access_token(verbose=verbose)
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {access_token}",
        "Origin": BASE_URL,
        "Referer": f"{BASE_URL}/",
    }
    playback = fetch_json(PLAYBACK_URL.format(id=metadata.video_id), headers=headers)
    playback_item = playback.get("playbackItem") or {}
    item_manifest = clean_text(playback_item.get("manifestUrl"))
    dash_url = playback_manifest(playback, "dash") or first_url(playback, "mpd")
    hls_url = playback_manifest(playback, "hls") or first_url(playback, "m3u8")
    if item_manifest.endswith(".mpd") or ".mpd" in item_manifest:
        dash_url = dash_url or item_manifest
    elif item_manifest:
        hls_url = hls_url or item_manifest
    license_url = playback_license(playback)
    license_token = playback_license_token(playback)
    is_encrypted = bool(license_url or ((playback.get("metadata") or {}).get("isDrmProtected")))

    if is_encrypted and hls_url and not dash_url:
        dash_url = hls_url.split("?", 1)[0].replace(".m3u8", ".mpd")

    if dash_url and is_encrypted:
        manifest_url = dash_url
        manifest_type = "mpd"
    elif dash_url:
        manifest_url = dash_url
        manifest_type = "mpd"
    elif hls_url:
        manifest_url = hls_url
        manifest_type = "m3u8"
    else:
        raise ValueError(f"No TV4 DASH or HLS manifest URL found in playback response: {json.dumps(playback)[:1000]}")

    return PlaybackInfo(
        manifest_url=manifest_url,
        manifest_type=manifest_type,
        license_url=license_url,
        license_token=license_token,
        metadata=metadata,
        is_encrypted=is_encrypted,
        subtitles=find_subtitles_in_playback(playback),
        playback_json=playback,
    )


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

    raise ValueError("PSSH not found in the DASH manifest.")


def get_pssh_from_hls(manifest_url):
    response = session.get(manifest_url, headers=DEFAULT_HEADERS, timeout=30)
    response.raise_for_status()
    match = re.search(r'URI="data:text/plain;base64,([^"]+)"', response.text)
    if match:
        pssh_data = match.group(1).strip()
        base64.b64decode(pssh_data)
        return pssh_data
    return None


def build_license_headers(metadata):
    headers = {
        "Accept": "*/*",
        "Content-Type": "application/octet-stream",
        "Origin": BASE_URL,
        "Referer": f"{BASE_URL}/",
        "User-Agent": DEFAULT_HEADERS["User-Agent"],
    }
    token = getattr(metadata, "license_token", None)
    if token:
        headers["x-dt-auth-token"] = token
    return headers


def post_license_challenge(license_url, challenge, metadata):
    response = session.post(license_url, headers=build_license_headers(metadata), data=challenge, timeout=30)
    try:
        response.raise_for_status()
    except requests.exceptions.HTTPError as exc:
        raise RuntimeError(f"TV4 license request failed: {exc}. Response: {response.text[:300]}") from exc
    return response.content


def get_keys(pssh, license_url, metadata):
    try:
        pssh = PSSH(pssh)
    except (binascii.Error, ValueError) as exc:
        raise ValueError(f"Could not parse PSSH: {exc}") from exc

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


def fetch_manifest_text(manifest_url):
    try:
        response = session.get(manifest_url, headers=DEFAULT_HEADERS, timeout=30)
        response.raise_for_status()
        response.encoding = response.encoding or "utf-8"
        return response.text
    except requests.RequestException as exc:
        raise ConnectionError(f"Failed to fetch TV4 manifest: {exc}") from exc


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
        url = subtitle_url(subtitle)
        if not url:
            continue
        lang = "-"
        if isinstance(subtitle, dict):
            lang = clean_text(subtitle.get("language") or subtitle.get("lang") or subtitle.get("locale")) or "-"
        codec = urlparse(url).path.rsplit(".", 1)[-1].lower() if "." in urlparse(url).path else "-"
        key = (codec, lang)
        if key in seen:
            continue
        seen.add(key)
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

        start, end = lines[time_index].split("-->", 1)
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
        params={"client": "gtx", "sl": "sv", "tl": "en", "dt": "t", "q": text},
        headers={"Accept": "application/json,text/plain,*/*", "User-Agent": DEFAULT_HEADERS["User-Agent"]},
        timeout=30,
    )
    response.raise_for_status()
    data = response.json()
    return clean_text("".join(part[0] for part in data[0] if part and part[0]))


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


def progress_bar(current, total, width=28):
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


def write_srt(cues, output_path):
    with open(output_path, "w", encoding="utf-8") as file:
        for index, cue in enumerate(cues, 1):
            file.write(f"{index}\n")
            file.write(f"{cue['start']} --> {cue['end']}\n")
            file.write(f"{cue['text']}\n\n")


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

        start, end = lines[time_index].split("-->", 1)
        end = end.strip().split(" ", 1)[0]
        text = clean_text(" ".join(lines[time_index + 1:]))
        if text:
            cues.append({"start": start.strip(), "end": end, "text": text})

    return cues


def subtitle_url(subtitle):
    if isinstance(subtitle, str):
        return subtitle
    if not isinstance(subtitle, dict):
        return ""
    for key in ("url", "href", "link", "location", "src", "uri"):
        value = clean_text(subtitle.get(key))
        if value:
            return value
    return ""


def subtitle_preference_score(subtitle):
    text = json.dumps(subtitle, ensure_ascii=False).lower() if isinstance(subtitle, dict) else clean_text(subtitle).lower()
    score = 0
    if "sv" in text or "swe" in text or "swedish" in text or "svenska" in text:
        score += 100
    if "webvtt" in text or ".vtt" in text:
        score += 20
    if subtitle_url(subtitle):
        score += 10
    return score


def find_subtitles_in_playback(playback):
    subtitles = []
    for key in ("subtitles", "subtitleTracks", "textTracks", "tracks"):
        value = playback.get(key) if isinstance(playback, dict) else None
        if isinstance(value, list):
            subtitles.extend(value)
    subtitles.extend({"url": url} for url in find_urls(playback, "vtt") if ".vtt" in url.lower())
    return subtitles


def subtitles_from_hls_manifest(manifest_url):
    response = session.get(manifest_url, headers=DEFAULT_HEADERS, timeout=30)
    response.raise_for_status()
    subtitles = []
    for line in response.text.splitlines():
        if "#EXT-X-MEDIA" not in line or "SUBTITLES" not in line.upper():
            continue
        uri_match = re.search(r'URI="([^"]+)"', line)
        if uri_match:
            subtitles.append({"url": urljoin(manifest_url, uri_match.group(1)), "manifest_line": line})
    return subtitles


def vtt_segment_urls(playlist_url, playlist_text):
    urls = []
    for line in playlist_text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if ".vtt" in line.lower() or ".webvtt" in line.lower():
            urls.append(urljoin(playlist_url, line))
    return urls


def fetch_vtt_text(url):
    response = session.get(url, headers=DEFAULT_HEADERS, timeout=30)
    response.raise_for_status()
    text = response.content.decode("utf-8", "replace")
    if "#EXTM3U" not in text[:200].upper():
        return text

    parts = []
    segments = vtt_segment_urls(url, text)
    total = len(segments)
    if total:
        print(f"{bcolors.LIGHTBLUE}{icons.ICON_WAITING} Fetching {total} TV4 subtitle segments...{bcolors.ENDC}")
    for index, segment_url in enumerate(segments, 1):
        if total and (index == 1 or index == total or index % 100 == 0):
            print(f"\r{bcolors.LIGHTBLUE}{progress_bar(index, total)}{bcolors.ENDC}", end="", flush=True)
        segment_response = session.get(segment_url, headers=DEFAULT_HEADERS, timeout=30)
        segment_response.raise_for_status()
        parts.append(segment_response.content.decode("utf-8", "replace"))
    if total:
        print()
    return "\n\n".join(parts)


def get_subtitle(playback):
    subtitles = [subtitle for subtitle in playback.subtitles or [] if subtitle_url(subtitle)]
    if playback.manifest_type == "m3u8":
        try:
            subtitles.extend(subtitles_from_hls_manifest(playback.manifest_url))
        except Exception as exc:
            print(f"{bcolors.WARNING}{icons.ICON_WARNING} Could not inspect HLS subtitles: {exc}{bcolors.ENDC}")
    if not subtitles:
        return None
    subtitles.sort(key=subtitle_preference_score, reverse=True)
    return subtitles[0]


def save_translated_subtitles(playback, filename):
    subtitle = get_subtitle(playback)
    if not subtitle:
        print(f"{bcolors.WARNING}{icons.ICON_WARNING} No external Swedish subtitle URL found in TV4 playback or HLS manifest.{bcolors.ENDC}")
        return None

    url = subtitle_url(subtitle)
    cues = parse_vtt(fetch_vtt_text(url))
    if not cues:
        print(f"{bcolors.WARNING}{icons.ICON_WARNING} No subtitle cues found in TV4 VTT response.{bcolors.ENDC}")
        return None

    output_path = SAVE_PATH / f"{filename}.en.srt"
    print(f"{bcolors.LIGHTBLUE}{icons.ICON_WAITING} Translating Swedish subtitles to English SRT...{bcolors.ENDC}")
    write_srt(translate_cues(cues), output_path)
    print(f"{bcolors.OKGREEN}{icons.ICON_SUCCESS} English subtitles saved: {bcolors.ENDC}{output_path}")
    return output_path


def ask_translate_subtitles():
    try:
        user_input = input("Do you wish to save translated English subtitles? Y or N: ").strip().lower()
    except EOFError:
        return False

    return user_input == "y"


def translate_srt_file(input_path, output_path):
    cues = parse_srt(input_path.read_text(encoding="utf-8", errors="replace"))
    if not cues:
        print(f"{bcolors.WARNING}{icons.ICON_WARNING} No subtitle cues found in {input_path}{bcolors.ENDC}")
        return None

    print(f"{bcolors.LIGHTBLUE}{icons.ICON_WAITING} Translating Swedish subtitles to English SRT...{bcolors.ENDC}")
    write_srt(translate_cues(cues), output_path)
    print(f"{bcolors.OKGREEN}{icons.ICON_SUCCESS} English subtitles saved: {bcolors.ENDC}{output_path}")
    return output_path


def find_downloaded_swedish_subtitle(filename, started_at):
    roots = []
    for root in (SAVE_PATH, EUROVINE_TEMP_DIR, SERVICE_DIR / "temp", SERVICE_DIR, Path.cwd()):
        if root.exists() and root not in roots:
            roots.append(root)

    candidates = []
    for root in roots:
        candidates.extend(root.glob(f"{filename}*.srt"))

    candidates = [
        path for path in candidates
        if path.is_file()
        and ".en." not in path.name.lower()
        and path.stat().st_mtime >= started_at - 5
    ]
    if not candidates:
        return None

    def score(path):
        name = path.name.lower()
        language_score = 2 if re.search(r"\.(sv|swe|svenska)\.", name) else 0
        return language_score, path.stat().st_mtime

    return sorted(candidates, key=score, reverse=True)[0]


def translate_downloaded_subtitles(filename, started_at, delete_source=True):
    subtitle_path = find_downloaded_swedish_subtitle(filename, started_at)
    if not subtitle_path:
        print(
            f"{bcolors.WARNING}{icons.ICON_WARNING} No Swedish SRT sidecar found after download. "
            f"N_m3u8DL-RE may have muxed or named it differently.{bcolors.ENDC}"
        )
        return None

    output_path = subtitle_path.with_name(f"{filename}.en.srt")
    result = translate_srt_file(subtitle_path, output_path)
    if result and delete_source:
        try:
            subtitle_path.unlink()
            print(f"{bcolors.OKGREEN}{icons.ICON_SUCCESS} Removed Swedish subtitle sidecar: {bcolors.ENDC}{subtitle_path}")
        except OSError as exc:
            print(f"{bcolors.WARNING}{icons.ICON_WARNING} Could not remove Swedish subtitle sidecar: {exc}{bcolors.ENDC}")
    return result


def episode_series_number(item):
    return int(item.get("season") or 1)


def episode_list_number(item):
    return int(item.get("episode") or 1)


def episode_tree_label(item):
    return str(episode_list_number(item)), clean_text(item.get("title")) or f"Episode {episode_list_number(item)}"


def group_episode_items_by_series(episode_items):
    grouped = {}
    for item in episode_items:
        label = f"Series {episode_series_number(item)}"
        grouped.setdefault(label, []).append(item)
    return grouped


def series_group_sort_key(label):
    match = re.search(r"\d+", label)
    return int(match.group(0)) if match else 9999


def print_series_rule(service_label, series_title):
    terminal_width = shutil.get_terminal_size((88, 20)).columns
    title = f" {service_label}: {series_title} "
    rule_width = max(terminal_width, len(title) + 4)
    left_width = max((rule_width - len(title)) // 2, 0)
    right_width = max(rule_width - len(title) - left_width, 0)
    print(
        f"{bcolors.LIGHTBLUE}"
        f"{'─' * left_width}"
        f"{bcolors.ENDC} {bcolors.LIGHTBLUE}{service_label}: {bcolors.ENDC}{bcolors.WHITE}{series_title}{bcolors.ENDC} "
        f"{bcolors.LIGHTBLUE}{'─' * right_width}{bcolors.ENDC}"
    )


def list_episode_items(episode_items):
    if not episode_items:
        print(f"{icons.ICON_FAILURE} {bcolors.FAIL}No TV4 episodes found.{bcolors.ENDC}")
        return
    show = episode_items[0].get("show_title", "TV4")
    grouped_items = group_episode_items_by_series(episode_items)
    group_labels = sorted(grouped_items, key=series_group_sort_key)
    series_summary = ",  ".join(f"{label}({len(grouped_items[label])})" for label in group_labels)
    print(f"{icons.ICON_SUCCESS} {bcolors.OKGREEN}Found {len(episode_items)} TV4 episodes{bcolors.ENDC}")
    print()
    print_series_rule("TV4 Series", show)
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
    return f"S{episode_series_number(item):02d}E{episode_list_number(item):02d}"


def select_episode_items(series_url, selector):
    parsed = parse_download_selector(selector)
    episode_items = collect_episode_items(series_url)
    selected = []
    for item in episode_items:
        season = episode_series_number(item)
        episode = episode_list_number(item)
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
        raise ValueError(f"No TV4 episodes matched selector {format_download_selector(parsed)}.")
    return selected


def print_download_queue(episode_items):
    print()
    print(f"{bcolors.GRAY}Download queue:{bcolors.ENDC}")
    for item in episode_items:
        print(f"{bcolors.GRAY}{format_queue_selector(item)} {item.get('title') or ''}{bcolors.ENDC}".rstrip())


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
    parts.extend([resolution, "TV4", "WEB-DL", "AAC2.0", "H.264"])
    return ".".join(part for part in parts if part and part != "Unknown")


def build_download_command(playback, filename, keys=None, include_subtitles=False, interactive=False, quality=None):
    selectors = "" if interactive else f"{video_selector(quality)} --select-audio best "
    subtitle_selector = "--select-subtitle all" if include_subtitles else "--drop-subtitle all"
    subtitle_format = "--sub-format SRT " if include_subtitles else ""
    mux_options = "format=mkv:skip_sub=true" if include_subtitles else "format=mkv"
    command = (
        f'{N_M3U8DL} "{playback.manifest_url}" '
        f'{selectors}{subtitle_selector} {subtitle_format}'
        f'-mt -M {mux_options} --save-dir "{SAVE_PATH}" --save-name "{filename}"'
    )

    if keys:
        command += " " + " ".join(f"--key {key}" for key in keys)

    if SERVICE_PROXY:
        command += f' --custom-proxy "{SERVICE_PROXY}"'

    return command


def print_playback_details(playback, keys, command):
    label = "MPD URL" if playback.manifest_type == "mpd" else "M3U8 URL"
    print(f"{bcolors.LIGHTBLUE}{label}: {bcolors.ENDC}{playback.manifest_url}")

    if playback.license_url:
        print(f"{bcolors.RED}License URL: {bcolors.ENDC}{playback.license_url}")
    if playback.pssh:
        print(f"{bcolors.LIGHTBLUE}PSSH: {bcolors.ENDC}{playback.pssh}")
    if keys:
        print(f"{bcolors.OKGREEN}KEYS: {bcolors.ENDC}" + " ".join(f"--key {key}" for key in keys))
    elif playback.is_encrypted:
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


def resolve_video(video_url, quality=None):
    video_url = canonical_url(video_url)
    video_id = extract_video_id(video_url)
    metadata = search_metadata(video_url, video_id)
    playback = get_playback_info(video_url, metadata, verbose=False)

    keys = []
    if playback.license_url:
        if not playback.pssh:
            if playback.manifest_type == "mpd":
                playback.pssh = get_pssh_from_manifest(playback.manifest_url)
            elif playback.manifest_type == "m3u8":
                playback.pssh = get_pssh_from_hls(playback.manifest_url)
        if playback.pssh:
            setattr(metadata, "license_token", playback.license_token)
            keys = get_keys(playback.pssh, playback.license_url, metadata)
    elif playback.manifest_type not in ("mpd", "m3u8"):
        raise ValueError(f"Unsupported manifest type: {playback.manifest_type}")

    manifest_text = fetch_manifest_text(playback.manifest_url)
    streams, detected_manifest_type = parse_manifest_streams(manifest_text)
    playback.manifest_type = "mpd" if detected_manifest_type == "DASH" else "m3u8"

    extra_subtitles = list(playback.subtitles or [])
    if playback.manifest_type == "m3u8":
        try:
            extra_subtitles.extend(subtitles_from_hls_manifest(playback.manifest_url))
        except Exception:
            pass
    existing_subtitle_keys = {
        (stream.get("codec"), stream.get("lang"))
        for stream in streams
        if stream.get("type") == "Sub"
    }
    for subtitle_stream in subtitle_info_streams(extra_subtitles):
        key = (subtitle_stream.get("codec"), subtitle_stream.get("lang"))
        if key not in existing_subtitle_keys:
            streams.append(subtitle_stream)
            existing_subtitle_keys.add(key)
    playback.streams = sorted(streams, key=stream_table_sort_key)

    resolution = highest_stream_resolution(playback.streams)
    if resolution == "Unknown":
        resolution = get_resolution(playback)
    filename = format_filename(metadata, resolution)
    filename = apply_quality_to_filename(filename, quality)
    return playback, keys, resolution, filename


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
        raise ValueError("Info mode requires a TV4 Play episode/video URL.")

    playback, keys, _resolution, filename = run_with_spinner(lambda: resolve_video(video_url))
    manifest_label = "DASH Manifest URL" if playback.manifest_type == "mpd" else "HLS Manifest URL"
    print(f"{bcolors.LIGHTBLUE}{manifest_label}: {bcolors.ENDC}{playback.manifest_url}")
    if keys:
        print(f"{bcolors.OKGREEN}KEYS: {bcolors.ENDC}" + " ".join(f"--key {key}" for key in keys))
    print_streams(playback.streams)
    print_episode_metadata(playback.metadata)
    print(f"\n{bcolors.YELLOW}Suggested filename: {bcolors.ENDC}{filename}.mkv")


def maybe_download(command, auto_download=False):
    if auto_download:
        user_input = "y"
    else:
        user_input = input("Do you wish to download? Y or N: ").strip().lower()
    if user_input == "y":
        print(f"{icons.ICON_WAITING} {bcolors.OKBLUE}Downloading video...{bcolors.ENDC}")
        started_at = time.time()
        subprocess.run(command, shell=True)
        return True, started_at
    else:
        print(f"{icons.ICON_FAILURE} {bcolors.RED}Download cancelled{bcolors.ENDC}")
        return False, None


def process_video(video_url, auto_download=False, interactive=False, quality=None):
    video_url = canonical_url(video_url)
    print(f"{icons.ICON_INFO} {bcolors.LIGHTBLUE}Processing: {bcolors.ENDC}{video_url}")
    playback, keys, _resolution, filename = run_with_spinner(lambda: resolve_video(video_url, quality=quality))
    metadata = playback.metadata
    episode_str = f"S{metadata.season:02d}E{metadata.episode:02d}" if metadata.season and metadata.episode else ""
    if metadata.title != "Unknown" or episode_str or metadata.episode_title:
        detail_parts = [part for part in (episode_str, metadata.episode_title) if part]
        detail = f" {' - '.join(detail_parts)}" if detail_parts else ""
        print(f"{icons.ICON_SUCCESS} {bcolors.OKGREEN}{metadata.title}{bcolors.ENDC}{detail}")

    translate_subtitles = True if auto_download else ask_translate_subtitles()
    command = build_download_command(playback, filename, keys, include_subtitles=translate_subtitles, interactive=interactive, quality=quality)
    print_playback_details(playback, keys, command)
    downloaded, started_at = maybe_download(command, auto_download=auto_download)
    if downloaded and translate_subtitles:
        translate_downloaded_subtitles(filename, started_at)


def download_selected_episodes(series_url, selector, quality=None):
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
        process_video(item["url"], auto_download=True, quality=quality)


def export_episode_urls(episode_items):
    if not episode_items:
        print(f"{icons.ICON_FAILURE} {bcolors.FAIL}No TV4 episodes found to export.{bcolors.ENDC}")
        return

    export_dir = Path(__file__).resolve().parents[2] / "export"
    export_dir.mkdir(parents=True, exist_ok=True)
    show_title = episode_items[0].get("show_title", "TV4")
    output_path = export_dir / f"tv4_{safe_name(show_title)}_episodes.txt"
    output_path.write_text("\n".join(item["url"] for item in episode_items) + "\n", encoding="utf-8")
    print(f"{icons.ICON_SUCCESS} {bcolors.OKGREEN}Exported list: {output_path}{bcolors.ENDC}")


def main(video_url, downloads_path, wvd_device_path, cookies_path=None, tv4_credentials=None, tv4_config=None, mode="auto", export_list=False, download_selector=None, quality=None):
    """Eurovine entry point for TV4 Play (Widevine with TV4 cookie/token auth)."""
    try:
        if not video_url:
            raise ValueError("No TV4 URL provided.")
        if not downloads_path or not wvd_device_path:
            raise ValueError("Eurovine config requires downloads_path and wvd_device_path for TV4.")

        configure_service(downloads_path, wvd_device_path, cookies_path, tv4_credentials, tv4_config)
        video_url = canonical_url(video_url.strip())

        if mode == "list":
            if is_episode_url(video_url):
                print(f"{icons.ICON_FAILURE} {bcolors.FAIL}List mode requires a TV4 series URL, not an episode or movie URL.{bcolors.ENDC}")
                return
            print(f"{icons.ICON_WAITING} {bcolors.LIGHTBLUE}Retrieving series information.....{bcolors.ENDC}")
            episode_items = collect_episode_items(video_url)
            list_episode_items(episode_items)
            if export_list:
                export_episode_urls(episode_items)
            return

        if mode == "download":
            if is_episode_url(video_url):
                print(f"{icons.ICON_FAILURE} {bcolors.FAIL}Download selector mode requires a TV4 series URL, not an episode or movie URL.{bcolors.ENDC}")
                return
            print(f"{icons.ICON_WAITING} {bcolors.LIGHTBLUE}Retrieving series information.....{bcolors.ENDC}")
            download_selected_episodes(video_url, download_selector, quality)
            return

        if mode == "info":
            print_info_mode(video_url)
            return

        if export_list:
            if is_episode_url(video_url):
                print(f"{icons.ICON_FAILURE} {bcolors.FAIL}Export mode requires a TV4 series URL, not an episode or movie URL.{bcolors.ENDC}")
                return
            print(f"{icons.ICON_WAITING} {bcolors.LIGHTBLUE}Retrieving series information.....{bcolors.ENDC}")
            export_episode_urls(collect_episode_items(video_url))
            return

        if is_series_url(video_url):
            print(f"{icons.ICON_WARNING} {bcolors.WARNING}Series URLs require a flag. Use --list/-l to list episodes, --export/-x to export episode URLs, or --download/-d SELECTOR to download selected episodes.{bcolors.ENDC}")
            return

        process_video(video_url, interactive=(mode == "interactive"), quality=quality)
    except Exception as exc:
        print(f"{icons.ICON_FAILURE} {bcolors.FAIL}Error: {exc}{bcolors.ENDC}")
        raise
