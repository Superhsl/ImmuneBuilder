#!/usr/bin/env bash
# 在 ImmuneBuilder SDAA 容器内运行冒烟测试。
#
# 用法：
#   ./scripts/run-smoke-test-sdaa.sh sdaa                 # 设备=sdaa，精修=CPU（默认，推荐）
#   ./scripts/run-smoke-test-sdaa.sh sdaa-gpu             # 设备=sdaa，精修=SDAA（可选加速路径）
#   ./scripts/run-smoke-test-sdaa.sh cpu                  # 设备=cpu，精修=CPU（纯 CPU 基线）
#   ./scripts/run-smoke-test-sdaa.sh sdaa single          # 只跑 antibody 一个官方示例
#   ./scripts/run-smoke-test-sdaa.sh sdaa all 12345       # 指定随机种子
#
# 产出：work/smoke_test_sdaa/<mode>/<case>.pdb 与 summary.json
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT}"

# 读取项目根的 .env.sdaa（容器名、默认平台等）；已导出的环境变量优先
if [[ -f ./.env.sdaa ]]; then
    # shellcheck disable=SC1091
    set -a; source ./.env.sdaa; set +a
fi

MODE="${1:-sdaa}"          # sdaa | sdaa-gpu | cpu
SCOPE="${2:-all}"          # all | single
SEED="${3:-}"

case "${MODE}" in
    sdaa)
        expect_device="sdaa"
        device="sdaa"
        platform="${IMMUNEBUILDER_OPENMM_PLATFORM:-CPU}"
        props="${IMMUNEBUILDER_OPENMM_PROPERTIES:-}"
        refine_threads="${SDAA_REFINE_THREADS:-32}"
        ;;
    sdaa-gpu)
        # 可选：精修也走 SDAA。需要 SDAA 专有属性，否则性能显著劣化
        expect_device="sdaa"
        device="sdaa"
        platform="SDAA"
        props="${IMMUNEBUILDER_OPENMM_PROPERTIES:-UseCpuDirect=true,Precision=mixed}"
        refine_threads="${SDAA_REFINE_THREADS:--1}"
        ;;
    cpu)
        expect_device="cpu"
        device="cpu"
        platform="CPU"
        props=""
        refine_threads="${CPU_REFINE_THREADS:-32}"
        ;;
    *)
        echo "用法: $0 [sdaa|sdaa-gpu|cpu] [all|single|逗号分隔用例] [seed]" >&2
        exit 2
        ;;
esac

case "${SCOPE}" in
    all)    cases="antibody,nanobody,tcr" ;;
    single) cases="antibody" ;;
    *)      cases="${SCOPE}" ;;
esac

OUTDIR="work/smoke_test_sdaa/${MODE}"
mkdir -p "${OUTDIR}"
LOG="${OUTDIR}/smoke.log"

CONTAINER="${CONTAINER_NAME:-immunebuilder-sdaa}"
if ! docker container inspect "${CONTAINER}" >/dev/null 2>&1; then
    echo "ERROR: 容器 ${CONTAINER} 不存在，请先运行 scripts/build-image.sh 与 scripts/run-container.sh" >&2
    exit 1
fi

extra_seed=()
[[ -n "${SEED}" ]] && extra_seed=(--seed "${SEED}")

echo "==> 冒烟测试 mode=${MODE} cases=${cases} seed=${SEED:-none}"
set -x
docker exec \
    -e "IMMUNEBUILDER_DEVICE=${device}" \
    -e "IMMUNEBUILDER_OPENMM_PLATFORM=${platform}" \
    -e "IMMUNEBUILDER_OPENMM_PROPERTIES=${props:-}" \
    -e "EXPECT_DEVICE=${expect_device}" \
    -e "REFINE_THREADS=${refine_threads}" \
    -e "OUTPUT_DIR=/workspace/repro/${OUTDIR}" \
    "${CONTAINER}" \
    python /workspace/repro/scripts/smoke_test_sdaa.py \
        --cases "${cases}" \
        --summary "/workspace/repro/${OUTDIR}/summary.json" \
        "${extra_seed[@]}" 2>&1 | tee "${LOG}"
set +x

echo
echo "==> 产物"
ls -la "${OUTDIR}"
echo
echo "==> 判定"
grep -q 'SMOKE_PASSED' "${LOG}" || { echo "冒烟测试未通过，详见 ${LOG}" >&2; exit 1; }
echo "冒烟测试通过：${OUTDIR}"
