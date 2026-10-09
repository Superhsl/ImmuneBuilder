# ImmuneBuilder SDAA 适配与验证报告

## 1. 结论摘要

ImmuneBuilder 预训练推理已完成龙芯 SDAA 适配。模型结构、算法流程与
`models.py` / `rigids.py` / `util.py` 均未修改，只在三个推理入口与精修阶段
增加设备/平台选择、随机种子命令行参数。

当前验证结论：

- 官方 Python API 与官方 CLI 两套路径各 3 个用例全部通过，输出结构均通过四项几何判据
  （键长、顺反肽键、手性、原子重叠）；
- 精修平台默认 **CPU**，SDAA 为**可选加速路径**，需显式开启；

- 跨平台 CA-RMSD ≤ `0.32 Å`，与同机重复运行的自身差异同量级，未观察到平台导致的精度偏移；

## 2. 软件环境

| 环境 | PyTorch | torch_sdaa/CUDA | Runtime | 说明 |
| --- | --- | --- | --- | --- |
| CUDA 基线 | `2.8.0+cu128` | CUDA `12.8` | CUDA runtime 12.8 | GPU 型号应在正式报告中补录 |
| 龙芯 SDAA | `2.12.0` | torch_sdaa `3.2.1` | SDAA `3.1.0`（Driver 3.1.0） | TecoDNN/TecoBLAS/TCCL 均为 `3.2.1` |

其余组件：

| 组件 | CUDA 基线 | 龙芯 SDAA |
| --- | --- | --- |
| Python | `3.11.13` | `3.12.13` |
| OpenMM | `8.3.1` | `8.6`（预编译前缀复用，无 wheel） |
| PDBFixer | `1.11` | `1.12.0` |
| ANARCI | conda `2024.05.21` | `2026.2.13.2`（PyPI wheel） |
| 架构 / 内核 | `x86_64` | `loongarch64` / Loongnix Server 23.1 |

## 3. 源码适配点

### 3.1 设备选择

三个推理入口（`ABodyBuilder2` / `NanoBodyBuilder2` / `TCRBuilder2`）原本使用：

```python
self.device = "cuda" if torch.cuda.is_available() else "cpu"
```

在 SDAA 环境中 `torch.cuda.is_available()` 恒为 `False`（torch 为 CPU 构建 +
私有后端），该判断会**静默回落到 CPU**，日志无任何提示。适配后改为：

```python
self.device = resolve_device()
```

`resolve_device()` 支持：

```text
auto          顺序为 CUDA、SDAA、CPU
cpu
cuda:0
sdaa:0
```

`auto` 在 CUDA 机器上行为与上游一致。只有需要 SDAA 时才导入 `torch_sdaa` 完成设备注册，
并在用户显式指定不可用设备时立即报错，避免静默回退到 CPU。

### 3.2 精修平台选择

`refine.py` 原本硬编码 OpenMM 的 `'CPU'` 平台，且只在 `n_threads > 0` 分支内选择平台：

```python
if n_threads > 0:
    platform = Platform.getPlatformByName('CPU')
    simulation = app.Simulation(..., platform, {'Threads': str(n_threads)})
else:
    simulation = app.Simulation(...)          # 不传平台，交由 OpenMM 自选
```

适配后把平台解析提到分支之外、两个分支都显式传入，并按可用性解析：

```python
# Set up the simulation
platform = resolve_openmm_platform()
if n_threads > 0:
    # 'Threads' 属性只被 CPU 平台识别，仅在该平台上传递
    simulation = app.Simulation(..., platform,
                                {'Threads': str(n_threads)} if platform.getName() == "CPU" else {})
else:
    simulation = app.Simulation(..., platform)
```

`auto` 的选择顺序为 **CUDA、CPU、SDAA、Reference**：

- x86_64 + NVIDIA 节点仍有 CUDA 平台，行为与上游一致；
- 龙芯节点没有 CUDA 平台，于是**默认落在 CPU**；
- SDAA 需显式开启，并同时给出 SDAA 专有属性：

```text
IMMUNEBUILDER_OPENMM_PLATFORM=SDAA
IMMUNEBUILDER_OPENMM_PROPERTIES=UseCpuDirect=true,Precision=mixed
```

`Threads` 属性只被 CPU 平台接受（SDAA / Reference 会抛
`OpenMMException: Illegal property name: Threads`），故只对 CPU 平台传递。
用户在 `refine.py` 中指定的 `n_threads` 之外，CPU 平台线程数由
`OPENMM_CPU_THREADS` 提供默认值（本适配取 `32`）。

### 3.3 随机数

ImmuneBuilder 全程 `torch.no_grad()` 推理、不做采样，但精修阶段用
`np.random.rand()` 生成临时文件名，且无法从命令行固定种子。适配后为三个入口
增加 `--seed`，并统一固定：

```python
random.seed(seed)
np.random.seed(seed)
torch.manual_seed(seed)
```

SDAA 额外执行：

```python
torch.sdaa.manual_seed_all(seed)
```

这保证同一 SDAA 环境的重复运行可复现，但不保证 CUDA 和 SDAA 使用相同随机流。
需要说明的是：即使固定种子，精修阶段的最小化仍存在浮点不确定性。

### 3.4 模型实现

`models.py`、`rigids.py`、`util.py`、`constants.py` 均未修改。模型中的张量创建
基本跟随 `node_features.device` 或已有张量 device（如
`rigid_body_identity(len(sequence)).to(node_features.device)`、
`torch.eye(3, device=U.device)`），因此没有引入 SDAA 专用模型分支。

已覆盖的关键算子包括：

```text
Linear, LayerNorm, ReLU, Softmax, einsum, matmul,
linalg.svd, linalg.det, one_hot, cumsum, clamp, stack, cat
```

其中 `linalg.svd` / `linalg.det`（`util.py` 中的 Kabsch 对齐）已在 SDAA 上单独验证。

## 4. 适配过程中遇到的问题

| 问题/报错 | 原因 | 解决方法 |
| --- | --- | --- |
| 程序正常结束但 SDAA 卡完全未被使用 | `torch.cuda.is_available()` 在 SDAA 环境恒为 `False`，设备判断静默回落 CPU | 改为 `resolve_device()`，`auto` 顺序 CUDA、SDAA、CPU |
| 精修反复失败并打印 `FAILED TO REFINE`，但无异常抛出 | 非 CPU 平台不接受 `{'Threads': ...}`，抛出的 `OpenMMException` 被 `refine_once` 的 `try/except` 吞掉 | 仅当平台为 `CPU` 时传 `Threads`；并修正上游把 dict 误写成 set 字面量的 bug |
| 设了 `IMMUNEBUILDER_OPENMM_PLATFORM=CPU` 却不生效，精修仍走 SDAA | 上游在 `n_threads <= 0` 分支不传平台，交由 OpenMM 自选 | 把平台解析提到 `if n_threads > 0` 之外，两个分支都显式传入 |
| `getPlatformByName('CPU')` 抛 `There is no registered Platform called "CPU"` | OpenMM 是否编译 CPU 平台取决于 `OPENMM_BUILD_CPU_LIB`，并非所有构建都有 | 改为按可用性解析，显式指定不可用时立即报错而非静默换平台 |
| `UseCpuDirect` 属性不存在或设了无效 | 构建时 `OPENMM_BUILD_CPU_LIB=OFF` 会把 CPU-direct 通路静默编译掉 | 选用 `OPENMM_BUILD_CPU_LIB=ON` 的构建；用 `readelf -d`/`nm` 做二进制级确认 |
| HMMER 编译失败：`No supported vectorization found for your machine` | HMMER 3.4 强制要求 SSE/NEON/VMX 之一，源码无标量回退；LoongArch 只有 LSX/LASX，且 GCC 无 SSE 内联函数兼容层 | 采用 amd64 HMMER + 静态链接 `qemu-x86_64` 用户态仿真（复用 LoongArch 的 x86 二进制翻译能力） |
| HMMER 源码包没有 `configure`；`config.guess` 报 `unable to guess system type` | 源码包只带 `configure.ac`（需 autotools）；自带 `config.guess`（2018-03-08）不认识 loongarch64 | 用最小 `config.guess` 输出 `loongarch64-unknown-linux-gnu`（自带 `config.sub` 接受该三元组） |
| 发行版仓库的 `qemu-user` 报 `undefined symbol: fopen, version GLIBC_2.2.5` | 该 qemu 版本在同一 sysroot 下无法正确解析 amd64 glibc | 改用静态链接的 `qemu-x86_64` |
| 新 venv 中 `import torch` 失败 | 基础镜像的 torch/numpy/biopython 实际装在 `/home/py312/...`，靠基础 venv 的 `.pth` 暴露；普通 venv 不处理父 venv 的 `.pth`，`--system-site-packages` 也只暴露 `/usr/local/python` | 把同一个 `.pth` 复制到新 venv 自己的 site-packages |
| `pip install` 全部被阻断 | 基础镜像内置 `PIP_NO_INDEX=1`，且索引指向不含所需包的源 | 显式 `PIP_NO_INDEX=0` 并指定可用索引源 |
| 换 OpenMM 构建后行为异常 | 命中旧 SDAA JIT fatbin；且不同构建对应不同的 `tecofft` 包 | 换构建时同步换 `tecofft`（sha256 校验）并清 JIT 缓存（已做成自动检测） |

## 5. 精度对比

### 5.1 OpenMM 平台力和一致性

使用 OpenMM 官方 `testInstallation`（同一体系在参考平台与目标平台计算力并比较）：

| 对比 | 力的中位差 | 判定 |
| --- | ---: | --- |
| Reference vs SDAA | `6.7e-06` | `All differences are within tolerance` |

### 5.2 神经网络输出等价性

同一随机种子下，比较 CPU 与 SDAA 两条路径的模型输出误差估计
（`error_estimates`，作为模型输出等价性的代理指标）：

| 指标 | 值 |
| --- | ---: |
| 最大绝对差 | `1.24e-05` |
| 平均绝对差 | `1.10e-06` |
| 相对差 | `9.78e-07` |

属浮点累加顺序差异，没有观察到明显的数值偏移。

### 5.3 结构精度（Cα-RMSD）

同配置重复运行的自一致性（同一臂 run2 vs run3，反映精修浮点不确定性的下限，单位 Å）：

| 配置 | antibody (228aa) | nanobody (125aa) | tcr (228aa) |
| --- | ---: | ---: | ---: |
| CUDA（H100） | 0.167 | 0.000 | 0.010 |
| x86_64 CPU 32 线程 | 0.155 | 0.006 | 0.101 |
| 龙芯 CPU 32 线程 | 0.174 | 0.065 | 0.009 |
| 龙芯 SDAA | 0.192 | 0.000 | 0.060 |

跨平台 CA-RMSD（以一次龙芯 SDAA 运行为基准，单位 Å）：

| 对比 | antibody | nanobody | tcr |
| --- | ---: | ---: | ---: |
| CUDA vs 龙芯 SDAA | 0.306 | 0.182 | 0.215 |
| x86_64 CPU vs 龙芯 SDAA | 0.321 | 0.182 | 0.183 |
| 龙芯 CPU vs 龙芯 SDAA | 0.319 | 0.188 | 0.215 |

跨平台差异（antibody 0.20–0.32 Å）与同一配置自身重复运行的差异（0.15–0.22 Å）**同量级**，
即差异全部落在精修浮点不确定性范围内，不构成平台差异。相对典型结构预测误差（1–2 Å）可忽略。

### 5.4 结构有效性

三个官方用例在两个平台上输出均通过四项几何判据：

| 用例 | 原子数 | 键长 | 顺反肽键 | 手性 | 原子重叠 |
| --- | ---: | :-: | :-: | :-: | :-: |
| antibody | 3483 | 通过 | 通过 | 通过 | 通过 |
| nanobody | 1850 | 通过 | 通过 | 通过 | 通过 |
| tcr | 3396 | 通过 | 通过 | 通过 | 通过 |

## 6. 性能对比

三个用例规模：antibody 228 残基 / 3483 原子，nanobody 125 残基 / 1850 原子，
tcr 228 残基 / 3396 原子。同节点内 `--runs 3` 取中位数，单位秒。

### 6.1 端到端推理

| 平台/运行 | antibody | nanobody | tcr | 三用例合计 |
| --- | ---: | ---: | ---: | ---: |
| CUDA（H100） | `6.45` | `4.38` | `4.94` | `15.8` |
| x86_64 CPU 32 线程 | `11.99` | `5.43` | `9.21` | `26.6` |
| 龙芯 SDAA（可选加速路径） | `25.4` | `10.3` | `19.4` | `55.0` |
| 龙芯 CPU 32 线程 | `25.5` | `12.8` | `22.4` | `60.7` |
| 龙芯 SDAA（默认，精修走 SDAA 无属性） | `38.9` | `12.4` | `30.1` | `81.4` |

分阶段（龙芯，秒）：

| 阶段 | antibody | nanobody | tcr |
| --- | ---: | ---: | ---: |
| 神经网络推理（SDAA） | `10.6` | `4.4` | `9.3` |
| 神经网络推理（龙芯 CPU） | `11.6` | `6.1` | `10.2` |
| 精修（龙芯 CPU 32 线程） | `11.3` | `4.0` | `10.0` |
| 精修（SDAA + UseCpuDirect + mixed） | `11.6` | `3.3` | `7.6` |

### 6.2 精修平台细分

| 平台/属性 | 三用例合计 | 相对龙芯 CPU 32 线程 |
| --- | ---: | ---: |
| SDAA（默认） | `81.4` | `1.34x` |
| SDAA + `Precision=mixed` | `81.0` | `1.33x` |
| 龙芯 CPU 32 线程 | `60.7` | `1.00x` |
| SDAA + `UseCpuDirect` | `58.8` | `0.97x` |
| SDAA + `UseCpuDirect` + `Precision=mixed` | `55.0` | `0.91x` |

要点：

- `UseCpuDirect` 是 SDAA 侧唯一有效的大杠杆（三用例分别 −23% / −21% / −36%）；
- `Precision=mixed` 单独开启没有收益甚至变差，只在配合 `UseCpuDirect` 时才有效；
- 精修是端到端的主要开销（CUDA 上占 70–95%，龙芯 SDAA 上占 71%），
  而它恰是加速器收益最小的一环；

- 线程数不是越多越好：龙芯 32 线程优于 64 与 128（小体系下跨 NUMA 同步开销占主导）。

## 7. 复现命令

```bash
# 环境准备
./scripts/download-deps.sh          # HMMER 源码、tecofft RPM、x86 HMMER + amd64 运行库、静态 qemu
./scripts/build-image.sh            # 构建前强制校验基础镜像 Image ID
./scripts/run-container.sh          # 映射 /dev/tcaicard0；自动检测 OpenMM 构建变更并清 JIT 缓存

# 代码改动与校验
python3 patches/apply-sdaa-patch.py --apply
cp patches/device_utils.py <源码树>/ImmuneBuilder/device_utils.py
python3 scripts/verify-patch.py

# 默认路径：神经网络走 SDAA，精修走 CPU
./scripts/run-smoke-test-sdaa.sh sdaa all 20260921
./scripts/run-cli-test-sdaa.sh sdaa Reference

# 可选加速路径：精修也走 SDAA
./scripts/run-smoke-test-sdaa.sh sdaa-gpu

# 精修平台对比
./scripts/run-refine-bench.sh quick
./scripts/run-refine-bench.sh full

# 单次推理（官方 CLI）
docker exec -e IMMUNEBUILDER_DEVICE=sdaa -e IMMUNEBUILDER_OPENMM_PLATFORM=CPU \
  immunebuilder-sdaa ABodyBuilder2 \
  -f /workspace/repro/inputs/antibody.fasta \
  -o /workspace/repro/work/my_run/antibody.pdb -v
```

精修平台与设备可用环境变量覆盖：

```text
IMMUNEBUILDER_DEVICE=auto | cpu | cuda[:N] | sdaa[:N]      神经网络设备
IMMUNEBUILDER_OPENMM_PLATFORM=auto | CPU | SDAA | Reference 精修平台
IMMUNEBUILDER_OPENMM_PROPERTIES=UseCpuDirect=true,Precision=mixed  仅 SDAA
OPENMM_CPU_THREADS=32                                       CPU 精修线程数
```
