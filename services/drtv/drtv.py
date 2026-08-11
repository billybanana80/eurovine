import html
import json
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree as ET
from urllib.parse import urljoin, urlparse

import requests
import urllib3
import icons
from colors import bcolors
from services.proxy import current_proxy_url, mask_proxy, mask_proxy_command
from beaupy.spinners import Spinner


urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

#   Ozivine: DRTV Video Downloader
#   Author: billybanana
#   Usage: enter a DRTV episode URL to retrieve the HLS Manifest.
#   eg: https://www.dr.dk/drtv/episode/uniformen_-foerste-skoledag_576484
#   Authentication: None
#   Geo-Locking: Denmark may be required for some titles
#   Quality: up to 1080p, depending on title
#   Key Features:
#   1. Extract Video ID: Parses the DRTV URL and fetches the episode page metadata.
#   2. Extract Manifest: Reads DRTV's embedded page state and finds the HLS m3u8 URL.
#   3. Print Download Information: Outputs manifest URL, detected resolution, and download command.
#   4. Subtitles: external Danish subtitles are detected and translated to English in script.
#   5. Note: this script is for DRTV's DRM-free HLS streams.


SERVICE_NAME = "drtv"
BASE_URL = "https://www.dr.dk"
API_BASE_URL = "https://prod95-webfacing.dr-massive.com/api"
TRANSLATE_URL = "https://translate.googleapis.com/translate_a/single"
TRANSLATE_BATCH_MARKER = "@@DRTVBREAK@@"
TRANSLATE_BATCH_SIZE = 40
TRANSLATE_BATCH_CHAR_LIMIT = 4500
SCRIPT_DIR = Path(__file__).resolve().parent
N_M3U8DL = "N_m3u8DL-RE"


session = requests.Session()
SAVE_PATH = None
SERVICE_PROXY = None


def configure_service(downloads_path, _wvd_device_path=None):
    global SAVE_PATH, SERVICE_PROXY
    SAVE_PATH = Path(downloads_path)
    SERVICE_PROXY = current_proxy_url()
    session.proxies.clear()
    session.verify = True
    if SERVICE_PROXY:
        session.proxies.update({"http": SERVICE_PROXY, "https": SERVICE_PROXY})
        session.verify = False


DEFAULT_HEADERS = {
    "Accept": "text/html,application/json,text/plain,*/*",
    "Accept-Language": "da-DK,da;q=0.9,en-US;q=0.8,en;q=0.7",
    "Origin": BASE_URL,
    "Referer": f"{BASE_URL}/drtv/",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36"
    ),
}


@dataclass
class Metadata:
    title: str = "Unknown"
    season: int | None = None
    episode: int | None = None
    episode_title: str | None = None
    aired_date: str = "Unknown"
    description: str = "No Description"
    video_id: str | None = None


@dataclass
class PlaybackInfo:
    manifest_url: str
    manifest_type: str = "m3u8"
    metadata: Metadata | None = None
    subtitles: list | None = None
    streams: list | None = None
    keys: list | None = None


def clean_text(value):
    return re.sub(r"\s+", " ", html.unescape(str(value or ""))).strip()


def canonical_url(value):
    value = clean_text(value)
    if re.match(r"^https?://", value, re.IGNORECASE):
        return value
    if value.startswith("/drtv/"):
        return urljoin(BASE_URL, value)
    if value.startswith(("/episode/", "/se/")):
        return urljoin(BASE_URL, f"/drtv{value}")
    return urljoin(BASE_URL, f"/drtv/{value.strip('/')}")


def extract_video_id(video_url):
    match = re.search(r"_(\d+)(?:[/?#].*)?$", urlparse(video_url).path)
    if match:
        return match.group(1)
    raise ValueError("Could not extract DRTV video ID from URL.")


def is_episode_url(video_url):
    return "/episode/" in urlparse(canonical_url(video_url)).path


def is_series_url(video_url):
    path = urlparse(canonical_url(video_url)).path
    return "/serie/" in path


def parse_window_data(page_html):
    match = re.search(r"window\.__data\s*=\s*", page_html)
    if not match:
        raise RuntimeError("Could not find DRTV page state in window.__data.")
    try:
        data, _ = json.JSONDecoder().raw_decode(page_html[match.end():])
        return data
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Could not parse DRTV page state: {exc}") from exc


def lookup_item(data, item_id):
    entry = data.get("cache", {}).get("itemDetail", {}).get(str(item_id), {})
    return entry.get("item") or {}


def walk(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk(child)


def collect_page_episode_nodes(data, wanted_id=None):
    episodes = []
    seen = set()
    for node in walk(data.get("cache", {}).get("itemDetail", {})):
        if not isinstance(node, dict) or node.get("type") != "episode":
            continue
        episode_id = clean_text(node.get("id"))
        if wanted_id and episode_id != wanted_id:
            continue
        if not episode_id or episode_id in seen:
            continue
        seen.add(episode_id)
        episodes.append(node)

    if not episodes and wanted_id:
        item = lookup_item(data, wanted_id)
        if item.get("type") == "episode":
            episodes.append(item)

    return episodes


def enrich_episode_context(data, item):
    item = dict(item)
    show = item.get("show") or lookup_item(data, item.get("showId"))
    season = item.get("season") or lookup_item(data, item.get("seasonId"))
    if show and not item.get("show"):
        item["show"] = show
    if season and not item.get("season"):
        item["season"] = season
    if isinstance(show, dict) and show.get("title"):
        item["_drtv_show_title"] = show.get("title")
    return item


def fetch_episode_data(video_url, video_id):
    response = session.get(canonical_url(video_url), headers=DEFAULT_HEADERS, timeout=30)
    response.raise_for_status()
    data = parse_window_data(response.text)
    item = lookup_item(data, video_id)
    if not item or item.get("type") != "episode":
        raise RuntimeError(f"Could not find DRTV episode metadata for ID {video_id}.")
    return data, item


def custom_field(item, key):
    return clean_text((item.get("customFields") or {}).get(key))


def first_offer(item):
    offers = item.get("offers") or []
    for offer in offers:
        if isinstance(offer, dict):
            return offer
    return {}


def date_value(value):
    value = clean_text(value)
    if not value:
        return "Unknown"
    return value.split("T", 1)[0]


def aired_date(item):
    return date_value(
        custom_field(item, "AvailableFrom")
        or first_offer(item).get("startDate")
        or item.get("publishDate")
    )


def source_description(item):
    return clean_text(item.get("description") or item.get("shortDescription")) or "No Description"


def translate_to_english(text):
    try:
        return translate_text(text) or clean_text(text) or "No Description"
    except Exception as exc:
        print(f"{bcolors.WARNING}{icons.ICON_WARNING} Description translation failed: {exc}{bcolors.ENDC}")
        return clean_text(text) or "No Description"


def season_number(item):
    season = item.get("season") or {}
    value = season.get("seasonNumber")
    if value not in (None, ""):
        return int(value)

    details = custom_field(item, "ExtraDetails")
    match = re.search(r"S(?:\u00c6|AE|A)SON\s+(\d+)", details, re.IGNORECASE)
    if match:
        return int(match.group(1))

    play_details = custom_field(item, "PlayButtonExtraDetails")
    match = re.search(r"S(\d+)\s*:", play_details, re.IGNORECASE)
    return int(match.group(1)) if match else 1


def episode_number(item):
    value = item.get("episodeNumber")
    if value not in (None, ""):
        return int(value)

    contextual_title = clean_text(item.get("contextualTitle"))
    match = re.match(r"(\d+)\.", contextual_title)
    if match:
        return int(match.group(1))

    details = custom_field(item, "ExtraDetails")
    match = re.search(r"EPISODE\s+(\d+)", details, re.IGNORECASE)
    return int(match.group(1)) if match else 1


def strip_show_prefix(show_title, item_title):
    show_title = clean_text(show_title)
    item_title = clean_text(item_title)
    prefix = f"{show_title}:"
    if show_title and item_title.lower().startswith(prefix.lower()):
        return clean_text(item_title[len(prefix):])
    return item_title


def search_metadata(video_url, video_id):
    data, item = fetch_episode_data(video_url, video_id)
    item = enrich_episode_context(data, item)
    show = item.get("show") or lookup_item(data, item.get("showId"))
    show_title = clean_text(show.get("title")) or "DRTV"
    title = clean_text(item.get("title") or item.get("episodeName")) or show_title
    description = source_description(item)

    return Metadata(
        title=show_title,
        season=season_number(item),
        episode=episode_number(item),
        episode_title=strip_show_prefix(show_title, title),
        aired_date=aired_date(item),
        description=translate_to_english(description),
        video_id=video_id,
    )


def get_playback_info(video_url, metadata):
    _, item = fetch_episode_data(video_url, metadata.video_id)
    response = session.get(
        f"{API_BASE_URL}/items/{metadata.video_id}/videos",
        params={
            "delivery": "stream,progressive",
            "resolution": "HD-1080",
            "device": "web_browser",
            "sub": "Anonymous",
        },
        headers={**DEFAULT_HEADERS, "Accept": "application/json"},
        timeout=30,
    )
    response.raise_for_status()
    streams = response.json()

    hls_streams = [
        stream for stream in streams
        if isinstance(stream, dict)
        and ".m3u8" in clean_text(stream.get("url")).lower()
    ]
    downloadable_hls_streams = [stream for stream in hls_streams if hls_stream_is_downloadable(stream)]
    if downloadable_hls_streams:
        downloadable_hls_streams.sort(key=stream_sort_key, reverse=True)
        selected = downloadable_hls_streams[0]
        manifest_text = fetch_manifest(selected["url"])
        manifest_streams, manifest_type = parse_manifest_streams(manifest_text)
        subtitle_streams = subtitle_info_streams(selected.get("subtitles") or [])
        return PlaybackInfo(
            manifest_url=selected["url"],
            manifest_type=manifest_type,
            metadata=metadata,
            subtitles=selected.get("subtitles") or [],
            streams=manifest_streams + subtitle_streams,
            keys=[],
        )

    manifest_url = custom_field(item, "ShortVideoUrl")
    if manifest_url and ".m3u8" in manifest_url.lower():
        print(f"{bcolors.WARNING}{icons.ICON_WARNING} Warning: using DRTV ShortVideoUrl fallback; this may be a preview stream.{bcolors.ENDC}")
        manifest_text = fetch_manifest(manifest_url)
        manifest_streams, manifest_type = parse_manifest_streams(manifest_text)
        return PlaybackInfo(
            manifest_url=manifest_url,
            manifest_type=manifest_type,
            metadata=metadata,
            subtitles=[],
            streams=manifest_streams,
            keys=[],
        )

    if hls_streams:
        raise RuntimeError("DRTV returned HLS streams, but they appear to use unsupported DRM.")

    raise RuntimeError("Could not find a downloadable DRTV HLS manifest in the playback response.")


def hls_stream_is_downloadable(stream):
    drm = clean_text(stream.get("drm")).lower()
    if drm in ("", "none"):
        return True

    manifest_url = clean_text(stream.get("url"))
    try:
        master = session.get(manifest_url, headers=DEFAULT_HEADERS, timeout=30)
        master.raise_for_status()
        child_url = first_child_playlist_url(manifest_url, master.text)
        if not child_url:
            return "EXT-X-KEY" not in master.text

        child = session.get(child_url, headers=DEFAULT_HEADERS, timeout=30)
        child.raise_for_status()
        key_line = next((line for line in child.text.splitlines() if line.startswith("#EXT-X-KEY")), "")
        if not key_line:
            return True

        method = attribute_value(key_line, "METHOD").upper()
        if method != "AES-128":
            return False

        key_url = attribute_value(key_line, "URI")
        if not key_url:
            return False
        key_url = urljoin(child_url, key_url)
        key_response = session.get(key_url, headers=DEFAULT_HEADERS, timeout=30)
        return key_response.status_code == 200 and len(key_response.content) == 16
    except requests.RequestException:
        return False


def subtitle_info_streams(subtitles):
    streams = []
    seen = set()
    for subtitle in subtitles or []:
        if not isinstance(subtitle, dict):
            continue
        link = clean_text(subtitle.get("link"))
        language = clean_text(subtitle.get("language") or subtitle.get("label") or "da")
        if not link or link in seen:
            continue
        seen.add(link)
        streams.append({
            "type": "Sub",
            "resolution": "-",
            "bitrate": "-",
            "codec": "vtt" if ".vtt" in link.lower() else "-",
            "lang": language or "da",
            "channels": "-",
        })
    return streams


def first_child_playlist_url(manifest_url, manifest_text):
    child_urls = [
        line.strip()
        for line in manifest_text.splitlines()
        if line.strip() and not line.startswith("#")
    ]
    if not child_urls:
        return None
    return urljoin(manifest_url, child_urls[-1])


def attribute_value(line, name):
    marker = f"{name}="
    start = line.find(marker)
    if start < 0:
        return ""
    start += len(marker)
    if start < len(line) and line[start] == '"':
        start += 1
        end = line.find('"', start)
        return line[start:end] if end >= 0 else line[start:]
    end = line.find(",", start)
    return line[start:] if end < 0 else line[start:end]


def parse_attribute_list(value):
    return {
        match.group(1): match.group(2).strip().strip('"')
        for match in re.finditer(r'([A-Z0-9-]+)=("[^"]*"|[^,]*)', value)
    }


def format_bitrate(value):
    try:
        bitrate = int(float(value))
    except (TypeError, ValueError):
        return "-"
    return f"{bitrate / 1000000:.2f} Mbps" if bitrate >= 1000000 else f"{bitrate // 1000} Kbps"


def info_stream_sort_key(stream):
    type_order = {"Vid": 0, "Aud": 1, "Sub": 2}
    height_match = re.search(r"x(\d+)", stream.get("resolution") or "")
    height = int(height_match.group(1)) if height_match else 0
    bitrate_text = stream.get("bitrate") or ""
    bitrate_match = re.search(r"[\d.]+", bitrate_text)
    bitrate = float(bitrate_match.group()) if bitrate_match else 0
    if "Mbps" in bitrate_text:
        bitrate *= 1000
    return (type_order.get(stream.get("type"), 9), -height, -bitrate, stream.get("lang") or "")


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
    return sorted(streams, key=info_stream_sort_key)


def parse_dash_streams(manifest_text):
    root = ET.fromstring(manifest_text)
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
    return sorted(streams, key=info_stream_sort_key)


def parse_manifest_streams(manifest_text):
    if manifest_text.lstrip().startswith("#EXTM3U"):
        return parse_hls_streams(manifest_text), "HLS"
    return parse_dash_streams(manifest_text), "DASH"


def fetch_manifest(manifest_url):
    response = session.get(manifest_url, headers=DEFAULT_HEADERS, timeout=30)
    response.raise_for_status()
    return response.text


def extract_hls_aes_keys(manifest_url, manifest_text):
    key_lines = [line.strip() for line in manifest_text.splitlines() if line.startswith("#EXT-X-KEY")]
    if not key_lines:
        child_url = first_child_playlist_url(manifest_url, manifest_text)
        if child_url:
            try:
                child = session.get(child_url, headers=DEFAULT_HEADERS, timeout=30)
                child.raise_for_status()
                key_lines = [line.strip() for line in child.text.splitlines() if line.startswith("#EXT-X-KEY")]
            except requests.RequestException:
                key_lines = []
    keys = []
    for key_line in key_lines:
        method = attribute_value(key_line, "METHOD").upper()
        key_url = attribute_value(key_line, "URI")
        if method and method != "NONE" and key_url:
            keys.append(f"{method} {urljoin(manifest_url, key_url)}")
    return list(dict.fromkeys(keys))


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


def highest_stream_resolution(streams, default="Unknown"):
    heights = []
    for stream in streams or []:
        if stream.get("type") != "Vid":
            continue
        match = re.search(r"x(\d+)", stream.get("resolution") or "")
        if match:
            heights.append(int(match.group(1)))
    return f"{max(heights)}p" if heights else default


def stream_sort_key(stream):
    resolution = clean_text(stream.get("resolution"))
    match = re.search(r"(\d+)", resolution)
    if match:
        return int(match.group(1))
    try:
        return int(stream.get("height") or 0)
    except (TypeError, ValueError):
        return 0


def get_hls_resolution(m3u8_url):
    response = session.get(m3u8_url, headers=DEFAULT_HEADERS, timeout=30)
    response.raise_for_status()
    resolutions = re.findall(r"RESOLUTION=\d+x(\d+)", response.text)
    if not resolutions:
        return "Unknown"
    return f"{max(int(height) for height in resolutions)}p"


def get_subtitle_url(playback):
    subtitles = [subtitle for subtitle in playback.subtitles or [] if isinstance(subtitle, dict)]
    subtitles = [subtitle for subtitle in subtitles if clean_text(subtitle.get("link"))]
    if not subtitles:
        return None

    subtitles.sort(key=subtitle_preference_score, reverse=True)
    return clean_text(subtitles[0].get("link"))


def subtitle_preference_score(subtitle):
    language = clean_text(subtitle.get("language")).lower()
    link = clean_text(subtitle.get("link")).lower()
    score = 0

    if language == "danishlanguagesubtitles":
        score += 100
    if language == "combinedlanguagesubtitles":
        score += 90
    if "hardofhearing" in link:
        score += 50
    if "foreign_hardofhearing" in link:
        score += 40
    if language == "foreignlanguagesubtitles":
        score -= 50
    if "foreign-" in link and "hardofhearing" not in link:
        score -= 50

    return score


def strip_vtt_tags(text):
    text = re.sub(r"<\d{2}:\d{2}:\d{2}\.\d{3}>", "", text)
    text = re.sub(r"</?[^>]+>", "", text)
    return html.unescape(clean_text(text))


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
        end = end.split(" ", 1)[0]
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
            "sl": "da",
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


def save_translated_subtitles(playback, filename):
    subtitle_url = get_subtitle_url(playback)
    if not subtitle_url:
        print(f"{bcolors.WARNING}{icons.ICON_WARNING} No Danish subtitle URL found in DRTV playback response.{bcolors.ENDC}")
        return None

    response = session.get(subtitle_url, headers=DEFAULT_HEADERS, timeout=30)
    response.raise_for_status()
    cues = parse_vtt(response.text)
    if not cues:
        print(f"{bcolors.WARNING}{icons.ICON_WARNING} No subtitle cues found in DRTV VTT response.{bcolors.ENDC}")
        return None

    output_path = SAVE_PATH / f"{filename}.en.srt"
    print(f"{bcolors.LIGHTBLUE}{icons.ICON_WAITING} Translating Danish subtitles to English SRT...{bcolors.ENDC}")
    write_srt(translate_cues(cues), output_path)
    print(f"{bcolors.OKGREEN}{icons.ICON_SUCCESS} English subtitles saved: {bcolors.ENDC}{output_path}")
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
    value = re.sub(r'[\\/:*?"<>|]', " ", value)
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
    parts.extend([resolution, "DRTV", "WEB-DL", "AAC2.0", "H.264"])
    return ".".join(part for part in parts if part and part != "Unknown")


def build_download_command(playback, filename, interactive=False):
    selectors = "" if interactive else "--select-video best --select-audio best --drop-subtitle all "
    command = (
        f'{N_M3U8DL} "{playback.manifest_url}" '
        f'{selectors}'
        f'-mt -M format=mkv --save-dir "{SAVE_PATH}" --save-name "{filename}"'
    )

    if SERVICE_PROXY:
        command += f' --custom-proxy "{SERVICE_PROXY}"'

    return command


def resolve_video(video_url, interactive=False):
    video_url = canonical_url(video_url)
    video_id = extract_video_id(video_url)
    metadata = search_metadata(video_url, video_id)
    playback = get_playback_info(video_url, metadata)
    resolution = highest_stream_resolution(playback.streams, get_hls_resolution(playback.manifest_url))
    filename = format_filename(metadata, resolution)
    command = build_download_command(playback, filename, interactive=interactive)
    return playback, resolution, filename, command


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


def print_episode_metadata(metadata):
    print(f"\n{bcolors.YELLOW}Episode metadata:{bcolors.ENDC}")
    print(f"{bcolors.LIGHTBLUE}Show: {bcolors.ENDC}{metadata.title}")
    print(f"{bcolors.LIGHTBLUE}Title: {bcolors.ENDC}{metadata.episode_title or 'Unknown'}")
    print(f"{bcolors.LIGHTBLUE}Date Aired: {bcolors.ENDC}{metadata.aired_date or 'Unknown'}")
    print(f"{bcolors.LIGHTBLUE}Description: {bcolors.ENDC}{metadata.description or 'No Description'}")


def print_info_mode(video_url):
    if not is_episode_url(video_url):
        raise ValueError("Info mode requires a DRTV episode URL.")
    playback, resolution, filename, command = run_with_spinner(lambda: resolve_video(video_url))
    manifest_type = "DASH" if playback.manifest_type.upper() == "DASH" else "HLS"
    print(f"{bcolors.LIGHTBLUE}{manifest_type} Manifest URL: {bcolors.ENDC}{playback.manifest_url}")
    for key in playback.keys or []:
        print(f"{bcolors.GREEN}KEYS: {bcolors.ENDC}{key}")
    print_streams(playback.streams or [])
    print_episode_metadata(playback.metadata)
    print(f"\n{bcolors.YELLOW}Suggested filename: {bcolors.ENDC}{filename}.mkv")


def episode_show_title(item):
    if item.get("_drtv_show_title"):
        return clean_text(item.get("_drtv_show_title"))
    show = item.get("show") or {}
    season = item.get("season") or {}
    title = clean_text(show.get("title") or season.get("title"))
    if title:
        return title
    title = clean_text(item.get("title") or item.get("episodeName"))
    return clean_text(title.split(":", 1)[0]) if ":" in title else title or "DRTV"


def episode_item_title(item):
    show_title = episode_show_title(item)
    title = clean_text(item.get("title") or item.get("episodeName")) or f"Episode {episode_number(item)}"
    prefix = f"{show_title}:"
    if show_title and title.lower().startswith(prefix.lower()):
        return clean_text(title[len(prefix):])
    return title


def episode_item_url(item):
    path = clean_text(item.get("path") or item.get("watchPath"))
    if not path:
        return ""
    if re.match(r"^https?://", path, re.IGNORECASE):
        return path
    if path.startswith("/drtv/"):
        return urljoin(BASE_URL, path)
    if path.startswith(("/episode/", "/serie/", "/saeson/", "/se/")):
        return urljoin(BASE_URL, f"/drtv{path}")
    return urljoin(BASE_URL, path)


def to_int(value, default=None):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def build_series_episode_item(item):
    return {
        "id": clean_text(item.get("id")),
        "show_title": episode_show_title(item),
        "season": season_number(item),
        "episode": episode_number(item),
        "title": episode_item_title(item),
        "url": episode_item_url(item),
    }


def collect_episode_items(series_url):
    response = session.get(canonical_url(series_url), headers=DEFAULT_HEADERS, timeout=30)
    response.raise_for_status()
    data = parse_window_data(response.text)
    wanted_id = extract_video_id(series_url) if is_episode_url(series_url) else None
    raw_episodes = [
        enrich_episode_context(data, item)
        for item in collect_page_episode_nodes(data, wanted_id=wanted_id)
    ]
    if not raw_episodes:
        raise RuntimeError("No DRTV episodes found for this URL.")

    items = []
    seen = set()
    for raw in raw_episodes:
        item = build_series_episode_item(raw)
        if not item["id"] or item["id"] in seen:
            continue
        if not item["url"]:
            continue
        seen.add(item["id"])
        items.append(item)
    items.sort(key=lambda item: (item.get("season") or 0, item.get("episode") or 0, item.get("title") or "", item.get("id") or ""))
    return items


def episode_series_number(item):
    return to_int(item.get("season"))


def episode_tree_number(item):
    return to_int(item.get("episode"))


def episode_tree_label(item):
    number = episode_tree_number(item)
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
        print(f"{icons.ICON_FAILURE} {bcolors.FAIL}No DRTV episodes found.{bcolors.ENDC}")
        return
    show_title = episode_items[0].get("show_title", "DRTV")
    grouped_items = group_episode_items_by_series(episode_items)
    group_labels = sorted(grouped_items, key=series_group_sort_key)
    series_summary = ",  ".join(f"{label}({len(grouped_items[label])})" for label in group_labels)
    print(f"{icons.ICON_SUCCESS} {bcolors.OKGREEN}Found {len(episode_items)} DRTV episodes{bcolors.ENDC}")
    print()
    print_series_rule("DRTV Series", show_title)
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
            "Examples: s01e01, s01, s01e01-s01e02"
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
        episode = episode_tree_number(item)
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
        raise ValueError(f"No DRTV episodes found for selector {format_download_selector(parsed_selector)}.")
    selected.sort(key=lambda item: (episode_series_number(item) or 0, episode_tree_number(item) or 0, item.get("id") or ""))
    return selected


def print_download_queue(episode_items):
    print()
    print(f"{bcolors.LIGHTBLUE}Download queue:{bcolors.ENDC}")
    for item in episode_items:
        season = episode_series_number(item)
        episode = episode_tree_number(item)
        selector = format_queue_selector(season, episode) if season is not None and episode is not None else item["id"]
        _, title = episode_tree_label(item)
        print(f"{selector} {title}")


def print_playback_details(playback, resolution, command):
    print(f"{bcolors.LIGHTBLUE}M3U8 URL: {bcolors.ENDC}{playback.manifest_url}")
    print(f"{bcolors.YELLOW}DOWNLOAD COMMAND: {bcolors.ENDC}")
    print(mask_proxy_command(command))


def maybe_download(command, auto_download=False):
    if auto_download:
        print(f"{bcolors.OKBLUE}{icons.ICON_WAITING} Downloading video...{bcolors.ENDC}")
        subprocess.run(command, shell=True)
        return

    try:
        user_input = input("Do you wish to download? Y or N: ").strip().lower()
    except EOFError:
        user_input = "n"

    if user_input == "y":
        print(f"{bcolors.OKBLUE}{icons.ICON_WAITING} Downloading video...{bcolors.ENDC}")
        subprocess.run(command, shell=True)
    else:
        print(f"{bcolors.RED}{icons.ICON_FAILURE} Download cancelled{bcolors.ENDC}")


def process_video(video_url, auto_download=False, interactive=False):
    print(f"{bcolors.LIGHTBLUE}{icons.ICON_WAITING} Processing: {bcolors.ENDC}{video_url}")
    playback, resolution, filename, command = run_with_spinner(lambda: resolve_video(video_url, interactive=interactive))
    metadata = playback.metadata
    episode_str = f"S{metadata.season:02d}E{metadata.episode:02d}" if metadata.season and metadata.episode else ""
    print(f"{bcolors.OKGREEN}{metadata.title}{bcolors.ENDC} {episode_str} - {metadata.episode_title or ''}".rstrip())

    print_playback_details(playback, resolution, command)
    subtitle_path = maybe_save_translated_subtitles(playback, filename, auto_download=auto_download)
    #if subtitle_path:
        #print(f"{bcolors.YELLOW}{icons.ICON_INFO} External English SRT: {bcolors.ENDC}{subtitle_path}")
    maybe_download(command, auto_download=auto_download)


def download_selected_episodes(series_url, selector):
    episode_items = select_episode_items(series_url, selector)
    print_download_queue(episode_items)
    episode_word = "episode" if len(episode_items) == 1 else "episodes"
    this_or_these = "this" if len(episode_items) == 1 else "these"
    user_input = input(f"Do you wish to download {this_or_these} {len(episode_items)} {episode_word}? Y or N: ").strip().lower()
    if user_input != "y":
        print(f"{bcolors.RED}{icons.ICON_FAILURE} Download cancelled{bcolors.ENDC}")
        return

    for index, item in enumerate(episode_items, 1):
        print()
        print(f"{bcolors.LIGHTBLUE}{icons.ICON_WAITING} Downloading {index}/{len(episode_items)}: {bcolors.ENDC}{item['url']}")
        process_video(item["url"], auto_download=True)


def safe_filename(value):
    return re.sub(r"[^A-Za-z0-9._-]+", "_", str(value or "drtv")).strip("._") or "drtv"


def export_episode_urls(episode_items):
    if not episode_items:
        print(f"{icons.ICON_FAILURE} {bcolors.FAIL}No DRTV episodes found to export.{bcolors.ENDC}")
        return

    export_dir = Path(__file__).resolve().parents[2] / "export"
    export_dir.mkdir(parents=True, exist_ok=True)
    show_title = episode_items[0].get("show_title", "DRTV")
    output_path = export_dir / f"drtv_{safe_filename(show_title)}_episodes.txt"
    output_path.write_text("\n".join(item["url"] for item in episode_items) + "\n", encoding="utf-8")
    print(f"{icons.ICON_SUCCESS} {bcolors.OKGREEN}Exported list: {output_path}{bcolors.ENDC}")


def main(video_url, downloads_path, wvd_device_path, mode="auto", export_list=False, download_selector=None):
    """Eurovine entry point for DRTV DRM-free HLS."""
    try:
        if not video_url:
            raise ValueError("No DRTV URL provided.")
        if not downloads_path:
            raise ValueError("Eurovine config requires downloads_path for DRTV.")

        configure_service(downloads_path, wvd_device_path)
        video_url = video_url.strip()
        print(f"{bcolors.LIGHTBLUE}DRTV URL: {bcolors.ENDC}{video_url}")

        if mode == "list":
            if is_episode_url(video_url):
                print(f"{bcolors.FAIL}{icons.ICON_FAILURE} List mode requires a DRTV series URL, not an episode URL.{bcolors.ENDC}")
                return
            print(f"{bcolors.LIGHTBLUE}{icons.ICON_WAITING} Retrieving series information.....{bcolors.ENDC}")
            episode_items = collect_episode_items(video_url)
            list_episode_items(episode_items)
            if export_list:
                export_episode_urls(episode_items)
            return

        if mode == "download":
            if is_episode_url(video_url):
                print(f"{bcolors.FAIL}{icons.ICON_FAILURE} Download selector mode requires a DRTV series URL, not an episode URL.{bcolors.ENDC}")
                return
            print(f"{bcolors.LIGHTBLUE}{icons.ICON_WAITING} Retrieving series information.....{bcolors.ENDC}")
            download_selected_episodes(video_url, download_selector)
            return

        if mode == "info":
            if not is_episode_url(video_url):
                print(f"{bcolors.FAIL}{icons.ICON_FAILURE} Info mode requires a DRTV episode URL, not a series URL.{bcolors.ENDC}")
                return
            print_info_mode(video_url)
            return

        if export_list:
            if is_episode_url(video_url):
                print(f"{bcolors.FAIL}{icons.ICON_FAILURE} Export mode requires a DRTV series URL, not an episode URL.{bcolors.ENDC}")
                return
            print(f"{bcolors.LIGHTBLUE}{icons.ICON_WAITING} Retrieving series information.....{bcolors.ENDC}")
            export_episode_urls(collect_episode_items(video_url))
            return

        if is_episode_url(video_url):
            process_video(video_url, interactive=(mode == "interactive"))
            return

        if is_series_url(video_url):
            print(f"{bcolors.WARNING}{icons.ICON_WARNING} Series URLs require a flag. Use --list/-l to list episodes, --export/-x to export episode URLs, or --download/-d SELECTOR to download selected episodes.{bcolors.ENDC}")
            return

        process_video(video_url, interactive=(mode == "interactive"))
    except Exception as exc:
        print(f"{bcolors.FAIL}{icons.ICON_FAILURE} Error: {exc}{bcolors.ENDC}")


if __name__ == "__main__":
    print("Run DRTV through eurovine.py so it can use the shared Eurovine configuration.")
