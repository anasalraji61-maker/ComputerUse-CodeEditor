"""D7: مقارنة مجلد العمل كله قبل/بعد الخطة. خطط سليمة لا تُفشَل (نسخ/نقل/إعادة تسمية/حذف/مجلدات متداخلة/أسماء فريدة/ملفات المساعد الجانبية)،
وأي تغيير خارج الخطة (ملف جديد، اختفاء ملف، تغيّر حجم) يُفشل التدقيق بشرح عربي. أسماء ASCII في الملفات حيث يمكن."""
import json
import pathlib
import sys
import tempfile
import traceback
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from assistant import verifier as vmod
    from assistant.runner import TaskRunner
    from assistant.ledger import SQLiteLedger
    from assistant.models.fake import FakeProvider
    from assistant.planner import PlanStep

    def prov(*steps):
        return FakeProvider(default_response=json.dumps({"steps": list(steps)}))

    def st(i, action, target, **params):
        return {"step_id": i, "action_type": action, "target": target, "params": params}

    def d7(rep):
        return [e for e in rep.evidence if e.get("type") == "مقارنة مجلد العمل"]

    with tempfile.TemporaryDirectory() as tmp:
        base = pathlib.Path(tmp).resolve()
        root = base / "work"
        root.mkdir()
        ledger = SQLiteLedger(db_path=base / "l.db", initial_balance=500, spending_limit=500)

        def mk(provider, **kw):
            return TaskRunner(root, provider, ledger, **kw)

        # ---------- 1) خطط سليمة: لا إيجابيات كاذبة ----------
        (root / "a.txt").write_text("aaa", encoding="utf-8")
        (root / "b.txt").write_text("bbbb", encoding="utf-8")
        (root / "proj").mkdir()
        (root / "proj" / "x.txt").write_text("x", encoding="utf-8")
        (root / "proj" / "sub").mkdir()
        (root / "proj" / "sub" / "y.txt").write_text("yy", encoding="utf-8")
        (root / "keep.txt").write_text("untouched", encoding="utf-8")
        (root / "arch").mkdir()
        (root / "arch" / "a.txt").write_text("old", encoding="utf-8")   # يجعل نسخ a.txt إلى arch يهبط على «a (1).txt»

        plan = prov(
            st(1, "create_dir", "deep/er/dir"),
            st(2, "copy", "a.txt", dest="arch"),
            st(3, "copy", "proj", dest="proj_copy"),
            st(4, "move", "b.txt", dest="deep/er/dir"),
            st(5, "rename", "keep.txt", new_name="kept.txt"),
            st(6, "move", "proj", dest="arch/proj_moved"),
            st(7, "inspect", "."),
        )
        rep = mk(plan, approver=lambda s: True).run("d7_ok", "x")
        assert rep.success, rep.failures
        ev = d7(rep)
        assert len(ev) == 1 and "لا تغييرات خارج الخطة" in ev[0]["value"], ev
        assert (root / "deep/er/dir/b.txt").exists() and (root / "kept.txt").exists() and (root / "arch/proj_moved/sub/y.txt").exists()
        assert (root / "arch" / "a (1).txt").exists()

        # حذف (إلى السلة) ثم ملفات المساعد الجانبية في الجذر (.trash و.undo_journal*) لا تُحتسب
        rep = mk(prov(st(1, "delete", "kept.txt")), approver=lambda s: True).run("d7_del", "x")
        assert rep.success, rep.failures
        assert d7(rep) and "لا تغييرات خارج الخطة" in d7(rep)[0]["value"]
        (root / ".undo_journal.extra.json").write_text("{}", encoding="utf-8")
        (root / ".trash" / "junk.bin").write_bytes(b"0" * 10)
        (root / ".cos_copy_tmp_0123abcd").write_text("t", encoding="utf-8")
        v = vmod.Verifier(root)
        snap = v.workspace_snapshot()
        assert snap is not None and not any(k.startswith((".trash", ".undo_journal", ".cos_copy_tmp_")) for k in snap), sorted(snap)
        assert "a.txt" in snap and "arch/proj_moved/sub/y.txt" in snap and snap["arch"][0] == "d"

        # ---------- 2) سلبي: تغييرات خارج الخطة تُفشل التدقيق ----------
        (root / "victim1.txt").write_text("v1", encoding="utf-8")
        (root / "victim2.txt").write_text("v2", encoding="utf-8")
        (root / "victim3.txt").write_text("v3", encoding="utf-8")
        (root / "to_delete.txt").write_text("bye", encoding="utf-8")

        def sneaky(step):
            (root / "stray_new.txt").write_text("surprise", encoding="utf-8")        # أُنشئ
            (root / "victim1.txt").unlink()                                          # اختفى
            (root / "victim2.txt").write_text("v2-now-longer", encoding="utf-8")     # تغيّر حجمه
            return True
        rep = mk(prov(st(1, "delete", "to_delete.txt")), approver=sneaky).run("d7_bad", "x")
        assert not rep.success, "extra changes outside the plan must fail verification"
        msg = " ".join(rep.failures)
        assert "تغييرات غير متوقعة في مجلد العمل" in msg and "stray_new.txt" in msg and "victim1.txt" in msg and "victim2.txt" in msg, msg
        assert "victim3.txt" not in msg and "to_delete.txt" not in msg, msg
        assert rep.points_charged == 0
        bad_ev = d7(rep)
        assert bad_ev and "غير متوقعة" in bad_ev[0]["value"], bad_ev

        # ---------- 3) وحدات: الفروق ونطاق الاستثناءات ----------
        v = vmod.Verifier(root)

        def S(**entries):
            return {k.casefold(): (t, n, k) for k, (t, n) in entries.items()}
        steps = [PlanStep(1, "move", "a.txt", {"dest": "z/a.txt"})]
        before = S(**{"a.txt": ("f", 3)})
        # الوجهة المبلغة هي المرجع (اسم فريد)
        ok, _, _ = v.verify_workspace(steps, {1: {"dest": str(root / "z" / "a (1).txt")}}, set(),
                                      before, S(**{"z": ("d", 0), "z/a (1).txt": ("f", 3)}))
        assert ok
        ok, ev1, err = v.verify_workspace(steps, {1: {"dest": str(root / "z" / "a.txt")}}, set(),
                                          before, S(**{"z": ("d", 0), "z/a.txt": ("f", 3), "z/other.txt": ("f", 1)}))
        assert not ok and "other.txt" in err
        # خطوة متخطّاة لا تفسّر شيئاً
        ok, _, err = v.verify_workspace(steps, {}, {1}, before, S(**{"z/a.txt": ("f", 3)}))
        assert not ok
        # run_command: لا يمكن توقعه فلا تُجرى المقارنة (لا فشل)
        rc = [PlanStep(1, "run_command", "whoami", {"cmd": ["whoami"]})]
        ok, ev2, _ = v.verify_workspace(rc, {}, set(), before, S(**{"new.txt": ("f", 1)}))
        assert ok and "لم تُجرَ" in ev2["value"]
        # مجلد كبير: لقطة None => تُتخطى المقارنة بتنبيه لا بفشل
        with mock.patch.object(vmod, "WS_MAX_ENTRIES", 3):
            assert v.workspace_snapshot() is None
        ok, ev3, _ = v.verify_workspace(steps, {}, set(), None, before)
        assert ok and "لم تُجرَ" in ev3["value"]
        # تحويل ملف إلى مجلد (تغيّر النوع) خارج الخطة يُكشف
        ok, _, err = v.verify_workspace([PlanStep(1, "inspect", ".", {})], {}, set(), S(**{"q": ("f", 1)}), S(**{"q": ("d", 0)}))
        assert not ok

    print("PASS: test_b42_d7 completed successfully")
except Exception:
    traceback.print_exc()
    print("FAIL: test_b42_d7")
    sys.exit(1)
