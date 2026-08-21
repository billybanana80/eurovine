import re
import subprocess
from base64 import b64encode
import requests
import os
import json
import sys
import urllib3
import html as html_lib
import shutil
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse
from pywidevine.cdm import Cdm
from pywidevine.device import Device
from pywidevine.pssh import PSSH
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

BASE_URL = "https://www.itv.com"
WVD_PATH = None
SAVE_PATH = None

def configure_service(downloads_path, wvd_device_path):
    """Apply configuration supplied by the Eurovine organizer."""
    global WVD_PATH, SAVE_PATH
    WVD_PATH = wvd_device_path
    SAVE_PATH = downloads_path

def clean_text(value):
    return re.sub(r"\s+", " ", html_lib.unescape(str(value or ""))).strip()

def canonical_url(value):
    value = clean_text(value)
    if re.match(r"^https?://", value, re.I):
        return value
    if value.startswith("/"):
        return urljoin(BASE_URL, value)
    return urljoin(BASE_URL, f"/{value.strip('/')}")

def programme_id(programme):
    encoded = programme.get("encodedProgrammeId")
    if isinstance(encoded, dict):
        return clean_text(encoded.get("letterA") or encoded.get("underscore"))
    return clean_text(encoded or programme.get("programmeId"))

def encoded_episode_id(item):
    encoded = item.get("encodedEpisodeId")
    if isinstance(encoded, dict):
        return clean_text(encoded.get("letterA") or encoded.get("underscore"))
    return clean_text(encoded or item.get("productionId") or item.get("episodeId"))

def title_slug_from_url(url):
    parts = [part for part in urlparse(canonical_url(url)).path.split("/") if part]
    return parts[1] if len(parts) >= 3 and parts[0] == "watch" else ""

def show_url(programme, source_url):
    slug = clean_text(programme.get("titleSlug")) or title_slug_from_url(source_url)
    prog_id = programme_id(programme)
    if slug and prog_id:
        return f"{BASE_URL}/watch/{slug}/{prog_id}"
    return canonical_url(source_url)

def video_url(programme, item, source_url):
    ep_id = encoded_episode_id(item)
    base = show_url(programme, source_url).rstrip("/")
    return f"{base}/{ep_id}" if ep_id else base

def season_number(series, item):
    return clean_text(item.get("series") or series.get("seriesNumber")) or "1"

def episode_number(item):
    return clean_text(item.get("episode")) or "1"

def episode_title(item):
    title = clean_text(item.get("episodeTitle"))
    return title or f"Episode {episode_number(item)}"

def collect_episodes(page_props):
    series_list = page_props.get("seriesList") or []
    episodes = []
    seen = set()

    for series in series_list:
        for item in series.get("titles") or []:
            ep_id = encoded_episode_id(item)
            key = ep_id or clean_text(item.get("episodeId") or item.get("ccid"))
            if not key or key in seen:
                continue
            seen.add(key)
            episodes.append((series, item))

    episode = page_props.get("episode") or {}
    if not episodes and episode:
        fake_series = {"seriesNumber": clean_text(episode.get("series")) or "1"}
        episodes.append((fake_series, episode))

    return episodes

def episode_sort_key(item):
    return (item.get("sort_season") or 9999, item.get("sort_episode") or 9999, item.get("id") or "")

def build_episode_item(programme, series, item, source_url):
    season = season_number(series, item)
    episode = episode_number(item)
    try:
        sort_season = int(season)
    except ValueError:
        sort_season = 9999
    try:
        sort_episode = int(episode)
    except ValueError:
        sort_episode = 9999

    return {
        "url": video_url(programme, item, source_url),
        "id": encoded_episode_id(item),
        "show_title": clean_text(programme.get("title")) or "Unknown Show",
        "season": season,
        "episode": episode,
        "title": episode_title(item),
        "sort_season": sort_season,
        "sort_episode": sort_episode,
    }

def is_episode_url(url):
    parts = [part for part in urlparse(canonical_url(url)).path.split("/") if part]
    return len(parts) >= 4 and parts[0] == "watch"

def group_episode_items_by_series(episode_items):
    grouped = {}
    for item in sorted(episode_items, key=episode_sort_key):
        season = item.get("sort_season")
        label = f"Series {season}" if season and season != 9999 else "Episodes"
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

def list_episode_items(episode_items):
    if not episode_items:
        print(f"{icons.ICON_FAILURE} {bcolors.FAIL}No ITVX episodes found.{bcolors.ENDC}")
        return

    show_title = episode_items[0].get("show_title") or "ITVX"
    grouped_items = group_episode_items_by_series(episode_items)
    group_labels = sorted(grouped_items, key=series_group_sort_key)
    series_summary = ",  ".join(f"{label}({len(grouped_items[label])})" for label in group_labels)

    print(f"{icons.ICON_SUCCESS} {bcolors.OKGREEN}Found {len(episode_items)} ITVX episodes{bcolors.ENDC}")
    print()
    print_series_rule("ITVX Series", show_title)
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
            episode_label = item.get("episode") or "-"
            print(f"{bcolors.GRAY}{group_child_prefix}{branch} {episode_label}. {bcolors.ENDC}{item.get('title') or '-'}")
            print(f"{bcolors.GRAY}{group_child_prefix}{url_branch} {bcolors.ENDC}{bcolors.LIGHTBLUE}{item.get('url') or '-'}{bcolors.ENDC}")

def export_episode_urls(episode_items):
    """Write listed ITVX episode URLs to Eurovine's shared export directory."""
    export_dir = Path(__file__).resolve().parents[2] / "export"
    export_dir.mkdir(parents=True, exist_ok=True)
    title = episode_items[0].get("show_title") if episode_items else "itvx"
    safe_title = re.sub(r"[^A-Za-z0-9._-]+", "_", title or "itvx").strip("._") or "itvx"
    output_path = export_dir / f"{safe_title}_episodes.txt"
    output_path.write_text("\n".join(item['url'] for item in episode_items) + "\n", encoding="utf-8")
    print(f"{icons.ICON_SUCCESS} {bcolors.OKGREEN}Exported list: {output_path}{bcolors.ENDC}")

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
        matched_start = (selected[0]["sort_season"], selected[0]["sort_episode"])
        matched_end = (selected[-1]["sort_season"], selected[-1]["sort_episode"])
        if matched_start > requested_start or matched_end < requested_end:
            matched_label = f"{format_queue_selector(*matched_start)}-{format_queue_selector(*matched_end)}"
            print(f"{bcolors.WARNING}{icons.ICON_WARNING} Requested range {format_download_selector(parsed_selector)} only matched {matched_label}.{bcolors.ENDC}")

    if parsed_selector["type"] == "season_range":
        requested_start = parsed_selector["start"]["season"]
        requested_end = parsed_selector["end"]["season"]
        matched_seasons = sorted({item["sort_season"] for item in selected})
        if matched_seasons[0] > requested_start or matched_seasons[-1] < requested_end:
            matched_label = f"{format_queue_selector(matched_seasons[0])}-{format_queue_selector(matched_seasons[-1])}"
            print(f"{bcolors.WARNING}{icons.ICON_WARNING} Requested range {format_download_selector(parsed_selector)} only matched seasons {matched_label}.{bcolors.ENDC}")

def print_download_queue(episode_items):
    print()
    print(f"{bcolors.LIGHTBLUE}Download queue:{bcolors.ENDC}")
    for item in episode_items:
        print(f"{format_queue_selector(item['sort_season'], item['sort_episode'])} {item.get('title') or '-'}")

def capitalize_words(s):
    return ".".join(word.capitalize() for word in s.split("."))

def format_save_title(value):
    value = re.sub(r"[^\w\s.-]", "", clean_text(value))
    return capitalize_words(value.replace(" ", "."))

def first_value(mapping, *keys):
    for key in keys:
        value = mapping.get(key)
        if value not in (None, "", []):
            return value
    return ""

def format_info_date(value):
    value = clean_text(value)
    if not value:
        return ""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return f"{parsed.day} {parsed.strftime('%B %Y')}"
    except (TypeError, ValueError):
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
    bitrate_match = re.search(r"[\d.]+", stream.get("bitrate") or "")
    height = int(height_match.group(1)) if height_match else 0
    bitrate = float(bitrate_match.group()) if bitrate_match else 0
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

def subtitle_url(subtitle):
    if isinstance(subtitle, str):
        return subtitle
    if not isinstance(subtitle, dict):
        return ""
    for key in ("Href", "href", "Url", "url", "Uri", "uri"):
        value = clean_text(subtitle.get(key))
        if value:
            return value
    return ""

def subtitle_language(subtitle):
    if not isinstance(subtitle, dict):
        return "en"
    return clean_text(
        subtitle.get("Language")
        or subtitle.get("language")
        or subtitle.get("Locale")
        or subtitle.get("locale")
        or "en"
    )

def subtitle_codec(subtitle):
    text = json.dumps(subtitle, ensure_ascii=False).lower() if isinstance(subtitle, dict) else str(subtitle).lower()
    if ".vtt" in text or "webvtt" in text:
        return "vtt"
    if ".ttml" in text or "ttml" in text:
        return "ttml"
    return "-"

def external_subtitle_streams(subtitles):
    rows = []
    seen = set()
    for subtitle in subtitles or []:
        url = subtitle_url(subtitle)
        if not url or url in seen:
            continue
        seen.add(url)
        rows.append({
            "type": "Sub",
            "resolution": "-",
            "bitrate": "-",
            "codec": subtitle_codec(subtitle),
            "lang": subtitle_language(subtitle) or "en",
            "channels": "-",
            "extra": "external",
        })
    return rows

def strip_subtitle_tags(text):
    text = re.sub(r"<[^>]+>", "", text)
    return clean_text(html_lib.unescape(text))

def vtt_time_to_srt(value):
    value = value.strip()
    if value.count(":") == 1:
        value = "00:" + value
    return value.replace(".", ",")

def parse_vtt(vtt_text):
    vtt_text = vtt_text.replace("\ufeff", "").replace("\r\n", "\n").replace("\r", "\n")
    cues = []
    for block in re.split(r"\n{2,}", vtt_text.strip()):
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        if not lines or lines[0].upper().startswith("WEBVTT"):
            continue
        time_index = next((index for index, line in enumerate(lines) if "-->" in line), None)
        if time_index is None:
            continue
        start, end = [part.strip().split()[0] for part in lines[time_index].split("-->", 1)]
        text = strip_subtitle_tags(" ".join(lines[time_index + 1:]))
        if text:
            cues.append({"start": vtt_time_to_srt(start), "end": vtt_time_to_srt(end), "text": text})
    return cues

def write_srt(cues, output_path):
    lines = []
    for index, cue in enumerate(cues, start=1):
        lines.extend([str(index), f"{cue['start']} --> {cue['end']}", cue["text"], ""])
    Path(output_path).write_text("\n".join(lines), encoding="utf-8")

def save_external_subtitles(client, subtitles, filename):
    candidates = [subtitle for subtitle in subtitles or [] if subtitle_url(subtitle)]
    if not candidates:
        print(f"{bcolors.WARNING}{icons.ICON_WARNING} No external ITVX subtitle URL found.{bcolors.ENDC}")
        return None

    subtitle = candidates[0]
    url = subtitle_url(subtitle)
    response = client.get(url, headers={"Accept": "text/vtt,*/*"}, timeout=30)
    response.raise_for_status()
    cues = parse_vtt(response.text)
    if not cues:
        print(f"{bcolors.WARNING}{icons.ICON_WARNING} No ITVX subtitle cues found.{bcolors.ENDC}")
        return None

    lang = subtitle_language(subtitle) or "en"
    output_path = Path(SAVE_PATH) / f"{filename}.{lang}.srt"
    write_srt(cues, output_path)
    print(f"{bcolors.OKGREEN}{icons.ICON_SUCCESS} External subtitles saved: {bcolors.ENDC}{output_path}")
    return output_path

def build_save_name(programme, episode, max_height):
    try:
        season = int(episode.get("series") or 1)
    except (TypeError, ValueError):
        season = 1
    try:
        number = int(episode.get("episode") or 1)
    except (TypeError, ValueError):
        number = 1
    title = format_save_title(programme.get("title") or "ITVX")
    return f"{title}.S{season:02d}E{number:02d}.{max_height}p.ITVX.WEB-DL.AAC2.0.H.264"

def print_episode_metadata(programme, episode):
    rows = [
        ("Show", clean_text(programme.get("title"))),
        ("Title", clean_text(first_value(episode, "episodeTitle", "title")) or episode_title(episode)),
        (
            "Date Aired",
            format_info_date(first_value(
                episode,
                "broadcastDateTime",
                "dateTime",
                "broadcastDate",
                "datePublished",
                "airDate",
                "originalTransmissionDate",
                "availabilityStart",
            )),
        ),
        ("Description", clean_text(first_value(episode, "longDescription", "description", "synopsis", "shortDescription"))),
    ]
    print(f"\n{bcolors.YELLOW}Episode metadata:{bcolors.ENDC}")
    for label, value in rows:
        if value:
            print(f"{bcolors.LIGHTBLUE}{label}: {bcolors.ENDC}{value}")

class ITV:
    def __init__(self):
        self.client = requests.Session()
        self.client.headers.update({
            "User-Agent": "okhttp/4.9.3" # updated User Agent 04062025 from Devine script
        })
        proxy_url = current_proxy_url()
        if proxy_url:
            self.client.proxies.update({'http': proxy_url, 'https': proxy_url})
        self.authorization = None  # Optional: for premium content

    def page_props(self, url):
        try:
            r = self.client.get(canonical_url(url), timeout=30)
        except requests.RequestException as exc:
            raise ConnectionError(f"Failed to fetch the ITVX page: {exc}") from exc
        if r.status_code != 200:
            raise ConnectionError(f"Failed to fetch the ITVX page: HTTP {r.status_code}")

        match = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', r.text, re.S)
        if not match:
            raise ValueError("Unable to find the __NEXT_DATA__ metadata on the ITVX page.")

        try:
            return json.loads(match.group(1)).get("props", {}).get("pageProps", {})
        except json.JSONDecodeError as exc:
            raise ValueError(f"Unable to parse the ITVX page metadata: {exc}") from exc

    def collect_episode_items(self, series_url):
        source_url = canonical_url(series_url)
        page_props = self.page_props(source_url)
        programme = page_props.get("programme") or {}

        if is_episode_url(source_url) and page_props.get("episode"):
            episode = page_props.get("episode") or {}
            series = {"seriesNumber": clean_text(episode.get("series")) or "1"}
            return [build_episode_item(programme, series, episode, source_url)]

        episodes = collect_episodes(page_props)
        if not episodes:
            return []

        episode_items = [
            build_episode_item(programme, series, item, source_url)
            for series, item in episodes
        ]
        episode_items.sort(key=episode_sort_key)
        return episode_items

    def select_episode_items(self, series_url, selector):
        parsed_selector = parse_download_selector(selector)
        episode_items = self.collect_episode_items(series_url)
        selected = []

        for item in episode_items:
            season = item.get("sort_season") or 0
            episode = item.get("sort_episode") or 0
            if episode <= 0 or season == 9999:
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
            series_title = episode_items[0].get("show_title") if episode_items else "ITVX"
            raise LookupError(f"No ITVX episodes found for selector {format_download_selector(parsed_selector)} in {series_title}.")

        selected.sort(key=episode_sort_key)
        warn_if_partial_range_match(parsed_selector, selected)
        return selected

    def download_selected_episodes(self, series_url, selector, quality=None, auto_confirm=False, save_subs=False):
        print(f"{icons.ICON_WAITING} {bcolors.LIGHTBLUE}Retrieving series information.....{bcolors.ENDC}")
        episode_items = self.select_episode_items(series_url, selector)
        print_download_queue(episode_items)

        episode_word = "episode" if len(episode_items) == 1 else "episodes"
        this_or_these = "this" if len(episode_items) == 1 else "these"
        if not confirm_download(f"Do you wish to download {this_or_these} {len(episode_items)} {episode_word}? Y or N: ", auto_confirm=auto_confirm):
            print(f"{icons.ICON_FAILURE} {bcolors.RED}Download cancelled{bcolors.ENDC}")
            return

        for index, item in enumerate(episode_items, start=1):
            print(f"\n{icons.ICON_INFO} {bcolors.LIGHTBLUE}Downloading {index}/{len(episode_items)}: {item.get('title') or item.get('url')}{bcolors.ENDC}")
            self.download(item["url"], auto_download=True, quality=quality, save_subs=save_subs)

    def get_pssh(self, mpd_url: str) -> str:
        r = self.client.get(mpd_url, timeout=30)
        kid = re.search(r'cenc:default_KID="([a-fA-F0-9-]+)"', r.text).group(1).replace('-', '')
        s = f'000000387073736800000000edef8ba979d64acea3c827dcd51d21ed000000181210{kid}48e3dc959b06'
        return b64encode(bytes.fromhex(s)).decode()

    def get_key(self, pssh: str, lic_url: str) -> str:
        pssh = PSSH(pssh)
        device = Device.load(WVD_PATH)
        cdm = Cdm.from_device(device)
        session_id = cdm.open()
        challenge = cdm.get_license_challenge(session_id, pssh)
        licence = self.client.post(lic_url, data=challenge, timeout=30).content
        cdm.parse_license(session_id, licence)
        decryption_keys = [f'{key.kid.hex}:{key.key.hex()}' for key in cdm.get_keys(session_id) if key.type == 'CONTENT']
        cdm.close(session_id)
        return decryption_keys[0] if decryption_keys else None

    def fetch_mpd(self, playlist_url: str) -> dict:
        
        payload = {
            "client": {
                "id": "lg",
            },
            "device": {
                "deviceGroup": "ctv",
            },
            "variantAvailability": {
                "player": "dash",
                "featureset": [
                    "mpeg-dash",
                    "widevine",
                    "outband-webvtt",
                    "hd",
                    "single-track",
                ],
                "platformTag": "ctv",
                "drm": {
                    "system": "widevine",
                    "maxSupported": "L3",
                },
            },
        }


        headers = {
            "User-Agent": "okhttp/4.9.3",
            "Accept": "application/vnd.itv.vod.playlist.v4+json",
            "Accept-Language": "en-US,en;q=0.9",
            "Connection": "keep-alive",
            "Content-Type": "application/json",
        }

        try:
            r = self.client.post(playlist_url, json=payload, headers=headers, timeout=30)
        except requests.RequestException as exc:
            raise ConnectionError(f"Failed to fetch playback information: {exc}") from exc
        if r.status_code != 200:
            detail = clean_text(r.text)[:240]
            raise ConnectionError(
                f"Failed to fetch playback information: HTTP {r.status_code}"
                + (f" ({detail})" if detail else "")
            )

        try:
            return r.json()
        except requests.JSONDecodeError as exc:
            raise ValueError(f"Unable to parse the ITVX playback response: {exc}") from exc

    def resolve_playback(self, url):
        page_props = self.page_props(url)
        programme = page_props.get("programme") or {}
        episode = page_props.get("episode") or {}
        series_list = programme.get("seriesList") or []
        series_titles = series_list[0].get("titles") or [] if series_list else []
        playlist_url = episode.get("playlistUrl") or (
            series_titles[0].get("playlistUrl") if series_titles else None
        )
        if not playlist_url:
            raise ValueError("Unable to find an ITVX playlist URL for this episode.")

        mpd_data = self.fetch_mpd(playlist_url)
        video = (mpd_data.get("Playlist") or {}).get("Video") or {}
        media_files = video.get("MediaFiles") or []
        if not media_files:
            raise ValueError("No media files were returned by ITVX.")

        def resolution_value(media_file):
            try:
                return int(media_file.get("Resolution") or 0)
            except (TypeError, ValueError):
                return 0

        best_file = max(media_files, key=resolution_value)
        manifest_url = best_file.get("Href")
        if not manifest_url:
            raise ValueError("ITVX playback information did not contain a manifest URL.")
        subtitles = video.get("Subtitles") or []
        return page_props, programme, episode, best_file, manifest_url, subtitles

    def info(self, url):
        spinner = Spinner()
        spinner.start()
        try:
            page_props, programme, episode, media_file, manifest_url, subtitles = self.resolve_playback(url)
            response = self.client.get(manifest_url, timeout=30)
            response.raise_for_status()
            streams, manifest_type = parse_manifest_streams(response.text)
            for subtitle_stream in external_subtitle_streams(subtitles):
                streams.append(subtitle_stream)
            streams = sorted(streams, key=stream_sort_key)
            max_height = max(
                (
                    int(match.group(1))
                    for stream in streams
                    if stream["type"] == "Vid"
                    for match in [re.search(r"x(\d+)", stream["resolution"])]
                    if match
                ),
                default=int(media_file.get("Resolution") or 1080),
            )
            lic_url = media_file.get("KeyServiceUrl")
            key = None
            key_error = ""
            if lic_url:
                try:
                    pssh = self.get_pssh(manifest_url)
                    key = self.get_key(pssh, lic_url) if pssh else None
                except Exception as exc:
                    key_error = clean_text(str(exc))
        except requests.RequestException as exc:
            spinner.stop()
            raise ConnectionError(f"Failed to fetch the manifest: {exc}") from exc
        except Exception:
            spinner.stop()
            raise
        spinner.stop()

        print(f"{bcolors.LIGHTBLUE}{manifest_type} Manifest URL: {bcolors.ENDC}{manifest_url}")
        if lic_url:
            if key_error:
                print(f"{bcolors.YELLOW}KEYS: {bcolors.ENDC}Unavailable ({key_error})")
            else:
                print(f"{bcolors.GREEN}KEYS: {bcolors.ENDC}{('--key ' + key) if key else 'Unavailable'}")
        else:
            print(f"{bcolors.YELLOW}KEYS: {bcolors.ENDC}Unavailable (no license URL)")
        print_streams(streams)
        print_episode_metadata(programme, episode)
        print(f"\n{bcolors.YELLOW}Suggested filename: {bcolors.ENDC}{build_save_name(programme, episode, max_height)}.mkv")
        return True


    def download(self, url: str, auto_download=False, interactive=False, quality=None, save_subs=False):
        # Step 1: Fetch and parse `#__NEXT_DATA__` metadata
        spinner = Spinner()
        spinner.start()
        try:
            page_props, programme, episode, best_file, mpd_url, subtitles = self.resolve_playback(url)
            lic_url = best_file['KeyServiceUrl']

            # Step 5: Generate PSSH and fetch the decryption key
            pssh = self.get_pssh(mpd_url)
            key = self.get_key(pssh, lic_url)

            # Step 6: Extract maximum resolution
            r = self.client.get(mpd_url, timeout=30)
            r.raise_for_status()
            max_height = "1080"  # Default
            match = re.search(r'maxHeight="(\d+)"', r.text)
            if match:
                max_height = match.group(1)

            # Step 7: Construct the save file name with properly formatted season and episode numbers
            save_name = build_save_name(programme, episode, max_height)
            save_name = apply_quality_to_filename(save_name, quality)
        except (ConnectionError, ValueError, requests.RequestException) as exc:
            spinner.stop()
            print(f"{bcolors.RED}{exc}{bcolors.ENDC}")
            return False
        except Exception as exc:
            spinner.stop()
            print(f"{bcolors.RED}{exc}{bcolors.ENDC}")
            return False
        spinner.stop()

        # Print download details
        print(f"{bcolors.LIGHTBLUE}MPD URL: {bcolors.ENDC}{mpd_url}")
        print(f"{bcolors.RED}License URL: {bcolors.ENDC}{lic_url}")
        print(f"{bcolors.LIGHTBLUE}PSSH: {bcolors.ENDC}{pssh}")
        if key:
            print(f"{bcolors.GREEN}KEYS: {bcolors.ENDC}--key {key}")

        # Step 8: Construct and execute the download command
        if interactive:
            selectors = ""
        else:
            subtitle_selector = "--select-subtitle all" if save_subs else "--drop-subtitle all"
            selectors = f"{video_selector(quality)} --select-audio best {subtitle_selector} "
        command = f'N_m3u8DL-RE "{mpd_url}" {selectors}-mt -M format=mkv:muxer=mkvmerge --save-name "{save_name}" --save-dir "{SAVE_PATH}" --key {key}'
        command = append_downloader_proxy(command)
        print(f"{bcolors.YELLOW}DOWNLOAD COMMAND: {bcolors.ENDC}{mask_proxy_command(command)}")
        if save_subs:
            save_external_subtitles(self.client, subtitles, save_name)
        if auto_download:
            print(f"{icons.ICON_WAITING} {bcolors.OKBLUE}Downloading video...{bcolors.ENDC}")
            subprocess.run(command, shell=True)
            return True

        if input("Do you wish to download? Y or N: ").strip().lower() == 'y':
            print(f"{icons.ICON_WAITING} {bcolors.OKBLUE}Downloading video...{bcolors.ENDC}")
            subprocess.run(command, shell=True)
            return True

        print(f"{icons.ICON_FAILURE} {bcolors.RED}Download cancelled{bcolors.ENDC}")
        return False

def main(video_url, downloads_path, wvd_device_path, mode="auto", export_list=False, download_selector=None, quality=None, auto_confirm=False, save_subs=False):
    """Eurovine entry point for ITVX (Widevine)."""
    if not video_url:
        raise ValueError("No ITVX URL provided.")
    if not downloads_path or not wvd_device_path:
        raise ValueError("Eurovine config requires downloads_path and wvd_device_path for ITVX.")
    configure_service(downloads_path, wvd_device_path)
    itv = ITV()
    video_url = video_url.strip()

    if mode == "list":
        try:
            episode_items = itv.collect_episode_items(video_url)
            list_episode_items(episode_items)
            if export_list:
                export_episode_urls(episode_items)
        except Exception as exc:
            print(f"{icons.ICON_FAILURE} {bcolors.FAIL}{exc}{bcolors.ENDC}")
        return

    if mode == "info":
        if not is_episode_url(video_url):
            print(f"{icons.ICON_FAILURE} {bcolors.FAIL}Info mode requires an ITVX episode/video URL.{bcolors.ENDC}")
            return
        try:
            itv.info(video_url)
        except (ConnectionError, ValueError, KeyError, requests.RequestException) as exc:
            print(f"{icons.ICON_FAILURE} {bcolors.FAIL}{exc}{bcolors.ENDC}")
        return

    if mode == "download":
        if is_episode_url(video_url):
            print(f"{icons.ICON_FAILURE} {bcolors.FAIL}Download selector mode requires an ITVX series URL, not an episode URL.{bcolors.ENDC}")
            return
        try:
            itv.download_selected_episodes(video_url, download_selector, quality, auto_confirm, save_subs=save_subs)
        except (LookupError, ValueError, ConnectionError) as exc:
            print(f"{icons.ICON_FAILURE} {bcolors.FAIL}{exc}{bcolors.ENDC}")
        return

    if is_episode_url(video_url):
        itv.download(video_url, auto_download=auto_confirm, interactive=(mode == "interactive"), quality=quality, save_subs=save_subs)
        return

    print(f"{icons.ICON_WARNING} {bcolors.WARNING}Series URLs require a flag. Use --list/-l to list episodes or --download/-d SELECTOR to download selected episodes.{bcolors.ENDC}")
