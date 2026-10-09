"""设备与平台解析工具（ImmuneBuilder SDAA 适配新增文件）。

上游 ImmuneBuilder 在三个 builder 中都硬编码了：

    self.device = "cuda" if torch.cuda.is_available() else "cpu"

在龙芯 SDAA 环境下 `torch.cuda.is_available()` 恒为 False（torch 是 CPU 构建 +
torch_sdaa 私有后端），因此上游代码会静默回落到 CPU，SDAA 卡完全不被使用。
本模块用最小的接口替换上述判断，并保持完全向后兼容：

* 默认 ``auto``：优先 CUDA，其次 SDAA，最后 CPU —— 在上游 CUDA 环境行为不变。
* 可用环境变量 ``IMMUNEBUILDER_DEVICE`` 覆盖，取值为
  ``auto | cpu | cuda | cuda:N | sdaa | sdaa:N``。
* ``torch_sdaa`` 只在需要时导入（导入它会注册 SDAA 后端并触发一次版本横幅输出）。
"""

import os
import random

import numpy as np
import torch

# 支持通过环境变量覆盖的取值
VALID_PREFIXES = ("auto", "cpu", "cuda", "sdaa")

_SDAA_REGISTERED = None


def _register_sdaa():
    """导入 torch_sdaa 以注册 SDAA 后端；返回 torch.sdaa 命名空间或 None。"""
    global _SDAA_REGISTERED
    if _SDAA_REGISTERED is None:
        try:
            import torch_sdaa  # noqa: F401  （导入即注册后端）
        except ImportError:
            _SDAA_REGISTERED = False
        else:
            _SDAA_REGISTERED = True
    if not _SDAA_REGISTERED:
        return None
    return getattr(torch, "sdaa", None)


def _sdaa_available():
    sdaa = _register_sdaa()
    if sdaa is None:
        return False
    try:
        return bool(sdaa.is_available())
    except Exception:
        return False


def _check_requested(requested):
    """显式请求的设备若不可用则直接报错，避免静默回落。"""
    if requested.startswith("sdaa"):
        if not _sdaa_available():
            raise RuntimeError(
                "IMMUNEBUILDER_DEVICE=%r 被指定，但 SDAA 设备不可用"
                "（检查 torch_sdaa 是否安装、/dev/tcaicard* 是否映射进容器）" % requested
            )
    elif requested.startswith("cuda"):
        if not torch.cuda.is_available():
            raise RuntimeError(
                "IMMUNEBUILDER_DEVICE=%r 被指定，但 CUDA 设备不可用" % requested
            )
    elif requested != "cpu":
        raise ValueError(
            "IMMUNEBUILDER_DEVICE 取值必须是 auto、cpu、cuda[:index] 或 sdaa[:index]，"
            "当前为 %r" % requested
        )


def resolve_device(requested=None):
    """解析计算设备，返回 torch.device。

    参数
    ----
    requested:
        显式指定设备；为 None 时依次读取环境变量 ``IMMUNEBUILDER_DEVICE`` 与 ``auto``。

    返回
    ----
    ``torch.device``，其 ``.type`` 为 ``cpu`` / ``cuda`` / ``sdaa``。
    """
    if requested is None:
        requested = os.environ.get("IMMUNEBUILDER_DEVICE", "auto")
    requested = str(requested).strip().lower()
    if not requested:
        requested = "auto"
    if not requested.startswith(VALID_PREFIXES):
        raise ValueError(
            "无法识别的设备 %r；支持 auto、cpu、cuda[:index]、sdaa[:index]" % requested
        )

    if requested == "auto":
        if torch.cuda.is_available():
            return torch.device("cuda:0")
        sdaa = _register_sdaa()
        if sdaa is not None and _sdaa_available():
            return torch.device("sdaa:0")
        return torch.device("cpu")

    _check_requested(requested)
    return torch.device(requested)


def resolve_openmm_platform(requested=None, n_threads=None):
    """解析 OpenMM 精修平台，返回一个已注册的 ``Platform`` 对象。

    上游 ``refine.py`` 硬编码 ``Platform.getPlatformByName('CPU')``，有两处问题：

    1. 要求 OpenMM 构建里必须有 CPU 平台（取决于 ``OPENMM_BUILD_CPU_LIB``），
       否则抛 ``OpenMMException: There is no registered Platform called "CPU"``；
    2. 无法切换平台，也就无法在具备加速器时利用它。

    本函数改为按可用性解析。**默认优先 CPU**，加速器平台需显式指定：

    * ``auto``（默认）顺序：**CUDA → CPU → SDAA → Reference**
      - x86_64 + NVIDIA 节点仍有 CUDA 平台，行为与上游一致；
      - 龙芯节点没有 CUDA 平台，于是默认落在 **CPU**；
      - SDAA 属加速器路径，需显式设置才启用（见下）。
    * 显式指定时若该平台未注册，**直接报错**而不是静默换用别的平台
      （静默回落正是本项目最初踩到的坑）。

    启用 SDAA 精修（可选）：

    .. code-block:: bash

        IMMUNEBUILDER_OPENMM_PLATFORM=SDAA
        IMMUNEBUILDER_OPENMM_PROPERTIES="UseCpuDirect=true,Precision=mixed"

    之所以默认不用 SDAA：实测当前 OpenMM 适配在**大原子数**体系上相对 CPU 并无优势，
    CPU 路径更稳定、更可移植。

    参数
    ----
    requested:
        显式平台名；为 None 时读 ``IMMUNEBUILDER_OPENMM_PLATFORM``，再退回 ``auto``。
        大小写不敏感。
    n_threads:
        保留以兼容既有调用方（历史版本曾用它影响选择顺序），**不再参与平台选择**。

    返回
    ----
    一个已注册的 ``Platform``；Reference 在任何 OpenMM 构建中都存在，故兜底可达。
    """
    from openmm import Platform

    if requested is None:
        requested = os.environ.get("IMMUNEBUILDER_OPENMM_PLATFORM", "auto")
    requested = str(requested).strip()

    available = {
        Platform.getPlatform(i).getName() for i in range(Platform.getNumPlatforms())
    }
    if not available:
        raise RuntimeError("OpenMM 未注册任何平台，环境异常")

    if requested and requested.lower() != "auto":
        for name in available:
            if name.lower() == requested.lower():
                return Platform.getPlatformByName(name)
        raise RuntimeError(
            "IMMUNEBUILDER_OPENMM_PLATFORM=%r 在本环境不可用；已注册平台：%s。"
            "如需自动选择请设为 auto。" % (requested, sorted(available))
        )

    for name in ("CUDA", "CPU", "SDAA", "Reference"):
        if name in available:
            return Platform.getPlatformByName(name)
    # 理论不可达：Reference 总是存在
    return Platform.getPlatform(0)


def describe_devices():
    """返回当前可见后端的诊断信息（供 verify/日志使用）。"""
    info = {
        "torch": torch.__version__,
        "torch_cuda": torch.version.cuda,
        "cuda_available": bool(torch.cuda.is_available()),
        "sdaa_available": _sdaa_available(),
    }
    if info["sdaa_available"]:
        sdaa = getattr(torch, "sdaa")
        info["sdaa_version"] = getattr(sdaa, "__version__", None)
        info["sdaa_device_count"] = int(sdaa.device_count())
    try:
        from openmm import Platform

        info["openmm_platforms"] = [
            Platform.getPlatform(i).getName() for i in range(Platform.getNumPlatforms())
        ]
    except Exception:
        pass
    return info


def seed_all(seed, device=None):
    """固定 python / numpy / torch 以及当前加速后端的随机数种子。

    上游 ImmuneBuilder 全程不做采样，但 OpenMM 精修阶段会用 ``np.random.rand()``
    生成临时文件名，且无法从命令行固定种子。加这个入口是为了让 CPU/SDAA 对比
    测试可复现。
    """
    seed = int(seed)
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)

    dev_type = getattr(device, "type", None) or str(device or "")
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(seed)
    if dev_type.startswith("sdaa") or _sdaa_available():
        sdaa = _register_sdaa()
        if sdaa is not None and hasattr(sdaa, "manual_seed_all"):
            sdaa.manual_seed_all(seed)
        elif sdaa is not None and hasattr(sdaa, "manual_seed"):
            sdaa.manual_seed(seed)
    return seed
