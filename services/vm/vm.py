import re
import requests
from bs4 import BeautifulSoup
import xml.etree.ElementTree as ET
import base64
import binascii
from pywidevine.cdm import Cdm
from pywidevine.device import Device
from pywidevine.pssh import PSSH
from pathlib import Path
import json
import subprocess
import sys
import urllib3
import uuid
import shutil
from datetime import datetime
from urllib.parse import urlparse, urlunparse
import icons
from colors import bcolors
from quality_utils import apply_quality_to_filename, video_selector
from services.proxy import current_proxy_url
from beaupy.spinners import Spinner

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

CONFIG_PATH = Path(__file__).resolve().parents[2] / "config.yaml"
config = {}
SAVE_PATH = None
WVD_PATH = None


def build_service_proxy(service_name):
    service_proxy = (config.get("service_proxies") or {}).get(service_name) or {}
    if not service_proxy.get("enabled"):
        return None

    provider_name = service_proxy.get("provider")
    country = service_proxy.get("country")
    provider = (config.get("proxy_providers") or {}).get(provider_name) or {}
    server_template = (provider.get("server_map") or {}).get(country)
    username = provider.get("username")
    password = provider.get("password")

    if not server_template or not username or not password:
        raise ValueError(f"Proxy is enabled for {service_name}, but provider, country, or credentials are missing.")

    return (
        server_template
        .replace("username", username)
        .replace("password", password)
    )


def mask_proxy(proxy_text):
    if not proxy_text:
        return ""
    return re.sub(r"//[^:@/]+:[^@/]+@", "//***:***@", proxy_text)


session = requests.Session()
VM_PROXY = None

def configure_service(downloads_path, wvd_device_path, device_id=None):
    global SAVE_PATH, WVD_PATH, VM_PROXY, config
    SAVE_PATH, WVD_PATH = Path(downloads_path), wvd_device_path
    config = {DEVICE_ID_CONFIG_KEY: device_id} if device_id else {}
    VM_PROXY = current_proxy_url()
    session.proxies.clear()
    session.verify = True
    if VM_PROXY:
        session.proxies.update({"http": VM_PROXY, "https": VM_PROXY})
        session.verify = False


DEVICE_ID_CONFIG_KEY = "vmplayer_device_id"


def save_device_id_to_config(value):
    try:
        lines = CONFIG_PATH.read_text(encoding="utf-8").splitlines()
        key_pattern = re.compile(rf"^{re.escape(DEVICE_ID_CONFIG_KEY)}\s*:")

        for index, line in enumerate(lines):
            if key_pattern.match(line):
                lines[index] = f"{DEVICE_ID_CONFIG_KEY}: {value}"
                break
        else:
            if lines and lines[-1]:
                lines.append("")
            lines.append(f"{DEVICE_ID_CONFIG_KEY}: {value}")

        CONFIG_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
        config[DEVICE_ID_CONFIG_KEY] = value
    except Exception:
        pass


def get_device_id():
    value = str(config.get(DEVICE_ID_CONFIG_KEY) or "").strip()
    if value:
        return value

    value = str(uuid.uuid4())
    save_device_id_to_config(value)
    return value


def json_response(response, label):
    content_type = response.headers.get("Content-Type", "")
    if "json" not in content_type.lower():
        preview = response.text[:300].replace("\r", " ").replace("\n", " ")
        raise ValueError(
            f"{label} returned non-JSON response "
            f"({response.status_code}, {content_type or 'no content-type'}): {preview}"
        )
    try:
        return response.json()
    except json.JSONDecodeError as exc:
        preview = response.text[:300].replace("\r", " ").replace("\n", " ")
        raise ValueError(f"{label} returned invalid JSON: {preview}") from exc


API_KEY = "821254297041614280861178657602"
COMPANY_ID = "company_942f683c-9041-42de-9911-a9e4cd98a4e9"
COUNTRY = "IE"
PLATFORM = "chrome"


def clean_url(url):
    parsed = urlparse(url)
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", "", ""))


def is_episode_url(url):
    path = urlparse(url).path
    return bool(re.search(r"/(?:watch/)?(?:vod|replay)/\d+", path))


def is_series_url(url):
    return "/shows/" in urlparse(url).path


def extract_series_uuid(url):
    match = re.search(r"/shows/([0-9a-f-]{36})", urlparse(url).path, re.IGNORECASE)
    return match.group(1) if match else None


def _to_int(value, default=0):
    try:
        if value is None:
            return default
        if isinstance(value, int):
            return value
        match = re.search(r"\d+", str(value))
        return int(match.group(0)) if match else int(value)
    except Exception:
        return default


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
    try:
        root = ET.fromstring(manifest_content)
    except ET.ParseError as exc:
        raise ValueError(f"Unable to parse the DASH manifest: {exc}") from exc

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


def first_value(mapping, *keys):
    for key in keys:
        value = mapping.get(key) if isinstance(mapping, dict) else None
        if value not in (None, "", []):
            return value
    return None


def format_airdate(iso_str):
    if not iso_str:
        return "Unknown"
    text = str(iso_str)
    dt = None
    for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            dt = datetime.strptime(text, fmt)
            break
        except ValueError:
            continue
    if dt is None:
        return text
    day = dt.day
    if 11 <= day <= 13:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")
    meridiem = "am" if dt.hour < 12 else "pm"
    return f"{day}{suffix} {dt.strftime('%B')} {dt.year} {dt.hour}.{dt.minute:02d}{meridiem}"


def print_episode_metadata(metadata):
    show = first_value(metadata, "series_title", "show_title", "programme_title", "brand_title")
    title = first_value(metadata, "title", "name")
    show = show or title
    if title and show and clean_text(title).lower() == clean_text(show).lower():
        episode_number_value = _to_int(first_value(metadata, "series_episode", "episode", "episode_number"), 0)
        if episode_number_value:
            title = f"Episode {episode_number_value:02d}"
    date_aired = first_value(metadata, "date_aired", "aired", "broadcast_date", "created_at", "created", "available_from", "start")
    description = first_value(metadata, "synopsis", "description", "short_description", "long_description")
    rows = [
        ("Show", clean_text(show or "Virgin Media")),
        ("Title", clean_text(title or "Unknown")),
        ("Date Aired", clean_text(format_airdate(date_aired))),
        ("Description", clean_text(description or "Unknown")),
    ]
    print(f"\n{bcolors.YELLOW}Episode metadata:{bcolors.ENDC}")
    for label, value in rows:
        if value:
            print(f"{bcolors.LIGHTBLUE}{label}: {bcolors.ENDC}{value}")


def get_metadata_for_series(series_uuid):
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-IE,en;q=0.9",
        "Origin": "https://play.virginmediatelevision.ie",
        "Referer": "https://play.virginmediatelevision.ie/",
        "Userid": get_device_id(),
        "X-Requested-With": "XMLHttpRequest",
    }
    params = {
        "key": API_KEY,
        "cc": COUNTRY,
        "lang": "en",
        "platform": PLATFORM,
    }
    candidates = [
        f"https://v6-metadata-cf.simplestreamcdn.com/api/series/{series_uuid}",
        f"https://v6-metadata.simplestreamcdn.com/api/series/{series_uuid}",
    ]
    for url in candidates:
        response = session.get(url, params=params, headers=headers, timeout=20)
        if response.status_code == 200 and "json" in response.headers.get("Content-Type", "").lower():
            return response.json()
    raise ValueError("Could not retrieve Virgin Media series metadata.")


def iterate_episode_nodes_from_series(meta, series_url):
    nodes = []
    series = (meta.get("response") or {}).get("series") or {}
    seasons = series.get("seasons") or []
    for season in seasons:
        season_number = _to_int(season.get("number") or season.get("season_number") or season.get("title"), 0)
        episodes = season.get("episodes") or season.get("tiles") or []
        for episode in episodes:
            video_id = episode.get("uvid") or episode.get("id") or episode.get("video_id") or episode.get("content_id") or episode.get("asset_id")
            if video_id is None:
                continue
            nodes.append({
                "id": str(video_id),
                "season_number": _to_int(episode.get("season") or episode.get("series_season"), season_number),
                "episode_number": _to_int(episode.get("episode") or episode.get("series_episode") or episode.get("episode_number"), 0),
                "title": episode.get("title") or episode.get("name") or series.get("title") or "Unknown",
                "description": episode.get("synopsis") or episode.get("description") or "",
                "show_title": episode.get("series_title") or series.get("title") or "Virgin Media",
                "url": clean_url(episode.get("url") or f"https://play.virginmediatelevision.ie/replay/{video_id}"),
            })
    return nodes


def collect_episode_items(series_url, show_progress=True):
    series_uuid = extract_series_uuid(series_url)
    if not series_uuid:
        raise ValueError("Could not extract Virgin Media series UUID from URL.")
    meta = get_metadata_for_series(series_uuid)
    episode_nodes = iterate_episode_nodes_from_series(meta, series_url)
    if not episode_nodes:
        raise ValueError("No Virgin Media episodes found.")

    episode_items = []
    for episode in episode_nodes:
        show_title = (episode.get("show_title") or "Virgin Media").strip()
        episode_items.append({
            "url": episode["url"],
            "id": episode["id"],
            "episode": episode,
            "show_title": show_title,
        })

    if show_progress and episode_items:
        print(f"{icons.ICON_INFO} {bcolors.LIGHTBLUE}Show: {bcolors.ENDC}{episode_items[0]['show_title']}")

    episode_items.sort(key=episode_sort_key)
    return episode_items


def episode_series_number(item):
    season = item["episode"].get("season_number")
    return int(season) if season not in (None, "") else None


def episode_number(item):
    episode = item["episode"].get("episode_number")
    return int(episode) if episode not in (None, "") else None


def episode_title(item):
    episode = item["episode"]
    title = (episode.get("title") or "").strip()
    show_title = (episode.get("show_title") or "").strip()
    if title and title != show_title:
        return title
    number = episode_number(item)
    return f"Episode {number:02d}" if number is not None else title or "Unknown"


def episode_tree_label(item):
    number = episode_number(item)
    return str(number).zfill(2) if number is not None else "-", episode_title(item)


def episode_sort_key(item):
    season = episode_series_number(item)
    episode = episode_number(item)
    return (
        season if season is not None else 9999,
        episode if episode is not None else 9999,
        item.get("id", ""),
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
        raise ValueError(f"No Virgin Media episodes found for selector {format_download_selector(parsed_selector)}.")

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
        print(f"{icons.ICON_FAILURE} {bcolors.FAIL}No Virgin Media episodes found.{bcolors.ENDC}")
        return

    show_title = episode_items[0].get("show_title", "Virgin Media")
    grouped_items = group_episode_items_by_series(episode_items)
    group_labels = sorted(grouped_items, key=series_group_sort_key)
    series_summary = ",  ".join(f"{label}({len(grouped_items[label])})" for label in group_labels)

    print(f"{icons.ICON_SUCCESS} {bcolors.OKGREEN}Found {len(episode_items)} Virgin Media episodes{bcolors.ENDC}")
    print()
    print_series_rule("Virgin Media Series", show_title)
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

# Step 1: Get the Video ID from the episode URL
def get_video_id_from_url(url):
    # Check for VOD URL format
    vod_match = re.search(r'/vod/(\d+)', url)
    if vod_match:
        return vod_match.group(1)
    
    # Check for Replay URL format
    replay_match = re.search(r'/replay/(\d+)', url)
    if replay_match:
        return replay_match.group(1)
    
    raise ValueError(f"{bcolors.FAIL}Could not extract video ID from URL.{bcolors.ENDC}")

# Step 2: Get the stream details including manifest and license URL
def get_stream_details(video_id, episode_url):
    api_key = "821254297041614280861178657602"
    api_url = f"https://api-virginmedia.simplestreamcdn.com/streams/v2/company_942f683c-9041-42de-9911-a9e4cd98a4e9/vod/{video_id}"
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-IE,en;q=0.9",
        "Referer": "https://play.virginmediatelevision.ie/",
        "Userid": get_device_id(),
        "Uvid": video_id,
        "Origin": "https://play.virginmediatelevision.ie",
        "X-Requested-With": "XMLHttpRequest",
    }
    
    params = {
        "key": api_key,
        "cc": "IE",
        "platform": "chrome",  # Modify platform as needed
        "user_hash": "undefined",
        "url": episode_url,
        "gdpr": "1",
        "gdpr_consent": "undefined"
    }

    response = session.get(api_url, headers=headers, params=params, timeout=20)
    response.raise_for_status()

    return json_response(response, "Stream API")

# Step 4: Extract PSSH from the manifest
def get_pssh_from_manifest(manifest_url):
    response = session.get(manifest_url, timeout=20)
    response.raise_for_status()

    # Parse the XML manifest
    root = ET.fromstring(response.content)

    NS_MPD = "{urn:mpeg:dash:schema:mpd:2011}"
    NS_CENC = "{urn:mpeg:cenc:2013}"
    widevine_uuid = "edef8ba9-79d6-4ace-a3c8-27dcd51d21ed"

    # 1) Prefer Widevine PSSH (short one) by schemeIdUri, case-insensitive
    for cp in root.findall(".//" + NS_MPD + "ContentProtection"):
        scheme = (cp.attrib.get("schemeIdUri") or "").lower()
        if widevine_uuid in scheme:
            pssh_el = cp.find(NS_CENC + "pssh")
            if pssh_el is not None and pssh_el.text:
                return pssh_el.text.strip()

    # 2) Fallback: any <cenc:pssh> in the MPD (first one wins)
    for pssh_el in root.findall(".//" + NS_CENC + "pssh"):
        if pssh_el.text:
            return pssh_el.text.strip()

    raise ValueError(f"{bcolors.FAIL}PSSH not found in the manifest.{bcolors.ENDC}")

# Step 5: Make the license request using PSSH
def get_keys(pssh, license_url):
    try:
        pssh = PSSH(pssh)
    except binascii.Error as e:
        print(f"{bcolors.FAIL}Could not decode PSSH data as Base64: {e}{bcolors.ENDC}")
        return []

    # Load the Widevine device from the config file path
    device = Device.load(WVD_PATH)
    cdm = Cdm.from_device(device)
    session_id = cdm.open()
    challenge = cdm.get_license_challenge(session_id, pssh)

    # Headers for the license request
    headers = {
        'Content-Type': 'application/octet-stream',
        'Origin': 'https://www.virginmediatelevision.ie',
        'Referer': 'https://www.virginmediatelevision.ie/',
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:129.0) Gecko/20100101 Firefox/129.0',
    }

    # Make the license request
    response = session.post(license_url, headers=headers, data=challenge, timeout=20)
    
    # Check for errors in the response
    try:
        response.raise_for_status()
    except requests.exceptions.HTTPError as e:
        print(f"{bcolors.FAIL}HTTPError: {e}{bcolors.ENDC}")
        print(f"Response Headers: {response.headers}")
        print(f"Response Text: {response.text}")
        raise

    # Parse the license response
    cdm.parse_license(session_id, response.content)
    keys = [f"{key.kid.hex}:{key.key.hex()}" for key in cdm.get_keys(session_id) if key.type == 'CONTENT']
    cdm.close(session_id)
    return keys

# Step 6: Get the maximum video resolution from the manifest
def get_max_resolution(manifest_url):
    response = session.get(manifest_url, timeout=20)
    response.raise_for_status()
    
    # Parse the XML manifest
    root = ET.fromstring(response.content)
    
    # Extract heights from Representation elements that have a 'height' attribute
    heights = [int(rep.get('height')) for rep in root.findall(".//{urn:mpeg:dash:schema:mpd:2011}Representation") if rep.get('height') is not None]
    
    # Return the maximum height found, or "Unknown" if no heights are found
    return max(heights) if heights else "Unknown"

# Step 7: Format video filename using show name, season, and episode
def format_filename(metadata, resolution):
    try:
        # Extract show name from the metadata
        title = metadata.get("title", "Unknown.Show")
        season = metadata.get("series_season", 1)
        episode = metadata.get("series_episode", 1)
    
        show_name = title.split(" Ep.")[0].replace(" ", ".")
    
        # Format the filename dynamically with the extracted season and episode numbers
        return f"{show_name}.S{int(season):02}E{int(episode):02}.{resolution}p.VirginMedia.WEB-DL.AAC2.0.H.264.mkv"
    except Exception as e:
        print(f"{bcolors.FAIL}Error formatting filename: {e}{bcolors.ENDC}")
        return "Unknown.Show.S01E01.1080p.VirginMedia.WEB-DL.AAC2.0.H.264.mkv"

# Step 8: Generate the download command
def generate_download_command(manifest_url, keys, filename, save_path, interactive=False, quality=None):
    keys_str = ' '.join([f'--key {key}' for key in keys])
    selectors = "" if interactive else f"{video_selector(quality)} --select-audio best --select-subtitle all "
    command = f'N_m3u8DL-RE "{manifest_url}" --save-name "{filename}" --save-dir "{save_path}" {selectors}-mt -M format=mkv {keys_str}'
    if VM_PROXY:
        command += f' --custom-proxy "{VM_PROXY}"'
    return command


def fetch_manifest(manifest_url):
    try:
        response = session.get(manifest_url, timeout=20)
        response.raise_for_status()
        return response.content
    except requests.RequestException as exc:
        raise RuntimeError(f"Failed to fetch Virgin Media manifest: {exc}") from exc


def get_pssh_from_manifest_content(manifest_content):
    root = ET.fromstring(manifest_content)
    ns_mpd = "{urn:mpeg:dash:schema:mpd:2011}"
    ns_cenc = "{urn:mpeg:cenc:2013}"
    widevine_uuid = "edef8ba9-79d6-4ace-a3c8-27dcd51d21ed"

    for cp in root.findall(".//" + ns_mpd + "ContentProtection"):
        scheme = (cp.attrib.get("schemeIdUri") or "").lower()
        if widevine_uuid in scheme:
            pssh_el = cp.find(ns_cenc + "pssh")
            if pssh_el is not None and pssh_el.text:
                return pssh_el.text.strip()

    for pssh_el in root.findall(".//" + ns_cenc + "pssh"):
        if pssh_el.text:
            return pssh_el.text.strip()

    raise ValueError(f"{bcolors.FAIL}PSSH not found in the manifest.{bcolors.ENDC}")


def max_resolution_from_streams(streams):
    heights = []
    for stream in streams:
        if stream.get("type") != "Vid":
            continue
        match = re.search(r"x(\d+)", stream.get("resolution") or "")
        if match:
            heights.append(int(match.group(1)))
    return max(heights) if heights else "Unknown"


def resolve_playback(video_url, interactive=False, quality=None):
    video_id = get_video_id_from_url(video_url)
    stream_details = get_stream_details(video_id, video_url)
    response = stream_details.get("response", {})
    drm = response.get("drm", {})
    widevine = drm.get("widevine", {})
    manifest_url = widevine.get("stream")
    license_url = widevine.get("licenseAcquisitionUrl")
    if not manifest_url:
        raise ValueError("Virgin Media stream response did not contain a Widevine manifest URL.")
    if not license_url:
        raise ValueError("Virgin Media stream response did not contain a Widevine license URL.")

    metadata = response.get("metadata", {}).get("metadata", {})
    manifest_content = fetch_manifest(manifest_url)
    streams, manifest_type = parse_manifest_streams(manifest_content)
    pssh = get_pssh_from_manifest_content(manifest_content)
    keys = get_keys(pssh, license_url)
    resolution = max_resolution_from_streams(streams)
    filename = format_filename(metadata, resolution)
    filename = apply_quality_to_filename(filename, quality)
    download_command = generate_download_command(manifest_url, keys, filename, SAVE_PATH, interactive=interactive, quality=quality)

    return {
        "video_id": video_id,
        "stream_details": stream_details,
        "manifest_url": manifest_url,
        "manifest_type": manifest_type,
        "license_url": license_url,
        "metadata": metadata,
        "manifest_content": manifest_content,
        "streams": streams,
        "pssh": pssh,
        "keys": keys,
        "resolution": resolution,
        "filename": filename,
        "download_command": download_command,
    }


def info(video_url):
    if not is_episode_url(video_url):
        raise ValueError("Info mode requires a Virgin Media episode/video URL.")

    spinner = Spinner()
    spinner.start()
    try:
        resolved = resolve_playback(video_url)
    except Exception:
        spinner.stop()
        raise
    else:
        spinner.stop()

    print(f"{bcolors.LIGHTBLUE}{resolved['manifest_type']} Manifest URL: {bcolors.ENDC}{resolved['manifest_url']}")
    keys = resolved.get("keys") or []
    if keys:
        print(f"{bcolors.OKGREEN}KEYS: {bcolors.ENDC}{' '.join(f'--key {key}' for key in keys)}")
    print_streams(resolved["streams"])
    print_episode_metadata(resolved["metadata"])
    print(f"\n{bcolors.YELLOW}Suggested filename: {bcolors.ENDC}{resolved['filename']}")


def process_video(video_url, auto_download=False, interactive=False, quality=None):
    try:
        spinner = Spinner()
        spinner.start()
        try:
            resolved = resolve_playback(video_url, interactive=interactive, quality=quality)
        except Exception:
            spinner.stop()
            raise
        else:
            spinner.stop()

        print(f"{bcolors.LIGHTBLUE}MPD URL: {bcolors.ENDC}{resolved['manifest_url']}")
        print(f"{bcolors.RED}License URL: {bcolors.ENDC}{resolved['license_url']}")
        print(f"{bcolors.LIGHTBLUE}PSSH: {bcolors.ENDC}{resolved['pssh']}")
        print(f"{bcolors.OKGREEN}KEYS:{bcolors.ENDC}{resolved['keys']}")
        print(f"{bcolors.YELLOW}DOWNLOAD COMMAND: {bcolors.ENDC}{mask_proxy(resolved['download_command'])}")

        if auto_download:
            print(f"{icons.ICON_WAITING} {bcolors.OKBLUE}Downloading video...{bcolors.ENDC}")
            subprocess.run(resolved["download_command"], shell=True)
            return True

        user_input = input("Do you wish to download? Y or N: ").strip().lower()
        if user_input == 'y':
            subprocess.run(resolved["download_command"], shell=True)
            return True
        return False

        video_id = get_video_id_from_url(video_url)

        if video_id:
            # Step 2: Get the stream details including manifest and license URL
            stream_details = get_stream_details(video_id, video_url)

            # Extract manifest and license URL
            manifest_url = stream_details.get("response", {}).get("drm", {}).get("widevine", {}).get("stream")
            license_url = stream_details.get("response", {}).get("drm", {}).get("widevine", {}).get("licenseAcquisitionUrl")

            print(f"{bcolors.LIGHTBLUE}MPD URL: {bcolors.ENDC}{manifest_url}")
            print(f"{bcolors.RED}License URL: {bcolors.ENDC}{license_url}")

            # Extract the metadata from the response
            metadata = stream_details.get("response", {}).get("metadata", {}).get("metadata", {})

            # Step 4: Extract PSSH from the manifest
            pssh = get_pssh_from_manifest(manifest_url)
            print(f"{bcolors.LIGHTBLUE}PSSH: {bcolors.ENDC}{pssh}")

            # Step 5: Make the license request and get keys
            keys = get_keys(pssh, license_url)
            print(f"{bcolors.OKGREEN}KEYS:{bcolors.ENDC}{keys}")

            # Step 6: Get the maximum video resolution from the manifest
            resolution = get_max_resolution(manifest_url)

            # Step 7: Formulate video filename using extracted season and episode
            filename = format_filename(metadata, resolution)

            # Step 8: Generate the download command
            download_command = generate_download_command(manifest_url, keys, filename, SAVE_PATH, interactive=interactive)
            print(f"{bcolors.YELLOW}DOWNLOAD COMMAND: {bcolors.ENDC}{mask_proxy(download_command)}")

            if auto_download:
                print(f"{icons.ICON_WAITING} {bcolors.OKBLUE}Downloading video...{bcolors.ENDC}")
                subprocess.run(download_command, shell=True)
                return True

            user_input = input("Do you wish to download? Y or N: ").strip().lower()
            if user_input == 'y':
                subprocess.run(download_command, shell=True)
                return True
            return False
                         
    except Exception as e:
        print(f"{bcolors.FAIL}Error: {e}{bcolors.ENDC}")
        return False


def download_selected_episodes(series_url, selector, quality=None):
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
        process_video(item["url"], auto_download=True, quality=quality)


def safe_filename(value):
    return re.sub(r"[^A-Za-z0-9._-]+", "_", str(value or "virgin_media")).strip("._") or "virgin_media"


def export_episode_urls(series_url, episode_items=None):
    if episode_items is None:
        episode_items = collect_episode_items(series_url, show_progress=False)
    if not episode_items:
        print(f"{icons.ICON_FAILURE} {bcolors.FAIL}No Virgin Media episodes found to export.{bcolors.ENDC}")
        return

    export_dir = Path(__file__).resolve().parents[2] / "export"
    export_dir.mkdir(parents=True, exist_ok=True)
    show_title = episode_items[0].get("show_title", "Virgin Media")
    output_path = export_dir / f"vm_{safe_filename(show_title)}_episodes.txt"
    output_path.write_text("\n".join(item["url"] for item in episode_items) + "\n", encoding="utf-8")
    print(f"{icons.ICON_SUCCESS} {bcolors.OKGREEN}Exported {len(episode_items)} Virgin Media episode URLs to:{bcolors.ENDC} {output_path}")


def eurovine_main(video_url, downloads_path, wvd_device_path, device_id=None, mode="auto", export_list=False, download_selector=None, quality=None):
    configure_service(downloads_path, wvd_device_path, device_id)
    video_url = str(video_url or "").strip()

    if not video_url:
        print(f"{icons.ICON_FAILURE} {bcolors.FAIL}No Virgin Media URL provided.{bcolors.ENDC}")
        return

    print(f"{icons.ICON_INFO} {bcolors.LIGHTBLUE}Virgin Media URL: {bcolors.ENDC}{video_url}")

    if export_list:
        if is_episode_url(video_url):
            print(f"{icons.ICON_FAILURE} {bcolors.FAIL}Export mode requires a Virgin Media series URL, not an episode URL.{bcolors.ENDC}")
            return
        try:
            episode_items = collect_episode_items(video_url, show_progress=False)
            if mode == "list":
                list_episode_items(episode_items)
                print()
            export_episode_urls(video_url, episode_items)
        except ValueError as exc:
            print(f"{icons.ICON_FAILURE} {bcolors.FAIL}{exc}{bcolors.ENDC}")
        return

    if mode == "list":
        if is_episode_url(video_url):
            print(f"{icons.ICON_FAILURE} {bcolors.FAIL}List mode requires a Virgin Media series URL, not an episode URL.{bcolors.ENDC}")
            return
        try:
            list_episode_items(collect_episode_items(video_url, show_progress=False))
        except ValueError as exc:
            print(f"{icons.ICON_FAILURE} {bcolors.FAIL}{exc}{bcolors.ENDC}")
        return

    if mode == "info":
        try:
            info(video_url)
        except Exception as exc:
            print(f"{icons.ICON_FAILURE} {bcolors.FAIL}{exc}{bcolors.ENDC}")
        return

    if mode == "download" or download_selector:
        if is_episode_url(video_url):
            print(f"{icons.ICON_FAILURE} {bcolors.FAIL}Download selector mode requires a Virgin Media series URL, not an episode URL.{bcolors.ENDC}")
            return
        try:
            download_selected_episodes(video_url, download_selector, quality)
        except ValueError as exc:
            print(f"{icons.ICON_FAILURE} {bcolors.FAIL}{exc}{bcolors.ENDC}")
        return

    if is_episode_url(video_url):
        process_video(video_url, interactive=(mode == "interactive"), quality=quality)
        return

    print(f"{icons.ICON_WARNING} {bcolors.WARNING}Series URLs require a flag. Use -l to list episodes, -x to export episode URLs, or -d SELECTOR to download selected episodes.{bcolors.ENDC}")


main = eurovine_main


if __name__ == "__main__":
    print(f"{icons.ICON_INFO} {bcolors.LIGHTBLUE}Run Virgin Media through eurovine.py from the Eurovine root folder.{bcolors.ENDC}")


