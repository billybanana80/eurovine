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
from urllib.parse import parse_qs, urlencode, urljoin, urlparse, urlunparse

import requests
import urllib3
from beaupy.spinners import Spinner

import icons
from colors import bcolors
from quality_utils import apply_quality_to_filename, video_selector
from services.proxy import current_proxy_url, mask_proxy_command


for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

SERVICE_NAME = "nrk"
BASE_URL = "https://tv.nrk.no"
PSAPI_BASE_URL = "https://psapi.nrk.no"
TRANSLATE_URL = "https://translate.googleapis.com/translate_a/single"
TRANSLATE_BATCH_MARKER = "@@NRKBREAK@@"
TRANSLATE_BATCH_SIZE = 40
TRANSLATE_BATCH_CHAR_LIMIT = 4500
N_M3U8DL = "N_m3u8DL-RE"
SAVE_PATH = None
SERVICE_PROXY = None


session = requests.Session()


def configure_service(downloads_path, _wvd_device_path=None):
    global SAVE_PATH, SERVICE_PROXY
    SAVE_PATH = Path(downloads_path)
    SERVICE_PROXY = current_proxy_url()
    session.proxies.clear()
    session.verify = True
    if SERVICE_PROXY:
        session.proxies.update({
            "http": SERVICE_PROXY,
            "https": SERVICE_PROXY,
        })
        session.verify = False


DEFAULT_HEADERS = {
    "Accept": "text/html,application/json,text/plain,*/*",
    "Accept-Language": "nb-NO,nb;q=0.9,no;q=0.8,en-US;q=0.7,en;q=0.6",
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


@dataclass
class PlaybackInfo:
    manifest_url: str
    manifest_type: str = "m3u8"
    api_hls_url: Optional[str] = None
    playback_json: dict = field(default_factory=dict)
    subtitles: list = field(default_factory=list)
    metadata: Metadata = field(default_factory=Metadata)
    streams: list = field(default_factory=list)
    warnings: list = field(default_factory=list)


def clean_text(value):
    return re.sub(r"\s+", " ", html.unescape(str(value or ""))).strip()


def strip_marker(value):
    return re.sub(r"^\[(?:Date|URL)\]", "", clean_text(value))


def date_value(value):
    value = strip_marker(value)
    if not value:
        return "Unknown"

    normalised = value.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(normalised).strftime("%Y-%m-%d")
    except ValueError:
        return value.split("T", 1)[0]


def description_text(item):
    titles = item.get("titles") or {}
    return clean_text(titles.get("subtitle") or item.get("description") or item.get("seriesDescription")) or "No Description"


def aired_date(item, manifest=None):
    value = item.get("releaseDateOnDemand")
    if not value:
        rights = item.get("usageRights") or {}
        value = ((rights.get("from") or {}).get("date"))
    if not value:
        value = ((((manifest or {}).get("availability") or {}).get("onDemand") or {}).get("from"))
    return date_value(value)


def canonical_url(value):
    value = clean_text(value)
    if re.match(r"^https?://", value, re.I):
        return value
    if value.startswith("/"):
        return urljoin(BASE_URL, value)
    return urljoin(BASE_URL, f"/{value.strip('/')}")


def api_url(path):
    if path.startswith("http"):
        return path
    return urljoin(PSAPI_BASE_URL, path)


def fetch_text(url, headers=None, attempts=4):
    request_headers = dict(DEFAULT_HEADERS)
    if headers:
        request_headers.update(headers)

    last_error = None
    for attempt in range(1, attempts + 1):
        try:
            response = session.get(url, headers=request_headers, timeout=35)
            if 400 <= response.status_code < 500:
                raise RuntimeError(f"NRK request failed with HTTP {response.status_code}: {response.text[:300]}")
            response.raise_for_status()
            response.encoding = response.encoding or "utf-8"
            return response.text
        except (requests.RequestException, RuntimeError) as exc:
            last_error = exc
        if attempt < attempts:
            time.sleep(0.75 * attempt)

    raise last_error


def fetch_json(url, headers=None):
    return json.loads(fetch_text(url, headers=headers or {"Accept": "application/json,text/plain,*/*"}))


def extract_video_id(video_url):
    parsed = urlparse(canonical_url(video_url))
    query_id = clean_text((parse_qs(parsed.query).get("v") or [""])[0])
    if query_id:
        return query_id.upper()

    match = re.search(r"/(?:episode|program)/([A-Z0-9]+)(?:[/?#]|$)", parsed.path, re.I)
    if match:
        return match.group(1).upper()
    raise ValueError("Could not extract NRK program ID from URL.")


def is_episode_url(video_url):
    try:
        return bool(extract_video_id(video_url))
    except ValueError:
        return False


def is_series_url(video_url):
    path = urlparse(canonical_url(video_url)).path
    return bool(re.search(r"/serie/[^/?#]+/?$", path, re.I))


def series_id_from_url(video_url):
    match = re.search(r"/serie/([^/?#]+)", urlparse(canonical_url(video_url)).path, re.I)
    return clean_text(match.group(1)) if match else ""


def fetch_series_catalog(series_id):
    return fetch_json(api_url(f"/tv/catalog/series/{series_id}"))


def fetch_program(program_id):
    return fetch_json(api_url(f"/programs/{program_id}"))


def fetch_manifest(program_id):
    return fetch_json(api_url(f"/playback/manifest/program/{program_id}"))


def video_id(item):
    return clean_text(item.get("prfId") or item.get("id")).upper()


def season_number(item):
    value = clean_text(item.get("_season_number") or item.get("seasonNumber"))
    if value:
        return int(re.search(r"\d+", value).group(0)) if re.search(r"\d+", value) else None

    season = item.get("season") or {}
    value = clean_text(season.get("id") or season.get("sequenceNumber") or season.get("title"))
    match = re.search(r"\d+", value)
    return int(match.group(0)) if match else None


def episode_number(item):
    for key in ("sequenceNumber", "episodeNumber"):
        value = clean_text(item.get(key))
        if value and re.search(r"\d+", value):
            return int(re.search(r"\d+", value).group(0))

    title = clean_text((item.get("titles") or {}).get("title") or item.get("episodeTitle"))
    match = re.match(r"(\d+)\.", title)
    return int(match.group(1)) if match else None


def episode_title(item):
    titles = item.get("titles") or {}
    title = clean_text(titles.get("title") or item.get("episodeTitle") or item.get("title"))
    match = re.match(r"\d+\.\s*(.+)", title)
    return clean_text(match.group(1)) if match else title or None


def show_title(catalog, item=None):
    titles = ((catalog.get("sequential") or {}).get("titles") or {})
    return (
        clean_text(titles.get("title"))
        or clean_text((item or {}).get("seriesTitle"))
        or clean_text((item or {}).get("originalTitle"))
        or "Unknown"
    )


def collect_episodes(catalog, wanted_id=None):
    episodes = []
    seasons = (catalog.get("_embedded") or {}).get("seasons") or []
    for season in seasons:
        season_value = clean_text(season.get("sequenceNumber"))
        if not season_value:
            season_value = clean_text(((season.get("titles") or {}).get("title")))
            match = re.search(r"\d+", season_value)
            season_value = match.group(0) if match else "1"

        for item in ((season.get("_embedded") or {}).get("episodes") or []):
            item = dict(item)
            item["_season_number"] = season_value
            if wanted_id and video_id(item) != wanted_id:
                continue
            episodes.append(item)
    return episodes


def hydrate_program_episode(program):
    return {
        "id": program.get("id"),
        "prfId": program.get("id"),
        "titles": {
            "title": program.get("episodeTitle") or program.get("title"),
            "subtitle": program.get("description") or program.get("seriesDescription"),
        },
        "originalTitle": program.get("seriesTitle") or program.get("originalTitle"),
        "sequenceNumber": program.get("episodeNumber"),
        "seasonNumber": program.get("seasonNumber"),
        "_season_number": program.get("seasonNumber") or "1",
    }


def load_catalog_for_url(source_url):
    series_id = series_id_from_url(source_url)
    if series_id:
        return fetch_series_catalog(series_id)

    program_id = extract_video_id(source_url)
    program = fetch_program(program_id)
    series_id = clean_text(program.get("seriesId"))
    if series_id:
        try:
            return fetch_series_catalog(series_id)
        except Exception:
            pass

    return {
        "sequential": {
            "urlFriendlySeriesId": series_id,
            "titles": {"title": program.get("seriesTitle") or program.get("originalTitle")},
        },
        "_embedded": {
            "seasons": [
                {
                    "sequenceNumber": program.get("seasonNumber") or "1",
                    "_embedded": {"episodes": [hydrate_program_episode(program)]},
                }
            ]
        },
    }


def search_metadata(video_url, video_id):
    source_url = canonical_url(video_url)
    catalog = load_catalog_for_url(source_url)
    episodes = collect_episodes(catalog, wanted_id=video_id)
    item = episodes[0] if episodes else hydrate_program_episode(fetch_program(video_id))
    description = description_text(item)
    if description != "No Description":
        try:
            description = translate_text(description)
        except Exception:
            pass
    return Metadata(
        title=show_title(catalog, item),
        season=season_number(item),
        episode=episode_number(item),
        episode_title=episode_title(item),
        aired_date=aired_date(item),
        description=description,
        video_id=video_id,
        video_url=source_url,
    )


def item_video_url(item, series_id):
    path = clean_text(item.get("playerPath"))
    if path:
        return canonical_url(path)

    season = season_number(item) or 1
    program_id = video_id(item)
    if series_id and program_id:
        return f"{BASE_URL}/serie/{series_id}/sesong/{season}/episode/{program_id}"

    share = (((item.get("_links") or {}).get("share") or {}).get("href"))
    return clean_text(share).replace("{&autoplay,t}", "").replace("{&autoplay}", "")


def episode_sort_key(item):
    return (
        int(season_number(item) or 9999),
        int(episode_number(item) or 9999),
        clean_text(episode_title(item)).lower(),
        video_id(item),
    )


def collect_episode_items(series_url):
    source_url = canonical_url(series_url)
    catalog = load_catalog_for_url(source_url)
    series_id = clean_text((catalog.get("sequential") or {}).get("urlFriendlySeriesId")) or series_id_from_url(source_url)
    items = sorted(collect_episodes(catalog), key=episode_sort_key)
    if not items:
        raise RuntimeError("No NRK episodes found for this URL.")

    return [
        {
            "show_title": show_title(catalog, item),
            "season": int(season_number(item) or 1),
            "episode": int(episode_number(item) or 1),
            "title": episode_title(item) or f"Episode {episode_number(item) or 1}",
            "url": item_video_url(item, series_id),
        }
        for item in items
        if item_video_url(item, series_id)
    ]


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
        print(f"{icons.ICON_FAILURE} {bcolors.FAIL}No NRK episodes found.{bcolors.ENDC}")
        return
    show = episode_items[0].get("show_title", "NRK")
    grouped_items = group_episode_items_by_series(episode_items)
    group_labels = sorted(grouped_items, key=series_group_sort_key)
    series_summary = ",  ".join(f"{label}({len(grouped_items[label])})" for label in group_labels)
    print(f"{icons.ICON_SUCCESS} {bcolors.OKGREEN}Found {len(episode_items)} NRK episodes{bcolors.ENDC}")
    print()
    print_series_rule("NRK Series", show)
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
        raise ValueError(f"No NRK episodes matched selector {format_download_selector(parsed)}.")
    return selected


def print_download_queue(episode_items):
    print()
    print(f"{bcolors.GRAY}Download queue:{bcolors.ENDC}")
    for item in episode_items:
        print(f"{bcolors.GRAY}{format_queue_selector(item)} {item.get('title') or ''}{bcolors.ENDC}".rstrip())


def manifest_asset_url(manifest, wanted_format):
    assets = (((manifest or {}).get("playable") or {}).get("assets") or [])
    for asset in assets:
        if clean_text(asset.get("format")).upper() == wanted_format.upper():
            return clean_text(asset.get("url"))
    return ""


def subtitles_from_playback_manifest(manifest):
    subtitles = (((manifest or {}).get("playable") or {}).get("subtitles") or [])
    candidates = []
    for subtitle in subtitles:
        if not isinstance(subtitle, dict):
            continue
        url = clean_text(subtitle.get("webVtt") or subtitle.get("url"))
        if not url:
            continue
        candidates.append(
            {
                "url": url,
                "label": clean_text(subtitle.get("label")),
                "language": clean_text(subtitle.get("language")),
                "type": clean_text(subtitle.get("type")),
            }
        )
    return candidates


def promote_to_cmaf_large(hls_url):
    parsed = urlparse(hls_url)
    if not parsed.scheme or not parsed.netloc:
        return hls_url
    path = re.sub(r"/[^/]*\.m3u8$", "/cmaf.m3u8", parsed.path)
    return urlunparse((parsed.scheme, parsed.netloc, path, "", "adap=large", ""))


def manifest_looks_playable(manifest_url):
    try:
        text = fetch_text(manifest_url, headers={"Accept": "application/vnd.apple.mpegurl,*/*"}, attempts=2)
    except Exception:
        return False
    return "#EXTM3U" in text[:200] and ("#EXT-X-STREAM-INF" in text or "#EXTINF" in text)


def get_playback_info(video_url, metadata):
    manifest = fetch_manifest(metadata.video_id)
    hls_url = manifest_asset_url(manifest, "HLS")
    if not hls_url:
        raise RuntimeError("No NRK HLS asset URL found in playback manifest.")

    cmaf_url = promote_to_cmaf_large(hls_url)
    manifest_url = cmaf_url if manifest_looks_playable(cmaf_url) else hls_url
    warnings = []
    if manifest_url == hls_url and hls_url != cmaf_url:
        warnings.append("CMAF large manifest was unavailable; using NRK API HLS URL.")
    if metadata.aired_date == "Unknown":
        metadata.aired_date = aired_date({}, manifest)

    return PlaybackInfo(
        manifest_url=manifest_url,
        manifest_type="hls" if ".m3u8" in manifest_url.lower() else "dash",
        api_hls_url=hls_url,
        playback_json=manifest,
        subtitles=subtitles_from_playback_manifest(manifest),
        metadata=metadata,
        warnings=warnings,
    )


def get_hls_resolution(m3u8_url):
    text = fetch_text(m3u8_url, headers={"Accept": "application/vnd.apple.mpegurl,*/*"})
    resolutions = re.findall(r"RESOLUTION=\d+x(\d+)", text)
    if resolutions:
        return f"{max(int(height) for height in resolutions)}p"

    heights = re.findall(r"_(\d{3,4})p(?:\d+)?(?:_|-|/)", text)
    return f"{max(int(height) for height in heights)}p" if heights else "Unknown"


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
        if not lines or lines[0].upper().startswith(("WEBVTT", "NOTE", "STYLE", "REGION")):
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
        params={"client": "gtx", "sl": "no", "tl": "en", "dt": "t", "q": text},
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


def subtitle_url(subtitle):
    if isinstance(subtitle, str):
        return subtitle
    if not isinstance(subtitle, dict):
        return ""
    return clean_text(subtitle.get("url") or subtitle.get("webVtt") or subtitle.get("href"))


def subtitle_preference_score(subtitle):
    text = json.dumps(subtitle, ensure_ascii=False).lower() if isinstance(subtitle, dict) else clean_text(subtitle).lower()
    score = 0
    if "norsk" in text or "nor" in text or '"language": "no"' in text or '"language": "nb"' in text:
        score += 100
    if "transcribes-spoken-dialog" in text:
        score += 160
    if "default=yes" in text or '"default": "yes"' in text:
        score += 60
    if 'name="norsk"' in text or '"name": "norsk"' in text or '"label": "norsk"' in text:
        score += 50
    if "kun ved annet" in text or "only when" in text or "forced" in text:
        score -= 180
    if "describes-music-and-sound" in text or "lydbeskrivelser" in text:
        score -= 120
    if "sdh" in text:
        score -= 60
    if "non-sdh" in text:
        score -= 30
    if "translated" in text:
        score -= 20
    if ".vtt" in text or "webvtt" in text or ".m3u8" in text:
        score += 10
    return score


def parse_hls_attribute_list(line):
    _, _, payload = line.partition(":")
    attrs = {}
    for match in re.finditer(r'([A-Z0-9-]+)=("[^"]*"|[^,]*)', payload, re.I):
        value = match.group(2).strip()
        if value.startswith('"') and value.endswith('"'):
            value = value[1:-1]
        attrs[match.group(1).lower()] = html.unescape(value)
    return attrs


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
        label = ""
        if isinstance(subtitle, dict):
            lang = clean_text(subtitle.get("language")) or "-"
            label = clean_text(subtitle.get("label") or subtitle.get("name"))
        codec = urlparse(url).path.rsplit(".", 1)[-1].lower() if "." in urlparse(url).path else "-"
        key = (codec, lang, label)
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
                    "characteristics": clean_text(attrs.get("characteristics")),
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
        print(f"{bcolors.LIGHTBLUE}{icons.ICON_WAITING} Fetching {total} NRK subtitle segments...{bcolors.ENDC}")
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
        print(f"{bcolors.WARNING}{icons.ICON_WARNING} No external Norwegian subtitle URL found in NRK playback or manifest.{bcolors.ENDC}")
        return None

    url = subtitle_url(subtitle)
    text = fetch_subtitle_text(url)
    cues = parse_vtt(text)
    if not cues:
        print(f"{bcolors.WARNING}{icons.ICON_WARNING} No subtitle cues found in NRK subtitle response.{bcolors.ENDC}")
        return None

    output_path = SAVE_PATH / f"{filename}.en.srt"
    print(f"{bcolors.LIGHTBLUE}{icons.ICON_WAITING} Translating Norwegian subtitles to English SRT...{bcolors.ENDC}")
    write_srt(translate_cues(cues), output_path)
    print(f"{bcolors.OKGREEN}{icons.ICON_SUCCESS} English subtitles saved: {bcolors.ENDC}{output_path}")
    return output_path


def maybe_save_translated_subtitles(playback, filename):
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
    parts.extend([resolution, "NRK", "WEB-DL", "AAC2.0", "H.264"])
    return ".".join(part for part in parts if part and part != "Unknown")


def build_download_command(playback, filename, interactive=False, quality=None):
    selectors = "" if interactive else f"{video_selector(quality)} --select-audio best --drop-subtitle all "
    command = (
        f'{N_M3U8DL} "{playback.manifest_url}" '
        f'{selectors}'
        f'-mt -M format=mkv --save-dir "{SAVE_PATH}" --save-name "{filename}"'
    )
    if SERVICE_PROXY:
        command += f' --custom-proxy "{SERVICE_PROXY}"'
    return command


def print_playback_details(playback, resolution, command):
    print(f"{bcolors.LIGHTBLUE}CMAF M3U8 URL: {bcolors.ENDC}{playback.manifest_url}")
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


def resolve_video(video_url, interactive=False, quality=None):
    video_url = canonical_url(video_url)
    video_id = extract_video_id(video_url)
    metadata = search_metadata(video_url, video_id)
    playback = get_playback_info(video_url, metadata)
    manifest_text = fetch_text(playback.manifest_url, headers={"Accept": "application/vnd.apple.mpegurl,application/dash+xml,*/*"})
    streams, detected_manifest_type = parse_manifest_streams(manifest_text)
    playback.manifest_type = "hls" if detected_manifest_type == "HLS" else "dash"

    existing_subtitle_keys = {
        (stream.get("codec"), stream.get("lang"))
        for stream in streams
        if stream.get("type") == "Sub"
    }
    for subtitle_stream in subtitle_info_streams(playback.subtitles):
        key = (subtitle_stream.get("codec"), subtitle_stream.get("lang"))
        if key not in existing_subtitle_keys:
            streams.append(subtitle_stream)
            existing_subtitle_keys.add(key)
    playback.streams = sorted(streams, key=stream_table_sort_key)

    resolution = highest_stream_resolution(playback.streams)
    if resolution == "Unknown" and playback.manifest_type == "hls":
        resolution = get_hls_resolution(playback.manifest_url)
    filename = format_filename(metadata, resolution)
    filename = apply_quality_to_filename(filename, quality)
    command = build_download_command(playback, filename, interactive=interactive, quality=quality)
    return playback, resolution, filename, command


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
        raise ValueError("Info mode requires an NRK episode/video URL.")
    playback, _resolution, filename, _command = run_with_spinner(lambda: resolve_video(video_url))
    manifest_label = "DASH Manifest URL" if playback.manifest_type == "dash" else "HLS Manifest URL"
    print(f"{bcolors.LIGHTBLUE}{manifest_label}: {bcolors.ENDC}{playback.manifest_url}")
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
        subprocess.run(command, shell=True)
    else:
        print(f"{icons.ICON_FAILURE} {bcolors.RED}Download cancelled{bcolors.ENDC}")


def process_video(video_url, auto_download=False, interactive=False, quality=None):
    video_url = canonical_url(video_url)
    print(f"{icons.ICON_INFO} {bcolors.LIGHTBLUE}Processing: {bcolors.ENDC}{video_url}")
    playback, resolution, filename, command = run_with_spinner(lambda: resolve_video(video_url, interactive=interactive, quality=quality))
    metadata = playback.metadata
    episode_str = f"S{metadata.season:02d}E{metadata.episode:02d}" if metadata.season and metadata.episode else ""
    if metadata.title != "Unknown" or episode_str or metadata.episode_title:
        print(f"{icons.ICON_SUCCESS} {bcolors.OKGREEN}{metadata.title}{bcolors.ENDC} {episode_str} - {metadata.episode_title or ''}".rstrip())

    for warning in playback.warnings:
        print(f"{icons.ICON_WARNING} {bcolors.WARNING}{warning}{bcolors.ENDC}")
    print_playback_details(playback, resolution, command)
    if auto_download:
        save_translated_subtitles(playback, filename)
    else:
        maybe_save_translated_subtitles(playback, filename)
    maybe_download(command, auto_download=auto_download)


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
        print(f"{icons.ICON_FAILURE} {bcolors.FAIL}No NRK episodes found to export.{bcolors.ENDC}")
        return

    export_dir = Path(__file__).resolve().parents[2] / "export"
    export_dir.mkdir(parents=True, exist_ok=True)
    show_title = episode_items[0].get("show_title", "NRK")
    output_path = export_dir / f"nrk_{safe_name(show_title)}_episodes.txt"
    output_path.write_text("\n".join(item["url"] for item in episode_items) + "\n", encoding="utf-8")
    print(f"{icons.ICON_SUCCESS} {bcolors.OKGREEN}Exported {len(episode_items)} NRK episode URLs to {output_path}{bcolors.ENDC}")


def main(video_url, downloads_path, wvd_device_path, mode="auto", export_list=False, download_selector=None, quality=None):
    """Eurovine entry point for NRK DRM-free HLS."""
    try:
        if not video_url:
            raise ValueError("No NRK URL provided.")
        if not downloads_path:
            raise ValueError("Eurovine config requires downloads_path for NRK.")

        configure_service(downloads_path, wvd_device_path)
        video_url = video_url.strip()

        if mode == "list":
            if is_episode_url(video_url):
                print(f"{icons.ICON_FAILURE} {bcolors.FAIL}List mode requires an NRK series URL, not an episode URL.{bcolors.ENDC}")
                return
            print(f"{icons.ICON_WAITING} {bcolors.LIGHTBLUE}Retrieving series information.....{bcolors.ENDC}")
            episode_items = collect_episode_items(video_url)
            list_episode_items(episode_items)
            if export_list:
                export_episode_urls(episode_items)
            return

        if mode == "download":
            if is_episode_url(video_url):
                print(f"{icons.ICON_FAILURE} {bcolors.FAIL}Download selector mode requires an NRK series URL, not an episode URL.{bcolors.ENDC}")
                return
            print(f"{icons.ICON_WAITING} {bcolors.LIGHTBLUE}Retrieving series information.....{bcolors.ENDC}")
            download_selected_episodes(video_url, download_selector, quality)
            return

        if mode == "info":
            print_info_mode(video_url)
            return

        if export_list:
            if is_episode_url(video_url):
                print(f"{icons.ICON_FAILURE} {bcolors.FAIL}Export mode requires an NRK series URL, not an episode URL.{bcolors.ENDC}")
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
