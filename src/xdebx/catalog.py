"""One catalogue for CLI, TUI, scheduling and acceptance tests."""
from dataclasses import dataclass, field


@dataclass
class Package:
    id: str
    name: str
    command: str
    apt: list = field(default_factory=list)
    resource: str = ""
    desktop: bool = False
    dependencies: list = field(default_factory=list)
    conflicts: list = field(default_factory=list)
    probe: list = field(default_factory=lambda: ["--version"])
    source: str = ""
    category: str = "tools"
    excluded_targets: list = field(default_factory=list)


def catalogue():
    packages = []
    for name in ("btop", "chafa", "curl", "fastfetch", "gdu", "git", "htop", "lsd", "rsync", "wget"):
        packages.append(Package(name, name, name, [name], name,
                                source={"chafa": "chafa", "git": "git", "curl": "curl",
                                        "rsync": "rsync", "wget": "wget", "htop": "htop",
                                        "btop": "btop", "gdu": "gdu", "lsd": "lsd", "fastfetch": "fastfetch"}[name]))
    packages += [
        Package("vim9", "Vim 9 + plugins", "vim", ["vim"], "vim", dependencies=["git"], source="vim9-source"),
        Package("zsh-omz", "Zsh + Oh My Zsh", "zsh", ["zsh"], "zsh", dependencies=["git"], source="zsh"),
        Package("tmux-tmuxp", "Tmux + tmuxp", "tmux", ["tmux", "tmuxp"], "tmux", dependencies=["git"], probe=["-V"], source="tmux"),
        Package("maple-mono-nf", "Maple Mono NF", "fc-match", ["fontconfig"], category="runtime"),
        Package("nodejs22-nvm", "Node.js 22 + NVM", "node", resource="nodejs", category="runtime"),
        Package("uv", "uv", "uv", resource="uv", category="runtime"),
        Package("miniconda3", "Miniconda3", "conda", resource="miniconda3", category="runtime"),
        Package("jdk", "JDK 21", "javac", ["openjdk-21-jdk"], "jdk-temurin21", category="runtime"),
        Package("go", "Go", "go", resource="go", probe=["version"], category="runtime"),
        Package("texlive", "TeX Live", "xelatex", ["texlive-latex-recommended", "texlive-plain-generic", "texlive-xetex", "texlive-lang-chinese"],
                resource="texlive-netinstaller", category="runtime"),
        Package("docker", "Docker", "docker", resource="docker-bootstrap", category="services"),
        Package("mysql", "MySQL", "mysql", resource="mysql-apt-config", category="services"),
        Package("redis", "Redis", "redis-server", ["redis-server"], source="redis", category="services"),
        Package("clashterminalx", "ClashTerminalX", "clashtx", resource="clashterminalx",
                dependencies=["uv"], conflicts=["clash-verge"], probe=["help"], category="network"),
        Package("clash-verge", "Clash Verge Rev", "clash-verge", resource="clash-verge-rev", desktop=True,
                conflicts=["clashterminalx"], category="network"),
        Package("gedit", "gedit", "gedit", ["gedit"], "gedit", desktop=True),
        Package("ghostty", "Ghostty", "ghostty", resource="ghostty-ubuntu", desktop=True),
        Package("google-chrome", "Google Chrome", "google-chrome", resource="chrome", desktop=True),
        Package("chatgpt-desktop", "ChatGPT Desktop (preview)", "chatgpt", resource="chatgpt", desktop=True),
        Package("spark-store", "Spark Store", "spark-store", resource="spark-store", desktop=True),
        Package("visual-studio-code", "Visual Studio Code", "code", resource="vscode", desktop=True),
        Package("wechat", "WeChat", "wechat", resource="wechat", desktop=True, excluded_targets=["ubuntu20"]),
        Package("qq", "QQ", "qq", resource="qq", desktop=True),
        Package("ctypora", "CTypora (local, unverified origin)", "typora", resource="CTypora", desktop=True),
        Package("vlc", "VLC", "vlc", ["vlc", "vlc-plugin-qt"], desktop=True),
        Package("nomachine", "NoMachine Personal (commercial trial)", "nxserver", resource="nomachine-personal-edition", desktop=True),
        Package("snipaste", "Snipaste", "snipaste", resource="snipaste", desktop=True),
        Package("claude-code", "Claude Code + DeepSeek", "claude", resource="@anthropic-ai/claude-code",
                dependencies=["nodejs22-nvm"], category="agents"),
        Package("opencode", "OpenCode", "opencode", resource="opencode-ai",
                dependencies=["nodejs22-nvm"], category="agents"),
        Package("openclaw", "OpenClaw", "openclaw", resource="openclaw",
                dependencies=["nodejs22-nvm"], category="agents"),
    ]
    return {p.id: p for p in sorted(packages, key=lambda p: p.name.lower())}


CATALOG = catalogue()


def resolve(ids):
    ordered, visiting, visited = [], set(), set()
    def visit(key):
        if key not in CATALOG:
            raise ValueError("Unknown software: " + key)
        if key in visiting:
            raise ValueError("Dependency cycle: " + key)
        if key in visited:
            return
        visiting.add(key)
        for dep in CATALOG[key].dependencies:
            visit(dep)
        visiting.remove(key)
        visited.add(key)
        ordered.append(key)
    for key in ids:
        visit(key)
    for key in ordered:
        for conflict in CATALOG[key].conflicts:
            if conflict in visited:
                raise ValueError("Mutually exclusive: {} / {}".format(key, conflict))
    return ordered
