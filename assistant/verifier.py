"""وحدة المدقّق المستقل (Verifier) — لقطة قبل التنفيذ ومقارنة بعده على القرص دون الوثوق بكلام المنفّذ."""
import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from assistant.planner import PlanStep
from assistant.safety import MUTATING_ACTIONS

VersionProbe = Callable[[str], Tuple[int, str, str]]


class PathEscapeError(Exception):
    """مسار يقع خارج الجذر المسموح."""
    pass


class Verifier:
    """المدقّق المستقل: يحسب بنفسه ما يتوقعه من الخطة، ويقارن بالحالة الفعلية على القرص."""

    def __init__(self, allowed_root: Path, version_probe: Optional[VersionProbe] = None):
        self.allowed_root = Path(allowed_root).resolve()
        self.trash_dir = self.allowed_root / ".trash"
        # version_probe(package_id) -> (code, stdout, stderr)؛ بدونه لا يُقبل أي تدقيق لـ winget (فشل مغلق).
        self.version_probe = version_probe

    # ---------- أدوات مساعدة ----------
    def calculate_hash(self, file_path: Path) -> str:
        h = hashlib.sha256()
        with open(file_path, "rb") as f:
            while chunk := f.read(65536):
                h.update(chunk)
        return h.hexdigest()

    def confine(self, path: Any) -> Path:
        """حلّ المسار والتأكد من وجوده داخل الجذر (R17)."""
        if not isinstance(path, (str, Path)) or str(path).strip() == "":
            raise PathEscapeError("مسار فارغ أو غير صالح.")
        p = Path(path)
        if not p.is_absolute():
            p = self.allowed_root / p
        resolved = p.resolve()
        try:
            resolved.relative_to(self.allowed_root)
        except ValueError:
            raise PathEscapeError(f"المسار '{path}' خارج الجذر المسموح.")
        return resolved

    def digest(self, path: Path) -> Optional[str]:
        """بصمة SHA-256 لملف، أو بصمة شجرة لمجلد (أسماء + محتوى)."""
        if not path.exists():
            return None
        if path.is_file():
            return "F:" + self.calculate_hash(path)
        h = hashlib.sha256()
        for current, dirs, files in os.walk(str(path), followlinks=False):
            dirs.sort()
            cur = Path(current)
            rel = cur.relative_to(path).as_posix()
            h.update(("D|" + rel + "\n").encode("utf-8"))
            for name in sorted(files):
                f = cur / name
                if f.is_symlink():
                    h.update(("L|" + (rel + "/" + name) + "|" + os.readlink(str(f)) + "\n").encode("utf-8"))
                else:
                    h.update(("F|" + (rel + "/" + name) + "|" + self.calculate_hash(f) + "\n").encode("utf-8"))
        return "T:" + h.hexdigest()

    @staticmethod
    def _is_unique_variant(expected: Path, actual: Path) -> bool:
        """الاسم الفعلي يساوي المتوقع أو صيغته الفريدة 'name (n).ext'."""
        if actual == expected:
            return True
        return (
            actual.parent == expected.parent
            and actual.suffix == expected.suffix
            and re.fullmatch(re.escape(expected.stem) + r" \(\d+\)", actual.stem) is not None
        )

    # ---------- اللقطة قبل التنفيذ (R18) ----------
    def snapshot(self, step: PlanStep) -> Dict[str, Any]:
        snap: Dict[str, Any] = {"step_id": step.step_id, "action": step.action_type.lower()}
        action = snap["action"]
        if action in ("move", "copy", "rename", "delete", "trash", "create_dir", "inspect"):
            try:
                tp = self.confine(step.target if step.target not in (".", "./") else self.allowed_root)
            except PathEscapeError as exc:
                snap["invalid"] = str(exc)
                return snap
            snap["exists"] = tp.exists()
            snap["kind"] = "dir" if tp.is_dir() else ("file" if tp.is_file() else None)
            if action in ("move", "copy", "rename", "delete", "trash"):
                snap["digest"] = self.digest(tp)
        return snap

    # ---------- التدقيق بعد التنفيذ ----------
    def verify_step(
        self, step: PlanStep, info: Optional[Dict[str, Any]], before: Optional[Dict[str, Any]]
    ) -> Tuple[bool, Dict[str, Any], Optional[str]]:
        action = step.action_type.strip().lower()
        ev: Dict[str, Any] = {"item": step.target, "action": action}

        def fail(msg: str):
            ev["type"] = "فشل التدقيق"
            ev["value"] = msg
            return False, ev, msg

        # R17: لا دليل تنفيذ = فشل
        if not info or info.get("step_id") != step.step_id:
            return fail(f"لا يوجد دليل تنفيذ للخطوة {step.step_id}؛ لا يُعتبر منفذاً.")
        if not info.get("success"):
            return fail(f"المنفّذ أبلغ عن عدم نجاح الخطوة {step.step_id}.")
        if before is None or before.get("invalid"):
            return fail(f"لا توجد لقطة صالحة قبل التنفيذ للخطوة {step.step_id}: {(before or {}).get('invalid', 'مفقودة')}")

        try:
            if action == "inspect":
                p = self.confine(step.target if step.target not in (".", "./") else self.allowed_root)
                if not p.exists():
                    return fail(f"العنصر '{step.target}' غير موجود فعلياً.")
                ev["type"] = "فحص فعلي"
                ev["value"] = ("مجلد" if p.is_dir() else f"ملف بحجم {p.stat().st_size} بايت") + " موجود"
                return True, ev, None

            if action == "create_dir":
                p = self.confine(step.target)
                if not p.is_dir():
                    return fail(f"المجلد المطلوب '{step.target}' غير موجود فعلياً على القرص.")
                ev["type"] = "مجلد موجود"
                ev["value"] = str(p)
                return True, ev, None

            if action in ("move", "copy", "rename"):
                return self._verify_transfer(step, action, info, before, ev, fail)

            if action in ("delete", "trash"):
                src = self.confine(step.target)
                if not before.get("exists"):
                    return fail(f"العنصر '{step.target}' لم يكن موجوداً قبل الحذف.")
                if src.exists():
                    return fail(f"'{step.target}' ما زال موجوداً في مساره الأصلي ولم يُنقل للسلة.")
                tp = self.confine(info.get("trash_path"))
                if self.trash_dir not in tp.parents:
                    return fail("مسار السلة المبلغ عنه ليس داخل .trash.")
                if not tp.exists():
                    return fail(f"العنصر المحذوف غير موجود في السلة '{tp}'.")
                if not self._is_unique_variant(self.trash_dir / Path(step.target).name, tp):
                    return fail("اسم العنصر في السلة لا يطابق اسم الأصل.")
                if self.digest(tp) != before.get("digest"):
                    return fail("محتوى العنصر في السلة لا يطابق محتواه قبل الحذف.")
                ev["type"] = "نقل آمن للسلة"
                ev["value"] = f"منقول إلى: {tp} (المحتوى مطابق للأصل)"
                return True, ev, None

            if action == "winget_upgrade":
                if info.get("exit_code") != 0:
                    return fail(f"فشل الأمر: رمز الخروج {info.get('exit_code')} بدلاً من 0.")
                if self.version_probe is None:
                    return fail("لا توجد أداة تحقق مستقلة لـ winget؛ لا يمكن تأكيد النجاح.")
                code, out, _err = self.version_probe(step.target)
                if code != 0 or step.target.lower() not in (out or "").lower():
                    return fail(f"الفحص المستقل لم يجد الحزمة '{step.target}' (رمز {code}).")
                ev["type"] = "رمز الخروج + فحص مستقل"
                ev["value"] = f"0 والحزمة ظاهرة في list: {step.target}"
                return True, ev, None

            if action == "run_command":
                if info.get("exit_code") != 0:
                    return fail(f"فشل الأمر: رمز الخروج {info.get('exit_code')} بدلاً من 0.")
                expected = step.params.get("expected_output")
                out = str(info.get("stdout", ""))
                if isinstance(expected, str) and expected and expected not in out:
                    return fail(f"مخرجات الأمر لا تحتوي النص المتوقع '{expected}'.")
                ev["type"] = "رمز الخروج"
                ev["value"] = f"نجاح (0) — {out.strip()[:60]}"
                return True, ev, None
        except PathEscapeError as exc:
            return fail(str(exc))

        return fail(f"لا يوجد تدقيق معرّف للإجراء '{action}'.")

    def _verify_transfer(self, step, action, info, before, ev, fail):
        if not before.get("exists"):
            return fail(f"المصدر '{step.target}' لم يكن موجوداً قبل التنفيذ.")
        src = self.confine(step.target)
        actual = self.confine(info.get("dest"))

        if action == "rename":
            expected = src.parent / str(step.params.get("new_name", ""))
        else:
            expected = self.confine(step.params.get("dest"))
        if not self._is_unique_variant(expected, actual):
            return fail(f"الوجهة الفعلية '{actual.name}' لا تطابق الوجهة المخططة '{expected.name}'.")
        if not actual.exists():
            return fail(f"الوجهة '{actual}' غير موجودة فعلياً على القرص.")

        after_digest = self.digest(actual)
        if after_digest != before.get("digest"):
            return fail(f"محتوى الوجهة '{actual.name}' لا يطابق محتوى المصدر قبل التنفيذ.")

        if action in ("move", "rename"):
            if src.exists() and src != actual:
                return fail(f"المصدر '{step.target}' ما زال موجوداً بعد {'النقل' if action == 'move' else 'إعادة التسمية'}.")
        else:  # copy
            if not src.exists() or self.digest(src) != before.get("digest"):
                return fail(f"المصدر '{step.target}' تغيّر أو اختفى أثناء النسخ.")

        expected_hash = step.params.get("expected_hash")
        if expected_hash:
            if not actual.is_file() or self.calculate_hash(actual) != expected_hash:
                got = self.calculate_hash(actual) if actual.is_file() else "DIR"
                return fail(f"عدم تطابق البصمة للملف '{actual.name}': متوقع {expected_hash}، الفعلي {got}.")

        ev["type"] = "ملف موجود ومطابق"
        ev["value"] = f"{after_digest[:18]}… (مطابق للأصل قبل التنفيذ)"
        return True, ev, None

    def verify_journal(
        self,
        plan_steps: List[PlanStep],
        befores: Optional[Dict[int, Dict[str, Any]]] = None,
        journal_start: int = 0,
    ) -> Tuple[bool, Dict[str, Any], Optional[str]]:
        """سجل التراجع مطلوب فقط إن غيّرت الخطة شيئاً فعلاً، ويجب أن يكون صالحاً ويحوي عمليات هذه المهمة."""
        befores = befores or {}
        need = 0
        for s in plan_steps:
            a = s.action_type.strip().lower()
            if a not in MUTATING_ACTIONS:
                continue
            if a == "create_dir" and befores.get(s.step_id, {}).get("exists"):
                continue  # مجلد موجود أصلاً: لا تغيير ولا إدخال في السجل
            need += 1
        ev = {"item": ".undo_journal.json", "type": "سلامة سجل التراجع"}
        if need == 0:
            ev["value"] = "غير مطلوب (خطة قراءة فقط)"
            return True, ev, None
        jp = self.allowed_root / ".undo_journal.json"
        try:
            data = json.loads(jp.read_text(encoding="utf-8"))
        except Exception:
            ev["value"] = "مفقود أو تالف"
            return False, ev, "سجل التراجع .undo_journal.json مفقود أو تالف على القرص!"
        if not isinstance(data, list) or len(data) - journal_start < need:
            ev["value"] = "ناقص"
            return False, ev, f"سجل التراجع لا يحوي عمليات هذه المهمة كاملة (المطلوب {need})."
        ev["value"] = f"صالح ويحوي {len(data) - journal_start} عملية لهذه المهمة"
        return True, ev, None
