import requests
import tempfile
import base64
from urllib.parse import urlparse, urlunparse
from pywidevine.pssh import PSSH
from pywidevine.device import Device
from pywidevine.cdm import Cdm
import subprocess
import re
import os
import sys
import shutil
import ssl
import urllib3
from datetime import datetime, timezone
from urllib3 import poolmanager
from requests.adapters import HTTPAdapter
from OpenSSL import SSL
import xml.etree.ElementTree as ET  
from beaupy.spinners import Spinner
import icons
from colors import bcolors
from pathlib import Path
from services.proxy import append_downloader_proxy, current_proxy_url, mask_proxy_command

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

#   Ozivine: My5 Video Downloader
#   Author: billybanana
#   Usage: enter the episode URL to retrieve the MPD, Licence, PSSH and Decryption keys.
#   eg: https://www.channel5.com/show/the-hotel-inspector/season-19/C5455370012 or movie/single episode shows https://www.channel5.com/show/the-abduction-of-milly-dowler
#   Authentication: None
#   Geo-Locking: requires a UK IP address
#   Quality: up to 1080p
#   Key Features:
#   1. Extract Video ID: Parses the My5 URL to extract the series name, season, and episode number.
#   2. Extract PSSH: Retrieves and parses the MPD file to generate the PSSH data necessary for Widevine decryption.
#   3. Fetch Decryption Keys: Uses the PSSH and license URL to request and retrieve the Widevine decryption keys.
#   4. Print Download Information: Outputs the MPD URL, license URL, PSSH, and decryption keys required for downloading and decrypting the video content.
#   5. Note: this script functions for encrypted video files only (My5 files are all currently encrypted).

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

session = requests.Session()
DOWNLOAD_DIR = None
WVD_PATH = None
MY5_CERTIFICATE = None
MY5_AUTH_URL = "https://cassie-auth.channel5.com/api/v2/media/my5androidhydradash/{title_id}.json"
MY5_EPISODE_URL = "https://corona.channel5.com/shows/{show}/seasons/{season}/episodes/{episode}.json?platform=my5android"
MY5_USER_AGENT = "Dalvik/2.1.0 (Linux; U; Android 14; SM-S901B Build/UP1A.231005.007)"
SERVICE_DIR = Path(__file__).resolve().parent
EUROVINE_DIR = SERVICE_DIR.parents[1]
EUROVINE_TEMP_DIR = EUROVINE_DIR / "temp"
EUROVINE_TEMP_DIR.mkdir(exist_ok=True, parents=True)

def configure_service(downloads_path, wvd_device_path, certificate):
    """Apply configuration supplied by the Eurovine organizer."""
    global DOWNLOAD_DIR, WVD_PATH, MY5_CERTIFICATE
    DOWNLOAD_DIR = downloads_path
    WVD_PATH = wvd_device_path
    MY5_CERTIFICATE = (certificate or "").strip()
    session.proxies.clear()
    proxy_url = current_proxy_url()
    if proxy_url:
        session.proxies.update({'http': proxy_url, 'https': proxy_url})


class SSLCiphersAdapter(HTTPAdapter):
    """
    Custom HTTP Adapter to change the TLS Cipher set and security requirements.
    This follows the logic used in the Devine `SSLCiphers` class.
    """
    def __init__(self, cipher_list: str = "DEFAULT", security_level: int = 0, *args, **kwargs):
        if "@SECLEVEL" in cipher_list:
            raise ValueError("You must not specify the Security Level manually in the cipher list.")
        if security_level not in range(6):
            raise ValueError(f"The security_level must be a value between 0 and 5, not {security_level}")
        
        # Append security level to cipher list
        cipher_list += f":@SECLEVEL={security_level}"
        
        # Create SSL context with custom ciphers
        ctx = ssl.create_default_context()
        ctx.set_ciphers(cipher_list)
        ctx.check_hostname = False
        
        self._ssl_context = ctx
        super().__init__(*args, **kwargs)

    def init_poolmanager(self, *args, **kwargs):
        kwargs['ssl_context'] = self._ssl_context
        return super().init_poolmanager(*args, **kwargs)

    def proxy_manager_for(self, *args, **kwargs):
        kwargs['ssl_context'] = self._ssl_context
        return super().proxy_manager_for(*args, **kwargs)

def get_playlist(asset_id: str) -> tuple:
    # Mount the custom adapter that allows weak ciphers with lower security level
    session.mount('https://', SSLCiphersAdapter(security_level=0))

    cert_path = None
    try:
        if not MY5_CERTIFICATE:
            raise ValueError("My5 certificate is not configured under my5.certificate in Eurovine config.yaml.")
        session.headers.update({"user-agent": MY5_USER_AGENT})

        # Decode the certificate and save it to a temporary file
        with tempfile.NamedTemporaryFile(delete=False, suffix=".pem", dir=EUROVINE_TEMP_DIR) as cert_file:
            cert_file.write(base64.b64decode(MY5_CERTIFICATE))
            cert_path = cert_file.name

        # Make the request with custom SSL cipher handling
        r = session.get(MY5_AUTH_URL.format(title_id=asset_id), cert=cert_path, timeout=30)
        r.raise_for_status()

        # Process the response
        data = r.json()
        if not data.get("assets"):
            raise ValueError(f"Could not find asset: {data}")

        # Get the Widevine DRM asset
        asset = [x for x in data["assets"] if x["drm"] == "widevine"][0]
        mpd_url = asset["renditions"][0]["url"]
        lic_url = asset["keyserver"]

        # Clean the MPD URL
        parse = urlparse(mpd_url)
        path = parse.path.split("/")
        path[-1] = path[-1].split("-")[0].split("_")[0]
        manifest = urlunparse(parse._replace(path="/".join(path)))
        manifest += ".mpd" if not manifest.endswith("mpd") else ""

        return manifest, lic_url
    except (requests.RequestException, KeyError, IndexError, ValueError) as ex:
        raise RuntimeError(f"Failed to get My5 playlist: {ex}") from ex
    finally:
        if cert_path:
            os.remove(cert_path)


def get_widevine_license(challenge: bytes, lic_url: str) -> bytes:
    try:
        r = session.post(lic_url, data=challenge, timeout=30)
        r.raise_for_status()
        return r.content
    except requests.RequestException as ex:
        raise RuntimeError(f"Failed to get Widevine license: {ex}") from ex

def clean_url(url):
    parsed = urlparse(url)
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path.rstrip('/'), '', '', ''))

def show_slug_from_url(url):
    path_segments = urlparse(url).path.strip("/").split("/")
    if len(path_segments) < 2 or path_segments[0] != "show":
        raise ValueError("Invalid My5 URL")
    return path_segments[1]

def is_episode_url(url):
    return bool(re.search(r'/season-\d+/', urlparse(url).path)) or bool(re.search(r'/C\d+', urlparse(url).path))

def build_episode_url(series_url, episode):
    return f"{clean_url(series_url)}/season-{episode['sea_num']}/{episode['id']}"

def collect_episode_items(series_url, show_progress=True):
    show = show_slug_from_url(series_url)
    api_url = f"https://corona.channel5.com/shows/{show}/episodes.json?platform=my5desktop"
    try:
        response = session.get(api_url, timeout=30)
        response.raise_for_status()
        data = response.json()
    except requests.RequestException as exc:
        raise RuntimeError(f"Failed to retrieve My5 episodes: {exc}") from exc
    episodes = data.get("episodes") or []
    if not episodes:
        raise ValueError(f"No My5 episodes found for show: {show}")

    items = []
    expected_show_title = None
    for episode in episodes:
        if not episode.get("vod_available", True):
            continue

        show_title = episode.get("sh_title", "My5")
        if expected_show_title is None:
            expected_show_title = show_title
            if show_progress:
                print(f"{icons.ICON_INFO} {bcolors.LIGHTBLUE}Show: {bcolors.ENDC}{expected_show_title}")

        items.append({
            "url": build_episode_url(series_url, episode),
            "id": episode["id"],
            "episode": episode,
        })

    items.sort(key=episode_sort_key)
    return items

def collect_episode_urls(series_url):
    return [item["url"] for item in collect_episode_items(series_url)]

def episode_sort_key(item):
    episode = item["episode"]
    return (
        int(episode.get("sea_num") or 9999),
        int(episode.get("ep_num") or 9999),
        item["id"],
    )

def episode_series_number(item):
    season = item["episode"].get("sea_num")
    return int(season) if season is not None else None

def episode_number(item):
    episode = item["episode"].get("ep_num")
    return int(episode) if episode is not None else None

def episode_tree_label(episode):
    number = episode.get("ep_num")
    title = episode.get("title") or "Unknown"
    return str(number).zfill(2) if number is not None else "-", title

def clean_text(value):
    if value is None:
        return ""
    value = re.sub(r"<[^>]+>", " ", str(value))
    return re.sub(r"\s+", " ", value).strip()

def format_info_date(value):
    if value in (None, ""):
        return ""
    try:
        return datetime.fromtimestamp(int(value), tz=timezone.utc).strftime("%d %B %Y")
    except (TypeError, ValueError, OSError):
        return clean_text(value)

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

def max_height_from_streams(streams, default="1080"):
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
        f"{'-' * left_width}"
        f"{bcolors.ENDC} {bcolors.LIGHTBLUE}{service_label}: {bcolors.ENDC}{bcolors.WHITE}{series_title}{bcolors.ENDC} "
        f"{bcolors.LIGHTBLUE}{'-' * right_width}{bcolors.ENDC}"
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
        raise ValueError(f"No My5 episodes found for selector {format_download_selector(parsed_selector)}.")

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
        _, title = episode_tree_label(item["episode"])
        print(f"{selector} {title}")

def list_episode_items(episode_items):
    if not episode_items:
        print(f"{icons.ICON_FAILURE} {bcolors.FAIL}No My5 episodes found.{bcolors.ENDC}")
        return

    show_title = episode_items[0]["episode"].get("sh_title", "My5")
    grouped_items = group_episode_items_by_series(episode_items)
    group_labels = sorted(grouped_items, key=series_group_sort_key)
    series_summary = ",  ".join(f"{label}({len(grouped_items[label])})" for label in group_labels)

    print(f"{icons.ICON_SUCCESS} {bcolors.OKGREEN}Found {len(episode_items)} My5 episodes{bcolors.ENDC}")
    print()
    print_series_rule("My5 Series", show_title)
    print()
    print(f"{bcolors.GRAY}{len(group_labels)} Series" + (f",  {series_summary}" if series_summary else "") + f"{bcolors.ENDC}")

    for series_index, series_label in enumerate(group_labels):
        series_items = grouped_items[series_label]
        if series_index > 0:
            print(f"{bcolors.GRAY}|{bcolors.ENDC}")

        group_is_last = series_index == len(group_labels) - 1
        group_branch = "`-" if group_is_last else "|-"
        group_child_prefix = "   " if group_is_last else "|  "
        print(f"{bcolors.GRAY}{group_branch} {series_label}: {bcolors.ENDC}{len(series_items)} episodes")

        for episode_index, item in enumerate(series_items):
            is_last = episode_index == len(series_items) - 1
            branch = "`-" if is_last else "|-"
            url_branch = "  " if is_last else "| "
            episode_number, episode_title = episode_tree_label(item["episode"])

            print(f"{bcolors.GRAY}{group_child_prefix}{branch} {episode_number}. {bcolors.ENDC}{episode_title}")
            print(f"{bcolors.GRAY}{group_child_prefix}{url_branch} {bcolors.ENDC}{bcolors.LIGHTBLUE}{item['url']}{bcolors.ENDC}")

def export_episode_urls(episode_items):
    """Write listed My5 episode URLs to Eurovine's shared export directory."""
    export_dir = Path(__file__).resolve().parents[2] / "export"
    export_dir.mkdir(parents=True, exist_ok=True)
    title = episode_items[0]["episode"].get("sh_title", "my5") if episode_items else "my5"
    safe_title = re.sub(r"[^A-Za-z0-9._-]+", "_", title).strip("._") or "my5"
    output_path = export_dir / f"{safe_title}_episodes.txt"
    output_path.write_text("\n".join(item['url'] for item in episode_items) + "\n", encoding="utf-8")
    print(f"{icons.ICON_SUCCESS} {bcolors.OKGREEN}Exported list: {output_path}{bcolors.ENDC}")

def get_content_info(episode_url):
    try:
        # Extract the show name from the episode URL
        path_segments = urlparse(episode_url).path.strip("/").split("/")
        if len(path_segments) < 2:
            raise ValueError("Invalid episode URL")

        show = path_segments[1]

        # Case 1: Standard series with season and episode information
        if 'season' in episode_url:
            season = path_segments[2]
            episode = path_segments[3]

            # Construct Android API content URL for standard episodes
            content_url = MY5_EPISODE_URL.format(show=show, season=season, episode=episode)

            r = session.get(content_url, timeout=30)
            if r.status_code != 200:
                print(f"[!] Received status code '{r.status_code}' when attempting to get the content ID")
                return

            resp = r.json()

            if not resp.get("vod_available", True):
                print("[!] Episode is not available")
                return

            return (
                resp["id"],  # content_id
                resp["sea_num"],
                str(resp["ep_num"]),
                resp["sh_title"],
                resp["title"],
            )

        # Case 2: Standalone shows (like documentaries or movies) with no season info
        else:
            # Query the episodes.json API for standalone shows
            standalone_url = f"https://corona.channel5.com/shows/{show}/episodes.json?platform=my5desktop"
            r = session.get(standalone_url, timeout=30)

            if r.status_code != 200:
                print(f"[!] Received status code '{r.status_code}' when attempting to get standalone content ID")
                return

            resp = r.json()

            if not resp['episodes']:
                raise ValueError(f"No episodes found for the show: {show}")

            # Extract the first episode's ID
            episode_data = resp['episodes'][0]

            return (
                episode_data["id"],  # content_id
                "01",  # Default season as 01 for standalone shows
                str(episode_data["ep_num"] or 1),  # Default episode as 1 if not present
                episode_data["sh_title"],
                episode_data["title"],
            )

    except Exception as ex:
        print(f"[!] Exception thrown when attempting to get content info: {ex}")
        raise

def get_episode_data(episode_url):
    path_segments = urlparse(episode_url).path.strip("/").split("/")
    if len(path_segments) < 2 or path_segments[0] != "show":
        raise ValueError("Invalid My5 episode URL")

    show = path_segments[1]
    if 'season' in episode_url:
        if len(path_segments) < 4:
            raise ValueError("Invalid My5 episode URL")
        content_url = MY5_EPISODE_URL.format(
            show=show,
            season=path_segments[2],
            episode=path_segments[3],
        )
        try:
            response = session.get(content_url, timeout=30)
            response.raise_for_status()
            episode = response.json()
        except requests.RequestException as exc:
            raise RuntimeError(f"Failed to get My5 episode metadata: {exc}") from exc
        if not episode.get("vod_available", True):
            raise ValueError("Episode is not available")
        return episode

    api_url = f"https://corona.channel5.com/shows/{show}/episodes.json?platform=my5desktop"
    try:
        response = session.get(api_url, timeout=30)
        response.raise_for_status()
        data = response.json()
    except requests.RequestException as exc:
        raise RuntimeError(f"Failed to get My5 episode metadata: {exc}") from exc

    episodes = data.get("episodes") or []
    if not episodes:
        raise ValueError(f"No episodes found for the show: {show}")
    return episodes[0]

def get_pssh_from_mpd(mpd: str, print_pssh=True):
    try:
        r = session.get(mpd, timeout=30)
        r.raise_for_status()

        pssh_values = re.findall(r"<cenc:pssh>(AAAA.*?)</cenc:pssh>", r.text)
        if not pssh_values:
            raise ValueError("Manifest did not contain Widevine PSSH data")
        pssh = pssh_values[1] if len(pssh_values) > 1 else pssh_values[0]
        if print_pssh:
            print(f"{bcolors.LIGHTBLUE}PSSH: {bcolors.ENDC}{pssh}")
        return pssh, r.text
    except (requests.RequestException, ValueError) as ex:
        raise RuntimeError(f"Failed to get PSSH: {ex}") from ex

def get_height_from_mpd(mpd_content: str):
    try:
        root = ET.fromstring(mpd_content)
        heights = [
            int(height)
            for rep in root.findall('.//{*}Representation')
            for height in [rep.get('height')]
            if height and str(height).isdigit()
        ]
        return str(max(heights)) if heights else "1080"
    except ET.ParseError as ex:
        raise RuntimeError(f"Failed to parse MPD for height: {ex}") from ex

def get_decryption_key(pssh: str, lic_url: str, print_keys=True):
    session_id = None
    try:
        device = Device.load(WVD_PATH)  # Load device from path
        cdm = Cdm.from_device(device)
        session_id = cdm.open()
        challenge = cdm.get_license_challenge(session_id, PSSH(pssh))

        license_response = get_widevine_license(challenge, lic_url)
        cdm.parse_license(session_id, license_response)

        decryption_keys = []
        for key in cdm.get_keys(session_id):
            if key.type == "CONTENT":
                decryption_key = f"{key.kid.hex}:{key.key.hex()}"
                if decryption_key not in decryption_keys:
                    decryption_keys.append(decryption_key)
                    if print_keys:
                        print(f"{bcolors.GREEN}KEYS: {bcolors.ENDC}--key {decryption_key}")
        return decryption_keys
    except Exception as ex:
        raise RuntimeError(f"Failed to get decryption keys: {ex}") from ex
    finally:
        if session_id is not None:
            cdm.close(session_id)

def get_streams(mpd, keys, show_title, full_title, auto_download=False, interactive=False):
    keys_str = ' '.join([f'--key {key}' for key in keys])
    selectors = "" if interactive else "--select-video best --select-audio best --select-subtitle all "
    command = f'N_m3u8DL-RE "{mpd}" {selectors}-mt -M format=mkv:muxer=mkvmerge --save-name "{full_title}" --save-dir "{DOWNLOAD_DIR}" {keys_str}'
    command = append_downloader_proxy(command)
    print(f"{bcolors.YELLOW}DOWNLOAD COMMAND: {bcolors.ENDC}{mask_proxy_command(command)}")
    if auto_download:
        print(f"{icons.ICON_WAITING} {bcolors.OKBLUE}Downloading video...{bcolors.ENDC}")
        subprocess.run(command, shell=True)
        return

    user_input = input("Do you wish to download? Y or N: ").strip().lower()
    if user_input == 'y':
        subprocess.run(command, shell=True)
    return

def format_save_part(value):
    value = clean_text(value).replace(" ", ".")
    value = re.sub(r"[^\w.-]+", "", value)
    return re.sub(r"\.+", ".", value).strip(".")

def build_save_name(show_title, season, episode, episode_title, height):
    show_title = format_save_part(show_title or "My5")
    episode_title = format_save_part(episode_title or "")
    if episode_title and show_title.lower() != episode_title.lower():
        return f"{show_title}.S{int(season):02d}E{int(episode):02d}.{episode_title}.{height}p.MY5.WEB-DL.AAC2.0.H.264"
    return f"{show_title}.{height}p.MY5.WEB-DL.AAC2.0.H.264"

def resolve_playback(url):
    url = url.encode('utf-8', 'ignore').decode().strip()
    episode_url = url

    # Check if the URL has a season/episode structure or if it's a standalone show
    if 'season' not in episode_url:
        # If it's a standalone show, get the episode ID and reconstruct the URL
        content_id, season, episode, show_title, episode_title = get_content_info(episode_url)
        episode_url = f"{episode_url}/{content_id}"

    episode_data = get_episode_data(episode_url)
    content_id = episode_data["id"]
    season = str(episode_data.get("sea_num") or 1)
    episode = str(episode_data.get("ep_num") or 1)
    show_title = episode_data.get("sh_title") or "My5"
    episode_title = episode_data.get("title") or ""

    # Get playlist (manifest) and license URL from Android API
    manifest, lic_url = get_playlist(content_id)

    # Fetch PSSH from the manifest
    pssh, mpd_content = get_pssh_from_mpd(manifest, print_pssh=False)

    # Get decryption keys using Widevine
    keys = get_decryption_key(pssh, lic_url, print_keys=False)

    # Determine the resolution from the MPD file
    height = get_height_from_mpd(mpd_content)
    save_name = build_save_name(show_title, season, episode, episode_title, height)
    return {
        "manifest_url": manifest,
        "license_url": lic_url,
        "pssh": pssh,
        "manifest_text": mpd_content,
        "keys": keys,
        "height": height,
        "save_name": save_name,
        "episode": episode_data,
    }

def print_playback_details(playback):
    print(f"{bcolors.LIGHTBLUE}MPD URL: {bcolors.ENDC}{playback['manifest_url']}")
    print(f"{bcolors.RED}License URL: {bcolors.ENDC}{playback['license_url']}")
    print(f"{bcolors.LIGHTBLUE}PSSH: {bcolors.ENDC}{playback['pssh']}")
    for key in playback.get("keys") or []:
        print(f"{bcolors.GREEN}KEYS: {bcolors.ENDC}--key {key}")

def print_episode_metadata(episode):
    title = f"Season {episode.get('sea_num')} Episode {episode.get('ep_num')} {episode.get('title')}".strip()
    rows = [
        ("Show", clean_text(episode.get("sh_title"))),
        ("Title", clean_text(title)),
        ("Date Aired", format_info_date(episode.get("vod_s"))),
        ("Description", clean_text(episode.get("s_desc"))),
    ]
    print(f"\n{bcolors.YELLOW}Episode metadata:{bcolors.ENDC}")
    for label, value in rows:
        if value:
            print(f"{bcolors.LIGHTBLUE}{label}: {bcolors.ENDC}{value}")

def info(url):
    if not is_episode_url(url):
        raise ValueError("Info mode requires a My5 episode/video URL.")

    spinner = Spinner()
    spinner.start()
    try:
        playback = resolve_playback(url)
        streams, manifest_type = parse_manifest_streams(playback["manifest_text"])
        height = max_height_from_streams(streams, playback["height"])
        save_name = build_save_name(
            playback["episode"].get("sh_title"),
            playback["episode"].get("sea_num") or 1,
            playback["episode"].get("ep_num") or 1,
            playback["episode"].get("title") or "",
            height,
        )
    except Exception:
        spinner.stop()
        raise
    spinner.stop()

    print(f"{bcolors.LIGHTBLUE}{manifest_type} Manifest URL: {bcolors.ENDC}{playback['manifest_url']}")
    if playback.get("keys"):
        for key in playback["keys"]:
            print(f"{bcolors.GREEN}KEYS: {bcolors.ENDC}--key {key}")
    print_streams(streams)
    print_episode_metadata(playback["episode"])
    print(f"\n{bcolors.YELLOW}Suggested filename: {bcolors.ENDC}{save_name}.mkv")

def process_video(url, auto_download=False, interactive=False):
    spinner = Spinner()
    spinner.start()
    try:
        playback = resolve_playback(url)
    except Exception:
        spinner.stop()
        raise
    spinner.stop()

    print_playback_details(playback)
    # Get the download streams
    get_streams(playback["manifest_url"], playback["keys"], "", playback["save_name"], auto_download=auto_download, interactive=interactive)

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
        _, title = episode_tree_label(item["episode"])
        print(f"\n{icons.ICON_INFO} {bcolors.LIGHTBLUE}Downloading {index}/{len(episode_items)}: {title}{bcolors.ENDC}")
        process_video(item["url"], auto_download=True)

def main(video_url, downloads_path, wvd_device_path, certificate=None, mode="auto", export_list=False, download_selector=None):
    """Eurovine entry point for My5 (Widevine)."""
    if not video_url:
        raise ValueError("No My5 URL provided.")
    if not downloads_path or not wvd_device_path:
        raise ValueError("Eurovine config requires downloads_path and wvd_device_path for My5.")
    configure_service(downloads_path, wvd_device_path, certificate)
    video_url = video_url.strip()

    if mode == "list":
        try:
            if is_episode_url(video_url):
                episode = get_episode_data(video_url)
                episode_items = [{
                    "url": clean_url(video_url),
                    "id": episode["id"],
                    "episode": episode,
                }]
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
            print(f"{icons.ICON_FAILURE} {bcolors.FAIL}Download selector mode requires a My5 series URL, not an episode URL.{bcolors.ENDC}")
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
    print("Run My5 through eurovine.py so it can use the shared Eurovine configuration.")
  
