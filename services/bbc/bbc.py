import requests
import re
import subprocess
import os
import sys
import urllib3
import shutil
import ssl
from datetime import datetime
from urllib.parse import urlsplit, urlunsplit
from lxml import etree, html
from requests.adapters import HTTPAdapter
from beaupy.spinners import Spinner
import icons
from colors import bcolors
from pathlib import Path
from quality_utils import apply_quality_to_filename, video_selector
from services.proxy import append_downloader_proxy, current_proxy_url, mask_proxy_command

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

session = requests.Session()
BBC_CERTIFICATE_PATH = None

def configure_service(certificate_path=None):
    """Apply proxy and optional UHD certificate configuration from Eurovine."""
    global BBC_CERTIFICATE_PATH
    BBC_CERTIFICATE_PATH = (certificate_path or "").strip()
    session.proxies.clear()
    proxy_url = current_proxy_url()
    if proxy_url:
        session.proxies.update({'http': proxy_url, 'https': proxy_url})


class BBCSecureTLSAdapter(HTTPAdapter):
    """TLS settings required by the BBC UHD client-certificate endpoint."""

    def __init__(self, *args, **kwargs):
        ssl_context = ssl.create_default_context()
        ssl_context.check_hostname = False
        ssl_context.set_ciphers("DEFAULT:@SECLEVEL=0")
        self.ssl_context = ssl_context
        super().__init__(*args, **kwargs)

    def init_poolmanager(self, *args, **kwargs):
        kwargs["ssl_context"] = self.ssl_context
        return super().init_poolmanager(*args, **kwargs)

    def proxy_manager_for(self, *args, **kwargs):
        kwargs["ssl_context"] = self.ssl_context
        return super().proxy_manager_for(*args, **kwargs)


def get_bbc_certificate_path():
    certificate_path = BBC_CERTIFICATE_PATH
    if not certificate_path:
        raise ValueError("BBC UHD certificate is not configured under bbc.certificate in config.yaml.")
    if not os.path.isfile(certificate_path):
        raise ValueError(f"BBC UHD certificate was not found: {certificate_path}")
    return certificate_path


def build_secure_session():
    secure_session = requests.Session()
    secure_session.mount("https://", BBCSecureTLSAdapter())
    secure_session.headers.update({
        'User-Agent': 'smarttv_AFTMM_Build_0003255372676_Chromium_41.0.2250.2'
    })
    proxy_url = current_proxy_url()
    if proxy_url:
        secure_session.proxies.update({'http': proxy_url, 'https': proxy_url})
    return secure_session

# Function to extract video ID from URL
def extract_video_id(video_url):
    match = re.search(r'/episode/([a-zA-Z0-9]+)', video_url)
    if match:
        return match.group(1)
    return None

def clean_url(url):
    split_url = urlsplit(url)
    return urlunsplit((split_url.scheme, split_url.netloc, split_url.path, '', ''))

def build_episode_url(href):
    if href.startswith('http'):
        return clean_url(href)
    return clean_url(f"https://www.bbc.co.uk{href}")

def get_redirected_url(series_url):
    response = session.get(series_url, timeout=30)
    response.raise_for_status()
    return response.url

def extract_series_ids(series_url):
    response = session.get(series_url, timeout=30)
    response.raise_for_status()
    tree = html.fromstring(response.content)
    hrefs = tree.xpath('//a[contains(@href, "seriesId=")]/@href')

    series_ids = []
    for href in hrefs:
        match = re.search(r'seriesId=([^&]+)', href)
        if match and match.group(1) not in series_ids:
            series_ids.append(match.group(1))

    return series_ids

def extract_episode_links(series_url):
    response = session.get(series_url, timeout=30)
    response.raise_for_status()
    tree = html.fromstring(response.content)
    hrefs = tree.xpath('//a[contains(@href, "/iplayer/episode/")]/@href')

    episode_links = []
    for href in hrefs:
        video_id = extract_video_id(href)
        if video_id:
            episode_links.append((href, video_id))

    return episode_links

def get_episode_from_metadata(metadata):
    try:
        return metadata['episodes'][0]
    except (KeyError, IndexError, TypeError):
        return None

def collect_episode_items(series_url, show_progress=True):
    redirected_url = clean_url(get_redirected_url(series_url))
    series_ids = extract_series_ids(redirected_url)
    series_urls = [redirected_url]

    for series_id in series_ids:
        series_urls.append(f"{redirected_url}?seriesId={series_id}")

    expected_show_title = None
    processed_episode_ids = set()
    episode_items = []

    for page_url in series_urls:
        episode_links = extract_episode_links(page_url)
        for href, episode_id in episode_links:
            if episode_id in processed_episode_ids:
                continue

            processed_episode_ids.add(episode_id)
            metadata = get_video_metadata(episode_id)
            episode = get_episode_from_metadata(metadata)
            if not episode:
                if show_progress:
                    print(f"{icons.ICON_WARNING} {bcolors.WARNING}Skipping {episode_id}: could not read episode metadata{bcolors.ENDC}")
                continue

            show_title = episode.get('title')
            if expected_show_title is None:
                expected_show_title = show_title
                if show_progress:
                    print(f"{icons.ICON_INFO} {bcolors.LIGHTBLUE}Show: {bcolors.ENDC}{expected_show_title}")

            if show_title != expected_show_title:
                if show_progress:
                    print(f"{icons.ICON_WARNING} {bcolors.WARNING}Skipping different show: {show_title}{bcolors.ENDC}")
                continue

            episode_items.append({
                'url': build_episode_url(href),
                'id': episode_id,
                'metadata': metadata,
                'episode': episode,
            })

    return episode_items

def collect_episode_urls(series_url):
    return [item['url'] for item in collect_episode_items(series_url)]

# Function to get video metadata from API
def get_video_metadata(video_id):
    metadata_url = f"https://ibl.api.bbci.co.uk/ibl/v1/episodes/{video_id}?rights=mobile&availability=available"
    headers = {
        'User-Agent': 'smarttv_AFTMM_Build_0003255372676_Chromium_41.0.2250.2',
        'Authorization': 'Bearer D2FgtcTxGqqIgLsfBWTJdrQh2tVdeaAp'
    }
    try:
        response = session.get(metadata_url, headers=headers, timeout=30)
    except requests.RequestException as exc:
        print(f"{bcolors.FAIL}Failed to fetch metadata: {exc}{bcolors.ENDC}")
        return None
    if response.status_code == 200:
        return response.json()
    else:
        print(f"{bcolors.FAIL}Failed to fetch metadata, status code: {response.status_code}{bcolors.ENDC}")
        return None

# Function to get playlist data
def get_playlist_data(video_id):
    playlist_url = f"https://www.bbc.co.uk/programmes/{video_id}/playlist.json"
    try:
        response = session.get(playlist_url, timeout=30)
    except requests.RequestException as exc:
        print(f"{bcolors.FAIL}Failed to fetch playlist data: {exc}{bcolors.ENDC}")
        return None
    if response.status_code == 200:
        return response.json()
    else:
        print(f"{bcolors.FAIL}Failed to fetch playlist data, status code: {response.status_code}{bcolors.ENDC}")
        return None

# Function to get media selection data
def get_media_selector_data(vpid, ultra=False, show_error=True):
    try:
        if ultra:
            certificate_path = get_bbc_certificate_path()
            media_selector_url = (
                "https://securegate.iplayer.bbc.co.uk/mediaselector/6/select/version/2.0/"
                f"vpid/{vpid}/format/json/mediaset/iptv-uhd/proto/https"
            )
            response = build_secure_session().get(
                media_selector_url,
                cert=certificate_path,
                timeout=30,
            )
        else:
            media_selector_url = f"https://open.live.bbc.co.uk/mediaselector/6/select/version/2.0/mediaset/iptv-all/vpid/{vpid}/format/json"
            response = session.get(media_selector_url, timeout=30)
    except (requests.RequestException, ValueError) as exc:
        if show_error:
            print(f"{bcolors.FAIL}Failed to fetch media selector data: {exc}{bcolors.ENDC}")
        return None

    if response.status_code == 200:
        return response.json()

    if show_error:
        print(f"{bcolors.FAIL}Failed to fetch media selector data, status code: {response.status_code}{bcolors.ENDC}")
    return None

# Function to get HLS manifest candidates
def get_m3u8_urls(media_selector_data):
    video_connections = []
    for media in media_selector_data.get('media', []):
        if media.get('kind') == 'video':
            video_connections.extend(sorted(media.get('connection', []), key=lambda x: int(x.get('priority', 99))))

    urls = []
    akamai_dash = next((c for c in video_connections if c.get('supplier') == 'mf_akamai' and c.get('transferFormat') == 'dash'), None)
    if akamai_dash and akamai_dash.get('href'):
        urls.append("/".join(
            akamai_dash['href']
            .replace('dash', 'hls')
            .split('?')[0]
            .split('/')[0:-1]
            + ['hls', 'master.m3u8']
        ))

    for connection in video_connections:
        if connection.get('transferFormat') == 'hls':
            urls.append(connection.get('href'))

    return [url for url in urls if url]

# Function to extract the maximum resolution from M3U8 content
def get_max_resolution(m3u8_content):
    resolutions = re.findall(r'RESOLUTION=(\d+x\d+)', m3u8_content)
    if not resolutions:
        return None
    max_resolution = max(resolutions, key=lambda res: int(res.split('x')[1]))
    return max_resolution.split('x')[1] + 'p'

# Function to select the first HLS manifest that works for the active route
def get_working_m3u8_url(media_selector_data):
    last_error = None
    best_url = None
    best_resolution = None
    for m3u8_url in get_m3u8_urls(media_selector_data):
        try:
            response = session.get(m3u8_url, timeout=30)
            max_resolution = get_max_resolution(response.text)
            if response.status_code == 200 and max_resolution:
                if not best_resolution or int(max_resolution.rstrip('p')) > int(best_resolution.rstrip('p')):
                    best_url = m3u8_url
                    best_resolution = max_resolution
            last_error = f"{response.status_code} from {m3u8_url}"
        except requests.RequestException as exc:
            last_error = f"{exc} from {m3u8_url}"

    if best_url:
        return best_url, best_resolution

    if last_error:
        print(f"{bcolors.FAIL}No usable M3U8 playlist found. Last error: {last_error}{bcolors.ENDC}")
    return None, None


def get_uhd_manifest_url(media_selector_data):
    candidates = []
    for media in media_selector_data.get('media', []):
        if media.get('kind') != 'video':
            continue
        height = int(media.get('height', 0)) if str(media.get('height', '')).isdigit() else 0
        encoding = str(media.get('encoding') or '').lower()
        for connection in media.get('connection', []):
            if connection.get('transferFormat') != 'dash' or not connection.get('href'):
                continue
            candidates.append((height, encoding, int(connection.get('priority', 99)), connection['href']))

    if not candidates:
        return None, None

    candidates.sort(key=lambda item: (-item[0], item[2]))
    for height, encoding, _, manifest_url in candidates:
        try:
            response = session.get(manifest_url, timeout=30)
            if response.status_code == 200 and b'<MPD' in response.content[:1000]:
                return manifest_url, f"{height}p" if height else "2160p"
        except requests.RequestException:
            continue

    return None, None


def get_available_version_ids(video_id, metadata):
    version_ids = []
    playlist_data = get_playlist_data(video_id) or {}
    for version in playlist_data.get('allAvailableVersions') or []:
        vpid = version.get('pid')
        if vpid and vpid not in version_ids:
            version_ids.append(vpid)

    try:
        metadata_versions = metadata['episodes'][0].get('versions') or []
    except (KeyError, IndexError, TypeError):
        metadata_versions = []

    for version in metadata_versions:
        vpid = version.get('id')
        if vpid and vpid not in version_ids:
            version_ids.append(vpid)

    return version_ids

# Function to extract series and episode information from subtitle
def extract_series_episode_from_subtitle(subtitle):
    match = re.search(r'Series (\d+):?\s?(Episode\s)?(\d+)?', subtitle)
    if match:
        series = match.group(1).zfill(2)
        episode = match.group(3).zfill(2) if match.group(3) else None
        return series, episode
    return None, None

def format_episode_label(episode):
    title = episode.get('title', 'Unknown')
    subtitle = episode.get('subtitle', '')
    series_info, episode_info = extract_series_episode_from_subtitle(subtitle)
    season_episode = f"S{series_info}E{episode_info}" if series_info and episode_info else ""
    label_parts = [part for part in [title, season_episode, subtitle] if part]
    return " - ".join(label_parts)

def episode_sort_key(item):
    episode = item['episode']
    series_info, episode_info = extract_series_episode_from_subtitle(episode.get('subtitle', ''))
    return (
        int(series_info) if series_info else 9999,
        int(episode_info) if episode_info else 9999,
        item['id'],
    )

def episode_tree_label(episode):
    subtitle = episode.get('subtitle', '')
    series_info, episode_info = extract_series_episode_from_subtitle(subtitle)
    if series_info and episode_info:
        episode_title = re.sub(r'^Series\s+\d+:?\s*', '', subtitle).strip()
        return episode_info, episode_title or subtitle
    if series_info:
        return "-", subtitle
    return "-", subtitle or episode.get('title', 'Unknown')

def group_episode_items_by_series(episode_items):
    grouped = {}
    for item in sorted(episode_items, key=episode_sort_key):
        episode = item['episode']
        series_info, _ = extract_series_episode_from_subtitle(episode.get('subtitle', ''))
        series_label = f"Series {int(series_info)}" if series_info else "Episodes"
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

def episode_series_number(item):
    series_info, _ = extract_series_episode_from_subtitle(item['episode'].get('subtitle', ''))
    return int(series_info) if series_info else None

def episode_number(item):
    _, episode_info = extract_series_episode_from_subtitle(item['episode'].get('subtitle', ''))
    return int(episode_info) if episode_info else None

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
        raise ValueError(f"No BBC episodes found for selector {format_download_selector(parsed_selector)}.")

    selected.sort(key=episode_sort_key)
    warn_if_partial_range_match(parsed_selector, selected)
    return selected

def print_download_queue(episode_items):
    print()
    print(f"{bcolors.LIGHTBLUE}Download queue:{bcolors.ENDC}")
    for item in episode_items:
        season = episode_series_number(item)
        episode = episode_number(item)
        if season is None or episode is None:
            selector = item['id']
        else:
            selector = format_queue_selector(season, episode)
        _, title = episode_tree_label(item['episode'])
        print(f"{selector} {title}")

def list_episode_items(episode_items):
    if not episode_items:
        print(f"{icons.ICON_FAILURE} {bcolors.FAIL}No BBC episodes found.{bcolors.ENDC}")
        return

    show_title = episode_items[0]['episode'].get('title', 'BBC')
    grouped_items = group_episode_items_by_series(episode_items)
    group_labels = sorted(grouped_items, key=series_group_sort_key)
    series_summary = ",  ".join(f"{label}({len(grouped_items[label])})" for label in group_labels)

    print(f"{icons.ICON_SUCCESS} {bcolors.OKGREEN}Found {len(episode_items)} BBC episodes{bcolors.ENDC}")
    print()
    print_series_rule("BBC Series", show_title)
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
            episode_number, episode_title = episode_tree_label(item['episode'])

            print(f"{bcolors.GRAY}{group_child_prefix}{branch} {episode_number}. {bcolors.ENDC}{episode_title}")
            print(f"{bcolors.GRAY}{group_child_prefix}{url_branch} {bcolors.ENDC}{bcolors.LIGHTBLUE}{item['url']}{bcolors.ENDC}")

def export_episode_urls(episode_items):
    """Write listed BBC episode URLs to Eurovine's shared export directory."""
    export_dir = Path(__file__).resolve().parents[2] / "export"
    export_dir.mkdir(parents=True, exist_ok=True)
    title = episode_items[0]['episode'].get('title', 'bbc') if episode_items else 'bbc'
    safe_title = re.sub(r"[^A-Za-z0-9._-]+", "_", title).strip("._") or "bbc"
    output_path = export_dir / f"{safe_title}_episodes.txt"
    output_path.write_text("\n".join(item['url'] for item in episode_items) + "\n", encoding="utf-8")
    print(f"{icons.ICON_SUCCESS} {bcolors.OKGREEN}Exported list: {output_path}{bcolors.ENDC}")

# Function to capitalize each word in the string
def capitalize_words(s):
    return '.'.join(word.capitalize() for word in s.split('.'))

# Function to format filename
def format_filename(title, series_info=None, episode_info=None, max_resolution="1080p", ultra=False):
    title = capitalize_words(title.replace(' ', '.'))
    series = f"S{series_info}" if series_info else ""
    episode = f"E{episode_info}" if episode_info else ""
    codec = "H.265" if ultra else "H.264"
    range_tag = "HLG" if ultra else None
    parts = filter(None, [title, series + episode, max_resolution, "iP", "WEB-DL", "AAC2.0", codec, range_tag])
    formatted_file_name = '.'.join(parts)
    return formatted_file_name


def clean_info_value(value):
    if value in (None, '', 'Not Available'):
        return ''
    return re.sub(r'\s+', ' ', str(value)).strip()


def format_info_date(value):
    if not value:
        return ''
    try:
        parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return f"{parsed.day} {parsed.strftime('%B %Y')}"
    except (TypeError, ValueError):
        return clean_info_value(value)


def print_bbc_metadata(metadata):
    episode = get_episode_from_metadata(metadata) or {}
    synopses = episode.get('synopses') or {}
    rows = [
        ('Show', clean_info_value(episode.get('title'))),
        ('Episode', clean_info_value(episode.get('subtitle'))),
        ('Date Aired', format_info_date(episode.get('release_date_time'))),
        ('Description', clean_info_value(
            synopses.get('large') or synopses.get('medium') or synopses.get('small')
            or episode.get('description')
        )),
    ]
    rows = [(label, value) for label, value in rows if value]
    if not rows:
        return

    print(f"\n{bcolors.YELLOW}Episode metadata:{bcolors.ENDC}")
    for label, value in rows:
        print(f"{bcolors.LIGHTBLUE}{label}: {bcolors.ENDC}{value}")


def get_hls_streams(manifest_content):
    streams = []
    pending = None
    for line in manifest_content.splitlines():
        line = line.strip()
        if not line.startswith('#EXT-X-STREAM-INF:'):
            if pending and line and not line.startswith('#'):
                streams.append(pending)
                pending = None
            continue
        attributes = line.split(':', 1)[1]
        resolution_match = re.search(r'RESOLUTION=(\d+x\d+)', attributes)
        bandwidth_match = re.search(r'BANDWIDTH=(\d+)', attributes)
        codecs_match = re.search(r'CODECS="([^"]+)"', attributes)
        pending = {
            'type': 'video',
            'resolution': resolution_match.group(1) if resolution_match else '',
            'bandwidth': int(bandwidth_match.group(1)) if bandwidth_match else 0,
            'codecs': codecs_match.group(1) if codecs_match else '',
            'language': '',
        }

    return sorted(streams, key=lambda item: item['bandwidth'], reverse=True)


def get_dash_streams(manifest_content):
    root = etree.fromstring(manifest_content)
    streams = []
    for representation in root.xpath("//*[local-name()='Representation']"):
        adaptation = representation.getparent()
        mime_type = adaptation.get('mimeType', '')
        content_type = adaptation.get('contentType', '')
        if 'video' in mime_type or content_type == 'video':
            stream_type = 'video'
        elif 'audio' in mime_type or content_type == 'audio':
            stream_type = 'audio'
        elif 'text' in mime_type or 'ttml' in mime_type or content_type == 'text':
            stream_type = 'subtitle'
        else:
            stream_type = 'stream'

        width = representation.get('width')
        height = representation.get('height')
        bandwidth = representation.get('bandwidth')
        streams.append({
            'type': stream_type,
            'resolution': f"{width}x{height}" if width and height else '',
            'bandwidth': int(bandwidth) if str(bandwidth or '').isdigit() else 0,
            'codecs': representation.get('codecs') or adaptation.get('codecs', ''),
            'language': adaptation.get('lang', ''),
        })

    return sorted(streams, key=lambda item: (item['type'] != 'video', -item['bandwidth']))


def print_streams(streams):
    if not streams:
        print(f"\n{bcolors.WARNING}No stream variants found.{bcolors.ENDC}")
        return

    print(f"\n{bcolors.YELLOW}Available streams:{bcolors.ENDC}")
    codec_width = max(28, max(len(stream.get('codecs') or 'unknown codecs') for stream in streams) + 2)
    header = f"  {'#':>2}  {'Type':<4} {'Resolution':<10} {'Bitrate':<16} {'Codec':<{codec_width}} {'Lang':<5}"
    divider = f"  {'-' * 2}  {'-' * 4} {'-' * 10} {'-' * 16} {'-' * codec_width} {'-' * 5}"
    print(header)
    print(divider)

    for index, stream in enumerate(streams, start=1):
        kbps = round(stream.get('bandwidth', 0) / 1000)
        bitrate = f"{kbps} Kbps" if kbps else 'unknown bitrate'
        codecs = stream.get('codecs') or 'unknown codecs'
        stream_type = stream.get('type', 'stream')
        if stream_type == 'video':
            label = 'Vid'
            resolution = stream.get('resolution') or '-'
        elif stream_type == 'audio':
            label = 'Aud'
            resolution = '-'
        elif stream_type == 'subtitle':
            label = 'Sub'
            resolution = '-'
        else:
            label = 'Stream'
            resolution = stream.get('resolution') or '-'
        language = stream.get('language') or '-'
        print(f"  {index:>2}  {label:<4} {resolution:<10} {bitrate:<16} {codecs:<{codec_width}} {language:<5}")


def display_info(manifest_url, formatted_file_name, metadata, ultra=False):
    manifest_label = 'MPD URL' if ultra else 'M3U8 URL'
    print(f"{bcolors.LIGHTBLUE}{manifest_label}: {bcolors.ENDC}{manifest_url}")
    try:
        response = session.get(manifest_url, timeout=30)
        response.raise_for_status()
        if ultra:
            print_streams(get_dash_streams(response.content))
        else:
            print_streams(get_hls_streams(response.text))
    except (requests.RequestException, etree.XMLSyntaxError, ValueError) as exc:
        print(f"{bcolors.WARNING}{icons.ICON_WARNING} Could not inspect manifest streams: {exc}{bcolors.ENDC}")

    print_bbc_metadata(metadata)
    print(f"\n{bcolors.YELLOW}Suggested filename: {bcolors.ENDC}{formatted_file_name}.mkv")

# Function to extract and print M3U8 URL
def extract_info(video_url, ultra=False):
    video_id = extract_video_id(video_url)
    if not video_id:
        print(f"{bcolors.FAIL}Failed to extract video ID from the URL.{bcolors.ENDC}")
        return None, None, None

    metadata = get_video_metadata(video_id)
    if not metadata:
        return None, None, None
    
    # print(f"{bcolors.YELLOW}Metadata: {metadata}{bcolors.ENDC}") # for debugging only

    version_ids = get_available_version_ids(video_id, metadata)
    if not version_ids:
        print(f"{bcolors.FAIL}Failed to extract a BBC version ID.{bcolors.ENDC}")
        return None, None, None

    manifest_url = None
    max_resolution = None
    for vpid in version_ids:
        media_selector_data = get_media_selector_data(vpid, ultra=ultra, show_error=False)
        if not media_selector_data:
            continue
        if ultra:
            manifest_url, max_resolution = get_uhd_manifest_url(media_selector_data)
        else:
            manifest_url, max_resolution = get_working_m3u8_url(media_selector_data)
        if manifest_url:
            break

    if not manifest_url:
        quality_label = 'UHD DASH' if ultra else 'HLS'
        print(f"{bcolors.FAIL}No {quality_label} manifest was found for the available versions.{bcolors.ENDC}")
        return None, None, metadata

    title = metadata['episodes'][0]['title']
    subtitle = metadata['episodes'][0].get('subtitle', '')

    # Extract series and episode information from subtitle
    series_info, episode_info = extract_series_episode_from_subtitle(subtitle)

    if series_info and episode_info:
        formatted_file_name = format_filename(title, series_info, episode_info, max_resolution, ultra=ultra)
    elif series_info:
        formatted_file_name = format_filename(title, series_info, None, max_resolution, ultra=ultra)
    else:
        # Use the full title from the URL when series and episode information is not available
        url_title = video_url.split('/')[-1].replace('-', ' ')
        formatted_file_name = format_filename(url_title, None, None, max_resolution, ultra=ultra)

    return manifest_url, formatted_file_name, metadata

# Function to format and display download command
def build_download_command(m3u8_url, formatted_file_name, downloads_path, interactive=False, quality=None):
    selectors = "" if interactive else f"{video_selector(quality)} --select-audio best --select-subtitle all "
    command = f'N_m3u8DL-RE "{m3u8_url}" {selectors}-mt -M format=mkv:muxer=mkvmerge --save-dir "{downloads_path}" --save-name "{formatted_file_name}" '
    return append_downloader_proxy(command)

def display_download_command(m3u8_url, formatted_file_name, downloads_path, auto_download=False, ultra=False, interactive=False, quality=None):
    quality = None if ultra else quality
    formatted_file_name = apply_quality_to_filename(formatted_file_name, quality)
    download_command = build_download_command(m3u8_url, formatted_file_name, downloads_path, interactive=interactive, quality=quality)
    manifest_label = 'MPD URL' if ultra else 'M3U8 URL'
    print(f"{bcolors.LIGHTBLUE}{manifest_label}: {bcolors.ENDC}{m3u8_url}")
    print(f"{bcolors.YELLOW}DOWNLOAD COMMAND: {bcolors.ENDC}")
    print(mask_proxy_command(download_command))

    if auto_download:
        print(f"{icons.ICON_WAITING} {bcolors.OKBLUE}Downloading video...{bcolors.ENDC}")
        subprocess.run(download_command, shell=True)
        return

    user_input = input("Do you wish to download? Y or N: ").strip().lower()
    if user_input == 'y':
        print(f"{icons.ICON_WAITING} {bcolors.OKBLUE}Downloading video...{bcolors.ENDC}")
        subprocess.run(download_command, shell=True)
    else:
        print(f"{icons.ICON_FAILURE} {bcolors.RED}Download cancelled{bcolors.ENDC}")

def process_video(video_url, downloads_path, auto_download=False, info=False, ultra=False, interactive=False, quality=None):
    print(f"{icons.ICON_INFO} {bcolors.LIGHTBLUE}Processing: {bcolors.ENDC}{video_url}")
    spinner = Spinner()
    spinner.start()
    try:
        manifest_url, formatted_file_name, metadata = extract_info(video_url, ultra=ultra)
    except Exception:
        spinner.stop()
        raise
    spinner.stop()
    if not manifest_url:
        return False

    if info:
        display_info(manifest_url, formatted_file_name, metadata, ultra=ultra)
        return True

    display_download_command(
        manifest_url,
        formatted_file_name,
        downloads_path,
        auto_download=auto_download,
        ultra=ultra,
        interactive=interactive,
        quality=quality,
    )
    return True

def download_selected_episodes(series_url, selector, downloads_path, ultra=False, quality=None):
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
        _, title = episode_tree_label(item['episode'])
        print(f"\n{icons.ICON_INFO} {bcolors.LIGHTBLUE}Downloading {index}/{len(episode_items)}: {title}{bcolors.ENDC}")
        process_video(item['url'], downloads_path, auto_download=True, ultra=ultra, quality=quality)

def is_episode_url(url):
    return extract_video_id(url) is not None

def main(video_url, downloads_path, wvd_device_path, certificate_path=None, mode="auto", export_list=False, download_selector=None, ultra=False, quality=None):
    """Eurovine entry point for BBC iPlayer; UHD requires a configured certificate."""
    if not video_url:
        raise ValueError("No BBC iPlayer URL provided.")
    if not downloads_path:
        raise ValueError("Eurovine config requires downloads_path for BBC.")
    configure_service(certificate_path)
    video_url = video_url.strip()

    if mode == "list":
        if ultra:
            print(f"{icons.ICON_FAILURE} {bcolors.FAIL}--ultra is used with an episode download, episode --info, or series --download selector.{bcolors.ENDC}")
            return
        try:
            if is_episode_url(video_url):
                video_id = extract_video_id(video_url)
                metadata = get_video_metadata(video_id)
                episode = get_episode_from_metadata(metadata)
                if not episode:
                    raise ValueError("Could not read BBC episode metadata.")
                episode_items = [{'url': clean_url(video_url), 'id': video_id, 'metadata': metadata, 'episode': episode}]
            else:
                episode_items = collect_episode_items(video_url, show_progress=False)
            list_episode_items(episode_items)
            if export_list:
                export_episode_urls(episode_items)
        except ValueError as exc:
            print(f"{icons.ICON_FAILURE} {bcolors.FAIL}{exc}{bcolors.ENDC}")
        return

    if mode == "download":
        if is_episode_url(video_url):
            print(f"{icons.ICON_FAILURE} {bcolors.FAIL}Download selector mode requires a BBC series URL, not an episode URL.{bcolors.ENDC}")
            return
        try:
            if ultra:
                get_bbc_certificate_path()
            download_selected_episodes(video_url, download_selector, downloads_path, ultra=ultra, quality=None if ultra else quality)
        except ValueError as exc:
            print(f"{icons.ICON_FAILURE} {bcolors.FAIL}{exc}{bcolors.ENDC}")
        return

    if mode == "info":
        if not is_episode_url(video_url):
            print(f"{icons.ICON_FAILURE} {bcolors.FAIL}Info mode requires a BBC episode URL, not a series URL.{bcolors.ENDC}")
            return
        try:
            if ultra:
                get_bbc_certificate_path()
            process_video(video_url, downloads_path, info=True, ultra=ultra)
        except ValueError as exc:
            print(f"{icons.ICON_FAILURE} {bcolors.FAIL}{exc}{bcolors.ENDC}")
        return

    if is_episode_url(video_url):
        try:
            if ultra:
                get_bbc_certificate_path()
            process_video(video_url, downloads_path, ultra=ultra, interactive=(mode == "interactive"), quality=None if ultra else quality)
        except ValueError as exc:
            print(f"{icons.ICON_FAILURE} {bcolors.FAIL}{exc}{bcolors.ENDC}")
        return

    print(f"{icons.ICON_WARNING} {bcolors.WARNING}Series URLs require a flag. Use --list/-l to list episodes or --download/-d SELECTOR to download selected episodes.{bcolors.ENDC}")

# Example usage
if __name__ == "__main__":
    print("Run BBC iPlayer through eurovine.py so it can use the shared Eurovine configuration.")
