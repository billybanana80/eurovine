import re
import shutil
import sys
from pathlib import Path
from urllib.parse import urljoin

import requests
import yaml
from lxml import html

import icons
from colors import bcolors
from services.proxy import current_proxy_url


SOURCE_URL = "https://www.bbc.co.uk/iplayer/help/questions/programme-availability/uhd-content"
SERVICE_DIR = Path(__file__).resolve().parent
EUROVINE_DIR = SERVICE_DIR.parents[1]
CONFIG_PATH = EUROVINE_DIR / "config.yaml"


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")


def read_config():
    if not CONFIG_PATH.is_file():
        return {}
    with open(CONFIG_PATH, "r", encoding="utf-8") as config_file:
        return yaml.safe_load(config_file) or {}


def build_session():
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) BBC-UHD-Catalogue/1.0"
    })

    proxy = current_proxy_url()
    if proxy:
        session.proxies.update({"http": proxy, "https": proxy})
    return session


def clean_text(value):
    return re.sub(r"\s+", " ", value or "").strip()


def fetch_uhd_programmes(session):
    response = session.get(SOURCE_URL, timeout=30)
    response.raise_for_status()
    tree = html.fromstring(response.content)

    headings = tree.xpath(
        "//h2[contains(normalize-space(.), 'Full list of Ultra HD programmes')]"
        " | //h3[contains(normalize-space(.), 'Full list of Ultra HD programmes')]"
    )
    if not headings:
        raise ValueError("BBC's Full list of Ultra HD programmes section was not found.")

    lists = headings[0].xpath("following-sibling::ul[1]")
    if not lists:
        raise ValueError("BBC's UHD programme list was not found after its heading.")

    programmes = []
    seen = set()
    for link in lists[0].xpath("./li//a[@href]"):
        title = clean_text(" ".join(link.itertext()))
        url = urljoin(SOURCE_URL, link.get("href"))
        identity = (title.casefold(), url)
        if not title or identity in seen:
            continue
        seen.add(identity)
        programmes.append({"title": title, "url": url})

    if not programmes:
        raise ValueError("BBC's UHD programme list was empty.")
    return programmes


def print_catalogue_rule():
    terminal_width = shutil.get_terminal_size((88, 20)).columns
    title = " BBC UHD Catalogue "
    rule_width = max(terminal_width, len(title) + 4)
    left_width = max((rule_width - len(title)) // 2, 0)
    right_width = max(rule_width - len(title) - left_width, 0)
    print(
        f"{bcolors.LIGHTBLUE}{'─' * left_width}{bcolors.ENDC} "
        f"{bcolors.LIGHTBLUE}BBC UHD Catalogue{bcolors.ENDC} "
        f"{bcolors.LIGHTBLUE}{'─' * right_width}{bcolors.ENDC}"
    )


def print_programmes(programmes):
    count = len(programmes)
    noun = "programme" if count == 1 else "programmes"
    print(f"{icons.ICON_SUCCESS} {bcolors.OKGREEN}Found {count} BBC UHD {noun}{bcolors.ENDC}")
    print()
    print_catalogue_rule()
    print()
    print(f"{bcolors.GRAY}1 Catalogue,  Full list({count}){bcolors.ENDC}")
    print(f"{bcolors.GRAY}└─ Full list: {bcolors.ENDC}{count} {noun}")

    for index, programme in enumerate(programmes, start=1):
        is_last = index == count
        branch = "└─" if is_last else "├─"
        url_branch = "  " if is_last else "│ "
        print(
            f"{bcolors.GRAY}   {branch} {index}. {bcolors.ENDC}"
            f"{bcolors.WHITE}{programme['title']}{bcolors.ENDC}"
        )
        print(
            f"{bcolors.GRAY}   {url_branch} {bcolors.ENDC}"
            f"{bcolors.LIGHTBLUE}{programme['url']}{bcolors.ENDC}"
        )


def main(config=None):
    try:
        if config is None:
            config = read_config()
        programmes = fetch_uhd_programmes(build_session())
        print_programmes(programmes)
    except (OSError, ValueError, requests.RequestException, yaml.YAMLError) as exc:
        print(f"{icons.ICON_FAILURE} {bcolors.FAIL}Could not retrieve BBC UHD catalogue: {exc}{bcolors.ENDC}")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
