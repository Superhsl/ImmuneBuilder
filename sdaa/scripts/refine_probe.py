#!/usr/bin/env python
"""精修阶段诊断：逐步驱动 ImmuneBuilder 的 OpenMM 精修，输出每一步的判定结果。

目的：区分「SDAA 平台问题」与「OpenMM 在 loongarch64 上通用的收敛问题」。

用法：
    python scripts/refine_probe.py /path/to/unrefined.pdb [Reference|SDAA] [max_iters]
"""

import sys
import time

import numpy as np
from openmm import Platform

from ImmuneBuilder import refine as R  # refine 是模块（from ImmuneBuilder.refine import refine 同名）


def main():
    path = sys.argv[1]
    want_platform = sys.argv[2] if len(sys.argv) > 2 else "Reference"
    max_iters = int(sys.argv[3]) if len(sys.argv) > 3 else 3

    print("platforms:", [Platform.getPlatform(i).getName() for i in range(Platform.getNumPlatforms())])
    plat = Platform.getPlatformByName(want_platform)
    print("selected platform:", plat.getName())

    import pdbfixer
    from openmm import app

    fixer = pdbfixer.PDBFixer(path)
    fixer.findMissingResidues()
    fixer.findMissingAtoms()
    fixer.addMissingAtoms()
    print("residues:", fixer.topology.getNumResidues(), "atoms:", fixer.topology.getNumAtoms())

    k1s = [2.5, 1, 0.5, 0.25, 0.1, 0.001]
    k2s = [2.5, 5, 7.5, 15, 25, 50]

    k1 = k1s[0]
    k2 = -1 if R.cis_check(fixer.topology, fixer.positions) else k2s[0]
    topology, positions = fixer.topology, fixer.positions
    print("initial cis_check:", R.cis_check(topology, positions), "k1:", k1, "k2:", k2)

    n = max_iters
    for i in range(n):
        t0 = time.perf_counter()
        try:
            # 复用上游逻辑，但显式注入我们想测试的平台
            import ImmuneBuilder.device_utils as du
            orig = du.resolve_openmm_platform
            du.resolve_openmm_platform = lambda requested=None: plat
            # refine.minimize_energy 用的是 refine.py 内导入的名字
            R.resolve_openmm_platform = lambda requested=None: plat
            simulation = R.minimize_energy(topology, positions, k1=k1, k2=k2, n_threads=4)
            topology = simulation.topology
            positions = simulation.context.getState(getPositions=True).getPositions()
            arr = np.array([[p.x, p.y, p.z] for p in positions])
            bond_ok = R.bond_check(topology, positions)
            cis_ok = R.cis_check(topology, positions)
            stereo_ok = R.stereo_check(topology, positions)
            clash_ok = R.clash_check(topology, positions)
            print(
                "iter %d: %.1fs finite=%s bond=%s cis=%s stereo=%s clash=%s"
                % (i, time.perf_counter() - t0, np.isfinite(arr).all(), bond_ok, cis_ok, stereo_ok, clash_ok),
                flush=True,
            )
            if not bond_ok:
                k1 = k1s[min(i, len(k1s) - 1)]
            if not cis_ok:
                k2 = k2s[min(i, len(k2s) - 1)]
            else:
                k2 = -1
            if bond_ok and cis_ok and stereo_ok and clash_ok:
                print("CONVERGED at iter", i)
                return 0
        except Exception as exc:
            print("iter %d FAILED %s: %s" % (i, type(exc).__name__, exc), flush=True)
            import traceback

            traceback.print_exc()
            return 1
    print("NOT_CONVERGED after %d iters" % n)
    return 2


if __name__ == "__main__":
    sys.exit(main())
