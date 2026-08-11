import base64
from base64 import b64encode
import json
import os
import re
import subprocess
from bs4 import BeautifulSoup
from pathlib import Path
from beaupy.spinners import Spinner
from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad
from pywidevine.pssh import PSSH
from pywidevine.device import Device
from pywidevine.cdm import Cdm
import time
import requests
import urllib3
import sys
import shutil
import icons
from colors import bcolors
import xml.etree.ElementTree as ET
from datetime import datetime
from urllib.parse import urljoin
from services.proxy import append_downloader_proxy, current_proxy_url, mask_proxy_command

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

#   Ozivine: All4 Video Downloader
#   Author: billybanana
#   Usage: enter the series/season/episode URL to retrieve the MPD, Licence, PSSH and Decryption keys.
#   eg: https://www.channel4.com/programmes/location-location-location/on-demand/72080-013 or movies eg: https://www.channel4.com/programmes/top-gun-maverick/on-demand/74807-001
#   Authentication: Client ID and secret
#   Geo-Locking: requires a UK address
#   Quality: up to 1080p
#   Key Features:
#   1. Extract Video ID: Parses the All4 URL to extract the series name, season, and episode number.
#   2. Extract PSSH: Retrieves and parses the MPD file to generate the PSSH data necessary for Widevine decryption.
#   3. Fetch Decryption Keys: Uses the PSSH and license URL to request and retrieve the Widevine decryption keys.
#   4. Print Download Information: Outputs the MPD URL, license URL, PSSH, and decryption keys required for downloading and decrypting the video content.
#   5. Note: this script functions for encrypted video files only (All4 files are all currently encrypted).

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ANSI escape codes for colors
WVD_PATH = None
DOWNLOAD_DIR = None
n_m3u8dl = "N_m3u8DL-RE"  # Change to however it is named

SERVICE_DIR = Path(__file__).resolve().parent
EUROVINE_DIR = SERVICE_DIR.parents[1]
TMP_DIR = EUROVINE_DIR / "temp"

TMP_DIR.mkdir(exist_ok=True, parents=True)

DEFAULT_HEADERS = {
    'Content-type': 'application/json',
    'Accept': '*/*',
    'Referer': 'https://www.channel4.com/',
    "user-agent": "Dalvik/2.1.0 (Linux; U; Android 12; SM-G930F Build/SQ1D.220105.007)"
}

MPD_HEADERS = {
    'Content-type': 'application/dash+xml',
    'Accept': '*/*',
    'Referer': 'https://www.channel4.com/',
    "user-agent": "Dalvik/2.1.0 (Linux; U; Android 12; SM-G930F Build/SQ1D.220105.007)"
}

global client
client = requests.Session()

def configure_service(downloads_path, wvd_device_path):
    """Apply configuration supplied by the Eurovine organizer."""
    global WVD_PATH, DOWNLOAD_DIR
    WVD_PATH = wvd_device_path
    DOWNLOAD_DIR = downloads_path
    client.proxies.clear()
    proxy_url = current_proxy_url()
    if proxy_url:
        client.proxies.update({'http': proxy_url, 'https': proxy_url})

def clean_url(url):
    parsed = requests.utils.urlparse(url)
    return requests.utils.urlunparse((parsed.scheme, parsed.netloc, parsed.path.rstrip('/'), '', '', ''))

def is_episode_url(url):
    return "/on-demand/" in requests.utils.urlparse(url).path

def parse_page_params(url):
    try:
        response = client.get(url, timeout=30)
        response.raise_for_status()
    except requests.RequestException as exc:
        raise ConnectionError(f"Failed to fetch Channel 4 page metadata: {exc}") from exc
    page_text = (
        response.content.decode()
        .replace('\u200c', '')
        .replace('\r\n', '')
        .replace('undefined', 'null')
    )
    init_data = re.search(r'<script>window\.__PARAMS__ = (.*)</script>', page_text)
    if not init_data:
        raise ValueError("Could not find Channel 4 page metadata.")
    try:
        return json.loads(init_data.group(1))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Unable to parse Channel 4 page metadata: {exc}") from exc

def parse_season_episode(episode):
    full_title = episode.get('fullTitle') or episode.get('title') or ''
    title_match = re.search(
        r'\bSeries\s+(\d+)\s*,?\s*Episode\s+(\d+)\b',
        full_title,
        flags=re.IGNORECASE
    )
    if title_match:
        return int(title_match.group(1)), int(title_match.group(2))

    season = episode.get('seriesNumber') or episode.get('series')
    episode_number = episode.get('episodeNumber') or episode.get('episode')
    return (
        int(season) if season not in (None, '') else None,
        int(episode_number) if episode_number not in (None, '') else None,
    )

def episode_title(episode):
    title = episode.get('fullTitle') or episode.get('title') or episode.get('secondaryTitle') or 'Unknown'
    title = re.sub(r'^\s*Series\s+\d+\s+Episode\s+\d+\s*:?\s*', '', title, flags=re.IGNORECASE)
    title = re.sub(r'^\s*Episode\s+\d+\s*:?\s*', '', title, flags=re.IGNORECASE)
    return title.strip() or 'Unknown'

def clean_text(value):
    if value is None:
        return ""
    value = re.sub(r"<[^>]+>", " ", str(value))
    return re.sub(r"\s+", " ", value).strip()

def first_value(mapping, *keys):
    for key in keys:
        value = mapping.get(key)
        if value not in (None, "", []):
            return value
    return ""

def format_info_date(value):
    value = clean_text(value)
    if value.lower().startswith("first shown:"):
        return value.split(":", 1)[1].strip()
    if not value:
        return ""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return f"{parsed.day} {parsed.strftime('%B %Y')}"
    except ValueError:
        return value

def format_bitrate(value):
    try:
        bitrate = int(float(value))
    except (TypeError, ValueError):
        return "-"
    return f"{bitrate / 1000000:.2f} Mbps" if bitrate >= 1000000 else f"{bitrate // 1000} Kbps"

def format_frame_rate(value):
    value = clean_text(value)
    if "/" in value:
        try:
            numerator, denominator = value.split("/", 1)
            return f"{float(numerator) / float(denominator):.3g} fps"
        except (ValueError, ZeroDivisionError):
            pass
    return f"{value} fps" if value else ""

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

def parse_dash_streams(manifest_text):
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
        role = next(
            (clean_text(node.get("value")) for node in adaptation.findall("{*}Role") if node.get("value")),
            "",
        )
        adaptation_channels = next(
            (
                clean_text(node.get("value"))
                for node in adaptation.findall("{*}AudioChannelConfiguration")
                if node.get("value")
            ),
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
            sample_rate = clean_text(
                representation.get("audioSamplingRate") or adaptation.get("audioSamplingRate")
            )
            if frame_rate:
                extra.append(frame_rate)
            if sample_rate:
                extra.append(f"{sample_rate} Hz")
            if role:
                extra.append(role)
            if representation.get("id"):
                extra.append(f"id={representation.get('id')}")

            streams.append({
                "type": stream_type,
                "resolution": f"{width}x{height}" if width and height else "-",
                "bitrate": format_bitrate(representation.get("bandwidth")),
                "codec": codec,
                "lang": lang,
                "channels": channels or "-",
                "extra": ", ".join(extra) or "-",
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
                "extra": ", ".join(
                    value for value in (
                        f"fps={attrs.get('FRAME-RATE')}" if attrs.get("FRAME-RATE") else "",
                        f"video={attrs.get('VIDEO')}" if attrs.get("VIDEO") else "",
                        f"audio={attrs.get('AUDIO')}" if attrs.get("AUDIO") else "",
                    ) if value
                ) or "-",
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
            "extra": ", ".join(
                value for value in (
                    attrs.get("NAME"),
                    "default" if attrs.get("DEFAULT") == "YES" else "",
                    "forced" if attrs.get("FORCED") == "YES" else "",
                    f"uri={urljoin('', attrs.get('URI'))}" if attrs.get("URI") else "",
                ) if value
            ) or "-",
        })
    return sorted(streams, key=stream_sort_key)

def parse_manifest_streams(manifest_text):
    if manifest_text.lstrip().startswith("#EXTM3U"):
        return parse_hls_streams(manifest_text), "HLS"
    return parse_dash_streams(manifest_text), "DASH"

def print_streams(streams):
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
    widths = [
        min(max(len(headings[column]), *(len(row[column]) for row in rows)), 52)
        for column in range(len(headings))
    ]
    widths[0] = 3
    print("  ".join(f"{heading:<{widths[index]}}" for index, heading in enumerate(headings)))
    print("  ".join("-" * width for width in widths))
    for row in rows:
        print("  ".join(f"{value[:widths[index]]:<{widths[index]}}" for index, value in enumerate(row)))

def max_height_from_streams(streams, default="1080"):
    heights = []
    for stream in streams:
        if stream.get("type") != "Vid":
            continue
        match = re.search(r"x(\d+)", stream.get("resolution") or "")
        if match:
            heights.append(int(match.group(1)))
    return str(max(heights)) if heights else str(default)

def print_episode_metadata(item):
    episode = item.get("episode") or {}
    rows = [
        ("Show", clean_text(item.get("show_title"))),
        ("Title", clean_text(episode.get("fullTitle") or episode.get("title") or episode.get("secondaryTitle"))),
        ("Date Aired", format_info_date(episode.get("dateLabel"))),
        ("Description", clean_text(episode.get("summary"))),
    ]
    print(f"\n{bcolors.YELLOW}Episode metadata:{bcolors.ENDC}")
    for label, value in rows:
        if value:
            print(f"{bcolors.LIGHTBLUE}{label}: {bcolors.ENDC}{value}")

def collect_episode_items(series_url, show_progress=True):
    data = parse_page_params(series_url)
    brand = data.get('initialData', {}).get('brand', {})
    show_title = brand.get('title', 'Channel 4')
    episodes = brand.get('episodes') or []
    if not episodes:
        raise ValueError("No Channel 4 episodes found.")

    if show_progress:
        print(f"{icons.ICON_INFO} {bcolors.LIGHTBLUE}Show: {bcolors.ENDC}{show_title}")

    episode_items = []
    for episode in episodes:
        air_date = episode.get('dateLabel') or ''
        if 'Next on TV' in air_date:
            continue

        href = episode.get('hrefLink')
        if not href:
            continue

        url = href if href.startswith('http') else f"https://www.channel4.com{href}"
        season, episode_number = parse_season_episode(episode)
        episode_items.append({
            'url': clean_url(url),
            'id': episode.get('programmeId') or clean_url(url).split('/')[-1],
            'episode': episode,
            'show_title': show_title,
            'sort_season': season if season is not None else 9999,
            'sort_episode': episode_number if episode_number is not None else 9999,
        })

    episode_items.sort(key=episode_sort_key)
    return episode_items

def collect_episode_item(video_url):
    data = parse_page_params(video_url)
    initial_data = data.get('initialData', {})
    episode = initial_data.get('selectedEpisode') or {}
    brand = initial_data.get('brand') or {}
    if not episode:
        raise ValueError("Could not read Channel 4 episode metadata.")

    season, episode_number = parse_season_episode(episode)
    return {
        'url': clean_url(video_url),
        'id': episode.get('programmeId') or clean_url(video_url).split('/')[-1],
        'episode': episode,
        'show_title': brand.get('title') or episode.get('brandTitle') or 'Channel 4',
        'sort_season': season if season is not None else 9999,
        'sort_episode': episode_number if episode_number is not None else 9999,
    }

def episode_sort_key(item):
    return (item.get('sort_season', 9999), item.get('sort_episode', 9999), item.get('id', ''))

def episode_series_number(item):
    season = item.get('sort_season')
    return season if season != 9999 else None

def episode_number(item):
    episode = item.get('sort_episode')
    return episode if episode != 9999 else None

def episode_tree_label(item):
    number = episode_number(item)
    return str(number).zfill(2) if number is not None else "-", episode_title(item['episode'])

def group_episode_items_by_series(episode_items):
    grouped = {}
    for item in sorted(episode_items, key=episode_sort_key):
        season = episode_series_number(item)
        series_label = f"Series {season}" if season is not None else "Episodes"
        grouped.setdefault(series_label, []).append(item)
    return grouped

def series_group_sort_key(label):
    match = re.search(r'\d+', label)
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
            print(f"{bcolors.WARNING}{icons.ICON_WARNING} Requested range {format_download_selector(parsed_selector)} only matched {matched_label}.{bcolors.ENDC}")

    if parsed_selector["type"] == "season_range":
        requested_start = parsed_selector["start"]["season"]
        requested_end = parsed_selector["end"]["season"]
        matched_seasons = sorted({episode_series_number(item) for item in selected if episode_series_number(item) is not None})
        if matched_seasons and (matched_seasons[0] > requested_start or matched_seasons[-1] < requested_end):
            matched_label = f"{format_queue_selector(matched_seasons[0])}-{format_queue_selector(matched_seasons[-1])}"
            print(f"{bcolors.WARNING}{icons.ICON_WARNING} Requested range {format_download_selector(parsed_selector)} only matched seasons {matched_label}.{bcolors.ENDC}")

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
        raise ValueError(f"No Channel 4 episodes found for selector {format_download_selector(parsed_selector)}.")

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
        print(f"{icons.ICON_FAILURE} {bcolors.FAIL}No Channel 4 episodes found.{bcolors.ENDC}")
        return

    show_title = episode_items[0].get('show_title', 'Channel 4')
    grouped_items = group_episode_items_by_series(episode_items)
    group_labels = sorted(grouped_items, key=series_group_sort_key)
    series_summary = ",  ".join(f"{label}({len(grouped_items[label])})" for label in group_labels)

    print(f"{icons.ICON_SUCCESS} {bcolors.OKGREEN}Found {len(episode_items)} Channel 4 episodes{bcolors.ENDC}")
    print()
    print_series_rule("Channel 4 Series", show_title)
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
    """Write the listed episode URLs to Eurovine's shared export directory."""
    export_dir = Path(__file__).resolve().parents[2] / "export"
    export_dir.mkdir(parents=True, exist_ok=True)
    show_title = episode_items[0].get("show_title", "channel4") if episode_items else "channel4"
    safe_title = re.sub(r"[^A-Za-z0-9._-]+", "_", show_title).strip("._") or "channel4"
    output_path = export_dir / f"{safe_title}_episodes.txt"
    output_path.write_text("\n".join(item["url"] for item in episode_items) + "\n", encoding="utf-8")
    print(f"{icons.ICON_SUCCESS} {bcolors.OKGREEN}Exported list: {output_path}{bcolors.ENDC}")

class ComplexJsonEncoder(json.JSONEncoder):
    def default(self, o):
        if hasattr(o, 'to_json'):
            return o.to_json()
        return json.JSONEncoder.default(self, o)

class Video:
    def __init__(self, video_type: str, url: str):
        self.video_type = video_type
        self.url = url

    def to_json(self):
        resp = {}
        if self.video_type != "":
            resp['type'] = self.video_type
        if self.url != "":
            resp['url'] = self.url
        return resp

class DrmToday:
    def __init__(self, request_id: str, token: str, video: Video, message: str):
        self.request_id = request_id
        self.token = token
        self.video = video
        self.message = message

    def to_json(self):
        resp = {}
        if self.request_id != "":
            resp['request_id'] = self.request_id
        if self.token != "":
            resp['token'] = self.token
        if self.video != "":
            resp['video'] = self.video
        if self.message != "":
            resp['message'] = self.message
        return resp

class Status:
    def __init__(self, success: bool, status_type: str):
        self.success = success
        self.status_type = status_type

class VodConfig:
    def __init__(self, vodbs_url: str, drm_today: DrmToday, message: str):
        self.vodbs_url = vodbs_url
        self.drm_today = drm_today
        self.message = message

class VodStream:
    def __init__(self, token: str, uri: str, brand_title: str, episode_title: str):
        self.token = token
        self.uri = uri
        self.brand_title = brand_title
        self.episode_title = episode_title

    def to_json(self):
        resp = {}
        if self.token != "":
            resp['token'] = self.token
        if self.uri != "":
            resp['uri'] = self.uri
        return resp

class LicenseResponse:
    def __init__(self, license_response: str, status: Status):
        self.license_response = license_response
        self.status = status

    def to_json(self):
        resp = {}
        if self.license_response != "":
            resp['license'] = self.license_response
        if self.status != "":
            resp['status'] = self.status
        return resp

def decrypt_token(token: str):
    try:
        cipher = AES.new(
            b"\x41\x59\x44\x49\x44\x38\x53\x44\x46\x42\x50\x34\x4d\x38\x44\x48",
            AES.MODE_CBC,
            b"\x31\x44\x43\x44\x30\x33\x38\x33\x44\x4b\x44\x46\x53\x4c\x38\x32"
        )
        decoded_token = base64.b64decode(token)
        decrypted_string = unpad(cipher.decrypt(
            decoded_token), 16, style='pkcs7').decode('UTF-8')
        license_info = decrypted_string.split('|')
        return VodStream(license_info[1], license_info[0], '', '')
    except: 
        print('[!] Failed decrypting VOD stream !!!')
        raise

def get_vod_stream(target: str):
    try:
        urla = "https://api.channel4.com/online/v2/auth/token"
        headers1 = {
            "accept-encoding": "gzip",
            "connection": "Keep-Alive",
            "content-length": "103",
            "content-type": "application/x-www-form-urlencoded",
            "host": "api.channel4.com",
            "user-agent": "Dalvik/2.1.0 (Linux; U; Android 14; Pixel 6a Build/UQ1A.240205.002) C4oD_Android/9.7.2 (uid:824f075b-dc9a-4dea-b059-6d6c040376ac; tid:-; did:Google_Pixel 6a_34;)",
            "x-correlation-id": "ANDROID-cb8d7eb8-5494-4792-92a6-353e33a30fd0",
        }
        data1 = (
            "grant_type=client_credentials&client_id=36UUCt98VMQvBAgQ27Au8zGHl31N9LQ1&client_secret=JYswyHvGe62VlikW"
        )
        response1 = requests.post(url=urla, headers=headers1, data=data1, proxies=client.proxies or None, timeout=30)
        response1.raise_for_status()
        response1 = response1.json()
        bearertoken = response1["accessToken"]

        url = f"https://api.channel4.com/online/v1/vod/stream/{target}?client=android-mod"

        headers = {
            'accept-encoding': 'gzip',
            'authorization': 'Bearer ' + bearertoken,
            'connection': 'Keep-Alive',
            'host': 'api.channel4.com',
            'x-c4-app-version': '"android_app:9.7.2"',
            'x-c4-date': '2024-03-02T11:10:28Z',
            'x-c4-device-name': 'Google Pixel 6a (bluejay)',
            'x-c4-device-type': 'mobile',
            'x-c4-optimizely-datafile': 'unknown',
            'x-c4-platform-name': 'android',
        }

        req = client.get(url, headers=headers, timeout=30)
        req.raise_for_status()
        myjson = req.json()
        uri = myjson['videoProfiles'][0]['streams'][0]['uri']
        token = myjson['videoProfiles'][0]['streams'][0]['token']
        brand_title = myjson['brandTitle']
        brand_title = brand_title.replace(':', ' ').replace('/', ' ')
        episode_title = myjson['episodeTitle']
        episode_title = episode_title.replace('/', ' ').replace(':', ' ')
        vod_stream = VodStream(token, uri, brand_title, episode_title)
        return vod_stream
    except (requests.RequestException, KeyError, IndexError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Failed getting VOD stream: {exc}") from exc

def get_asset_id(url: str):
    try:
        req = client.get(url, timeout=30)
        req.raise_for_status()
        init_data = re.search(
            r'<script>window\.__PARAMS__ = (.*)</script>',
            ''.join(
                req.content.decode()
                .replace('\u200c', '')
                .replace('\r\n', '')
                .replace('undefined', 'null')
            )
        )
        init_data = json.loads(init_data.group(1))
        asset_id = int(init_data['initialData']['selectedEpisode']['assetId'])
        if asset_id == 0:
            raise ValueError("Channel 4 page did not contain a playable asset ID.")
        return asset_id
    except (requests.RequestException, AttributeError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Failed getting asset ID: {exc}") from exc

def get_config():
    try:
        req = client.get('https://static.c4assets.com/all4-player/latest/bundle.app.js', timeout=30)
        req.raise_for_status()
        configs = re.findall(
            r"JSON\.parse\(\'(.*?)\'\)",
            ''.join(
                req.content.decode()
                .replace('\u200c', '')
                .replace('\\"', '\"')
            )
        )
        config = json.loads(configs[1])
        video_type = config['protectionData']['com.widevine.alpha']['drmtoday']['video']['type']
        message = config['protectionData']['com.widevine.alpha']['drmtoday']['message']
        video = Video(video_type, '')
        drm_today = DrmToday('', '', video, message)
        vod_config = VodConfig(config['vodbsUrl'], drm_today, '')
        return vod_config
    except (requests.RequestException, IndexError, KeyError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Failed getting production config: {exc}") from exc

def get_service_certificate(url: str, drm_today: DrmToday):
    try:
        req = client.post(url, data=json.dumps(
            drm_today.to_json(), cls=ComplexJsonEncoder), headers=DEFAULT_HEADERS, timeout=30)
        req.raise_for_status()
        resp = json.loads(req.content)
        license_response = resp['license']
        status = Status(resp['status']['success'], resp['status']['type'])
        return LicenseResponse(license_response, status)
    except (requests.RequestException, KeyError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Failed getting signed DRM certificate: {exc}") from exc

def get_license_response(url: str, drm_today: DrmToday):
    try:
        req = client.post(url, data=json.dumps(
            drm_today.to_json(), cls=ComplexJsonEncoder), headers=DEFAULT_HEADERS, timeout=30)
        req.raise_for_status()
        resp = json.loads(req.content)
        license_response = resp['license']
        status = Status(resp['status']['success'], resp['status']['type'])
        if not status.success:
            raise ValueError(f"DRM service returned status {status.status_type}")
        return LicenseResponse(license_response, status)
    except (requests.RequestException, KeyError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Failed getting license challenge: {exc}") from exc

def get_kid(url: str):
    try:
        req = client.get(url, headers=MPD_HEADERS, timeout=30)
        req.raise_for_status()
        match = re.search(r'cenc:default_KID="([^"]+)"', req.text)
        if not match:
            raise ValueError("manifest did not contain a Widevine KID")
        kid = match.group(1)
        return kid
    except (requests.RequestException, ValueError) as exc:
        raise RuntimeError(f"Failed getting KID: {exc}") from exc

def generate_pssh(kid: str):
    try:
        kid = kid.replace('-','')
        s = f'000000387073736800000000edef8ba979d64acea3c827dcd51d21ed000000181210{kid}48e3dc959b06'
        return b64encode(bytes.fromhex(s)).decode()
    except: 
        print('[!] Failed generating PSSH !!!')
        raise

def get_videoname_by_soup(url):
    HEADERS = {
        "user-agent": "Dalvik/2.1.0 (Linux; U; Android 12; SM-G930F Build/SQ1D.220105.007) C4oD_Android/9.4.3 (uid:3e113df8-0a46-4fa6-8e5f-ee0b3d5f0a3b; tid:-; did:samsung_SM-G930F_31;)",
        'Accept-Language': 'en-US, en;q=0.5'
    }
    webpage = client.get(url, headers=HEADERS, timeout=30)
    webpage.raise_for_status()
    page_text = (
        webpage.content.decode()
        .replace('\u200c', '')
        .replace('\r\n', '')
        .replace('undefined', 'null')
    )
    init_data = re.search(r'<script>window\.__PARAMS__ = (.*)</script>', page_text)
    if init_data:
        try:
            init_data = json.loads(init_data.group(1))
            initial_data = init_data.get('initialData', {})
            selected_episode = initial_data.get('selectedEpisode', {})
            brand = initial_data.get('brand', {})
            brand_title = brand.get('title') or selected_episode.get('brandTitle')
            full_title = (
                selected_episode.get('fullTitle')
                or selected_episode.get('title')
                or selected_episode.get('secondaryTitle')
                or ''
            )
            series_number = selected_episode.get('seriesNumber')
            episode_number = selected_episode.get('episodeNumber')
            episode_title = full_title
            title_match = re.search(
                r'\bSeries\s+(\d+)\s+Episode\s+(\d+)\s*:?\s*(.*)$',
                full_title,
                flags=re.IGNORECASE
            )
            if title_match:
                series_number = title_match.group(1)
                episode_number = title_match.group(2)
                episode_title = title_match.group(3)
            elif episode_title:
                episode_title = re.sub(
                    r'^\s*(?:Series\s+\d+\s+)?Episode\s+\d+\s*:?\s*',
                    '',
                    episode_title,
                    flags=re.IGNORECASE
                )
            if brand_title and series_number and episode_number:
                videoname = f"{brand_title}_S{int(series_number):02d}E{int(episode_number):02d}"
                if episode_title:
                    videoname = f"{videoname}_{episode_title}"
                return videoname
        except Exception:
            pass
    soup = BeautifulSoup(webpage.content, "html.parser")
    videoname = str(soup.text).split('|')[0].replace(':','').replace("'",'').replace('-','').replace(' ','_')
    videoname = re.sub(r"(\d+)", pad_number, videoname)
    videoname = videoname.replace('Watch_','').replace('_Series_', 'S').replace('_Episode_','E').rstrip('_')
    return videoname

def pad_number(match):
    number = int(match.group(1))
    return format(number, "02d")

def extract_max_height(mpd_url):
    try:
        req = client.get(mpd_url, headers=MPD_HEADERS, timeout=30)
        req.raise_for_status()
        max_height = re.search(r'maxHeight="(\d+)"', req.text).group(1)
        return max_height
    except:
        print('[!] Failed extracting max height from MPD !!!')
        return "1080"

def format_videoname(videoname, max_height):
    formatted_name = videoname.replace(' ', '.').replace('_', '.')
    return f"{formatted_name}.{max_height}p.ALL4.WEB-DL.AAC2.0.H.264"

def get_streams(mpd, decryption_key, output_title, auto_download=False, interactive=False):
    selectors = "" if interactive else "--select-video best --select-audio best --select-subtitle all "
    command = f'N_m3u8DL-RE "{mpd}" {selectors}-mt -M format=mkv:muxer=mkvmerge --save-name "{output_title}" --save-dir "{DOWNLOAD_DIR}" --key {decryption_key}'
    command = append_downloader_proxy(command)
    
    if auto_download:
        print(f"{icons.ICON_WAITING} {bcolors.OKBLUE}Downloading video...{bcolors.ENDC}")
        subprocess.run(command, shell=True)
        return

    user_input = input("Do you wish to download? Y or N: ").strip().lower()
    if user_input == 'y':
        subprocess.run(command, shell=True)
    return

def clean(videoname):
    illegals = "*'%$!(),.:;"
    replacements = {
        'Episode ': 'E',
        'Series ': 'S',
        ' - ': ' ',
        ' ': '_',
        '&': 'and',
        '?': '',
    }
    videoname = ''.join(c for c in videoname if c.isprintable() and c not in illegals)
    for rep in replacements:  
        videoname = videoname.replace(rep, replacements[rep])
    return videoname

def resolve_playback(url):
    wvd = WVD_PATH
    device = Device.load(wvd)
    cdm = Cdm.from_device(device)
    session_id = cdm.open()
    try:
        config = get_config()
        asset_id = get_asset_id(url)
        target = url.split('/')[-1]
        encrypted_vod_stream = get_vod_stream(target)
        decrypted_vod_stream = decrypt_token(encrypted_vod_stream.token)
        config.drm_today.video.url = encrypted_vod_stream.uri
        config.drm_today.token = decrypted_vod_stream.token
        config.drm_today.request_id = asset_id
        service_cert = get_service_certificate(decrypted_vod_stream.uri, config.drm_today).license_response
        cdm.set_service_certificate(session_id, service_cert)
        kid = get_kid(config.drm_today.video.url)
        pssh = generate_pssh(kid)
        challenge = cdm.get_license_challenge(session_id, PSSH(pssh), privacy_mode=True)
        config.drm_today.message = base64.b64encode(challenge).decode('UTF-8')
        license_response = get_license_response(decrypted_vod_stream.uri, config.drm_today)
        cdm.parse_license(session_id, license_response.license_response)
        decryption_keys = list(dict.fromkeys(
            f'{key.kid.hex}:{key.key.hex()}'
            for key in cdm.get_keys(session_id)
            if key.type == 'CONTENT'
        ))
        return {
            "manifest_url": config.drm_today.video.url,
            "license_url": decrypted_vod_stream.uri,
            "pssh": pssh,
            "keys": decryption_keys,
        }
    finally:
        cdm.close(session_id)

def print_playback_details(playback):
    print(f"{bcolors.LIGHTBLUE}MPD URL: {bcolors.ENDC}{playback['manifest_url']}")
    print(f"{bcolors.RED}License URL: {bcolors.ENDC}{playback['license_url']}")
    print(f"{bcolors.LIGHTBLUE}PSSH: {bcolors.ENDC}{playback['pssh']}")
    for key in playback.get("keys") or []:
        print(f"{bcolors.GREEN}KEYS: {bcolors.ENDC}--key {key}")

def fetch_manifest(manifest_url):
    try:
        response = client.get(manifest_url, headers=MPD_HEADERS, timeout=30)
        response.raise_for_status()
        return response.text
    except requests.RequestException as exc:
        raise ConnectionError(f"Failed to fetch manifest: {exc}") from exc

def info(url):
    if not is_episode_url(url):
        raise ValueError("Info mode requires a Channel 4 episode/video URL.")

    spinner = Spinner()
    spinner.start()
    try:
        item = collect_episode_item(url)
        playback = resolve_playback(url)
        manifest_text = fetch_manifest(playback["manifest_url"])
        streams, manifest_type = parse_manifest_streams(manifest_text)
        max_height = max_height_from_streams(streams)
        videoname = clean(get_videoname_by_soup(url))
        formatted_videoname = format_videoname(videoname, max_height)
    except Exception:
        spinner.stop()
        raise
    spinner.stop()

    print(f"{bcolors.LIGHTBLUE}{manifest_type} Manifest URL: {bcolors.ENDC}{playback['manifest_url']}")
    if playback.get("keys"):
        for key in playback["keys"]:
            print(f"{bcolors.GREEN}KEYS: {bcolors.ENDC}--key {key}")
    print_streams(streams)
    print_episode_metadata(item)
    print(f"\n{bcolors.YELLOW}Suggested filename: {bcolors.ENDC}{formatted_videoname}.mkv")

def process_video(url, auto_download=False, interactive=False):
    spinner = Spinner()
    spinner.start()
    try:
        playback = resolve_playback(url)
        videoname = get_videoname_by_soup(url)
        videoname = clean(videoname)
        max_height = extract_max_height(playback["manifest_url"])
        formatted_videoname = format_videoname(videoname, max_height)
    except Exception:
        spinner.stop()
        raise
    spinner.stop()

    print_playback_details(playback)
    decryption_keys = playback.get("keys") or []
    if not decryption_keys:
        raise RuntimeError("No decryption keys were returned for this Channel 4 stream.")
    selectors = "" if interactive else "--select-video best --select-audio best --select-subtitle all "
    download_command = f'N_m3u8DL-RE "{playback["manifest_url"]}" {selectors}-mt -M format=mkv:muxer=mkvmerge --save-name "{formatted_videoname}" --save-dir "{DOWNLOAD_DIR}" --key {decryption_keys[0]}'
    download_command = append_downloader_proxy(download_command)
    print(f"{bcolors.YELLOW}DOWNLOAD COMMAND: {bcolors.ENDC}{mask_proxy_command(download_command)}")
    get_streams(playback["manifest_url"], decryption_keys[0], formatted_videoname, auto_download=auto_download, interactive=interactive)
    return

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
        process_video(item['url'], auto_download=True)

def main(video_url, downloads_path, wvd_device_path, mode="auto", export_list=False, download_selector=None):
    """Eurovine entry point for Channel 4 (Widevine)."""
    if not video_url:
        raise ValueError("No Channel 4 URL provided.")
    if not downloads_path or not wvd_device_path:
        raise ValueError("Eurovine config requires downloads_path and wvd_device_path for All4.")
    configure_service(downloads_path, wvd_device_path)
    video_url = video_url.strip()

    if mode == "list":
        try:
            if is_episode_url(video_url):
                episode_items = [collect_episode_item(video_url)]
            else:
                episode_items = collect_episode_items(video_url, show_progress=False)
            list_episode_items(episode_items)
            if export_list:
                export_episode_urls(episode_items)
        except Exception as exc:
            print(f"{icons.ICON_FAILURE} {bcolors.FAIL}{exc}{bcolors.ENDC}")
        return

    if mode == "info":
        try:
            info(video_url)
        except Exception as exc:
            print(f"{icons.ICON_FAILURE} {bcolors.FAIL}{exc}{bcolors.ENDC}")
        return

    if mode == "download":
        if is_episode_url(video_url):
            print(f"{icons.ICON_FAILURE} {bcolors.FAIL}Download selector mode requires a Channel 4 series URL, not an episode URL.{bcolors.ENDC}")
            return
        try:
            download_selected_episodes(video_url, download_selector)
        except Exception as exc:
            print(f"{icons.ICON_FAILURE} {bcolors.FAIL}{exc}{bcolors.ENDC}")
        return

    if is_episode_url(video_url):
        try:
            process_video(video_url, interactive=(mode == "interactive"))
        except Exception as exc:
            print(f"{icons.ICON_FAILURE} {bcolors.FAIL}{exc}{bcolors.ENDC}")
        return

    print(f"{icons.ICON_WARNING} {bcolors.WARNING}Series URLs require a flag. Use --list/-l to list episodes or --download/-d SELECTOR to download selected episodes.{bcolors.ENDC}")

if __name__ == "__main__":
    print("Run Channel 4 through eurovine.py so it can use the shared Eurovine configuration.")
