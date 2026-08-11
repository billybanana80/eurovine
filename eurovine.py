import sys
import importlib
import argparse
import shutil
import yaml
from rich.console import Console
from rich.padding import Padding
from rich.text import Text
from datetime import datetime
from pathlib import Path
from colors import bcolors
from proxy_config import configure_proxy
import icons

for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

#   Eurovine: Downloader for European FTA services
#   Author: billybanana
#   Quality: up to 1080p, service dependent
#   Geo: Respective European country IP address required, service dependent
#
#   Supports:
#   - Single episode/video downloads
#   - Episode info and download command preview modes
#   - Series listing, export, and selector-based downloads
#   - Encrypted and non-encrypted streams
#   - Surfshark and NordVPN proxy profiles
#
#   Full usage details and examples are in README.md.

console = Console()
__version__ = "1.0"  # Replace with the actual version
SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = SCRIPT_DIR / "config.yaml"
TEMP_DIR = SCRIPT_DIR / "temp"

def print_ascii_art(version=None):
    ascii_art = Text(
        r"                                           " + "\n"
        r"  ___ _   _ _ __ ___ __   __(_)_ __   __ " + "\n"
        r" / _ \ | | | '__/ _ \\ \ / /| | '_ \ / _ \ " + "\n"
        r"|  __/ |_| | | | (_) |\ V / | | | | |  __/" + "\n"
        r" \___\\__,_|_|  \___/  \_/  |_|_| |_|\___\ " + "\n"
        r"                                            ",
    )

    version_info = Text(f"Version {__version__} Copyright © {datetime.now().year} billybanana", style="none")
    github_link = Text("https://github.com/billybanana80/eurovine", style="bright_blue")

    combined_text = ascii_art + Text("\n") + version_info + Text("\n") + github_link
    padded_art = Padding(combined_text, (1, 21, 1, 20), expand=True)

    console.print(padded_art, justify="left")

    if version:
        return
    
def load_config():
    with open(CONFIG_PATH, 'r', encoding="utf-8") as file:
        return yaml.safe_load(file) or {}

def line_indent(line):
    return len(line) - len(line.lstrip(" "))

def remove_nested_yaml_key(lines, parent_key, child_key):
    removed = False
    parent_index = None
    parent_indent = 0
    for index, line in enumerate(lines):
        if line.strip() == f"{parent_key}:":
            parent_index = index
            parent_indent = line_indent(line)
            break
    if parent_index is None:
        return lines, removed

    index = parent_index + 1
    while index < len(lines):
        line = lines[index]
        stripped = line.strip()
        indent = line_indent(line)
        if stripped and indent <= parent_indent:
            break
        if stripped.startswith(f"{child_key}:") and indent == parent_indent + 2:
            end = index + 1
            while end < len(lines):
                end_line = lines[end]
                end_stripped = end_line.strip()
                end_indent = line_indent(end_line)
                if end_stripped and end_indent <= indent:
                    break
                end += 1
            del lines[index:end]
            removed = True
            continue
        index += 1
    return lines, removed

def normalize_empty_yaml_section(lines, parent_key):
    for index, line in enumerate(lines):
        if line.strip() != f"{parent_key}:":
            continue

        parent_indent = line_indent(line)
        next_index = index + 1
        has_nested_lines = False

        while next_index < len(lines):
            candidate = lines[next_index]
            stripped = candidate.strip()
            if not stripped:
                next_index += 1
                continue
            if line_indent(candidate) <= parent_indent:
                break
            has_nested_lines = True
            break

        if not has_nested_lines:
            newline = "\r\n" if line.endswith("\r\n") else "\n" if line.endswith("\n") else ""
            lines[index] = f"{' ' * parent_indent}{parent_key}: {{}}{newline}"

    return lines

def clear_config_token_cache():
    if not CONFIG_PATH.exists():
        return []

    lines = CONFIG_PATH.read_text(encoding="utf-8").splitlines(keepends=True)
    removed_items = []

    for parent_key, child_keys in {
        "tf1": ["cache"],
        "tv4": ["cache", "access_token", "token", "bearer_token", "refresh_token", "tv4_refresh_token"],
    }.items():
        for child_key in child_keys:
            lines, removed = remove_nested_yaml_key(lines, parent_key, child_key)
            if removed:
                removed_items.append(f"{parent_key}.{child_key}")

    if removed_items:
        for parent_key in ("tf1", "tv4"):
            lines = normalize_empty_yaml_section(lines, parent_key)
        CONFIG_PATH.write_text("".join(lines), encoding="utf-8")

    return removed_items

def clear_temp_folder():
    TEMP_DIR.mkdir(parents=True, exist_ok=True)
    resolved_temp = TEMP_DIR.resolve()
    resolved_root = SCRIPT_DIR.resolve()
    if resolved_temp.parent != resolved_root or resolved_temp.name.lower() != "temp":
        raise RuntimeError(f"Refusing to clear unexpected temp folder: {TEMP_DIR}")

    removed_count = 0
    for item in TEMP_DIR.iterdir():
        if item.is_dir() and not item.is_symlink():
            shutil.rmtree(item)
        else:
            item.unlink()
        removed_count += 1
    return removed_count

def clear_project_cache():
    removed_config_items = clear_config_token_cache()
    removed_temp_items = clear_temp_folder()

    if removed_config_items:
        print(f"{icons.ICON_SUCCESS} {bcolors.OKGREEN}Cleared config cache entries: {', '.join(removed_config_items)}{bcolors.ENDC}")
    else:
        print(f"{icons.ICON_INFO} {bcolors.LIGHTBLUE}No cached token entries found in config.yaml{bcolors.ENDC}")

    print(f"{icons.ICON_SUCCESS} {bcolors.OKGREEN}Cleared {removed_temp_items} item(s) from {TEMP_DIR}{bcolors.ENDC}")

def parse_args():
    parser = argparse.ArgumentParser(description="Eurovine downloader")
    parser.add_argument("video_url", nargs="?", help="Episode URL to download, show URL with --list/-l or --download/-d, or service shortcut such as 'bbc -u'")
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument("--info", "-i", action="store_true", help="Show available formats without downloading")
    mode_group.add_argument("--action", "-a", action="store_true", help="Let N_m3u8DL-RE prompt for stream choices")
    mode_group.add_argument("--list", "-l", action="store_true", help="List available episodes for a show URL")
    mode_group.add_argument("--download", "-d", metavar="SELECTOR", help="Download from a show URL using sXXeXX, sXXXXeXX, sXX, or sXXXX")
    parser.add_argument("--export", "-x", action="store_true", help="Export list-mode episode URLs to a text file")
    parser.add_argument("--ultra", "-u", action="store_true", help="With a BBC URL, request UHD streams; with 'bbc', list the BBC UHD catalogue")
    parser.add_argument("--clear-cache", "-c", action="store_true", help="Clear cached service tokens from config.yaml and remove files from temp/")
    return parser.parse_args()

def parse_prompt_input(value, mode, export_list=False, download_selector=None, ultra=False):
    parts = value.strip().split()
    if not parts:
        return "", mode, export_list, download_selector, ultra

    detected_modes = []
    url_parts = []
    index = 0
    while index < len(parts):
        part = parts[index]
        if part in {"--info", "-i"}:
            detected_modes.append("info")
        elif part in {"--action", "-a"}:
            detected_modes.append("interactive")
        elif part in {"--list", "-l"}:
            detected_modes.append("list")
        elif part in {"--download", "-d"}:
            detected_modes.append("download")
            if index + 1 >= len(parts):
                raise ValueError("Download mode requires a selector such as s01e01, s01, or s01e01-s02e02.")
            index += 1
            download_selector = parts[index]
        elif part in {"--export", "-x"}:
            export_list = True
        elif part in {"--ultra", "-u"}:
            ultra = True
        else:
            url_parts.append(part)
        index += 1

    if len(set(detected_modes)) > 1:
        raise ValueError("Use only one of --info/-i, --action/-a, --list/-l, or --download/-d.")

    if detected_modes:
        mode = detected_modes[-1]

    return " ".join(url_parts).strip(), mode, export_list, download_selector, ultra

def input_label_for_mode(mode):
    return "Series URL" if mode in {"list", "download"} else "Episode URL"

def main():
    print_ascii_art(version=__version__)  # Display the ASCII art and version info
    parsed_args = parse_args()
    if parsed_args.clear_cache:
        clear_project_cache()
        return

    mode = "auto"
    if parsed_args.info:
        mode = "info"
    elif parsed_args.action:
        mode = "interactive"
    elif parsed_args.list:
        mode = "list"
    elif parsed_args.download:
        mode = "download"
    export_list = parsed_args.export
    download_selector = parsed_args.download
    ultra = parsed_args.ultra

    config = load_config()
    downloads_path = config.get('downloads_path')
    wvd_device_path = config.get('wvd_device_path')
    prd_device_path = config.get('prd_device_path')
    cookies_path = config.get('cookies_path')
    credentials = config.get('credentials', {})

    # Check if a URL is provided as a command-line argument
    if parsed_args.video_url:
        video_url = parsed_args.video_url.strip()
    else:
        # Prompt user for manual input if no command-line argument is given
        prompt_value = input(f"{bcolors.LIGHTBLUE}Enter URL with optional flags: {bcolors.ENDC}").strip()
        video_url, mode, export_list, download_selector, ultra = parse_prompt_input(prompt_value, mode, export_list, download_selector, ultra)

    if video_url.casefold() in {"bbc", "bbc_uhd", "bbc-uhd", "iplayer"} and ultra:
        print(f"{bcolors.LIGHTBLUE}Service: {bcolors.ENDC}BBC UHD Catalogue")
    else:
        print(f"{bcolors.LIGHTBLUE}{input_label_for_mode(mode)}: {bcolors.ENDC}{video_url}")

    if video_url.casefold() in {"bbc", "bbc_uhd", "bbc-uhd", "iplayer"}:
        if not ultra:
            print(f"{bcolors.YELLOW}{icons.ICON_FAILURE} The BBC service shortcut is currently used for the UHD catalogue only. Use: bbc -u{bcolors.ENDC}")
            sys.exit(1)
        service_key = "bbc"
        service_module = "services.bbc.bbc_uhd"
        print(f"{bcolors.LIGHTBLUE}{icons.ICON_WAITING} Eurovine..........initiating BBC UHD Catalogue{bcolors.ENDC}")
        args = (config,)
    elif video_url.startswith("https://www.channel4.com"):
        service_key = "all4"
        service_module = "services.all4.all4"
        print(f"{bcolors.LIGHTBLUE}{icons.ICON_WAITING} Eurovine..........initiating All4{bcolors.ENDC}")
        args = (video_url, downloads_path, wvd_device_path, mode, export_list, download_selector)
    elif video_url.startswith("https://www.bbc.co.uk"):
        service_key = "bbc"
        service_module = "services.bbc.bbc"
        print(f"{bcolors.LIGHTBLUE}{icons.ICON_WAITING} Eurovine..........initiating BBC{bcolors.ENDC}")
        args = (video_url, downloads_path, wvd_device_path, (config.get("bbc") or {}).get("certificate"), mode, export_list, download_selector, ultra)
    elif video_url.startswith("https://www.dr.dk/drtv"):
        service_key = "drtv"
        service_module = "services.drtv.drtv"
        print(f"{bcolors.LIGHTBLUE}{icons.ICON_WAITING} Eurovine..........initiating DRTV{bcolors.ENDC}")
        args = (video_url, downloads_path, wvd_device_path, mode, export_list, download_selector)
    elif video_url.startswith("https://www.france.tv"):
        service_key = "frtv"
        service_module = "services.frtv.frtv"
        print(f"{bcolors.LIGHTBLUE}{icons.ICON_WAITING} Eurovine..........initiating France TV{bcolors.ENDC}")
        args = (video_url, downloads_path, wvd_device_path, mode, export_list, download_selector) 
    elif video_url.startswith("https://www.itv.com"):
        service_key = "itvx"
        service_module = "services.itvx.itvx"
        print(f"{bcolors.LIGHTBLUE}{icons.ICON_WAITING} Eurovine..........initiating ITVX{bcolors.ENDC}")
        args = (video_url, downloads_path, wvd_device_path, mode, export_list, download_selector)                        
    elif video_url.startswith("https://www.m6.fr"):
        service_key = "m6"
        service_module = "services.m6.m6"
        print(f"{bcolors.LIGHTBLUE}{icons.ICON_WAITING} Eurovine..........initiating M6{bcolors.ENDC}")
        args = (video_url, downloads_path, wvd_device_path, prd_device_path, credentials.get("m6"), config.get("m6"), mode, export_list, download_selector) 
    elif video_url.startswith("https://www.channel5.com"):
        service_key = "my5"
        service_module = "services.my5.my5"
        print(f"{bcolors.LIGHTBLUE}{icons.ICON_WAITING} Eurovine..........initiating My5{bcolors.ENDC}")
        args = (video_url, downloads_path, wvd_device_path, (config.get("my5") or {}).get("certificate"), mode, export_list, download_selector)
    elif video_url.startswith("https://tv.nrk.no"):
        service_key = "nrk"
        service_module = "services.nrk.nrk"
        print(f"{bcolors.LIGHTBLUE}{icons.ICON_WAITING} Eurovine..........initiating NRK{bcolors.ENDC}")
        args = (video_url, downloads_path, wvd_device_path, mode, export_list, download_selector) 
    elif video_url.startswith("https://www.rte.ie"):
        service_key = "rte"
        service_module = "services.rte.rte"
        print(f"{bcolors.LIGHTBLUE}{icons.ICON_WAITING} Eurovine..........initiating RTE{bcolors.ENDC}")
        args = (video_url, downloads_path, wvd_device_path, mode, export_list, download_selector) 
    elif video_url.startswith("https://www.ruv.is"):
        service_key = "ruv"
        service_module = "services.ruv.ruv"
        print(f"{bcolors.LIGHTBLUE}{icons.ICON_WAITING} Eurovine..........initiating RUV{bcolors.ENDC}")
        args = (video_url, downloads_path, wvd_device_path, mode, export_list, download_selector)   
    elif video_url.startswith("https://player.stv.tv"):
        service_key = "stv"
        service_module = "services.stv.stv"
        print(f"{bcolors.LIGHTBLUE}{icons.ICON_WAITING} Eurovine..........initiating STV{bcolors.ENDC}")
        args = (video_url, downloads_path, wvd_device_path, mode, export_list, download_selector) 
    elif video_url.startswith("https://u.co.uk"):
        service_key = "u"
        service_module = "services.u.u"
        print(f"{bcolors.LIGHTBLUE}{icons.ICON_WAITING} Eurovine..........initiating U{bcolors.ENDC}")
        args = (video_url, downloads_path, wvd_device_path, mode, export_list, download_selector)  
    elif video_url.startswith("https://play.virginmediatelevision.ie"):
        service_key = "vm"
        service_module = "services.vm.vm"
        print(f"{bcolors.LIGHTBLUE}{icons.ICON_WAITING} Eurovine..........initiating Virgin Media{bcolors.ENDC}")
        args = (video_url, downloads_path, wvd_device_path, config.get("vmplayer_device_id"), mode, export_list, download_selector)                                                   
    elif video_url.startswith("https://www.tf1.fr"):
        service_key = "tf1"
        service_module = "services.tf1.tf1"
        print(f"{bcolors.LIGHTBLUE}{icons.ICON_WAITING} Eurovine..........initiating TF1{bcolors.ENDC}")
        args = (video_url, downloads_path, wvd_device_path, credentials.get("tf1"), config.get("tf1"), mode, export_list, download_selector)   
    elif video_url.startswith("https://npo.nl"):
        service_key = "npo"
        service_module = "services.npo.npo"
        print(f"{bcolors.LIGHTBLUE}{icons.ICON_WAITING} Eurovine..........initiating NPO{bcolors.ENDC}")
        args = (video_url, downloads_path, wvd_device_path, mode, export_list, download_selector) 
    elif video_url.startswith("https://www.tv4play.se"):
        service_key = "tv4"
        service_module = "services.tv4.tv4"
        print(f"{bcolors.LIGHTBLUE}{icons.ICON_WAITING} Eurovine..........initiating TV4{bcolors.ENDC}")
        args = (video_url, downloads_path, wvd_device_path, cookies_path, credentials.get("tv4"), config.get("tv4"), mode, export_list, download_selector)                       
    elif video_url.startswith("https://www.svtplay.se"):
        service_key = "svt"
        service_module = "services.svt.svt"
        print(f"{bcolors.LIGHTBLUE}{icons.ICON_WAITING} Eurovine..........initiating SVT{bcolors.ENDC}")
        args = (video_url, downloads_path, wvd_device_path, mode, export_list, download_selector)                     
    else:
        print(f"{bcolors.RED}{icons.ICON_FAILURE} Unsupported URL. Please enter a valid video URL from All4, BBC, DRTV, France TV, ITVX, M6, My5, NPO, NRK, RTE, RUV, STV, SVT, TF1, TV4, U or Virgin Media, or use bbc -u for the BBC UHD catalogue.{bcolors.ENDC}")
        sys.exit(1)

    try:
        if service_module == "services.bbc.bbc_uhd" and (mode != "auto" or export_list or download_selector):
            print(f"{bcolors.YELLOW}{icons.ICON_FAILURE} BBC UHD catalogue mode only uses the -u flag. Use: bbc -u{bcolors.ENDC}")
            sys.exit(1)
        if export_list and mode != "list":
            print(f"{bcolors.YELLOW}{icons.ICON_FAILURE} Export mode is only available with --list/-l.{bcolors.ENDC}")
            sys.exit(1)
        if ultra and service_key != "bbc":
            print(f"{bcolors.YELLOW}{icons.ICON_FAILURE} UHD mode (--ultra/-u) is currently available for BBC only.{bcolors.ENDC}")
            sys.exit(1)
        if mode == "download" and service_key not in {"all4", "bbc", "drtv", "frtv", "itvx", "m6", "my5", "npo", "nrk", "rte", "ruv", "stv", "svt", "tf1", "tv4", "u", "vm"}:
            print(f"{bcolors.YELLOW}{icons.ICON_FAILURE} Download selector mode is currently implemented for All4, BBC, DRTV, France TV, ITVX, M6, My5, NPO, NRK, RTE, RUV, STV, SVT, TF1, TV4, U and Virgin Media only.{bcolors.ENDC}")
            sys.exit(1)
        if mode == "download" and not download_selector:
            print(f"{bcolors.YELLOW}{icons.ICON_FAILURE} Download mode requires a selector such as s01e01, s2026e01, s01, s2026, s01e01-s02e02, or s01-s03.{bcolors.ENDC}")
            sys.exit(1)
        if mode == "list" and service_key not in {"all4", "bbc", "drtv", "frtv", "itvx", "m6", "my5", "npo", "nrk", "rte", "ruv", "stv", "svt", "tf1", "tv4", "u", "vm"}:
            print(f"{bcolors.YELLOW}{icons.ICON_FAILURE} List mode is currently implemented for All4, BBC, DRTV, France TV, ITVX, M6, My5, NPO, NRK, RTE, RUV, STV, SVT, TF1, TV4, U and Virgin Media only.{bcolors.ENDC}")
            sys.exit(1)
        if mode != "auto" and service_key not in {"all4", "bbc", "drtv", "frtv", "itvx", "m6", "my5", "npo", "nrk", "rte", "ruv", "stv", "svt", "tf1", "tv4", "u", "vm"}:
            print(f"{bcolors.YELLOW}{icons.ICON_FAILURE} {mode} mode is not implemented for this service yet; using default service behavior.{bcolors.ENDC}")
        configure_proxy(config, service_key)
        service = importlib.import_module(service_module)
        service.main(*args)
    except ValueError as e:
        print(f"{bcolors.RED}{icons.ICON_FAILURE} {e}{bcolors.ENDC}")
    except Exception as e:
        print(f"{bcolors.RED}{icons.ICON_FAILURE} Error importing or running the service module: {e}{bcolors.ENDC}")

if __name__ == "__main__":
    main()
