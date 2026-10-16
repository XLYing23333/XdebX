"""Explicit resource-specific mirrors. URLs retain the same version and checksum."""
CN_BASES = (
    ('https://archive.ubuntu.com/ubuntu', 'https://mirrors.tuna.tsinghua.edu.cn/ubuntu'),
    ('https://security.ubuntu.com/ubuntu', 'https://mirrors.tuna.tsinghua.edu.cn/ubuntu'),
    ('https://deb.debian.org/debian-security', 'https://mirrors.tuna.tsinghua.edu.cn/debian-security'),
    ('https://deb.debian.org/debian', 'https://mirrors.tuna.tsinghua.edu.cn/debian'),
    ('https://download.docker.com/linux', 'https://mirrors.tuna.tsinghua.edu.cn/docker-ce/linux'),
    ('https://registry.npmjs.org', 'https://registry.npmmirror.com'),
    ('https://pypi.org/simple', 'https://pypi.tuna.tsinghua.edu.cn/simple'),
    ('https://nodejs.org/dist', 'https://npmmirror.com/mirrors/node'),
    ('https://mirror.ctan.org', 'https://mirrors.tuna.tsinghua.edu.cn/CTAN'),
)


def cn_url(url):
    for original, mirror in CN_BASES:
        if url == original or url.startswith(original + '/'):
            return mirror + url[len(original):]
    return url


def original_url(url):
    for original, mirror in CN_BASES:
        if url == mirror or url.startswith(mirror + '/'):
            return original + url[len(mirror):]
    return url
