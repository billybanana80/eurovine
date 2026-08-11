# Eurovine

<p align="center">
  <strong>A script organiser/originator for European free-to-air streaming services.</strong>
</p>

<p align="center">
  <a href="https://python.org">
    <img src="https://img.shields.io/badge/python-3.9+-blue" alt="Python version">
  </a>
  <a href="https://docs.python.org/3/library/venv.html">
    <img src="https://img.shields.io/badge/python-venv-blue" alt="Python virtual environments">
  </a>
</p>

Eurovine is a shared launcher for European FTA service scripts. It keeps each service in its own folder while routing common configuration, proxy selection, download paths, CDM paths, colours, list/export behaviour, and command-line modes through `eurovine.py`.

This project prioritises English-speaking users. Where a non-English service exposes default-language subtitles, the service script can translate those subtitles into an English sidecar `.srt` file.

## Supported Services

Eurovine currently supports 17 services:

| Country | Service | Website | Authentication |
| --- | --- | --- | --- |
| UK | All4 | https://www.channel4.com/categories | No account |
| UK | BBC iPlayer | https://www.bbc.co.uk/iplayer | No account; optional certificate for UHD |
| UK | ITVX | https://www.itv.com/ | No account |
| UK | My5 | https://www.channel5.com/ | No account; service certificate configured internally |
| UK | STV | https://player.stv.tv/ | No account |
| UK | U | https://u.co.uk/ | No account |
| Ireland | RTE Player | https://www.rte.ie/player | No account |
| Ireland | Virgin Media Player | https://play.virginmediatelevision.ie/shows | No account |
| Denmark | DRTV | https://www.dr.dk/drtv | No account |
| France | France TV | https://www.france.tv | No account |
| France | M6+ | https://www.m6.fr | Free account credentials |
| France | TF1 | https://www.tf1.fr | Free account credentials |
| Iceland | RUV | https://www.ruv.is/sjonvarp | No account |
| Netherlands | NPO | https://npo.nl/start | Free account credentials |
| Norway | NRK | https://tv.nrk.no | No account |
| Sweden | SVT Play | https://www.svtplay.se | No account |
| Sweden | TV4 Play | https://www.tv4play.se | Browser cookies |

## Features

- Movies, single episodes, and TV series where supported by the service.
- Automatic manifest, PSSH, licence, and key handling where needed.
- Widevine support where required.
- PlayReady support for M6+ hardware-preferred downloads.
- Optional BBC UHD mode with a configured BBC certificate.
- Optional proxy support using Surfshark or NordVPN-style HTTPS proxy endpoints.
- Shared list/export/download-selector behaviour across converted services.
- English subtitle sidecar translation for non-English services where subtitles are available.
- Shared `colors.py`, `icons.py`, `proxy.py`, and `config.yaml` usage from the Eurovine root.

## Requirements

- [Python](https://www.python.org/)
- [N_m3u8DL-RE](https://github.com/nilaoda/N_m3u8DL-RE/releases/)
- [ffmpeg](https://ffmpeg.org/)
- [mkvmerge](https://mkvtoolnix.download/downloads.html)
- [mp4decrypt](https://www.bento4.com/downloads/)
- A valid Widevine `.wvd` device file for Widevine services. This is not included.
- A PlayReady `.prd` device file if using M6+ hardware/PlayReady mode. This is not included.

Install Python dependencies:

```powershell
pip install -r requirements.txt
```

## Folder Layout

Recommended layout:

```text
Eurovine/
+-- eurovine.py
+-- config.yaml
+-- requirements.txt
+-- colors.py
+-- icons.py
+-- certificates/
+-- cookies/
+-- export/
+-- prd/
+-- temp/
+-- wvd/
+-- services/
    +-- all4/
    +-- bbc/
    +-- drtv/
    +-- frtv/
    +-- itvx/
    +-- m6/
    +-- my5/
    +-- npo/
    +-- nrk/
    +-- rte/
    +-- ruv/
    +-- stv/
    +-- svt/
    +-- tf1/
    +-- tv4/
    +-- u/
    +-- vm/
```

`export/` is used for `-l -x` episode URL lists.

`temp/` is used for temporary files shared by services that need one.

## Configuration

Create or edit `config.yaml` in the Eurovine root.

Example template:

```yaml
downloads_path: D:/Downloads/
wvd_device_path: D:/Downloads/Eurovine/wvd/device.wvd
prd_device_path: D:/Downloads/Eurovine/prd/device.prd
cookies_path: D:/Downloads/Eurovine/cookies/cookies.txt

credentials:
  m6: email@example.com:password
  npo: email@example.com:password
  tf1: email@example.com:password

proxy:
  enabled: false
  provider_order:
    - surfsharkvpn
    - nordvpn
  services:
    all4: false
    bbc: false
    drtv: true
    frtv: true
    itvx: false
    m6: true
    my5: false
    npo: true
    nrk: true
    rte: true
    ruv: true
    stv: false
    svt: false
    tf1: true
    tv4: true
    u: true
    vm: true

proxy_providers:
  surfsharkvpn:
    username: your_proxy_username
    password: your_proxy_password
    server_map:
      UK: https://username:password@uk-lon.prod.surfshark.com:443
      IE: https://username:password@ie-dub.prod.surfshark.com:443
      DK: https://username:password@dk-cph.prod.surfshark.com:443
      FR: https://username:password@fr-par.prod.surfshark.com:443
      IS: https://username:password@is-rkv.prod.surfshark.com:443
      NL: https://username:password@nl-ams.prod.surfshark.com:443
      NO: https://username:password@no-osl.prod.surfshark.com:443
      SE: https://username:password@se-sto.prod.surfshark.com:443
  nordvpn:
    username: your_proxy_username
    password: your_proxy_password
    server_map:
      UK: https://username:password@uk2076.proxy.nordvpn.com:89
      IE: https://username:password@ie248.proxy.nordvpn.com:89
      DK: https://username:password@dk244.proxy.nordvpn.com:89
      FR: https://username:password@fr1200.proxy.nordvpn.com:89
      IS: https://username:password@is96.proxy.nordvpn.com:89
      NL: https://username:password@nl993.proxy.nordvpn.com:89
      NO: https://username:password@no269.proxy.nordvpn.com:89
      SE: https://username:password@se682.proxy.nordvpn.com:89

bbc:
  certificate: D:/Downloads/Eurovine/certificates/iplayer.pem

m6:
  prefer_hardware: true
  prefer_hls: false

my5:
  certificate: pre-populated in the default config.yaml

tf1: {}

vmplayer_device_id:
```

Notes:

- `downloads_path` is where completed files are saved.
- `wvd_device_path` is required for Widevine services.
- `prd_device_path` is only needed for PlayReady/hardware flows such as M6+.
- `cookies_path` is used by TV4 when reading an exported `cookies.txt` file.
- `vmplayer_device_id` can be left blank. Virgin Media can generate and save one.
- The `username:password` values used for Surfshark and NordVPN proxy servers are service/manual-setup credentials created for OpenVPN/proxy support. They are NOT your normal subscription login email and password.
- NordVPN hostnames above are examples based on Nord's manual OpenVPN server list. Replace them if Nord recommends newer servers for your account.

## Authentication Notes

### Credentials

The following services may require free-account credentials in `config.yaml`:

| Service | Config key | Notes |
| --- | --- | --- |
| M6+ | `credentials.m6` | Used for M6+ account login/token flow. |
| NPO | `credentials.npo` | Used for NPO account login/token flow. |
| TF1 | `credentials.tf1` | Used for TF1 account login/token flow. |

Use this format:

```yaml
credentials:
  m6: email@example.com:password
  npo: email@example.com:password
  tf1: email@example.com:password
```

### Cookies

TV4 uses an exported browser cookies file from a signed-in TV4 session.

Cookie export extensions:

- Firefox: [Export Cookies TXT](https://addons.mozilla.org/addon/export-cookies-txt)
- Chrome: [Get cookies.txt Clean](https://chromewebstore.google.com/detail/get-cookiestxt-clean/ahmnmhfbokciafffnknlekllgcnafnie)

Example:

```yaml
cookies_path: D:/Downloads/Eurovine/cookies/cookies.txt
```

### Certificates

BBC UHD mode uses a configured certificate:

```yaml
bbc:
  certificate: D:/Downloads/Eurovine/certificates/iplayer.pem
```

## Proxy Support

Eurovine can route service requests and downloader commands through a configured HTTPS proxy provider. This is useful when a service requires a local IP address, or when a direct IP is temporarily rate-limited.

Proxy routing is selected automatically from the input URL.

| Country | Code | Services |
| --- | --- | --- |
| UK | `UK` | All4, BBC, ITVX, My5, STV, U |
| Ireland | `IE` | RTE, Virgin Media |
| Denmark | `DK` | DRTV |
| France | `FR` | France TV, M6+, TF1 |
| Iceland | `IS` | RUV |
| Netherlands | `NL` | NPO |
| Norway | `NO` | NRK |
| Sweden | `SE` | SVT, TV4 |

Provider selection works like this:

1. If `proxy.enabled` is `false`, Eurovine uses a direct connection.
2. If `proxy.enabled` is `true`, Eurovine checks the current service under `proxy.services`.
3. If the service is set to `false`, Eurovine uses a direct connection for that service.
4. If the service is set to `true`, Eurovine tries providers in `provider_order`.
5. The first provider with complete credentials and a matching country server is used.
6. If no complete provider is configured, Eurovine falls back to a direct connection.

When a proxy is active, Eurovine also passes it to `N_m3u8DL-RE` with `--custom-proxy`. Printed commands mask proxy credentials, but the real command uses the full proxy URL.

## Usage

Run Eurovine and paste a supported URL:

```powershell
python eurovine.py
```

Or pass the URL directly:

```powershell
python eurovine.py "https://www.channel4.com/programmes/come-dine-with-me/on-demand/78463-009"
```

The same flags can be entered after the URL when using the interactive prompt.

### Modes

| Mode | Flags | Behaviour |
| --- | --- | --- |
| Auto | none | Builds the default best-quality command and asks whether to download. |
| Info | `--info` or `-i` | Shows available streams, metadata, keys where applicable, and suggested filename without downloading. |
| Action | `--action` or `-a` | Builds a command without automatic video/audio selectors so `N_m3u8DL-RE` can prompt for manual stream choices. |
| List | `--list` or `-l` | Lists available episodes for a supported show/series URL. |
| Export | `--export` or `-x` | Used with list mode to export episode URLs to the `export/` folder. |
| Download selector | `--download` or `-d` | Downloads a selected episode, season, or range from a show/series URL. |
| BBC UHD catalogue | `bbc -u` | Lists the current BBC iPlayer programmes advertised as available in UHD. |
| BBC UHD download | `--ultra` or `-u` with a BBC URL | Requests UHD streams using the configured BBC certificate. |
| Clear cache | `--clear-cache` or `-c` | Clears known cached service tokens from `config.yaml` and removes files from `temp/`. |

Selectors use season/episode formatting:

```text
s01e01
s01
s01e01-s01e03
s01-s03
s2026e01
s2026
```

Whole-season and range selectors print a download queue and ask once before downloading:

```text
Do you wish to download these 8 episodes? Y or N:
```

### Examples

All4 show list:

```powershell
python eurovine.py "https://www.channel4.com/programmes/come-dine-with-me" -l
```

All4 list and export:

```powershell
python eurovine.py "https://www.channel4.com/programmes/come-dine-with-me" -l -x
```

All4 episode info:

```powershell
python eurovine.py "https://www.channel4.com/programmes/come-dine-with-me/on-demand/78463-009" -i
```

All4 direct download prompt:

```powershell
python eurovine.py "https://www.channel4.com/programmes/come-dine-with-me/on-demand/78463-009"
```

TF1 show list:

```powershell
python eurovine.py "https://www.tf1.fr/tf1/zodiaque-176" -l
```

TF1 selected episode:

```powershell
python eurovine.py "https://www.tf1.fr/tf1/zodiaque-176" -d s01e01
```

TF1 episode info:

```powershell
python eurovine.py "https://www.tf1.fr/tf1/zodiaque-176/videos/zodiaque-s01-e01-78201107.html" -i
```

BBC UHD show selector:

```powershell
python eurovine.py "https://www.bbc.co.uk/iplayer/episodes/p0f2cxpr" -d s03e03 -u
```

BBC UHD catalogue:

```powershell
python eurovine.py bbc -u
```

BBC UHD episode:

```powershell
python eurovine.py "https://www.bbc.co.uk/iplayer/episode/m002k78r/blue-lights-series-3-3-the-bird" -u
```

Manual stream selection:

```powershell
python eurovine.py "https://www.bbc.co.uk/iplayer/episode/m002k78r/blue-lights-series-3-3-the-bird" -a
```

Clear cached tokens and temporary files:

```powershell
python eurovine.py -c
```

## Clear Cache

The clear-cache option is project-wide and does not require a service URL:

```powershell
python eurovine.py --clear-cache
```

It removes:

- known cached token entries from `config.yaml`, currently `tf1.cache` and TV4 cache entries;
- files and folders inside `temp/`.

It does not remove:

- service credentials;
- browser cookies;
- certificates;
- `.wvd` or `.prd` device files;
- downloaded videos;
- exported episode lists;
- Python `__pycache__` folders.

Python `__pycache__` folders contain bytecode files automatically created by Python when modules are imported. They are safe to delete manually, but they are not Eurovine service caches and Python will recreate them as needed.

## Subtitle Translation

For non-English services, Eurovine tries to use the service's default-language subtitles where available and translates them to an English sidecar `.srt` file.

The translated subtitle is saved beside the video using a filename like:

```text
Show.Name.S01E01.1080p.SERVICE.WEB-DL.AAC2.0.H.264.en.srt
```

Subtitle availability depends on the service and the specific title. If no subtitles are exposed by the service, Eurovine cannot create an English sidecar.

## Service Notes

- BBC UHD downloads require `-u` and a valid `bbc.certificate`. The `bbc -u` catalogue shortcut only lists BBC's current UHD programme page.
- BBC sometimes exposes separate TTML captions through its media-selector data rather than inside the HLS/DASH manifest. Eurovine currently relies on manifest subtitles for BBC downloads, so those separate BBC caption files are not downloaded automatically.
- M6+ can use Widevine/software or PlayReady/hardware depending on config and available streams.
- NPO age-restricted content may only be available during the Dutch watershed window, typically 20:00-06:00 Netherlands time.
- TV4 requires a valid TV4 browser refresh token/cookie for playback.
- Some services may work without a proxy one day and require a local proxy the next, depending on CDN, geoblocking, rate limits, and IP reputation.

## Common Issues

### `ModuleNotFoundError: No module named ...`

Install the Python requirements:

```powershell
pip install -r requirements.txt
```

### `Unsupported URL`

The URL does not match one of the supported service URL patterns. Use a show, series, episode, or movie page from a supported website.

### `Download mode requires a selector`

`-d` needs a selector:

```powershell
python eurovine.py "https://www.channel4.com/programmes/come-dine-with-me" -d s01e01
```

### Proxy or geoblock errors

Enable proxy support for the affected service and make sure the matching country server is configured.

### NPO HTTP 450

For some age-restricted NPO titles, try again between 20:00 and 06:00 Netherlands time.

## Disclaimer

1. This project is purely for educational purposes and does not condone piracy. Users are responsible for complying with applicable laws, service terms, and local regulations.
2. CDM required for key derivation is not included in this project.
3. BBC certificate required for UHD is not included in this project.
