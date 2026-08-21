"""RTVE Play support for Eurovine."""

import base64
import contextlib
import html
import io
import json
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import requests
import urllib3
import yaml
from beaupy.spinners import Spinner
from pywidevine.cdm import Cdm
from pywidevine.device import Device
from pywidevine.pssh import PSSH

import icons
from colors import bcolors
from download_confirm import confirm_download
from quality_utils import apply_quality_to_filename, video_selector
from services.proxy import current_proxy_url, mask_proxy_command


urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

SERVICE_NAME = "RTVE"
BASE_URL = "https://www.rtve.es"
API_URL = "https://api.rtve.es/api"
LICENSE_URL = "https://3e6900a5.drm-widevine-licensing.axprod.net/AcquireLicense"
COMPLETE_VIDEO_TYPE = 39816
N_M3U8DL = "N_m3u8DL-RE"
CONFIG_PATH = Path(__file__).resolve().parents[2] / "config.yaml"
TRANSLATE_BATCH_MARKER = "@@RTVEBREAK@@"
TRANSLATE_BATCH_SIZE = 40
TRANSLATE_BATCH_CHAR_LIMIT = 4500

DEFAULT_HEADERS = {
    "Accept": "*/*",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36"
    ),
}

session = requests.Session()
SAVE_PATH = Path(".")
WVD_PATH = ""
SERVICE_PROXY = ""


@dataclass
class Metadata:
    title: str = "Unknown"
    season: int | None = None
    episode: int | None = None
    episode_title: str = "Unknown"
    aired_date: str = "Unknown"
    description: str = "No Description"
    video_id: str = ""
    year: int | None = None


@dataclass
class PlaybackInfo:
    manifest_url: str
    metadata: Metadata
    license_url: str = LICENSE_URL
    pssh: str | None = None
    streams: list = field(default_factory=list)
    subtitles: list = field(default_factory=list)


def clean_text(value):
    return re.sub(r"\s+", " ", html.unescape(str(value or ""))).strip()


def clean_description(value):
    value = re.sub(r"<\s*br\s*/?\s*>", " ", str(value or ""), flags=re.IGNORECASE)
    return clean_text(re.sub(r"<[^>]+>", " ", value))


def int_value(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def date_value(value):
    value = clean_text(value)
    if not value:
        return "Unknown"
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).strftime("%Y-%m-%d")
    except ValueError:
        return value.split("T", 1)[0]


def year_value(value):
    match = re.search(r"\b(19|20)\d{2}\b", clean_text(value))
    return int(match.group(0)) if match else None


def configure_service(downloads_path, wvd_device_path):
    global SAVE_PATH, WVD_PATH, SERVICE_PROXY
    SAVE_PATH = Path(downloads_path)
    WVD_PATH = str(wvd_device_path or "")
    SERVICE_PROXY = current_proxy_url()
    session.proxies.clear()
    session.verify = True
    if SERVICE_PROXY:
        session.proxies.update({"http": SERVICE_PROXY, "https": SERVICE_PROXY})
        session.verify = False


def canonical_url(url):
    url = clean_text(url)
    return url if url.startswith("http") else urllib.parse.urljoin(BASE_URL, url)


def extract_video_id(url):
    match = re.search(r"/(\d+)(?:[/?#]|$)", urllib.parse.urlparse(url).path + "/")
    if not match:
        raise ValueError("Could not find an RTVE video ID in this URL.")
    return match.group(1)


def is_episode_url(url):
    return bool(re.search(r"/(\d+)(?:[/?#]|$)", urllib.parse.urlparse(canonical_url(url)).path + "/"))


def fetch_json(url, **kwargs):
    response = session.get(url, headers=DEFAULT_HEADERS, timeout=30, **kwargs)
    response.raise_for_status()
    return response.json()


def fetch_text(url):
    response = session.get(url, headers=DEFAULT_HEADERS, timeout=30)
    response.raise_for_status()
    return response.text


def translate_text(text):
    text = clean_text(text)
    if not text:
        return ""
    response = session.get(
        "https://translate.googleapis.com/translate_a/single",
        params={"client": "gtx", "sl": "es", "tl": "en", "dt": "t", "q": text},
        headers={**DEFAULT_HEADERS, "Accept": "application/json,text/plain,*/*"},
        timeout=30,
    )
    response.raise_for_status()
    return clean_text("".join(part[0] for part in response.json()[0] if part and part[0]))


def translate_to_english(text):
    text = clean_text(text)
    if not text or text == "No Description":
        return "No Description"
    try:
        return translate_text(text) or text
    except Exception as exc:
        print(f"{icons.ICON_WARNING} {bcolors.WARNING}Could not translate description: {exc}{bcolors.ENDC}")
        return text


def metadata_payload(video_id):
    payload = fetch_json(f"{API_URL}/videos/{video_id}.json")
    page = payload.get("page") or {}
    items = page.get("items") or []
    if not items:
        raise ValueError(f"RTVE did not return metadata for video {video_id}.")
    return items[0]


def metadata_from_item(item, video_id=None, translate_description=True):
    show = clean_text((item.get("programInfo") or {}).get("title") or item.get("programTitle") or item.get("program") or "RTVE")
    raw_title = clean_text(item.get("shortTitle") or item.get("title") or item.get("longTitle") or "Unknown")
    season = int_value(item.get("temporadaOrden") or item.get("temporada") or item.get("season"))
    episode = int_value(item.get("episode") or item.get("episodio"))
    if episode is None:
        match = re.search(r"(?:episodio|cap[ií]tulo)\s*(\d+)", raw_title, re.IGNORECASE)
        episode = int_value(match.group(1)) if match else None
    if season is None and episode is not None:
        season = 1
    aired_at = item.get("dateOfEmission") or item.get("publicationDate")
    description = clean_description(item.get("description") or item.get("shortDescription") or "No Description")
    return Metadata(
        title=show,
        season=season,
        episode=episode,
        episode_title=raw_title,
        aired_date=date_value(aired_at),
        description=translate_to_english(description) if translate_description else description,
        video_id=str(video_id or item.get("id") or ""),
        year=year_value(aired_at),
    )


def metadata_for_url(video_url):
    video_id = extract_video_id(video_url)
    return metadata_from_item(metadata_payload(video_id), video_id)


def parse_manifest(manifest_url):
    response = session.get(manifest_url, headers=DEFAULT_HEADERS, timeout=30)
    response.raise_for_status()
    root = ET.fromstring(response.content)
    ns = "{urn:mpeg:dash:schema:mpd:2011}"
    cenc = "{urn:mpeg:cenc:2013}"
    streams, subtitles, pssh = [], [], None
    for protection in root.findall(f".//{ns}ContentProtection"):
        if "edef8ba9-79d6-4ace-a3c8-27dcd51d21ed" in protection.attrib.get("schemeIdUri", "").lower():
            element = protection.find(f"{cenc}pssh")
            if element is not None and element.text:
                pssh = clean_text(element.text)
                break
    for adaptation in root.findall(f".//{ns}AdaptationSet"):
        mime = clean_text(adaptation.attrib.get("mimeType"))
        content_type = clean_text(adaptation.attrib.get("contentType"))
        lang = clean_text(adaptation.attrib.get("lang") or "und")
        is_subtitle = content_type == "text" or mime.startswith("text/") or "ttml" in mime or "wvtt" in mime
        adaptation_base = adaptation.find(f"{ns}BaseURL")
        for representation in adaptation.findall(f"{ns}Representation"):
            rep_base = representation.find(f"{ns}BaseURL")
            base = clean_text(rep_base.text if rep_base is not None else (adaptation_base.text if adaptation_base is not None else ""))
            if is_subtitle:
                if base:
                    subtitles.append({"lang": lang, "url": urllib.parse.urljoin(manifest_url, base), "kind": "dash-vtt"})
                else:
                    subtitles.append({"lang": lang, "kind": "dash-wvtt"})
                streams.append({"type": "Sub", "resolution": "-", "bitrate": "-", "codec": clean_text(representation.attrib.get("codecs") or mime), "lang": lang, "channels": "-"})
                continue
            width, height = representation.attrib.get("width"), representation.attrib.get("height")
            if content_type == "video" or (width and height) or mime.startswith("video/"):
                stream_type, resolution, channels = "Vid", f"{width or '?'}x{height or '?'}", "-"
            elif content_type == "audio" or mime.startswith("audio/"):
                stream_type, resolution, channels = "Aud", "-", clean_text(representation.attrib.get("audioSamplingRate") or "-")
            else:
                continue
            streams.append({"type": stream_type, "resolution": resolution, "bitrate": clean_text(representation.attrib.get("bandwidth") or "-"), "codec": clean_text(representation.attrib.get("codecs") or mime), "lang": lang, "channels": channels})
    return pssh, streams, subtitles


def get_playback_info(video_url, metadata):
    manifest_url = f"https://ztnr.rtve.es/ztnr/{metadata.video_id}.mpd"
    pssh, streams, subtitles = parse_manifest(manifest_url)
    token_payload = fetch_json(f"{API_URL}/token/{metadata.video_id}")
    license_url = clean_text(token_payload.get("widevineURL")) or LICENSE_URL
    return PlaybackInfo(manifest_url=manifest_url, metadata=metadata, license_url=license_url, pssh=pssh, streams=streams, subtitles=subtitles)


def get_keys(pssh, license_url):
    if not WVD_PATH:
        raise ValueError("RTVE requires wvd_device_path in config.yaml to obtain Widevine keys.")
    device = Device.load(WVD_PATH)
    cdm = Cdm.from_device(device)
    session_id = cdm.open()
    try:
        challenge = cdm.get_license_challenge(session_id, PSSH(pssh))
        headers = {"Content-Type": "application/octet-stream", "Origin": BASE_URL, "Referer": f"{BASE_URL}/", "User-Agent": DEFAULT_HEADERS["User-Agent"]}
        response = session.post(license_url, headers=headers, data=challenge, timeout=30)
        response.raise_for_status()
        cdm.parse_license(session_id, response.content)
        return [f"{key.kid.hex}:{key.key.hex()}" for key in cdm.get_keys(session_id) if key.type == "CONTENT"]
    finally:
        cdm.close(session_id)


def safe_name(value):
    value = clean_text(value).replace("'", "")
    value = re.sub(r"[\\/:*?\"<>|]", " ", value)
    return re.sub(r"\s+", ".", value).strip(".") or "Unknown"


def highest_resolution(streams):
    heights = []
    for stream in streams:
        if stream["type"] == "Vid":
            match = re.search(r"x(\d+)", stream["resolution"])
            if match:
                heights.append(int(match.group(1)))
    return f"{max(heights)}p" if heights else "Unknown"


def format_filename(metadata, resolution):
    standalone = metadata.episode is None
    parts = [safe_name(metadata.episode_title if standalone else metadata.title)]
    if metadata.season is not None and metadata.episode is not None:
        parts.append(f"S{metadata.season:02}E{metadata.episode:02}")
    parts.extend([resolution, "RTVE", "WEB-DL", "AAC2.0", "H.264"])
    return ".".join(part for part in parts if part and part != "Unknown")


def build_download_command(playback, filename, keys, interactive=False, quality=None, include_subtitles=False):
    selectors = "" if interactive else f"{video_selector(quality)} --select-audio best {'--select-subtitle all' if include_subtitles else '--drop-subtitle all'} "
    command = f'{N_M3U8DL} "{playback.manifest_url}" {selectors}-mt -M format=mkv --save-dir "{SAVE_PATH}" --save-name "{filename}"'
    if keys:
        command += " " + " ".join(f"--key {key}" for key in keys)
    if SERVICE_PROXY:
        command += f' --custom-proxy "{SERVICE_PROXY}"'
    return command


def resolve_video(video_url, interactive=False, quality=None, include_subtitles=False):
    metadata = metadata_for_url(video_url)
    playback = get_playback_info(video_url, metadata)
    if not playback.pssh:
        raise ValueError("Widevine PSSH not found in RTVE manifest.")
    keys = get_keys(playback.pssh, playback.license_url)
    resolution = highest_resolution(playback.streams)
    filename = apply_quality_to_filename(format_filename(metadata, resolution), quality)
    return playback, keys, resolution, filename, build_download_command(playback, filename, keys, interactive, quality, include_subtitles)


def run_with_spinner(callback, quiet=False):
    spinner = Spinner()
    spinner.start()
    try:
        with contextlib.redirect_stdout(io.StringIO()) if quiet else contextlib.nullcontext():
            result = callback()
    finally:
        spinner.stop()
    return result


def print_streams(streams):
    print(f"\n{bcolors.YELLOW}Available streams:{bcolors.ENDC}")
    headings = ("#", "Type", "Resolution", "Bitrate", "Codec", "Lang", "Channels")
    type_order = {"Vid": 0, "Aud": 1, "Sub": 2}
    ordered_streams = sorted(streams, key=lambda item: type_order.get(item["type"], 99))
    rows = [(str(index), item["type"], item["resolution"], item["bitrate"], item["codec"], item["lang"], item["channels"]) for index, item in enumerate(ordered_streams, 1)]
    if not rows:
        print("No video, audio, or subtitle streams were found in the manifest.")
        return
    widths = [min(max(len(headings[i]), *(len(row[i]) for row in rows)), 52) for i in range(len(headings))]
    widths[0] = 3
    print("  ".join(f"{name:<{widths[i]}}" for i, name in enumerate(headings)))
    print("  ".join("-" * width for width in widths))
    for row in rows:
        print("  ".join(f"{value[:widths[i]]:<{widths[i]}}" for i, value in enumerate(row)))


def print_episode_metadata(metadata):
    print(f"\n{bcolors.YELLOW}Episode metadata:{bcolors.ENDC}")
    print(f"{bcolors.LIGHTBLUE}Show: {bcolors.ENDC}{metadata.title}")
    print(f"{bcolors.LIGHTBLUE}Title: {bcolors.ENDC}{metadata.episode_title}")
    print(f"{bcolors.LIGHTBLUE}Date Aired: {bcolors.ENDC}{metadata.aired_date}")
    print(f"{bcolors.LIGHTBLUE}Description: {bcolors.ENDC}{metadata.description}")


def print_info_mode(video_url):
    playback, keys, resolution, filename, command = run_with_spinner(lambda: resolve_video(video_url), quiet=True)
    print(f"{bcolors.LIGHTBLUE}DASH Manifest URL: {bcolors.ENDC}{playback.manifest_url}")
    for key in keys:
        print(f"{bcolors.OKGREEN}KEYS: {bcolors.ENDC}--key {key}")
    print_streams(playback.streams)
    print_episode_metadata(playback.metadata)
    print(f"\n{bcolors.YELLOW}Suggested filename: {bcolors.ENDC}{filename}.mkv")


def program_id_from_series(series_url):
    page = fetch_text(series_url)
    match = re.search(r'<meta[^>]+name=["\']DC.identifier["\'][^>]+content=["\']([^"\']+)', page, re.IGNORECASE)
    if not match:
        match = re.search(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+name=["\']DC.identifier["\']', page, re.IGNORECASE)
    if not match:
        raise ValueError("Could not find the RTVE programme ID on this series page.")
    return match.group(1)


def collect_episode_items(series_url):
    program_id = program_id_from_series(canonical_url(series_url))
    page = 1
    page_size = 500
    total_pages = None
    items = []
    seen_video_ids = set()
    while True:
        payload = fetch_json(f"{API_URL}/programas/{program_id}/videos", params={"type": COMPLETE_VIDEO_TYPE, "page": page, "size": page_size})
        page_data = payload.get("page") or {}
        page_items = page_data.get("items") or payload.get("items") or []
        total_pages = int_value(page_data.get("totalPages")) or total_pages
        if not page_items:
            break
        new_items = 0
        for raw in page_items:
            video_id = str(raw.get("id") or "")
            url = clean_text(raw.get("htmlUrl") or raw.get("url"))
            if not video_id or not url or video_id in seen_video_ids:
                continue
            seen_video_ids.add(video_id)
            new_items += 1
            metadata = metadata_from_item(raw, video_id, translate_description=False)
            items.append({"id": video_id, "url": canonical_url(url), "show_title": metadata.title, "season": metadata.season or 1, "episode": metadata.episode, "title": metadata.episode_title})
        # Older RTVE programme endpoints may ignore page and repeat page one.
        if len(page_items) < page_size or not new_items or (total_pages is not None and page >= total_pages):
            break
        page += 1
    if not items:
        raise ValueError("No RTVE episodes found for this series URL.")
    return sorted(items, key=lambda item: (item["season"] if item["season"] is not None else 9999, item["episode"] if item["episode"] is not None else 9999, item["id"]))


def group_episode_items(items):
    grouped = {}
    for item in items:
        label = f"Series {item['season']}" if item["season"] is not None else "Episodes"
        grouped.setdefault(label, []).append(item)
    return grouped


def print_series_rule(service_label, series_title):
    width = shutil.get_terminal_size((88, 20)).columns
    title = f" {service_label}: {series_title} "
    rule = max(width, len(title) + 4)
    left = (rule - len(title)) // 2
    print(f"{bcolors.LIGHTBLUE}{'─' * left}{bcolors.ENDC} {bcolors.LIGHTBLUE}{service_label}: {bcolors.ENDC}{bcolors.WHITE}{series_title}{bcolors.ENDC} {bcolors.LIGHTBLUE}{'─' * (rule - len(title) - left)}{bcolors.ENDC}")


def list_episode_items(items):
    grouped = group_episode_items(items)
    labels = sorted(grouped, key=lambda label: int(re.search(r"\d+", label).group(0)) if re.search(r"\d+", label) else 0)
    print(f"{icons.ICON_SUCCESS} {bcolors.OKGREEN}Found {len(items)} RTVE episodes{bcolors.ENDC}")
    print()
    print_series_rule("RTVE Series", items[0]["show_title"])
    print()
    summary = ",  ".join(f"{label}({len(grouped[label])})" for label in labels)
    print(f"{bcolors.GRAY}{len(labels)} Series,  {summary}{bcolors.ENDC}")
    for group_index, label in enumerate(labels):
        group_items = grouped[label]
        group_last = group_index == len(labels) - 1
        prefix = "   " if group_last else "│  "
        print(f"{bcolors.GRAY}{'└─' if group_last else '├─'} {label}: {bcolors.ENDC}{len(group_items)} episodes")
        for index, item in enumerate(group_items):
            last = index == len(group_items) - 1
            number = item["episode"] if item["episode"] is not None else "-"
            print(f"{bcolors.GRAY}{prefix}{'└─' if last else '├─'} {number}. {bcolors.ENDC}{item['title']}")
            print(f"{bcolors.GRAY}{prefix}{'  ' if last else '│ '} {bcolors.ENDC}{bcolors.LIGHTBLUE}{item['url']}{bcolors.ENDC}")
        if not group_last:
            print(f"{bcolors.GRAY}│{bcolors.ENDC}")


def parse_selector_part(part):
    match = re.fullmatch(r"s(?P<season>\d{2}|\d{4})(?:e(?P<episode>\d{2,3}))?", str(part).lower())
    if not match:
        raise ValueError("Download selector must be sXXeXX, sXXXXeXX, sXX, sXXXX, or a matching range.")
    return {"season": int(match.group("season")), "episode": int_value(match.group("episode"))}


def parse_download_selector(selector):
    parts = str(selector or "").lower().split("-", 1)
    start = parse_selector_part(parts[0])
    end = parse_selector_part(parts[-1])
    if len(parts) == 2 and bool(start["episode"]) != bool(end["episode"]):
        raise ValueError("Download range must use two episode selectors or two season selectors.")
    if (start["season"], start["episode"] or 0) > (end["season"], end["episode"] or 0):
        raise ValueError("Download range start must be before the end selector.")
    return start, end


def select_episode_items(series_url, selector):
    start, end = parse_download_selector(selector)
    selected = []
    for item in collect_episode_items(series_url):
        season, episode = item["season"], item["episode"]
        if season is None or (start["episode"] is not None and episode is None):
            continue
        if start["episode"] is None:
            keep = start["season"] <= season <= end["season"]
        else:
            keep = (start["season"], start["episode"]) <= (season, episode) <= (end["season"], end["episode"])
        if keep:
            selected.append(item)
    if not selected:
        raise ValueError("No RTVE episodes match that selector.")
    return selected


def parse_vtt(vtt_text):
    cues = []
    for block in re.split(r"\n{2,}", vtt_text.replace("\r", "").strip()):
        lines = [line.strip() for line in block.split("\n") if line.strip()]
        time_index = next((i for i, line in enumerate(lines) if "-->" in line), None)
        if time_index is None:
            continue
        start, _, end = lines[time_index].partition("-->")
        text = clean_text(re.sub(r"</?[^>]+>", "", " ".join(lines[time_index + 1:])))
        if text:
            cues.append((start.strip().replace(".", ","), end.strip().split(" ", 1)[0].replace(".", ","), text))
    return cues


def translate_texts_batch(texts):
    texts = [clean_text(text) for text in texts]
    if not texts:
        return []
    if len(texts) == 1:
        return [translate_text(texts[0])]
    translated = translate_text(f" {TRANSLATE_BATCH_MARKER} ".join(texts))
    parts = [clean_text(part) for part in translated.split(TRANSLATE_BATCH_MARKER)]
    if len(parts) == len(texts):
        return parts
    midpoint = len(texts) // 2
    return translate_texts_batch(texts[:midpoint]) + translate_texts_batch(texts[midpoint:])


def cue_batches(cues):
    batch = []
    chars = 0
    for cue in cues:
        text = clean_text(cue[2])
        projected = chars + len(text) + len(TRANSLATE_BATCH_MARKER) + 2
        if batch and (len(batch) >= TRANSLATE_BATCH_SIZE or projected > TRANSLATE_BATCH_CHAR_LIMIT):
            yield batch
            batch, chars = [], 0
        batch.append(cue)
        chars += len(text) + len(TRANSLATE_BATCH_MARKER) + 2
    if batch:
        yield batch


def progress_bar(done, total, width=30):
    total = max(total, 1)
    filled = int(width * done / total)
    return "[" + "#" * filled + "-" * (width - filled) + f"] {done}/{total}"


def translate_cues(cues):
    translated = []
    batches = list(cue_batches(cues))
    for batch_index, batch in enumerate(batches, 1):
        print(f"\r{bcolors.LIGHTBLUE}{progress_bar(batch_index - 1, len(batches))}{bcolors.ENDC}", end="", flush=True)
        try:
            translated_texts = translate_texts_batch([cue[2] for cue in batch])
        except Exception as exc:
            print()
            print(f"{icons.ICON_WARNING} {bcolors.WARNING}Subtitle batch translation failed: {exc}; keeping Spanish text for this batch.{bcolors.ENDC}")
            translated_texts = [cue[2] for cue in batch]
        translated.extend((start, end, text) for (start, end, _), text in zip(batch, translated_texts))
        print(f"\r{bcolors.LIGHTBLUE}{progress_bar(batch_index, len(batches))}{bcolors.ENDC}", end="", flush=True)
    print()
    return translated


def write_srt(cues, output_path):
    lines = []
    for index, (start, end, text) in enumerate(cues, 1):
        lines.extend([str(index), f"{start} --> {end}", text, ""])
    output_path.write_text("\n".join(lines), encoding="utf-8")


def find_created_subtitle_file(temp_name, existing_paths):
    candidates = []
    for pattern in (f"{temp_name}*.srt", f"{temp_name}*.vtt", f"{temp_name}*.ass"):
        candidates.extend(SAVE_PATH.glob(pattern))
    candidates = [path for path in candidates if path not in existing_paths and path.is_file()]
    return max(candidates, key=lambda path: path.stat().st_mtime) if candidates else None


def extract_dash_subtitles(playback, filename):
    temp_name = f"{filename}.es.{int(time.time())}"
    existing_paths = set(SAVE_PATH.glob(f"{temp_name}*"))
    command = f'{N_M3U8DL} "{playback.manifest_url}" --sub-only --save-dir "{SAVE_PATH}" --save-name "{temp_name}"'
    if SERVICE_PROXY:
        command += f' --custom-proxy "{SERVICE_PROXY}"'
    print(f"{icons.ICON_WAITING} {bcolors.LIGHTBLUE}Extracting Spanish DASH subtitles with N_m3u8DL-RE...{bcolors.ENDC}", flush=True)
    result = subprocess.run(command, shell=True)
    if result.returncode != 0:
        print(f"{icons.ICON_WARNING} {bcolors.WARNING}Subtitle extraction exited with code {result.returncode}.{bcolors.ENDC}")
        return None
    return find_created_subtitle_file(temp_name, existing_paths)


def save_translated_subtitles(playback, filename):
    subtitle = next((item for item in playback.subtitles if item.get("lang", "").lower().startswith("es")), None)
    if not subtitle:
        print(f"{icons.ICON_WARNING} {bcolors.WARNING}No RTVE Spanish subtitle track is available for English translation.{bcolors.ENDC}")
        return None
    subtitle_path = None
    if subtitle.get("url", "").lower().split("?", 1)[0].endswith((".vtt", ".webvtt")):
        response = session.get(subtitle["url"], headers=DEFAULT_HEADERS, timeout=30)
        response.raise_for_status()
        cues = parse_vtt(response.text)
    else:
        subtitle_path = extract_dash_subtitles(playback, filename)
        if not subtitle_path:
            print(f"{icons.ICON_WARNING} {bcolors.WARNING}No RTVE subtitle file was created for translation.{bcolors.ENDC}")
            return None
        if subtitle_path.suffix.lower() not in {".vtt", ".srt"}:
            print(f"{icons.ICON_WARNING} {bcolors.WARNING}Unsupported RTVE subtitle file format: {subtitle_path}{bcolors.ENDC}")
            return None
        cues = parse_vtt(subtitle_path.read_text(encoding="utf-8-sig", errors="replace"))
    if not cues:
        print(f"{icons.ICON_WARNING} {bcolors.WARNING}No Spanish subtitle cues were found for English translation.{bcolors.ENDC}")
        return None
    output = SAVE_PATH / f"{filename}.en.srt"
    print(f"{icons.ICON_WAITING} {bcolors.LIGHTBLUE}Translating Spanish subtitles to English SRT...{bcolors.ENDC}")
    write_srt(translate_cues(cues), output)
    print(f"{icons.ICON_SUCCESS} {bcolors.OKGREEN}English subtitles saved: {bcolors.ENDC}{output}")
    if subtitle_path:
        subtitle_path.unlink(missing_ok=True)
    return output


def process_video(video_url, auto_download=False, interactive=False, quality=None, save_native_subs=False):
    print(f"{icons.ICON_INFO} {bcolors.LIGHTBLUE}Processing: {bcolors.ENDC}{canonical_url(video_url)}")
    translate_subtitles = auto_download or confirm_download("Do you wish to save translated English subtitles? Y or N: ", auto_confirm=False)
    playback, keys, resolution, filename, command = run_with_spinner(lambda: resolve_video(video_url, interactive, quality, save_native_subs))
    metadata = playback.metadata
    print(f"{icons.ICON_SUCCESS} {bcolors.OKGREEN}{metadata.title}{bcolors.ENDC} {metadata.episode_title}")
    print(f"{bcolors.LIGHTBLUE}MPD URL: {bcolors.ENDC}{playback.manifest_url}")
    print(f"{bcolors.YELLOW}DOWNLOAD COMMAND: {bcolors.ENDC}")
    print(mask_proxy_command(command))
    if translate_subtitles:
        save_translated_subtitles(playback, filename)
    if confirm_download("Do you wish to download? Y or N: ", auto_confirm=auto_download):
        subprocess.run(command, shell=True)
    else:
        print(f"{icons.ICON_FAILURE} {bcolors.RED}Download cancelled{bcolors.ENDC}")


def download_selected_episodes(series_url, selector, quality, auto_confirm, save_native_subs):
    items = select_episode_items(series_url, selector)
    print()
    print(f"{bcolors.LIGHTBLUE}Download queue:{bcolors.ENDC}")
    for item in items:
        label = f"S{item['season']:02}E{item['episode']:02}" if item["episode"] is not None else f"S{item['season']:02}"
        print(f"{label} {item['title']}")
    if not confirm_download(f"Do you wish to download {'this' if len(items) == 1 else 'these'} {len(items)} {'episode' if len(items) == 1 else 'episodes'}? Y or N: ", auto_confirm=auto_confirm):
        print(f"{icons.ICON_FAILURE} {bcolors.RED}Download cancelled{bcolors.ENDC}")
        return
    for item in items:
        process_video(item["url"], auto_download=True, quality=quality, save_native_subs=save_native_subs)


def main(video_url, downloads_path, wvd_device_path, mode="auto", export_list=False, download_selector=None, quality=None, auto_confirm=False, save_native_subs=False):
    configure_service(downloads_path, wvd_device_path)
    video_url = canonical_url(video_url)
    if mode == "list":
        if is_episode_url(video_url):
            raise ValueError("List mode requires an RTVE series URL, not an episode URL.")
        print(f"{icons.ICON_WAITING} {bcolors.LIGHTBLUE}Retrieving series information.....{bcolors.ENDC}")
        list_episode_items(collect_episode_items(video_url))
        return
    if mode == "download":
        if is_episode_url(video_url):
            raise ValueError("Download selector mode requires an RTVE series URL, not an episode URL.")
        print(f"{icons.ICON_WAITING} {bcolors.LIGHTBLUE}Retrieving series information.....{bcolors.ENDC}")
        download_selected_episodes(video_url, download_selector, quality, auto_confirm, save_native_subs)
        return
    if mode == "info":
        if not is_episode_url(video_url):
            raise ValueError("Info mode requires an RTVE episode/video URL.")
        print_info_mode(video_url)
        return
    if not is_episode_url(video_url):
        raise ValueError("Series URLs require --list/-l or --download/-d SELECTOR.")
    process_video(video_url, auto_confirm, mode == "interactive", quality, save_native_subs)
