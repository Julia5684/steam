"""Apply the supervisor selection to the frontmatter Claude reads at invocation."""

import argparse
import json
import os
from pathlib import Path
import re
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]


def desired_text(root, config):
    try:
        supervisor = config["supervisor"]
        agent = config["agents"][supervisor["agent"]]
        model, level = supervisor["model"], supervisor["level"]
        valid = (
            agent.get("enabled", True)
            and agent["adapter"] == "claude"
            and isinstance(model, str)
            and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", model)
            and level in agent["models"][model]
            and level in {"low", "medium", "high", "xhigh", "max"}
        )
    except (KeyError, TypeError) as exc:
        raise ValueError("请检查 config.json 的 supervisor 和 agents 配置。") from exc
    if not valid:
        raise ValueError("主管需要已启用的 Claude 执行器、已列出的模型和可用等级。")

    src = (root / "SKILL.md").read_text(encoding="utf-8")
    sections = src.split("---\n", 2)
    if len(sections) != 3 or sections[0] != "":
        raise ValueError("SKILL.md 缺少 Claude 技能所需的文件头。")
    header = sections[1]
    for key, value in (("model", model), ("effort", level)):
        header, matches = re.subn(rf"^{key}: [^\n]+$", f"{key}: {value}", header, flags=re.M)
        if matches != 1:
            raise ValueError(f"SKILL.md 文件头内需要一个 {key} 字段。")
    return src, "---\n" + header + "---\n" + sections[2]


def check_synced(skill_root: Path, config: dict) -> None:
    before, after = desired_text(skill_root, config)
    if before != after:
        raise ValueError("主管设置有更新。请在 steam 目录运行 python3 scripts/sync_supervisor.py"
                         "（Windows 可用 py -3 scripts/sync_supervisor.py），然后重新输入 /steam。")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="只核对文件头")
    args = parser.parse_args()
    config = json.loads((ROOT / "config.json").read_text(encoding="utf-8-sig"))
    if args.check:
        check_synced(ROOT, config)
        print("OK")
        return
    before, after = desired_text(ROOT, config)
    if before == after:
        print("主管配置已同步。")
        return
    path = ROOT / "SKILL.md"
    staging = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n",
                                         dir=ROOT, delete=False) as temp:
            staging = Path(temp.name)
            temp.write(after)
        staging.chmod(path.stat().st_mode & 0o777)
        os.replace(staging, path)
    finally:
        if staging:
            staging.unlink(missing_ok=True)
    print("主管配置已同步。下一次输入 /steam 时生效。")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError) as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
