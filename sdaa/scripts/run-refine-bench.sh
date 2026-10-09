#!/usr/bin/env bash
# ImmuneBuilder 精修平台对比：Reference / SDAA / SDAA+UseCpuDirect / CPU(多线程)
#
# 用途：在 OpenMM v2 构建下对比不同精修平台的耗时与收敛性。
#   ImmuneBuilder 只暴露"选哪个平台"，不暴露平台属性；
#   UseCpuDirect 这类属性通过 scripts/refine-platform-bench.py 在 OpenMM 层注入。
#
# 用法：
#   ./scripts/run-refine-bench.sh                 # 默认 quick（n=1, tries=1）
#   ./scripts/run-refine-bench.sh full            # 更接近真实（n=6, tries=3）
#   CPU_THREADS=32 ./scripts/run-refine-bench.sh full
#
# 产物：work/refine_bench/<tag>.pdb 与 bench-summary.json
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT}"

# 读取项目根的 .env.sdaa（容器名、构建标识等）；已导出的环境变量优先
if [[ -f ./.env.sdaa ]]; then
    # shellcheck disable=SC1091
    set -a; source ./.env.sdaa; set +a
fi

MODE="${1:-quick}"
CPU_THREADS="${CPU_THREADS:-64}"
CONTAINER="${CONTAINER_NAME:-immunebuilder-sdaa}"
BENCH_DIR="/workspace/repro/work/refine_bench"
INPUT="${BENCH_DIR}/antibody_unrefined.pdb"

if [[ "${MODE}" == "full" ]]; then
    N=6; TRIES=3
else
    N=1; TRIES=1
fi

if ! docker container inspect "${CONTAINER}" >/dev/null 2>&1; then
    echo "ERROR: 容器 ${CONTAINER} 不存在，请先 scripts/build-image.sh + scripts/run-container.sh" >&2
    exit 1
fi

mkdir -p work/refine_bench

# 1) 准备未精修输入（跳过精修，只跑神经网络，约 15-70s）
if [[ ! -s "work/refine_bench/antibody_unrefined.pdb" ]]; then
    echo "==> 生成未精修输入（--no_sidechain_bond_check，跳过精修）"
    docker exec "${CONTAINER}" ABodyBuilder2 \
        -f /workspace/repro/inputs/antibody.fasta \
        -o "${INPUT}" -u >/dev/null 2>&1
fi
echo "==> 输入: work/refine_bench/antibody_unrefined.pdb ($(grep -c '^ATOM' work/refine_bench/antibody_unrefined.pdb) atoms)"
echo "==> 模式: ${MODE} (n=${N}, tries=${TRIES})   构建: $(grep OPENMM_VERSION_TAG .env.sdaa | cut -d= -f2)"
echo

RESULTS_FILE="work/refine_bench/${MODE}-results.jsonl"
: > "${RESULTS_FILE}"

run_case() {
    local label="$1"; shift
    echo "===== ${label} ====="
    local out
    out="$(docker exec "${CONTAINER}" python /workspace/repro/scripts/refine-platform-bench.py \
        --n "${N}" --tries "${TRIES}" \
        --input "${INPUT}" "$@" 2>&1 | grep -E 'REFINE_BENCH_RESULT=' || true)"
    if [[ -z "${out}" ]]; then
        echo "  (无结果，命令失败)"
        return 0
    fi
    local json="${out#REFINE_BENCH_RESULT=}"
    printf '%s\n' "${json}" >> "${RESULTS_FILE}"
    echo "${json}" | python3 -c "
import json, sys
d = json.load(sys.stdin)
print('  platform=%-10s props=%-30s converged=%-5s seconds=%s' % (
    d['platform'], d['injected_properties'], d['converged'], d['seconds']))
" || true
}

run_case "Reference（单线程参考实现）"   --platform Reference --output "${BENCH_DIR}/${MODE}_ref.pdb"
run_case "SDAA（默认）"                  --platform SDAA --output "${BENCH_DIR}/${MODE}_sdaa.pdb"
run_case "SDAA + UseCpuDirect=true"      --platform SDAA --sdaa-props UseCpuDirect=true --output "${BENCH_DIR}/${MODE}_sdaa_cpudirect.pdb"
run_case "CPU + ${CPU_THREADS} 线程"      --platform CPU --threads "${CPU_THREADS}" --output "${BENCH_DIR}/${MODE}_cpu${CPU_THREADS}.pdb"

echo
echo "==> 汇总"
python3 - "${RESULTS_FILE}" <<'PY'
import json
import sys

path = sys.argv[1]
rows = []
with open(path) as fh:
    for line in fh:
        line = line.strip()
        if line:
            rows.append(json.loads(line))
rows.sort(key=lambda d: d.get("seconds") or 1e9)
print("  %-12s %-32s %-10s %s" % ("platform", "properties", "converged", "seconds"))
for d in rows:
    print("  %-12s %-32s %-10s %s" % (
        d["platform"], json.dumps(d["injected_properties"]), d["converged"], d["seconds"]))
with open("work/refine_bench/bench-summary.json", "w") as fh:
    json.dump(rows, fh, indent=2, sort_keys=True)
print()
print("  明细已写入 work/refine_bench/bench-summary.json")
PY
echo
echo "==> 产物"
ls -la work/refine_bench/*.pdb
