"""沙箱：路径边界 + 命令策略 + 审批门。

原则：安全边界写在**代码结构**里，不写在提示词里。
提示词只是"请求"，guard 才是"约束"。
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .settings import is_project_dotenv


class SandboxViolation(Exception):
    """越过沙箱边界。属于"策略层失败"，会关掉当前轮次而不是让模型自己改。"""


def resolve_within(root: Path, user_path: str) -> Path:
    """把用户给的路径解析到 workspace 内；越界直接抛异常。

    用 resolve() 展开 symlink 和 ..，防止 `../../etc/passwd` 与符号链接逃逸。
    """
    root = Path(root).resolve()
    raw = Path(user_path)
    target = (root / raw).resolve() if not raw.is_absolute() else raw.resolve()
    try:
        target.relative_to(root)
    except ValueError:
        raise SandboxViolation(f"路径越界：{user_path} 不在 workspace {root} 内") from None
    if is_project_dotenv(target):
        raise SandboxViolation("项目 .env 为模型连接配置，工作区文件工具无法访问。")
    return target


@dataclass(frozen=True)
class CommandVerdict:
    allowed: bool
    reason: str = ""

    @property
    def denied(self) -> bool:
        return not self.allowed


# 已知危险模式。这是"负向清单"，不是完备防线——真正的防线是不给它 root 权限。
_END = r"""["']?(?=\s|$|;|&|\|)"""          # 参数结束：空白、行尾或命令连接符
_HOME = r"""(?:~|\$HOME|\$\{HOME\})"""

_DENY_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"""\brm\s+(?:-{1,2}[\w-]*\s+)+["']?/\*?""" + _END), "删除根目录"),
    (re.compile(r"""\brm\s+(?:-{1,2}[\w-]*\s+)+["']?""" + _HOME + r"""/?\*?""" + _END),
     "删除家目录"),
    (re.compile(r"""\bcd\s+["']?(?:/|""" + _HOME + r""")/?["']?\s*(?:&&|;)\s*rm\s+(?:-{1,2}[\w-]*\s+)*-[a-zA-Z]*[rR]"""),
     "切到根目录或家目录后递归删除"),
    (re.compile(_HOME + r"""/\.(?:ssh|aws|gnupg|kube|docker|config/gh)\b"""), "访问凭据目录"),
    (re.compile(r"\bdd\b[^\n]*\bof=/dev/(sd|nvme|disk)"), "写裸设备"),
    (re.compile(r"\bmkfs(\.\w+)?\b"), "格式化文件系统"),
    (re.compile(r":\s*\(\s*\)\s*\{.*\}\s*;\s*:"), "fork bomb"),
    (re.compile(r"\b(shutdown|reboot|halt)\b"), "关机/重启"),
    (re.compile(r"\bsudo\b"), "提权"),
    (re.compile(r"\b(curl|wget)\b[^\n|]*\|\s*(sudo\s+)?(ba)?sh\b"), "管道执行远程脚本"),
    (re.compile(r"\bchmod\s+-R\s+777\s+/(?:\s|$)"), "全局放开权限"),
    (re.compile(r">\s*/dev/(sd|nvme|disk)"), "重定向写裸设备"),
    (re.compile(r"\bgit\s+push\b[^\n]*--force(?!-with-lease)"), "强制推送"),
    (re.compile(r"\bhistory\s+-c\b|\bunset\s+HISTFILE\b"), "清理痕迹"),
]


def check_command(command: str) -> CommandVerdict:
    for pat, why in _DENY_PATTERNS:
        if pat.search(command):
            return CommandVerdict(False, f"命中危险命令模式（{why}）：{pat.pattern}")
    return CommandVerdict(True)


@dataclass
class ApprovalRequest:
    kind: str          # "bash" | "write" | "network" ...
    detail: str
    reason: str = ""
    scope: str | None = None  # None 时仅批准当前调用


# 返回 True = 批准。没有配置审批回调时按"拒绝"处理（fail closed）。
ApprovalFn = Callable[[ApprovalRequest], bool]


class ApprovalGate:
    """需要人工确认的操作走这里。默认 fail closed。

    需审批的工具（写文件、编辑文件、bash）每次都必须调用 ask()：
      · ApprovalGate()            → 一律拒绝（非交互且没加 --yes 时就是它）；
      · ApprovalGate(fn)          → 交给回调决定；
      · ApprovalGate.allow_all()  → 显式全部放行（CLI 的 --yes、离线演示）。
    """

    def __init__(self, fn: ApprovalFn | None = None):
        self._fn = fn
        self._audit: list[tuple[ApprovalRequest, bool]] = []

    @classmethod
    def allow_all(cls) -> "ApprovalGate":
        """显式放行全部需审批操作。只在用户明确同意（如 --yes）时使用。"""
        return cls(lambda req: True)

    @property
    def enabled(self) -> bool:
        """是否配置了审批回调。注意：未配置 ≠ 放行，未配置时 ask() 一律拒绝。"""
        return self._fn is not None

    def ask(self, req: ApprovalRequest) -> bool:
        if self._fn is None:
            self._audit.append((req, False))
            return False
        ok = bool(self._fn(req))
        self._audit.append((req, ok))
        return ok

    @property
    def audit(self) -> list[tuple[ApprovalRequest, bool]]:
        return list(self._audit)


def is_binary(data: bytes) -> bool:
    return b"\x00" in data[:4096]


MAX_READ_BYTES = 2_000_000


def safe_read_text(path: Path, max_bytes: int = MAX_READ_BYTES) -> tuple[str, bool]:
    """读文本文件；超限或二进制返回截断标记。"""
    size = path.stat().st_size
    with open(path, "rb") as fh:
        data = fh.read(max_bytes)
    if is_binary(data):
        return f"(二进制文件，{size} bytes，未读取)", True
    text = data.decode("utf-8", errors="replace")
    return text, size > max_bytes


# bash 子进程能看到的环境变量：白名单，而不是黑名单。
# 黑名单永远列不全（HARNESS_API_KEY、DEEPSEEK_API_KEY、各种 *_TOKEN……），
# 模型一句 `env` 或 `echo $XXX_KEY` 就能把密钥读进上下文。
SHELL_ENV_ALLOW = frozenset({
    "PATH", "HOME", "USER", "LOGNAME", "SHELL", "TERM", "TMPDIR", "TZ",
    "LANG", "LANGUAGE",
})
SHELL_ENV_ALLOW_PREFIXES = ("LC_",)
# 确实需要额外变量时（如 VIRTUAL_ENV），用逗号分隔写进这个环境变量显式放行
EXTRA_ALLOW_VAR = "HARNESS_SHELL_ENV_ALLOW"


def shell_env(root: Path) -> dict[str, str]:
    extra = {k.strip() for k in os.environ.get(EXTRA_ALLOW_VAR, "").split(",") if k.strip()}
    allowed = SHELL_ENV_ALLOW | extra
    env = {k: v for k, v in os.environ.items()
           if k in allowed or k.startswith(SHELL_ENV_ALLOW_PREFIXES)}
    env["PWD"] = str(root)
    env["HARNESS_WORKSPACE"] = str(root)
    return env
