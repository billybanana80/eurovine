<h1 align="center">Eurovine</h1>

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

Eurovine currently supports 23 services:

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
| Germany | ARD Mediathek | https://www.ardmediathek.de | No account |
| Germany | ZDF | https://www.zdf.de | No account |
| Iceland | RUV | https://www.ruv.is/sjonvarp | No account |
| Italy | Mediaset Infinity IT | https://mediasetinfinity.mediaset.it | No account |
| Italy | RaiPlay | https://www.raiplay.it | No account |
| Netherlands | NPO | https://npo.nl/start | Free account credentials |
| Norway | NRK | https://tv.nrk.no | No account |
| Spain | Mediaset Infinity ES | https://www.mediasetinfinity.es | No account |
| Spain | RTVE Play | https://www.rtve.es/play | No account |
| Sweden | SVT Play | https://www.svtplay.se | No account |
| Sweden | TV4 Play | https://www.tv4play.se | Browser cookies |

## Features

- Movies, single episodes, and TV series where supported by the service.
- Automatic manifest, PSSH, licence, and key handling where needed.
- Widevine support where required.
- PlayReady support for M6+ hardware-preferred downloads.
- Optional BBC UHD mode with a configured BBC certificate, including quality selection and manual stream selection.
- Optional proxy support using Surfshark or NordVPN-style HTTPS proxy endpoints.
- Shared list/export/download-selector behaviour across all services.
- Batch import mode using exported episode URL text files.
- Optional quality selection with `-q/--quality` for single episode, selector-based, batch, and BBC UHD downloads.
- English subtitle sidecar translation for non-English services where subtitles are available.
- Optional `-s/--subs` mode to keep native/default service subtitles where implemented.
- Project-wide cache clearing for known token caches and temporary files.

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
    +-- ard/
    +-- bbc/
    +-- drtv/
    +-- frtv/
    +-- itvx/
    +-- m6/
    +-- mse/
    +-- msi/
    +-- my5/
    +-- npo/
    +-- nrk/
    +-- rai/
    +-- rte/
    +-- ruv/
    +-- rtve/
    +-- stv/
    +-- svt/
    +-- tf1/
    +-- tv4/
    +-- u/
    +-- vm/
    +-- zdf/
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
update_checks: true

download:
  auto_confirm: false

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
    ard: true
    bbc: false
    drtv: true
    frtv: true
    itvx: false
    m6: true
    mse: true
    msi: true
    my5: false
    npo: true
    nrk: true
    rai: true
    rte: true
    rtve: true
    ruv: true
    stv: false
    svt: false
    tf1: true
    tv4: true
    u: true
    vm: true
    zdf: true

proxy_providers:
  surfsharkvpn:
    username: your_proxy_username
    password: your_proxy_password
    server_map:
      DE: https://username:password@de-ber.prod.surfshark.com:443
      DK: https://username:password@dk-cph.prod.surfshark.com:443
      ES: https://username:password@es-mad.prod.surfshark.com:443
      FR: https://username:password@fr-par.prod.surfshark.com:443
      IE: https://username:password@ie-dub.prod.surfshark.com:443
      IS: https://username:password@is-rkv.prod.surfshark.com:443
      IT: https://username:password@it-mil.prod.surfshark.com:443
      NL: https://username:password@nl-ams.prod.surfshark.com:443
      'NO': https://username:password@no-osl.prod.surfshark.com:443
      SE: https://username:password@se-sto.prod.surfshark.com:443
      UK: https://username:password@uk-lon.prod.surfshark.com:443
  nordvpn:
    username: your_proxy_username
    password: your_proxy_password
    server_map:
      DE: https://username:password@de1481.proxy.nordvpn.com:89
      DK: https://username:password@dk244.proxy.nordvpn.com:89
      ES: https://username:password@es286.proxy.nordvpn.com:89
      FR: https://username:password@fr1200.proxy.nordvpn.com:89
      IE: https://username:password@ie248.proxy.nordvpn.com:89
      IS: https://username:password@is96.proxy.nordvpn.com:89
      IT: https://username:password@it444.proxy.nordvpn.com:89
      NL: https://username:password@nl993.proxy.nordvpn.com:89
      'NO': https://username:password@no269.proxy.nordvpn.com:89
      SE: https://username:password@se682.proxy.nordvpn.com:89
      UK: https://username:password@uk2076.proxy.nordvpn.com:89
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

Surfshark VPN and NordVPN are supported out of the box. Other providers such as ExpressVPN, Windscribe, PIA, and similar services can also be added, provided they offer OpenVPN-style proxy server addresses and separate proxy/service credentials.

Proxy routing is selected automatically from the input URL.

| Country | Code | Services |
| --- | --- | --- |
| UK | `UK` | All4, BBC, ITVX, My5, STV, U |
| Ireland | `IE` | RTE, Virgin Media |
| Denmark | `DK` | DRTV |
| France | `FR` | France TV, M6+, TF1 |
| Germany | `DE` | ARD, ZDF |
| Iceland | `IS` | RUV |
| Italy | `IT` | Mediaset Infinity IT, RaiPlay |
| Netherlands | `NL` | NPO |
| Norway | `NO` | NRK |
| Spain | `ES` | Mediaset Infinity ES, RTVE Play |
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
| Batch | `--batch` or `-b` | Imports episode URLs from all `.txt` files in `export/`, asks once, then processes them automatically. |
| Quality | `--quality` or `-q` | With auto, download selector, batch, and BBC UHD modes, selects a video height such as `720` or `1080` and uses that height in the generated filename. |
| Auto-confirm | `--yes` or `-y` | Automatically answers yes to download and subtitle-save prompts for the current run. |
| BBC UHD catalogue | `bbc -u` | Lists the current BBC iPlayer programmes advertised as available in UHD. |
| BBC UHD download | `--ultra` or `-u` with a BBC URL | Requests UHD streams using the configured BBC certificate. |
| Subtitles | `--subs` or `-s` | Keeps service subtitles where implemented. This may retain subtitle tracks in the downloaded file or save an extra native/default `.srt` sidecar, depending on the service. |
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

RaiPlay show list:

```powershell
python eurovine.py "https://www.raiplay.it/programmi/thebeachserietv" -l
```

RaiPlay selected episode at a requested height:

```powershell
python eurovine.py "https://www.raiplay.it/programmi/thebeachserietv" -d s01e64 -q 1080
```

RaiPlay movie info:

```powershell
python eurovine.py "https://www.raiplay.it/programmi/itaca-ilritorno" -i
```

Mediaset Infinity IT show list:

```powershell
python eurovine.py "https://mediasetinfinity.mediaset.it/fiction/tuttoperlamiafamiglia_SE000000002688" -l
```

Mediaset Infinity IT episode info:

```powershell
python eurovine.py "https://mediasetinfinity.mediaset.it/video/tuttoperlamiafamiglia2/episodio-20_F313587401002004" -i
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

BBC episode with external sidecar subtitles:

```powershell
python eurovine.py "https://www.bbc.co.uk/iplayer/episode/p0p107tb/cefn-gwlad-cyfres-2026-teulu-tan-y-bryn" -s
```

BBC UHD episode with external sidecar subtitles:

```powershell
python eurovine.py "https://www.bbc.co.uk/iplayer/episode/p0f2d07v/blue-lights-series-1-2-bad-batch" -u -s
```

NPO episode with native Dutch subtitles retained:

```powershell
python eurovine.py "https://npo.nl/start/afspelen/alles-op-scherp" -s
```

Manual stream selection:

```powershell
python eurovine.py "https://www.bbc.co.uk/iplayer/episode/m002k78r/blue-lights-series-3-3-the-bird" -a
```

Quality selection:

```powershell
python eurovine.py "https://www.channel4.com/programmes/come-dine-with-me/on-demand/78463-009" -q 720
python eurovine.py "https://www.ardmediathek.de/serie/babylon-berlin-oder-alle-vier-staffeln/staffel-1/Y3JpZDovL2Rhc2Vyc3RlLmRlL2JhYnlsb24tYmVybGlu/1" -d s01e01 -q 1080
python eurovine.py "https://www.bbc.co.uk/iplayer/episodes/p0f2cxpr" -d s03e03 -q 720
python eurovine.py "https://www.m6.fr/desperate-housewives-p_840" -d s08 -q 576
python eurovine.py "https://www.france.tv/france-3/opj/" -d s02e01-s02e02 -q 576
python eurovine.py "https://www.zdf.de/serien/soko-leipzig-104" -d s26e24 -q 1080
```

Auto-confirm:

```powershell
python eurovine.py "https://www.channel4.com/programmes/come-dine-with-me/on-demand/78463-009" -y
python eurovine.py "https://www.m6.fr/desperate-housewives-p_840" -d s08 -q 576 -y
```

Batch import from exported episode lists:

```powershell
python eurovine.py -b
python eurovine.py -b -y
python eurovine.py -b -q 720 -s
python eurovine.py -b -u -q 1080
```

Batch mode reads every `.txt` file in `export/`, accepts both URL-only lines and tab-separated export lines, removes duplicate URLs, asks once unless `-y` or `download.auto_confirm: true` is enabled, and then runs each episode automatically. `-q` and `-s` are passed through to each episode. `-u` is applied to BBC URLs only and ignored for non-BBC URLs.

Quality mode builds the download command with a specific video height, using an exact-height selector such as `--select-video res=x720$` instead of `--select-video best`. The requested height is also used in filenames for actual download modes, so a `-q 720` download is saved with a `720p` filename tag.

It is recommended to check available streams with `-i` first, because requesting a height that does not exist can leave N_m3u8DL-RE with no matching video stream selected. BBC `-u/--ultra` can be combined with `-q`, so BBC UHD can also be used to request lower UHD-ladder heights such as `1080`.

Auto-confirm mode is useful when you want Eurovine to run without service `Y/N` prompts. Use `-y` for a single run, or set `download.auto_confirm: true` in `config.yaml` to make it the default. This also confirms download selector queues and subtitle-save prompts where a service offers them, so check the URL, selector, save path, proxy, and quality options carefully before enabling it globally.

Update checks are enabled by default with `update_checks: true` in `config.yaml`. On startup, Eurovine checks the latest GitHub release for `billybanana80/eurovine` and prints a notice only when a newer version is available. It does not warn for unreleased master branch pushes.

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

Where implemented, `-s` / `--subs` keeps the service's native/default subtitles as well. Depending on the service, this may retain manifest subtitle tracks in the downloaded file or save a native-language `.srt` sidecar beside the translated English `.en.srt`.

### Subtitle handling by service

| Service | Subtitle source | Normal download | With `-s` / `--subs` |
| --- | --- | --- | --- |
| All4 | Manifest | Drops subtitle tracks | Keeps/muxes manifest subtitles |
| BBC iPlayer | External TTML where available | No sidecar subtitle is saved | Saves external English `.en.srt` sidecar; also works with `-u` UHD |
| ITVX | External WebVTT where available | No sidecar subtitle is saved | Saves external English `.en.srt` sidecar |
| My5 | Manifest | Drops subtitle tracks | Keeps/muxes manifest subtitles |
| STV | Manifest | Drops subtitle tracks | Keeps/muxes manifest subtitles |
| U | Manifest | Drops subtitle tracks | Keeps/muxes manifest subtitles |
| RTE | Manifest or external VTT where available | Drops subtitle tracks | Keeps/muxes manifest subtitles, or saves external English `.en.srt` sidecar |
| Virgin Media | Manifest if present | Drops subtitle tracks | Keeps/muxes manifest subtitles |
| ARD Mediathek | External German subtitles | Saves translated English `.en.srt` sidecar | Also saves native German `.de.srt` sidecar |
| DRTV | Manifest subtitles | Saves translated English `.en.srt` sidecar | Also keeps/muxes Danish manifest subtitles |
| France TV | Manifest subtitles | Saves translated English `.en.srt` sidecar | Also keeps/muxes French manifest subtitles |
| M6+ | Manifest subtitles where available | Saves translated English `.en.srt` sidecar when subtitles exist | Also keeps/muxes French manifest subtitles |
| Mediaset Infinity ES | External Spanish VTT where available | Saves translated English `.en.srt` sidecar when subtitles exist | Also saves native Spanish `.es.srt` sidecar |
| Mediaset Infinity IT | External Italian subtitles where available | Saves translated English `.en.srt` sidecar when subtitles exist | Also saves native Italian `.it.srt` sidecar |
| NPO | External Dutch VTT where available | Saves translated English `.en.srt` sidecar when subtitles exist | Also saves native Dutch `.nl.srt` sidecar |
| NRK | Manifest subtitles | Saves translated English `.en.srt` sidecar | Also keeps/muxes Norwegian manifest subtitles |
| RaiPlay | External Italian SRT where available | Saves translated English `.en.srt` sidecar when subtitles exist | Also saves native Italian `.it.srt` sidecar |
| RTVE Play | Manifest subtitles | Saves translated English `.en.srt` sidecar when RTVE exposes a direct VTT subtitle URL | Also keeps/muxes Spanish manifest subtitles |
| RUV | Manifest subtitles | Saves translated English `.en.srt` sidecar | Also keeps/muxes Icelandic manifest subtitles |
| SVT Play | Manifest subtitles where available | Saves translated English `.en.srt` sidecar when subtitles exist | Also keeps/muxes Swedish manifest subtitles |
| TF1 | Manifest subtitles | Saves translated English `.en.srt` sidecar | Also keeps/muxes French manifest subtitles |
| TV4 Play | Manifest subtitles | Saves translated English `.en.srt` sidecar | Also keeps/muxes Swedish manifest subtitles |
| ZDF | External German subtitles | Saves translated English `.en.srt` sidecar | Also saves native German `.de.srt` sidecar |

## Service Notes

- BBC UHD downloads require `-u` and a valid `bbc.certificate`. The `bbc -u` catalogue shortcut only lists BBC's current UHD programme page.
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
