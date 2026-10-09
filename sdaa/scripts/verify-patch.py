#!/usr/bin/env python
"""校验 SDAA 适配补丁是否已正确应用（用于构建后/回退后复核）。

用法（必须在**项目根**执行，即 sdaa/ 已以符号链接挂上来的那一层）：
    cd <项目根>
    python3 scripts/verify-patch.py
"""

from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path


def _find_root() -> Path:
    """定位「项目根」：旁边就有 ImmuneBuilder/ 源码仓库的那一层。

    只认「项目根」这一种布局（见 LOONG_SDAA_REPRO.md 第 1 节），因为下面的
    git 校验需要 ROOT/ImmuneBuilder 是真正的 git 仓库。
    用 os.path.abspath（只做词法规范化）而不是 Path.resolve()，
    因为 resolve() 会解析符号链接，会把根错算到 sdaa/ 上。
    """
    here = Path(os.path.abspath(__file__)).parent
    for candidate in (here.parent, here.parent.parent):
        if (candidate / "ImmuneBuilder" / "ImmuneBuilder" / "refine.py").is_file():
            return candidate
    raise SystemExit(
        "无法定位项目根。请先建立正确布局再执行：\n"
        "    cd <项目根>\n"
        "    ./ImmuneBuilder/sdaa/scripts/bootstrap-workspace.sh\n"
        "    python3 scripts/verify-patch.py\n"
        "详见 sdaa/LOONG_SDAA_REPRO.md 第 1 节。"
    )


ROOT = _find_root()
SRC = ROOT / "ImmuneBuilder" / "ImmuneBuilder"

# (文件, 必须存在, 必须不存在)
CHECKS = [
    ("ABodyBuilder2.py", ["resolve_device()", "resolve_device, seed_all", "--seed", "seed_all(args.seed)"],
     ['self.device = "cuda" if torch.cuda.is_available() else "cpu"']),
    ("NanoBodyBuilder2.py", ["resolve_device()", "resolve_device, seed_all", "--seed", "seed_all(args.seed)"],
     ['self.device = "cuda" if torch.cuda.is_available() else "cpu"']),
    ("TCRBuilder2.py", ["resolve_device()", "resolve_device, seed_all", "--seed", "seed_all(args.seed)"],
     ['self.device = "cuda" if torch.cuda.is_available() else "cpu"']),
    ("refine.py", [
        "resolve_openmm_platform()",
        "from ImmuneBuilder.device_utils import resolve_openmm_platform",
        'platform.getName() == "CPU"',
        # 平台解析已提到分支之外，两个分支都显式传入平台
        "app.Simulation(modeller.topology, system, integrator, platform)",
     ],
     ["Platform.getPlatformByName('CPU')", "simulation_with_platform"]),
    ("device_utils.py", ["def resolve_device", "def resolve_openmm_platform", "def seed_all"],
     []),
]

# 上游 commit，用于确认没有额外的非预期改动
EXPECTED_UPSTREAM_COMMIT = "0df4e2ad82a1aa60f37ea9dae335d1198159ef78"

# 允许被修改的文件白名单（其余文件必须与上游一致 —— 由 git status 校验）
ALLOWED_MODIFIED = {
    "ImmuneBuilder/ABodyBuilder2.py",
    "ImmuneBuilder/NanoBodyBuilder2.py",
    "ImmuneBuilder/TCRBuilder2.py",
    "ImmuneBuilder/refine.py",
}
ALLOWED_UNTRACKED = {"ImmuneBuilder/device_utils.py"}
# 允许的未跟踪目录前缀（SDAA 适配工件目录、备份目录）
ALLOWED_UNTRACKED_PREFIXES = ("sdaa/", "backups/")
# 允许被修改的目录前缀：sdaa/ 是适配材料本身，不是上游源码。
# 适配提交之后如果再改动 sdaa/ 内的文件（文档、脚本、示例），git status 会显示
# " M sdaa/..."，这不应算作非预期改动 —— 真正需要保护的源码文件由上面的
# CHECKS 逐文件做内容校验。
ALLOWED_MODIFIED_PREFIXES = ("sdaa/",)


def main() -> int:
    ok = True
    print("== 补丁内容校验 ==")
    for name, must_have, must_not_have in CHECKS:
        path = SRC / name
        if not path.is_file():
            print("  FAIL %-22s 文件不存在" % name)
            ok = False
            continue
        text = path.read_text()
        missing = [m for m in must_have if m not in text]
        leftover = [m for m in must_not_have if m in text]
        if missing or leftover:
            ok = False
            if missing:
                print("  FAIL %-22s 缺少: %s" % (name, missing))
            if leftover:
                print("  FAIL %-22s 仍存在上游硬编码: %s" % (name, leftover))
        else:
            sha = hashlib.sha256(text.encode()).hexdigest()
            print("  OK   %-22s sha256=%s" % (name, sha))

    print()
    print("== git 状态校验（确认没有非预期改动） ==")
    import subprocess

    repo = ROOT / "ImmuneBuilder"
    head = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                          capture_output=True, text=True).stdout.strip()
    print("  HEAD = %s" % head)
    if head != EXPECTED_UPSTREAM_COMMIT:
        # 适配提交会把 HEAD 前移到新的 commit；此时只要求上游基线仍是它的祖先。
        ancestor = subprocess.run(
            ["git", "-C", str(repo), "merge-base", "--is-ancestor",
             EXPECTED_UPSTREAM_COMMIT, head],
            capture_output=True, text=True)
        if ancestor.returncode == 0:
            print("  OK   上游基线 %s 是当前 HEAD 的祖先（适配已提交）"
                  % EXPECTED_UPSTREAM_COMMIT[:8])
        else:
            print("  FAIL 当前 HEAD 与上游基线 %s 无祖先关系"
                  % EXPECTED_UPSTREAM_COMMIT[:8])
            ok = False

    porcelain = subprocess.run(["git", "-C", str(repo), "status", "--porcelain"],
                               capture_output=True, text=True).stdout.splitlines()
    unexpected = []
    for line in porcelain:
        status, _, path = line[:2], line[2], line[3:].strip()
        if status.strip() == "??":
            if path not in ALLOWED_UNTRACKED and not path.startswith(ALLOWED_UNTRACKED_PREFIXES):
                unexpected.append(line)
        elif path not in ALLOWED_MODIFIED and not path.startswith(ALLOWED_MODIFIED_PREFIXES):
            unexpected.append(line)
    if unexpected:
        ok = False
        print("  未预期的改动:")
        for line in unexpected:
            print("    " + line)
    else:
        modified = sorted(l[3:].strip() for l in porcelain)
        print("  改动文件（均在白名单内）: %s" % (modified or "无"))

    print()
    print("PATCH_VERIFY_" + ("OK" if ok else "FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
