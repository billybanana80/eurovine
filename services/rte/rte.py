import json
import re
import requests
import subprocess
import sys
import os
import urllib3
import shutil
from base64 import b64encode, b64decode
from xml.etree import ElementTree as ET
from urllib.parse import urljoin, urlsplit, urlunsplit
from pywidevine.device import Device
from pywidevine.cdm import Cdm
from pywidevine.pssh import PSSH
import icons
from download_confirm import confirm_download
from colors import bcolors
from pathlib import Path
from quality_utils import apply_quality_to_filename, video_selector
from services.proxy import append_downloader_proxy, current_proxy_url, mask_proxy_command
from beaupy.spinners import Spinner

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


CONFIG = {
    "headers": {
        "user-agent": "Dalvik/2.1.0 (Linux; U; Android 13; SM-A536E Build/RSR1.210722.013.A2)",
        "accept": "application/json",
        "origin": "https://www.rte.ie",
    },
    "endpoints": {
        "base_url": "https://www.rte.ie",
        "license": "https://widevine.entitlement.eu.theplatform.com/wv/web/ModularDrm",
    },
    "wvd_path": None,
    "save_path": None,
}

session = requests.Session()
RTE_PROXY = None

def mask_proxy(proxy_text):
    return mask_proxy_command(proxy_text)


def configure_service(downloads_path, wvd_device_path):
    global RTE_PROXY
    CONFIG["save_path"] = downloads_path
    CONFIG["wvd_path"] = wvd_device_path
    session.proxies.clear()
    proxy_url = current_proxy_url()
    RTE_PROXY = proxy_url
    if proxy_url:
        session.proxies.update({"http": proxy_url, "https": proxy_url})
        session.verify = False

# ----------------------------------------------------------------------
# Core RTE helpers
# ----------------------------------------------------------------------
def rte_get(api, params=None, headers=None):
    """
    Helper to mimic Unshackle's _request().
    api can be a relative path (e.g. "/servicelayer/api/anonymouslogin")
    or a full URL (e.g. https://link.eu.theplatform.com/s/1uC-gC/media/...).
    """
    if isinstance(api, str) and api.startswith("http"):
        url = api
    else:
        url = urljoin(CONFIG["endpoints"]["base_url"], api)

    merged_headers = CONFIG["headers"].copy()
    if headers:
        merged_headers.update(headers)

    r = session.get(url, params=params, headers=merged_headers, timeout=15)
    if r.status_code != 200:
        raise ConnectionError(
            f"Status: {r.status_code} - {r.url}\n"
            "Content may be geo-restricted to IE"
        )

    try:
        return r.json()
    except ValueError:
        return r.text


def get_config():
    """
    Get anonymous MPX token and account ID.
    """
    anon = rte_get("/servicelayer/api/anonymouslogin")
    token = anon["mpx_token"]

    cfg = rte_get("/wordpress/wp-content/uploads/standard/web/config.json")
    account = cfg["mpx_config"]["account_id"]

    return token, account


def get_episode_metadata(episode_url: str) -> dict:
    """
    Resolve the RTE programme metadata from a series/episode or movie URL.
    """
    # Example:
    # https://www.rte.ie/player/series/hidden-assets/SI0000012001?epguid=IP10012641-03-0001
    m = re.search(r"/series/[^/]+/(SI\d+)(?:\?epguid=([A-Z0-9\-]+))?", episode_url)
    if not m:
        movie_match = re.search(r"/movie/[^/]+/(\d+)", episode_url)
        if movie_match:
            movie_id = movie_match.group(1)
            programs = rte_get(f"/mpx/1uC-gC/rte-prd-prd-all-programs?byId={movie_id}")
            entries = programs.get("entries", [])
            if not entries:
                raise ValueError("No matching RTE movie/programme found for this URL")
            return entries[0]
        raise ValueError("Could not parse series GUID / epguid or movie ID from URL")

    series_guid = m.group(1)
    ep_guid = m.group(2)

    # 1) Map series GUID -> seriesId via "all-movies-series"
    series_meta = rte_get(f"/mpx/1uC-gC/rte-prd-prd-all-movies-series?byGuid={series_guid}")
    series_entry_id = series_meta["entries"][0]["id"]
    series_id = series_entry_id.split("/")[-1]

    # 2) Fetch all-programs for this seriesId, then filter episodes
    programs = rte_get(f"/mpx/1uC-gC/rte-prd-prd-all-programs?bySeriesId={series_id}")
    entries = programs.get("entries", [])

    episodes = [
        e for e in entries
        if e.get("plprogram$programType") == "episode"
    ]
    if ep_guid:
        episodes = [e for e in episodes if e.get("guid") == ep_guid]

    if not episodes:
        raise ValueError("No matching episode found for this URL")

    return episodes[0]


def clean_url(url):
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/"), "", ""))


def parse_series_url(series_url: str):
    m = re.search(r"/series/[^/]+/(SI\d+)", series_url)
    if not m:
        raise ValueError("Could not parse RTE series GUID from URL")
    return clean_url(series_url), m.group(1)


def is_episode_url(url):
    return "epguid=" in urlsplit(url).query or re.search(r"/movie/[^/]+/\d+", urlsplit(url).path) is not None


def clean_text(value):
    return re.sub(r"\s+", " ", str(value or "")).strip()


def get_series_program_entries(series_guid):
    series_meta = rte_get(f"/mpx/1uC-gC/rte-prd-prd-all-movies-series?byGuid={series_guid}")
    series_entry_id = series_meta["entries"][0]["id"]
    series_id = series_entry_id.split("/")[-1]
    programs = rte_get(f"/mpx/1uC-gC/rte-prd-prd-all-programs?bySeriesId={series_id}")
    return programs.get("entries", [])


def episode_series_number(item):
    season = item["episode"].get("plprogram$tvSeasonNumber")
    return int(season) if season not in (None, "") else None


def episode_number(item):
    episode = item["episode"].get("plprogram$tvSeasonEpisodeNumber")
    return int(episode) if episode not in (None, "") else None


def episode_title(episode):
    title = episode.get("description") or episode.get("plprogram$shortDescription") or ""
    ep_num = episode.get("plprogram$tvSeasonEpisodeNumber")
    if title:
        compact = title.lower().replace(" ", "")
        redundant = {f"episode{ep_num}", f"ep{ep_num}", f"e{ep_num}"} if ep_num else set()
        if compact not in redundant:
            return title.strip()
    return f"Episode {int(ep_num):02d}" if ep_num not in (None, "") else "Unknown"


def episode_tree_label(item):
    number = episode_number(item)
    return str(number).zfill(2) if number is not None else "-", episode_title(item["episode"])


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


def has_subtitle_stream(streams):
    return any(stream.get("type") == "Sub" for stream in streams or [])


def external_subtitle_streams(subtitles):
    rows = []
    seen = set()
    for subtitle in subtitles or []:
        url = clean_text(subtitle.get("url") if isinstance(subtitle, dict) else subtitle)
        if not url or url in seen:
            continue
        seen.add(url)
        codec = "vtt" if ".vtt" in url.lower() else "-"
        rows.append({
            "type": "Sub",
            "resolution": "-",
            "bitrate": "-",
            "codec": codec,
            "lang": clean_text(subtitle.get("lang") if isinstance(subtitle, dict) else "") or "en",
            "channels": "-",
        })
    return rows


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


def episode_air_date(episode):
    return (
        episode.get("plprogram$pubDate")
        or episode.get("plprogram$airDate")
        or episode.get("plprogram$originalAirDate")
        or "Unknown"
    )


def episode_description(episode):
    return clean_text(
        episode.get("plprogram$longDescription")
        or episode.get("longDescription")
        or episode.get("plprogram$shortDescription")
        or episode.get("description")
    ) or "Unknown"


def print_episode_metadata(episode):
    rows = [
        ("Show", clean_text(episode.get("plprogram$longTitle")) or "RTE"),
        ("Title", episode_title(episode)),
        ("Date Aired", clean_text(episode_air_date(episode))),
        ("Description", episode_description(episode)),
    ]
    print(f"\n{bcolors.YELLOW}Episode metadata:{bcolors.ENDC}")
    for label, value in rows:
        if value:
            print(f"{bcolors.LIGHTBLUE}{label}: {bcolors.ENDC}{value}")


def episode_sort_key(item):
    season = episode_series_number(item)
    episode = episode_number(item)
    return (
        season if season is not None else 9999,
        episode if episode is not None else 9999,
        item.get("id", ""),
    )


def collect_episode_items(series_url, show_progress=True):
    base_series_url, series_guid = parse_series_url(series_url)
    entries = get_series_program_entries(series_guid)
    episode_items = []
    show_title = None

    for episode in entries:
        if episode.get("plprogram$programType") != "episode":
            continue
        guid = episode.get("guid")
        if not guid:
            continue
        show_title = show_title or episode.get("plprogram$longTitle") or "RTE"
        episode_items.append({
            "url": f"{base_series_url}?epguid={guid}",
            "id": guid,
            "episode": episode,
            "show_title": show_title,
        })

    if show_progress and show_title:
        print(f"{icons.ICON_INFO} {bcolors.LIGHTBLUE}Show: {bcolors.ENDC}{show_title}")

    episode_items.sort(key=episode_sort_key)
    return episode_items


def collect_episode_item(episode_url):
    episode = get_episode_metadata(episode_url)
    guid = episode.get("guid")
    return {
        "url": clean_url(episode_url) + f"?epguid={guid}",
        "id": guid,
        "episode": episode,
        "show_title": episode.get("plprogram$longTitle") or "RTE",
    }


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
        raise ValueError(f"No RTE episodes found for selector {format_download_selector(parsed_selector)}.")

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
        print(f"{icons.ICON_FAILURE} {bcolors.FAIL}No RTE episodes found.{bcolors.ENDC}")
        return

    show_title = episode_items[0].get("show_title", "RTE")
    grouped_items = group_episode_items_by_series(episode_items)
    group_labels = sorted(grouped_items, key=series_group_sort_key)
    series_summary = ",  ".join(f"{label}({len(grouped_items[label])})" for label in group_labels)

    print(f"{icons.ICON_SUCCESS} {bcolors.OKGREEN}Found {len(episode_items)} RTE episodes{bcolors.ENDC}")
    print()
    print_series_rule("RTE Series", show_title)
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


def get_manifest_and_pid(media_url: str, token: str):
    """
    Request SMIL from theplatform and pull out:
      - MPEG-DASH manifest URL
      - releasePid (for license requests)
    """
    params = {
        "formats": "MPEG-DASH",
        "auth": token,
        "assetTypes": "default:isl",
        "tracking": "true",
        "format": "SMIL",
        "iu": "/3014/RTE_Player_VOD/Android_Phone/NotRegistered",
        "policy": "168602703",
    }

    smil = rte_get(media_url, params=params)

    # smil is XML text; parse with ElementTree
    root = ET.fromstring(smil.encode("utf-8") if isinstance(smil, str) else smil)

    def tagname(elem):
        return elem.tag.split("}", 1)[-1]  # strip namespace

    manifest_url = None
    tracking_value = None
    subtitle_streams = []

    for elem in root.iter():
        if tagname(elem) == "textstream" and elem.attrib.get("src"):
            subtitle_streams.append({
                "url": elem.attrib["src"],
                "type": elem.attrib.get("type") or "text/vtt",
                "lang": elem.attrib.get("lang") or "en",
            })
        if tagname(elem) == "switch":
            # video child -> manifest src
            for child in list(elem):
                if tagname(child) == "video" and "src" in child.attrib:
                    manifest_url = child.attrib["src"]
                if tagname(child) == "ref":
                    # find param name="trackingData"
                    for p in child.iter():
                        if tagname(p) == "param" and p.attrib.get("name") == "trackingData":
                            tracking_value = p.attrib.get("value")

    if not manifest_url:
        # DEBUG: dump SMIL for inspection
        with open("smil_surfshark_debug.xml", "w", encoding="utf-8") as f:
            f.write(smil if isinstance(smil, str) else smil.decode("utf-8", errors="ignore"))
        raise ValueError("Could not find manifest URL in SMIL")


    if not tracking_value:
        raise ValueError("Could not find trackingData in SMIL")

    m = re.search(r"pid=([^|]+)", tracking_value)
    if not m:
        raise ValueError("Could not extract releasePid from trackingData")

    pid = m.group(1)
    return manifest_url, pid, subtitle_streams


# ---- DASH / DRM helpers -------------------------------------------------
def get_max_resolution(manifest_content: bytes) -> str:
    root = ET.fromstring(manifest_content)
    max_height = 0
    for representation in root.iter("{urn:mpeg:dash:schema:mpd:2011}Representation"):
        if "thumb" in representation.attrib.get("id", "").lower():
            continue
        height = int(representation.attrib.get("height", 0))
        if height > max_height:
            max_height = height
    return f"{max_height}p" if max_height else "Unknown"


def extract_pssh(mpd_url: str):
    """
    Fetch the MPD through the global requests.Session
    and extract ONLY the Widevine PSSH:
      schemeIdUri="urn:uuid:EDEF8BA9-79D6-4ACE-A3C8-27DCD51D21ED"
    Returns (pssh_b64, manifest_content_bytes)
    """
    # session already has proxy settings wired from config.yaml
    r = session.get(mpd_url, timeout=15)
    if r.status_code != 200:
        raise Exception(f"Failed to fetch MPD: {r.status_code}")

    manifest_content = r.content
    root = ET.fromstring(manifest_content)

    widevine_uuid = "urn:uuid:EDEF8BA9-79D6-4ACE-A3C8-27DCD51D21ED"
    pssh_b64 = None

    # iterate over all ContentProtection elements
    for elem in root.findall(".//{urn:mpeg:dash:schema:mpd:2011}ContentProtection"):
        if elem.attrib.get("schemeIdUri") == widevine_uuid:
            pssh = elem.find("{urn:mpeg:cenc:2013}pssh")
            if pssh is not None and pssh.text:
                pssh_b64 = pssh.text.strip()
                break

    if not pssh_b64:
        raise Exception("Widevine PSSH not found in MPD.")

    return pssh_b64, manifest_content


def generate_widevine_challenge(pssh_b64: str):
    pssh_bytes = b64decode(pssh_b64)
    pssh = PSSH(pssh_bytes)

    device = Device.load(CONFIG["wvd_path"])
    cdm = Cdm.from_device(device)

    session_id = cdm.open()
    challenge = cdm.get_license_challenge(pssh=pssh, session_id=session_id)
    return challenge, cdm, session_id


def extract_keys(cdm, session_id, license_response: bytes):
    cdm.parse_license(session_id, license_response)

    keys = []
    for key in cdm.get_keys(session_id):
        if "CONTENT" in key.type:
            kid_hex = key.kid.hex
            key_hex = key.key.hex()
            keys.append(f"{kid_hex}:{key_hex}")

    cdm.close(session_id)
    return keys


def get_widevine_license(challenge: bytes, token: str, account: str, pid: str) -> bytes:
    """
    Request the Widevine license from theplatform through the global requests.Session.
    """
    params = {
        "token": token,
        "account": account,
        "form": "json",
        "schema": "1.0",
    }
    payload = {
        "getWidevineLicense": {
            "releasePid": pid,
            "widevineChallenge": b64encode(challenge).decode("utf-8"),
        }
    }

    # Use the same requests.Session used by the metadata and manifest calls.
    r = session.post(CONFIG["endpoints"]["license"], params=params, json=payload, timeout=15)
    if not r.ok:
        raise Exception(f"License request failed: {r.status_code}, {r.text[:200]}")

    data = r.json()
    return b64decode(data["getWidevineLicenseResponse"]["license"])


def build_video_name(episode, max_res):
    show_name = episode.get("plprogram$longTitle", "RTE_Show").replace(" ", ".")
    season_num = int(episode.get("plprogram$tvSeasonNumber") or 0)
    episode_num = int(episode.get("plprogram$tvSeasonEpisodeNumber") or 0)
    show_clean = re.sub(r"[^A-Za-z0-9]+", ".", show_name).strip(".")
    if not season_num or not episode_num or episode.get("plprogram$programType") == "movie":
        return f"{show_clean}.{max_res}.RTE.WEB-DL.AAC2.0.H.264"
    return f"{show_clean}.S{season_num:02d}E{episode_num:02d}.{max_res}.RTE.WEB-DL.AAC2.0.H.264"


def subtitle_selector(save_subs=False):
    return "--select-subtitle all" if save_subs else "--drop-subtitle all"


def strip_subtitle_tags(text):
    return clean_text(re.sub(r"<[^>]+>", "", text))


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


def save_external_subtitles(subtitles, video_name):
    candidates = [subtitle for subtitle in subtitles or [] if clean_text(subtitle.get("url"))]
    if not candidates:
        print(f"{bcolors.WARNING}{icons.ICON_WARNING} No RTE subtitles found.{bcolors.ENDC}")
        return None

    subtitle = candidates[0]
    response = session.get(subtitle["url"], headers={"Accept": "text/vtt,*/*"}, timeout=30)
    response.raise_for_status()
    cues = parse_vtt(response.text)
    if not cues:
        print(f"{bcolors.WARNING}{icons.ICON_WARNING} No RTE subtitle cues found.{bcolors.ENDC}")
        return None

    lang = clean_text(subtitle.get("lang")) or "en"
    output_path = Path(CONFIG["save_path"]) / f"{video_name}.{lang}.srt"
    write_srt(cues, output_path)
    print(f"{bcolors.OKGREEN}{icons.ICON_SUCCESS} External subtitles saved: {bcolors.ENDC}{output_path}")
    return output_path


def resolve_playback(ep_url, interactive=False, quality=None, save_subs=False):
    episode = get_episode_metadata(ep_url)
    media_list = episode.get("plprogramavailability$media") or []
    if not media_list:
        raise Exception("No plprogramavailability$media found for episode")
    media_url = media_list[0].get("plmedia$publicUrl")
    if not media_url:
        raise Exception("No plmedia$publicUrl in media entry")

    token, account = get_config()
    mpd_url, pid, subtitle_streams = get_manifest_and_pid(media_url, token)
    licence_url = (
        f'{CONFIG["endpoints"]["license"]}'
        f'?token={token}&account={account}&form=json&schema=1.0'
    )
    pssh_b64, manifest_content = extract_pssh(mpd_url)
    max_res = get_max_resolution(manifest_content)

    challenge, cdm, session_id = generate_widevine_challenge(pssh_b64)
    license_blob = get_widevine_license(challenge, token, account, pid)
    keys = extract_keys(cdm, session_id, license_blob)
    if not keys:
        raise Exception("No decryption keys found in license response")

    video_name = build_video_name(episode, max_res)
    video_name = apply_quality_to_filename(video_name, quality)
    key = keys[0]
    selector = "" if interactive else f"{video_selector(quality)} --select-audio lang=en:for=best {subtitle_selector(save_subs)} "
    command = (
        f'N_m3u8DL-RE.exe "{mpd_url}" '
        f'{selector}'
        f'-mt -M format=mkv:muxer=mkvmerge '
        f'--thread-count 16 '
        f'--download-retry-count 10 '
        f'--save-name "{video_name}" '
        f'--save-dir "{CONFIG["save_path"]}" '
        f'--key {key} '
    )
    if RTE_PROXY:
        command += f'--custom-proxy "{RTE_PROXY}" '

    streams, manifest_type = parse_manifest_streams(manifest_content)
    manifest_has_subtitles = has_subtitle_stream(streams)
    if not manifest_has_subtitles:
        streams = sorted(streams + external_subtitle_streams(subtitle_streams), key=stream_sort_key)
    return {
        "episode": episode,
        "manifest_url": mpd_url,
        "manifest_type": manifest_type,
        "licence_url": licence_url,
        "pssh": pssh_b64,
        "keys": keys,
        "max_res": max_res,
        "video_name": video_name,
        "command": command,
        "streams": streams,
        "manifest_has_subtitles": manifest_has_subtitles,
        "subtitle_streams": subtitle_streams,
    }


def info(ep_url):
    if not is_episode_url(ep_url):
        raise ValueError("Info mode requires an RTE episode/video URL.")

    spinner = Spinner()
    spinner.start()
    try:
        resolved = resolve_playback(ep_url)
    except Exception:
        spinner.stop()
        raise
    else:
        spinner.stop()

    print(f"{bcolors.LIGHTBLUE}{resolved['manifest_type']} Manifest URL: {bcolors.ENDC}{resolved['manifest_url']}")
    keys = resolved.get("keys") or []
    if keys:
        print(f"{bcolors.GREEN}KEYS: {bcolors.ENDC}{' '.join(f'--key {key}' for key in keys)}")
    print_streams(resolved["streams"])
    print_episode_metadata(resolved["episode"])
    print(f"\n{bcolors.YELLOW}Suggested filename: {bcolors.ENDC}{resolved['video_name']}.mkv")


# ---- Main ---------------------------------------------------------------
def process_video(ep_url, auto_download=False, interactive=False, quality=None, save_subs=False):
    try:
        spinner = Spinner()
        spinner.start()
        try:
            resolved = resolve_playback(ep_url, interactive=interactive, quality=quality, save_subs=save_subs)
        except Exception:
            spinner.stop()
            raise
        else:
            spinner.stop()

        print(f"{bcolors.LIGHTBLUE}MPD URL: {bcolors.ENDC}{resolved['manifest_url']}")
        print(f"{bcolors.RED}Licence URL: {bcolors.ENDC}{resolved['licence_url']}")
        print(f"{bcolors.LIGHTBLUE}PSSH: {bcolors.ENDC}{resolved['pssh']}")

        for key in resolved["keys"]:
            print(f"{bcolors.GREEN}KEYS: {bcolors.ENDC}--key {key}")

        print(f"{bcolors.YELLOW}DOWNLOAD COMMAND: {bcolors.ENDC}{mask_proxy(resolved['command'])}")

        if save_subs and not resolved.get("manifest_has_subtitles"):
            save_external_subtitles(resolved.get("subtitle_streams"), resolved["video_name"])

        if auto_download:
            print(f"{icons.ICON_WAITING} {bcolors.OKBLUE}Downloading video...{bcolors.ENDC}")
            subprocess.run(resolved["command"], shell=True)
            return True

        user_input = input("Do you wish to download? Y or N: ").strip().lower()
        if user_input == "y":
            subprocess.run(resolved["command"], shell=True)
            return True
        return False

        episode = get_episode_metadata(ep_url)

        show_name = episode.get("plprogram$longTitle", "RTE_Show").replace(" ", ".")
        season_num = int(episode.get("plprogram$tvSeasonNumber") or 0)
        episode_num = int(episode.get("plprogram$tvSeasonEpisodeNumber") or 0)

        media_list = episode.get("plprogramavailability$media") or []
        if not media_list:
            raise Exception("No plprogramavailability$media found for episode")
        media_url = media_list[0].get("plmedia$publicUrl")
        if not media_url:
            raise Exception("No plmedia$publicUrl in media entry")

        # print(f"{bcolors.ORANGE}Media URL: {bcolors.ENDC}{media_url}")

        # 3) Config: token + account
        token, account = get_config()

        # 4) Manifest + pid
        mpd_url, pid, _subtitle_streams = get_manifest_and_pid(media_url, token)
        print(f"{bcolors.LIGHTBLUE}MPD URL: {bcolors.ENDC}{mpd_url}")
        # print(f"{bcolors.RED}releasePid: {bcolors.ENDC}{pid}")

        # 4a) Licence URL (query string only – body still sent via POST later)
        licence_url = (
            f'{CONFIG["endpoints"]["license"]}'
            f'?token={token}&account={account}&form=json&schema=1.0'
        )
        print(f"{bcolors.RED}Licence URL: {bcolors.ENDC}{licence_url}")

        # 5) PSSH + max resolution
        pssh_b64, manifest_content = extract_pssh(mpd_url)
        max_res = get_max_resolution(manifest_content)
        print(f"{bcolors.LIGHTBLUE}PSSH: {bcolors.ENDC}{pssh_b64}")
        # print(f"{bcolors.OKGREEN}Max resolution: {bcolors.ENDC}{max_res}")

        # 6) Widevine: challenge -> license -> keys
        challenge, cdm, session_id = generate_widevine_challenge(pssh_b64)
        license_blob = get_widevine_license(challenge, token, account, pid)
        keys = extract_keys(cdm, session_id, license_blob)

        key = None
        for k in keys:
            key = k
            print(f"{bcolors.GREEN}KEYS: {bcolors.ENDC}--key {k}")

        if not key:
            raise Exception("No decryption keys found in license response")

        # 7) Build nice filename + N_m3u8DL-RE command
        show_clean = re.sub(r"[^A-Za-z0-9]+", ".", show_name).strip(".")
        video_name = f"{show_clean}.S{season_num:02d}E{episode_num:02d}.{max_res}.RTE.WEB-DL.AAC2.0.H.264"
        video_name = apply_quality_to_filename(video_name, quality)

        command = (
            f'N_m3u8DL-RE.exe "{mpd_url}" '
            f'{video_selector(quality)} --select-audio lang=en:for=best --select-subtitle all '
            f'-mt -M format=mkv:muxer=mkvmerge '
            f'--thread-count 16 '
            f'--download-retry-count 10 '
            f'--save-name "{video_name}" '
            f'--save-dir "{CONFIG["save_path"]}" '
            f'--key {key} '
        )
        if RTE_PROXY:
            command += f'--custom-proxy "{RTE_PROXY}" '

        print(f"{bcolors.YELLOW}DOWNLOAD COMMAND: {bcolors.ENDC}{mask_proxy(command)}")

        if auto_download:
            print(f"{icons.ICON_WAITING} {bcolors.OKBLUE}Downloading video...{bcolors.ENDC}")
            subprocess.run(command, shell=True)
            return True

        user_input = input("Do you wish to download? Y or N: ").strip().lower()
        if user_input == "y":
            subprocess.run(command, shell=True)
            return True
        return False

    except Exception as e:
        print(f"{bcolors.FAIL}Failed to download RTE video: {bcolors.ENDC}{e}")
        return False


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
        process_video(item["url"], auto_download=True, quality=quality, save_subs=save_subs)


def eurovine_main(video_url, downloads_path, wvd_device_path, mode="auto", export_list=False, download_selector=None, quality=None, auto_confirm=False, save_subs=False):
    configure_service(downloads_path, wvd_device_path)
    if mode == "list":
        items = [collect_episode_item(video_url)] if is_episode_url(video_url) else collect_episode_items(video_url, show_progress=False)
        list_episode_items(items)
        if export_list:
            out = Path(__file__).resolve().parents[2] / "export" / "rte_episodes.txt"; out.parent.mkdir(parents=True, exist_ok=True); out.write_text("\n".join(x["url"] for x in items)+"\n", encoding="utf-8")
            print(f"{icons.ICON_SUCCESS} {bcolors.OKGREEN}Exported list: {out}{bcolors.ENDC}")
        return
    if mode == "info": return info(video_url)
    if mode == "download": return download_selected_episodes(video_url, download_selector, quality, auto_confirm, save_subs=save_subs)
    if is_episode_url(video_url): return process_video(video_url, auto_download=auto_confirm, interactive=(mode == "interactive"), quality=quality, save_subs=save_subs)
    raise ValueError("Series URLs require --list/-l or --download/-d.")

main = eurovine_main
