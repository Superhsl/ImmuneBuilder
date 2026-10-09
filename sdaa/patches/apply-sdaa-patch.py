#!/usr/bin/env python
"""把 ImmuneBuilder SDAA/龙芯 适配补丁应用到 ImmuneBuilder/ 源码树。

设计目标：**最小化修改** + **可审计** + **可回退**。

* 所有替换都基于精确的原文串匹配；匹配数不为 1 时立即报错（不会产生半改状态）。
* 默认 dry-run（只报告将要改什么）；加 --apply 才真正写文件。
* 每次写入前把原文件备份到 backups/patches-before-apply-<timestamp>/。
* 应用后打印每个文件的 sha256 与统一 diff。

补丁清单（共 4 类，全部为设备/平台选择，不触碰任何算法逻辑）：
  1. 三个 builder：`self.device = "cuda" if torch.cuda.is_available() else "cpu"`
     -> `self.device = resolve_device()`
  2. 三个 builder：新增 `from ImmuneBuilder.device_utils import resolve_device, seed_all`
  3. 三个 builder：新增 `--seed` 命令行参数（CPU/SDAA 对比可复现）
  4. refine.py：两处 `if n_threads > 0:` 分支
     - 平台由硬编码 `Platform.getPlatformByName('CPU')` 改为 `resolve_openmm_platform()`
     - **把平台解析提到分支之外**，并在两个分支都显式传入平台，
       使 `IMMUNEBUILDER_OPENMM_PLATFORM` 在 `n_threads <= 0` 时同样生效
       （原上游在 `n_threads <= 0` 时不传平台，交由 OpenMM 自选，无法受控）
     - `Threads` 属性只被 CPU 平台识别，改为仅在该平台上传递
     - 第二处同时修掉上游把 dict 误写成 set 字面量的 bug

用法：
    python scripts/apply-sdaa-patch.py                # dry-run
    python scripts/apply-sdaa-patch.py --apply        # 实际应用
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path


def _find_root() -> Path:
    """定位「项目根」（旁边就是 ImmuneBuilder/ 源码仓库的那一层）。

    用法上要求在项目根执行，即 sdaa/ 的材料已以符号链接挂到项目根
    （见 LOONG_SDAA_REPRO.md 第 1 节）。
    这里用 os.path.abspath（只做词法规范化）而不是 Path.resolve()，
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
        "    python3 patches/apply-sdaa-patch.py\n"
        "详见 sdaa/LOONG_SDAA_REPRO.md 第 1 节。"
    )


ROOT = _find_root()
SRC = ROOT / "ImmuneBuilder" / "ImmuneBuilder"

BUILDERS = ["ABodyBuilder2.py", "NanoBodyBuilder2.py", "TCRBuilder2.py"]

DEVICE_OLD = '        self.device = "cuda" if torch.cuda.is_available() else "cpu"\n'
DEVICE_NEW = "        self.device = resolve_device()\n"

IMPORT_OLD = "from ImmuneBuilder.models import StructureModule\n"
IMPORT_NEW = (
    "from ImmuneBuilder.device_utils import resolve_device, seed_all\n"
    "from ImmuneBuilder.models import StructureModule\n"
)

SEED_ARG_OLD = "    args = parser.parse_args()\n"
SEED_ARG_NEW = (
    "    parser.add_argument(\"--seed\", help=\"Fix the random seed for reproducible CPU/SDAA comparison runs.\", type=int, default=None)\n"
    "\n"
    "    args = parser.parse_args()\n"
    "\n"
    "    if args.seed is not None:\n"
    "        seed_all(args.seed)\n"
)

REFINE_IMPORT_OLD = "from scipy import spatial\n"
REFINE_IMPORT_NEW = (
    "from scipy import spatial\n"
    "from ImmuneBuilder.device_utils import resolve_openmm_platform\n"
)

# ---- refine.py 的两处 Simulation 构造 ----
# 上游第一处：dict 写法
BLOCK_A_OLD = (
    "    # Set up the simulation\n"
    "    if n_threads > 0:\n"
    "        # Set number of threads used by OpenMM\n"
    "        platform = Platform.getPlatformByName('CPU')\n"
    "        simulation = app.Simulation(modeller.topology, system, integrator, platform, {'Threads': str(n_threads)})\n"
    "    else:\n"
    "        simulation = app.Simulation(modeller.topology, system, integrator)\n"
)
# 上游第二处：set 字面量（上游 bug），且 else 行尾有 4 个多余空格
BLOCK_B_OLD = (
    "    # Set up the simulation\n"
    "    if n_threads > 0:\n"
    "        # Set number of threads used by OpenMM\n"
    "        platform = Platform.getPlatformByName('CPU')\n"
    "        simulation = app.Simulation(modeller.topology, system, integrator, platform, {'Threads', str(n_threads)})\n"
    "    else:\n"
    "        simulation = app.Simulation(modeller.topology, system, integrator)    \n"
)
BLOCK_NEW = (
    "    # Set up the simulation\n"
    "    # SDAA 适配：平台改为按可用性解析（auto: CUDA -> CPU -> SDAA -> Reference），\n"
    "    # 并把解析提到分支之外、在两个分支都显式传入，使 IMMUNEBUILDER_OPENMM_PLATFORM\n"
    "    # 在 n_threads<=0 时同样生效（上游此时不传平台，交由 OpenMM 自选，无法受控）。\n"
    "    platform = resolve_openmm_platform()\n"
    "    if n_threads > 0:\n"
    "        # Set number of threads used by OpenMM\n"
    "        # 'Threads' 属性只被 CPU 平台识别，仅在该平台上传递。\n"
    "        simulation = app.Simulation(modeller.topology, system, integrator, platform,\n"
    "                                    {'Threads': str(n_threads)} if platform.getName() == \"CPU\" else {})\n"
    "    else:\n"
    "        simulation = app.Simulation(modeller.topology, system, integrator, platform)\n"
)


def build_plan():
    plan = []

    for name in BUILDERS:
        path = SRC / name
        if not path.is_file():
            raise SystemExit("ERROR: 找不到 %s" % path)
        text = path.read_text()
        for old, new, label in (
            (DEVICE_OLD, DEVICE_NEW, "device select"),
            (IMPORT_OLD, IMPORT_NEW, "import resolve_device/seed_all"),
            (SEED_ARG_OLD, SEED_ARG_NEW, "--seed CLI option"),
        ):
            count = text.count(old)
            if count != 1:
                raise SystemExit(
                    "ERROR: %s 中 %s 的匹配数为 %d（期望 1）" % (name, label, count)
                )
            plan.append((path, old, new, label))
        merged = (
            text.replace(DEVICE_OLD, DEVICE_NEW)
            .replace(IMPORT_OLD, IMPORT_NEW)
            .replace(SEED_ARG_OLD, SEED_ARG_NEW)
        )
        for marker in ("resolve_device()", "seed_all", "--seed", "args.seed"):
            if marker not in merged:
                raise SystemExit("ERROR: %s 替换自检失败（缺少 %s）" % (name, marker))

    refine = SRC / "refine.py"
    text = refine.read_text()
    if text.count(REFINE_IMPORT_OLD) != 1:
        raise SystemExit("ERROR: refine.py 导入锚点匹配数不为 1")
    if text.count(BLOCK_A_OLD) != 1:
        raise SystemExit(
            "ERROR: refine.py 第一处 Simulation 块匹配数为 %d（期望 1）"
            % text.count(BLOCK_A_OLD)
        )
    if text.count(BLOCK_B_OLD) != 1:
        raise SystemExit(
            "ERROR: refine.py 第二处 Simulation 块匹配数为 %d（期望 1）"
            % text.count(BLOCK_B_OLD)
        )

    plan.append((refine, REFINE_IMPORT_OLD, REFINE_IMPORT_NEW, "import resolve_openmm_platform"))
    plan.append((refine, BLOCK_A_OLD, BLOCK_NEW, "platform resolution hoisted out of branch (1/2)"))
    plan.append((refine, BLOCK_B_OLD, BLOCK_NEW, "platform resolution + upstream set bug (2/2)"))

    return plan


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真正写入文件（默认 dry-run）")
    args = ap.parse_args()

    if not SRC.is_dir():
        raise SystemExit("ERROR: 找不到源码目录 %s" % SRC)

    plan = build_plan()

    print("== 计划修改（dry-run）==" if not args.apply else "== 应用修改 ==")
    by_file: dict[Path, list[str]] = {}
    for path, _old, _new, label in plan:
        by_file.setdefault(path, []).append(label)
    for path, labels in by_file.items():
        print("  %-22s : %s" % (path.name, "; ".join(labels)))

    if not args.apply:
        print("\n未写入任何文件。加 --apply 才会应用。")
        return 0

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_dir = ROOT / "backups" / ("patches-before-apply-%s" % stamp)
    backup_dir.mkdir(parents=True, exist_ok=True)

    originals = {}
    for path, _old, _new, _label in plan:
        if path not in originals:
            originals[path] = path.read_text()
            shutil.copy2(path, backup_dir / path.name)
    print("\n== 原文件已备份到 %s ==" % backup_dir)

    for path, old, new, _label in plan:
        text = path.read_text()
        if text.count(old) < 1:
            raise SystemExit("ERROR: 应用阶段 %s 匹配失败: %r" % (path.name, old[:60]))
        path.write_text(text.replace(old, new))

    print("\n== 修改后 sha256 与 diff ==")
    for path in sorted(originals):
        new_text = path.read_text()
        print("  %-22s sha256=%s" % (path.name, hashlib.sha256(new_text.encode()).hexdigest()))
        sys.stdout.writelines(
            difflib.unified_diff(
                originals[path].splitlines(keepends=True),
                new_text.splitlines(keepends=True),
                fromfile="a/%s" % path.name,
                tofile="b/%s" % path.name,
                n=2,
            )
        )
        print()

    print("== 完成 ==")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
