"""وحدة المدقّق المستقل (Verifier) — لقطة قبل التنفيذ ومقارنة بعده على القرص دون الوثوق بكلام المنفّذ."""
import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from assistant.planner import PlanStep
from assistant.safety import MUTATING_ACTIONS
from assistant.tools.files import is_unc_like, check_not_big_cloud_file, CloudFileNotLocalError

_NO_UPGRADE = re.compile(r"no (applicable|available) upgrade|no newer package|no installed package|no applicable update"
                         r"|لا (?:يوجد|توجد|تتوفر|يتوفر)\s+(?:أي\s+)?(?:ترقي|تحديث|إصدار|اصدار)", re.I)
_VERSIONISH = re.compile(r"\d[\w.+\-]*")
VersionProbe = Callable[[str], Tuple[int, str, str]]


class PathEscapeError(Exception):
    """مسار يقع خارج الجذر المسموح."""
    pass


class Verifier:
    """المدقّق المستقل: يحسب بنفسه ما يتوقعه من الخطة، ويقارن بالحالة الفعلية على القرص."""

    def __init__(self, allowed_root: Path, version_probe: Optional[VersionProbe] = None, kill_switch=None):
        self.allowed_root = Path(allowed_root).resolve()
        self.trash_dir = self.allowed_root / ".trash"
        self.kill_switch = kill_switch  # يُفحص داخل حلقات البصمة الطويلة
        # version_probe(package_id) -> (code, stdout, stderr)؛ بدونه لا يُقبل أي تدقيق لـ winget (فشل مغلق).
        self.version_probe = version_probe

    # ---------- أدوات مساعدة ----------
    def calculate_hash(self, file_path: Path) -> str:
        check_not_big_cloud_file(Path(file_path))  # لا نُنزّل ملفاً سحابياً كبيراً بصمت لمجرد حساب بصمته
        h = hashlib.sha256()
        with open(file_path, "rb") as f:
            while chunk := f.read(65536):
                if self.kill_switch is not None:
                    self.kill_switch.check()
                h.update(chunk)
        return h.hexdigest()

    def confine(self, path: Any) -> Path:
        """حلّ المسار والتأكد من وجوده داخل الجذر (R17)."""
        if not isinstance(path, (str, Path)) or str(path).strip() == "":
            raise PathEscapeError("مسار فارغ أو غير صالح.")
        if is_unc_like(path):
            raise PathEscapeError(f"المسارات الشبكية/الأجهزة غير مسموحة: '{path}'.")
        p = Path(path)
        if not p.is_absolute():
            p = self.allowed_root / p
        resolved = p.resolve()
        try:
            resolved.relative_to(self.allowed_root)
        except ValueError:
            raise PathEscapeError(f"المسار '{path}' خارج الجذر المسموح.")
        return resolved

    def _file_sig(self, f: Path) -> str:
        """بصمة المحتوى؛ لملف سحابي كبير غير محمّل نستعمل بصمة وصفية (حجم + وقت تعديل) بدل تنزيله بصمت."""
        try:
            return self.calculate_hash(f)
        except CloudFileNotLocalError:
            st = os.lstat(str(f))
            return f"CLOUD:{st.st_size}:{st.st_mtime_ns}"

    def digest(self, path: Path) -> Optional[str]:
        """بصمة SHA-256 لملف، أو بصمة شجرة لمجلد (أسماء + محتوى)."""
        if not path.exists():
            return None
        if path.is_file():
            return "F:" + self._file_sig(path)
        h = hashlib.sha256()
        for current, dirs, files in os.walk(str(path), followlinks=False):
            dirs.sort()
            cur = Path(current)
            rel = cur.relative_to(path).as_posix()
            h.update(("D|" + rel + "\n").encode("utf-8", "surrogatepass"))
            for name in sorted(files):
                f = cur / name
                if f.is_symlink():
                    h.update(("L|" + (rel + "/" + name) + "|" + os.readlink(str(f)) + "\n").encode("utf-8", "surrogatepass"))
                else:
                    h.update(("F|" + (rel + "/" + name) + "|" + self._file_sig(f) + "\n").encode("utf-8", "surrogatepass"))
        return "T:" + h.hexdigest()

    @staticmethod
    def _is_unique_variant(expected: Path, actual: Path) -> bool:
        """الاسم الفعلي يساوي المتوقع أو صيغته الفريدة 'name (n).ext'."""
        if actual == expected:
            return True
        return (
            actual.parent == expected.parent
            and actual.suffix.casefold() == expected.suffix.casefold()
            and re.fullmatch(re.escape(expected.stem.casefold()) + r" \(\d+\)", actual.stem.casefold()) is not None
        )

    # ---------- اللقطة قبل التنفيذ (R18) ----------
    def snapshot(self, step: PlanStep) -> Dict[str, Any]:
        snap: Dict[str, Any] = {"step_id": step.step_id, "action": step.action_type.strip().lower()}
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
        elif action == "winget_upgrade" and self.version_probe is not None:
            try:  # سطر الحزمة قبل الترقية ليُقارَن بعدها في التقرير (الإصدار قبل/بعد)
                snap["probe_line"] = self._package_line(self.version_probe(step.target)[1], step.target)
            except Exception:
                snap["probe_line"] = None
        return snap

    MAX_LISTED = 40

    def _listing(self, p: Path) -> str:
        """قائمة عناصر المجلد (مقتطعة بحد معقول) ليعرض تقرير الفحص ما بداخله لا «مجلد موجود» فقط. قراءة أسماء فقط."""
        try:
            names = []
            for e in sorted(os.scandir(str(p)), key=lambda x: (not x.is_dir(follow_symlinks=False), x.name.lower())):
                if p == self.allowed_root and (e.name == ".trash" or e.name.startswith(".undo_journal")):
                    continue
                names.append(e.name + ("/" if e.is_dir(follow_symlinks=False) else ""))
        except OSError:
            return ""
        if not names:
            return " (فارغ)"
        shown = "، ".join(names[: self.MAX_LISTED])
        more = f" … و{len(names) - self.MAX_LISTED} عنصراً آخر" if len(names) > self.MAX_LISTED else ""
        return f" — يحوي {len(names)} عنصراً: {shown}{more}"

    @staticmethod
    def _package_line(out: str, pkg: str) -> str:
        """سطر الحزمة بمطابقة معرّفها كاملاً (عمود Id) لا كجزء من معرّف آخر: Git.GitLFS ليس Git.Git."""
        want = (pkg or "").strip().lower()
        for ln in (out or "").splitlines():
            if want and want in (tok.lower() for tok in ln.split()):
                return " ".join(ln.split())[:160]
        return ""

    @staticmethod
    def _still_upgradable(line: str, pkg: str) -> bool:
        """هل يظهر في سطر الحزمة عمود «Available» بإصدار مختلف عن المثبّت؟ (مستقل عن لغة رسائل winget)"""
        toks = line.split()
        low = [t.lower() for t in toks]
        want = (pkg or "").strip().lower()
        if want not in low:
            return False
        vers = [t for t in toks[low.index(want) + 1:] if _VERSIONISH.fullmatch(t)]
        return len(vers) >= 2 and vers[0] != vers[1]

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
                if p.is_dir():
                    ev["value"] = "مجلد موجود" + self._listing(p)
                else:
                    ev["value"] = f"ملف بحجم {p.stat().st_size} بايت موجود"
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
                after_line = self._package_line(out, step.target)
                if code != 0 or not after_line:
                    return fail(f"الفحص المستقل لم يجد الحزمة '{step.target}' (رمز {code}).")
                ev["type"] = "رمز الخروج + فحص مستقل"
                before_line = (before or {}).get("probe_line")
                if before_line and after_line and before_line == after_line:
                    if _NO_UPGRADE.search(str(info.get("stdout", "")) + " " + str(info.get("stderr", ""))):
                        return fail(f"لا يوجد تحديث متاح: الحزمة محدّثة أصلاً (لم يتغير سطرها): {after_line}")
                    if self._still_upgradable(after_line, step.target):  # الدليل من القرص نفسه: سطرها لم يتغير وإصدار أحدث ما زال متاحاً
                        return fail(f"لم تتم الترقية: سطر الحزمة لم يتغير وإصدار أحدث ما زال متاحاً: {after_line}")
                    ev["value"] = f"0 والحزمة ظاهرة، لكن سطرها لم يتغير: {after_line}"
                elif before_line and after_line:
                    ev["value"] = f"0 والحزمة ظاهرة. قبل: {before_line} ← بعد: {after_line}"
                else:
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
        candidates = [expected]
        if action != "rename" and expected.is_dir() and expected != actual:
            candidates.append(expected / src.name)  # الوجهة مجلد موجود: العنصر يوضع بداخله
        if not any(self._is_unique_variant(c, actual) for c in candidates):
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
            want = str(expected_hash).strip().lower()
            if want.startswith("sha256:"):
                want = want[len("sha256:"):].strip()
            if not actual.is_file() or self.calculate_hash(actual).lower() != want:
                got = self.calculate_hash(actual) if actual.is_file() else "DIR"
                return fail(f"عدم تطابق البصمة للملف '{actual.name}': متوقع {want}، الفعلي {got}.")

        ev["type"] = "ملف موجود ومطابق"
        ev["value"] = f"{after_digest[:18]}… (مطابق للأصل قبل التنفيذ)"
        if not any(actual == c for c in candidates):  # هبط على اسم فريد «name (1).ext» لأن المخطط موجود أصلاً
            ev["value"] += f" — destination renamed to {actual.name} (الوجهة أُعيدت تسميتها إلى «{actual.name}» لوجود اسم مطابق)"
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
        data = None
        for attempt in range(4):  # قفل لحظي من مضاد فيروسات/OneDrive لا يعني أن السجل مفقود
            try:
                data = json.loads(jp.read_text(encoding="utf-8"))
                break
            except Exception:
                if attempt < 3:
                    time.sleep(0.25)
        if data is None:
            ev["value"] = "مفقود أو تالف"
            return False, ev, "سجل التراجع .undo_journal.json مفقود أو تالف على القرص!"
        if not isinstance(data, list) or len(data) - journal_start < need:
            ev["value"] = "ناقص"
            return False, ev, f"سجل التراجع لا يحوي عمليات هذه المهمة كاملة (المطلوب {need})."
        missing = self._unjournaled_steps(plan_steps, befores, data[journal_start:])
        if missing:
            ev["value"] = "ناقص"
            return False, ev, ("سجل التراجع لا يحوي عملية الخطوات التالية فلن يمكن التراجع عنها: "
                               + "، ".join(f"{a} «{t}»" for a, t in missing[:5]) + ".")
        ev["value"] = f"صالح ويحوي {len(data) - journal_start} عملية لهذه المهمة"
        return True, ev, None

    # ---------- مطابقة سجل التراجع لكل خطوة ----------
    _JOURNAL_ACTION = {"move": "move", "copy": "copy", "rename": "rename", "delete": "delete", "trash": "delete", "create_dir": "create_dir"}
    _JOURNAL_SRC_KEY = {"move": "src", "copy": "src", "rename": "src", "delete": "original_path", "create_dir": "path"}

    def _jnorm(self, value: Any) -> Optional[str]:
        """مسار من سجل التراجع (rel:… أو مطلق داخل الجذر) أو هدف خطوة ← صيغة مقارنة موحّدة نسبية صغيرة الحروف، أو None."""
        if not isinstance(value, (str, Path)):
            return None
        v = str(value)
        if v.startswith("rel:"):
            v = v[4:]
        try:
            p = Path(v)
            rel = p.resolve().relative_to(self.allowed_root) if p.is_absolute() else Path(os.path.normpath(v))
        except (ValueError, OSError):
            return None
        return rel.as_posix().casefold()

    def _unjournaled_steps(self, plan_steps, befores, entries) -> List[Tuple[str, str]]:
        """الخطوات المغيِّرة التي لا يقابلها إدخال في السجل (بحسب الإجراء والمسار)، كل إدخال يخدم خطوة واحدة.
        الخطوة بلا target معروف تُقارَن بالإجراء وحده. (create_dir ينشئ أحياناً مجلدات أصل إضافية في السجل: لا بأس.)"""
        used = set()
        missing: List[Tuple[str, str]] = []
        for s in plan_steps:
            a = s.action_type.strip().lower()
            if a not in MUTATING_ACTIONS:
                continue
            if a == "create_dir" and befores.get(s.step_id, {}).get("exists"):
                continue
            want_action = self._JOURNAL_ACTION.get(a)
            target = getattr(s, "target", None)
            want_path = self._jnorm(target) if target is not None else None
            key = self._JOURNAL_SRC_KEY.get(want_action)
            hit = None
            for i, e in enumerate(entries):
                if i in used or not isinstance(e, dict) or e.get("action") != want_action:
                    continue
                if want_path is not None and key and self._jnorm(e.get(key)) != want_path:
                    continue
                hit = i
                break
            if hit is None:
                missing.append((a, str(target) if target is not None else "?"))
            else:
                used.add(hit)
        return missing
