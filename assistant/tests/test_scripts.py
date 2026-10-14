"""ملفات .bat: نهايات أسطر CRLF، ASCII فقط (لتفادي مشاكل الترميز)، وبلا أي مفتاح."""
import sys
import pathlib
import traceback

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent

try:
    bats = sorted(ROOT.glob("*.bat")) + sorted((ROOT / "tools").glob("*.bat"))
    new_ones = [p for p in bats if p.name in ("run_assistant.bat", "run_bench.bat", "build_exe.bat")]
    assert len(new_ones) == 3, [p.name for p in bats]
    for p in new_ones:
        raw = p.read_bytes()
        raw.decode("ascii")  # يرفع إن وُجد غير ASCII
        low = raw.lower()
        assert b"sk-ant" not in low and b"api_key=" not in low.replace(b"set anthropic_api_key=", b""), p.name
    for p in bats:  # كل .bat بنهايات CRLF صرفة (LF منفرد يكسر أوامر cmd أحياناً)
        raw = p.read_bytes()
        assert b"\r\n" in raw and b"\n" not in raw.replace(b"\r\n", b""), f"{p.name}: يجب CRLF صرف"
    assert (ROOT / "launch.py").is_file()
    ga = (ROOT / ".gitattributes").read_text(encoding="utf-8")
    assert "*.bat text eol=crlf" in ga, "يجب فرض CRLF لملفات bat عبر .gitattributes"
    print("PASS: test_scripts completed successfully")
    sys.exit(0)
except SystemExit:
    raise
except BaseException:
    traceback.print_exc()
    print("FAIL: test_scripts", file=sys.stderr)
    sys.exit(1)
