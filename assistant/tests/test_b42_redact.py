"""حجب مفتاح معروف بصيغ محوّلة (معكوس، ست عشري، مقطّع بشرطات/مسافات، أي جزء متصل >= 12 محرفاً) للمفاتيح >= 16 فقط؛
مفاتيح وهمية فقط. يغطي redact_key ومزوّد HTTP (_redact) وإعدادات الواجهة (settings.redact) ومسار الخطأ الكامل من خادم محلي يعكس المفتاح."""
import base64
import http.server
import json
import pathlib
import sys
import threading
import traceback

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from assistant.redact import redact_key, KEY_FRAGMENT_LEN
    from assistant.models.openai_compat import OpenAICompatProvider
    from assistant.models.http_base import ProviderError
    from assistant.ui import settings as S

    KEY = "sk" + "-test-FAKE-" + "Zq8Rt5" * 5          # وهمي، 41 محرفاً
    ODD = "sk" + '-a"b\\c<d&e' + "Xy7Kp" * 4              # فيه محارف خاصة
    SHORT = "sk" + "-short1"                              # < 16: لا صيغ محوّلة

    def forms(k):
        raw = k.encode()
        return {
            "raw": k, "upper": k.upper(), "reversed": k[::-1],
            "hex": raw.hex(), "hex-upper": raw.hex().upper(), "hex-spaced": " ".join(f"{b:02x}" for b in raw),
            "dash4": "-".join(k[i:i + 4] for i in range(0, len(k), 4)),
            "space5": " ".join(k[i:i + 5] for i in range(0, len(k), 5)),
            "dots": ".".join(k[i:i + 3] for i in range(0, len(k), 3)),
            "first16": k[:16], "last12": k[-12:], "mid14": k[7:21], "mid12": k[9:21],
            "base64": base64.b64encode(raw).decode(),
        }

    # ---------- 1) redact_key ----------
    for key in (KEY, ODD):
        for name, f in forms(key).items():
            out = redact_key(f"error: bad header {f} (retry)", key)
            assert f.lower() not in out.lower(), (name, out)
            assert out.startswith("error: bad header ") and out.endswith(" (retry)"), out
    # جزء أطول من 12 يُحجب كله لا أول 12 فقط
    frag = KEY[3:30]
    assert redact_key(f"<{frag}>", KEY) == "<***>", redact_key(f"<{frag}>", KEY)
    # جزء 11 محرفاً لا يُحجب (حدّ الإيجابيات الكاذبة)
    eleven = KEY[5:5 + KEY_FRAGMENT_LEN - 1]
    assert redact_key(f"a {eleven} b", KEY) == f"a {eleven} b"
    # مفتاح < 16: الصيغ الدقيقة فقط، لا أجزاء ولا معكوس
    assert redact_key(f"k={SHORT}", SHORT) == "k=***"
    assert redact_key(f"k={SHORT[::-1]} {SHORT[:8]}", SHORT) == f"k={SHORT[::-1]} {SHORT[:8]}"
    # نصوص عادية طويلة لا تتأثر
    plain = "تم نقل 12 ملفاً إلى المجلد disk-cleanup-plan بنجاح 0123456789abcdef " * 3
    assert redact_key(plain, KEY) == plain
    assert redact_key("", KEY) == "" and redact_key("abc", "") == "abc"

    # ---------- 2) مزوّد HTTP ----------
    p = OpenAICompatProvider(model="m", base_url="http://127.0.0.1:1/v1", api_key=KEY)
    for name, f in forms(KEY).items():
        assert f.lower() not in p._redact("echo " + f).lower(), name
    p2 = OpenAICompatProvider(model="m", base_url="http://127.0.0.1:1/v1", api_key=ODD)
    for name, f in forms(ODD).items():
        assert f.lower() not in p2._redact("echo " + f).lower(), name

    # ---------- 3) settings.redact (سجل المفاتيح المعروفة) ----------
    for name, f in forms(KEY).items():
        assert f.lower() not in S.redact(f"x {f} y", [KEY]).lower(), name
    S.remember_secrets([KEY])
    try:
        for name, f in forms(KEY).items():
            assert f.lower() not in S.redact_known(f"x {f} y").lower(), name
    finally:
        S._KNOWN_SECRETS.discard(KEY)

    # ---------- 4) من النهاية إلى النهاية: خادم محلي يعكس صيغاً محوّلة ----------
    class H(http.server.BaseHTTPRequestHandler):
        variant = ""

        def log_message(self, *a):
            pass

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            self.rfile.read(n)
            body = json.dumps({"error": {"message": "bad key " + H.variant}}).encode()
            self.send_response(418)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        for key in (KEY, ODD):
            for name, f in forms(key).items():
                if name == "base64":
                    continue
                H.variant = f
                prov = OpenAICompatProvider(model="m", base_url=f"http://127.0.0.1:{srv.server_port}/v1", api_key=key, max_retries=0,
                                            sleep=lambda s: None)
                try:
                    prov.generate("x", "")
                    raise AssertionError("expected ProviderError")
                except ProviderError as exc:
                    blob = (str(exc) + "|" + exc.body_lc + "|" + repr(exc) + "|" + "".join(traceback.format_exception(exc))).lower()
                    assert f.lower() not in blob, (name, blob)
    finally:
        srv.shutdown()
        srv.server_close()

    print("PASS: test_b42_redact completed successfully")
except Exception:
    traceback.print_exc()
    print("FAIL: test_b42_redact")
    sys.exit(1)
