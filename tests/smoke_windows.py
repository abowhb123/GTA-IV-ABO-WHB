"""اختبار على ويندوز: OCR على صورة ترجمة مصطنعة + خدمات الترجمة + استبعاد نافذة الترجمة من التصوير."""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from PIL import Image, ImageDraw, ImageFilter, ImageFont  # noqa: E402

import ocr_win  # noqa: E402
import translators  # noqa: E402

OUT = os.environ.get("SMOKE_OUT", "smoke-out")
os.makedirs(OUT, exist_ok=True)
fails = []

# 1) صورة تشبه ترجمة لعبة: نص أبيض بحدود سوداء فوق خلفية ملونة ومشوشة
W, H = 1100, 120
bg = Image.radial_gradient("L").resize((W, H)).convert("RGB")
bg = Image.merge("RGB", [bg.getchannel(0), bg.getchannel(0).point(lambda p: p // 2), Image.new("L", (W, H), 90)])
bg = bg.filter(ImageFilter.GaussianBlur(3))
d = ImageDraw.Draw(bg)
font = ImageFont.truetype(r"C:\Windows\Fonts\arialbd.ttf", 34)
line1 = "Get in the car, we need to leave right now."
line2 = "The cops are coming and I am not going back!"
for i, t in enumerate((line1, line2)):
    d.text((W // 2, 35 + i * 46), t, font=font, fill="white", anchor="mm", stroke_width=3, stroke_fill="black")
bg.save(os.path.join(OUT, "subtitle.png"))

for mode in ("bright", "gray"):
    t0 = time.time()
    proc = ocr_win.preprocess(bg, mode, 175)
    text = ocr_win.clean_text(ocr_win.recognize_lines(proc))
    ms = (time.time() - t0) * 1000
    print(f"OCR[{mode}] {ms:.0f}ms: {text}")
    if mode == "bright":
        proc.save(os.path.join(OUT, "subtitle-processed.png"))
        low = text.lower()
        if not all(w in low for w in ("car", "leave", "cops", "going")):
            fails.append("OCR bright mode did not read the subtitle")

# 2) الترجمة
ok_any = False
for key, (label, fn) in translators.ENGINES.items():
    try:
        out = fn(line1)
        print(f"{key}: {out}")
        ok_any = ok_any or any("\u0600" <= c <= "\u06ff" for c in out)
    except Exception as e:  # noqa: BLE001
        print(f"{key} FAILED: {e}")
if not ok_any:
    fails.append("no translation engine worked")

for name, fn in translators._G_ENDPOINTS:
    try:
        print(f"google/{name}:", fn(line1, "en", "ar"))
    except Exception as e:  # noqa: BLE001
        print(f"google/{name} FAILED:", e)
tr = translators.Translator("microsoft")
print("Translator fallback:", tr.translate("Where is my money?"), "via", tr.last_engine_used)

# 3) البرنامج كامل فوق نافذة لعبة مزيفة اسمها chiaki-ng
try:
    from PySide6.QtCore import QRect, Qt
    from PySide6.QtGui import QColor, QFont, QPainter
    from PySide6.QtWidgets import QApplication, QWidget

    import main

    app = QApplication([])
    main.load_fonts()
    app.setStyleSheet(main.APP_STYLE)

    # فلتر الكلمات
    for t, want in [("(_på.l.nJ I", False), ("e ä.n..2Jill", False), ("Get it together..", True),
                    ("I fuckin' hate city folk!", True)]:
        got = ocr_win.is_real_text(t, 0.7)
        print("filter:", t, "->", got)
        if got != want:
            fails.append(f"word filter wrong for {t!r}")

    class FakeGame(QWidget):
        def paintEvent(self, _):
            p = QPainter(self)
            p.fillRect(self.rect(), QColor(35, 60, 55))
            v = main.game_video_rect({"left": 0, "top": 0, "width": self.width(), "height": self.height()})
            f = QFont("Arial", 20)
            f.setBold(True)
            p.setFont(f)
            p.setPen(QColor("white"))
            p.drawText(QRect(v["left"], v["top"] + int(v["height"] * 0.84), v["width"], 40), Qt.AlignCenter,
                       "Lucia: We need to leave right now.")
            f.setPointSize(15)
            p.setFont(f)
            p.drawText(QRect(v["left"] + int(v["width"] * 0.08), v["top"] + int(v["height"] * 0.3), 520, 80),
                       Qt.AlignLeft | Qt.TextWordWrap,
                       "Jason's vehicle was recognized by the cops and impounded. Check the map to locate it.")
            p.drawText(QRect(v["left"] + int(v["width"] * 0.6), v["top"] + int(v["height"] * 0.1), 300, 40),
                       Qt.AlignRight, "إعدادات النظام والشبكة")
            # جوال: ثلاث فقاعات رسائل تحت بعض
            f.setPointSize(12)
            f.setBold(False)
            p.setFont(f)
            bx, by = v["left"] + int(v["width"] * 0.62), v["top"] + int(v["height"] * 0.28)
            for i, msg in enumerate(["Cool yeah. We'll have to hit up\nthe Rusty when she's out.",
                                     "Beers in your fridge and me in\nyour pad.", "Hurry home babe"]):
                r = QRect(bx, by + i * 78, 300, 56 if "\n" in msg else 32)
                p.setBrush(QColor(235, 235, 240))
                p.setPen(Qt.NoPen)
                p.drawRoundedRect(r, 12, 12)
                p.setPen(QColor(30, 30, 40))
                p.drawText(r.adjusted(10, 4, -10, -4), Qt.AlignLeft | Qt.AlignVCenter, msg)

    game = FakeGame()
    game.setWindowTitle("chiaki-ng | PS5")
    game.setProperty("no_mask", True)  # نافذة اللعبة المزيفة داخل نفس البرنامج — لا تُغطّى
    scr = QApplication.primaryScreen().availableGeometry()
    game.setGeometry(scr.adjusted(0, 30, 0, 0))
    game.show()
    for _ in range(30):
        app.processEvents()
        time.sleep(0.03)

    w = main.MainWindow()
    w.s["autostart"] = False
    w.showMinimized()  # عشان ما تغطي نافذة اللعبة المزيفة
    print("game detected:", w.game_app, w.game)
    if w.game_app != "chiaki-ng":
        fails.append("did not detect the chiaki-ng window")
    game.raise_()
    game.activateWindow()
    w.start()
    t0 = time.time()
    seen = {}
    while time.time() - t0 < 12:
        app.processEvents()
        for k, it in w.overlay.items.items():
            if k not in seen:
                seen[k] = time.time() - t0
                print(f"  +{seen[k]:.2f}s {k}: {it['en']} -> {it['ar']}")
        if "box:0" in seen and len([k for k in seen if k.startswith("auto")]) >= 4 \
                and time.time() - t0 > 6:
            break
        time.sleep(0.03)
    ens = " ".join(it["en"] for it in w.overlay.items.values())
    if "box:0" not in seen:
        fails.append("dialog box did not translate")
    bubbles = [it["en"] for it in w.overlay.items.values() if it["kind"] == "auto"]
    print("auto blocks:", bubbles)
    if not any(b.startswith("Cool yeah") and "Beers" not in b for b in bubbles) or \
            not any(b.startswith("Beers in your fridge") for b in bubbles):
        fails.append("phone bubbles were merged or missed")
    rects = [b[0] for ov in w.overlay.overlays.values() for b in ov.boxes]
    overl = sum(1 for i in range(len(rects)) for j in range(i + 1, len(rects)) if rects[i].intersects(rects[j]))
    print("overlapping translation boxes:", overl)
    if overl:
        fails.append("translation boxes overlap")
    if "impounded" not in ens:
        fails.append("auto mode did not translate the mission text")
    print("overlay excluded from capture:", w.overlay.excluded)
    n = len(seen)
    for _ in range(80):
        app.processEvents()
        time.sleep(0.03)
    extra = [k for k in w.overlay.items if k not in seen]
    print("new items after 2.4s (should be 0):", extra)
    if extra:
        fails.append("overlay text was read back or duplicated")
    w.report()
    rep = main.data_path("بلاغات")
    print("report folder:", os.path.isdir(rep) and os.listdir(rep))
    w.stop()
    for name, ov in w.overlay.overlays.items():
        ov.grab().save(os.path.join(OUT, "overlay.png"))
    with main.new_mss() as sct:
        main.grab(sct, main.monitors(True)[1]).save(os.path.join(OUT, "desktop.png"))
    w.grab().save(os.path.join(OUT, "window.png"))
    sd = main.StyleDialog(w, w.s, lambda: None)
    sd.grab().save(os.path.join(OUT, "style.png"))
    sd.reject()

    en0 = next(iter(w.overlay.items.values()), {"en": "Get in the car"})["en"] if w.overlay.items else "Get in the car"
    w.corrections.set_sentence(en0, "تصحيح تجريبي")
    print("correction lookup:", w.translator.translate(en0))
    if w.translator.translate(en0) != "تصحيح تجريبي":
        fails.append("corrections not applied")
    w.corrections.set_sentence(en0, "")
    w.close()
    game.close()
except Exception as e:  # noqa: BLE001
    import traceback
    traceback.print_exc()
    fails.append(f"GUI: {e}")

if fails:
    print("FAILED:", fails)
    sys.exit(1)
print("ALL OK")
