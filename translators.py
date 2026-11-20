"""خدمات الترجمة المجانية: جوجل ومايكروسوفت (بدون مفاتيح API).

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


def google_translate(text: str, src: str = "en", dst: str = "ar") -> str:
    r = _session.get(
        "https://translate.googleapis.com/translate_a/single",
        params={"client": "gtx", "sl": src, "tl": dst, "dt": "t", "q": text},
        timeout=4,
    )
    if r.status_code == 429:
        raise RateLimited("Google: too many requests")
    r.raise_for_status()
    data = r.json()
    out = "".join(seg[0] for seg in data[0] if seg and seg[0])
    if not out.strip():
        raise RuntimeError("Google: empty result")
    return out.strip()


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

    def __init__(self, engine: str = "google", corrections: Corrections | None = None):
        self.engine = engine
        self.cache: dict[str, str] = {}
        self.last_engine_used = engine
        self.corrections = corrections or Corrections()
        self.history: list[tuple[str, str]] = []  # آخر الترجمات للتصحيح
        self._lock = threading.Lock()
        self.down_until: dict[str, float] = {}  # خدمة معطلة مؤقتاً: نتجاوزها بدل ما ننتظرها
        self.last_error = ""

    def _raw(self, text: str) -> str:
        key = text.strip().lower()
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
                if len(self.cache) > 3000:
                    self.cache.clear()
                self.cache[key] = out
                return out
            except PermissionError as e:
                self.down_until[name] = time.time() + 900  # مرفوضة: نوقفها ربع ساعة
                errors.append(f"{name}: {e}")
            except RateLimited as e:
                self.down_until[name] = time.time() + 20
                errors.append(f"{name}: {e}")
            except Exception as e:  # noqa: BLE001
                self.down_until[name] = time.time() + 5
                errors.append(f"{name}: {e}")
        self.last_error = " | ".join(errors)
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
