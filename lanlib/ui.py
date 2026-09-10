"""Small immediate-mode widget kit and a text cache.

Nothing here is general-purpose; it exists so the menus, lobby and shop can be
written as a few lines each.  The text cache matters more than it looks: naive
per-frame ``font.render`` calls are one of the few things that will genuinely
cost a Pi 400 its frame rate.
"""

from __future__ import annotations

import pygame

from .theme import (UI_ACCENT, UI_DIM, UI_PANEL, UI_PANEL_HI, UI_PANEL_LO,
                    UI_SHADOW, UI_TEXT, shade)

_FONTS: dict[int, pygame.font.Font] = {}
_TEXT_CACHE: dict[tuple, pygame.Surface] = {}
_CACHE_LIMIT = 900


def font(size: int) -> pygame.font.Font:
    """pygame's bundled font, so text is identical on Windows and Raspbian."""
    got = _FONTS.get(size)
    if got is None:
        got = pygame.font.Font(None, size)
        _FONTS[size] = got
    return got


def text(msg: str, size: int = 16, color=UI_TEXT) -> pygame.Surface:
    key = (msg, size, color)
    surf = _TEXT_CACHE.get(key)
    if surf is None:
        if len(_TEXT_CACHE) > _CACHE_LIMIT:
            _TEXT_CACHE.clear()
        surf = font(size).render(msg, False, color)
        _TEXT_CACHE[key] = surf
    return surf


def draw_text(dest: pygame.Surface, msg: str, x: int, y: int, size: int = 16,
              color=UI_TEXT, anchor: str = "topleft",
              shadow: bool = False) -> pygame.Rect:
    surf = text(msg, size, color)
    rect = surf.get_rect(**{anchor: (x, y)})
    if shadow:
        dest.blit(text(msg, size, UI_SHADOW), (rect.x + 1, rect.y + 1))
    dest.blit(surf, rect)
    return rect


def draw_panel(dest: pygame.Surface, rect, fill=UI_PANEL, bevel: bool = True) -> None:
    """A flat panel with a one-pixel bevel -- the whole UI vocabulary."""
    rect = pygame.Rect(rect)
    dest.fill(fill, rect)
    if not bevel:
        return
    pygame.draw.line(dest, UI_PANEL_HI, rect.topleft, (rect.right - 1, rect.top))
    pygame.draw.line(dest, UI_PANEL_HI, rect.topleft, (rect.left, rect.bottom - 1))
    pygame.draw.line(dest, UI_PANEL_LO, (rect.left, rect.bottom - 1),
                     (rect.right - 1, rect.bottom - 1))
    pygame.draw.line(dest, UI_PANEL_LO, (rect.right - 1, rect.top),
                     (rect.right - 1, rect.bottom - 1))


def draw_bar(dest: pygame.Surface, rect, fraction: float, color,
             back=(28, 28, 36)) -> None:
    rect = pygame.Rect(rect)
    dest.fill(back, rect)
    fraction = max(0.0, min(1.0, fraction))
    width = int(rect.width * fraction)
    if width > 0:
        dest.fill(color, pygame.Rect(rect.x, rect.y, width, rect.height))
        dest.fill(shade(color, 1.35),
                  pygame.Rect(rect.x, rect.y, width, 1))
    pygame.draw.rect(dest, UI_PANEL_LO, rect, 1)


class Button:
    """A rectangle that knows whether the pointer is on it."""

    def __init__(self, rect, label: str, action: str = "", size: int = 18,
                 enabled: bool = True, hidden: bool = False) -> None:
        self.rect = pygame.Rect(rect)
        self.label = label
        self.action = action or label.lower()
        self.size = size
        self.enabled = enabled
        #: A hidden button is a click target only -- used where the screen
        #: already draws its own richer artwork for the row, as the shop does.
        self.hidden = hidden
        self.hot = False

    def update(self, mouse) -> None:
        self.hot = self.enabled and self.rect.collidepoint(mouse)

    def draw(self, dest: pygame.Surface) -> None:
        if self.hidden:
            return
        if not self.enabled:
            fill, fg = shade(UI_PANEL, 0.7), UI_DIM
        elif self.hot:
            fill, fg = UI_PANEL_HI, UI_ACCENT
        else:
            fill, fg = UI_PANEL, UI_TEXT
        draw_panel(dest, self.rect, fill)
        draw_text(dest, self.label, self.rect.centerx, self.rect.centery,
                  self.size, fg, anchor="center")

    def clicked(self, pos) -> bool:
        return self.enabled and self.rect.collidepoint(pos)


class TextInput:
    """One-line text field. Deliberately minimal -- names and IP addresses."""

    def __init__(self, rect, value: str = "", limit: int = 24,
                 allowed: str | None = None) -> None:
        self.rect = pygame.Rect(rect)
        self.value = value
        self.limit = limit
        self.allowed = allowed
        self.focused = False
        self._blink = 0

    def handle(self, event) -> bool:
        """Returns True when the user pressed Enter."""
        if event.type == pygame.MOUSEBUTTONDOWN:
            self.focused = self.rect.collidepoint(event.pos)
            return False
        if not self.focused or event.type != pygame.KEYDOWN:
            return False
        if event.key == pygame.K_BACKSPACE:
            self.value = self.value[:-1]
        elif event.key in (pygame.K_RETURN, pygame.K_KP_ENTER):
            return True
        elif event.unicode and event.unicode.isprintable():
            if len(self.value) < self.limit:
                if self.allowed is None or event.unicode in self.allowed:
                    self.value += event.unicode
        return False

    def draw(self, dest: pygame.Surface, label: str = "") -> None:
        self._blink = (self._blink + 1) % 60
        draw_panel(dest, self.rect, shade(UI_PANEL, 0.75))
        if label:
            draw_text(dest, label, self.rect.x, self.rect.y - 13, 15, UI_DIM)
        shown = self.value
        surf = text(shown, 17, UI_TEXT)
        # Scroll the field rather than letting long input escape the box.
        offset = max(0, surf.get_width() - (self.rect.width - 10))
        dest.set_clip(self.rect.inflate(-4, -2))
        dest.blit(surf, (self.rect.x + 5 - offset, self.rect.y + 4))
        if self.focused and self._blink < 32:
            cursor_x = self.rect.x + 5 - offset + surf.get_width()
            pygame.draw.line(dest, UI_ACCENT, (cursor_x, self.rect.y + 4),
                             (cursor_x, self.rect.bottom - 5))
        dest.set_clip(None)
        pygame.draw.rect(dest, UI_ACCENT if self.focused else UI_PANEL_LO,
                         self.rect, 1)


def clear_caches() -> None:
    _TEXT_CACHE.clear()
