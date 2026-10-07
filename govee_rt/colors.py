"""Small color helpers shared by visuals, effects and the CLI."""

import colorsys


def hsv(h, s, v):
    r, g, b = colorsys.hsv_to_rgb(h % 1.0, max(0, min(1, s)), max(0, min(1, v)))
    return int(r * 255), int(g * 255), int(b * 255)


def gamma(v, g=2.2):
    return max(0.0, min(1.0, v)) ** g
