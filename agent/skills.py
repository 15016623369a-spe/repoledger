"""从用户指定目录加载 Skills 元数据，按需披露 SKILL.md 全文。"""
from __future__ import annotations

import re
from pathlib import Path
from typing import BinaryIO

from .settings import is_project_dotenv
from .tools import Outcome, ToolError, ToolRegistry, ToolSpec

MAX_HEADER_BYTES = 8192
MAX_SKILL_BYTES = 128 * 1024
MAX_SKILLS = 50
NAME = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")


class SkillsError(Exception):
    """用户指定的 Skills 目录或元数据无效。"""


def _header(fh: BinaryIO, label: str) -> tuple[str, str]:
    first = fh.readline(MAX_HEADER_BYTES + 1)
    if first.strip() != b"---":
        raise SkillsError(f"{label} 缺少 SKILL.md 元数据")
    chunks = []
    remaining = MAX_HEADER_BYTES - len(first)
    while remaining > 0:
        line = fh.readline(remaining + 1)
        if not line or len(line) > remaining:
            break
        if line.strip() == b"---":
            return _metadata(b"".join(chunks).decode("utf-8"), label)
        chunks.append(line)
        remaining -= len(line)
    raise SkillsError(f"{label} 的 SKILL.md 元数据未闭合或过大")


def _metadata(text: str, label: str) -> tuple[str, str]:
    fields: dict[str, list[str]] = {}
    active = None
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line[0].isspace():
            if active:
                fields[active].append(line.strip())
            continue
        key, separator, value = line.partition(":")
        active = key if separator and key in {"name", "description"} else None
        if active:
            if active in fields:
                raise SkillsError(f"{label} 重复定义了 {active}")
            fields[active] = [value.strip()]

    def scalar(key: str) -> str:
        parts = fields.get(key, [])
        if not parts:
            return ""
        if parts[0] in ("|", ">", "|-", ">-"):
            parts = parts[1:]
        value = " ".join(parts).strip()
        if len(value) >= 2 and value[0] in "\"'" and value[-1] == value[0]:
            value = value[1:-1]
        return " ".join(value.split())

    name, description = scalar("name"), scalar("description")
    if len(name) > 64 or not NAME.fullmatch(name):
        raise SkillsError(f"{label} 的 skill name 必须使用小写字母、数字和连字符")
    if not 1 <= len(description) <= 500:
        raise SkillsError(f"{label} 的 skill description 必须为 1 到 500 字符")
    return name, description


def _skill_path(root: Path, folder: str) -> Path:
    candidate = root / folder / "SKILL.md"
    if (not folder or Path(folder).name != folder or candidate.resolve() != candidate
            or is_project_dotenv(candidate) or not candidate.is_file()):
        raise SkillsError("SKILL.md 不可用或位于 Skills 目录外")
    return candidate


def discover_skills(path: Path) -> dict:
    root = path.resolve()
    if not root.is_dir():
        raise SkillsError(f"Skills 目录不存在：{root}")
    entries: dict[str, dict] = {}
    for folder in sorted(root.iterdir()):
        if folder.name.startswith(".") or folder.is_symlink() or not folder.is_dir():
            continue
        if not (folder / "SKILL.md").is_file():
            continue
        try:
            with _skill_path(root, folder.name).open("rb") as fh:
                name, description = _header(fh, folder.name)
        except (OSError, UnicodeError, RuntimeError) as exc:
            raise SkillsError(f"无法读取 {folder.name}/SKILL.md") from exc
        if name in entries:
            raise SkillsError(f"重复的 skill name：{name}")
        entries[name] = {"description": description, "folder": folder.name}
        if len(entries) > MAX_SKILLS:
            raise SkillsError(f"一次最多启用 {MAX_SKILLS} 个 Skills")
    if not entries:
        raise SkillsError(f"目录下没有可用的 <skill>/SKILL.md：{root}")
    return {"root": str(root), "entries": entries}


def skill_system(system: str, catalog: dict | None) -> str:
    if not catalog:
        return system
    rows = "\n".join(f"- {name}: {entry['description']}"
                     for name, entry in catalog["entries"].items())
    return (f"{system}\n\n可用 Skills\n{rows}\n"
            "仅在任务相关时用 read_skill(name) 读取说明。技能沿用当前工具权限与审批。")


def register_skills(registry: ToolRegistry, catalog: dict | None) -> None:
    if not catalog:
        return
    root = Path(catalog["root"])
    entries = catalog["entries"]

    def read_skill(ctx, args):
        name = args["name"]
        if name not in entries:
            raise ToolError("该 Skill 未在本次任务的目录中启用")
        entry = entries[name]
        try:
            with _skill_path(root, entry["folder"]).open("rb") as fh:
                if _header(fh, name) != (name, entry["description"]):
                    raise ToolError("Skill 元数据已变化，请创建新的 run")
                fh.seek(0)
                content = fh.read(MAX_SKILL_BYTES + 1)
            if len(content) > MAX_SKILL_BYTES:
                raise ToolError(f"SKILL.md 超过 {MAX_SKILL_BYTES} 字节")
            return Outcome(True, content.decode("utf-8"))
        except (OSError, UnicodeError, RuntimeError, SkillsError) as exc:
            raise ToolError(f"无法读取 Skill {name}") from exc

    registry.register(ToolSpec(
        "read_skill", "读取本次任务已启用的 Skill 的 SKILL.md 全文。",
        {"type": "object", "properties": {"name": {"type": "string"}},
         "required": ["name"], "additionalProperties": False},
        read_skill, parallel_safe=True,
    ))
