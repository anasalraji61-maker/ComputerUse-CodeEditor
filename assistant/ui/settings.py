"""الإعدادات وفحص الاتصال: منطق خالص (بلا tkinter عند الاستيراد) + نافذة الإعدادات.

قواعد صارمة:
- مفتاح API يبقى في ذاكرة المتحكم لهذه الجلسة فقط ويُمرَّر إلى المصنع في قاموس بيئة؛ لا يُكتب في config.json ولا في السجلات ولا في الأحداث.
- config.json يستقبل مفاتيح غير سرية فقط (SAFE_CONFIG_KEYS).
- فحص الاتصال ينسخ المزوّد (لا يغيّر حالته) ويحترم سقف الإنفاق اليومي ويسجّل كلفته في العدّاد.
"""
import copy
import html
import json
import math
import os
import re
import time
import urllib.parse
from typing import Any, Dict, Mapping, Optional

from assistant.models.http_base import HttpProvider, ProviderConfigError, ProviderError, Usage
from assistant.redact import redact_key as _redact_key
from assistant.models.openai_compat import OpenAICompatProvider
from assistant.models.anthropic_provider import AnthropicProvider
from assistant.models.gemini_provider import GeminiProvider
from assistant.models.pricing import cost_usd
from assistant.models.router import estimate_max_cost

# (المفتاح الداخلي، النص الظاهر)
PROVIDER_CHOICES = [
    ("auto", "تلقائي (حسب المفاتيح المتوفرة)"),
    ("gemini", "Gemini (Google)"),
    ("anthropic", "Anthropic (Claude)"),
    ("openai", "OpenAI"),
    ("ollama", "Ollama (محلي ومجاني)"),
    ("demo", "الوضع المحلي (بلا إنترنت)"),
]
CHOICE_KEYS = tuple(k for k, _ in PROVIDER_CHOICES)
KEY_ENV = {"gemini": "GEMINI_API_KEY", "anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY"}
KEY_LABEL = {"gemini": "Gemini", "anthropic": "Anthropic", "openai": "OpenAI"}
SAFE_CONFIG_KEYS = ("provider", "small_model", "large_model", "daily_cap_usd")   # كل ما يُكتب في config.json من هذه النافذة (غير workspace)
MAX_DAILY_CAP_USD = 1000.0
PING_MAX_TOKENS = 24
PING_PROMPT = 'ping. Reply with the JSON object {"ok": true} only.'
RULES_ONLY_TEXT = "لا يوجد نموذج — أعمل بالقواعد المحلية فقط"
SESSION_ONLY_NOTE = "لا يُحفظ على القرص — يبقى في ذاكرة هذه الجلسة فقط ويُمسح عند إغلاق البرنامج"
MSG_APPLY_FAILED = "تعذّر تطبيق الإعدادات. تأكد من المفتاح واسم النموذج ثم جرّب «فحص اتصال الخدمة»."
MIN_KEY_LEN = 8

_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/\-]{0,99}$")
_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹٫،", "01234567890123456789.,")


class SettingsError(Exception):
    """إعداد غير صالح (رسالة عربية جاهزة للعرض، لا تحوي المفتاح)."""


# ---------- التحقق ----------
def infer_key_kind(key: str) -> Optional[str]:
    k = (key or "").strip()
    if k.startswith("sk-ant-"):
        return "anthropic"
    if k.startswith("AIza"):
        return "gemini"
    if k.startswith("sk-"):
        return "openai"
    return None


def validate_key(key: str) -> str:
    k = (key or "").strip()
    if not k:
        return ""
    if len(k) > 400 or any(ch.isspace() or ord(ch) < 33 or ord(ch) > 126 for ch in k):
        raise SettingsError("المفتاح فيه مسافات أو رموز غير مقبولة. انسخه من جديد كما هو.")
    if len(k) < MIN_KEY_LEN:
        raise SettingsError("المفتاح قصير جداً. انسخه كاملاً من موقع الخدمة.")
    return k


def validate_model(name: str) -> str:
    n = (name or "").strip()
    if not n:
        return ""
    if not _MODEL_RE.match(n):
        raise SettingsError("اسم النموذج يُكتب بحروف إنجليزية وأرقام فقط، مثل «gemini-2.5-flash».")
    return n


def parse_cap(value) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, bool):
        raise SettingsError("اكتب السقف اليومي رقماً بالدولار، مثل 1 أو 0.5.")
    if isinstance(value, (int, float)):
        v = float(value)
    else:
        s = str(value).strip().translate(_DIGITS).replace(",", ".").replace("$", "").strip()
        if not s:
            return None
        try:
            v = float(s)
        except ValueError:
            raise SettingsError("اكتب السقف اليومي رقماً بالدولار، مثل 1 أو 0.5.")
    if not math.isfinite(v) or v < 0 or v > MAX_DAILY_CAP_USD:
        raise SettingsError(f"السقف اليومي يجب أن يكون بين 0 و{MAX_DAILY_CAP_USD:g} دولار.")
    return v


def normalize(values: Mapping[str, Any]) -> Dict[str, Any]:
    """يتحقق من قيم النافذة ويعيد قاموساً نظيفاً. المفتاح (إن وُجد) يمر هنا فقط إلى المتحكم."""
    choice = str(values.get("provider") or "auto").strip().lower()
    if choice not in CHOICE_KEYS:
        raise SettingsError("اختر الخدمة من القائمة.")
    key = validate_key(values.get("api_key") or "")
    if key and choice in ("ollama", "demo"):
        raise SettingsError("هذا الخيار لا يحتاج مفتاحاً. امسح خانة المفتاح أو اختر خدمة سحابية.")
    kind = infer_key_kind(key) if key else None
    if key and choice == "auto":
        if kind is None:
            raise SettingsError("لم أتعرّف على نوع المفتاح. اختر الخدمة صراحةً (Gemini أو Anthropic أو OpenAI) ثم الصق المفتاح.")
    elif key and kind is not None and kind != choice:   # أي نوع معروف مخالف للخدمة المختارة (حتى sk- لـ OpenAI مع Gemini/Anthropic): لا يُرسل المفتاح لمورّد خاطئ
        raise SettingsError(f"هذا المفتاح يخص {KEY_LABEL[kind]} بينما الخدمة المختارة {dict(PROVIDER_CHOICES)[choice]}. غيّر أحدهما ليتطابقا.")
    return {
        "provider": choice,
        "api_key": key,
        "key_kind": (kind if choice == "auto" else choice) if key else None,
        "clear_key": bool(values.get("clear_key")),
        "small_model": validate_model(values.get("small_model") or ""),
        "large_model": validate_model(values.get("large_model") or ""),
        "workspace": str(values.get("workspace") or "").strip(),
        "daily_cap": parse_cap(values.get("daily_cap")),
    }


# ---------- البيئة والإعدادات المحفوظة ----------
def build_env(base: Mapping[str, str], choice: str, secrets: Mapping[str, str], small: str = "", large: str = "") -> Dict[str, str]:
    """قاموس البيئة الذي يُمرَّر إلى build_provider. المفاتيح المُدخلة تتقدم على المحيطة."""
    env = {k: v for k, v in base.items() if isinstance(k, str) and isinstance(v, str)}
    env["COS_PROVIDER"] = choice
    env.update({k: v for k, v in secrets.items() if v})
    if small:
        env["COS_SMALL_MODEL"] = small
    if large:
        env["COS_LARGE_MODEL"] = large
    return env


def key_available(env: Mapping[str, str], choice: str) -> bool:
    if choice == "gemini":
        return bool((env.get("GEMINI_API_KEY") or env.get("GOOGLE_API_KEY") or "").strip())
    if choice in ("anthropic", "openai"):
        return bool((env.get(KEY_ENV[choice]) or "").strip())
    return True


def load_saved(cfg: Mapping[str, Any]) -> Dict[str, Any]:
    """المفاتيح الآمنة الصالحة فقط من config.json (أي قيمة تالفة تُتجاهل)."""
    out: Dict[str, Any] = {}
    p = cfg.get("provider")
    if isinstance(p, str) and p in CHOICE_KEYS:
        out["provider"] = p
    for k in ("small_model", "large_model"):
        try:
            v = validate_model(cfg.get(k) or "")
        except SettingsError:
            v = ""
        if v:
            out[k] = v
    try:
        cap = parse_cap(cfg.get("daily_cap_usd"))
    except SettingsError:
        cap = None
    if cap is not None:
        out["daily_cap_usd"] = cap
    return out


def startup_env(environ: Mapping[str, str], cfg: Mapping[str, Any]) -> Dict[str, str]:
    """بيئة التشغيل: متغيرات البيئة الصريحة تتقدم على المحفوظ في config.json (الذي لا يحوي مفاتيح أبداً)."""
    env = {k: v for k, v in environ.items() if isinstance(v, str)}
    saved = load_saved(cfg)
    if not (env.get("COS_PROVIDER") or "").strip() and saved.get("provider"):
        env["COS_PROVIDER"] = saved["provider"]
    if not (env.get("COS_SMALL_MODEL") or "").strip() and saved.get("small_model"):
        env["COS_SMALL_MODEL"] = saved["small_model"]
    if not (env.get("COS_LARGE_MODEL") or "").strip() and saved.get("large_model"):
        env["COS_LARGE_MODEL"] = saved["large_model"]
    return env


def saved_cap(environ: Mapping[str, str], cfg: Mapping[str, Any]) -> Optional[float]:
    """سقف config.json يُستعمل فقط إن لم يحدد COS_MAX_USD_PER_DAY صراحةً."""
    if (environ.get("COS_MAX_USD_PER_DAY") or "").strip():
        return None
    return load_saved(cfg).get("daily_cap_usd")


def safe_config_update(cfg: Mapping[str, Any], norm: Mapping[str, Any]) -> Dict[str, Any]:
    """نسخة من config فيها المفاتيح غير السرية المحدّثة؛ لا يمر منها مفتاح API بأي حال."""
    out = dict(cfg)
    out["provider"] = norm["provider"]
    for k in ("small_model", "large_model"):
        if norm.get(k):
            out[k] = norm[k]
        else:
            out.pop(k, None)
    if norm.get("daily_cap") is not None:
        out["daily_cap_usd"] = norm["daily_cap"]
    for k in list(out):   # حزام أمان: أي اسم يشبه سراً لا يُكتب
        if re.search(r"key|secret|token|password", str(k), re.I):
            out.pop(k, None)
    return out


_KNOWN_SECRETS: set = set()   # مفاتيح رأيناها في هذه العملية (ذاكرة فقط): تُحجب من أي سجل/رسالة حتى لو فشل التطبيق


def remember_secrets(values) -> None:
    for v in values:
        v = str(v or "").strip()
        if len(v) >= 8:
            _KNOWN_SECRETS.add(v)


ENV_KEY_NAMES = ("ANTHROPIC_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY", "OPENAI_API_KEY", "COS_OPENAI_API_KEY")


def seed_env_secrets(environ=None) -> None:
    """مفاتيح الخدمة القادمة من البيئة (لم تُلصق في النافذة) تدخل سجل الحجب منذ البدء."""
    env = os.environ if environ is None else environ
    remember_secrets(env.get(n) for n in ENV_KEY_NAMES)


def provider_keys(provider) -> list:
    """مفاتيح API التي يحملها المزوّد وفروعه (الموجّه/القواعد أولاً): تُحجب حتى لو بُني المزوّد من قاموس بيئة لا من os.environ."""
    out, seen, stack = [], set(), [provider]
    while stack:
        p = stack.pop()
        if p is None or id(p) in seen:
            continue
        seen.add(id(p))
        k = getattr(p, "_api_key", None)
        if isinstance(k, str) and k:
            out.append(k)
        stack.append(getattr(p, "fallback", None))
        d = getattr(p, "providers", None)
        if isinstance(d, dict):
            stack.extend(d.values())
    return out


def redact_known(text: Any) -> str:
    """حجب شامل لنص سيُكتب أو يُعرض: سجل المفاتيح المعروفة (ذاكرة)، ثم قيم البيئة السرية وأنماط المفاتيح المعروفة (assistant.redact)."""
    s = redact(text, list(_KNOWN_SECRETS))
    try:
        from assistant.redact import redact as _pattern_redact
        s = _pattern_redact(s, os.environ)
    except Exception:
        pass
    return s


def redact(text: Any, secrets) -> str:
    s = str(text)
    for sec in secrets:
        if sec and len(sec) >= MIN_KEY_LEN:
            s = _redact_key(s, sec)    # صيغ دقيقة + (لمفاتيح >= 16) معكوس/ست عشري/مقطّع/أجزاء >= 12 محرفاً
    return s


# ---------- وصف المزوّد الفعّال ----------
def real_providers(provider):
    """(صغير، كبير) من سلسلة RuleFirst/Router، أو (None, None) إن لم يكن هناك نموذج حقيقي."""
    inner = getattr(provider, "fallback", None) or provider
    prov = getattr(inner, "providers", None)
    if isinstance(prov, dict) and prov:
        return prov.get("small"), prov.get("large") or prov.get("small")
    if isinstance(inner, HttpProvider):
        return inner, inner
    return None, None


def _is_free(p) -> bool:
    price = getattr(p, "price_per_mtok", None)
    return price is not None and not any(price)


def describe_provider(provider, cap: Optional[float] = None) -> Dict[str, Any]:
    """وصف للواجهة: kind (rules|gemini|anthropic|openai|ollama|local|openai_compat|other)، label، cost (free|paid|none)، models، line."""
    small, large = real_providers(provider)
    cap_txt = f"{cap:g} دولار" if isinstance(cap, (int, float)) else "المحدد"
    if small is None:
        if hasattr(provider, "analyze") or provider.__class__.__name__ in ("RuleProvider", "DemoProvider"):
            return {"kind": "rules", "label": "القواعد المحلية فقط", "cost": "free", "models": (None, None),
                    "line": "الوضع الحالي: محلي (بلا إنترنت) — مجاني تماماً ولا يغادر أي شيء جهازك. لاستعمال النموذج الذكي افتح «الإعدادات»."}
        try:
            nm = str(provider.name())
        except Exception:
            nm = provider.__class__.__name__
        return {"kind": "other", "label": nm, "cost": "none", "models": (None, None), "line": f"الخدمة الحالية: {nm}."}
    models = (getattr(small, "model", None), getattr(large, "model", None))
    if isinstance(small, AnthropicProvider):
        kind, label = "anthropic", "Anthropic (Claude)"
    elif isinstance(small, GeminiProvider):
        kind, label = "gemini", "Gemini (Google)"
    elif isinstance(small, OpenAICompatProvider):
        if getattr(small, "ollama", False):
            kind, label = "ollama", "Ollama (محلي)"
        elif getattr(small, "local", False):
            kind, label = "local", "خادم محلي"
        elif small.PROVIDER_LABEL == "OpenAI":
            kind, label = "openai", "OpenAI"
        else:
            kind, label = "openai_compat", str(small.PROVIDER_LABEL)
    else:
        kind, label = "other", str(getattr(small, "PROVIDER_LABEL", small.__class__.__name__))
    free = _is_free(small)
    if free:
        cost_text = "مجاني؛ يعمل على جهازك ولا تغادر بياناتك جهازك"
    else:
        cost_text = f"مدفوع بحسب الاستعمال (سقفك اليومي {cap_txt})"
        if kind == "gemini":
            cost_text += ". قد يكون لحسابك حصة مجانية، لكن المساعد يحسبه مدفوعاً للاحتياط"
    m = models[0] if models[0] == models[1] or not models[1] else f"{models[0]} / {models[1]}"
    return {"kind": kind, "label": label, "cost": "free" if free else "paid", "models": models,
            "line": f"الخدمة الذكية الحالية: {label} (النموذج «{m}») — {cost_text}. الطلبات التي أفهمها بنفسي تُنفَّذ محلياً بلا كلفة."}


def choice_of(provider) -> str:
    k = describe_provider(provider)["kind"]
    return {"rules": "demo", "gemini": "gemini", "anthropic": "anthropic", "openai": "openai", "ollama": "ollama"}.get(k, "auto")


# ---------- فحص الاتصال ----------
def run_ping(provider, usage_meter=None, cancel_check=None, secrets=()) -> Dict[str, Any]:
    """طلب صغير جداً على نسخة من المزوّد الصغير (لا يمس حالة المزوّد الفعّال). يحترم سقف الإنفاق ويسجّل الكلفة.
    يعيد {ok, text, kind, label, model, ms, usd}."""
    info = describe_provider(provider)
    small, _ = real_providers(provider)
    if small is None:
        if info["kind"] == "rules":
            return {"ok": True, "text": RULES_ONLY_TEXT, "kind": "rules", "label": info["label"], "model": None, "ms": 0, "usd": 0.0}
        return _ping_generic(provider, info, secrets)
    if not isinstance(small, HttpProvider):
        return _ping_generic(small, info, secrets)
    c = copy.copy(small)                     # نسخة سطحية: لا نغيّر max_tokens ولا العدّادات في المزوّد الحي
    c.usage = Usage()
    c.max_tokens = PING_MAX_TOKENS
    c.last_usage = (0, 0)
    c.cancel_check = cancel_check
    label, model = info["label"], c.model
    free = _is_free(c)
    token = None
    t0 = time.monotonic()
    ok, text, note = False, "", ""

    def fail(msg):
        return {"ok": False, "text": "تعذّر الاتصال: " + redact(msg, list(secrets) + [getattr(c, "_api_key", "")]), "kind": info["kind"],
                "label": label, "model": model, "ms": int((time.monotonic() - t0) * 1000), "usd": 0.0}
    try:
        c._require_key()
        if usage_meter is not None and not free:
            est = estimate_max_cost(c, PING_PROMPT, "")
            if est > 0:
                token = usage_meter.reserve(est)
                if token is None:
                    return fail(usage_meter.reserve_denied_message(est))
        t0 = time.monotonic()
        try:
            c.generate(PING_PROMPT, "")
            ok = True
        except ProviderConfigError:
            raise
        except ProviderError as exc:
            if "فارغ" in str(exc) and tuple(c.last_usage) != (0, 0):   # الخدمة ردّت (200) ولو بنص فارغ بسبب حد التوكنات: الاتصال سليم
                ok = True
            else:
                raise
    except ProviderError as exc:
        err = str(exc)
        return fail(err)
    except Exception as exc:  # noqa: BLE001
        return fail("حدث خلل غير متوقع أثناء الفحص.")
    finally:
        if token is not None:
            try:
                usage_meter.release(token)
            except Exception:
                pass
    ms = int((time.monotonic() - t0) * 1000)
    tin, tout = c.last_usage
    usd = 0.0 if free else cost_usd(model, tin, tout, getattr(c, "price_per_mtok", None))
    if usd > 0 and usage_meter is not None:
        try:
            usage_meter.add(usd)
        except Exception:
            pass
    cost_note = "بلا كلفة" if free else ("الكلفة أقل من سنت" if usd < 0.01 else f"الكلفة نحو {usd:.2f} دولار")
    return {"ok": True, "text": f"الاتصال يعمل ✔ — {label}، النموذج «{model}»، استجابة خلال {ms / 1000:.1f} ثانية، {cost_note}.",
            "kind": info["kind"], "label": label, "model": model, "ms": ms, "usd": usd}


def _ping_generic(p, info, secrets) -> Dict[str, Any]:
    t0 = time.monotonic()
    try:
        p.generate(PING_PROMPT, "")
    except Exception as exc:  # noqa: BLE001
        msg = str(exc) if isinstance(exc, ProviderError) else "حدث خلل غير متوقع أثناء الفحص."
        return {"ok": False, "text": "تعذّر الاتصال: " + redact(msg, secrets), "kind": info["kind"], "label": info["label"], "model": None,
                "ms": int((time.monotonic() - t0) * 1000), "usd": 0.0}
    ms = int((time.monotonic() - t0) * 1000)
    return {"ok": True, "text": f"الاتصال يعمل ✔ — {info['label']}، استجابة خلال {ms / 1000:.1f} ثانية.", "kind": info["kind"],
            "label": info["label"], "model": None, "ms": ms, "usd": 0.0}


# ---------- النافذة ----------
FONT = ("Segoe UI", 10)
FONT_B = ("Segoe UI", 10, "bold")


class SettingsDialog:
    """نافذة الإعدادات. الزر الأخير في الأسفل يجب أن يكون آخر ابن لـ Toplevel (اتفاق الاختبارات)."""

    def __init__(self, widget):
        import tkinter as tk
        from tkinter import ttk
        self.widget = widget
        self.controller = widget.controller
        cur = self.controller.current_settings()
        root = widget.root
        win = self.win = tk.Toplevel(root)
        win.title("الإعدادات")
        win.attributes("-topmost", True)
        win.configure(bg="#f5f5f5")
        win.transient(root)
        pad = dict(padx=10, pady=3)
        body = tk.Frame(win, bg="#f5f5f5")
        body.pack(fill="both", expand=True, padx=8, pady=8)
        body.columnconfigure(0, weight=1)
        labels = dict(PROVIDER_CHOICES)
        self._label_to_key = {v: k for k, v in PROVIDER_CHOICES}
        self.v_provider = tk.StringVar(value=labels.get(cur["provider"], labels["auto"]))
        self.v_key = tk.StringVar(value="")
        self.v_clear = tk.IntVar(value=0)
        self.v_small = tk.StringVar(value=cur.get("small_model", ""))
        self.v_large = tk.StringVar(value=cur.get("large_model", ""))
        self.v_ws = tk.StringVar(value=cur.get("workspace", ""))
        self.v_cap = tk.StringVar(value=f"{cur['daily_cap']:g}" if cur.get("daily_cap") is not None else "")
        row = 0

        def lab(text, r, fg="#263238", font=FONT, wrap=0):
            l = tk.Label(body, text=text, bg="#f5f5f5", fg=fg, font=font, anchor="e", justify="right", wraplength=wrap)
            l.grid(row=r, column=0, sticky="e", **pad)
            return l
        self.cur_lbl = lab(cur["line"], row, "#37474f", ("Segoe UI", 9), widget.px(380))
        row += 1
        lab("الخدمة", row)
        self.combo = ttk.Combobox(body, textvariable=self.v_provider, values=[v for _, v in PROVIDER_CHOICES], state="readonly", justify="right")
        self.combo.grid(row=row + 1, column=0, sticky="ew", **pad)
        row += 2
        lab("مفتاح الخدمة (API) — اختياري، لهذه الجلسة فقط", row)
        self.key_entry = tk.Entry(body, textvariable=self.v_key, show="•", font=FONT, justify="left", relief="solid", bd=1)
        self.key_entry.grid(row=row + 1, column=0, sticky="ew", **pad)
        hint = SESSION_ONLY_NOTE + ("\nيوجد مفتاح محفوظ في هذه الجلسة؛ اتركه فارغاً للإبقاء عليه." if cur.get("has_session_key") else "")
        lab(hint, row + 2, "#2e7d32", ("Segoe UI", 9), widget.px(380))
        row += 3
        if cur.get("has_session_key"):
            self.clear_chk = tk.Checkbutton(body, text="احذف المفتاح المحفوظ في هذه الجلسة", variable=self.v_clear, bg="#f5f5f5", font=("Segoe UI", 9))
            self.clear_chk.grid(row=row, column=0, sticky="e", **pad)
            row += 1
        lab("النموذج السريع (اتركه فارغاً للافتراضي)", row)
        self.small_entry = tk.Entry(body, textvariable=self.v_small, font=FONT, justify="left", relief="solid", bd=1)
        self.small_entry.grid(row=row + 1, column=0, sticky="ew", **pad)
        lab("النموذج الأقوى (اختياري)", row + 2)
        self.large_entry = tk.Entry(body, textvariable=self.v_large, font=FONT, justify="left", relief="solid", bd=1)
        self.large_entry.grid(row=row + 3, column=0, sticky="ew", **pad)
        row += 4
        lab("مجلد العمل", row)
        wsrow = tk.Frame(body, bg="#f5f5f5")
        wsrow.grid(row=row + 1, column=0, sticky="ew", **pad)
        wsrow.columnconfigure(1, weight=1)
        tk.Button(wsrow, text="اختر…", font=FONT, relief="groove", command=self._browse).grid(row=0, column=0, padx=(0, 4))
        self.ws_entry = tk.Entry(wsrow, textvariable=self.v_ws, font=FONT, justify="left", relief="solid", bd=1)
        self.ws_entry.grid(row=0, column=1, sticky="ew")
        row += 2
        lab("السقف اليومي للإنفاق بالدولار", row)
        self.cap_entry = tk.Entry(body, textvariable=self.v_cap, font=FONT, justify="left", relief="solid", bd=1, width=10)
        self.cap_entry.grid(row=row + 1, column=0, sticky="e", **pad)
        row += 2
        self.msg_lbl = tk.Label(body, text="", bg="#f5f5f5", fg="#b71c1c", font=("Segoe UI", 9), anchor="e", justify="right", wraplength=widget.px(380))
        self.msg_lbl.grid(row=row, column=0, sticky="e", **pad)
        btns = tk.Frame(win, bg="#f5f5f5")   # آخر ابن
        btns.pack(side="bottom", fill="x", padx=8, pady=(0, 8))
        self.apply_btn = tk.Button(btns, text="تطبيق", font=FONT_B, bg="#2e7d32", fg="white", relief="flat", command=self.apply)
        self.apply_btn.pack(side="right", padx=3)
        self.ping_btn = tk.Button(btns, text="فحص اتصال الخدمة", font=FONT, relief="groove", command=self.ping)
        self.ping_btn.pack(side="right", padx=3)
        self.close_btn = tk.Button(btns, text="إغلاق", font=FONT, relief="flat", command=self.close)
        self.close_btn.pack(side="left", padx=3)
        win.protocol("WM_DELETE_WINDOW", self.close)
        try:
            self.key_entry.focus_set()
        except Exception:
            pass
        for ent in (self.key_entry, self.small_entry, self.large_entry, self.ws_entry, self.cap_entry):
            try:
                widget._bind_keycodes(ent)   # لصق/نسخ بتخطيط عربي في كل الحقول
            except Exception:
                pass
            for seq in ("<Return>", "<KP_Enter>"):
                ent.bind(seq, lambda e: self.apply())

    # ---- منطق ----
    def collect(self) -> Dict[str, Any]:
        return {"provider": self._label_to_key.get(self.v_provider.get(), "auto"), "api_key": self.v_key.get(),
                "clear_key": bool(self.v_clear.get()), "small_model": self.v_small.get(), "large_model": self.v_large.get(),
                "workspace": self.v_ws.get(), "daily_cap": self.v_cap.get()}

    def show_result(self, text: str, ok: bool = False):
        try:
            self.msg_lbl.configure(text=text, fg="#1b5e20" if ok else "#b71c1c")
        except Exception:
            pass

    def _browse(self):
        from tkinter import filedialog
        p = filedialog.askdirectory(title="اختر مجلد العمل", initialdir=self.v_ws.get() or None, parent=self.win)
        if p:
            self.v_ws.set(p)

    def apply(self):
        ok = self.controller.apply_settings(self.collect())
        if ok:
            self.v_key.set("")   # لا يبقى المفتاح في حقل الإدخال
            self.close()
        else:
            self.show_result(self.controller.last_settings_message or MSG_APPLY_FAILED, False)
        return ok

    def ping(self):
        self.show_result("أفحص اتصال الخدمة…", True)
        self.widget._on_ping()

    def close(self):
        try:
            self.v_key.set("")
        except Exception:
            pass
        try:
            self.win.destroy()
        except Exception:
            pass
        if getattr(self.widget, "_settings_dlg", None) is self:
            self.widget._settings_dlg = None


def open_settings_dialog(widget) -> "SettingsDialog":
    old = getattr(widget, "_settings_dlg", None)
    if old is not None:
        try:
            old.win.lift()
            return old
        except Exception:
            pass
    dlg = SettingsDialog(widget)
    widget._settings_dlg = dlg
    return dlg
