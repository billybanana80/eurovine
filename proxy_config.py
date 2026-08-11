import os
import re

from colors import bcolors
from icons import ICON_PROXY


REGION_BY_SERVICE = {
    "all4": "UK",
    "bbc": "UK",
    "blaze": "UK",
    "drtv": "DK",
    "frtv": "FR",
    "itvx": "UK",    
    "m6": "FR",
    "my5": "UK",
    "npo": "NL",   
    "nrk": "NO",
    "rte": "IE",
    "ruv": "IS",
    "stv": "UK",
    "svt": "SE",
    "tf1": "FR",
    "tv4": "SE",
    "u": "UK",
    "vm": "IE",
}

DEFAULT_PROVIDER_ORDER = ("surfsharkvpn", "nordvpn")
DEFAULT_SERVICE_PROXY_ENABLED = {
    "all4": True,
    "bbc": True,
    "blaze": True,
    "drtv": True,
    "frtv": True,
    "itvx": True,    
    "m6": True,
    "my5": True,
    "npo": True,
    "nrk": True,
    "rte": True,
    "ruv": True,
    "stv": True,
    "svt": True,
    "tf1": True,
    "tv4": True,
    "u": True,
    "vm": True,
}
PROXY_ENV_KEYS = ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "EUROVINE_PROXY_URL")


def mask_proxy(proxy_url):
    if not proxy_url:
        return ""
    return re.sub(r"//[^:@/]+:[^@/]+@", "//***:***@", proxy_url)


def clear_proxy_environment():
    for key in PROXY_ENV_KEYS:
        os.environ.pop(key, None)


def _has_value(value):
    return bool(str(value or "").strip())


def _provider_order(config):
    proxy_config = config.get("proxy") or {}
    configured = proxy_config.get("provider_order") or DEFAULT_PROVIDER_ORDER
    return [str(provider).strip() for provider in configured if str(provider).strip()]


def _service_proxy_enabled(proxy_config, service_key):
    services = proxy_config.get("services") or {}
    if service_key in services:
        return services.get(service_key) is not False
    return DEFAULT_SERVICE_PROXY_ENABLED.get(service_key, True)


def _build_provider_proxy(provider_config, region):
    username = str(provider_config.get("username") or "").strip()
    password = str(provider_config.get("password") or "").strip()
    server_map = provider_config.get("server_map") or {}
    template = str(server_map.get(region) or "").strip()

    if not (_has_value(username) and _has_value(password) and _has_value(template)):
        return None

    return template.replace("username", username).replace("password", password)


def select_proxy(config, service_key):
    proxy_config = config.get("proxy") or {}
    if proxy_config.get("enabled") is False:
        return None
    if not _service_proxy_enabled(proxy_config, service_key):
        return None

    region = REGION_BY_SERVICE.get(service_key)
    if not region:
        return None

    providers = config.get("proxy_providers") or {}
    for provider_name in _provider_order(config):
        proxy_url = _build_provider_proxy(providers.get(provider_name) or {}, region)
        if proxy_url:
            return {
                "provider": provider_name,
                "region": region,
                "url": proxy_url,
            }

    return None


def configure_proxy(config, service_key, printer=print):
    clear_proxy_environment()

    proxy = select_proxy(config, service_key)
    if not proxy:
        printer(f"{bcolors.ORANGE}{ICON_PROXY} Proxy:{bcolors.ENDC} disabled or not configured; using direct connection")
        return None

    proxy_url = proxy["url"]
    os.environ["HTTP_PROXY"] = proxy_url
    os.environ["HTTPS_PROXY"] = proxy_url
    os.environ["http_proxy"] = proxy_url
    os.environ["https_proxy"] = proxy_url
    os.environ["EUROVINE_PROXY_URL"] = proxy_url

    printer(f"{bcolors.ORANGE}{ICON_PROXY} Proxy:{bcolors.ENDC} {proxy['provider']} {proxy['region']} {mask_proxy(proxy_url)}")
    return proxy
