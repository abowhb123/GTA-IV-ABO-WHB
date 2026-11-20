"""أبو وهب — المبرمج الخطير لترجمة قراند يامعلم.

يترجم نصوص GTA VI (الحوارات، الجوال، القوائم، الشروحات) للعربي فوق الشاشة.
يشتغل فوق نافذة PS Remote Play أو chiaki-ng على البي سي.
اختصارات: F6 بلاغ مع صورة — F8 تشغيل/إيقاف — F9 مكان الترجمة — F10 إخفاء/إظهار.
"""
import ctypes
import json
import logging
import logging.handlers
import os
import shutil
import sys
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from difflib import SequenceMatcher

import mss
from PIL import Image, ImageOps
from PySide6.QtCore import QObject, QRect, Qt, QTimer, Signal
from PySide6.QtGui import (
    QColor, QCursor, QFont, QFontDatabase, QFontMetrics, QGuiApplication, QIcon, QImage,
    QPainter, QPainterPath, QPen, QPixmap, QTextOption,
)
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QColorDialog, QComboBox, QDialog, QDoubleSpinBox, QFormLayout,
    QFrame, QGridLayout, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QMessageBox,
    QPlainTextEdit, QPushButton, QScrollArea, QSlider, QVBoxLayout, QWidget,
)

import ocr_win
from translators import ENGINES, Corrections, Translator

new_mss = getattr(mss, "MSS", None) or mss.mss

APP_NAME = "أبو وهب"
APP_TAGLINE = "المبرمج الخطير لترجمة قراند يامعلم"
GAME_NAME = "GTA VI"
VERSION = "2.2"
IS_WIN = sys.platform == "win32"

# ---------------- الألوان ----------------
C_BG = "#120e1c"        # ليل فايس سيتي
C_CARD = "#1c1629"
C_CHIP = "#261e37"
C_LINE = "#3a2f52"
C_PINK = "#ff4f9a"      # غروب: وردي ← برتقالي
C_ORANGE = "#ffa24a"
C_PURPLE = "#8b6cff"
C_TEXT = "#f3eefa"
C_MUTED = "#a69cba"
C_OK = "#3ee8a0"
C_WARN = "#ffcc4d"
C_TEAL = C_PINK           # أسماء قديمة يستخدمها باقي الكود
C_TEAL_LIGHT = C_ORANGE

FONTS = [  # (الاسم في القائمة، اسم العائلة، ملفات)
    ("Alexandria", "Alexandria", ["Alexandria.ttf"]),
    ("Cairo", "Cairo", ["Cairo.ttf"]),
    ("Tajawal", "Tajawal", ["Tajawal-Regular.ttf", "Tajawal-Bold.ttf"]),
    ("IBM Plex Sans Arabic", "IBM Plex Sans Arabic", ["IBMPlexSansArabic-Regular.ttf", "IBMPlexSansArabic-Bold.ttf"]),
    ("Noto Naskh Arabic", "Noto Naskh Arabic", ["NotoNaskhArabic.ttf"]),
]

STYLE_PRESETS = {
    "original": {"label": "الأصلي", "font": "Alexandria", "bold": True, "size": 100,
                 "color": "#FFFFFF", "bg": "#0b0f12", "bg_opacity": 78},
    "cinema": {"label": "سينما", "font": "Cairo", "bold": True, "size": 110,
               "color": "#FFE14D", "bg": "#000000", "bg_opacity": 0},
    "calm": {"label": "هادئ", "font": "Tajawal", "bold": False, "size": 100,
             "color": "#DDDDDD", "bg": "#1b2238", "bg_opacity": 60},
}
TEXT_COLORS = ["#FFFFFF", "#FFE14D", "#DDDDDD", "#7FD8FF"]
BG_COLORS = ["#0b0f12", "#000000", "#1b2238", "#3a3a3a"]

# أماكن افتراضية (نسبة من صورة اللعبة): الحوار أسفل الوسط، والمربعات الإضافية تبدأ أعلى اليسار
DEFAULT_DIALOG = {"x": 0.14, "y": 0.80, "w": 0.72, "h": 0.18}
DEFAULT_EXTRA = [
    {"x": 0.015, "y": 0.04, "w": 0.34, "h": 0.24},
    {"x": 0.62, "y": 0.04, "w": 0.36, "h": 0.22},
    {"x": 0.30, "y": 0.40, "w": 0.40, "h": 0.20},
]


def box_name(i):
    return "مربع الحوار" if i == 0 else f"مربع إضافي {i}"


def resource_path(*parts):
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, *parts)


def data_path(name):
    if getattr(sys, "frozen", False):
        base = os.path.dirname(sys.executable)
    else:
        base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, name)


DEFAULTS = {
    "boxes": [dict(DEFAULT_DIALOG)],   # الأول مربع الحوار، والباقي مربعات إضافية
    "auto": True,              # ترجمة الجوال والقوائم وأي نص في صورة اللعبة
    "noise_filter": True,      # تجاهل لوحات الشوارع والكتابات الدخيلة
    "style": dict(STYLE_PRESETS["original"], preset="original"),
    "engine": "google",
    "threshold": 175,
    "interval": 0.25,
    "auto_interval": 0.6,
    "stable": True,
    "show_english": False,
    "autostart": True,
}


_logger = None


def log(msg):
    """سجل بسيط في log.txt جنب البرنامج — يساعد في معرفة سبب أي مشكلة."""
    global _logger
    try:
        if _logger is None:
            _logger = logging.getLogger("abuwahab")
            _logger.setLevel(logging.INFO)
            h = logging.handlers.RotatingFileHandler(data_path("log.txt"), maxBytes=400_000,
                                                     backupCount=1, encoding="utf-8")
            h.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%H:%M:%S"))
            _logger.addHandler(h)
        _logger.info(msg)
    except Exception:  # noqa: BLE001
        pass


def load_settings():
    s = json.loads(json.dumps(DEFAULTS))
    try:
        with open(data_path("settings.json"), encoding="utf-8") as f:
            saved = json.load(f)
        if str(saved.get("version", "")).startswith("2"):
            for k, v in saved.items():
                if isinstance(v, dict) and isinstance(s.get(k), dict):
                    s[k].update(v)
                else:
                    s[k] = v
        if "dialog_box" in saved and "boxes" not in saved:  # ترقية من 2.0
            s["boxes"] = [saved["dialog_box"]] + ([saved["second_box"]] if saved.get("second_enabled") else [])
    except Exception:  # noqa: BLE001
        pass
    for k in ("dialog_box", "second_box", "second_enabled"):
        s.pop(k, None)
    if not s.get("boxes"):
        s["boxes"] = [dict(DEFAULT_DIALOG)]
    s["version"] = VERSION
    return s


def save_settings(s):
    try:
        with open(data_path("settings.json"), "w", encoding="utf-8") as f:
            json.dump(s, f, ensure_ascii=False, indent=2)
    except Exception:  # noqa: BLE001
        pass


# ---------------- ويندوز ----------------

def win_make_overlay(hwnd: int) -> bool:
    """يجعل نافذة الترجمة لا تستقبل الماوس، ومستبعدة من تصوير الشاشة (حتى لا يقرأها البرنامج)."""
    if not IS_WIN:
        return False
    user32 = ctypes.windll.user32
    GWL_EXSTYLE = -20
    WS_EX_TRANSPARENT, WS_EX_TOOLWINDOW, WS_EX_NOACTIVATE = 0x20, 0x80, 0x08000000
    try:
        style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        user32.SetWindowLongW(hwnd, GWL_EXSTYLE,
                              style | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE)
    except Exception:  # noqa: BLE001
        pass
    try:
        return bool(user32.SetWindowDisplayAffinity(hwnd, 0x11))  # WDA_EXCLUDEFROMCAPTURE
    except Exception:  # noqa: BLE001
        return False


def win_keep_on_top(hwnd: int):
    if IS_WIN:
        try:
            ctypes.windll.user32.SetWindowPos(hwnd, -1, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010)
        except Exception:  # noqa: BLE001
            pass


GAME_TITLES = (("chiaki", "chiaki-ng"), ("ps remote play", "PS Remote Play"), ("remote play", "PS Remote Play"))


def find_game_window():
    """يلقى نافذة chiaki-ng أو PS Remote Play ويرجع مكانها بالبكسل الفعلي."""
    if not IS_WIN:
        return None
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    found = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, _):
        if not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd):
            return True
        n = user32.GetWindowTextLengthW(hwnd)
        if n <= 0:
            return True
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, buf, n + 1)
        title = buf.value.lower()
        if APP_NAME in buf.value:
            return True
        for key, label in GAME_TITLES:
            if key in title:
                rc = wintypes.RECT()
                user32.GetClientRect(hwnd, ctypes.byref(rc))
                pt = wintypes.POINT(0, 0)
                user32.ClientToScreen(hwnd, ctypes.byref(pt))
                w, h = rc.right - rc.left, rc.bottom - rc.top
                if w > 300 and h > 200:
                    found.append({"left": pt.x, "top": pt.y, "width": w, "height": h, "app": label})
                break
        return True

    try:
        user32.EnumWindows(cb, 0)
    except Exception:  # noqa: BLE001
        return None
    return max(found, key=lambda r: r["width"] * r["height"]) if found else None


def game_video_rect(win):
    """الصورة داخل نافذة البث 16:9 مع أشرطة سوداء أحياناً — نحسب مكان الصورة نفسها."""
    w, h = win["width"], win["height"]
    if w / h > 16 / 9:
        vw, vh = round(h * 16 / 9), h
    else:
        vw, vh = w, round(w * 9 / 16)
    return {"left": win["left"] + (w - vw) // 2, "top": win["top"] + (h - vh) // 2, "width": vw, "height": vh}


def detect_game():
    """يرجع (مكان صورة اللعبة، اسم البرنامج أو None)."""
    win = find_game_window()
    if win:
        return game_video_rect(win), win["app"]
    screen = QGuiApplication.primaryScreen()
    return game_video_rect(dict(monitor_for_screen(screen, monitors(refresh=True)))), None


def frac_to_phys(frac, game):
    return {"left": game["left"] + round(frac["x"] * game["width"]),
            "top": game["top"] + round(frac["y"] * game["height"]),
            "width": max(20, round(frac["w"] * game["width"])),
            "height": max(12, round(frac["h"] * game["height"]))}


# ---------------- الشاشات: ربط شاشات Qt بشاشات mss ----------------

_MONITORS = []


def monitors(refresh=False):
    global _MONITORS
    if refresh or not _MONITORS:
        with new_mss() as sct:
            _MONITORS = [dict(m) for m in sct.monitors]
    return _MONITORS


def monitor_for_screen(screen, mons):
    geo, dpr = screen.geometry(), screen.devicePixelRatio()
    w, h = round(geo.width() * dpr), round(geo.height() * dpr)
    px, py = geo.x() * dpr, geo.y() * dpr
    cands = [m for m in mons[1:] if abs(m["width"] - w) <= 2 and abs(m["height"] - h) <= 2]
    if not cands:
        cands = mons[1:]
    return min(cands, key=lambda m: abs(m["left"] - px) + abs(m["top"] - py))


def phys_to_logical(rect):
    """يحوّل مستطيل بالبكسل الفعلي إلى (شاشة Qt، مستطيل Qt، نسبة التكبير)."""
    mons = monitors()
    cx = rect["left"] + rect["width"] / 2
    cy = rect["top"] + rect["height"] / 2
    for screen in QGuiApplication.screens():
        m = monitor_for_screen(screen, mons)
        if m["left"] <= cx < m["left"] + m["width"] and m["top"] <= cy < m["top"] + m["height"]:
            geo, dpr = screen.geometry(), screen.devicePixelRatio()
            x = geo.x() + (rect["left"] - m["left"]) / dpr
            y = geo.y() + (rect["top"] - m["top"]) / dpr
            return screen, QRect(round(x), round(y), round(rect["width"] / dpr), round(rect["height"] / dpr)), dpr
    s = QGuiApplication.primaryScreen()
    dpr = s.devicePixelRatio()
    return s, QRect(round(rect["left"] / dpr), round(rect["top"] / dpr),
                    round(rect["width"] / dpr), round(rect["height"] / dpr)), dpr


def rect_of(r):
    return {k: int(r[k]) for k in ("left", "top", "width", "height")}


def overlap_ratio(a, b):
    x1, y1 = max(a["left"], b["left"]), max(a["top"], b["top"])
    x2 = min(a["left"] + a["width"], b["left"] + b["width"])
    y2 = min(a["top"] + a["height"], b["top"] + b["height"])
    inter = max(0, x2 - x1) * max(0, y2 - y1)
    return inter / max(1, min(a["width"] * a["height"], b["width"] * b["height"]))


def grab(sct, region, masks=()) -> Image.Image:
    """يصوّر المنطقة، ويغطي نوافذ البرنامج نفسه (عشان ما يقرأ «GTA VI» و«متصل» من واجهته)."""
    shot = sct.grab(rect_of(region))
    img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
    if masks:
        from PIL import ImageDraw
        d = ImageDraw.Draw(img)
        for m in masks:
            x1, y1 = m["left"] - region["left"], m["top"] - region["top"]
            x2, y2 = x1 + m["width"], y1 + m["height"]
            if x2 > 0 and y2 > 0 and x1 < img.width and y1 < img.height:
                d.rectangle([x1, y1, x2, y2], fill=(0, 0, 0))
    return img


def logical_to_phys(widget):
    """مكان نافذة Qt بالبكسل الفعلي."""
    screen = widget.screen() or QGuiApplication.primaryScreen()
    m = monitor_for_screen(screen, monitors())
    geo, dpr = screen.geometry(), screen.devicePixelRatio()
    g = widget.frameGeometry()
    return {"left": m["left"] + round((g.x() - geo.x()) * dpr), "top": m["top"] + round((g.y() - geo.y()) * dpr),
            "width": round(g.width() * dpr), "height": round(g.height() * dpr)}


def pil_to_pixmap(img: Image.Image) -> QPixmap:
    img = img.convert("RGB")
    qimg = QImage(img.tobytes(), img.width, img.height, img.width * 3, QImage.Format_RGB888)
    return QPixmap.fromImage(qimg.copy())


def similar(a, b, ratio=0.88):
    return bool(a and b) and SequenceMatcher(None, a.lower(), b.lower()).ratio() > ratio


# ---------------- رسم الترجمة (مشترك بين الشاشة والمعاينة والصور) ----------------

def style_fonts(style, px):
    f = QFont(style.get("font", "Alexandria"))
    f.setPixelSize(max(10, int(px)))
    f.setWeight(QFont.Bold if style.get("bold", True) else QFont.Normal)
    fe = QFont("Segoe UI")
    fe.setPixelSize(max(10, int(px * 0.55)))
    return f, fe


def layout_box(style, ar, en, px, anchor: QRect, max_w, show_en, bounds: QRect, place="center"):
    """يحسب مربع الترجمة: عرضه على قد النص، ومكانه فوق النص الأصلي."""
    f, fe = style_fonts(style, px)
    pad_x, pad_y = max(8, int(px * 0.45)), max(4, int(px * 0.22))
    fm = QFontMetrics(f)
    natural = fm.horizontalAdvance(ar) + pad_x * 2 + 4
    width = int(min(max(natural, px * 3), max_w))
    tw = width - pad_x * 2
    h = fm.boundingRect(QRect(0, 0, tw, 4000), Qt.TextWordWrap, ar).height()
    if show_en and en:
        h += QFontMetrics(fe).boundingRect(QRect(0, 0, tw, 4000), Qt.TextWordWrap, en).height() + 4
    height = h + pad_y * 2
    x = anchor.center().x() - width // 2
    y = anchor.top() - height - 6 if place == "above" else anchor.center().y() - height // 2
    x = min(max(x, bounds.left()), bounds.right() - width)
    y = min(max(y, bounds.top()), bounds.bottom() - height)
    return QRect(x, y, width, height), (pad_x, pad_y)


def draw_box(p: QPainter, style, rect: QRect, pads, ar, en, px, show_en):
    f, fe = style_fonts(style, px)
    pad_x, pad_y = pads
    op = int(style.get("bg_opacity", 78))
    if op > 0:
        bg = QColor(style.get("bg", "#0b0f12"))
        bg.setAlpha(int(op * 2.55))
        path = QPainterPath()
        radius = min(10, rect.height() / 3)
        path.addRoundedRect(rect.adjusted(0, 0, -1, -1), radius, radius)
        p.fillPath(path, bg)
    inner = rect.adjusted(pad_x, pad_y, -pad_x, -pad_y)
    opt = QTextOption(Qt.AlignHCenter | Qt.AlignVCenter)
    opt.setWrapMode(QTextOption.WordWrap)
    opt.setTextDirection(Qt.RightToLeft)
    ar_rect = inner
    if show_en and en:
        eh = QFontMetrics(fe).boundingRect(QRect(0, 0, inner.width(), 4000), Qt.TextWordWrap, en).height()
        ar_rect = inner.adjusted(0, 0, 0, -(eh + 4))
        eopt = QTextOption(Qt.AlignHCenter)
        eopt.setWrapMode(QTextOption.WordWrap)
        eopt.setTextDirection(Qt.LeftToRight)
        p.setFont(fe)
        p.setPen(QColor(210, 210, 210, 230))
        p.drawText(QRect(inner.left(), inner.bottom() - eh, inner.width(), eh), en, eopt)
    p.setFont(f)
    # حدود للنص: قوية لما الخلفية شفافة (ستايل سينما)، وظل خفيف غيرها
    outline = 2 if op < 35 else 1
    p.setPen(QColor(0, 0, 0, 230 if op < 35 else 170))
    for dx in range(-outline, outline + 1):
        for dy in range(-outline, outline + 1):
            if dx or dy:
                p.drawText(ar_rect.translated(dx, dy), ar, opt)
    p.setPen(QColor(style.get("color", "#FFFFFF")))
    p.drawText(ar_rect, ar, opt)


# ---------------- طبقة الترجمة فوق اللعبة (نافذة شفافة لكل شاشة) ----------------

class ScreenOverlay(QWidget):
    def __init__(self, screen, manager):
        super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool
                         | Qt.WindowTransparentForInput | Qt.WindowDoesNotAcceptFocus)
        self.m = manager
        self.screen_ref = screen
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setGeometry(screen.geometry())
        self.show()
        self.excluded = win_make_overlay(int(self.winId()))
        self.hide()
        self.boxes = []  # [(rect, pads, item, px)]

    def layout_items(self, items):
        s = self.m.s
        style = s["style"]
        geo = self.geometry()
        bounds = QRect(0, 0, geo.width(), geo.height())
        show_en = s.get("show_english")
        place = "center" if self.excluded else "above"
        scale = style.get("size", 100) / 100
        boxes, placed = [], []
        # الحوار أول، وبعده نصوص الجوال والقوائم من فوق لتحت
        order = sorted(items, key=lambda it: (it["kind"] == "auto", it["logical"].top()))
        for it in order:
            anchor = it["logical"].translated(-geo.x(), -geo.y())
            if it["kind"] == "auto":
                rect, pads, px = self.fit_auto(style, it, anchor, scale, show_en, bounds, place, placed)
            else:
                game_h = it.get("game_h") or geo.height()
                px = game_h * 0.028 * scale
                max_w = max(it.get("area_w") or anchor.width(), px * 12)
                rect, pads = layout_box(style, it["ar"], it["en"], px, anchor, min(max_w, geo.width() - 8),
                                        show_en, bounds, place)
            placed.append(rect)
            boxes.append((rect, pads, it, px))
        self.boxes = boxes
        if boxes and not self.m.user_hidden:
            if not self.isVisible():
                self.show()
            win_keep_on_top(int(self.winId()))
        elif not boxes:
            self.hide()
        self.update()

    @staticmethod
    def fit_auto(style, it, anchor, scale, show_en, bounds, place, placed):
        """ترجمة الجوال/القوائم: على قد الفقاعة الأصلية، وتصغر لين ما تتداخل مع اللي جنبها."""
        lh = it.get("lh") or anchor.height()
        px = min(max(lh * 0.85, 11), 40) * scale
        max_w = max(anchor.width() * 1.12, 90)
        max_h = anchor.height() * 1.25 + 6
        while True:
            rect, pads = layout_box(style, it["ar"], it["en"], px, anchor, max_w, show_en, bounds, place)
            hits = [r for r in placed if r.intersects(rect)]
            if (rect.height() <= max_h and not hits) or px <= 10:
                break
            px -= 1
        for r in placed:  # لو للحين متداخل: نزّله تحت اللي قبله
            if r.intersects(rect):
                rect.moveTop(r.bottom() + 3)
        return rect, pads, px

    def paintEvent(self, _):
        if self.boxes:
            p = QPainter(self)
            self.draw(p)

    def draw(self, p):
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.TextAntialiasing)
        style = self.m.s["style"]
        show_en = self.m.s.get("show_english")
        for rect, pads, it, px in self.boxes:
            draw_box(p, style, rect, pads, it["ar"], it["en"], px, show_en)


class OverlayManager(QObject):
    def __init__(self, settings):
        super().__init__()
        self.s = settings
        self.items = {}
        self.overlays = {}
        self.user_hidden = False
        self.timer = QTimer(self, interval=1500, timeout=self._keep_top)
        self.timer.start()
        self.prepare(QGuiApplication.primaryScreen())

    def prepare(self, screen):
        key = screen.name()
        if key not in self.overlays:
            self.overlays[key] = ScreenOverlay(screen, self)
        return self.overlays[key]

    @property
    def excluded(self):
        return all(o.excluded for o in self.overlays.values())

    def update_item(self, key, info, en, ar, kind):
        screen, logical, dpr = phys_to_logical(info)
        self.prepare(screen)
        self.items[key] = {
            "logical": logical, "screen": screen.name(), "en": en, "ar": ar, "kind": kind,
            "lh": (info.get("lh") or 0) / dpr, "game_h": (info.get("game_h") or 0) / dpr,
            "area_w": (info.get("area_w") or 0) / dpr,
        }
        self.relayout()

    def clear_item(self, key):
        if self.items.pop(key, None) is not None:
            self.relayout()

    def clear_all(self):
        self.items.clear()
        self.relayout()

    def replace_text(self, en, ar):
        for it in self.items.values():
            if it["en"] == en:
                it["ar"] = ar
        self.relayout()

    def relayout(self):
        for name, ov in self.overlays.items():
            ov.layout_items([it for it in self.items.values() if it["screen"] == name])

    def set_hidden(self, hidden):
        self.user_hidden = hidden
        for ov in self.overlays.values():
            if hidden:
                ov.hide()
        if not hidden:
            self.relayout()

    def snapshot(self):
        """صورة للشاشة مع الترجمة مرسومة فوقها (لأن الترجمة مخفية عن تصوير ويندوز)."""
        target = next((ov for ov in self.overlays.values() if ov.boxes), None)
        screen = target.screen_ref if target else (QGuiApplication.screenAt(QCursor.pos())
                                                   or QGuiApplication.primaryScreen())
        mon = monitor_for_screen(screen, monitors(refresh=True))
        with new_mss() as sct:
            img = pil_to_pixmap(grab(sct, mon)).toImage()
        if target:
            p = QPainter(img)
            p.scale(img.width() / target.width(), img.height() / target.height())
            target.draw(p)
            p.end()
        return img

    def _keep_top(self):
        for ov in self.overlays.values():
            if ov.isVisible():
                win_keep_on_top(int(ov.winId()))


# ---------------- المحرك: تصوير ← قراءة ← ترجمة ----------------

class TranslatePool:
    """يترجم أكثر من نص بنفس الوقت بدون ما يوقف القراءة."""

    def __init__(self, translator, workers=4):
        self.translator = translator
        self.ex = ThreadPoolExecutor(max_workers=workers)

    def submit(self, text, done):
        fut = self.ex.submit(self.translator.translate, text)

        def _cb(f):
            try:
                done(f.result(), None)
            except Exception as e:  # noqa: BLE001
                done(None, e)
        fut.add_done_callback(_cb)

    def shutdown(self):
        self.ex.shutdown(wait=False, cancel_futures=True)


class RegionTracker:
    """يتابع مربع الحوار (أو المربع الثاني): يقرأه، يتأكد من النص، ويترجم الجديد فقط."""

    def __init__(self, key, region, game):
        self.key = key
        self.region = region
        self.game = game
        self.last_sig = b""
        self.last_ocr = 0.0
        self.last_text = ""
        self.pending = ""
        self.pending_since = 0.0
        self.pending_box = None
        self.showing = False
        self.empty_since = None
        self.version = 0

    def _read(self, img, scale):
        lines = sorted(ocr_win.recognize_boxes(img), key=lambda l: (l[1][1], l[1][0]))
        text = ocr_win.clean_text([t for t, _ in lines])
        box = None
        if lines:
            x1 = min(r[0] for _, r in lines)
            y1 = min(r[1] for _, r in lines)
            x2 = max(r[0] + r[2] for _, r in lines)
            y2 = max(r[1] + r[3] for _, r in lines)
            box = {"left": self.region["left"] + int(x1 / scale), "top": self.region["top"] + int(y1 / scale),
                   "width": max(1, int((x2 - x1) / scale)), "height": max(1, int((y2 - y1) / scale))}
        return text, box

    def step(self, sct, eng):
        s = eng.s
        now = time.time()
        raw = grab(sct, self.region, eng.masks)
        scale = ocr_win.scale_for(raw)
        img = ocr_win.preprocess(raw, "color", int(s["threshold"]))
        sig = ocr_win.signature(img)
        unchanged = self.last_sig and ocr_win.sig_distance(sig, self.last_sig) < 0.004
        if unchanged and not self.pending and now - self.last_ocr < 1.0:
            return
        self.last_sig = sig
        if unchanged and self.pending:
            text, box = self.pending, self.pending_box
        else:
            self.last_ocr = now
            text, box = self._read(img, scale)
        min_score = 0.7 if s.get("noise_filter", True) else 0.5
        if not ocr_win.is_real_text(text, min_score) or (s.get("noise_filter", True) and ocr_win.is_button_hint(text)):
            if text and text != getattr(self, "_last_skip", ""):
                self._last_skip = text
                log(f"[{self.key}] skipped: {text[:80]}")
            self.pending = ""
            self.empty_since = self.empty_since or now
            if self.showing and now - self.empty_since > 0.8:
                self.version += 1
                eng.cleared.emit(self.key)
                self.showing, self.last_text = False, ""
            return
        self.empty_since = None
        if similar(text, self.last_text):
            self.pending = ""
            return
        if s.get("stable", True):
            confirmed = similar(text, self.pending, 0.9)
            waited = self.pending and now - self.pending_since > 0.5
            if not confirmed and not waited:
                if not self.pending:
                    self.pending_since = now
                self.pending, self.pending_box = text, box
                return
        self.pending = ""
        self.last_text = text
        self.version += 1
        v = self.version
        info = dict(box or self.region, game_h=self.game["height"], area_w=self.region["width"])
        log(f"[{self.key}] EN: {text}")

        def done(ar, err):
            if err is not None:
                log(f"  ERROR: {err}")
                eng.status.emit(str(err)[:160] if "جوجل" in str(err) else f"تعذرت الترجمة: {err}"[:160])
                if self.version == v:
                    self.last_text = ""
                return
            log(f"  AR: {ar}")
            if self.version == v:
                self.showing = True
                eng.updated.emit(self.key, info, text, ar, "dialog")
        eng.pool.submit(text, done)


class AutoTracker:
    """يمسح صورة اللعبة كلها: يلقى كتل النص (الجوال، القوائم، الشروحات) ويترجم كل كتلة في مكانها."""

    MAX_NEW = 5

    def __init__(self, game):
        self.game = game
        self.items = {}
        self.counter = 0

    def _match(self, rect, used):
        best, score = None, 0.3
        for k, it in self.items.items():
            if k in used:
                continue
            o = overlap_ratio(rect, it["rect"])
            if o > score:
                best, score = k, o
        return best

    def step(self, sct, eng):
        s = eng.s
        now = time.time()
        g = self.game
        img = ImageOps.grayscale(grab(sct, g, eng.masks))
        blocks = ocr_win.group_blocks(ocr_win.recognize_boxes(img))
        exclude = [r for _, r in eng.region_rects]
        strict = s.get("noise_filter", True)
        used, sent = set(), 0
        for raw, (x, y, bw, bh), nlines in blocks:
            text = ocr_win.clean_text([raw])
            if len(text.split()) < 2 or not ocr_win.is_real_text(text, 0.75 if strict else 0.55):
                continue
            if strict and ocr_win.is_button_hint(text):
                continue  # أزرار التحكم (SELECT، BACK، SCROLL)
            rect = {"left": g["left"] + int(x) - 3, "top": g["top"] + int(y) - 2,
                    "width": int(bw) + 6, "height": int(bh) + 4}
            if any(overlap_ratio(rect, r) > 0.3 for r in exclude):
                continue  # مربع الحوار يتابعه بنفسه
            key = self._match(rect, used)
            if key is None:
                # نفس الجملة مقطّعة؟ لو النص جزء من ترجمة معروضة، نتجاهله
                if any(it["text"] and text in it["text"] and overlap_ratio(rect, it["rect"]) > 0.1
                       for it in self.items.values()):
                    continue
                self.counter += 1
                key = f"auto:{self.counter}"
                self.items[key] = {"rect": rect, "pending": text, "text": "", "seen": now, "v": 0,
                                   "lh": bh / max(nlines, 1)}
                used.add(key)
                if not s.get("stable", True) and not strict:
                    self.items[key]["pending"] = ""
                    self._translate(key, self.items[key], text, eng)
                continue
            used.add(key)
            it = self.items[key]
            moved = overlap_ratio(rect, it["rect"]) < 0.85
            it["rect"], it["seen"], it["lh"] = rect, now, bh / max(nlines, 1)
            if similar(text, it["text"]):
                continue
            if strict and moved:
                it["pending"] = text  # النص يتحرك مع الكاميرا = غالباً لوحة في الشارع
                continue
            if (s.get("stable", True) or strict) and not similar(text, it["pending"], 0.9):
                it["pending"] = text
                continue
            if sent >= self.MAX_NEW:
                continue
            sent += 1
            it["pending"] = ""
            self._translate(key, it, text, eng)
        for key in [k for k, it in self.items.items() if now - it["seen"] > 1.2]:
            if self.items[key]["text"]:
                eng.cleared.emit(key)
            del self.items[key]

    def _translate(self, key, it, text, eng):
        it["text"] = text
        it["v"] += 1
        v = it["v"]
        log(f"[auto] EN: {text}")

        def done(ar, err):
            cur = self.items.get(key)
            if err is not None:
                log(f"  ERROR: {err}")
                if cur is not None and cur["v"] == v:
                    cur["text"] = ""
                return
            log(f"  AR: {ar}")
            if cur is not None and cur["v"] == v:
                eng.updated.emit(key, dict(cur["rect"], lh=cur["lh"]), text, ar, "auto")
        eng.pool.submit(text, done)


class Engine(QObject):
    """يشغّل مربعات الحوار والمسح التلقائي كل واحد على خيط مستقل."""

    updated = Signal(str, dict, str, str, str)
    cleared = Signal(str)
    status = Signal(str)
    failed = Signal(str)

    def __init__(self, settings, translator, game):
        super().__init__()
        self.s = settings
        self.pool = TranslatePool(translator)
        self.game = game
        self._running = False
        self.generation = 0
        self.threads = []
        self.region_rects = []
        self.masks = []  # نوافذ البرنامج الظاهرة (تتحدث من الواجهة)

    def _compute_regions(self):
        self.region_rects = [(f"box:{i}", frac_to_phys(b, self.game)) for i, b in enumerate(self.s["boxes"])]

    def start(self):
        self._running = True
        self._spawn()

    def _spawn(self):
        self.generation += 1
        g = self.generation
        self._compute_regions()
        self.threads = [threading.Thread(target=self._loop, args=("regions", g), daemon=True),
                        threading.Thread(target=self._loop, args=("auto", g), daemon=True)]
        for t in self.threads:
            t.start()

    def rebuild(self, game=None):
        if game:
            self.game = game
        if self._running:
            self._spawn()

    def stop(self):
        self._running = False
        self.generation += 1
        for t in self.threads:
            t.join(timeout=3)
        self.pool.shutdown()

    def _alive(self, g):
        return self._running and g == self.generation

    def _trackers(self, which):
        if which == "regions":
            return [RegionTracker(k, r, self.game) for k, r in self.region_rects]
        return [AutoTracker(dict(self.game))] if self.s.get("auto") else []

    def _loop(self, which, g):
        trackers = self._trackers(which)
        if not trackers:
            return
        err_count = 0
        try:
            with new_mss() as sct:
                while self._alive(g):
                    t0 = time.time()
                    for tr in trackers:
                        if not self._alive(g):
                            break
                        try:
                            tr.step(sct, self)
                            err_count = 0
                        except ocr_win.OcrNotAvailable as e:
                            self.failed.emit(str(e))
                            return
                        except Exception as e:  # noqa: BLE001
                            err_count += 1
                            log(f"ERROR {which}: {e}")
                            self.status.emit(f"خطأ مؤقت: {e}"[:160])
                            if err_count > 5:
                                time.sleep(2)
                    key = "interval" if which == "regions" else "auto_interval"
                    time.sleep(max(0.02, float(self.s.get(key, 0.3)) - (time.time() - t0)))
        except Exception:  # noqa: BLE001
            msg = traceback.format_exc()[-600:]
            log(msg)
            self.failed.emit(msg)


# ---------------- الاختصارات العامة ----------------

class Hotkeys(QObject):
    toggle = Signal()
    select = Signal()
    hide = Signal()
    shot = Signal()

    def start(self):
        try:
            from pynput import keyboard
            self._l = keyboard.GlobalHotKeys({
                "<f6>": self.shot.emit,
                "<f8>": self.toggle.emit,
                "<f9>": self.select.emit,
                "<f10>": self.hide.emit,
            })
            self._l.daemon = True
            self._l.start()
            return True
        except Exception:  # noqa: BLE001
            return False


# ---------------- الواجهة: الستايل العام ----------------

APP_STYLE = f"""
QWidget {{ background:{C_BG}; color:{C_TEXT}; font-family:'Alexandria'; font-size:13px; }}
QLabel {{ background:transparent; }}
QFrame#card {{ background:{C_CARD}; border:1px solid {C_LINE}; border-radius:20px; }}
QFrame#row {{ background:{C_CARD}; border:1px solid {C_LINE}; border-radius:16px; }}
QFrame#row:hover {{ border-color:{C_ORANGE}; }}
QLabel#title {{ font-size:26px; font-weight:bold; color:#ffffff; }}
QLabel#tagline {{ color:{C_ORANGE}; font-size:12px; }}
QLabel#game {{ color:#ffffff; font-size:14px; font-weight:bold; border-radius:12px; padding:5px 14px;
               background:qlineargradient(x1:0,y1:0,x2:1,y2:0, stop:0 {C_ORANGE}, stop:1 {C_PINK}); }}
QLabel#pill {{ background:{C_CHIP}; border:1px solid {C_LINE}; border-radius:11px; padding:4px 10px; color:{C_TEXT}; }}
QLabel#muted {{ color:{C_MUTED}; }}
QLabel#rowtitle {{ color:#ffffff; font-size:14px; font-weight:bold; }}
QLabel#rowsub {{ color:{C_MUTED}; font-size:11px; }}
QLabel#rowicon {{ background:{C_CHIP}; border-radius:12px; font-size:18px; min-width:42px; max-width:42px;
                  min-height:42px; max-height:42px; }}
QLabel#chev {{ color:{C_MUTED}; font-size:20px; }}
QLabel#h2 {{ color:#ffffff; font-size:17px; font-weight:bold; }}
QLabel#state {{ color:{C_MUTED}; font-size:14px; font-weight:bold; }}
QLabel#last {{ background:#0c0914; border:1px solid {C_LINE}; border-radius:14px; padding:10px; }}
QPushButton {{ background:{C_CHIP}; border:1px solid {C_LINE}; border-radius:14px; padding:9px 14px; color:{C_TEXT}; }}
QPushButton:hover {{ border-color:{C_ORANGE}; }}
QPushButton:checked {{ border:2px solid {C_ORANGE}; color:#ffffff; }}
QPushButton:disabled {{ color:#5d5470; }}
QPushButton#teal {{ border:none; border-radius:18px; color:#ffffff; font-size:16px; font-weight:bold; padding:13px;
                    background:qlineargradient(x1:0,y1:0,x2:1,y2:0, stop:0 {C_ORANGE}, stop:1 {C_PINK}); }}
QPushButton#link {{ background:transparent; border:none; color:{C_ORANGE}; text-decoration:underline; padding:4px; }}
QPushButton#icon {{ border-radius:16px; min-width:32px; max-width:32px; min-height:32px; max-height:32px; padding:0; }}
QPushButton#swatch {{ border-radius:22px; min-width:44px; max-width:44px; min-height:44px; max-height:44px; padding:0; }}
QSlider::groove:horizontal {{ height:6px; background:{C_CHIP}; border-radius:3px; }}
QSlider::sub-page:horizontal {{ background:qlineargradient(x1:0,y1:0,x2:1,y2:0, stop:0 {C_ORANGE}, stop:1 {C_PINK}); border-radius:3px; }}
QSlider::handle:horizontal {{ background:#ffffff; width:22px; height:22px; margin:-8px 0; border-radius:11px; }}
QComboBox, QDoubleSpinBox {{ background:{C_CHIP}; border:1px solid {C_LINE}; border-radius:8px; padding:4px 8px; min-height:26px; }}
QComboBox QAbstractItemView {{ background:{C_CHIP}; selection-background-color:{C_PINK}; }}
QListWidget, QPlainTextEdit {{ background:#0c0914; border:1px solid {C_LINE}; border-radius:12px; padding:4px; }}
QListWidget::item {{ padding:6px; border-bottom:1px solid {C_CHIP}; }}
QListWidget::item:selected {{ background:#3a1f3f; color:#ffffff; }}
QCheckBox::indicator {{ width:18px; height:18px; border:1px solid {C_ORANGE}; border-radius:4px; background:{C_CHIP}; }}
QCheckBox::indicator:checked {{ background:{C_ORANGE}; }}
QScrollArea {{ border:none; }}
"""


class Switch(QPushButton):
    """مفتاح تشغيل/إطفاء صغير."""

    def __init__(self, checked=False):
        super().__init__()
        self.setCheckable(True)
        self.setChecked(checked)
        self.setFixedSize(48, 26)
        self.setStyleSheet("QPushButton { background:transparent; border:none; }")
        self.setCursor(Qt.PointingHandCursor)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        on = self.isChecked()
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(C_PINK if on else "#3d3352"))
        p.drawRoundedRect(QRect(0, 2, 48, 22), 11, 11)
        p.setBrush(QColor("#ffffff"))
        p.drawEllipse(QRect(4 if on else 26, 4, 18, 18))


class Row(QFrame):
    """سطر في قائمة الإعدادات: أيقونة + عنوان + وصف + (مفتاح أو سهم)."""

    clicked = Signal()

    def __init__(self, icon, title, sub="", switch=None):
        super().__init__(objectName="row")
        self.setCursor(Qt.PointingHandCursor)
        self.setMinimumHeight(56)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 6, 14, 6)
        lay.setSpacing(12)
        ic = QLabel(icon, objectName="rowicon", alignment=Qt.AlignCenter)
        lay.addWidget(ic)
        txt = QVBoxLayout()
        txt.setSpacing(1)
        self.lbl_title = QLabel(title, objectName="rowtitle")
        self.lbl_sub = QLabel(sub, objectName="rowsub", wordWrap=True)
        txt.addWidget(self.lbl_title)
        txt.addWidget(self.lbl_sub)
        lay.addLayout(txt, 1)
        self.switch = switch
        if switch is not None:
            lay.addWidget(switch)
        else:
            lay.addWidget(QLabel("‹", objectName="chev"))

    def set_sub(self, t):
        self.lbl_sub.setText(t)

    def mouseReleaseEvent(self, e):
        if self.switch is not None:
            self.switch.toggle()
        else:
            self.clicked.emit()
        super().mouseReleaseEvent(e)


class PowerButton(QPushButton):
    """زر التشغيل الدائري الكبير."""

    def __init__(self):
        super().__init__()
        self.setFixedSize(132, 132)
        self.running = False
        self.setCursor(Qt.PointingHandCursor)
        self.setStyleSheet("QPushButton { background:transparent; border:none; }")

    def set_running(self, v):
        self.running = v
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = self.rect().adjusted(6, 6, -6, -6)
        if self.running:
            from PySide6.QtGui import QConicalGradient
            g = QConicalGradient(r.center(), 90)
            g.setColorAt(0, QColor(C_ORANGE))
            g.setColorAt(0.5, QColor(C_PINK))
            g.setColorAt(1, QColor(C_ORANGE))
            p.setPen(QPen(g, 6))
        else:
            p.setPen(QPen(QColor(C_LINE), 6))
        p.setBrush(QColor(C_CARD))
        p.drawEllipse(r)
        # رمز التشغيل
        c = r.center()
        col = QColor("#ffffff" if self.running else C_MUTED)
        p.setPen(QPen(col, 7, Qt.SolidLine, Qt.RoundCap))
        p.setBrush(Qt.NoBrush)
        arc = QRect(c.x() - 26, c.y() - 22, 52, 52)
        p.drawArc(arc, 120 * 16, -240 * 16)
        p.drawLine(c.x(), c.y() - 30, c.x(), c.y() + 2)


# ---------------- تحديد مكان قراءة الترجمة (إطار يتحرك فوق اللعبة) ----------------

class FrameEditor(QWidget):
    """كل أماكن القراءة فوق اللعبة: تسحب أي مربع، تكبّره من الزوايا، تضيف مربعات أو تحذفها."""

    done = Signal()
    HANDLE = 14
    MAX_BOXES = 6

    def __init__(self, settings, game, select=0):
        super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.s = settings
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_DeleteOnClose)
        self.screen_, game_l, _ = phys_to_logical(game)
        sg = self.screen_.geometry()
        self.setGeometry(sg)
        self.game_l = game_l.translated(-sg.x(), -sg.y())
        self.frames = [self.to_rect(b) for b in self.s["boxes"]]
        self.sel = min(select, len(self.frames) - 1)
        self.drag = None
        self.setMouseTracking(True)
        self.build_card()

    def to_rect(self, fr):
        g = self.game_l
        return QRect(round(g.x() + fr["x"] * g.width()), round(g.y() + fr["y"] * g.height()),
                     round(fr["w"] * g.width()), round(fr["h"] * g.height()))

    def to_frac(self, f):
        g, f = self.game_l, f.normalized()
        return {"x": (f.x() - g.x()) / g.width(), "y": (f.y() - g.y()) / g.height(),
                "w": f.width() / g.width(), "h": f.height() / g.height()}

    def build_card(self):
        card = QFrame(self, objectName="card")
        card.setLayoutDirection(Qt.RightToLeft)
        card.setFixedWidth(400)
        lay = QVBoxLayout(card)
        lay.setContentsMargins(20, 16, 20, 16)
        lay.setSpacing(8)
        lay.addWidget(QLabel("🎯  أماكن قراءة الترجمة", objectName="h2"))
        lay.addWidget(QLabel("اسحب أي مربع فوق النص الإنجليزي، وكبّره أو صغّره من الزوايا."
                             " وهذي البطاقة نفسها تقدر تسحبها بعيد.", objectName="muted", wordWrap=True))
        self.lbl_sel = QLabel(objectName="pill")
        lay.addWidget(self.lbl_sel, alignment=Qt.AlignRight)
        row = QHBoxLayout()
        self.b_add = QPushButton("＋  مربع إضافي")
        self.b_add.clicked.connect(self.add_box)
        self.b_del = QPushButton("🗑  احذف المحدد")
        self.b_del.clicked.connect(self.del_box)
        row.addWidget(self.b_add)
        row.addWidget(self.b_del)
        lay.addLayout(row)
        b_reset = QPushButton("رجّع المكان الافتراضي", objectName="link")
        b_reset.clicked.connect(self.reset)
        lay.addWidget(b_reset, alignment=Qt.AlignCenter)
        b_ok = QPushButton("حفظ  ✓", objectName="teal")
        b_ok.clicked.connect(self.finish)
        lay.addWidget(b_ok)
        card.adjustSize()
        card.move((self.width() - card.width()) // 2, max(24, self.game_l.top() + int(self.game_l.height() * 0.3)))
        card.setCursor(Qt.OpenHandCursor)
        card.installEventFilter(self)
        self._card_drag = None
        self.card = card
        self.refresh_card()

    def eventFilter(self, obj, ev):
        """البطاقة تقدر تسحبها لأي مكان عشان ما تغطي المربعات."""
        if obj is getattr(self, "card", None):
            t = ev.type()
            if t == ev.Type.MouseButtonPress:
                self._card_drag = ev.globalPosition().toPoint() - self.card.pos()
                return True
            if t == ev.Type.MouseMove and self._card_drag is not None:
                self.card.move(ev.globalPosition().toPoint() - self._card_drag)
                return True
            if t == ev.Type.MouseButtonRelease:
                self._card_drag = None
                return True
        return super().eventFilter(obj, ev)

    def refresh_card(self):
        extra = len(self.frames) - 1
        self.lbl_sel.setText(f"المحدد: {box_name(self.sel)}   •   مربعات إضافية: {extra}")
        self.b_add.setEnabled(len(self.frames) < self.MAX_BOXES)
        self.b_del.setEnabled(self.sel > 0)
        self.card.adjustSize()
        self.update()

    def add_box(self):
        i = len(self.frames)
        fr = DEFAULT_EXTRA[(i - 1) % len(DEFAULT_EXTRA)]
        self.frames.append(self.to_rect(fr).translated(14 * ((i - 1) // len(DEFAULT_EXTRA)), 0))
        self.sel = i
        self.refresh_card()

    def del_box(self):
        if self.sel > 0:
            del self.frames[self.sel]
            self.sel = 0
            self.refresh_card()

    def reset(self):
        fr = DEFAULT_DIALOG if self.sel == 0 else DEFAULT_EXTRA[(self.sel - 1) % len(DEFAULT_EXTRA)]
        self.frames[self.sel] = self.to_rect(fr)
        self.update()

    def finish(self):
        self.s["boxes"] = [self.to_frac(f) for f in self.frames]
        save_settings(self.s)
        self.close()
        self.done.emit()

    def handles(self, f):
        f = f.normalized()
        pts = {"tl": f.topLeft(), "tr": f.topRight(), "bl": f.bottomLeft(), "br": f.bottomRight()}
        return {k: QRect(v.x() - self.HANDLE, v.y() - self.HANDLE, self.HANDLE * 2, self.HANDLE * 2)
                for k, v in pts.items()}

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.fillRect(self.rect(), QColor(10, 6, 20, 90))
        p.setCompositionMode(QPainter.CompositionMode_Source)
        for f in self.frames:
            p.fillRect(f.normalized(), QColor(0, 0, 0, 1))  # فتحات شفافة تستقبل السحب
        p.setCompositionMode(QPainter.CompositionMode_SourceOver)
        font = QFont("Alexandria")
        font.setPixelSize(14)
        font.setBold(True)
        p.setFont(font)
        for i, f in enumerate(self.frames):
            f = f.normalized()
            active = i == self.sel
            color = QColor(C_ORANGE if active else C_PURPLE)
            p.setPen(QPen(color, 3 if active else 2, Qt.SolidLine if active else Qt.DashLine))
            p.setBrush(Qt.NoBrush)
            p.drawRoundedRect(f, 6, 6)
            if active:
                p.setBrush(color)
                p.setPen(Qt.NoPen)
                for r in self.handles(f).values():
                    p.drawEllipse(r.center(), 7, 7)
            tag = QRect(f.right() - 130, f.top() - 26, 130, 22)
            p.setPen(Qt.NoPen)
            p.setBrush(color)
            p.drawRoundedRect(tag, 8, 8)
            p.setPen(QColor("#120e1c"))
            p.drawText(tag, Qt.AlignCenter, box_name(i))

    def hit(self, pos):
        f = self.frames[self.sel]
        for k, r in self.handles(f).items():
            if r.contains(pos):
                return self.sel, k
        for i in [self.sel] + [j for j in range(len(self.frames) - 1, -1, -1) if j != self.sel]:
            if self.frames[i].normalized().contains(pos):
                return i, "move"
        return None, None

    def mousePressEvent(self, e):
        pos = e.position().toPoint()
        i, kind = self.hit(pos)
        if i is not None:
            self.sel = i
            self.drag = (kind, pos, QRect(self.frames[i]))
            self.refresh_card()

    def mouseMoveEvent(self, e):
        pos = e.position().toPoint()
        if not self.drag:
            i, kind = self.hit(pos)
            if kind in ("tl", "br"):
                self.setCursor(Qt.SizeFDiagCursor)
            elif kind in ("tr", "bl"):
                self.setCursor(Qt.SizeBDiagCursor)
            elif kind == "move":
                self.setCursor(Qt.SizeAllCursor)
            else:
                self.setCursor(Qt.ArrowCursor)
            return
        kind, start, orig = self.drag
        d = pos - start
        f = QRect(orig)
        if kind == "move":
            f.translate(d)
        else:
            if "l" in kind:
                f.setLeft(orig.left() + d.x())
            if "r" in kind:
                f.setRight(orig.right() + d.x())
            if "t" in kind:
                f.setTop(orig.top() + d.y())
            if "b" in kind:
                f.setBottom(orig.bottom() + d.y())
        f = f.normalized()
        if f.width() >= 50 and f.height() >= 20:
            self.frames[self.sel] = f
        self.update()

    def mouseReleaseEvent(self, _):
        self.drag = None

    def keyPressEvent(self, e):
        if e.key() in (Qt.Key_Return, Qt.Key_Enter):
            self.finish()
        elif e.key() == Qt.Key_Escape:
            self.close()
            self.done.emit()
        elif e.key() == Qt.Key_Delete:
            self.del_box()


# ---------------- تخصيص شكل الترجمة ----------------

class StylePreview(QWidget):
    TEXT = "هذا شكل الترجمة على الشاشة"

    def __init__(self, style):
        super().__init__()
        self.style_ = style
        self.setMinimumHeight(150)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        path = QPainterPath()
        path.addRoundedRect(self.rect().adjusted(0, 0, -1, -1), 16, 16)
        p.fillPath(path, QColor("#000000"))
        px = 26 * self.style_.get("size", 100) / 100
        anchor = QRect(0, self.height() - 60, self.width(), 40)
        rect, pads = layout_box(self.style_, self.TEXT, "", px, anchor, self.width() - 20, False,
                                self.rect().adjusted(6, 6, -6, -6))
        draw_box(p, self.style_, rect, pads, self.TEXT, "", px, False)


class StyleDialog(QDialog):
    def __init__(self, parent, settings, on_change):
        super().__init__(parent)
        self.s = settings
        self.on_change = on_change
        self.orig = dict(settings["style"])
        self.st = settings["style"]
        self.setWindowTitle("شكل الترجمة")
        self.setLayoutDirection(Qt.RightToLeft)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        area = QScrollArea()
        area.setWidgetResizable(True)
        outer.addWidget(area)
        body = QWidget()
        area.setWidget(body)
        lay = QVBoxLayout(body)
        lay.setContentsMargins(22, 18, 22, 18)
        lay.setSpacing(10)
        lay.addWidget(QLabel("شكل الترجمة", objectName="h2"))
        lay.addWidget(QLabel("شوف التغيير على الشاشة مباشرة.", objectName="muted"))
        self.preview = StylePreview(self.st)
        lay.addWidget(self.preview)

        self.preset_btns = {}
        row = QHBoxLayout()
        for key, pr in STYLE_PRESETS.items():
            b = QPushButton(f"ع  {pr['label']}")
            b.setCheckable(True)
            b.clicked.connect(lambda _=False, k=key: self.apply_preset(k))
            self.preset_btns[key] = b
            row.addWidget(b)
        lay.addLayout(row)

        lay.addWidget(QLabel("الخط", objectName="h2"))
        grid = QGridLayout()
        self.font_btns = {}
        for i, (label, fam, _files) in enumerate(FONTS):
            b = QPushButton(label)
            b.setCheckable(True)
            f = QFont(fam)
            f.setPixelSize(14)
            f.setBold(True)
            b.setFont(f)
            b.clicked.connect(lambda _=False, fam=fam: self.setk("font", fam))
            self.font_btns[fam] = b
            grid.addWidget(b, i // 2, i % 2)
        lay.addLayout(grid)

        lay.addWidget(QLabel("سماكة الخط", objectName="h2"))
        row = QHBoxLayout()
        self.b_bold = QPushButton("عريض")
        self.b_norm = QPushButton("عادي")
        for b, v in ((self.b_bold, True), (self.b_norm, False)):
            b.setCheckable(True)
            b.clicked.connect(lambda _=False, v=v: self.setk("bold", v))
            row.addWidget(b)
        lay.addLayout(row)

        self.lbl_size = QLabel()
        lay.addLayout(self.slider_row("الحجم", self.lbl_size))
        self.sld_size = QSlider(Qt.Horizontal, minimum=60, maximum=170)
        self.sld_size.valueChanged.connect(lambda v: self.setk("size", v))
        lay.addWidget(self.sld_size)

        lay.addWidget(QLabel("لون النص", objectName="h2"))
        self.color_btns = self.swatches(lay, TEXT_COLORS, "color")
        lay.addWidget(QLabel("لون الخلفية", objectName="h2"))
        self.bg_btns = self.swatches(lay, BG_COLORS, "bg")

        self.lbl_op = QLabel()
        lay.addLayout(self.slider_row("شفافية الخلفية", self.lbl_op))
        self.sld_op = QSlider(Qt.Horizontal, minimum=0, maximum=100)
        self.sld_op.valueChanged.connect(lambda v: self.setk("bg_opacity", v))
        lay.addWidget(self.sld_op)

        b_reset = QPushButton("رجّع الأصلي", objectName="link")
        b_reset.clicked.connect(lambda: self.apply_preset("original"))
        lay.addWidget(b_reset, alignment=Qt.AlignCenter)
        b_save = QPushButton("احفظ  ←", objectName="teal")
        b_save.clicked.connect(self.save)
        lay.addWidget(b_save)
        self.resize(480, 860)
        self.refresh()

    @staticmethod
    def slider_row(title, value_lbl):
        row = QHBoxLayout()
        row.addWidget(QLabel(title, objectName="h2"))
        row.addStretch()
        value_lbl.setStyleSheet(f"color:{C_TEAL_LIGHT}; font-weight:bold;")
        row.addWidget(value_lbl)
        return row

    def swatches(self, lay, colors, key):
        row = QHBoxLayout()
        btns = {}
        for c in colors:
            b = QPushButton(objectName="swatch")
            b.setCheckable(True)
            b.setStyleSheet(f"QPushButton#swatch {{ background:{c}; border:1px solid {C_LINE}; }}"
                            f"QPushButton#swatch:checked {{ border:3px solid {C_TEAL_LIGHT}; }}")
            b.clicked.connect(lambda _=False, c=c: self.setk(key, c))
            btns[c] = b
            row.addWidget(b)
        row.addStretch()
        custom = QPushButton("●  لون مخصص")
        custom.clicked.connect(lambda: self.pick_color(key))
        row.addWidget(custom)
        lay.addLayout(row)
        btns["_custom"] = custom
        return btns

    def pick_color(self, key):
        c = QColorDialog.getColor(QColor(self.st.get(key, "#ffffff")), self, "اختر لون")
        if c.isValid():
            self.setk(key, c.name())

    def apply_preset(self, key):
        pr = STYLE_PRESETS[key]
        self.st.update({k: v for k, v in pr.items() if k != "label"})
        self.st["preset"] = key
        self.refresh()

    def setk(self, k, v):
        self.st[k] = v
        self.st["preset"] = "custom"
        self.refresh()

    def refresh(self):
        st = self.st
        for k, b in self.preset_btns.items():
            b.setChecked(st.get("preset") == k)
        for fam, b in self.font_btns.items():
            b.setChecked(st.get("font") == fam)
        self.b_bold.setChecked(bool(st.get("bold")))
        self.b_norm.setChecked(not st.get("bold"))
        for w, key in ((self.sld_size, "size"), (self.sld_op, "bg_opacity")):
            w.blockSignals(True)
            w.setValue(int(st.get(key, 100)))
            w.blockSignals(False)
        self.lbl_size.setText(f"{int(st.get('size', 100))}%")
        self.lbl_op.setText(f"{int(st.get('bg_opacity', 78))}%")
        for btns, key in ((self.color_btns, "color"), (self.bg_btns, "bg")):
            for c, b in btns.items():
                if c != "_custom":
                    b.setChecked(st.get(key, "").lower() == c.lower())
            btns["_custom"].setStyleSheet(f"QPushButton {{ color:{st.get(key)}; }}")
        self.preview.update()
        self.on_change()

    def save(self):
        save_settings(self.s)
        self.accept()

    def reject(self):
        self.s["style"].clear()
        self.s["style"].update(self.orig)
        self.on_change()
        super().reject()


# ---------------- نوافذ صغيرة: التصحيح، الملاحظات، إعدادات إضافية ----------------

class CorrectionsDialog(QDialog):
    def __init__(self, parent, translator, on_change):
        super().__init__(parent)
        self.tr_ = translator
        self.on_change = on_change
        self.setWindowTitle("عدّل الترجمة")
        self.setLayoutDirection(Qt.RightToLeft)
        self.resize(560, 620)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 16, 20, 16)
        lay.addWidget(QLabel("عدّل الترجمة", objectName="h2"))
        lay.addWidget(QLabel("اختر جملة من آخر الترجمات وصحّحها. البرنامج يتذكر تصحيحك دايم.",
                             objectName="muted", wordWrap=True))
        self.lst = QListWidget()
        for en, ar in reversed(translator.history):
            it = QListWidgetItem(f"{ar}\n{en}")
            it.setData(Qt.UserRole, en)
            self.lst.addItem(it)
        self.lst.currentItemChanged.connect(self.pick)
        lay.addWidget(self.lst, 3)
        self.lbl_en = QLabel("—", objectName="last", wordWrap=True)
        self.lbl_en.setLayoutDirection(Qt.LeftToRight)
        self.lbl_en.setAlignment(Qt.AlignLeft)
        lay.addWidget(self.lbl_en)
        self.ed_ar = QPlainTextEdit()
        self.ed_ar.setPlaceholderText("اكتب الترجمة الصحيحة هنا")
        self.ed_ar.setMaximumHeight(90)
        lay.addWidget(self.ed_ar)
        b_save = QPushButton("احفظ التصحيح", objectName="teal")
        b_save.clicked.connect(self.save_sentence)
        lay.addWidget(b_save)
        lay.addWidget(QLabel("استبدال كلمات في كل الترجمات (سطر لكل كلمة:  الغلط = الصح)",
                             objectName="muted", wordWrap=True))
        self.ed_rep = QPlainTextEdit()
        self.ed_rep.setPlainText("\n".join(f"{k} = {v}" for k, v in translator.corrections.replace.items()))
        self.ed_rep.setPlaceholderText("مثال:\nاركبي = اركب\nرجال الشرطة = الشرطة")
        lay.addWidget(self.ed_rep, 2)
        b_rep = QPushButton("احفظ الاستبدالات")
        b_rep.clicked.connect(self.save_replace)
        lay.addWidget(b_rep)
        self.lbl_msg = QLabel(objectName="muted")
        lay.addWidget(self.lbl_msg)
        if self.lst.count():
            self.lst.setCurrentRow(0)

    def pick(self, item, _prev=None):
        if not item:
            return
        en = item.data(Qt.UserRole)
        self.lbl_en.setText(en)
        self.ed_ar.setPlainText(self.tr_.corrections.lookup(en) or dict(self.tr_.history).get(en, ""))

    def save_sentence(self):
        item = self.lst.currentItem()
        if not item:
            return
        en, ar = item.data(Qt.UserRole), self.ed_ar.toPlainText().strip()
        self.tr_.corrections.set_sentence(en, ar)
        self.tr_.cache.clear()
        item.setText(f"{ar}\n{en}")
        self.on_change(en, ar)
        self.lbl_msg.setText("✔ تم حفظ التصحيح")

    def save_replace(self):
        rep = {}
        for line in self.ed_rep.toPlainText().splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                if k.strip():
                    rep[k.strip()] = v.strip()
        self.tr_.corrections.replace = rep
        self.tr_.corrections.save()
        self.tr_.cache.clear()
        self.lbl_msg.setText(f"✔ تم حفظ {len(rep)} استبدال")


NOTES = """
<h3>قبل ما تلعب</h3>
<ul>
<li>فعّل <b>الترجمة (Subtitles)</b> من إعدادات اللعبة، ولو فيه <b>خلفية للترجمة</b> فعّلها، تفرق كثير.</li>
<li>خلّ لغة اللعبة <b>إنجليزي</b>.</li>
<li>شغّل اللعب في <b>chiaki-ng</b> أو <b>PS Remote Play</b>، ويفضّل ملء الشاشة.</li>
<li>البي سي والسوني على نفس الراوتر، والأفضل البي سي بكيبل.</li>
</ul>
<h3>كل مرة تلعب</h3>
<ol>
<li>شغّل السوني، وافتح chiaki-ng أو Remote Play.</li>
<li>افتح «أبو وهب»، يلقى نافذة اللعب ويبدأ الترجمة لحاله.</li>
<li>لو ما اتصل، اضغط <b>تحديث</b>.</li>
</ol>
<h3>الأزرار السريعة</h3>
<ul>
<li><b>F8</b> تشغيل/إيقاف &nbsp; <b>F9</b> مكان الترجمة &nbsp; <b>F10</b> إخفاء/إظهار</li>
<li><b>F6</b> بلاغ: يحفظ صورة مع الترجمة والسجل في مجلد «بلاغات».</li>
</ul>
<h3>لو ما ظهرت الترجمة</h3>
<ul>
<li>تأكد إن الإطار فوق مكان الترجمة الإنجليزية (تحديد مكان قراءة الترجمة).</li>
<li>الصور العادية ما تطلع فيها الترجمة لأنها مخفية عن التصوير، استخدم F6.</li>
</ul>
"""


class NotesDialog(QDialog):
    def __init__(self, parent):
        super().__init__(parent)
        self.setWindowTitle("ملاحظات قبل اللعب")
        self.setLayoutDirection(Qt.RightToLeft)
        self.resize(520, 580)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(22, 16, 22, 16)
        lbl = QLabel(NOTES, wordWrap=True)
        lbl.setTextFormat(Qt.RichText)
        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setWidget(lbl)
        lay.addWidget(area)
        b = QPushButton("تمام  ←", objectName="teal")
        b.clicked.connect(self.accept)
        lay.addWidget(b)


class MoreDialog(QDialog):
    def __init__(self, parent, win):
        super().__init__(parent)
        self.w = win
        s = win.s
        self.setWindowTitle("إعدادات إضافية")
        self.setLayoutDirection(Qt.RightToLeft)
        self.setMinimumWidth(460)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(22, 16, 22, 16)
        lay.addWidget(QLabel("إعدادات إضافية", objectName="h2"))
        f = QFormLayout()
        cmb = QComboBox()
        for key, (label, _) in ENGINES.items():
            cmb.addItem(label, key)
        cmb.setCurrentIndex(max(0, cmb.findData(s["engine"])))
        cmb.currentIndexChanged.connect(lambda: win.on_engine(cmb.currentData()))
        f.addRow("خدمة الترجمة", cmb)
        thr = QSlider(Qt.Horizontal, minimum=110, maximum=245, value=int(s["threshold"]))
        thr.valueChanged.connect(lambda v: win.setv("threshold", v))
        f.addRow("درجة البياض", thr)
        it = QDoubleSpinBox(minimum=0.1, maximum=2.0, singleStep=0.05, value=float(s["interval"]), suffix=" ث")
        it.valueChanged.connect(lambda v: win.setv("interval", v))
        f.addRow("سرعة فحص الحوار", it)
        st = QCheckBox("التأكد من النص قبل الترجمة (أدق)")
        st.setChecked(bool(s["stable"]))
        st.toggled.connect(lambda v: win.setv("stable", v))
        f.addRow("", st)
        en = QCheckBox("إظهار النص الإنجليزي تحت الترجمة")
        en.setChecked(bool(s["show_english"]))
        en.toggled.connect(lambda v: win.setv("show_english", v, relayout=True))
        f.addRow("", en)
        au = QCheckBox("يبدأ الترجمة لحاله أول ما يفتح")
        au.setChecked(bool(s["autostart"]))
        au.toggled.connect(lambda v: win.setv("autostart", v))
        f.addRow("", au)
        lay.addLayout(f)
        row = QHBoxLayout()
        b_test = QPushButton("اختبار الترجمة")
        b_test.clicked.connect(self.test)
        b_prev = QPushButton("معاينة مربع الحوار")
        b_prev.clicked.connect(self.preview)
        row.addWidget(b_test)
        row.addWidget(b_prev)
        lay.addLayout(row)
        self.out = QLabel("", objectName="last", wordWrap=True)
        lay.addWidget(self.out)
        self.img = QLabel(alignment=Qt.AlignCenter)
        lay.addWidget(self.img)
        b = QPushButton("تم  ←", objectName="teal")
        b.clicked.connect(self.accept)
        lay.addWidget(b)

    def test(self):
        sample = "Get in the car, we need to go right now!"
        lines = [sample]
        for _key, (label, fn) in ENGINES.items():
            try:
                lines.append(f"✔ {label}: {fn(sample)}")
            except Exception as e:  # noqa: BLE001
                lines.append(f"✘ {label}: لا يعمل ({str(e)[:50]})")
        self.out.setText("\n".join(lines))

    def preview(self):
        try:
            r = frac_to_phys(self.w.s["boxes"][0], self.w.game)
            with new_mss() as sct:
                img = ocr_win.preprocess(grab(sct, r), "color", int(self.w.s["threshold"]))
            pm = pil_to_pixmap(img)
            self.img.setPixmap(pm.scaledToWidth(min(400, pm.width()), Qt.SmoothTransformation))
            text = ocr_win.clean_text(ocr_win.recognize_lines(img))
            self.out.setText((text or "(ما قرأ أي نص)") + "\n\nالنص لازم يطلع أسود واضح على أبيض.")
        except Exception as e:  # noqa: BLE001
            self.out.setText(f"تعذرت المعاينة: {e}")


# ---------------- النافذة الرئيسية ----------------

class MainWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.s = load_settings()
        self.corrections = Corrections(data_path("corrections.json"))
        self.translator = Translator(self.s["engine"], self.corrections, data_path("translation_cache.json"))
        self.engine = None
        self.editor = None
        self.overlay = OverlayManager(self.s)
        self.game, self.game_app = detect_game()
        self.setWindowTitle(f"{APP_NAME} — {APP_TAGLINE}")
        self.setLayoutDirection(Qt.RightToLeft)
        self.setMinimumWidth(500)
        self.build()
        self.update_connection()
        self.hotkeys = Hotkeys()
        self.hotkeys.toggle.connect(self.toggle)
        self.hotkeys.select.connect(self.edit_frame)
        self.hotkeys.hide.connect(self.toggle_overlay)
        self.hotkeys.shot.connect(self.report)
        self.hotkeys.start()
        self.watch = QTimer(self, interval=3000, timeout=self.watch_game)
        self.watch.start()
        self.mask_timer = QTimer(self, interval=700, timeout=self.update_masks)
        self.mask_timer.start()
        log(f"--- start {VERSION} --- game={self.game} app={self.game_app}")
        if self.s.get("autostart", True):
            QTimer.singleShot(900, self.start)

    # --- الواجهة ---
    def build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 12)
        root.setSpacing(10)

        head = QHBoxLayout()
        icon = QLabel()
        icon.setPixmap(QPixmap(resource_path("icon.ico")).scaled(56, 56, Qt.KeepAspectRatio,
                                                                 Qt.SmoothTransformation))
        head.addWidget(icon)
        tl = QVBoxLayout()
        tl.setSpacing(0)
        tl.addWidget(QLabel(APP_NAME, objectName="title"))
        tl.addWidget(QLabel(APP_TAGLINE, objectName="tagline"))
        head.addLayout(tl)
        head.addStretch()
        head.addWidget(QLabel(GAME_NAME, objectName="game"), alignment=Qt.AlignTop)
        root.addLayout(head)

        # بطاقة التشغيل: زر دائري + حالة الاتصال
        hero = QFrame(objectName="card")
        hl = QHBoxLayout(hero)
        hl.setContentsMargins(16, 12, 16, 12)
        self.btn_power = PowerButton()
        self.btn_power.clicked.connect(self.toggle)
        hl.addWidget(self.btn_power)
        info = QVBoxLayout()
        info.setSpacing(6)
        self.lbl_state = QLabel(objectName="state")
        info.addWidget(self.lbl_state)
        self.lbl_conn = QLabel(objectName="muted", wordWrap=True)
        info.addWidget(self.lbl_conn)
        rr = QHBoxLayout()
        b_ref = QPushButton("⟳  تحديث الاتصال")
        b_ref.clicked.connect(self.refresh_game)
        rr.addWidget(b_ref)
        rr.addStretch()
        info.addLayout(rr)
        info.addWidget(QLabel("F8 تشغيل • F9 الأماكن • F10 إخفاء • F6 بلاغ", objectName="muted"))
        hl.addLayout(info, 1)
        root.addWidget(hero)

        # قائمة الإعدادات
        self.r_boxes = Row("🎯", "أماكن قراءة الترجمة", "")
        self.r_boxes.clicked.connect(lambda: self.edit_frame())
        self.r_style = Row("🎨", "شكل الترجمة", "")
        self.r_style.clicked.connect(self.edit_style)
        self.sw_auto = Switch(self.s["auto"])
        self.sw_auto.toggled.connect(lambda v: self.setv("auto", v, restart=True))
        r_auto = Row("📱", "ترجمة الجوال والقوائم", "كل رسالة تترجم فوق فقاعتها", self.sw_auto)
        self.sw_filter = Switch(self.s["noise_filter"])
        self.sw_filter.toggled.connect(lambda v: self.setv("noise_filter", v, restart=True))
        r_filter = Row("🧹", "فلتر النصوص الدخيلة", "يتجاهل اللوحات وأزرار التحكم والعربي", self.sw_filter)
        r_fix = Row("✏️", "تصحيح الترجمات", "صحّح أي جملة والبرنامج يتذكرها")
        r_fix.clicked.connect(self.open_corrections)
        r_rep = Row("🚩", "بلاغ مع صورة", "يحفظ صورة الشاشة مع الترجمة والسجل (F6)")
        r_rep.clicked.connect(self.report)
        r_tips = Row("💡", "نصائح قبل اللعب", "إعدادات اللعبة و chiaki-ng")
        r_tips.clicked.connect(lambda: NotesDialog(self).exec())
        for r in (self.r_boxes, self.r_style, r_auto, r_filter, r_fix, r_rep, r_tips):
            root.addWidget(r)
        self.update_rows()

        self.lbl_last = QLabel("آخر ترجمة تطلع هنا", objectName="last", wordWrap=True)
        self.lbl_last.setMinimumHeight(60)
        root.addWidget(self.lbl_last)

        foot = QHBoxLayout()
        b_log = QPushButton("السجل", objectName="link")
        b_log.clicked.connect(self.send_log)
        b_more = QPushButton("⚙ متقدم", objectName="link")
        b_more.clicked.connect(lambda: MoreDialog(self, self).exec())
        foot.addWidget(b_log)
        foot.addStretch()
        foot.addWidget(QLabel(f"v{VERSION}", objectName="muted"))
        foot.addStretch()
        foot.addWidget(b_more)
        root.addLayout(foot)
        self.set_running_ui(False)

    def update_rows(self):
        extra = len(self.s["boxes"]) - 1
        self.r_boxes.set_sub("مربع الحوار" + (f" + {extra} مربعات إضافية" if extra else " — تقدر تضيف مربعات إضافية"))
        st = self.s["style"]
        name = STYLE_PRESETS.get(st.get("preset"), {}).get("label", "مخصص")
        self.r_style.set_sub(f"{name} • {st.get('font')} • {int(st.get('size', 100))}%")

    def set_running_ui(self, running, msg=None):
        self.btn_power.set_running(running)
        dot = f"<span style='color:{C_OK if running else C_MUTED}'>●</span>"
        self.lbl_state.setText(msg or (f"{dot}  الترجمة شغّالة" if running else f"{dot}  الترجمة متوقفة — اضغط الزر"))

    def set_status(self, t):
        self.lbl_state.setText(t)

    def update_connection(self):
        if self.game_app:
            self.lbl_conn.setText(f"<span style='color:{C_OK}'>✔</span>  متصل بـ {self.game_app}")
        else:
            self.lbl_conn.setText(f"<span style='color:{C_WARN}'>!</span>  ما لقيت chiaki-ng أو Remote Play،"
                                  " نترجم الشاشة كاملة")

    def setv(self, key, value, relayout=False, restart=False):
        self.s[key] = value
        save_settings(self.s)
        if relayout:
            self.overlay.relayout()
        if restart and self.engine:
            self.overlay.clear_all()
            self.engine.rebuild()

    def update_masks(self):
        if not self.engine:
            return
        masks = []
        for w in QApplication.topLevelWidgets():
            if w.isVisible() and not w.isMinimized() and not w.property("no_mask") \
                    and not isinstance(w, (ScreenOverlay, FrameEditor)):
                try:
                    masks.append(logical_to_phys(w))
                except Exception:  # noqa: BLE001
                    pass
        self.engine.masks = masks

    # --- اللعبة ---
    def refresh_game(self):
        self.game, self.game_app = detect_game()
        self.update_connection()
        if self.engine:
            self.overlay.clear_all()
            self.engine.rebuild(self.game)
        log(f"refresh game={self.game} app={self.game_app}")

    def watch_game(self):
        if self.editor:
            return
        game, app = detect_game()
        if game != self.game or app != self.game_app:
            self.game, self.game_app = game, app
            self.update_connection()
            if self.engine:
                self.overlay.clear_all()
                self.engine.rebuild(self.game)

    # --- الأوامر ---
    def toggle(self):
        if self.engine:
            self.stop()
        else:
            self.start()

    def start(self):
        if self.engine or self.editor:
            return
        self.engine = Engine(self.s, self.translator, self.game)
        self.engine.updated.connect(self.on_translated)
        self.engine.cleared.connect(self.overlay.clear_item)
        self.engine.status.connect(self.set_status)
        self.engine.failed.connect(self.on_failed)
        self.update_masks()
        self.engine.start()
        self.set_running_ui(True)

    def stop(self):
        if self.engine:
            self.engine.stop()
            self.engine = None
        self.overlay.clear_all()
        self.set_running_ui(False)

    def toggle_overlay(self):
        self.overlay.set_hidden(not self.overlay.user_hidden)
        if self.overlay.user_hidden:
            self.set_status("الترجمة مخفية (F10 لإظهارها)")

    def on_translated(self, key, info, en, ar, kind):
        if not self.engine:
            return
        self.lbl_last.setText(f"{ar}<br><span style='color:{C_MUTED}'>{en}</span>")
        self.overlay.update_item(key, info, en, ar, kind)

    def on_failed(self, msg):
        self.stop()
        QMessageBox.warning(self, APP_NAME, msg)

    def edit_frame(self, select=0):
        if self.editor:
            return
        was = self.engine is not None
        self.stop()
        self.game, self.game_app = detect_game()
        self.editor = FrameEditor(self.s, self.game, select if isinstance(select, int) else 0)

        def finished():
            self.editor = None
            self.update_rows()
            self.showNormal()
            if was or self.s.get("autostart", True):
                self.start()
        self.editor.done.connect(finished)
        self.showMinimized()
        self.editor.show()
        self.editor.activateWindow()

    def edit_style(self):
        StyleDialog(self, self.s, self.overlay.relayout).exec()
        self.update_rows()

    def on_engine(self, key):
        self.translator.engine = key
        self.translator.cache.clear()
        self.translator.down_until.clear()
        self.setv("engine", key)

    def open_corrections(self):
        CorrectionsDialog(self, self.translator, self.overlay.replace_text).exec()

    def report(self):
        """بلاغ: صورة الشاشة مع الترجمة + السجل + آخر الترجمات، في مجلد «بلاغات»."""
        try:
            folder = os.path.join(data_path("بلاغات"), time.strftime("%Y%m%d-%H%M%S"))
            os.makedirs(folder, exist_ok=True)
            self.overlay.snapshot().save(os.path.join(folder, "صورة.png"))
            with open(os.path.join(folder, "آخر الترجمات.txt"), "w", encoding="utf-8") as f:
                for en, ar in self.translator.history:
                    f.write(f"{en}\n{ar}\n\n")
            if os.path.exists(data_path("log.txt")):
                shutil.copy(data_path("log.txt"), os.path.join(folder, "log.txt"))
            log(f"report saved: {folder}")
            self.set_status("✔ انحفظ البلاغ (صورة + سجل) في مجلد «بلاغات»")
            self.open_path(folder)
        except Exception as e:  # noqa: BLE001
            self.set_status(f"تعذر حفظ البلاغ: {e}")

    def send_log(self):
        self.open_path(os.path.dirname(data_path("log.txt")))
        self.set_status("أرسل ملف log.txt من المجلد اللي انفتح")

    @staticmethod
    def open_path(path):
        try:
            if IS_WIN:
                os.startfile(path)  # noqa: S606
        except Exception:  # noqa: BLE001
            pass

    def closeEvent(self, e):
        self.stop()
        save_settings(self.s)
        self.translator.save_cache()
        QApplication.quit()
        super().closeEvent(e)


def load_fonts():
    for _label, _fam, files in FONTS:
        for fn in files:
            QFontDatabase.addApplicationFont(resource_path("fonts", fn))


def main():
    if IS_WIN:
        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("AbuWahab.GTA.Translator")
        except Exception:  # noqa: BLE001
            pass
    app = QApplication(sys.argv)
    load_fonts()
    app.setFont(QFont("Alexandria", 10))
    app.setLayoutDirection(Qt.RightToLeft)
    app.setStyleSheet(APP_STYLE)
    icon = resource_path("icon.ico")
    if os.path.exists(icon):
        app.setWindowIcon(QIcon(icon))
    w = MainWindow()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
