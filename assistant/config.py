"""إعدادات التطبيق المحلية (مجلد العمل) مع تحقق صارم يمنع اختيار مجلدات النظام أو الحساسة."""
import json
import os
from pathlib import Path
from typing import Optional


class WorkspaceError(Exception):
    """مجلد عمل غير مسموح أو غير صالح (رسالة عربية)."""


def data_dir() -> Path:
    return Path(os.environ.get("COS_DATA_DIR") or (Path.home() / ".cos_assistant"))


def default_workspace() -> Path:
    return Path(os.environ.get("COS_WORKSPACE") or (Path.home() / "COS_Workspace"))


_POSIX_BLOCKED = {"/", "/etc", "/usr", "/bin", "/sbin", "/lib", "/lib64", "/var", "/boot", "/dev", "/proc", "/sys", "/root", "/home", "/opt"}
_SENSITIVE_PARTS = {".ssh", ".gnupg", ".aws", ".git", "$recycle.bin", "system volume information"}


def _blocked_roots():
    roots = []
    for var in ("WINDIR", "SystemRoot", "ProgramFiles", "ProgramFiles(x86)", "ProgramW6432", "ProgramData"):
        v = os.environ.get(var)
        if v:
            roots.append(Path(v))
    return roots


def validate_workspace(path) -> Path:
    """يعيد المسار المحلول أو يرفع WorkspaceError. لا يُقبل: جذر قرص، مجلد النظام/البرامج، جذر المستخدم، مجلد بيانات التطبيق، أو مجلد حساس."""
    if path is None or str(path).strip() == "":
        raise WorkspaceError("لم يُحدَّد مجلد.")
    try:
        from assistant.tools.files import is_unc_like
        if is_unc_like(path):
            raise WorkspaceError("المسارات الشبكية غير مسموحة.")
        p = Path(str(path).strip()).expanduser().resolve()
    except (OSError, RuntimeError, ValueError):
        raise WorkspaceError("مسار غير صالح.")
    if not p.exists() or not p.is_dir():
        raise WorkspaceError("المجلد غير موجود.")
    if p.parent == p:
        raise WorkspaceError("لا يمكن اختيار جذر القرص. اختر مجلداً فرعياً مخصصاً للعمل.")
    if str(p) in _POSIX_BLOCKED:
        raise WorkspaceError("هذا مجلد نظام ولا يُسمح باختياره.")
    for sysroot in _POSIX_BLOCKED - {"/", "/home", "/opt"}:
        if Path(sysroot) in p.parents:
            raise WorkspaceError("هذا داخل مجلد نظام ولا يُسمح باختياره.")
    for b in _blocked_roots():
        try:
            rb = b.resolve()
        except OSError:
            continue
        if p == rb or rb in p.parents:
            raise WorkspaceError("لا يُسمح بمجلدات النظام أو البرامج.")
    home = Path.home().resolve()
    if p == home:
        raise WorkspaceError("لا يمكن اختيار مجلد المستخدم الرئيسي نفسه. اختر مجلداً داخله (مثل Downloads).")
    if p in home.parents:
        raise WorkspaceError("هذا المجلد يحوي مجلد المستخدم الرئيسي؛ اختر مجلداً فرعياً مخصصاً للعمل.")
    dd = data_dir().resolve()
    if p == dd or dd in p.parents or p in dd.parents:
        raise WorkspaceError("هذا المجلد يخص بيانات التطبيق نفسه.")
    appdata = Path.home().resolve() / "AppData"
    temp = appdata / "Local" / "Temp"
    if (p == appdata or appdata in p.parents) and not (p == temp or temp in p.parents):
        raise WorkspaceError("مجلد بيانات البرامج (AppData) غير مسموح.")
    if any(part.lower() in _SENSITIVE_PARTS for part in p.parts):
        raise WorkspaceError("المجلد حساس (مفاتيح/بيانات برامج/مستودع git) ولا يُسمح باختياره.")
    from assistant.tools.files import _is_link
    for name, want_dir in ((".trash", True), (".undo_journal.json", False), (".undo_journal.json.tmp", False)):
        q = p / name
        if os.path.lexists(str(q)):
            if _is_link(q):
                raise WorkspaceError(f"في المجلد رابط باسم {name} — احذفه يدوياً أولاً (غير آمن).")
            if q.is_dir() != want_dir:
                raise WorkspaceError(f"في المجلد عنصر باسم {name} من نوع غير متوقع — غيّر اسمه يدوياً أولاً.")
    return p


def _config_path() -> Path:
    return data_dir() / "config.json"


def load_config() -> dict:
    try:
        data = json.loads(_config_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_config(cfg: dict) -> None:
    d = data_dir()
    d.mkdir(parents=True, exist_ok=True)
    _config_path().write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


def current_workspace() -> Path:
    """المجلد المحفوظ إن كان ما زال صالحاً، وإلا الافتراضي (يُنشأ عند الحاجة)."""
    saved = load_config().get("workspace")
    if saved:
        try:
            return validate_workspace(saved)
        except WorkspaceError:
            pass
    for d in (default_workspace(), Path.home() / "COS_Workspace"):
        try:
            d.mkdir(parents=True, exist_ok=True)
            return validate_workspace(d)  # الافتراضي يخضع لنفس التحقق (قد يأتي من متغير بيئة)
        except (WorkspaceError, OSError):
            continue
    raise WorkspaceError("تعذّر تجهيز مجلد عمل آمن. اختر مجلداً من الواجهة.")
