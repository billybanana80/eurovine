import base64
import html
import json
import re
import shutil
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional
from urllib.parse import urlencode, urljoin, urlparse

import requests
import urllib3
from beaupy.spinners import Spinner
import icons
from colors import bcolors
from services.proxy import current_proxy_url, mask_proxy_command


for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

SERVICE_NAME = "ruv"
BASE_URL = "https://www.ruv.is"
TRANSLATE_URL = "https://translate.googleapis.com/translate_a/single"
TRANSLATE_BATCH_MARKER = "@@RUVBREAK@@"
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
    "Accept-Language": "is-IS,is;q=0.9,en-US;q=0.7,en;q=0.6",
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
    aired_date: str = "Unknown"
    description: str = "No Description"
    video_id: Optional[str] = None
    video_url: Optional[str] = None
    duration: Optional[str] = None


@dataclass
class PlaybackInfo:
    manifest_url: str
    manifest_type: str = "m3u8"
    subtitles: list = field(default_factory=list)
    streams: list = field(default_factory=list)
    metadata: Metadata = field(default_factory=Metadata)


def clean_text(value):
    return re.sub(r"\s+", " ", html.unescape(str(value or ""))).strip()


def canonical_url(value):
    value = clean_text(value)
    if re.match(r"^https?://", value, re.I):
        return value
    if value.startswith("/"):
        return urljoin(BASE_URL, value)
    return urljoin(BASE_URL, f"/sjonvarp/spila/{value.strip('/')}")


def fetch_text(url, headers=None, attempts=4):
    request_headers = dict(DEFAULT_HEADERS)
    if headers:
        request_headers.update(headers)

    last_error = None
    for attempt in range(1, attempts + 1):
        try:
            response = session.get(url, headers=request_headers, timeout=35)
            if 400 <= response.status_code < 500:
                raise RuntimeError(f"RUV request failed with HTTP {response.status_code}: {response.text[:300]}")
            response.raise_for_status()
            response.encoding = response.encoding or "utf-8"
            return response.text
        except (requests.RequestException, RuntimeError) as exc:
            last_error = exc
        if attempt < attempts:
            time.sleep(0.75 * attempt)

    raise last_error


def parse_apollo_state(html_text):
    match = re.search(r"window\.__APOLLO_STATE__\s*=\s*", html_text)
    if not match:
        raise RuntimeError("Could not find RUV Apollo state in the page HTML.")
    try:
        data, _ = json.JSONDecoder().raw_decode(html_text[match.end():])
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Could not parse RUV Apollo state: {exc}") from exc
    return data


def program_id_from_url(url):
    parts = [part for part in urlparse(canonical_url(url)).path.split("/") if part]
    for part in parts:
        if part.isdigit():
            return part
    return ""


def extract_video_id(video_url):
    parts = [part for part in urlparse(canonical_url(video_url)).path.split("/") if part]
    for index, part in enumerate(parts):
        if part.isdigit() and index + 1 < len(parts):
            candidate = parts[index + 1]
            if re.match(r"^[a-z0-9]+$", candidate, re.I):
                return candidate
    raise ValueError("Could not extract RUV episode ID from URL.")


def episode_id_from_url(video_url):
    try:
        return extract_video_id(video_url)
    except ValueError:
        return ""


def is_episode_url(video_url):
    return bool(episode_id_from_url(video_url))


def is_series_url(video_url):
    return bool(program_id_from_url(video_url)) and not is_episode_url(video_url)


def apollo_ref(value):
    if isinstance(value, dict):
        return clean_text(value.get("__ref"))
    return ""


def find_program(data, program_id=""):
    key = f"Program:{program_id}" if program_id else ""
    if key and isinstance(data.get(key), dict):
        return data[key]
    for value in data.values():
        if isinstance(value, dict) and value.get("__typename") == "Program":
            return value
    return {}


def collect_episodes(data, program, wanted_id=""):
    refs = [apollo_ref(item) for item in (program.get("episodes") or [])]
    keys = [ref for ref in refs if ref] or [
        key for key, value in data.items() if key.startswith("Episode:") and isinstance(value, dict)
    ]
    episodes = []
    seen = set()
    for key in keys:
        item = data.get(key) or {}
        episode_id = clean_text(item.get("id") or key.split(":", 1)[-1])
        if wanted_id and episode_id != wanted_id:
            continue
        if episode_id and episode_id not in seen:
            seen.add(episode_id)
            episodes.append(dict(item))
    if wanted_id and not episodes and isinstance(data.get(f"Episode:{wanted_id}"), dict):
        episodes.append(dict(data[f"Episode:{wanted_id}"]))
    return episodes


def merge_episode(base, extra):
    merged = dict(base)
    for key, value in extra.items():
        if value not in (None, "", [], {}):
            merged[key] = value
    return merged


def episode_url(program, episode):
    slug = clean_text(program.get("slug"))
    program_id = clean_text(program.get("id"))
    episode_id = clean_text(episode.get("id"))
    if not slug or not program_id or not episode_id:
        return ""
    return f"{BASE_URL}/sjonvarp/spila/{slug}/{program_id}/{episode_id}"


def hydrate_episode(program, item):
    url = episode_url(program, item)
    if not url:
        return item
    data = parse_apollo_state(fetch_text(url))
    full = data.get(f"Episode:{clean_text(item.get('id'))}") or {}
    return merge_episode(item, full) if full else item


def episode_title(item):
    return clean_text(item.get("title"))


def episode_number(item):
    title = clean_text(item.get("title"))
    match = re.search(r"(\d+)\s*(?:af|/)\s*\d+", title, re.I)
    if match:
        return int(match.group(1))
    match = re.search(r"\d+", clean_text(item.get("id")))
    return int(match.group(0)) if match else 1


def season_number(item):
    try:
        return int(clean_text(item.get("season") or item.get("season_number") or "1"))
    except ValueError:
        return 1


def show_title(program):
    return clean_text(program.get("title")) or "Unknown"


def duration_text(item):
    try:
        total_seconds = int(item.get("duration"))
    except (TypeError, ValueError):
        return None
    if total_seconds <= 0:
        return None
    minutes, seconds = divmod(total_seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes}:{seconds:02d}"


def date_value(value):
    value = clean_text(value)
    if not value:
        return "Unknown"
    normalised = value.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(normalised).strftime("%Y-%m-%d")
    except ValueError:
        return value.split("T", 1)[0]


def source_description(item, program):
    return (
        clean_text(item.get("description"))
        or clean_text(program.get("description"))
        or clean_text(program.get("short_description"))
        or "No Description"
    )


def translate_to_english(text):
    text = clean_text(text)
    if not text or text == "No Description":
        return "No Description"
    try:
        return translate_text(text) or text
    except Exception as exc:
        print(f"{bcolors.WARNING}{icons.ICON_WARNING} Description translation failed, keeping Icelandic text: {exc}{bcolors.ENDC}")
        return text


def search_metadata(video_url, video_id):
    source_url = canonical_url(video_url)
    data = parse_apollo_state(fetch_text(source_url))
    program = find_program(data, program_id_from_url(source_url))
    if not program:
        raise RuntimeError("No RUV program found for this URL.")

    episodes = collect_episodes(data, program, wanted_id=video_id)
    if not episodes:
        raise RuntimeError("No RUV episode found for this URL.")
    item = episodes[0]
    if not item.get("file") or not item.get("duration"):
        item = hydrate_episode(program, item)

    item_title = episode_title(item)
    is_standalone = len(episodes) == 1 and not item_title

    return Metadata(
        title=show_title(program),
        season=None if is_standalone else season_number(item),
        episode=None if is_standalone else episode_number(item),
        episode_title=item_title or None,
        aired_date=date_value(item.get("firstrun")),
        description=translate_to_english(source_description(item, program)),
        video_id=video_id,
        video_url=episode_url(program, item) or source_url,
        duration=duration_text(item),
    ), item


def get_playback_info(video_url, metadata, episode_item):
    manifest_url = clean_text(episode_item.get("file"))
    if not manifest_url:
        raise RuntimeError("No RUV HLS URL found in episode metadata.")

    subtitles = []
    for subtitle in episode_item.get("subtitles") or []:
        if not isinstance(subtitle, dict):
            continue
        url = clean_text(subtitle.get("url") or subtitle.get("href") or subtitle.get("file") or subtitle.get("value"))
        if url:
            subtitles.append(
                {
                    "url": urljoin(metadata.video_url or video_url, url),
                    "language": clean_text(subtitle.get("language") or subtitle.get("lang") or subtitle.get("name")),
                    "label": clean_text(subtitle.get("label") or subtitle.get("title") or subtitle.get("name")),
                }
            )

    manifest_text = fetch_text(manifest_url, headers={"Accept": "application/vnd.apple.mpegurl,application/dash+xml,*/*"})
    streams, manifest_type = parse_manifest_streams(manifest_text)
    streams.extend(subtitle_info_streams(subtitles))
    return PlaybackInfo(
        manifest_url=manifest_url,
        manifest_type=manifest_type.lower(),
        subtitles=subtitles,
        streams=streams,
        metadata=metadata,
    )


def get_hls_resolution(m3u8_url):
    text = fetch_text(m3u8_url, headers={"Accept": "application/vnd.apple.mpegurl,*/*"})
    resolutions = re.findall(r"RESOLUTION=\d+x(\d+)", text)
    if not resolutions:
        return "Unknown"
    return f"{max(int(height) for height in resolutions)}p"


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


def parse_manifest_streams(manifest_text):
    if manifest_text.lstrip().startswith("#EXTM3U"):
        return parse_hls_streams(manifest_text), "HLS"
    return parse_dash_streams(manifest_text), "DASH"


def parse_hls_streams(manifest_text):
    streams = []
    pending_variant = None
    for raw_line in manifest_text.splitlines():
        line = raw_line.strip()
        if line.startswith("#EXT-X-STREAM-INF:"):
            pending_variant = parse_hls_attribute_list(line)
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
        attrs = parse_hls_attribute_list(line)
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


def subtitle_info_streams(subtitles):
    streams = []
    seen = set()
    for subtitle in subtitles or []:
        url = subtitle_url(subtitle)
        if not url or url in seen:
            continue
        seen.add(url)
        streams.append({
            "type": "Sub",
            "resolution": "-",
            "bitrate": "-",
            "codec": "vtt" if ".vtt" in url.lower() or ".m3u8" in url.lower() else "-",
            "lang": clean_text(subtitle.get("language") or subtitle.get("label") or subtitle.get("name")) if isinstance(subtitle, dict) else "-",
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
        if not lines or lines[0].upper().startswith(("WEBVTT", "NOTE", "STYLE", "REGION", "X-TIMESTAMP-MAP")):
            continue
        time_index = next((index for index, line in enumerate(lines) if "-->" in line), None)
        if time_index is None:
            continue
        start, _, end = lines[time_index].partition("-->")
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
        params={"client": "gtx", "sl": "is", "tl": "en", "dt": "t", "q": text},
        headers={"Accept": "application/json,text/plain,*/*", "User-Agent": DEFAULT_HEADERS["User-Agent"]},
        timeout=30,
    )
    response.raise_for_status()
    payload = response.json()
    translated = "".join(part[0] for part in payload[0] if part and part[0])
    return clean_text(translated)


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


def write_srt(cues, output_path):
    with open(output_path, "w", encoding="utf-8") as file:
        for index, cue in enumerate(cues, 1):
            file.write(f"{index}\n{cue['start']} --> {cue['end']}\n{cue['text']}\n\n")


def parse_hls_attribute_list(line):
    _, _, payload = line.partition(":")
    attrs = {}
    for match in re.finditer(r'([A-Z0-9-]+)=("[^"]*"|[^,]*)', payload, re.I):
        value = match.group(2).strip()
        if value.startswith('"') and value.endswith('"'):
            value = value[1:-1]
        attrs[match.group(1).lower()] = html.unescape(value)
    return attrs


def subtitle_url(subtitle):
    if isinstance(subtitle, str):
        return subtitle
    if not isinstance(subtitle, dict):
        return ""
    return clean_text(subtitle.get("url") or subtitle.get("webVtt") or subtitle.get("href"))


def subtitle_preference_score(subtitle):
    text = json.dumps(subtitle, ensure_ascii=False).lower() if isinstance(subtitle, dict) else clean_text(subtitle).lower()
    score = 0
    if "muninn.nyr.ruv.is/files/subtitles" in text:
        score += 220
    if '"language": "is"' in text or '"label": "is"' in text or '"name": "is"' in text:
        score += 140
    if "ice" in text or "isl" in text or "icelandic" in text or "islenska" in text or "íslenska" in text:
        score += 120
    if "default=yes" in text or '"default": "yes"' in text:
        score += 40
    if ".vtt" in text or ".m3u8" in text or "webvtt" in text:
        score += 10
    return score


def subtitles_from_hls_manifest(manifest_url):
    text = fetch_text(manifest_url, headers={"Accept": "application/vnd.apple.mpegurl,*/*"})
    subtitles = []
    for line in text.splitlines():
        if "#EXT-X-MEDIA" not in line or "TYPE=SUBTITLES" not in line.upper():
            continue
        attrs = parse_hls_attribute_list(line)
        uri = attrs.get("uri")
        if uri:
            subtitles.append(
                {
                    "url": urljoin(manifest_url, uri),
                    "manifest_line": line,
                    "name": clean_text(attrs.get("name")),
                    "language": clean_text(attrs.get("language")),
                    "default": clean_text(attrs.get("default")),
                    "autoselect": clean_text(attrs.get("autoselect")),
                }
            )
    return subtitles


def get_subtitle(playback):
    subtitles = [subtitle for subtitle in playback.subtitles or [] if subtitle_url(subtitle)]
    try:
        subtitles.extend(subtitles_from_hls_manifest(playback.manifest_url))
    except Exception as exc:
        print(f"{bcolors.WARNING}{icons.ICON_WARNING} Could not inspect HLS subtitles: {exc}{bcolors.ENDC}")
    subtitles = [subtitle for subtitle in subtitles if subtitle_url(subtitle)]
    if not subtitles:
        return None
    subtitles.sort(key=subtitle_preference_score, reverse=True)
    return subtitles[0]


def vtt_segment_urls(playlist_url, playlist_text):
    urls = []
    for line in playlist_text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        urls.append(urljoin(playlist_url, line))
    return urls


def fetch_subtitle_text(url):
    text = fetch_text(url, headers={"Accept": "application/vnd.apple.mpegurl,text/vtt,text/plain,*/*"})
    if "#EXTM3U" not in text[:200].upper():
        return text

    segments = vtt_segment_urls(url, text)
    parts = []
    total = len(segments)
    if total:
        print(f"{bcolors.LIGHTBLUE}{icons.ICON_WAITING} Fetching {total} RUV subtitle segment(s)...{bcolors.ENDC}")
    for index, segment_url in enumerate(segments, 1):
        if total and (index == 1 or index == total or index % 100 == 0):
            print(f"\r{bcolors.LIGHTBLUE}{progress_bar(index, total)}{bcolors.ENDC}", end="", flush=True)
        parts.append(fetch_text(segment_url, headers={"Accept": "text/vtt,text/plain,*/*"}))
    if total:
        print()
    return "\n\n".join(parts)


def save_translated_subtitles(playback, filename):
    subtitle = get_subtitle(playback)
    if not subtitle:
        print(f"{bcolors.WARNING}{icons.ICON_WARNING} No Icelandic subtitle URL found in RUV playback or manifest.{bcolors.ENDC}")
        return None

    url = subtitle_url(subtitle)
    print(f"{bcolors.LIGHTBLUE}Subtitle URL: {bcolors.ENDC}{url}")
    text = fetch_subtitle_text(url)
    cues = parse_vtt(text)
    if not cues:
        print(f"{bcolors.WARNING}{icons.ICON_WARNING} No subtitle cues found in RUV subtitle response.{bcolors.ENDC}")
        return None

    output_path = SAVE_PATH / f"{filename}.en.srt"
    print(f"{bcolors.LIGHTBLUE}{icons.ICON_WAITING} Translating Icelandic subtitles to English SRT...{bcolors.ENDC}")
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
    parts.extend([resolution, "RUV", "WEB-DL", "AAC2.0", "H.264"])
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
    metadata, episode_item = search_metadata(video_url, video_id)
    playback = get_playback_info(video_url, metadata, episode_item)
    resolution = highest_stream_resolution(
        playback.streams,
        get_hls_resolution(playback.manifest_url) if playback.manifest_type == "hls" else "Unknown",
    )
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
        raise ValueError("Info mode requires an RUV episode URL.")
    playback, resolution, filename, command = run_with_spinner(lambda: resolve_video(video_url))
    manifest_label = "DASH Manifest URL" if playback.manifest_type == "dash" else "HLS Manifest URL"
    print(f"{bcolors.LIGHTBLUE}{manifest_label}: {bcolors.ENDC}{playback.manifest_url}")
    print_streams(playback.streams)
    print_episode_metadata(playback.metadata)
    print(f"\n{bcolors.YELLOW}Suggested filename: {bcolors.ENDC}{filename}.mkv")


def build_series_episode_item(program, item):
    return {
        "id": clean_text(item.get("id")),
        "show_title": show_title(program),
        "season": season_number(item),
        "episode": episode_number(item),
        "title": episode_title(item) or f"Episode {episode_number(item)}",
        "url": episode_url(program, item),
    }


def collect_episode_items(series_url):
    source_url = canonical_url(series_url)
    data = parse_apollo_state(fetch_text(source_url))
    program = find_program(data, program_id_from_url(source_url))
    if not program:
        raise RuntimeError("No RUV program found for this URL.")

    wanted_id = episode_id_from_url(source_url) if is_episode_url(source_url) else ""
    raw_episodes = collect_episodes(data, program, wanted_id=wanted_id)
    if not raw_episodes:
        raise RuntimeError("No RUV episodes found for this URL.")

    episode_items = []
    seen = set()
    for item in raw_episodes:
        if not item.get("file") or not item.get("duration"):
            try:
                item = hydrate_episode(program, item)
            except Exception as exc:
                print(f"{bcolors.WARNING}{icons.ICON_WARNING} Could not hydrate RUV episode {clean_text(item.get('id'))}: {exc}{bcolors.ENDC}")
        episode_item = build_series_episode_item(program, item)
        key = episode_item["id"] or episode_item["url"]
        if not key or key in seen or not episode_item["url"]:
            continue
        seen.add(key)
        episode_items.append(episode_item)
    episode_items.sort(key=lambda item: (episode_series_number(item) or 0, episode_tree_number(item) or 0, item.get("id") or ""))
    return episode_items


def episode_series_number(item):
    try:
        return int(item.get("season"))
    except (TypeError, ValueError):
        return None


def episode_tree_number(item):
    try:
        return int(item.get("episode"))
    except (TypeError, ValueError):
        return None


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
        print(f"{icons.ICON_FAILURE} {bcolors.FAIL}No RUV episodes found.{bcolors.ENDC}")
        return
    show = episode_items[0].get("show_title", "RUV")
    grouped_items = group_episode_items_by_series(episode_items)
    group_labels = sorted(grouped_items, key=series_group_sort_key)
    series_summary = ",  ".join(f"{label}({len(grouped_items[label])})" for label in group_labels)
    print(f"{icons.ICON_SUCCESS} {bcolors.OKGREEN}Found {len(episode_items)} RUV episodes{bcolors.ENDC}")
    print()
    print_series_rule("RUV Series", show)
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
            "Examples: s01e01, s01, s01e01-s01e03"
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
        raise ValueError(f"No RUV episodes found for selector {format_download_selector(parsed_selector)}.")
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
    #print(f"{bcolors.LIGHTBLUE}Resolution: {bcolors.ENDC}{resolution}")
    #if playback.metadata.duration:
        #print(f"{bcolors.LIGHTBLUE}Duration: {bcolors.ENDC}{playback.metadata.duration}")
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
    playback, resolution, filename, command = run_with_spinner(lambda: resolve_video(video_url, interactive=interactive))
    metadata = playback.metadata
    episode_str = f"S{metadata.season:02d}E{metadata.episode:02d}" if metadata.season and metadata.episode else ""
    if metadata.title != "Unknown" or episode_str or metadata.episode_title:
        label_parts = [part for part in [episode_str, metadata.episode_title] if part]
        suffix = f" {' - '.join(label_parts)}" if label_parts else ""
        print(f"{icons.ICON_SUCCESS} {bcolors.OKGREEN}{metadata.title}{bcolors.ENDC}{suffix}")

    print_playback_details(playback, resolution, command)
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
        print(f"{icons.ICON_FAILURE} {bcolors.FAIL}No RUV episodes found to export.{bcolors.ENDC}")
        return

    export_dir = Path(__file__).resolve().parents[2] / "export"
    export_dir.mkdir(parents=True, exist_ok=True)
    show_title = episode_items[0].get("show_title", "RUV")
    output_path = export_dir / f"ruv_{safe_name(show_title)}_episodes.txt"
    output_path.write_text("\n".join(item["url"] for item in episode_items) + "\n", encoding="utf-8")
    print(f"{icons.ICON_SUCCESS} {bcolors.OKGREEN}Exported list: {output_path}{bcolors.ENDC}")


def main(video_url, downloads_path, wvd_device_path, mode="auto", export_list=False, download_selector=None):
    """Eurovine entry point for RUV DRM-free HLS."""
    try:
        if not video_url:
            raise ValueError("No RUV URL provided.")
        if not downloads_path:
            raise ValueError("Eurovine config requires downloads_path for RUV.")

        configure_service(downloads_path, wvd_device_path)
        video_url = video_url.strip()

        if mode == "list":
            if is_episode_url(video_url):
                print(f"{icons.ICON_FAILURE} {bcolors.FAIL}List mode requires an RUV series URL, not an episode URL.{bcolors.ENDC}")
                return
            print(f"{icons.ICON_WAITING} {bcolors.LIGHTBLUE}Retrieving series information.....{bcolors.ENDC}")
            episode_items = collect_episode_items(video_url)
            list_episode_items(episode_items)
            if export_list:
                export_episode_urls(episode_items)
            return

        if mode == "download":
            if is_episode_url(video_url):
                print(f"{icons.ICON_FAILURE} {bcolors.FAIL}Download selector mode requires an RUV series URL, not an episode URL.{bcolors.ENDC}")
                return
            print(f"{icons.ICON_WAITING} {bcolors.LIGHTBLUE}Retrieving series information.....{bcolors.ENDC}")
            download_selected_episodes(video_url, download_selector)
            return

        if mode == "info":
            if not is_episode_url(video_url):
                print(f"{icons.ICON_FAILURE} {bcolors.FAIL}Info mode requires an RUV episode URL, not a series URL.{bcolors.ENDC}")
                return
            print_info_mode(video_url)
            return

        if export_list:
            if is_episode_url(video_url):
                print(f"{icons.ICON_FAILURE} {bcolors.FAIL}Export mode requires an RUV series URL, not an episode URL.{bcolors.ENDC}")
                return
            print(f"{icons.ICON_WAITING} {bcolors.LIGHTBLUE}Retrieving series information.....{bcolors.ENDC}")
            export_episode_urls(collect_episode_items(video_url))
            return

        if is_episode_url(video_url):
            process_video(video_url, interactive=(mode == "interactive"))
            return

        if is_series_url(video_url):
            print(f"{icons.ICON_WARNING} {bcolors.WARNING}Series URLs require a flag. Use --list/-l to list episodes, --export/-x to export episode URLs, or --download/-d SELECTOR to download selected episodes.{bcolors.ENDC}")
            return

        process_video(video_url, interactive=(mode == "interactive"))
    except Exception as exc:
        print(f"{icons.ICON_FAILURE} {bcolors.FAIL}Error: {exc}{bcolors.ENDC}")


if __name__ == "__main__":
    print("Run RUV through eurovine.py so it can use the shared Eurovine configuration.")
