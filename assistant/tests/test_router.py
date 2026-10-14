"""اختبارات الموجّه والمصنع: اختيار الطبقة، الذاكرة المؤقتة، القصّ، الكلفة بالنقاط، وغياب المفاتيح."""
import json
import sys
import pathlib
import traceback

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from assistant.models.anthropic_provider import AnthropicProvider
    from assistant.models.router import ModelRouter, truncate_text, SMALL, LARGE
    from assistant.models.factory import build_provider
    from assistant.models.http_base import ProviderConfigError
    from assistant.models.demo import DemoProvider
    from assistant.models.rules import RuleFirstProvider
    from assistant.models.pricing import cost_usd, usd_to_points, COST_USD_PER_POINT
    from assistant.planner import PLANNER_SYSTEM

    def net_for(label, calls):
        def transport(url, headers, body, timeout):
            calls.append((label, json.loads(body.decode())["model"]))
            return 200, json.dumps({"content": [{"type": "text", "text": f"رد-{label}"}],
                                    "usage": {"input_tokens": 1_000_000, "output_tokens": 0}}).encode()
        return transport

    calls = []
    small = AnthropicProvider("claude-haiku-5-5", api_key="k", transport=net_for("s", calls))
    large = AnthropicProvider("claude-sonnet-5-5", api_key="k", transport=net_for("l", calls))
    r = ModelRouter(small, large)

    # اختيار الطبقة
    assert ModelRouter.choose_tier("x", "CLARIFY — أسئلة") == SMALL
    assert ModelRouter.choose_tier("x", PLANNER_SYSTEM) == LARGE
    assert ModelRouter.choose_tier("x", "") == SMALL
    assert r.generate("p", system_prompt=PLANNER_SYSTEM) == "رد-l"
    assert r.generate("p", system_prompt="CLARIFY") == "رد-s"
    assert [c[1] for c in calls] == ["claude-sonnet-5-5", "claude-haiku-5-5"]

    # ذاكرة مؤقتة: نفس الطلب لا يستدعي الشبكة ولا يُكلّف
    n = len(calls)
    assert r.generate("p", system_prompt=PLANNER_SYSTEM) == "رد-l" and len(calls) == n and r.cache_hits == 1
    t = r.totals()
    assert t["calls"] == 2 and t["cache_hits"] == 1
    # مليون توكن إدخال لكل استدعاء: sonnet 2$ + haiku 0.10$
    assert abs(t["usd"] - 2.10) < 1e-9 and abs(t["points"] - 2.10 / COST_USD_PER_POINT) < 1e-6

    # تعطيل الذاكرة المؤقتة (للقياس)
    r.cache_size = 0
    r.generate("p", system_prompt=PLANNER_SYSTEM)
    assert len(calls) == n + 1

    # القصّ
    long = "ا" * 20000
    cut = truncate_text(long, 1000)
    assert len(cut) < 1100 and "اقتُطع" in cut and truncate_text("قصير", 1000) == "قصير"

    # التسعير
    assert abs(cost_usd("claude-sonnet-5-5", 1_000_000, 1_000_000) - 12.0) < 1e-9
    assert cost_usd("unknown-model", 10, 10) == 0.0 and usd_to_points(0.06) == 2.0

    # المصنع
    prov, note = build_provider({})
    assert isinstance(prov, DemoProvider) and "المحلي" in note
    try:
        build_provider({"COS_PROVIDER": "anthropic"})
        raise AssertionError("كان يجب طلب المفتاح")
    except ProviderConfigError as exc:
        assert "ANTHROPIC_API_KEY" in str(exc)
    prov, note = build_provider({"COS_PROVIDER": "anthropic", "ANTHROPIC_API_KEY": "k"})
    assert isinstance(prov, RuleFirstProvider) and isinstance(prov.fallback, ModelRouter) and "claude-haiku-5-5" in note and "claude-sonnet-5-5" in note
    prov, _ = build_provider({"COS_PROVIDER": "gemini", "GEMINI_API_KEY": "k"})
    assert prov.fallback.providers[SMALL].model.startswith("gemini")
    prov, _ = build_provider({"COS_PROVIDER": "auto", "GOOGLE_API_KEY": "k"})
    assert prov.fallback.providers[SMALL].model.startswith("gemini")
    prov, _ = build_provider({"COS_PROVIDER": "auto"})
    assert isinstance(prov, DemoProvider)
    prov, _ = build_provider({"COS_PROVIDER": "anthropic", "ANTHROPIC_API_KEY": "k", "COS_SMALL_MODEL": "m1", "COS_LARGE_MODEL": "m1"})
    assert prov.fallback.providers[SMALL] is prov.fallback.providers[LARGE]
    try:
        build_provider({"COS_PROVIDER": "nonsense"})
        raise AssertionError
    except ProviderConfigError:
        pass

    print("PASS: test_router completed successfully")
    sys.exit(0)
except SystemExit:
    raise
except BaseException:
    traceback.print_exc()
    print("FAIL: test_router", file=sys.stderr)
    sys.exit(1)
