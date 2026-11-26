"""从项目根目录读模型配置。文件内容只作为数据解析。"""
from __future__ import annotations

import shlex
from pathlib import Path

DOTENV_PATH = Path(__file__).resolve().parents[1] / ".env"
PROVIDER_KEYS = frozenset({"HARNESS_BASE_URL", "HARNESS_API_KEY", "HARNESS_MODEL"})


class ProviderConfigError(Exception):
    """项目中的模型配置不可读或格式无效。"""


def dotenv_values() -> dict[str, str]:
    try:
        lines = DOTENV_PATH.read_text(encoding="utf-8-sig").splitlines()
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeError) as exc:
        raise ProviderConfigError("无法读取项目 .env，请检查文件权限与 UTF-8 编码。") from exc

    values = {}
    for number, line in enumerate(lines, 1):
        name, separator, value = line.partition("=")
        name = name.strip().removeprefix("export ").strip()
        if not separator or name not in PROVIDER_KEYS:
            continue
        try:
            tokens = shlex.split(value, comments=True)
        except ValueError:
            tokens = None
        if tokens is None or len(tokens) > 1:
            raise ProviderConfigError(f"项目 .env 第 {number} 行的 {name} 格式无效。")
        values[name] = tokens[0] if tokens else ""
    return values


def is_project_dotenv(resolved_path: Path) -> bool:
    if resolved_path == DOTENV_PATH.resolve():
        return True
    try:
        return resolved_path.samefile(DOTENV_PATH)
    except OSError:
        return False
