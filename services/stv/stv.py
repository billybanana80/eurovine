import requests
import re
import json
import subprocess
from xml.etree import ElementTree as ET
from pywidevine.cdm import Cdm
from pywidevine.device import Device
from pywidevine.pssh import PSSH
import base64
import binascii
import sys
import urllib3
import shutil
from urllib.parse import urljoin, urlparse, urlunparse
from selectolax.lexbor import LexborHTMLParser
from pathlib import Path
import icons
from colors import bcolors
from beaupy.spinners import Spinner
from services.proxy import append_downloader_proxy, current_proxy_url, mask_proxy_command

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

session = requests.Session()

# Constants for API URLs and headers
BRIGHTCOVE_KEY = "BCpkADawqM1WJ12PwtUWqGXx3nbAo2XVSxyAQxPRZKBc75svhrUB9qIMPN_d9US0Vib5smumeNMbntSmZIpzeVV1iUrnzYgf5k7UMaVN46PGYe_oSZ-xbPVnsm4"  # Non-DRM
BRIGHTCOVE_KEY_DRM = "BCpkADawqM1fQNUrQOvg-vTo4VGDTJ_lGjxp2zBSPcXJntYd5csQkjm7hBKviIVgfFoEJLW4_JPPsHUwXNEjZspbr3d1HqGDw2gUqGCBZ_9Y_BF7HJsh2n6PQcpL9b2kdbi103oXvmTNZWiQ"  # DRM

# Account IDs
BRIGHTCOVE_ACCOUNT_DRM = "6204867266001"  # DRM
BRIGHTCOVE_ACCOUNT_NON_DRM = "1486976045"  # Non-DRM

SAVE_PATH = None
WVD_PATH = None
STV_PROXY = None

def mask_proxy(proxy_text):
    return mask_proxy_command(proxy_text)

def configure_service(downloads_path, wvd_device_path):
    """Apply configuration supplied by the Eurovine organizer."""
    global SAVE_PATH, WVD_PATH, STV_PROXY
    SAVE_PATH = Path(downloads_path)
    WVD_PATH = wvd_device_path
    SAVE_PATH.mkdir(exist_ok=True, parents=True)
    session.proxies.clear()
    proxy_url = current_proxy_url()
    STV_PROXY = proxy_url
    if proxy_url:
        session.proxies.update({'http': proxy_url, 'https': proxy_url})

def clean_url(url):
    parsed = urlparse(url)
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path.rstrip('/') + '/', '', '', ''))

def is_episode_url(url):
    return "/episode/" in urlparse(url).path

def is_series_url(url):
    return "/summary/" in urlparse(url).path

def fetch_page_data(url):
    headers = {
        'Accept': '*/*',
        'Connection': 'keep-alive',
        'Origin': 'https://player.stv.tv',
        'Referer': 'https://player.stv.tv/',
    }
    response = session.get(url, headers=headers, timeout=30)
    response.raise_for_status()
    tree = LexborHTMLParser(response.text)
    node = tree.root.css_first('#__NEXT_DATA__')
    if not node:
        raise ValueError("Could not find STV page metadata.")
    return json.loads(node.text())

STV_API_HEADERS = {
    'stv-drm': 'true',
    'user-agent': 'okhttp/4.11.0',
}

def summary_slug_from_url(url):
    match = re.search(r"/summary/([^/?#]+)", urlparse(url).path)
    if not match:
        raise ValueError("Could not extract STV summary slug from URL.")
    return match.group(1)

def fetch_stv_api(path, params=None):
    url = urljoin("https://player.api.stv.tv/v1/", path.lstrip("/"))
    response = session.get(url, params=params, headers=STV_API_HEADERS, timeout=30)
    response.raise_for_status()
    return response.json()

def fetch_programme_data(slug):
    data = fetch_stv_api(f"programmes/{slug}")
    results = data.get('results')
    if not isinstance(results, dict):
        raise ValueError("STV programme API returned an unexpected response.")
    return results

def fetch_episodes_for_series_guid(series_guid):
    data = fetch_stv_api("episodes", params={'series.guid': series_guid, 'limit': 200, 'groupToken': '0071'})
    return data.get('results') or []

def _to_int(value, default=0):
    try:
        if value is None:
            return default
        match = re.search(r"\d+", str(value))
        return int(match.group(0)) if match else int(value)
    except Exception:
        return default

def episode_number_from_title(title):
    match = re.search(r"\bEpisode\s+(\d+)\b", title or "", flags=re.IGNORECASE)
    return int(match.group(1)) if match else None

def season_number_from_title(title):
    match = re.search(r"\bSeries\s+(\d+)\b", title or "", flags=re.IGNORECASE)
    return int(match.group(1)) if match else 1

def collect_episode_items(series_url, show_progress=True):
    slug = summary_slug_from_url(series_url)
    programme = fetch_programme_data(slug)
    show_title = programme.get('name') or programme.get('shortName') or "STV"

    episode_items = []
    seen = set()
    for series in programme.get('series') or []:
        series_guid = series.get('guid')
        if not series_guid:
            continue
        season = season_number_from_title(series.get('name'))
        for episode in fetch_episodes_for_series_guid(series_guid):
            episode_id = str(episode.get('id') or '')
            permalink = episode.get('_permalink') or episode.get('link')
            if not permalink or not episode_id or episode_id in seen:
                continue
            seen.add(episode_id)
            url = clean_url(urljoin("https://player.stv.tv", permalink))
            player_series = episode.get('playerSeries') if isinstance(episode.get('playerSeries'), dict) else {}
            episode_season = (
                season_number_from_title(player_series.get('name'))
                or season_number_from_title(series.get('name'))
                or season
            )
            episode_number = _to_int(episode.get('number'), 0) or episode_number_from_title(episode.get('title'))
            episode_items.append({
                'url': url,
                'id': episode_id,
                'episode': episode,
                'show_title': show_title,
                'sort_season': episode_season,
                'sort_episode': episode_number,
            })

    if not episode_items:
        raise ValueError("No STV episodes found.")

    if show_progress:
        print(f"{icons.ICON_INFO} {bcolors.LIGHTBLUE}Show: {bcolors.ENDC}{show_title}")

    episode_items.sort(key=episode_sort_key)
    return episode_items

def episode_series_number(item):
    season = item.get('sort_season')
    return int(season) if season not in (None, 0, '') else None

def episode_number(item):
    episode = item.get('sort_episode')
    return int(episode) if episode not in (None, 0, '') else None

def episode_title(item):
    episode = item['episode']
    title = episode.get('title') or 'Unknown'
    summary = episode.get('summary') or ''
    return title if not summary else title

def clean_text(value):
    return re.sub(r"\s+", " ", str(value or "")).strip()

def format_bitrate(value):
    try:
        bitrate = int(float(value))
    except (TypeError, ValueError):
        return "-"
    return f"{bitrate / 1000000:.2f} Mbps" if bitrate >= 1000000 else f"{bitrate // 1000} Kbps"

def parse_attribute_list(value):
    return {
        match.group(1): match.group(2).strip().strip('"')
        for match in re.finditer(r'([A-Z0-9-]+)=("[^"]*"|[^,]*)', value)
    }

def stream_sort_key(stream):
    type_order = {"Vid": 0, "Aud": 1, "Sub": 2}
    height_match = re.search(r"x(\d+)", stream.get("resolution") or "")
    height = int(height_match.group(1)) if height_match else 0
    bitrate_text = stream.get("bitrate") or ""
    bitrate_match = re.search(r"[\d.]+", bitrate_text)
    bitrate = float(bitrate_match.group()) if bitrate_match else 0
    if "Mbps" in bitrate_text:
        bitrate *= 1000
    return (type_order.get(stream.get("type"), 9), -height, -bitrate, stream.get("lang") or "")

def parse_dash_streams(manifest_content):
    root = ET.fromstring(manifest_content)
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
    return sorted(streams, key=stream_sort_key)

def parse_hls_streams(manifest_text):
    streams = []
    pending_variant = None
    for raw_line in manifest_text.splitlines():
        line = raw_line.strip()
        if line.startswith("#EXT-X-STREAM-INF:"):
            pending_variant = parse_attribute_list(line.split(":", 1)[1])
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
        attrs = parse_attribute_list(line.split(":", 1)[1])
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
    return sorted(streams, key=stream_sort_key)

def parse_manifest_streams(manifest_content):
    manifest_text = manifest_content.decode("utf-8", errors="ignore") if isinstance(manifest_content, bytes) else str(manifest_content)
    if manifest_text.lstrip().startswith("#EXTM3U"):
        return parse_hls_streams(manifest_text), "HLS"
    return parse_dash_streams(manifest_content), "DASH"

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
        for index, stream in enumerate(streams, start=1)
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

def format_date(value):
    if not value:
        return "Unknown"
    return clean_text(value)

def print_episode_metadata(episode):
    programme = episode.get("programme") if isinstance(episode.get("programme"), dict) else {}
    player_series = episode.get("playerSeries") if isinstance(episode.get("playerSeries"), dict) else {}
    show_title = clean_text(programme.get("name") or "STV")
    title = clean_text(episode.get("title") or "Unknown")
    schedule = episode.get("schedule") if isinstance(episode.get("schedule"), dict) else {}
    rows = [
        ("Show", show_title),
        ("Title", title),
        ("Date Aired", format_date(schedule.get("startTime"))),
        ("Description", clean_text(episode.get("summary") or programme.get("shortDescription") or "Unknown")),
    ]
    print(f"\n{bcolors.YELLOW}Episode metadata:{bcolors.ENDC}")
    for label, value in rows:
        if value:
            print(f"{bcolors.LIGHTBLUE}{label}: {bcolors.ENDC}{value}")

def episode_tree_label(item):
    number = episode_number(item)
    return str(number).zfill(2) if number is not None else "-", episode_title(item)

def episode_sort_key(item):
    season = episode_series_number(item)
    episode = episode_number(item)
    return (
        season if season is not None else 9999,
        episode if episode is not None else 9999,
        item.get('id', ''),
    )

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
        f"{bcolors.LIGHTBLUE}"
        f"{'─' * left_width}"
        f"{bcolors.ENDC} {bcolors.LIGHTBLUE}{service_label}: {bcolors.ENDC}{bcolors.WHITE}{series_title}{bcolors.ENDC} "
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

def warn_if_partial_range_match(parsed_selector, selected):
    if parsed_selector["type"] == "episode_range":
        requested_start = (parsed_selector["start"]["season"], parsed_selector["start"]["episode"])
        requested_end = (parsed_selector["end"]["season"], parsed_selector["end"]["episode"])
        matched_start = (episode_series_number(selected[0]), episode_number(selected[0]))
        matched_end = (episode_series_number(selected[-1]), episode_number(selected[-1]))
        if matched_start > requested_start or matched_end < requested_end:
            matched_label = f"{format_queue_selector(*matched_start)}-{format_queue_selector(*matched_end)}"
            print(f"{bcolors.WARNING}{icons.ICON_WARNING} Requested range {format_download_selector(parsed_selector)} only matched {matched_label}.{bcolors.ENDC}")

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
        raise ValueError(f"No STV episodes found for selector {format_download_selector(parsed_selector)}.")
    selected.sort(key=episode_sort_key)
    warn_if_partial_range_match(parsed_selector, selected)
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

def list_episode_items(episode_items):
    if not episode_items:
        print(f"{icons.ICON_FAILURE} {bcolors.FAIL}No STV episodes found.{bcolors.ENDC}")
        return
    show_title = episode_items[0].get('show_title', 'STV')
    grouped_items = group_episode_items_by_series(episode_items)
    group_labels = sorted(grouped_items, key=series_group_sort_key)
    series_summary = ",  ".join(f"{label}({len(grouped_items[label])})" for label in group_labels)
    print(f"{icons.ICON_SUCCESS} {bcolors.OKGREEN}Found {len(episode_items)} STV episodes{bcolors.ENDC}")
    print()
    print_series_rule("STV Series", show_title)
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

def export_episode_urls(episode_items):
    """Write listed STV episode URLs to Eurovine's shared export directory."""
    export_dir = Path(__file__).resolve().parents[2] / "export"
    export_dir.mkdir(parents=True, exist_ok=True)
    title = episode_items[0].get("show_title", "stv") if episode_items else "stv"
    safe_title = re.sub(r"[^A-Za-z0-9._-]+", "_", title).strip("._") or "stv"
    output_path = export_dir / f"{safe_title}_episodes.txt"
    output_path.write_text("\n".join(item['url'] for item in episode_items) + "\n", encoding="utf-8")
    print(f"{icons.ICON_SUCCESS} {bcolors.OKGREEN}Exported list: {output_path}{bcolors.ENDC}")

# Function to get the video ID from the epsidoe page source
def get_video_id_from_url(url):
    headers = {
        'Accept': '*/*',
        'Connection': 'keep-alive',
        'Origin': 'https://player.stv.tv',
        'Referer': 'https://player.stv.tv/',
    }
    response = session.get(url, headers=headers, timeout=30)
    response.raise_for_status()
    tree = LexborHTMLParser(response.text)
    jsondata = tree.root.css_first('#__NEXT_DATA__').text()
    myjson = json.loads(jsondata)

    try:
        episodeId = str(myjson['props']['pageProps']['episodeId'])
        interim = f"/episodes/{episodeId}"
        cache_entry = myjson['props']['initialReduxState']['playerApiCache'][interim]
        jsonshort = cache_entry.get('results')
        if not jsonshort:
            jsonshort = get_episode_from_api(myjson, url)
        videoId = jsonshort['video']['id']
        guid = jsonshort['guid']
        
        # Check for 'playerSeries' first, if null, use 'programme.guid'
        if jsonshort.get('playerSeries') is not None:
            seriesguid = jsonshort['playerSeries'].get('guid', 'null')
        else:
            seriesguid = jsonshort['programme'].get('guid', 'null')  # Fallback to 'programme.guid'
        
        DRM = jsonshort['programme']['drmEnabled']
    
    except Exception as e:
        print(f"Error parsing video data: {e}")
        raise ValueError("The data supplied has a non-compliant structure.")
    
    return videoId, seriesguid, guid, DRM

def get_episode_from_api(page_data, page_url):
    page_props = page_data['props']['pageProps']
    episode_info = page_props.get('episodeInfo') or {}
    episode_id = str(episode_info.get('episodeId') or page_props.get('episodeId') or '')
    episode_guid = episode_info.get('episodeGuid')
    programme_guid = episode_info.get('programmeGuid') or page_props.get('programme')

    if not programme_guid:
        raise ValueError("Could not find programme GUID for STV episode API lookup.")

    api_url = "https://player.api.stv.tv/v1/episodes"
    params = {'programme.guid': programme_guid, 'limit': 200, 'groupToken': '0071'}
    response = session.get(api_url, params=params, headers=STV_API_HEADERS, timeout=30)
    response.raise_for_status()
    data = response.json()

    for episode in data.get('results', []):
        if (
            str(episode.get('id')) == episode_id
            or episode.get('guid') == episode_guid
            or episode.get('_permalink', '').rstrip('/') == page_url.rstrip('/')
        ):
            return episode

    raise ValueError("Could not match the requested STV episode in the public episode API.")

def get_episode_details_from_url(url):
    headers = {
        'Accept': '*/*',
        'Connection': 'keep-alive',
        'Origin': 'https://player.stv.tv',
        'Referer': 'https://player.stv.tv/',
    }
    response = session.get(url, headers=headers, timeout=30)
    response.raise_for_status()
    tree = LexborHTMLParser(response.text)
    node = tree.root.css_first('#__NEXT_DATA__')
    if not node:
        raise ValueError("Could not find STV page metadata.")
    myjson = json.loads(node.text())

    try:
        episodeId = str(myjson['props']['pageProps']['episodeId'])
        interim = f"/episodes/{episodeId}"
        cache_entry = myjson['props']['initialReduxState']['playerApiCache'][interim]
        episode = cache_entry.get('results')
        if not episode:
            episode = get_episode_from_api(myjson, url)
        video_id = episode['video']['id']
        guid = episode['guid']
        if episode.get('playerSeries') is not None:
            seriesguid = episode['playerSeries'].get('guid', 'null')
        else:
            seriesguid = episode['programme'].get('guid', 'null')
        drm = episode['programme']['drmEnabled']
    except Exception as exc:
        raise ValueError(f"Could not parse STV episode playback metadata: {exc}") from exc

    return episode, video_id, seriesguid, guid, drm

# Function to get PSSH from MPD URL
def get_pssh(mpd_url):
    response = session.get(mpd_url, timeout=30)
    root = ET.fromstring(response.content)
    pssh_elements = root.findall(".//{urn:mpeg:dash:schema:mpd:2011}ContentProtection")

    for elem in pssh_elements:
        pssh = elem.find("{urn:mpeg:cenc:2013}pssh")
        if pssh is not None and pssh.text:
            pssh_data = pssh.text.strip()
            try:
                base64.b64decode(pssh_data)  # Validate Base64
                return pssh_data
            except binascii.Error as e:
                print(f"Invalid PSSH data: {e}")
    return None

def get_pssh_from_manifest(manifest_content):
    root = ET.fromstring(manifest_content)
    pssh_elements = root.findall(".//{*}ContentProtection")
    for elem in pssh_elements:
        pssh = elem.find("{urn:mpeg:cenc:2013}pssh")
        if pssh is not None and pssh.text:
            pssh_data = pssh.text.strip()
            try:
                base64.b64decode(pssh_data)
                return pssh_data
            except binascii.Error:
                continue
    return None

# Function to get keys using PSSH and license URL
def get_keys(pssh, lic_url):
    try:
        pssh = PSSH(pssh)
    except binascii.Error as e:
        print(f"Could not decode PSSH data as Base64: {e}")
        return []

    device = Device.load(WVD_PATH)
    cdm = Cdm.from_device(device)
    session_id = cdm.open()
    challenge = cdm.get_license_challenge(session_id, pssh)

    headers = {
        'Content-Type': 'application/octet-stream',
        'Origin': 'https://player.stv.tv',
        'Referer': 'https://player.stv.tv/',
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
    }

    licence = session.post(lic_url, headers=headers, data=challenge, timeout=30)
    licence.raise_for_status()
    
    cdm.parse_license(session_id, licence.content)
    keys = [f"{key.kid.hex}:{key.key.hex()}" for key in cdm.get_keys(session_id) if key.type == 'CONTENT']
    cdm.close(session_id)
    return keys

# Function to extract the maximum resolution
def get_max_resolution(manifest_data):
    max_resolution = "1080p"  # Default resolution
    try:
        resolutions = [item['Resolution'] for item in manifest_data if 'Resolution' in item]
        if resolutions:
            max_res = max(resolutions, key=lambda r: int(r.split('x')[1]))  # Extract height and find max
            max_resolution = max_res.split('x')[1] + "p"
    except Exception:
        pass  # Use default resolution if any issue occurs
    return max_resolution

# Function to format the video name
def format_videoname(videoname):
    # Replace dashes and spaces with dots
    videoname = videoname.replace('-', '.').replace(' ', '.')
    # Extract season and episode numbers
    season_episode_match = re.search(r'Series\.(\d+)\.\.*Episode\.(\d+)', videoname)
    if season_episode_match:
        season = f"S{int(season_episode_match.group(1)):02}"
        episode = f"E{int(season_episode_match.group(2)):02}"
        # Remove the episode title and construct the final name
        videoname = re.sub(r'Series\.\d+\.\.*Episode\.\d+.*', f'{season}{episode}', videoname)
    # Clean up any multiple dots
    videoname = re.sub(r'\.+', '.', videoname)
    videoname = videoname.rstrip('.')
    return videoname

def fetch_brightcove_playback(video_id, drm):
    account_id = BRIGHTCOVE_ACCOUNT_DRM if drm else BRIGHTCOVE_ACCOUNT_NON_DRM
    key = BRIGHTCOVE_KEY_DRM if drm else BRIGHTCOVE_KEY
    headers = {
        'Accept': f'application/json;pk={key}',
    }
    url = f"https://edge.api.brightcove.com/playback/v1/accounts/{account_id}/videos/{video_id}"
    response = session.get(url, headers=headers, timeout=30)
    response.raise_for_status()
    return response.json()

def select_dash_source(sources):
    for source in sources or []:
        source_url = source.get('src') or ''
        source_type = (source.get('type') or '').lower()
        key_systems = source.get('key_systems') or {}
        if ('dash' in source_type or '.mpd' in source_url.lower()) and key_systems.get('com.widevine.alpha'):
            return source
    for source in sources or []:
        source_url = source.get('src') or ''
        source_type = (source.get('type') or '').lower()
        if 'dash' in source_type or '.mpd' in source_url.lower():
            return source
    return None

def select_hls_source(sources):
    for source in sources or []:
        source_url = source.get('src') or ''
        source_type = (source.get('type') or '').lower()
        if 'mpegurl' in source_type or 'hls' in source_type or '.m3u8' in source_url.lower():
            return source
    for source in sources or []:
        if source.get('src'):
            return source
    return None

def fetch_manifest_content(manifest_url):
    response = session.get(manifest_url, timeout=30)
    response.raise_for_status()
    return response.content

def highest_stream_resolution(streams, default="1080p"):
    heights = []
    for stream in streams or []:
        if stream.get("type") != "Vid":
            continue
        match = re.search(r"x(\d+)", stream.get("resolution") or "")
        if match:
            heights.append(int(match.group(1)))
    return f"{max(heights)}p" if heights else default

def build_drm_download_command(manifest, save_name, keys, interactive=False):
    selectors = "" if interactive else "--select-video best --select-audio best --select-subtitle all "
    command = f'N_m3u8DL-RE "{manifest}" {selectors}-mt -M format=mkv --save-name "{save_name}" --save-dir "{SAVE_PATH}" --key ' + ' --key '.join(keys)
    return append_downloader_proxy(command)

def build_hls_download_command(manifest, save_name, interactive=False):
    selectors = "" if interactive else "--select-video best --select-audio best --select-subtitle all "
    command = f'N_m3u8DL-RE "{manifest}" {selectors}-mt --save-name "{save_name}" --save-dir "{SAVE_PATH}" '
    return append_downloader_proxy(command)

def resolve_playback_info(video_url, include_manifest=True, include_keys=True, interactive=False):
    episode, video_id, seriesguid, guid, drm = get_episode_details_from_url(video_url)
    playback = fetch_brightcove_playback(video_id, drm)
    sources = playback.get('sources') or []
    videoname = format_videoname(playback.get('name') or episode.get('title') or 'STV')

    if drm:
        source = select_dash_source(sources)
        if not source:
            raise ValueError("Could not find a DASH Widevine source for this STV episode.")
        manifest = source.get('src')
        license_url = ((source.get('key_systems') or {}).get('com.widevine.alpha') or {}).get('license_url')
        if not manifest or not license_url:
            raise ValueError("STV returned incomplete DASH/Widevine playback details.")
        manifest_content = fetch_manifest_content(manifest) if include_manifest else None
        streams, manifest_type = parse_manifest_streams(manifest_content) if manifest_content else ([], "DASH")
        pssh = get_pssh_from_manifest(manifest_content) if manifest_content else get_pssh(manifest)
        keys = get_keys(pssh, license_url) if include_keys and pssh else []
        save_name = f"{videoname}.1080p.STV.WEB-DL.AAC2.0.H.264"
        return {
            "episode": episode,
            "manifest": manifest,
            "manifest_type": manifest_type,
            "license_url": license_url,
            "pssh": pssh,
            "keys": keys,
            "streams": streams,
            "save_name": save_name,
            "download_command": build_drm_download_command(manifest, save_name, keys, interactive=interactive),
            "drm": True,
        }

    source = select_hls_source(sources)
    if not source:
        raise ValueError("Could not find an HLS source for this STV episode.")
    manifest = source.get('src')
    if not manifest:
        raise ValueError("STV returned incomplete HLS playback details.")
    manifest_content = fetch_manifest_content(manifest) if include_manifest else None
    streams, manifest_type = parse_manifest_streams(manifest_content) if manifest_content else ([], "HLS")
    resolution = highest_stream_resolution(streams, get_max_resolution(sources))
    save_name = f"{videoname}.{resolution}.STV.WEB-DL.AAC2.0.H.264"
    return {
        "episode": episode,
        "manifest": manifest,
        "manifest_type": manifest_type,
        "license_url": None,
        "pssh": None,
        "keys": [],
        "streams": streams,
        "save_name": save_name,
        "download_command": build_hls_download_command(manifest, save_name, interactive=interactive),
        "drm": False,
    }

def run_with_spinner(message, callback):
    spinner = Spinner()
    spinner.start()
    try:
        result = callback()
    except Exception:
        spinner.stop()
        raise
    spinner.stop()
    return result

def print_info_mode(video_url):
    resolved = run_with_spinner("Resolving STV playback information...", lambda: resolve_playback_info(video_url))
    manifest_label = "DASH Manifest URL" if resolved["manifest_type"] == "DASH" else "HLS Manifest URL"
    print(f"{bcolors.LIGHTBLUE}{manifest_label}: {bcolors.ENDC}{resolved['manifest']}")
    if resolved["keys"]:
        print(f"{bcolors.GREEN}KEYS: {bcolors.ENDC}" + " ".join(f"--key {key}" for key in resolved["keys"]))
    print_streams(resolved["streams"])
    print_episode_metadata(resolved["episode"])
    print(f"\n{bcolors.LIGHTBLUE}Suggested filename: {bcolors.ENDC}{resolved['save_name']}.mkv")

# Function to handle DRM-protected videos
def handle_drm(video_id, auto_download=False):
    headers = {
        'Accept': f'application/json;pk={BRIGHTCOVE_KEY_DRM}',
    }
    url = f"https://edge.api.brightcove.com/playback/v1/accounts/{BRIGHTCOVE_ACCOUNT_DRM}/videos/{video_id}"
    response = session.get(url, headers=headers, timeout=30)
    response.raise_for_status()
    myjson = response.json()

    videoname = format_videoname(myjson['name'])
    manifest = myjson['sources'][3]['src']
    license = myjson['sources'][3]['key_systems']['com.widevine.alpha']['license_url']
    
    pssh = get_pssh(manifest)
    keys = get_keys(pssh, license)

    # Default resolution to 1080p for DRM files
    resolution = "1080p"
    
    save_name = f"{videoname}.{resolution}.STV.WEB-DL.AAC2.0.H.264"
    
    download_command = f"""N_m3u8DL-RE "{manifest}" --select-video best --select-audio best --select-subtitle all -mt -M format=mkv --save-name "{save_name}" --save-dir "{SAVE_PATH}" --key """ + ' --key '.join(keys)
    if STV_PROXY:
        download_command += f' --custom-proxy "{STV_PROXY}"'
    
    # Print the download command and other details for encrypted videos
    print(f"{bcolors.LIGHTBLUE}MPD URL: {bcolors.ENDC}{manifest}")
    print(f"{bcolors.RED}License URL: {bcolors.ENDC}{license}")
    print(f"{bcolors.LIGHTBLUE}PSSH: {bcolors.ENDC}{pssh}")
    for key in keys:
        print(f"{bcolors.GREEN}KEYS: {bcolors.ENDC}--key {key}")
    print(f"{bcolors.YELLOW}DOWNLOAD COMMAND:{bcolors.ENDC} {mask_proxy(download_command)}")

    if download_command:
        if auto_download:
            print(f"{icons.ICON_WAITING} {bcolors.OKBLUE}Downloading video...{bcolors.ENDC}")
            subprocess.run(download_command, shell=True)
            return
        user_input = input("Do you wish to download? Y or N: ").strip().lower()
        if user_input == 'y':
            subprocess.run(download_command, shell=True)    

# Function to handle non-DRM videos
def handle_no_drm(video_id, auto_download=False):
    headers = {
        'Accept': f'application/json;pk={BRIGHTCOVE_KEY}',
    }
    url = f"https://edge.api.brightcove.com/playback/v1/accounts/{BRIGHTCOVE_ACCOUNT_NON_DRM}/videos/{video_id}"
    response = session.get(url, headers=headers, timeout=30)
    response.raise_for_status()
    myjson = response.json()

    videoname = format_videoname(myjson['name'])
    manifest_data = myjson['sources']
    manifest = manifest_data[0]['src']
    
    resolution = get_max_resolution(manifest_data)
    
    save_name = f"{videoname}.{resolution}.STV.WEB-DL.AAC2.0.H.264"
    
    download_command = f"""N_m3u8DL-RE "{manifest}" --select-video best --select-audio best --select-subtitle all -mt --save-name "{save_name}" --save-dir "{SAVE_PATH}" """
    if STV_PROXY:
        download_command += f' --custom-proxy "{STV_PROXY}"'
    
    # Print the download command for unencrypted videos
    print(f"{bcolors.LIGHTBLUE}M3U8 URL: {bcolors.ENDC}{manifest}")    
    print(f"{bcolors.YELLOW}DOWNLOAD COMMAND:{bcolors.ENDC} {mask_proxy(download_command)}")

    if download_command:
        if auto_download:
            print(f"{icons.ICON_WAITING} {bcolors.OKBLUE}Downloading video...{bcolors.ENDC}")
            subprocess.run(download_command, shell=True)
            return
        user_input = input("Do you wish to download? Y or N: ").strip().lower()
        if user_input == 'y':
            subprocess.run(download_command, shell=True)

# Main function to determine if DRM or non-DRM and proceed accordingly
def get_download_command(video_url, auto_download=False, interactive=False):
    resolved = run_with_spinner("Resolving STV playback information...", lambda: resolve_playback_info(video_url, interactive=interactive))

    if resolved["drm"]:
        print(f"{bcolors.LIGHTBLUE}MPD URL: {bcolors.ENDC}{resolved['manifest']}")
        print(f"{bcolors.RED}License URL: {bcolors.ENDC}{resolved['license_url']}")
        print(f"{bcolors.LIGHTBLUE}PSSH: {bcolors.ENDC}{resolved['pssh']}")
        for key in resolved["keys"]:
            print(f"{bcolors.GREEN}KEYS: {bcolors.ENDC}--key {key}")
        print(f"{bcolors.YELLOW}DOWNLOAD COMMAND:{bcolors.ENDC} {mask_proxy_command(resolved['download_command'])}")
    else:
        print(f"{bcolors.LIGHTBLUE}M3U8 URL: {bcolors.ENDC}{resolved['manifest']}")
        print(f"{bcolors.YELLOW}DOWNLOAD COMMAND:{bcolors.ENDC} {mask_proxy_command(resolved['download_command'])}")

    if auto_download:
        print(f"{icons.ICON_WAITING} {bcolors.OKBLUE}Downloading video...{bcolors.ENDC}")
        subprocess.run(resolved["download_command"], shell=True)
        return

    user_input = input("Do you wish to download? Y or N: ").strip().lower()
    if user_input == 'y':
        subprocess.run(resolved["download_command"], shell=True)

def download_selected_episodes(series_url, selector):
    print(f"{icons.ICON_WAITING} {bcolors.LIGHTBLUE}Retrieving series information.....{bcolors.ENDC}")
    episode_items = select_episode_items(series_url, selector)
    print_download_queue(episode_items)

    episode_word = "episode" if len(episode_items) == 1 else "episodes"
    this_or_these = "this" if len(episode_items) == 1 else "these"
    user_input = input(f"Do you wish to download {this_or_these} {len(episode_items)} {episode_word}? Y or N: ").strip().lower()
    if user_input != 'y':
        print(f"{icons.ICON_FAILURE} {bcolors.RED}Download cancelled{bcolors.ENDC}")
        return

    for index, item in enumerate(episode_items, start=1):
        _, title = episode_tree_label(item)
        print(f"\n{icons.ICON_INFO} {bcolors.LIGHTBLUE}Downloading {index}/{len(episode_items)}: {title}{bcolors.ENDC}")
        get_download_command(item['url'], auto_download=True)

def main(video_url, downloads_path, wvd_device_path, mode="auto", export_list=False, download_selector=None):
    """Eurovine entry point for STV (Widevine where applicable)."""
    if not video_url:
        raise ValueError("No STV URL provided.")
    if not downloads_path or not wvd_device_path:
        raise ValueError("Eurovine config requires downloads_path and wvd_device_path for STV.")
    configure_service(downloads_path, wvd_device_path)
    video_url = video_url.strip()

    if mode == "list":
        if is_episode_url(video_url):
            print(f"{icons.ICON_FAILURE} {bcolors.FAIL}List mode requires an STV summary URL, not an episode URL.{bcolors.ENDC}")
            return
        try:
            episode_items = collect_episode_items(video_url, show_progress=False)
            list_episode_items(episode_items)
            if export_list:
                export_episode_urls(episode_items)
        except (requests.RequestException, ValueError) as exc:
            print(f"{icons.ICON_FAILURE} {bcolors.FAIL}{exc}{bcolors.ENDC}")
        return

    if mode == "info":
        if not is_episode_url(video_url):
            print(f"{icons.ICON_FAILURE} {bcolors.FAIL}Info mode requires an STV episode URL, not a summary URL.{bcolors.ENDC}")
            return
        try:
            print_info_mode(video_url)
        except (requests.RequestException, ValueError, ET.ParseError, KeyError) as exc:
            print(f"{icons.ICON_FAILURE} {bcolors.FAIL}Error: {exc}{bcolors.ENDC}")
        return

    if mode == "download":
        if is_episode_url(video_url):
            print(f"{icons.ICON_FAILURE} {bcolors.FAIL}Download selector mode requires an STV summary URL, not an episode URL.{bcolors.ENDC}")
            return
        try:
            download_selected_episodes(video_url, download_selector)
        except (requests.RequestException, ValueError, ET.ParseError, KeyError) as exc:
            print(f"{icons.ICON_FAILURE} {bcolors.FAIL}{exc}{bcolors.ENDC}")
        return

    if is_episode_url(video_url):
        try:
            get_download_command(video_url, interactive=(mode == "interactive"))
        except (requests.RequestException, ValueError, ET.ParseError, KeyError) as exc:
            print(f"{icons.ICON_FAILURE} {bcolors.FAIL}Error: {exc}{bcolors.ENDC}")
        return

    print(f"{icons.ICON_WARNING} {bcolors.WARNING}Series URLs require a flag. Use --list/-l to list episodes or --download/-d SELECTOR to download selected episodes.{bcolors.ENDC}")

if __name__ == "__main__":
    print("Run STV through eurovine.py so it can use the shared Eurovine configuration.")
