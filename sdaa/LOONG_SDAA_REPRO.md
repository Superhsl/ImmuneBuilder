# ImmuneBuilder 龙芯 SDAA 复现教程

在**另一台龙芯 LoongArch + SDAA 机器**上，用本仓库复现 ImmuneBuilder 官方推理示例的
完整流程。

* 本文件：**怎么做、怎么判断做对了**
* `SDAA_ADAPTATION.md`：适配原理、精度与性能数据
* `source.sdaa.lock`：环境指纹（镜像 ID、sha256、实测数字）

一句话结论：神经网络推理走 SDAA，OpenMM 精修默认走 CPU（SDAA 为可选加速路径）。
上游算法文件（`models.py` / `rigids.py` / `util.py`）未被修改，改动只有 4 个文件、+38/−9，
全部集中在设备与平台选择。

---

## 1. 目录布局：为什么项目根在仓库的上一层

仓库里所有脚本都以「脚本目录的上一级」作为**项目根**，并假定项目根旁边就有
`ImmuneBuilder/`（源码仓库）、`weights/`、`downloads/`、`.env.sdaa`。
而本仓库是 `ImmuneBuilder/sdaa/...`，所以项目根必须是**仓库的上一级目录**，
再用符号链接把 `sdaa/` 的材料挂上来 —— 这样脚本与 Dockerfile 一个字节都不用改。

**仓库（`git clone` 得到的）**

```text
ImmuneBuilder/                    ← 仓库根
├── ImmuneBuilder/                ← 官方源码包（已含本适配的 4 处改动）
├── sdaa/                         ← 适配材料：Dockerfile、脚本、补丁、本文档
├── data/  notebook/  setup.py  README.md  LICENSE  .github/
```

**项目根（`bootstrap-workspace.sh` 之后）**

```text
<项目根>/                          ← 例如 ~/ImmuneBuilder-sdaa
├── ImmuneBuilder/                ← 仓库（放在这里）
├── scripts -> ImmuneBuilder/sdaa/scripts          # 符号链接
├── patches -> ImmuneBuilder/sdaa/patches
├── locks   -> ImmuneBuilder/sdaa/locks
├── Dockerfile.sdaa -> ImmuneBuilder/sdaa/Dockerfile.sdaa
├── compose.sdaa.yaml -> ImmuneBuilder/sdaa/compose.sdaa.yaml
├── source.sdaa.lock -> ImmuneBuilder/sdaa/source.sdaa.lock
├── .env.sdaa                     # 主机相关变量（生成，不入库）
├── .dockerignore                 # 构建上下文 = 项目根
├── inputs/antibody.fasta         # 官方示例输入
├── weights/                      # 模型权重，官方机制下载到这里
├── downloads/                    # tecofft RPM、x86 HMMER + 静态 qemu
└── work/  logs/  backups/        # 运行产物与备份
```

因为运行产物都在项目根（仓库之外），仓库本身不会被 `weights/`、`work/` 之类的
大文件和垃圾文件污染。

---

## 2. 前置条件

| 项 | 要求 | 自检 |
| --- | --- | --- |
| 架构 / 系统 | `loongarch64`，Loongnix Server 23.1（本机 kernel 6.6.52） | `uname -m` |
| SDAA 卡 | 至少 1 张，设备节点 `/dev/tcaicard0` | `ls -l /dev/tcaicard0` |
| SDAA 驱动 / 运行时 | 与 `torch_sdaa 3.2.1` 匹配（本机实测 3.2.0） | `/opt/tecoai/bin/teco-smi` |
| Docker | 可用（**不需要** compose 插件） | `docker info` |
| 磁盘 | 镜像约 18 GB + 权重约 2.5 GB | `df -h /var/lib/docker` |
| 网络 | 首次需访问 Zenodo 下载权重；pip 源用清华镜像 | `curl -sI https://zenodo.org` |
| 工具 | `git` `curl` `sha256sum` `md5sum` `python3` | — |

### 2.1 必须自备的两项产物

它们都是本地构建物，**仓库和 PyPI 里都没有**，必须先准备好：

**（1）SDAA 基础镜像**（沿用 ProteinMPNN 的成果）

```text
lasermpnn-sdaa321-loong:torch2.12.0-sdaa3.2.1-py312
Image ID: sha256:3e425da47a74a03b589f8e1c18a67dcb90c8bcbde977cc1df09fa01209b7a524
```

获取方式：从已有该镜像的机器导出/导入，或按
`ProteinMPNN-sdaa/ProteinMPNN/sdaa/docker/Dockerfile.sdaa` 重新构建。
该镜像没有 `RepoDigests`，无法用 `@sha256:` 引用，因此 `scripts/build-image.sh` 在构建前
**强制比对 Image ID**（不一致直接拒绝构建），达到等价的 digest 固定效果。
若你拿到的镜像 ID 不同，请把 `.env.sdaa` 里的 `BASE_IMAGE_ID` 改成实际值。

**（2）OpenMM 预编译前缀**（含 SDAA 平台，LoongArch 无 wheel）

```text
<OPENMM_INSTALL>/        进容器后挂到 /opt/openmm        （只读）
<OPENMM_SITE_PACKAGES>/  进容器后挂到 /opt/openmm-py     （只读）
```

本机使用 v2 构建（`OPENMM_BUILD_CPU_LIB=ON`、`USE_CPU_DIRECT=1`）：

```text
OPENMM_INSTALL       = <WORKSPACE>/Openmm-sdaa-v2/work-v2/install
OPENMM_SITE_PACKAGES = <WORKSPACE>/Openmm-sdaa-v2/work-v2/venv/lib/python3.12/site-packages
```

由 `Openmm-sdaa-v2/`（其 README 有完整重建步骤）产出，或直接从已有机器拷贝这两个目录。
挂载前请确认两点：

```bash
# 1) 平台插件齐全
ls <OPENMM_INSTALL>/lib/plugins        # 应含 libOpenMMCPU.so 与 libOpenMMSDAA.so
# 2) SDAA 插件通过 NEEDED 链接到 CPU 库（UseCpuDirect 通路的前提）
readelf -d <OPENMM_INSTALL>/lib/plugins/libOpenMMSDAA.so | grep NEEDED   # 应含 libOpenMMCPU.so
```

---

## 3. 建立项目根

```bash
# 1) 把仓库放到项目根的下一层
mkdir -p ~/ImmuneBuilder-sdaa && cd ~/ImmuneBuilder-sdaa
git clone <本仓库地址> ImmuneBuilder        # 或直接拷贝仓库目录

# 2) 一键建立布局（幂等，可重复执行；已存在的文件不会被覆盖）
./ImmuneBuilder/sdaa/scripts/bootstrap-workspace.sh
```

脚本会：链接 `scripts/ patches/ locks/ Dockerfile.sdaa compose.sdaa.yaml source.sdaa.lock`、
创建 `weights/ downloads/ work/ logs/ backups/ inputs/`、由 `.env.sdaa.example` 生成
`.env.sdaa`（自动填入当前 UID/GID、并按 `<项目根>/../Openmm-sdaa-v2` 推断 OpenMM 路径）、
生成 `.dockerignore` 与官方示例 `inputs/antibody.fasta`。

**然后检查 `.env.sdaa` 里的两个路径指向真实的 OpenMM 前缀**（这是唯一需要人工确认的地方）：

```bash
grep -E '^(HOST_UID|HOST_GID|IMMUNEBUILDER_|OPENMM_|OPENMM_CPU_THREADS)' .env.sdaa
```

以下命令全部在**项目根**执行。

---

## 4. 校验源码适配

仓库里的源码已经打好补丁，这一步只是复核：

```bash
python3 scripts/verify-patch.py        # 期望最后一行 PATCH_VERIFY_OK
```

它检查 4 个文件的内容特征、`device_utils.py` 是否存在、以及 `git` 工作树只有白名单内的改动。
适配提交会产生新的 HEAD，脚本此时改为确认「上游基线 commit 是本 HEAD 的祖先」。

改动清单（供 review 对照，`patches/sdaa-adaptation.diff` 是同一改动的统一 diff）：

| 文件 | 改动 |
| --- | --- |
| `ImmuneBuilder/{ABody,NanoBody,TCR}Builder2.py` | `"cuda" if torch.cuda.is_available() else "cpu"` → `resolve_device()`；新增 `--seed` |
| `ImmuneBuilder/refine.py` | 平台由硬编码 `Platform.getPlatformByName('CPU')` → `resolve_openmm_platform()`；解析提到 `if n_threads > 0` 之外并在两分支都传入；`Threads` 只对 CPU 平台传递 |
| `ImmuneBuilder/device_utils.py`（新增） | `resolve_device` / `resolve_openmm_platform` / `seed_all` |

> `patches/apply-sdaa-patch.py` 用于给**纯净的上游克隆**重新打补丁，或配合
> `git checkout` 做回退演练；正常复现流程不需要它。

---

## 5. 构建镜像并启动容器

```bash
./scripts/download-deps.sh            # tecofft RPM、x86 HMMER + 运行库、静态 qemu
./scripts/prewarm-weights-sdaa.sh     # 首次：预热模型权重（详见第 7 节）
./scripts/build-image.sh              # 先校验基础镜像 Image ID，再 docker build --pull=false
./scripts/run-container.sh            # 创建/复用容器，映射 /dev/tcaicard0
```

镜像内做了三件补不齐就跑不通的事：

1. 安装 **tecofft RPM**（`libOpenMMSDAA.so` 的硬依赖，缺它 SDAA 插件会静默加载失败）；
2. 新建 venv 时**复制基础镜像的 `sdaa321-base-runtime.pth`**，以复用 `/home/py312` 下的
   torch / torch_sdaa / biopython；
3. 装 **HMMER**：LoongArch 无法原生编译 HMMER 3.4（强制 SSE/NEON/VMX），
   改用 Debian amd64 版 + 静态 `qemu-x86_64`，并放一个 `hmmscan` wrapper 到 `/usr/local/bin`。

### 5.1 挂载策略：源码只读、权重只读、只有输出目录可写

| 宿主 | 容器 | 模式 | 说明 |
| --- | --- | --- | --- |
| `<项目根>` | `/workspace/repro` | **ro** | 源码、脚本、`inputs/`、`downloads/` |
| `<项目根>/work` | `/workspace/repro/work` | **rw** | 唯一可写目录（覆盖上面那层只读） |
| `<项目根>/weights` | `/weights` | **ro** | 模型权重（镜像内 `trained_model` 软链到这里） |
| `OPENMM_INSTALL` | `/opt/openmm` | ro | 复用的 OpenMM 构建 |
| `OPENMM_SITE_PACKAGES` | `/opt/openmm-py` | ro | 同上（python 包部分） |
| `<项目根>/work/sdaa-cache` | `/workspace/sdaa-cache` | rw | SDAA JIT 缓存（`TMPDIR`） |

容器工作目录设为 **`/workspace/repro/work`**（可写），这一点不能省：
上游 `refine.py` 会把 `side_chain_fix_tmp_<rand>.pdb` 写到**进程当前目录**，
项目根一旦只读，CWD 还留在那里就会精修失败。

自检（创建容器后跑一次，四条都应有预期结果）：

```bash
docker exec immunebuilder-sdaa sh -c 'pwd'                                     # /workspace/repro/work
docker exec immunebuilder-sdaa sh -c 'touch /workspace/repro/x'                # 应: Read-only file system
docker exec immunebuilder-sdaa sh -c 'touch /weights/x'                        # 应: Read-only file system
docker exec immunebuilder-sdaa sh -c 'touch /workspace/repro/work/x && rm -f /workspace/repro/work/x'  # 成功
```

> 挂载模式**无法在既有容器上修改**。`run-container.sh` 检测到容器是用旧挂载创建时只会**告警**，
> 不会自动删容器。要生效需重建（容器本身无状态，产物都在宿主 `work/` 里）：
> `docker rm -f immunebuilder-sdaa && ./scripts/run-container.sh`

`run-container.sh` 里另外两个容易踩的点：`LD_LIBRARY_PATH` 必须同时含 `/opt/openmm/lib` 与
`/opt/openmm/lib/plugins`；换 OpenMM 构建时会自动清空 SDAA JIT 缓存（旧 fatbin 会命中错误内核）。

---

## 6. 环境自检

```bash
./scripts/verify-environment-sdaa.sh
```

期望最后一行 `"ok": true`，且关键项为：

```text
python                     3.12.13
torch                      2.12.0
torch_sdaa_version         3.2.1
torch_sdaa_is_available    true
openmm                     8.6.0.dev-Unknown
openmm_platforms           ["Reference", "CPU", "SDAA"]     ← 必须三个都有
resolve_device(sdaa)       "sdaa"
resolve_openmm_platform()  "CPU"                            ← 默认精修平台
```

---

## 7. 权重：官方下载机制 + 一次性预热

官方仓库里的 `ImmuneBuilder/trained_model/*` 是 **0 字节占位文件**，真正的权重在
Zenodo。官方代码在构造模型时检查 `are_weights_ready()`，不满足就调用
`ImmuneBuilder.util.download_file()` 现场下载。本适配沿用这个机制，只把落盘位置改到宿主机：

```text
镜像内  .../site-packages/ImmuneBuilder/trained_model  ->  /weights（符号链接）
容器    -v <项目根>/weights:/weights:ro        ← 正常容器：只读
预热    -v <项目根>/weights:/weights:rw        ← 临时容器：官方下载写盘
```

因为正常容器是**只读**挂载（第 5.1 节），官方下载写不进去，所以首次用**预热脚本**
跑一次；它起一个临时容器（`--rm`，只读挂载换成可写），触发官方下载，权重直接落到宿主
`weights/`，之后正常容器全程只读、可离线：

```bash
./scripts/prewarm-weights-sdaa.sh                # 默认 12 份（约 1.96 GB）
./scripts/prewarm-weights-sdaa.sh --original     # 外加旧版 TCRBuilder2 权重（共 16 份）
```

预热脚本的特性：幂等（已就绪的权重不会重复下载）、用 CPU 设备（不占 SDAA 卡）、
结束时打印文件数与 `are_weights_ready()` 检查结果；**不会碰现有容器和镜像**。

### 7.1 实测：Zenodo 链路的吞吐是瓶颈

| 实测项 | 结果 |
| --- | --- |
| Zenodo DNS | 解析到 **IPv6** 地址（`2001:1458:...`），无 IPv4 记录 |
| Zenodo 首字节延迟 | `0.69 s`（连通性正常） |
| Zenodo 下载吞吐 | 约 **0.07 MB/s**（61 MB 的 `antibody_model_1` 跑 10 分钟只完成 40 MB） |
| 同期清华 PyPI 吞吐 | `9.4 MB/s`（说明本机网络本身没问题） |
| 建立 CUDA/SDAA 基线时 | 官方机制 + 4 路并发约 `0.7 MB/s`，2.5 GB 约 1 小时 |

结论：**机制本身没问题，瓶颈在 Zenodo 到目标机的国际链路。**
默认 12 个权重（约 1.96 GB）在 0.07 MB/s 下要数小时，请据此安排。
`download_file()` 是单连接、`requests` 会自动跟随 `?download=1` 的 301 跳转。

### 7.2 加速做法（不改变官方机制）：拷贝 + 官方 md5 校验

最省时的做法是从**已经有权重的机器**把 `weights/` 整个拷贝过来，然后按 Zenodo 官方
md5 逐份校验。下表就是 Zenodo 元数据（`md5 / 字节数 / 文件名 / 下载 URL`）：

```text
b63013452ad420b9bb3eeb6968216a6e    61050011  antibody_model_1  https://zenodo.org/api/records/7258553/files/antibody_model_1/content
e640916c5f1f8d5d8b2090688b6da64a   214267291  antibody_model_2  https://zenodo.org/api/records/7258553/files/antibody_model_2/content
4b87b871b195ad4c53d9ee557bd2a47d   214267291  antibody_model_3  https://zenodo.org/api/records/7258553/files/antibody_model_3/content
f731fe2474f0d4533a27aa9412844d47   214267291  antibody_model_4  https://zenodo.org/api/records/7258553/files/antibody_model_4/content
4591075f467ca9f76a37a5d1d3cfe591    61050011  nanobody_model_1  https://zenodo.org/api/records/7258553/files/nanobody_model_1/content
620fc916720bc7068cd18c7afa1aea8d   214267291  nanobody_model_2  https://zenodo.org/api/records/7258553/files/nanobody_model_2/content
f1ef0a66a54efb9d14ddbb97fdc30785   214267291  nanobody_model_3  https://zenodo.org/api/records/7258553/files/nanobody_model_3/content
a7e14a33f4c00e96df896120e9cf8522   214267291  nanobody_model_4  https://zenodo.org/api/records/7258553/files/nanobody_model_4/content
087a9354d7417ec436367ff6c72135a3    61050011  tcr2_model_1      https://zenodo.org/api/records/7258553/files/tcr_model_1/content
9ab1968b33086144596fe73b27631c9b    61050011  tcr2_model_2      https://zenodo.org/api/records/7258553/files/tcr_model_2/content
9e2adeaf379308f546da963270d27542   214267291  tcr2_model_3      https://zenodo.org/api/records/7258553/files/tcr_model_3/content
4f3a07356d366ec52fc301d6f0f21f35   214267291  tcr2_model_4      https://zenodo.org/api/records/7258553/files/tcr_model_4/content
3656f4bdc4d396ccfcf150e026e1b99d   214262113  tcr_model_1       https://zenodo.org/api/records/10892159/files/tcr_model_1/content
cff7b435d3ff11622a5c56805eaa5e7f   214262113  tcr_model_2       https://zenodo.org/api/records/10892159/files/tcr_model_2/content
06884c5808352b968116bf25d48005d2    61044833  tcr_model_3       https://zenodo.org/api/records/10892159/files/tcr_model_3/content
bb6dd0dd9a5160e80c9abfe1f5ac9eac    61044833  tcr_model_4       https://zenodo.org/api/records/10892159/files/tcr_model_4/content
```

共 16 个文件、`2,508,952,264` 字节。默认流程只需要 12 个
（antibody 4 + nanobody 4 + **TCRBuilder2+** 4 = `tcr_model_*`）；
`tcr2_model_*` 只在 `TCRBuilder2 --original_weights` 时用到。

校验（把上面的 `md5 + 文件名` 两列存成清单即可）：

```bash
cd weights
md5sum -c <清单>          # 期望 16 行全部 OK
```

> 注意名字容易混：**旧版** TCRBuilder2 权重在 Zenodo 上叫 `tcr_model_N`
> （record 7258553），落到本地叫 `tcr2_model_N`；而 `tcr_model_N`
> （record 10892159）才是现在默认的 TCRBuilder2+ 权重。

---

## 8. 跑通官方推理示例

### 8.1 Python API（三个用例，一次跑完）

```bash
# 默认路径：神经网络走 SDAA，精修走 CPU
./scripts/run-smoke-test-sdaa.sh sdaa all 20260921

# 纯 CPU 基线对照 / 只跑一个用例
./scripts/run-smoke-test-sdaa.sh cpu all 20260921
./scripts/run-smoke-test-sdaa.sh sdaa single
```

通过标志：日志末尾出现 `SMOKE_PASSED`（并打印 `failures: []`）。
产物：`work/smoke_test_sdaa/sdaa/{antibody,nanobody,tcr}.pdb` 与 `summary.json`。

### 8.2 官方 CLI（上游 README 里给用户的用法）

```bash
./scripts/run-cli-test-sdaa.sh sdaa CPU
```

依次执行 `ABodyBuilder2` / `NanoBodyBuilder2` / `TCRBuilder2`（输入序列由脚本生成）。

通过标志：

```text
CLI_TEST_DONE
NO_REFINE_FAILURE      ← 没有出现 FAILED TO REFINE
```

### 8.3 手工单次（等价于上游 README 命令）

```bash
mkdir -p work/my_run                            # 输出目录必须在 work/ 下（项目根只读）
docker exec \
  -e IMMUNEBUILDER_DEVICE=sdaa \
  -e IMMUNEBUILDER_OPENMM_PLATFORM=CPU \
  immunebuilder-sdaa \
  ABodyBuilder2 --fasta_file /workspace/repro/inputs/antibody.fasta \
                --output /workspace/repro/work/my_run/antibody.pdb -v
```

（`ABodyBuilder2 --help` 可看全部参数：`-H/-L` 直接给序列、`-n` 编号方案、
`-u` 跳过侧链键长检查、`--seed` 固定随机种子。注意 `--output` 必须落在
`/workspace/repro/work/` 之内 —— 项目根是只读的。）

Python API 等价写法：

```python
from ImmuneBuilder import ABodyBuilder2
sequences = {
  'H': 'EVQLVESGGGVVQPGGSLRLSCAASGFTFNSYGMHWVRQAPGKGLEWVAFIRYDGGNKYYADSVKGRFTISRDNSKNTLYLQMKSLRAEDTAVYYCANLKDSRYSGSYYDYWGQGTLVTVS',
  'L': 'VIWMTQSPSSLSASVGDRVTITCQASQDIRFYLNWYQQKPGKAPKLLISDASNMETGVPSRFSGSGSGTDFTFTISSLQPEDIATYYCQQYDNLPFTFGPGTKVDFK'}
ABodyBuilder2().predict(sequences).save("antibody.pdb")
```

---

## 9. 验收判据

| # | 检查项 | 命令 | 通过标志 |
| --- | --- | --- | --- |
| 1 | 源码适配就位 | `python3 scripts/verify-patch.py` | `PATCH_VERIFY_OK` |
| 2 | 运行时环境 | `./scripts/verify-environment-sdaa.sh` | `"ok": true`，平台含 `CPU`+`SDAA` |
| 3 | Python API 三用例 | `./scripts/run-smoke-test-sdaa.sh sdaa all` | `SMOKE_PASSED` |
| 4 | 官方 CLI 三入口 | `./scripts/run-cli-test-sdaa.sh sdaa CPU` | `CLI_TEST_DONE` + `NO_REFINE_FAILURE` |
| 5 | 结构有效性 | 上述产物 | 键长、顺反肽键、手性、原子重叠四项通过 |
| 6 | SDAA 真的被用上 | `summary.json` / 日志 | `device: sdaa` |
| 7 | 挂载策略正确 | 第 5.1 节的 4 条 `docker exec` 自检 | 项目根/权重只读、`work/` 可写、CWD 在 `work/` |

参考基线（本机实测，`device=sdaa` + `CPU` 精修 32 线程；取多次 `sdaa` 冒烟的范围）：

| 用例 | 原子数 | predict（SDAA） | save_and_refine（CPU 32 线程） | total |
| --- | ---: | ---: | ---: | ---: |
| antibody | 3483 | 10.6 – 10.9 s | 11.6 – 33.2 s | 25.4 – 46.9 s |
| nanobody | 1850 | 4.4 – 4.5 s | 3.7 – 4.1 s | 10.8 – 11.2 s |
| tcr | 3396 | 9.4 – 9.9 s | 8.9 – 14.4 s | 20.8 – 26.7 s |

CLI 验收三个入口：antibody 33–37 s、nanobody 23–24 s、tcr 33–35 s。

> 区间偏宽是因为测试机是共享机器：波动几乎全部落在精修阶段（antibody 最明显，
> 11.6 → 33.2 s）。用本流程只判断「能不能跑通」；正式的性能对比数据见
> `SDAA_ADAPTATION.md`（同节点、三次取中位数）。

注意两点：

* 输出**不是字节可复现**的（精修的能量最小化存在浮点不确定性）。
  不要用 sha256 比对两次运行，应比对 Cα-RMSD；跨平台 CA-RMSD ≤ 0.32 Å，
  与同配置自身重复运行的差异同量级。
* 三个用例中只有 antibody 用的是上游 README 的示例序列，
  nanobody / TCR 的序列由脚本自带（均为合法序列）。

---

## 10. 可选：把精修也切到 SDAA

默认精修走 CPU，因为实测最大体系（antibody，3483 原子）上 SDAA 相对龙芯 CPU 32 线程
没有优势。确需开启时**必须同时给出 SDAA 专有属性**，否则性能明显劣化：

```bash
./scripts/run-smoke-test-sdaa.sh sdaa-gpu
./scripts/run-refine-bench.sh quick      # 约 40 s
./scripts/run-refine-bench.sh full       # n=6, tries=3，接近真实
```

等价的环境变量：

```text
IMMUNEBUILDER_OPENMM_PLATFORM=SDAA
IMMUNEBUILDER_OPENMM_PROPERTIES=UseCpuDirect=true,Precision=mixed
```

`UseCpuDirect` 是唯一有效的大杠杆；`Precision=mixed` 单独开启无收益。
这些属性**只被 SDAA 平台接受**：平台切成 CPU / Reference / CUDA 时该变量必须留空，
否则 OpenMM 抛 `Illegal property name`。

---

## 11. 环境变量速查

| 变量 | 取值 | 作用 |
| --- | --- | --- |
| `IMMUNEBUILDER_DEVICE` | `auto` \| `cpu` \| `cuda[:N]` \| `sdaa[:N]` | 神经网络设备；`auto` = CUDA → SDAA → CPU |
| `IMMUNEBUILDER_OPENMM_PLATFORM` | `auto` \| `CPU` \| `SDAA` \| `Reference` | 精修平台；`auto` = CUDA → CPU → SDAA → Reference |
| `IMMUNEBUILDER_OPENMM_PROPERTIES` | 如 `UseCpuDirect=true,Precision=mixed` | 仅 SDAA 平台可用，其他平台必须留空 |
| `OPENMM_CPU_THREADS` | 默认 `32` | CPU 精修线程数；本机 32 优于 64/128 |
| `HOST_UID` / `HOST_GID` | 由 bootstrap 自动填 | 容器内用户映射，避免 root 属主产物 |
| `BENCH_DIR` 等脚本内路径 | 固定 `/workspace/repro/...` | 由 `run-*.sh` 注入，无需设置 |

各 `run-*.sh` / `verify-*.sh` 脚本都会读取**项目根**的 `.env.sdaa`（容器名、默认平台等），
所以改 `CONTAINER_NAME` 之类的配置只需改一处；直接 `export` 的同名变量优先于文件里的值。

显式指定**不可用**的设备/平台会立即报错，不会静默回退到 CPU —— 这是相对上游的关键改进。

---

## 12. 常见故障

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| 脚本报找不到 `.env.sdaa` / `Dockerfile.sdaa`，或产物落到奇怪的位置 | 没有在**项目根**执行，或 `sdaa/` 没被链接到项目根 | 回到项目根执行；重跑 `bootstrap-workspace.sh` |
| `python3 scripts/verify-patch.py` 报无法定位项目根 | 仓库不在项目根的下一层 | 按第 1 节的布局摆放 |
| 程序正常结束但 SDAA 完全没被使用 | `torch.cuda.is_available()` 在 SDAA 环境恒为 `False`（上游会静默回落 CPU） | 确认补丁已应用（判据 1）；`IMMUNEBUILDER_DEVICE=sdaa` 或 `auto` |
| 精修反复失败，只在日志里出现 `FAILED TO REFINE` | 异常被 `refine_once` 的 `try/except` 吞掉；常见根因是 `Illegal property name: Threads` | 确认 `Threads` 只对 CPU 平台传递；确认 `IMMUNEBUILDER_OPENMM_PROPERTIES` 非空时平台确实是 SDAA |
| `There is no registered Platform called "CPU"` | 所用 OpenMM 构建是 `OPENMM_BUILD_CPU_LIB=OFF` | 换用 v2 构建；见 §2.1 的 `readelf -d` 检查 |
| 只有 `Reference` 平台，SDAA 插件没加载 | 缺 `libtecofft.so`（tecofft RPM 未装或与 OpenMM 构建不配套） | 确认 `scripts/download-deps.sh` 已运行、镜像里有 `/opt/tecoai/lib64/libtecofft.so` |
| 权重下载失败 / `Permission denied` 写 `/weights` | 正常容器是**只读**挂载，官方下载写不进去 | 用 `./scripts/prewarm-weights-sdaa.sh` 预热（§7）；不要在正常容器里下载 |
| 输出写到项目根报 `Read-only file system` | 项目根只读，只有 `work/` 可写 | 把 `--output` / `OUTPUT_DIR` 放到 `/workspace/repro/work/` 下；不要往源码目录写 |
| 精修写临时文件失败 / `side_chain_fix_tmp_*.pdb` 报错 | 上游 `refine.py` 往**进程当前目录**写临时文件，而 CWD 不在可写目录 | 确认容器 `WorkingDir=/workspace/repro/work`（`docker inspect <name> --format '{{.Config.WorkingDir}}'`） |
| 改了挂载却"没生效" | 挂载模式无法在既有容器上修改，脚本只告警不删容器 | `docker rm -f immunebuilder-sdaa && ./scripts/run-container.sh`（容器无状态，产物都在宿主 `work/`） |
| 下载中断后重跑报 `not downloaded or corrupted` | `are_weights_ready()` 只判断「文件存在且非空」，**残缺文件会被当成有效** | 删掉 `weights/` 里尺寸明显偏小的文件再重跑；按 §7.2 的 md5 清单核对 |
| 下载极慢（数小时） | Zenodo 只有 IPv6 出口、国际链路吞吐低 | 见 §7.1 实测与 §7.2 的拷贝 + md5 校验做法 |
| 换 OpenMM 构建后行为异常 | 命中旧 SDAA JIT fatbin | 删 `work/sdaa-cache`（`run-container.sh` 已能自动检测） |
| `hmmscan` 报错或找不到 | qemu wrapper 未生效 | `docker exec immunebuilder-sdaa hmmscan -h` 应打印 HMMER 3.4 头 |
| 镜像构建时 pip 全部被阻断 | 基础镜像内置 `PIP_NO_INDEX=1` | Dockerfile 已显式 `PIP_NO_INDEX=0` + 清华源 |

---

## 13. 回退与清理

```bash
# 源码回退到上游（补丁只碰 4 个文件 + 1 个新增文件）
git -C ImmuneBuilder checkout -- ImmuneBuilder/ABodyBuilder2.py \
    ImmuneBuilder/NanoBodyBuilder2.py ImmuneBuilder/TCRBuilder2.py ImmuneBuilder/refine.py
rm -f ImmuneBuilder/ImmuneBuilder/device_utils.py

# 用打补丁前的自动备份回退（在项目根执行过 apply-sdaa-patch.py 时才有）
ls backups/patches-before-apply-*/

# 容器 / 镜像
docker rm -f immunebuilder-sdaa
docker rmi immunebuilder-sdaa:torch2.12.0-sdaa3.2.1-py312

# 只删运行产物，保留镜像与权重
rm -rf work logs
```

上游基线 commit：`0df4e2ad82a1aa60f37ea9dae335d1198159ef78`（version 1.2）。
`weights/` 可长期保留复用；删除后下次运行会重新下载。
