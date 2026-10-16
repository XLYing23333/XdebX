"""Curses-native 3D backdrop adapted from the supplied outputs/xdebx.py.

Geometry and lighting are independent of terminal state. The UI owns colors,
compositing and refresh; no ANSI escapes or second terminal loop are used.
"""
import curses
import math
import os
import time


FONT = {
    "X": ["10001", "10001", "01010", "00100", "01010", "10001", "10001"],
    "d": ["00001", "00001", "01101", "10011", "10001", "10011", "01101"],
    "e": ["00000", "00000", "01110", "10001", "11111", "10000", "01111"],
    "b": ["10000", "10000", "10110", "11001", "10001", "11001", "10110"],
}


def build_mesh():
    cells = set()
    for i, char in enumerate("XdebX"):
        for row, line in enumerate(FONT[char]):
            for col, value in enumerate(line):
                if value == "1":
                    cells.add((i * 6 + col, 6 - row))
    vertices, lookup, faces = [], {}, []

    def add(points, normal, hue_position):
        indices = []
        for x, y, z in points:
            point = (x - 14.5, y - 3.5, z)
            if point not in lookup:
                lookup[point] = len(vertices)
                vertices.append(point)
            indices.append(lookup[point])
        faces.append((indices, normal, hue_position))

    for x, y in sorted(cells):
        a, b = -0.85, 0.85
        add([(x, y, a), (x + 1, y, a), (x + 1, y + 1, a), (x, y + 1, a)], (0, 0, -1), x)
        add([(x, y, b), (x, y + 1, b), (x + 1, y + 1, b), (x + 1, y, b)], (0, 0, 1), x)
        if (x - 1, y) not in cells:
            add([(x, y, a), (x, y + 1, a), (x, y + 1, b), (x, y, b)], (-1, 0, 0), x)
        if (x + 1, y) not in cells:
            add([(x + 1, y, a), (x + 1, y, b), (x + 1, y + 1, b), (x + 1, y + 1, a)], (1, 0, 0), x)
        if (x, y - 1) not in cells:
            add([(x, y, a), (x, y, b), (x + 1, y, b), (x + 1, y, a)], (0, -1, 0), x)
        if (x, y + 1) not in cells:
            add([(x, y + 1, a), (x + 1, y + 1, a), (x + 1, y + 1, b), (x, y + 1, b)], (0, 1, 0), x)
    return vertices, faces


VERTICES, FACES = build_mesh()

BRAKE_AT = 10.0
STOP_AT = 19.0
FADE_DURATION = 5.0
ENTRANCE_DURATION = 1.0


def motion_pose(t):
    """One forward turn, braking after 10s and parked at the front by 19s.

    Angular speed falls quadratically to zero; its integral lands exactly on
    a full turn. Tilt and drift smoothly fade out over the same interval.
    All times include the one-second fragment entrance.
    """
    t = max(0.0, t)
    if t >= STOP_AT:
        return 2 * math.pi, 0.0, 0.0, 0.0, 0.0
    duration = STOP_AT - BRAKE_AT
    speed = 2 * math.pi / (BRAKE_AT - ENTRANCE_DURATION + duration / 3)
    motion = max(0.0, t - ENTRANCE_DURATION)
    if t <= BRAKE_AT:
        yaw, envelope = speed * motion, 1.0
    else:
        u = (t - BRAKE_AT) / duration
        yaw = speed * (BRAKE_AT - ENTRANCE_DURATION + duration * (u - u * u + u ** 3 / 3))
        envelope = 1 - u * u * (3 - 2 * u)
    return (yaw, 0.20 * math.sin(motion * 0.819) * envelope,
            0.13 * math.sin(motion * 0.559) * envelope,
            0.065 * math.sin(motion * 0.37) * envelope,
            0.085 * math.sin(motion * 0.51) * envelope)


def rotation(t):
    yaw, pitch, roll, _, _ = motion_pose(t)
    cy, sy = math.cos(yaw), math.sin(yaw)
    cx, sx = math.cos(pitch), math.sin(pitch)
    cz, sz = math.cos(roll), math.sin(roll)

    def apply(point):
        x, y, z = point
        x, z = cy * x + sy * z, -sy * x + cy * z
        y, z = cx * y - sx * z, sx * y + cx * z
        return cz * x - sz * y, sz * x + cz * y, z

    return apply


def raster_triangle(a, b, c, width, height, depths, pixels, material):
    ax, ay, az = a
    bx, by, bz = b
    cx, cy, cz = c
    denominator = (by - cy) * (ax - cx) + (cx - bx) * (ay - cy)
    if abs(denominator) < 1e-8:
        return
    left = max(0, math.floor(min(ax, bx, cx)))
    right = min(width - 1, math.ceil(max(ax, bx, cx)))
    top = max(0, math.floor(min(ay, by, cy)))
    bottom = min(height - 1, math.ceil(max(ay, by, cy)))
    inv = 1 / denominator
    for y in range(top, bottom + 1):
        py = y + 0.5
        for x in range(left, right + 1):
            px = x + 0.5
            u = ((by - cy) * (px - cx) + (cx - bx) * (py - cy)) * inv
            v = ((cy - ay) * (px - cx) + (ax - cx) * (py - cy)) * inv
            w = 1 - u - v
            if min(u, v, w) < -1e-7:
                continue
            depth = u * az + v * bz + w * cz
            index = y * width + x
            if depth > depths[index]:
                depths[index] = depth
                pixels[index] = material



LEVELS = 6
# Fixed xterm colors: neutral silver faces and blue extruded sides. Keep
# brightness levels distinct instead of quantizing every face to one dark hue.
PALETTE = (238, 241, 244, 247, 249, 252, 17, 18, 25, 26, 33, 39)
COLOR_COUNT = len(PALETTE)
MONO_SHADES = str.maketrans("░▒▓█", "·░░▒")


def render_frame(width, height, t):
    """Return nonempty (row, column, text, material) spans for a bounded canvas."""
    if width <= 0 or height <= 0:
        return []
    t = min(max(0.0, t), STOP_AT)
    rotate = rotation(t)
    world = [rotate(point) for point in VERTICES]
    distance = 44.0
    focal = min(width * 1.30, height * 6.0)
    _, _, _, drift_x, drift_y = motion_pose(t)
    drift_x, drift_y = width * drift_x, height * drift_y
    projected = [(width / 2 + drift_x + x * focal / (z + distance),
                  height / 2 + drift_y - y * focal / (2 * (z + distance)),
                  1 / (z + distance)) for x, y, z in world]
    depths = [0.0] * (width * height)
    pixels = [None] * (width * height)
    for indices, normal, position in FACES:
        nx, ny, nz = rotate(normal)
        center = tuple(sum(world[i][axis] for i in indices) / 4 for axis in range(3))
        if nx * center[0] + ny * center[1] + nz * (center[2] + distance) >= 0:
            continue
        light = max(0.0, nx * -0.34 + ny * 0.47 + nz * -0.81)
        brightness = min(1.0, 0.28 + 0.72 * light)
        silver = normal[2] != 0
        if silver:
            # A soft moving reflection across the silver caps makes lighting
            # visible even near the readable, almost flat orientation.
            brightness = max(0.0, min(1.0, brightness + 0.10 * math.sin(position * 0.28 - t * 0.7)))
        shade = "░▒▓█"[min(3, int(brightness * 4))]
        color = (0 if silver else LEVELS) + min(LEVELS - 1, int(brightness * LEVELS))
        material = (shade, color)
        points = [projected[i] for i in indices]
        raster_triangle(points[0], points[1], points[2], width, height, depths, pixels, material)
        raster_triangle(points[0], points[2], points[3], width, height, depths, pixels, material)
    spans = []
    for y in range(height):
        row = pixels[y * width:(y + 1) * width]
        x = 0
        while x < width:
            if row[x] is None:
                x += 1
                continue
            start, color, chars = x, row[x][1], []
            while x < width and row[x] is not None and row[x][1] == color:
                chars.append(row[x][0])
                x += 1
            spans.append((y, start, "".join(chars), color))
    return spans


def assemble_frame(spans, width, height, progress):
    """Move deterministic 4×2 character fragments into place in one second."""
    if progress >= 1:
        return spans
    remaining = (1 - max(0.0, progress)) ** 3
    result = []
    for y, x, text, material in spans:
        offset = 0
        while offset < len(text):
            column = x + offset
            tile_x, tile_y = column // 4, y // 2
            length = min(len(text) - offset, 4 - column % 4)
            seed = (tile_x * 73856093) ^ (tile_y * 19349663)
            origin_x = (seed * 1664525 + 1013904223) % max(1, width)
            origin_y = (seed * 1103515245 + 12345) % max(1, height)
            dx = round((origin_x - tile_x * 4) * remaining)
            dy = round((origin_y - tile_y * 2) * remaining)
            new_x, new_y = column + dx, y + dy
            piece = text[offset:offset + length]
            if 0 <= new_y < height:
                if new_x < 0:
                    piece, new_x = piece[-new_x:], 0
                piece = piece[:max(0, width - new_x)]
                if piece:
                    result.append((new_y, new_x, piece, material))
            offset += length
    return result


def fade_opacity(elapsed):
    progress = min(1.0, max(0.0, (elapsed - STOP_AT) / FADE_DURATION))
    return 1.0 - progress * progress * (3.0 - 2.0 * progress)


def fade_frame(spans, opacity):
    """Dim and thin fixed pixels, without moving or randomly flickering them."""
    if opacity >= 1:
        return spans
    if opacity <= 0:
        return []
    result = []
    shades = "░▒▓█"
    for y, x, text, material in spans:
        base, level = divmod(material, LEVELS)
        faded_material = base * LEVELS + round(level * opacity)
        for offset, char in enumerate(text):
            column = x + offset
            seed = (((column * 73856093) ^ (y * 19349663)) * 2654435761) & 0xffffffff
            if seed / 4294967296.0 >= opacity:
                continue
            char = shades[round(shades.index(char) * opacity)] if char in shades else char
            if result and result[-1][0] == y and result[-1][1] + len(result[-1][2]) == column and result[-1][3] == faded_material:
                row, start, previous, color = result[-1]
                result[-1] = (row, start, previous + char, color)
            else:
                result.append((y, column, char, faded_material))
    return result


class Background:
    """Low-contrast, ten-fps animation, painted before all foreground widgets."""
    def __init__(self):
        self.started = time.monotonic()
        self.frame_key, self.spans = None, []
        self.attributes = [curses.A_DIM] * COLOR_COUNT
        self.entrance_started = None

    def start_entrance(self):
        self.started = self.entrance_started = time.monotonic()
        self.frame_key = None

    def configure_colors(self, background=-1):
        if not curses.has_colors() or "NO_COLOR" in os.environ:
            return
        # Pairs 1..4 belong to the foreground. Never redefine terminal colors.
        available = max(0, curses.COLOR_PAIRS - 5)
        if curses.COLORS >= 256 and available >= COLOR_COUNT:
            definitions = [(color, 0) for color in PALETTE]
            mapping = list(range(COLOR_COUNT))
        elif available >= 3:
            definitions = [(curses.COLOR_WHITE, curses.A_DIM),
                           (curses.COLOR_WHITE, 0), (curses.COLOR_BLUE, curses.A_BOLD)]
            mapping = [0] * 3 + [1] * 3 + [2] * LEVELS
        elif available >= 2:
            definitions = [(curses.COLOR_WHITE, curses.A_DIM), (curses.COLOR_BLUE, curses.A_BOLD)]
            mapping = [0] * LEVELS + [1] * LEVELS
        elif available:
            definitions = [(curses.COLOR_WHITE, curses.A_DIM)]
            mapping = [0] * COLOR_COUNT
        else:
            return
        for index, (color, attribute) in enumerate(definitions):
            try:
                curses.init_pair(index + 5, color, background)
                attribute |= curses.color_pair(index + 5)
            except curses.error:
                attribute = curses.A_DIM
            for material, pair in enumerate(mapping):
                if pair == index:
                    self.attributes[material] = attribute

    def draw(self, screen):
        rows, columns = screen.getmaxyx()
        if rows < 12 or columns < 38:
            return
        # Keep rasterization cheap even on oversized terminals. Center the
        # canvas in the content area, leaving the title/footer/border clear.
        width, height = min(columns - 4, 160), min(rows - 7, 48)
        left, top = (columns - width) // 2, 3 + (rows - 7 - height) // 2
        now = time.monotonic()
        elapsed = max(0.0, now - self.started)
        opacity = fade_opacity(elapsed)
        if opacity <= 0:
            return
        entrance = self.entrance_started is not None and now - self.entrance_started < ENTRANCE_DURATION
        animation_time = min(elapsed, STOP_AT)
        tick = int(animation_time * 10)
        key = (width, height, tick)
        if key != self.frame_key:
            self.spans = render_frame(width, height, tick / 10)
            self.frame_key = key
        spans = assemble_frame(self.spans, width, height, now - self.entrance_started) if entrance else self.spans
        spans = fade_frame(spans, opacity)
        for y, x, text, material in spans:
            attribute = self.attributes[material]
            if attribute & curses.A_DIM:
                # Sparse shading stays unobtrusive even if the terminal
                # ignores the dim attribute (common on older terminals).
                text = text.translate(MONO_SHADES)
            try:
                screen.addstr(top + y, left + x, text, attribute)
            except curses.error:
                # A resize or an unsupported glyph must not stop installation.
                pass
