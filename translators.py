"""خدمات الترجمة المجانية: جوجل (أكثر من مدخل) وماي ميموري ومايكروسوفت (بدون مفاتيح API).

كل دالة تأخذ نصاً إنجليزياً وترجع الترجمة العربية، أو ترمي استثناء عند الفشل.
"""
import json
import re
import threading
import time

import requests

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

_local = threading.local()


class _SessionProxy:
    """جلسة HTTP مستقلة لكل خيط (الترجمة تعمل على أكثر من خيط بنفس الوقت)."""

    def __getattr__(self, name):
        sess = getattr(_local, "session", None)
        if sess is None:
            sess = requests.Session()
            sess.headers.update({"User-Agent": UA})
            _local.session = sess
        return getattr(sess, name)


_session = _SessionProxy()


class RateLimited(RuntimeError):
    pass


class _Throttle:
    """فاصل بسيط بين الطلبات لنفس الخدمة — يقلل احتمال إن جوجل يوقف الجهاز."""

    def __init__(self, gap):
        self.gap = gap
        self.next = 0.0
        self.lock = threading.Lock()

    def wait(self):
        with self.lock:
            now = time.time()
            delay = self.next - now
            self.next = max(now, self.next) + self.gap
        if delay > 0:
            time.sleep(delay)


_g_throttle = _Throttle(0.35)


def _google_gtx(text, src, dst):
    r = _session.get("https://translate.googleapis.com/translate_a/single",
                     params={"client": "gtx", "sl": src, "tl": dst, "dt": "t", "q": text}, timeout=4)
    if r.status_code == 429 or "/sorry/" in r.url:
        raise RateLimited("gtx 429")
    r.raise_for_status()
    return "".join(seg[0] for seg in r.json()[0] if seg and seg[0])


def _google_dict(text, src, dst):
    r = _session.get("https://clients5.google.com/translate_a/t",
                     params={"client": "dict-chrome-ex", "sl": src, "tl": dst, "q": text}, timeout=4)
    if r.status_code == 429 or "/sorry/" in r.url:
        raise RateLimited("dict 429")
    r.raise_for_status()
    data = r.json()
    while isinstance(data, list) and data and isinstance(data[0], list):
        data = data[0]
    if isinstance(data, list):
        data = data[0]
    if isinstance(data, dict):  # شكل قديم للرد
        data = "".join(x.get("trans", "") for x in data.get("sentences", []))
    return str(data)


def _google_mobile(text, src, dst):
    import html as _html
    r = _session.get("https://translate.google.com/m", params={"sl": src, "tl": dst, "q": text}, timeout=5)
    if r.status_code == 429 or "/sorry/" in r.url:
        raise RateLimited("mobile 429")
    r.raise_for_status()
    m = re.search(r'class="result-container">(.*?)</div>', r.text, re.S)
    if not m:
        raise RuntimeError("mobile: no result")
    return _html.unescape(m.group(1))


_G_ENDPOINTS = [("gtx", _google_gtx), ("dict", _google_dict), ("mobile", _google_mobile)]
_g_down: dict[str, float] = {}


def google_translate(text: str, src: str = "en", dst: str = "ar") -> str:
    """جوجل من أكثر من مدخل: لو واحد وقّف الجهاز (429)، ننتقل للي بعده."""
    errors = []
    now = time.time()
    order = [e for e in _G_ENDPOINTS if _g_down.get(e[0], 0) < now] or _G_ENDPOINTS[:1]
    for name, fn in order:
        _g_throttle.wait()
        try:
            out = fn(text, src, dst).strip()
            if out:
                _g_down.pop(name, None)
                return out
            errors.append(f"{name}: empty")
        except RateLimited:
            _g_down[name] = time.time() + 120
            errors.append(f"{name}: 429")
        except Exception as e:  # noqa: BLE001
            _g_down[name] = time.time() + 10
            errors.append(f"{name}: {str(e)[:60]}")
    if all("429" in e for e in errors):
        raise RateLimited("Google: too many requests (" + ", ".join(errors) + ")")
    raise RuntimeError("Google: " + "; ".join(errors))


def mymemory_translate(text: str, src: str = "en", dst: str = "ar") -> str:
    """MyMemory: مجاني بدون حساب (حد يومي محدود) — يستخدم احتياط بس."""
    r = _session.get("https://api.mymemory.translated.net/get",
                     params={"q": text[:480], "langpair": f"{src}|{dst}"}, timeout=6)
    if r.status_code == 429:
        raise RateLimited("MyMemory: 429")
    r.raise_for_status()
    js = r.json()
    out = (js.get("responseData") or {}).get("translatedText", "").strip()
    if js.get("quotaFinished") or "MYMEMORY WARNING" in out.upper():
        raise RateLimited("MyMemory: daily limit")
    if not out:
        raise RuntimeError("MyMemory: empty")
    return out


class _BingSession:
    """جلسة مجانية من موقع Bing Translator (مايكروسوفت)، تتجدد تلقائياً."""

    PAGE = "https://www.bing.com/translator"

    def __init__(self):
        self.data = None
        self._time = 0.0
        self._lock = threading.Lock()

    def get(self, force: bool = False) -> dict:
        with self._lock:
            if force or not self.data or time.time() - self._time > 600:
                html = _session.get(self.PAGE, timeout=4).text
                ig = re.search(r'IG:"([^"]+)"', html)
                iid = re.search(r'data-iid="([^"]+)"', html)
                ab = re.search(r"params_AbusePreventionHelper\s*=\s*\[(\d+),\s*\"([^\"]+)\"", html)
                if not (ig and ab):
                    raise RuntimeError("Microsoft: could not start Bing session")
                self.data = {
                    "IG": ig.group(1),
                    "IID": iid.group(1) if iid else "translator.5028",
                    "key": ab.group(1),
                    "count": 0,
                    "token": ab.group(2),
                }
                self._time = time.time()
            return self.data


_bing = _BingSession()


def microsoft_translate(text: str, src: str = "en", dst: str = "ar") -> str:
    last_err = None
    for attempt in range(2):
        try:
            d = _bing.get(force=attempt > 0)
            d["count"] += 1
            r = _session.post(
                "https://www.bing.com/ttranslatev3",
                params={"isVertical": "1", "IG": d["IG"], "IID": f'{d["IID"]}.{d["count"]}'},
                data={"fromLang": src, "to": dst, "text": text,
                      "token": d["token"], "key": d["key"], "tryFetchingGenderDebiasedTranslations": "true"},
                headers={"Referer": _BingSession.PAGE},
                timeout=4,
            )
            if r.status_code in (401, 403):
                raise PermissionError(f"Microsoft: {r.status_code}")
            r.raise_for_status()
            js = r.json()
            if isinstance(js, list):
                out = js[0]["translations"][0]["text"].strip()
                if out:
                    return out
            last_err = RuntimeError(f"Microsoft: unexpected response {str(js)[:80]}")
        except PermissionError:
            raise  # مرفوض: لا فائدة من الإعادة
        except Exception as e:  # noqa: BLE001
            last_err = e
    raise last_err or RuntimeError("Microsoft failed")


ENGINES = {
    "google": ("جوجل", google_translate),
    "mymemory": ("ماي ميموري", mymemory_translate),
    "microsoft": ("مايكروسوفت", microsoft_translate),
}


_SPEAKER = re.compile(r"^([A-Z][A-Za-z.'\- ]{1,22}?)\s*:\s+(.{3,})$")


def norm_key(text: str) -> str:
    return re.sub(r"[^a-z0-9' ]+", "", text.lower()).strip()


class Corrections:
    """تصحيحات المستخدم: جمل كاملة يتذكرها البرنامج، واستبدال كلمات في الترجمة العربية."""

    def __init__(self, path: str | None = None):
        self.path = path
        self.sentences: dict[str, str] = {}
        self.replace: dict[str, str] = {}
        self.load()

    def load(self):
        if not self.path:
            return
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
            self.sentences = dict(data.get("sentences", {}))
            self.replace = dict(data.get("replace", {}))
        except Exception:  # noqa: BLE001
            pass

    def save(self):
        if not self.path:
            return
        try:
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump({"sentences": self.sentences, "replace": self.replace},
                          f, ensure_ascii=False, indent=2)
        except Exception:  # noqa: BLE001
            pass

    def set_sentence(self, english: str, arabic: str):
        k = norm_key(english)
        if not k:
            return
        if arabic.strip():
            self.sentences[k] = arabic.strip()
        else:
            self.sentences.pop(k, None)
        self.save()

    def lookup(self, english: str):
        return self.sentences.get(norm_key(english))

    def apply(self, arabic: str) -> str:
        for wrong, right in self.replace.items():
            if wrong:
                arabic = arabic.replace(wrong, right)
        return arabic


class Translator:
    """يترجم بالخدمة المختارة، ويحوّل للثانية تلقائياً لو فشلت، مع ذاكرة للجمل المكررة وتصحيحات المستخدم."""

    def __init__(self, engine: str = "google", corrections: Corrections | None = None,
                 cache_path: str | None = None):
        self.engine = engine
        self.cache: dict[str, str] = {}
        self.cache_path = cache_path
        self._unsaved = 0
        if cache_path:
            try:
                with open(cache_path, encoding="utf-8") as f:
                    self.cache = dict(json.load(f))
            except Exception:  # noqa: BLE001
                pass
        self.last_engine_used = engine
        self.corrections = corrections or Corrections()
        self.history: list[tuple[str, str]] = []  # آخر الترجمات للتصحيح
        self._lock = threading.Lock()
        self.down_until: dict[str, float] = {}  # خدمة معطلة مؤقتاً: نتجاوزها بدل ما ننتظرها
        self.last_error = ""

    @staticmethod
    def cache_key(text: str) -> str:
        """قراءات متشابهة لنفس الجملة (فرق نقطة أو مسافة) = مفتاح واحد، فما ننطلب مرتين."""
        return re.sub(r"[^a-z0-9']+", " ", text.lower()).strip()

    def save_cache(self):
        if not self.cache_path:
            return
        try:
            items = list(self.cache.items())[-5000:]
            with open(self.cache_path, "w", encoding="utf-8") as f:
                json.dump(dict(items), f, ensure_ascii=False)
            self._unsaved = 0
        except Exception:  # noqa: BLE001
            pass

    def _raw(self, text: str) -> str:
        key = self.cache_key(text)
        if key in self.cache:
            return self.cache[key]
        now = time.time()
        order = [self.engine] + [e for e in ENGINES if e != self.engine]
        usable = [e for e in order if self.down_until.get(e, 0) < now] or order[:1]
        errors = []
        for name in usable:
            try:
                out = ENGINES[name][1](text)
                self.last_engine_used = name
                self.down_until.pop(name, None)
                self.cache[key] = out
                self._unsaved += 1
                if self._unsaved >= 10:
                    self.save_cache()
                return out
            except PermissionError as e:
                self.down_until[name] = time.time() + 900  # مرفوضة: نوقفها ربع ساعة
                errors.append(f"{name}: {e}")
            except RateLimited as e:
                self.down_until[name] = time.time() + 90
                errors.append(f"{name}: {e}")
            except Exception as e:  # noqa: BLE001
                self.down_until[name] = time.time() + 5
                errors.append(f"{name}: {e}")
        self.last_error = " | ".join(errors)
        if any("429" in e or "limit" in e or "too many" in e for e in errors):
            raise RateLimited("جوجل موقّف الترجمة مؤقتاً بسبب كثرة الطلبات — ترجع لحالها خلال دقائق")
        raise RuntimeError(self.last_error)

    def translate(self, text: str) -> str:
        fixed = self.corrections.lookup(text)
        if fixed:
            out = fixed
        else:
            m = _SPEAKER.match(text)
            if m and len(m.group(1).split()) <= 3:
                name, line = m.group(1).strip(), m.group(2).strip()
                fixed_line = self.corrections.lookup(line)
                out = f"{self._raw(name)}: {fixed_line or self._raw(line)}"
            else:
                out = self._raw(text)
            out = self.corrections.apply(out)
        with self._lock:
            self.history = [h for h in self.history if h[0] != text][-29:] + [(text, out)]
        return out


if __name__ == "__main__":
    import sys

    sample = " ".join(sys.argv[1:]) or "Hey, get in the car. We need to leave right now!"
    for key, (label, fn) in ENGINES.items():
        try:
            print(key, "->", fn(sample))
        except Exception as e:  # noqa: BLE001
            print(key, "FAILED:", e)
