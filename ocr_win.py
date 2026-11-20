"""قراءة النص من الصورة باستخدام محرك OCR المدمج في ويندوز 10/11 (مجاني وسريع)."""
import asyncio
import os
import re
import sys
import threading

from PIL import Image, ImageOps

_tls = threading.local()  # لكل خيط محركه وحلقته الخاصة (كانوا يتزاحمون في 1.2)


class OcrNotAvailable(RuntimeError):
    pass


def _get_engine(lang: str = "en-US"):
    _engine = getattr(_tls, "engine", None)
    if _engine is None:
        from winrt.windows.globalization import Language
        from winrt.windows.media.ocr import OcrEngine

        eng = OcrEngine.try_create_from_language(Language(lang))
        if eng is None:
            raise OcrNotAvailable(
                "حزمة قراءة النص الإنجليزي غير مثبتة في ويندوز. "
                "ثبّتها من: الإعدادات ← الوقت واللغة ← اللغة ← إضافة English (United States)"
            )
        _tls.engine = _engine = eng
    return _engine


async def _recognize(img: Image.Image, lang: str):
    from winrt.windows.graphics.imaging import BitmapPixelFormat, SoftwareBitmap
    from winrt.windows.storage.streams import DataWriter

    img = img.convert("RGBA")
    dw = DataWriter()
    dw.write_bytes(img.tobytes())
    bmp = SoftwareBitmap.create_copy_from_buffer(
        dw.detach_buffer(), BitmapPixelFormat.RGBA8, img.width, img.height
    )
    return await _get_engine(lang).recognize_async(bmp)


def _run(img, lang):
    loop = getattr(_tls, "loop", None)
    if loop is None:
        loop = _tls.loop = asyncio.new_event_loop()
    return loop.run_until_complete(_recognize(img, lang))


def recognize_lines(img: Image.Image, lang: str = "en-US") -> list[str]:
    """ترجع أسطر النص الموجودة في الصورة."""
    return [line.text for line in _run(img, lang).lines]


def recognize_boxes(img: Image.Image, lang: str = "en-US") -> list[tuple[str, tuple]]:
    """ترجع كل سطر مع مكانه (x, y, w, h) داخل الصورة."""
    out = []
    for line in _run(img, lang).lines:
        rects = [w.bounding_rect for w in line.words]
        if not rects:
            continue
        x1 = min(r.x for r in rects)
        y1 = min(r.y for r in rects)
        x2 = max(r.x + r.width for r in rects)
        y2 = max(r.y + r.height for r in rects)
        out.append((line.text, (x1, y1, x2 - x1, y2 - y1)))
    return out


def group_blocks(lines: list[tuple[str, tuple]]) -> list[tuple[str, tuple, int]]:
    """يجمع الأسطر المتقاربة (فقرة واحدة) في كتلة نص واحدة. يرجع (النص، المكان، عدد الأسطر)."""
    # 1) دمج القطع اللي على نفس السطر وقريبة من بعض
    merged = []
    for text, (x, y, w, h) in sorted(lines, key=lambda l: (l[1][1], l[1][0])):
        for m in merged:
            mx, my, mw, mh = m[1]
            same_row = abs((y + h / 2) - (my + mh / 2)) < max(h, mh) * 0.5
            gap = x - (mx + mw)
            if same_row and -mh < gap < max(h, mh) * 1.5:
                nx1, ny1 = min(mx, x), min(my, y)
                nx2, ny2 = max(mx + mw, x + w), max(my + mh, y + h)
                m[0] = m[0] + " " + text
                m[1] = (nx1, ny1, nx2 - nx1, ny2 - ny1)
                break
        else:
            merged.append([text, (x, y, w, h)])
    # 2) تجميع الأسطر المتتالية في فقرة
    blocks = []  # [lines_texts, x1, y1, x2, y2, line_h]
    for text, (x, y, w, h) in sorted(merged, key=lambda l: (l[1][1], l[1][0])):
        placed = False
        for b in blocks:
            gap = y - b[4]
            overlap = min(x + w, b[3]) - max(x, b[1])
            narrower = max(1, min(w, b[3] - b[1]))
            same_size = 0.7 < h / max(b[5], 1) < 1.45
            # أسطر نفس الفقرة متلاصقة؛ فقاعات الجوال بينها فراغ أكبر — نخليها منفصلة
            if -h * 0.3 < gap < b[5] * 0.6 and overlap > narrower * 0.5 and same_size:
                b[0].append(text)
                b[1], b[2] = min(b[1], x), min(b[2], y)
                b[3], b[4] = max(b[3], x + w), max(b[4], y + h)
                placed = True
                break
        if not placed:
            blocks.append([[text], x, y, x + w, y + h, h])
    return [(" ".join(b[0]), (b[1], b[2], b[3] - b[1], b[4] - b[2]), len(b[0])) for b in blocks]


# ---------- تجهيز الصورة ----------

def preprocess(img: Image.Image, mode: str = "bright", threshold: int = 175) -> Image.Image:
    """تكبير الصورة وعزل النص الأبيض (نص الترجمة في الألعاب غالباً أبيض بحدود سوداء).

    mode:
      bright — يبقي البكسلات الفاتحة فقط ويحوّلها لنص أسود على خلفية بيضاء.
      color  — مثل bright لكن يلتقط الكلمات الملونة الفاتحة أيضاً (أصفر، أزرق...) — لشرح المهمات.
      gray   — تكبير وتحويل لرمادي فقط (للترجمات التي لها خلفية داكنة).
    """
    scale = scale_for(img)
    if scale > 1:
        img = img.resize((img.width * scale, img.height * scale), Image.LANCZOS)
    if mode == "color":
        r, g, b = img.convert("RGB").split()
        from PIL import ImageChops
        bright = ImageChops.lighter(ImageChops.lighter(r, g), b)
        return bright.point(lambda p: 0 if p >= threshold else 255)
    gray = ImageOps.grayscale(img)
    if mode == "bright":
        return gray.point(lambda p: 0 if p >= threshold else 255)
    return gray


def scale_for(img: Image.Image) -> int:
    return 2 if img.height < 400 else 1


def signature(img: Image.Image) -> bytes:
    """بصمة صغيرة للصورة لمعرفة هل تغيّر النص أو لا (بدون OCR)."""
    small = img.convert("L").resize((128, 24))
    return bytes(1 if p < 128 else 0 for p in small.getdata())


def sig_distance(a: bytes, b: bytes) -> float:
    if not a or not b or len(a) != len(b):
        return 1.0
    return sum(x != y for x, y in zip(a, b)) / len(a)


# ---------- تنظيف النص ----------

_FIXES = [
    (re.compile(r"\s*\|\s*"), " I "),
    (re.compile(r"\s+"), " "),
    (re.compile(r"\s+([,.!?;:])"), r"\1"),
]


def clean_text(lines: list[str]) -> str:
    text = " ".join(l.strip() for l in lines if l.strip())
    for pat, rep in _FIXES:
        text = pat.sub(rep, text)
    text = text.strip(" -_~*•")
    return text.strip()


def looks_like_text(text: str, min_letters: int = 4) -> bool:
    """يتجاهل الضجيج: لازم يكون فيه حروف كافية ونسبة الحروف معقولة."""
    letters = sum(c.isalpha() for c in text)
    if letters < min_letters:
        return False
    if letters / max(len(text.replace(" ", "")), 1) < 0.6:
        return False
    words = [w for w in re.findall(r"[A-Za-z']+", text) if len(w) >= 2]
    return len(words) >= 1


# ---------- فلتر الكلمات الإنجليزية الحقيقية ----------

_WORDS = None


def _words():
    global _WORDS
    if _WORDS is None:
        base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
        try:
            with open(os.path.join(base, "words_en.txt"), encoding="utf-8") as f:
                _WORDS = set(f.read().split())
        except Exception:  # noqa: BLE001
            _WORDS = set()
    return _WORDS


_TOKEN = re.compile(r"[A-Za-z][A-Za-z']*")


def english_score(text: str) -> float:
    """نسبة الكلمات الإنجليزية المعروفة في النص (0 إلى 1). النص العربي المقروء غلط يطلع قريب من صفر."""
    if any(ord(c) > 127 and c.isalpha() for c in text):
        return 0.0  # حروف مثل ä å é = قراءة خاطئة لنص عربي أو رموز
    words = _words()
    toks = [t.lower().strip("'") for t in _TOKEN.findall(text)]
    toks = [t for t in toks if t]
    if not toks or not words:
        return 1.0 if toks else 0.0
    known = 0
    for t in toks:
        if t in words or (t.endswith("in") and t + "g" in words) or (len(t) == 1 and t in "ai"):
            known += 1
        elif "'" in t and t.split("'")[0] in words:
            known += 1
    return known / len(toks)


def is_button_hint(text: str) -> bool:
    """أزرار التحكم والعناوين القصيرة بحروف كبيرة (SELECT BACK، SCROLL، GTA VI) — ما تحتاج ترجمة."""
    toks = _TOKEN.findall(text)
    return 0 < len(toks) <= 3 and all(t.isupper() or len(t) <= 2 for t in toks)


def is_real_text(text: str, min_score: float = 0.6) -> bool:
    return looks_like_text(text) and english_score(text) >= min_score
