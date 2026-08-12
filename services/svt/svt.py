import base64
import binascii
import html
import json
import re
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin, urlparse

import icons
import requests
import urllib3
from beaupy.spinners import Spinner
from colors import bcolors
from quality_utils import apply_quality_to_filename, video_selector
from services.proxy import current_proxy_url, mask_proxy_command


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

SERVICE_NAME = "svt"
BASE_URL = "https://www.svtplay.se"
VIDEO_API_URL = "https://api.svt.se/video/{video_id}"
TRANSLATE_URL = "https://translate.googleapis.com/translate_a/single"
TRANSLATE_BATCH_MARKER = "@@SVTBREAK@@"
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
    "Accept-Language": "sv-SE,sv;q=0.9,en-US;q=0.8,en;q=0.7",
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
    page_id: Optional[str] = None
    program_version_id: Optional[str] = None


@dataclass
class PlaybackInfo:
    manifest_url: str
    manifest_type: str
    license_url: Optional[str] = None
    key_id: Optional[str] = None
    is_encrypted: bool = False
    metadata: Metadata = field(default_factory=Metadata)
    subtitles: list = field(default_factory=list)
    subtitle_type: Optional[str] = None
    streams: list = field(default_factory=list)


def clean_text(value):
    return re.sub(r"\s+", " ", html.unescape(str(value or ""))).strip()


def date_value(value):
    value = clean_text(value)
    if not value:
        return "Unknown"

    normalised = value.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(normalised).strftime("%Y-%m-%d")
    except ValueError:
        pass

    match = re.search(r"(\d{1,2})\s+([a-zåäö]+)\s+(\d{4})", value, re.I)
    if match:
        months = {
            "jan": 1,
            "feb": 2,
            "mar": 3,
            "apr": 4,
            "maj": 5,
            "jun": 6,
            "jul": 7,
            "aug": 8,
            "sep": 9,
            "okt": 10,
            "nov": 11,
            "dec": 12,
        }
        month = months.get(match.group(2).lower()[:3])
        if month:
            return f"{int(match.group(3)):04d}-{month:02d}-{int(match.group(1)):02d}"

    return value.split("T", 1)[0]


def fetch_json(url, headers=None):
    request_headers = dict(DEFAULT_HEADERS)
    request_headers["Accept"] = "application/json,*/*"
    if headers:
        request_headers.update(headers)
    response = session.get(url, headers=request_headers, timeout=30)
    response.raise_for_status()
    return response.json()


def fetch_text(url, headers=None):
    request_headers = dict(DEFAULT_HEADERS)
    if headers:
        request_headers.update(headers)
    response = session.get(url, headers=request_headers, timeout=35)
    response.raise_for_status()
    response.encoding = response.encoding or "utf-8"
    return response.text


def canonical_url(value):
    value = clean_text(value)
    if re.match(r"^https?://", value, re.IGNORECASE):
        return value
    if value.startswith("/"):
        return urljoin(BASE_URL, value)
    return urljoin(BASE_URL, f"/{value.strip('/')}")


def extract_video_id(video_url):
    match = re.search(r"/(?:video|klipp)/([^/?#]+)", urlparse(video_url).path)
    if match:
        return match.group(1)
    raise ValueError("Could not extract SVT video ID from URL.")


def is_episode_url(video_url):
    return bool(re.search(r"/(?:video|klipp)/[^/?#]+", urlparse(canonical_url(video_url)).path))


def is_series_url(video_url):
    path = urlparse(canonical_url(video_url)).path
    return bool(path.strip("/")) and not is_episode_url(video_url)


def extract_int(value):
    match = re.search(r"(\d+)", clean_text(value))
    return int(match.group(1)) if match else None


def episode_number_from_title(title):
    match = re.match(r"\s*0*(\d+)[\.\s-]+", clean_text(title))
    if match:
        return int(match.group(1))
    match = re.search(r"(?:avsnitt|episode)\s*0*(\d+)", clean_text(title), re.I)
    return int(match.group(1)) if match else None


def parse_urql_data(html_text):
    match = re.search(r"window\.URQL_DATA\s*=\s*(\{.*?\});", html_text, re.S)
    if not match:
        raise RuntimeError("Could not find SVT page state in window.URQL_DATA.")

    try:
        outer = json.loads(match.group(1))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Could not parse SVT page state: {exc}") from exc

    payloads = []
    for entry in outer.values():
        data = entry.get("data") if isinstance(entry, dict) else None
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except json.JSONDecodeError:
                continue
        if isinstance(data, dict):
            payloads.append(data)
    return payloads


def details_page(payloads):
    for payload in payloads:
        page = payload.get("detailsPageByPath")
        if isinstance(page, dict):
            return page
    raise RuntimeError("Could not find SVT details page metadata.")


def page_title(page, source_url=None):
    analytics = page.get("analytics") or {}
    analytics_json = analytics.get("json") or {}
    for value in (analytics_json.get("programName"), analytics_json.get("title"), analytics_json.get("name")):
        title = clean_text(value)
        if title:
            return title

    item = page.get("item") or {}
    for value in (item.get("programTitle"), item.get("seriesTitle"), item.get("title")):
        title = clean_text(value)
        if title:
            return title

    for module in page.get("modules") or []:
        details = module.get("details") or {}
        title = clean_text(details.get("programTitle") or details.get("seriesTitle"))
        if title:
            return title

    if source_url:
        path_parts = [part for part in urlparse(canonical_url(source_url)).path.strip("/").split("/") if part]
        if len(path_parts) >= 3 and path_parts[0] in ("video", "klipp"):
            slug = path_parts[2]
        else:
            slug = path_parts[0] if path_parts else ""
        if slug and slug not in ("video", "klipp"):
            return " ".join(part.capitalize() for part in slug.split("-"))

    for module in page.get("modules") or []:
        selection = module.get("selection") or {}
        if selection.get("selectionType") == "season":
            continue
        details = module.get("details") or {}
        title = clean_text(details.get("heading"))
        if title:
            return title

    return "Unknown Show"


def season_number_from_selection(selection):
    for value in (selection.get("name"), selection.get("slug"), selection.get("id")):
        match = re.search(r"(\d+)", clean_text(value))
        if match:
            return int(match.group(1))
    return 1


def page_svt_id(item):
    nested = item.get("item") or {}
    return clean_text(nested.get("svtId") or (item.get("analytics") or {}).get("json", {}).get("svtId"))


def video_svt_id(item, video_payload=None):
    if isinstance(video_payload, dict):
        return clean_text(video_payload.get("svtId"))
    nested = item.get("item") or {}
    return clean_text(nested.get("videoSvtId") or page_svt_id(item))


def series_episode_number(item):
    heading = clean_text(item.get("heading"))
    match = re.search(r"(?:avsnitt|episode)\s*0*(\d+)", heading, re.I)
    if match:
        return int(match.group(1))

    match = re.match(r"\s*0*(\d+)[\.\s-]+", heading)
    if match:
        return int(match.group(1))

    description = clean_text(item.get("description"))
    match = re.search(r"\bDel\s+0*(\d+)\s+av\b", description, re.I)
    if match:
        return int(match.group(1))

    return int(item.get("_episode_index") or 1)


def series_episode_title(item):
    title = clean_text(item.get("heading"))
    title = re.sub(r"^\s*\d+\.\s*", "", title)
    return title or f"Avsnitt {series_episode_number(item)}"


def series_video_url(item):
    nested = item.get("item") or {}
    path = clean_text((nested.get("urls") or {}).get("svtplay"))
    return canonical_url(path) if path else ""


def collect_series_episode_items_from_page(page, wanted_id=None):
    episodes = []
    seen = set()

    for module in page.get("modules") or []:
        selection = module.get("selection") or {}
        if selection.get("selectionType") != "season":
            continue

        season = season_number_from_selection(selection)
        for index, teaser in enumerate(selection.get("items") or [], start=1):
            item = teaser.get("item") if isinstance(teaser, dict) else {}
            if not isinstance(item, dict) or item.get("__typename") != "Episode":
                continue

            item_id = clean_text((item.get("urls") or {}).get("svtplay"))
            page_id = page_svt_id(teaser)
            if wanted_id and wanted_id not in (page_id, item_id):
                continue
            key = page_id or item_id
            if not key or key in seen:
                continue

            seen.add(key)
            merged = dict(teaser)
            merged["_season"] = season
            merged["_episode_index"] = index
            episodes.append(merged)

    return episodes


def series_episode_sort_key(item):
    return (
        int(item.get("_season") or 9999),
        series_episode_number(item),
        series_episode_title(item).lower(),
        page_svt_id(item),
    )


def collect_episode_items(series_url):
    source_url = canonical_url(series_url)
    page = details_page(parse_urql_data(fetch_text(source_url)))
    show = page_title(page, source_url)
    episodes = sorted(collect_series_episode_items_from_page(page), key=series_episode_sort_key)
    if not episodes:
        raise RuntimeError("No SVT episodes found for this URL.")

    return [
        {
            "show_title": show,
            "season": int(item.get("_season") or 1),
            "episode": series_episode_number(item),
            "title": series_episode_title(item),
            "url": series_video_url(item),
            "page_id": page_svt_id(item),
        }
        for item in episodes
        if series_video_url(item)
    ]


def page_metadata(video_url, page_id):
    try:
        page = details_page(parse_urql_data(fetch_text(video_url)))
        show = page_title(page, video_url)
        matches = collect_series_episode_items_from_page(page, wanted_id=page_id)
        item = matches[0] if matches else None
        if not item:
            return {}
        return {
            "title": show,
            "season": int(item.get("_season") or 1),
            "episode": series_episode_number(item),
            "episode_title": series_episode_title(item),
            "description": clean_text(item.get("description")) or "No Description",
            "aired_date": date_value(((item.get("item") or {}).get("validFromFormatted"))),
        }
    except Exception:
        return {}


def search_metadata(video_url, video_id):
    payload = fetch_json(VIDEO_API_URL.format(video_id=video_id))
    title = clean_text(payload.get("programTitle")) or "Unknown"
    episode_title = clean_text(payload.get("episodeTitle")) or None
    episode = episode_number_from_title(episode_title)
    season = 1 if episode is not None else None
    page_meta = page_metadata(video_url, video_id)
    if page_meta:
        title = clean_text(page_meta.get("title")) or title
        season = page_meta.get("season") or season
        episode = page_meta.get("episode") or episode
        episode_title = clean_text(page_meta.get("episode_title")) or episode_title
    description = clean_text(page_meta.get("description")) if page_meta else ""
    if not description:
        description = clean_text(payload.get("description") or payload.get("shortDescription")) or "No Description"
    if description != "No Description":
        try:
            description = translate_text(description)
        except Exception:
            pass
    aired = date_value(((payload.get("rights") or {}).get("validFrom")) or (page_meta or {}).get("aired_date"))
    if episode_title and clean_text(episode_title).lower() == clean_text(title).lower() and episode is None:
        episode_title = None

    stats = payload.get("mmsStatistics") or {}
    program_version_id = clean_text(payload.get("programVersionId") or stats.get("mms_tid")) or None

    return Metadata(
        title=title,
        season=season,
        episode=episode,
        episode_title=episode_title,
        aired_date=aired,
        description=description,
        video_id=clean_text(payload.get("svtId")) or video_id,
        page_id=video_id,
        program_version_id=program_version_id,
    )


def stream_url(reference):
    return clean_text(reference.get("url") or reference.get("resolve") or reference.get("redirect"))


def select_reference(references, preferred_formats):
    by_format = {clean_text(item.get("format")): item for item in references if isinstance(item, dict)}
    for wanted in preferred_formats:
        item = by_format.get(wanted)
        if item and stream_url(item):
            return item
    for wanted in preferred_formats:
        for item in references:
            fmt = clean_text(item.get("format")) if isinstance(item, dict) else ""
            if wanted in fmt and stream_url(item):
                return item
    return None


def get_playback_info(video_url, metadata):
    payload = fetch_json(VIDEO_API_URL.format(video_id=metadata.page_id or metadata.video_id))
    metadata.video_id = clean_text(payload.get("svtId")) or metadata.video_id
    metadata.program_version_id = clean_text(payload.get("programVersionId")) or metadata.program_version_id

    references = payload.get("videoReferences") or []
    dash_ref = select_reference(references, ["dash-full", "dash-lb-full", "dash", "dash-avc"])
    hls_ref = select_reference(references, ["hls-cmaf-full", "hls-ts-full", "hls", "hls-ts-avc"])

    if dash_ref:
        return PlaybackInfo(
            manifest_url=stream_url(dash_ref),
            manifest_type="mpd",
            metadata=metadata,
            subtitles=payload.get("subtitleReferences") or [],
            subtitle_type=clean_text((payload.get("rights") or {}).get("subtitleType")) or None,
        )
    if hls_ref:
        return PlaybackInfo(
            manifest_url=stream_url(hls_ref),
            manifest_type="m3u8",
            metadata=metadata,
            subtitles=payload.get("subtitleReferences") or [],
            subtitle_type=clean_text((payload.get("rights") or {}).get("subtitleType")) or None,
        )

    raise ValueError("No SVT DASH or HLS manifest URL found.")


def b64url_to_hex(value):
    value = clean_text(value)
    padded = value + "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(padded).hex()


def hex_to_b64url(value):
    raw = bytes.fromhex(value.replace("-", ""))
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def parse_dash_clearkey_data(manifest_url):
    response = session.get(manifest_url, headers=DEFAULT_HEADERS, timeout=30)
    response.raise_for_status()
    root = ET.fromstring(response.content)
    ns_mpd = "{urn:mpeg:dash:schema:mpd:2011}"
    ns_cenc = "{urn:mpeg:cenc:2013}"
    laurl_tags = [
        "{https://dashif.org/guidelines/clearKey}Laurl",
        "{urn:mpeg:dash:schema:mpd:2011}Laurl",
    ]

    content_protections = root.findall(".//" + ns_mpd + "ContentProtection")
    if not content_protections:
        return None, None, False

    key_id = None
    license_url = None
    for content_protection in content_protections:
        if not key_id:
            key_id = clean_text(content_protection.attrib.get(ns_cenc + "default_KID"))
        for tag in laurl_tags:
            laurl = content_protection.find(tag)
            if laurl is not None and laurl.text:
                license_url = clean_text(laurl.text)
                break
        if key_id and license_url:
            return key_id.replace("-", ""), license_url, True

    raise ValueError("ClearKey KID or licence URL not found in DASH manifest.")


def get_clearkey_keys(key_id, license_url):
    kid_b64 = hex_to_b64url(key_id)
    headers = {
        "Accept": "*/*",
        "Content-Type": "application/json",
        "Origin": BASE_URL,
        "Referer": f"{BASE_URL}/",
        "User-Agent": DEFAULT_HEADERS["User-Agent"],
    }
    response = session.post(
        license_url,
        headers=headers,
        json={"kids": [kid_b64], "type": "temporary"},
        timeout=30,
    )
    try:
        response.raise_for_status()
    except requests.exceptions.HTTPError as exc:
        raise RuntimeError(f"SVT ClearKey licence request failed: {exc}. Response: {response.text[:300]}") from exc

    payload = response.json()
    keys = []
    for item in payload.get("keys") or []:
        kid = b64url_to_hex(item.get("kid"))
        key = b64url_to_hex(item.get("k"))
        keys.append(f"{kid}:{key}")
    return keys


def get_dash_resolution(mpd_url):
    response = session.get(mpd_url, headers=DEFAULT_HEADERS, timeout=30)
    response.raise_for_status()
    root = ET.fromstring(response.content)
    heights = [
        int(rep.get("height"))
        for rep in root.findall(".//{urn:mpeg:dash:schema:mpd:2011}Representation")
        if rep.get("height")
    ]
    return f"{max(heights)}p" if heights else "Unknown"


def get_hls_resolution(m3u8_url):
    response = session.get(m3u8_url, headers=DEFAULT_HEADERS, timeout=30)
    response.raise_for_status()
    resolutions = re.findall(r"RESOLUTION=\d+x(\d+)", response.text)
    if not resolutions:
        return "Unknown"
    return f"{max(int(height) for height in resolutions)}p"


def get_resolution(playback):
    if playback.manifest_type == "mpd":
        return get_dash_resolution(playback.manifest_url)
    if playback.manifest_type == "m3u8":
        return get_hls_resolution(playback.manifest_url)
    return "Unknown"


def fetch_manifest(manifest_url):
    try:
        response = session.get(manifest_url, headers=DEFAULT_HEADERS, timeout=30)
        response.raise_for_status()
        response.encoding = response.encoding or "utf-8"
        return response.text
    except requests.RequestException as exc:
        raise ConnectionError(f"Failed to fetch SVT manifest: {exc}") from exc


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


def parse_manifest_attributes(line):
    _, _, payload = line.partition(":")
    attrs = {}
    for match in re.finditer(r'([A-Z0-9-]+)=("[^"]*"|[^,]*)', payload, re.I):
        value = match.group(2).strip()
        if value.startswith('"') and value.endswith('"'):
            value = value[1:-1]
        attrs[match.group(1).lower()] = html.unescape(value)
    return attrs


def parse_hls_streams(manifest_text):
    streams = []
    pending_variant = None
    for raw_line in manifest_text.splitlines():
        line = raw_line.strip()
        if line.startswith("#EXT-X-STREAM-INF:"):
            pending_variant = parse_manifest_attributes(line)
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
        attrs = parse_manifest_attributes(line)
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
        lang = clean_text(subtitle.get("language") or subtitle.get("lang") or subtitle.get("locale")) if isinstance(subtitle, dict) else "-"
        codec = urlparse(url).path.rsplit(".", 1)[-1].lower() if "." in urlparse(url).path else "-"
        key = (codec, lang or "-")
        if key in seen:
            continue
        seen.add(key)
        streams.append({
            "type": "Sub",
            "resolution": "-",
            "bitrate": "-",
            "codec": codec,
            "lang": lang or "-",
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
        if not lines:
            continue
        if lines[0].upper().startswith(("WEBVTT", "NOTE", "STYLE", "REGION")):
            continue

        time_index = next((index for index, line in enumerate(lines) if "-->" in line), None)
        if time_index is None:
            continue

        start, _, end = lines[time_index].partition("-->")
        end = end.strip().split(" ", 1)[0]
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
            "sl": "sv",
            "tl": "en",
            "dt": "t",
            "q": text,
        },
        headers={"Accept": "application/json,text/plain,*/*", "User-Agent": DEFAULT_HEADERS["User-Agent"]},
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


def progress_bar(current, total, width=28):
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


def subtitle_url(subtitle):
    if not isinstance(subtitle, dict):
        return ""
    for key in ("url", "href", "link", "location", "src"):
        value = clean_text(subtitle.get(key))
        if value:
            return value
    return ""


def subtitle_preference_score(subtitle):
    text = json.dumps(subtitle, ensure_ascii=False).lower() if isinstance(subtitle, dict) else ""
    score = 0
    if "sv" in text or "swe" in text or "swedish" in text or "svenska" in text:
        score += 100
    if "webvtt" in text or ".vtt" in text:
        score += 20
    if subtitle_url(subtitle):
        score += 10
    return score


def get_subtitle(playback):
    subtitles = [subtitle for subtitle in playback.subtitles or [] if subtitle_url(subtitle)]
    if not subtitles:
        return None
    subtitles.sort(key=subtitle_preference_score, reverse=True)
    return subtitles[0]


def save_translated_subtitles(playback, filename):
    subtitle = get_subtitle(playback)
    if not subtitle:
        detail = f" subtitleType={playback.subtitle_type}" if playback.subtitle_type else ""
        print(f"{bcolors.WARNING}{icons.ICON_WARNING} No external Swedish subtitle URL found in SVT playback response.{detail}{bcolors.ENDC}")
        return None

    url = subtitle_url(subtitle)
    response = session.get(url, headers=DEFAULT_HEADERS, timeout=30)
    response.raise_for_status()
    cues = parse_vtt(response.text)
    if not cues:
        print(f"{bcolors.WARNING}{icons.ICON_WARNING} No subtitle cues found in SVT VTT response.{bcolors.ENDC}")
        return None

    output_path = SAVE_PATH / f"{filename}.en.srt"
    print(f"{bcolors.LIGHTBLUE}{icons.ICON_WAITING} Translating Swedish subtitles to English SRT...{bcolors.ENDC}")
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
    parts.extend([resolution, "SVT", "WEB-DL", "AAC2.0", "H.265"])
    return ".".join(part for part in parts if part and part != "Unknown")


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
        print(f"{icons.ICON_FAILURE} {bcolors.FAIL}No SVT episodes found.{bcolors.ENDC}")
        return
    show = episode_items[0].get("show_title", "SVT")
    grouped_items = group_episode_items_by_series(episode_items)
    group_labels = sorted(grouped_items, key=series_group_sort_key)
    series_summary = ",  ".join(f"{label}({len(grouped_items[label])})" for label in group_labels)
    print(f"{icons.ICON_SUCCESS} {bcolors.OKGREEN}Found {len(episode_items)} SVT episodes{bcolors.ENDC}")
    print()
    print_series_rule("SVT Series", show)
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
        raise ValueError(f"No SVT episodes matched selector {format_download_selector(parsed)}.")
    return selected


def print_download_queue(episode_items):
    print()
    print(f"{bcolors.GRAY}Download queue:{bcolors.ENDC}")
    for item in episode_items:
        print(f"{bcolors.GRAY}{format_queue_selector(item)} {item.get('title') or ''}{bcolors.ENDC}".rstrip())


def build_download_command(playback, filename, keys=None, interactive=False, quality=None):
    selectors = "" if interactive else f"{video_selector(quality)} --select-audio best --drop-subtitle all "
    command = (
        f'{N_M3U8DL} "{playback.manifest_url}" '
        f'{selectors}'
        f'-mt -M format=mkv --save-dir "{SAVE_PATH}" --save-name "{filename}"'
    )

    if keys:
        command += " " + " ".join(f"--key {key}" for key in keys)

    if SERVICE_PROXY:
        command += f' --custom-proxy "{SERVICE_PROXY}"'

    return command


def print_playback_details(playback, keys, command):
    label = "MPD URL" if playback.manifest_type == "mpd" else "M3U8 URL"
    print(f"{bcolors.LIGHTBLUE}{label}: {bcolors.ENDC}{playback.manifest_url}")

    if playback.license_url:
        print(f"{bcolors.RED}License URL: {bcolors.ENDC}{playback.license_url}")
    if playback.key_id:
        print(f"{bcolors.LIGHTBLUE}ClearKey KID: {bcolors.ENDC}{playback.key_id}")
    if keys:
        print(f"{bcolors.OKGREEN}KEYS: {bcolors.ENDC}" + " ".join(f"--key {key}" for key in keys))
    elif playback.manifest_type != "mpd" or playback.is_encrypted:
        print(f"{icons.ICON_WARNING} {bcolors.WARNING}No keys available - content may be encrypted{bcolors.ENDC}")

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
    page_id = extract_video_id(video_url)
    metadata = search_metadata(video_url, page_id)
    playback = get_playback_info(video_url, metadata)

    keys = []
    if playback.manifest_type == "mpd":
        playback.key_id, playback.license_url, playback.is_encrypted = parse_dash_clearkey_data(playback.manifest_url)
        if playback.license_url and playback.key_id:
            keys = get_clearkey_keys(playback.key_id, playback.license_url)
    elif playback.manifest_type != "m3u8":
        raise ValueError(f"Unsupported manifest type: {playback.manifest_type}")

    manifest_text = fetch_manifest(playback.manifest_url)
    streams, detected_manifest_type = parse_manifest_streams(manifest_text)
    playback.manifest_type = "mpd" if detected_manifest_type == "DASH" else "m3u8"
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
    if resolution == "Unknown":
        resolution = get_resolution(playback)
    filename = format_filename(metadata, resolution)
    filename = apply_quality_to_filename(filename, quality)
    command = build_download_command(playback, filename, keys, interactive=interactive, quality=quality)
    return playback, keys, resolution, filename, command


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
        raise ValueError("Info mode requires an SVT Play episode/video URL.")

    playback, keys, _resolution, filename, _command = run_with_spinner(lambda: resolve_video(video_url))
    manifest_label = "DASH Manifest URL" if playback.manifest_type == "mpd" else "HLS Manifest URL"
    print(f"{bcolors.LIGHTBLUE}{manifest_label}: {bcolors.ENDC}{playback.manifest_url}")
    if keys:
        print(f"{bcolors.OKGREEN}KEYS: {bcolors.ENDC}" + " ".join(f"--key {key}" for key in keys))
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
    print(f"{icons.ICON_INFO} {bcolors.LIGHTBLUE}Processing: {bcolors.ENDC}{video_url}")
    playback, keys, resolution, filename, command = run_with_spinner(lambda: resolve_video(video_url, interactive=interactive, quality=quality))
    metadata = playback.metadata
    episode_str = f"S{metadata.season:02d}E{metadata.episode:02d}" if metadata.season and metadata.episode else ""
    if metadata.title != "Unknown" or episode_str or metadata.episode_title:
        detail_parts = [part for part in (episode_str, metadata.episode_title) if part]
        detail = f" {' - '.join(detail_parts)}" if detail_parts else ""
        print(f"{icons.ICON_SUCCESS} {bcolors.OKGREEN}{metadata.title}{bcolors.ENDC}{detail}")

    print_playback_details(playback, keys, command)
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
        print(f"{icons.ICON_FAILURE} {bcolors.FAIL}No SVT episodes found to export.{bcolors.ENDC}")
        return

    export_dir = Path(__file__).resolve().parents[2] / "export"
    export_dir.mkdir(parents=True, exist_ok=True)
    show_title = episode_items[0].get("show_title", "SVT")
    output_path = export_dir / f"svt_{safe_name(show_title)}_episodes.txt"
    output_path.write_text("\n".join(item["url"] for item in episode_items) + "\n", encoding="utf-8")
    print(f"{icons.ICON_SUCCESS} {bcolors.OKGREEN}Exported list: {output_path}{bcolors.ENDC}")


def main(video_url, downloads_path, wvd_device_path, mode="auto", export_list=False, download_selector=None, quality=None):
    """Eurovine entry point for SVT Play (DASH/HLS with ClearKey where required)."""
    try:
        if not video_url:
            raise ValueError("No SVT URL provided.")
        if not downloads_path:
            raise ValueError("Eurovine config requires downloads_path for SVT.")

        configure_service(downloads_path, wvd_device_path)
        video_url = canonical_url(video_url.strip())

        if mode == "list":
            print(f"{icons.ICON_WAITING} {bcolors.LIGHTBLUE}Retrieving series information.....{bcolors.ENDC}")
            episode_items = collect_episode_items(video_url)
            list_episode_items(episode_items)
            if export_list:
                export_episode_urls(episode_items)
            return

        if mode == "download":
            print(f"{icons.ICON_WAITING} {bcolors.LIGHTBLUE}Retrieving series information.....{bcolors.ENDC}")
            download_selected_episodes(video_url, download_selector, quality)
            return

        if mode == "info":
            print_info_mode(video_url)
            return

        if export_list:
            print(f"{icons.ICON_WAITING} {bcolors.LIGHTBLUE}Retrieving series information.....{bcolors.ENDC}")
            export_episode_urls(collect_episode_items(video_url))
            return

        if is_series_url(video_url):
            print(f"{icons.ICON_WARNING} {bcolors.WARNING}Series URLs require a flag. Use --list/-l to list episodes, --export/-x to export episode URLs, or --download/-d SELECTOR to download selected episodes.{bcolors.ENDC}")
            return

        process_video(video_url, interactive=(mode == "interactive"), quality=quality)
    except (binascii.Error, ValueError, requests.RequestException, RuntimeError) as exc:
        print(f"{icons.ICON_FAILURE} {bcolors.FAIL}Error: {exc}{bcolors.ENDC}")
        raise
    except Exception as exc:
        print(f"{icons.ICON_FAILURE} {bcolors.FAIL}Unexpected error: {exc}{bcolors.ENDC}")
        raise
