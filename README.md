<p align="center">
  <img src="assets/xdebx-hero.png" alt="XdebX — silver and blue terminal-inspired project banner" width="100%">
</p>

<h1 align="center">XdebX</h1>

<p align="center">
  A bilingual terminal interface for setting up your Linux environment.
</p>

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.8%2B-3776AB?logo=python&logoColor=white" alt="Python 3.8+">
  <img src="https://img.shields.io/badge/Architecture-AMD64-0969DA" alt="AMD64">
  <img src="https://img.shields.io/badge/Ubuntu-20.04%20%7C%2022.04%20%7C%2024.04-E95420?logo=ubuntu&logoColor=white" alt="Ubuntu 20.04, 22.04 and 24.04">
  <img src="https://img.shields.io/badge/Debian-12%20%7C%2013-A81D33?logo=debian&logoColor=white" alt="Debian 12 and 13">
</p>

<p align="center">
  <strong>English</strong> · <a href="README-CN.md">简体中文</a>
  <br>
  <a href="#quick-start">Quick start</a> · <a href="#features">Features</a> · <a href="#documentation">Documentation</a>
</p>

---

XdebX brings software installation, shell configuration and installation recovery into one framed TUI. Choose a batch, follow the live log, and retry incomplete items without discarding your existing configuration.

## Features

- **Three clear entry points.** Fast Install, Install List and Settings, alongside a scrolling installation log.
- **English and Chinese.** JSON-backed translations, desktop detection and a software list suited to your environment.
- **Recoverable installations.** Failed, interrupted and partial items remain selectable. Retries clean recorded remnants, preserve working components and keep existing user configuration.
- **Multiple installation paths.** System APT or official releases, compatible pinned packages, then supported source builds. Independent items continue after a failure.
- **Downloads for your location.** Raw and CN modes, task-scoped mirror fallback, and four verified Gitee plugin mirrors with pinned commits and file hashes.
- **A polished terminal experience.** Silver and blue styling, a 3D text animation, colored logs and optional music. `--lite` provides a plain interface.
- **A smaller default cache.** Enabled small assets up to 5 MiB per file; large packages download on demand. The cache is not a complete offline dependency repository.

## Supported systems

| Distribution | Versions |
| --- | --- |
| Ubuntu | 20.04 · 22.04 · 24.04 |
| Debian | 12 · 13 |

**AMD64 / x86_64 only.** Python 3.8+ with `curses` is required; no third-party Python packages are needed for the TUI. Desktop applications are shown when a desktop environment is detected. Individual package availability depends on the target system.

## Quick start

### From source

```sh
git clone --depth 1 --single-branch --branch main \
  https://github.com/XLYing23333/XdebX.git
cd XdebX
./run_xdebx.sh
```

`main` contains the shared source for all five supported systems. The shallow clone above downloads only the latest history of that branch. Released versions use tags such as `v0.1.0`; when a tag has been published, replace `--branch main` with `--branch v0.1.0` to obtain that version.

Run as your normal user. XdebX requests `sudo` authentication when a task needs administrator privileges. Direct root execution requires an explicit target user:

```sh
./run_xdebx.sh --user YOUR_USERNAME
```

Source checkouts exclude package binaries, font binaries and music. Program archives built by this project include the four configured Maple Mono NF fonts. See [cache and assets](#cache-and-assets) for optional source-checkout resources.

### Choose your interface

```sh
./run_xdebx.sh --language EN --mirror Raw
./run_xdebx.sh --language CN --mirror CN
./run_xdebx.sh --lite
```

The defaults are English, Raw downloads and music enabled. Music is attempted only in a display session when the audio file and an existing player are available.

## Using the TUI

| Menu | What it does |
| --- | --- |
| **Fast Install** | Preselects the configured 25-item batch, filtered for your system and desktop environment. Review it before running. |
| **Install List** | Search, select and install software; access configuration, backups and installation history. |
| **Settings** | Change language, mirror and music preferences; clear download cache or delete XdebX state. |

Complete installations stay locked. Failed, interrupted and partial items can be selected again. **DeleteXdebX is in Settings and requires two confirmations**; it removes tool settings, logs and downloaded cache, preserving installed software and configuration backups.

| Key | Action |
| --- | --- |
| `↑` / `↓`, `j` / `k` | Move |
| `Space`, `Enter` | Select; open or confirm an action |
| `/` | Search the software list |
| `Tab`, `PgUp` / `PgDn` | Switch to logs; scroll logs |
| `X` | Request a safe stop and stay in the interface |
| `Esc`, `Q` | Go back; exit |
| `C`, `B`, `H` | In Install List: configure, view backups, view history |
| `R` | Refresh; in history, retry an item |

`[OK]` means installed, `[~]` needs attention, and `[x]` is selected. Logs and item-count progress show the outcome of each task. Active package-manager transactions finish safely before shutdown; XdebX does not forcibly kill `dpkg`.

## Command-line usage

```sh
# Inspect your environment and available software
./run_xdebx.sh --doctor
./run_xdebx.sh --list --desktop no

# Review a plan before installing
./run_xdebx.sh --dry-run --fast
./run_xdebx.sh --install vim9 nodejs22-nvm --yes

# Reapply configuration with backups; the software must already exist
./run_xdebx.sh --configure vim9 --yes

# Restore a saved configuration record
./run_xdebx.sh --restore /path/to/record.json
```

Use `--help` for all options. Explicit configuration actions may reapply templates; retry recovery preserves existing configuration. A conflicting restore is refused unless `--force` is supplied, which first preserves the modified file.

## Software catalogue

| Category | Examples |
| --- | --- |
| Terminal tools | Git, curl, wget, btop, htop, fastfetch, gdu, lsd |
| Shell and editors | Zsh + Oh My Zsh, Vim 9 + plugins, Tmux + tmuxp |
| Runtimes | Node.js 22 + NVM, uv, Miniconda, JDK 21, Go, TeX Live |
| Services | Docker, MySQL, Redis |
| Coding agents | Claude Code + DeepSeek, OpenCode, OpenClaw |
| Desktop applications | Ghostty, VS Code, Chrome, gedit, VLC, Snipaste |

Versions and target exclusions are recorded in the package manifests. The provided Zsh, Vim, Tmux and DeepSeek templates are used during configuration; fill in your own credentials where required. New Go SDKs use `~/Software/go`; existing `/opt/go` installations remain detectable.

## Mirrors and recovery

```text
APT / official release → compatible pinned package → supported source build → record failure
```

CN mirror changes apply to the current task rather than rewriting global APT or npm settings. Failed CN downloads can retry the corresponding official source. Git uses bounded shallow clones, HTTP/1.1 and fixed commits; CN prefers verified Gitee mirrors for **Oh My Zsh, NERDTree, zsh-autosuggestions and zsh-syntax-highlighting**. Unavailable Git sources fall back to hash-verified archives.

Recovery removes only recorded temporary remnants. Incomplete managed components are backed up, and directories of uncertain ownership are preserved for review. Settings, state, cache and backups follow XDG directories.

## Cache and assets

From a source checkout, fetch the default small cache without installing software:

```sh
python3 tools/cache_packages.py
# Opt into the complete cache and corresponding source
python3 tools/cache_packages.py --full
```

Recognized cache archives can be imported with `./run_xdebx.sh --import-cache /path/to/cache.tar.gz`. Large applications and unresolved dependencies can still require network access. See the [cache guide](data/pkgs/README.md).

Fonts live under `data/fonts/`. Optional audio uses `data/music/電脳眠眠猫short.mp3` with an existing `ffplay`, `mpv` or `mpg123`; missing audio or playback failure does not block startup. The README artwork is stored in [`assets/`](assets/).

### Music credits

Optional background music: **“電脳眠眠猫 ins.” (DENNO MIENMIEN MAO, instrumental)**. Music, lyrics and mixing by **なみぐる (namigroove)**. The original song also has vocal versions by 桃寝ちのい (Momone Chinoi) and 可不 (KAFU); `ins.` denotes the instrumental version.

- [Instrumental and usage terms published by the composer (piapro)](https://piapro.jp/t/21O9)
- [Full original production credits](https://nekomo.info/works/myenmao/)

The local playback file is `data/music/電脳眠眠猫short.mp3`. Audio is excluded from the source repository and default releases. Attribution does not change the creator’s usage terms; the composer’s instrumental page specifies noncommercial use only.

### Download only the cache you need

The program archive supports all five systems. Cache releases are separate and named `cache-YYYY.MM.DD.N`; they do not require switching source branches.

| Download | When needed |
| --- | --- |
| `xdebx-v<VERSION>-linux-amd64.tar.gz` | Program and configured fonts |
| `xdebx-cache-<SNAPSHOT>-common.tar.gz` | Shared optional small cache |
| `xdebx-cache-<SNAPSHOT>-<TARGET>.tar.gz` | Optional cache for your system only |

`<SNAPSHOT>` is the date and sequence after the `cache-` prefix. `<TARGET>` is `ubuntu20`, `ubuntu22`, `ubuntu24`, `debian12` or `debian13`. For example, Ubuntu 24.04 users import the common archive and the `ubuntu24` archive **from the same snapshot**, when those attachments are present. Other systems' caches are unnecessary. Omitted groups have no bundled assets. The full cache is optional; unresolved dependencies still require network access.

Cache releases also provide `resources.json`, checksums, third-party notices and corresponding-source attachments where required. Publish cache snapshots separately from program version tags; cache releases should not be marked Latest. Existing release assets must retain their original bytes.

## Development

Run checks from a source checkout:

```sh
python3 -m unittest discover -s tests -v
# Real installation and recovery checks in disposable local containers
python3 tools/test_recovery_matrix.py
```

Build a local program and cache release into an empty directory:

```sh
python3 tools/build_release.py \
  --repository XLYing23333/XdebX \
  --output dist/local
```

Contributions are welcome: include the distribution, desktop context and relevant logs with a bug report; add focused regression coverage when changing installation or recovery behavior.

## Documentation

[Cache guide](data/pkgs/README.md): small-cache policy and on-demand packages.
