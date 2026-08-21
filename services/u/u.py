import requests
import re
import subprocess
from xml.etree import ElementTree as ET
from pywidevine.cdm import Cdm
from pywidevine.device import Device
from pywidevine.pssh import PSSH
import base64
import binascii
from pathlib import Path
import sys
import urllib3
import shutil
from urllib.parse import urlparse, urlunparse
from datetime import datetime
from beaupy.spinners import Spinner
import icons
from download_confirm import confirm_download
from colors import bcolors
from quality_utils import apply_quality_to_filename, video_selector
from services.proxy import append_downloader_proxy, current_proxy_url, mask_proxy_command

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

session = requests.Session()

# Constants for API URLs and headers
BRIGHTCOVE_ACCOUNT = "1242911124001"  # Replace with correct account ID
BRIGHTCOVE_KEY = "BCpkADawqM2ZEz-kf0i2xEP9VuhJF_DB5boH7YAeSx5EHDSNFFl4QUoHZ3bKLQ9yWboSOBNyvZKm4HiZrqMNRxXm-laTAnmls1QOL7_kUM3Eij4KjQMz0epMs3WIedg64fnRxQTX6XubGE9p"
BRIGHTCOVE_HEADERS = {
    "User-Agent": "Dalvik/2.1.0 (Linux; U; Android 12; SM-A226B Build/SP1A.210812.016)",
    "Accept": "application/json;pk=" + BRIGHTCOVE_KEY,
    "Host": "edge.api.brightcove.com",
    "Connection": "keep-alive"
}
BRIGHTCOVE_API = lambda video_id: f"https://edge.api.brightcove.com/playback/v1/accounts/{BRIGHTCOVE_ACCOUNT}/videos/{video_id}"

SAVE_PATH = None
WVD_PATH = None

def configure_service(downloads_path, wvd_device_path):
    global SAVE_PATH, WVD_PATH
    SAVE_PATH = Path(downloads_path)
    WVD_PATH = wvd_device_path
    SAVE_PATH.mkdir(exist_ok=True, parents=True)
    session.proxies.clear()
    proxy_url = current_proxy_url()
    if proxy_url:
        session.proxies.update({'http': proxy_url, 'https': proxy_url})

def clean_url(url):
    parsed = urlparse(url)
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path.rstrip('/'), '', '', ''))

def series_slug_from_url(url):
    parts = [part for part in urlparse(url).path.split('/') if part]
    if len(parts) < 2 or parts[0] != 'shows':
        raise ValueError("Invalid U URL.")
    return parts[1]

def normalize_series_url(url):
    slug = series_slug_from_url(url)
    return f"https://u.co.uk/shows/{slug}/watch-online"

def is_episode_url(url):
    path = urlparse(url).path
    return bool(re.search(r'/series-\d+/episode-\d+/\d+', path) or re.search(r'/watch-online/\d+', path))

def get_series_api_url(series_slug):
    return f"https://vschedules.uktv.co.uk/vod/brand/?slug={series_slug}"

def get_brand_data(series_slug):
    try:
        response = session.get(get_series_api_url(series_slug), timeout=30)
        response.raise_for_status()
        return response.json()
    except requests.RequestException as exc:
        raise RuntimeError(f"Failed to fetch U brand metadata: {exc}") from exc

def get_all_series_ids(series_slug):
    data = get_brand_data(series_slug)
    series_info = data.get('series', [])
    if not series_info:
        raise ValueError("No U series found.")
    return [{'id': series['id'], 'number': series['number']} for series in series_info]

def standalone_episode_from_brand(series_slug):
    data = get_brand_data(series_slug)
    landing_episode = data.get('landing_episode') or {}
    if not landing_episode:
        return None

    if data.get('is_feature') or data.get('available_episodes') == 1 or landing_episode.get('available_episodes') == 1:
        return landing_episode

    return None

def is_standalone_show_url(url):
    if is_episode_url(url):
        return False
    try:
        return standalone_episode_from_brand(series_slug_from_url(url)) is not None
    except Exception:
        return False

def get_episode_data(series_id):
    try:
        response = session.get(f"https://vschedules.uktv.co.uk/vod/series/?id={series_id}", timeout=30)
        response.raise_for_status()
        return response.json()
    except requests.RequestException as exc:
        raise RuntimeError(f"Failed to fetch U episode metadata: {exc}") from exc

def build_episode_url(series_slug, series_number, episode):
    video_id = episode.get('video_id')
    episode_number = episode.get('episode_number')
    return f"https://u.co.uk/shows/{series_slug}/series-{series_number}/episode-{episode_number}/{video_id}"

def collect_episode_items(series_url, show_progress=True):
    series_slug = series_slug_from_url(normalize_series_url(series_url))
    episode_items = []
    show_title = None

    for series in get_all_series_ids(series_slug):
        episode_data = get_episode_data(series['id'])
        show_title = show_title or episode_data.get('brand', {}).get('name', 'U')
        for episode in episode_data.get('episodes', []):
            video_id = episode.get('video_id')
            episode_number = episode.get('episode_number')
            if not video_id or episode_number in (None, ''):
                continue

            episode_items.append({
                'url': build_episode_url(series_slug, series['number'], episode),
                'id': str(video_id),
                'episode': episode,
                'show_title': show_title,
                'sort_season': int(series['number']),
                'sort_episode': int(episode_number),
            })

    if show_progress and show_title:
        print(f"{icons.ICON_INFO} {bcolors.LIGHTBLUE}Show: {bcolors.ENDC}{show_title}")

    episode_items.sort(key=episode_sort_key)
    return episode_items

def episode_sort_key(item):
    return (item.get('sort_season', 9999), item.get('sort_episode', 9999), item.get('id', ''))

def episode_series_number(item):
    return item.get('sort_season')

def episode_number(item):
    return item.get('sort_episode')

def episode_tree_label(item):
    number = episode_number(item)
    title = item['episode'].get('name') or 'Unknown'
    return str(number).zfill(2) if number is not None else "-", title

def clean_text(value):
    if value is None:
        return ""
    value = re.sub(r"<[^>]+>", " ", str(value))
    return re.sub(r"\s+", " ", value).strip()

def format_info_date(value):
    value = clean_text(value)
    if not value:
        return ""
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").strftime("%d %B %Y %I:%M %p")
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
            sample_rate = clean_text(representation.get("audioSamplingRate") or adaptation.get("audioSamplingRate"))
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

def max_height_from_streams(streams, default="720"):
    heights = []
    for stream in streams:
        if stream.get("type") != "Vid":
            continue
        match = re.search(r"x(\d+)", stream.get("resolution") or "")
        if match:
            heights.append(int(match.group(1)))
    return str(max(heights)) if heights else str(default)

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
        raise ValueError(f"No U episodes found for selector {format_download_selector(parsed_selector)}.")

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
        print(f"{icons.ICON_FAILURE} {bcolors.FAIL}No U episodes found.{bcolors.ENDC}")
        return

    show_title = episode_items[0].get('show_title', 'U')
    grouped_items = group_episode_items_by_series(episode_items)
    group_labels = sorted(grouped_items, key=series_group_sort_key)
    series_summary = ",  ".join(f"{label}({len(grouped_items[label])})" for label in group_labels)

    print(f"{icons.ICON_SUCCESS} {bcolors.OKGREEN}Found {len(episode_items)} U episodes{bcolors.ENDC}")
    print()
    print_series_rule("U Series", show_title)
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
    export_dir = Path(__file__).resolve().parents[2] / "export"
    export_dir.mkdir(parents=True, exist_ok=True)
    title = episode_items[0].get('show_title', 'u') if episode_items else 'u'
    output_path = export_dir / f"{re.sub(r'[^A-Za-z0-9._-]+', '_', title).strip('._') or 'u'}_episodes.txt"
    output_path.write_text("\n".join(item['url'] for item in episode_items) + "\n", encoding="utf-8")
    print(f"{icons.ICON_SUCCESS} {bcolors.OKGREEN}Exported list: {output_path}{bcolors.ENDC}")

def get_video_id_and_info_from_url(video_url):
    # Extract video ID, series, episode, and show name from the URL
    video_id_match = re.search(r'/episode-\d+/(\d+)', video_url)
    series_match = re.search(r'/series-(\d+)/', video_url)
    episode_match = re.search(r'/episode-(\d+)/', video_url)
    show_name_match = re.search(r'/shows/([^/]+)/', video_url)
    
    if video_id_match and series_match and episode_match and show_name_match:
        video_id = video_id_match.group(1)
        series = series_match.group(1)
        episode = episode_match.group(1)
        # Capitalize the first letter of each word in the show name
        show_name = show_name_match.group(1).replace("-", ".").title()
    elif show_name_match and re.search(r'/watch-online/(\d+)', video_url):
        video_id = re.search(r'/watch-online/(\d+)', video_url).group(1)
        series_slug = show_name_match.group(1)
        try:
            matching_item = next((item for item in collect_episode_items(f"https://u.co.uk/shows/{series_slug}", show_progress=False) if item['id'] == video_id), None)
        except ValueError:
            matching_item = None
        if not matching_item:
            landing_episode = standalone_episode_from_brand(series_slug)
            if not landing_episode or str(landing_episode.get('video_id')) != video_id:
                raise ValueError("Could not match watch-online episode URL to series metadata.")
            series = str(landing_episode.get('series_number') or 1)
            episode = str(landing_episode.get('episode_number') or 1)
            show_name = (landing_episode.get('brand_name') or landing_episode.get('name') or series_slug.replace("-", ".").title()).replace(" ", ".")
        else:
            series = str(episode_series_number(matching_item))
            episode = str(episode_number(matching_item))
            show_name = matching_item.get('show_title', series_slug.replace("-", ".").title()).replace(" ", ".")
    elif show_name_match:
        series_slug = show_name_match.group(1)
        landing_episode = standalone_episode_from_brand(series_slug)
        if not landing_episode:
            raise ValueError("Could not extract necessary information from the video URL.")
        video_id = str(landing_episode['video_id'])
        series = str(landing_episode.get('series_number') or 1)
        episode = str(landing_episode.get('episode_number') or 1)
        show_name = (landing_episode.get('brand_name') or landing_episode.get('name') or series_slug.replace("-", ".").title()).replace(" ", ".")
    else:
        raise ValueError("Could not extract necessary information from the video URL.")
    
    return video_id, series, episode, show_name

def find_episode_metadata(video_url, video_id):
    try:
        series_slug = series_slug_from_url(video_url)
    except ValueError:
        return {}
    try:
        for item in collect_episode_items(f"https://u.co.uk/shows/{series_slug}", show_progress=False):
            if str(item.get("id")) == str(video_id):
                episode = item.get("episode") or {}
                return {
                    "show": item.get("show_title") or "",
                    "title": episode.get("name") or "",
                    "date_aired": format_info_date(episode.get("available_start")),
                    "description": episode.get("synopsis") or "",
                    "episode": episode,
                    "season": episode_series_number(item),
                    "episode_number": episode_number(item),
                }
    except Exception:
        pass
    return {}

def fetch_brightcove_video(video_id):
    try:
        response = session.get(BRIGHTCOVE_API(video_id), headers=BRIGHTCOVE_HEADERS, timeout=30)
        response.raise_for_status()
        return response.json()
    except requests.RequestException as exc:
        raise RuntimeError(f"Failed to fetch Brightcove playback data: {exc}") from exc

def build_save_name(show_name, series, episode, max_resolution):
    return f"{show_name}.S{int(series):02}E{int(episode):02}.{max_resolution}p.U.WEB-DL.AAC2.0.H.264"

def build_download_command(manifest_url, source_type, save_name, keys=None, interactive=False, quality=None, save_subs=False):
    if interactive:
        selectors = ""
    else:
        subtitle_selector = "--select-subtitle all" if save_subs else "--drop-subtitle all"
        selectors = f"{video_selector(quality)} --select-audio best {subtitle_selector} "
    command = (
        f'N_m3u8DL-RE "{manifest_url}" '
        f"{selectors}"
        f'-mt -M format=mkv --save-name "{save_name}" --save-dir "{SAVE_PATH}" '
    )
    if keys:
        command += "--key " + " --key ".join(keys)
    return append_downloader_proxy(command)

def resolve_playback(video_url, interactive=False, quality=None, save_subs=False):
    video_id, series, episode, show_name = get_video_id_and_info_from_url(video_url)
    response = fetch_brightcove_video(video_id)
    sources = response.get("sources") or []
    if not sources:
        raise ValueError("No Brightcove sources found in the response.")

    source = next((src for src in sources if 'key_systems' in src and 'com.widevine.alpha' in src['key_systems']), None)
    encrypted = bool(source)
    lic_url = ""
    pssh = None
    keys = []
    if encrypted:
        manifest_url = source['src']
        lic_url = source['key_systems']['com.widevine.alpha']['license_url']
        pssh = get_pssh(manifest_url)
        max_resolution = get_max_resolution(manifest_url)
        if pssh:
            keys = get_keys(pssh, lic_url)
    else:
        source = next((src for src in sources if 'src' in src and ('master.m3u8' in src['src'] or '.m3u8' in src['src'])), None)
        if not source:
            raise ValueError("No suitable encrypted or unencrypted Brightcove source found.")
        manifest_url = source['src']
        max_resolution = "720"

    try:
        manifest_response = session.get(manifest_url, timeout=30)
        manifest_response.raise_for_status()
        manifest_text = manifest_response.text
        streams, manifest_type = parse_manifest_streams(manifest_text)
        max_resolution = max_height_from_streams(streams, str(max_resolution))
    except requests.RequestException as exc:
        raise RuntimeError(f"Failed to fetch the manifest: {exc}") from exc

    metadata = find_episode_metadata(video_url, video_id)
    save_name = build_save_name(show_name, series, episode, max_resolution)
    save_name = apply_quality_to_filename(save_name, quality)
    return {
        "video_id": video_id,
        "series": series,
        "episode_number": episode,
        "show_name": show_name,
        "metadata": metadata,
        "manifest_url": manifest_url,
        "manifest_type": manifest_type,
        "manifest_text": manifest_text,
        "streams": streams,
        "license_url": lic_url,
        "pssh": pssh,
        "keys": list(dict.fromkeys(keys)),
        "encrypted": encrypted,
        "save_name": save_name,
        "download_command": build_download_command(manifest_url, manifest_type, save_name, keys, interactive=interactive, quality=quality, save_subs=save_subs),
    }

# Function to get PSSH from MPD URL
def get_pssh(url_mpd):
    response = session.get(url_mpd, timeout=30)
    response.raise_for_status()
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
    
    # Headers for the license request
    headers = {
        'Content-Type': 'application/octet-stream',
        'Origin': 'https://u.co.uk',
        'Referer': 'https://u.co.uk',
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:129.0) Gecko/20100101 Firefox/129.0',
    }

    # Make the license request
    licence = session.post(lic_url, headers=headers, data=challenge, timeout=30)
    licence.raise_for_status()

    # Parse the license response
    cdm.parse_license(session_id, licence.content)
    keys = [f"{key.kid.hex}:{key.key.hex()}" for key in cdm.get_keys(session_id) if key.type == 'CONTENT']
    cdm.close(session_id)
    return keys

def get_max_resolution(url_mpd):
    response = session.get(url_mpd, timeout=30)
    response.raise_for_status()
    root = ET.fromstring(response.content)
    
    # Extract heights from Representation elements that have a 'height' attribute
    heights = [int(rep.get('height')) for rep in root.findall(".//{urn:mpeg:dash:schema:mpd:2011}Representation") if rep.get('height') is not None]
    
    # Return the maximum height found, or "Unknown" if no heights are found
    return max(heights) if heights else "Unknown"

def print_episode_metadata(playback):
    metadata = playback.get("metadata") or {}
    rows = [
        ("Show", clean_text(metadata.get("show") or playback.get("show_name", "").replace(".", " "))),
        ("Title", clean_text(metadata.get("title"))),
        ("Date Aired", clean_text(metadata.get("date_aired"))),
        ("Description", clean_text(metadata.get("description"))),
    ]
    print(f"\n{bcolors.YELLOW}Episode metadata:{bcolors.ENDC}")
    for label, value in rows:
        if value:
            print(f"{bcolors.LIGHTBLUE}{label}: {bcolors.ENDC}{value}")

def print_playback_details(playback):
    label = "MPD URL" if playback["manifest_type"] == "DASH" else "M3U8 URL"
    print(f"{bcolors.LIGHTBLUE}{label}: {bcolors.ENDC}{playback['manifest_url']}")
    if playback.get("license_url"):
        print(f"{bcolors.RED}License URL: {bcolors.ENDC}{playback['license_url']}")
    if playback.get("pssh"):
        print(f"{bcolors.LIGHTBLUE}PSSH: {bcolors.ENDC}{playback['pssh']}")
    for key in playback.get("keys") or []:
        print(f"{bcolors.GREEN}KEYS: {bcolors.ENDC}--key {key}")

def info(video_url):
    if not is_episode_url(video_url):
        raise ValueError("Info mode requires a U episode/video URL.")
    spinner = Spinner()
    spinner.start()
    try:
        playback = resolve_playback(video_url)
    except Exception:
        spinner.stop()
        raise
    spinner.stop()

    print(f"{bcolors.LIGHTBLUE}{playback['manifest_type']} Manifest URL: {bcolors.ENDC}{playback['manifest_url']}")
    for key in playback.get("keys") or []:
        print(f"{bcolors.GREEN}KEYS: {bcolors.ENDC}--key {key}")
    print_streams(playback["streams"])
    print_episode_metadata(playback)
    print(f"\n{bcolors.YELLOW}Suggested filename: {bcolors.ENDC}{playback['save_name']}.mkv")

# Function to process and print the download command
def get_download_command(video_url, auto_download=False, interactive=False, quality=None, save_subs=False):
    spinner = Spinner()
    spinner.start()
    try:
        playback = resolve_playback(video_url, interactive=interactive, quality=quality, save_subs=save_subs)
    except Exception:
        spinner.stop()
        raise
    spinner.stop()

    print_playback_details(playback)
    print(f"{bcolors.YELLOW}DOWNLOAD COMMAND:{bcolors.ENDC}")
    print(mask_proxy_command(playback["download_command"]))

    if auto_download:
        print(f"{icons.ICON_WAITING} {bcolors.OKBLUE}Downloading video...{bcolors.ENDC}")
        subprocess.run(playback["download_command"], shell=True)
        return

    user_input = input("Do you wish to download? Y or N: ").strip().lower()
    if user_input == 'y':
        subprocess.run(playback["download_command"], shell=True)

def download_selected_episodes(series_url, selector, quality=None, auto_confirm=False, save_subs=False):
    print(f"{icons.ICON_WAITING} {bcolors.LIGHTBLUE}Retrieving series information.....{bcolors.ENDC}")
    episode_items = select_episode_items(series_url, selector)
    print_download_queue(episode_items)

    episode_word = "episode" if len(episode_items) == 1 else "episodes"
    this_or_these = "this" if len(episode_items) == 1 else "these"
    if not confirm_download(f"Do you wish to download {this_or_these} {len(episode_items)} {episode_word}? Y or N: ", auto_confirm=auto_confirm):
        print(f"{icons.ICON_FAILURE} {bcolors.RED}Download cancelled{bcolors.ENDC}")
        return

    for index, item in enumerate(episode_items, start=1):
        _, title = episode_tree_label(item)
        print(f"\n{icons.ICON_INFO} {bcolors.LIGHTBLUE}Downloading {index}/{len(episode_items)}: {title}{bcolors.ENDC}")
        get_download_command(item['url'], auto_download=True, quality=quality, save_subs=save_subs)

def main(video_url, downloads_path, wvd_device_path, mode="auto", export_list=False, download_selector=None, quality=None, auto_confirm=False, save_subs=False):
    if not video_url or not downloads_path or not wvd_device_path:
        raise ValueError("Eurovine config requires URL, downloads_path, and wvd_device_path for U.")
    configure_service(downloads_path, wvd_device_path)
    video_url = video_url.strip()
    try:
        if mode == "list":
            if is_episode_url(video_url):
                print(f"{icons.ICON_FAILURE} {bcolors.FAIL}List mode requires a U series URL, not an episode URL.{bcolors.ENDC}")
                return
            episode_items = collect_episode_items(video_url, show_progress=False)
            list_episode_items(episode_items)
            if export_list:
                export_episode_urls(episode_items)
            return

        if mode == "info":
            try:
                info(video_url)
            except Exception as exc:
                print(f"{icons.ICON_FAILURE} {bcolors.FAIL}{exc}{bcolors.ENDC}")
            return

        if mode == "download":
            if is_episode_url(video_url):
                print(f"{icons.ICON_FAILURE} {bcolors.FAIL}Download selector mode requires a U series URL, not an episode URL.{bcolors.ENDC}")
                return
            download_selected_episodes(video_url, download_selector, quality, auto_confirm, save_subs=save_subs)
            return

        if is_episode_url(video_url):
            try:
                get_download_command(video_url, auto_download=auto_confirm, interactive=(mode == "interactive"), quality=quality, save_subs=save_subs)
            except Exception as exc:
                print(f"{icons.ICON_FAILURE} {bcolors.FAIL}{exc}{bcolors.ENDC}")
            return

        if is_standalone_show_url(video_url):
            try:
                get_download_command(video_url, auto_download=auto_confirm, interactive=(mode == "interactive"), save_subs=save_subs)
            except Exception as exc:
                print(f"{icons.ICON_FAILURE} {bcolors.FAIL}{exc}{bcolors.ENDC}")
            return

        print(f"{icons.ICON_WARNING} {bcolors.WARNING}Series URLs require a flag. Use --list/-l to list episodes or --download/-d SELECTOR to download selected episodes.{bcolors.ENDC}")

    except Exception as e:
        print(f"{bcolors.FAIL}Error: {e}{bcolors.ENDC}")
