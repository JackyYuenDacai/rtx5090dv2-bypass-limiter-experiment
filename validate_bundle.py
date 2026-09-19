"""Offline patch and mocked control-flow checks. Never accesses GPU devices."""
from pathlib import Path
import json
import shutil
import subprocess
import tempfile

from build_bundle import OUT, ROOT, PERF, GR, HEADER, original


def run(args, **kwargs):
    return subprocess.run(args, check=True, text=True, capture_output=True, **kwargs)


PREAMBLE = r'''
#include <assert.h>
#include <stddef.h>
#include <stdio.h>
#include <stdint.h>
#include <stdarg.h>
typedef uint32_t NvU32;
typedef int NvBool;
typedef int NV_STATUS;
#define NV_TRUE 1
#define NV_FALSE 0
#define NV_OK 0
#define NV_ERR_INVALID_REQUEST 1
#define NV_ERR_INVALID_POINTER 2
#define NV_INLINE inline
#define LEVEL_INFO 0
#define LEVEL_ERROR 1
#define LEVEL_WARNING 2
#define NV0080_CTRL_CMD_INTERNAL_PERF_CUDA_LIMIT_SET_CONTROL 0x802009u
#define NV0080_CTRL_CMD_INTERNAL_PERF_CUDA_LIMIT_DISABLE 0x802004u
#define PDB_PROP_GPU_CLKS_IN_TEGRA_SOC 1
typedef struct OBJGPU OBJGPU;
typedef struct RM_API RM_API;
struct RM_API { NV_STATUS (*Control)(RM_API *, NvU32, NvU32, NvU32, void *, size_t); };
struct OBJGPU {
    struct { NvU32 PCIDeviceID; } idInfo;
    int isVirtual, tegra;
    NvU32 hInternalClient, hInternalDevice;
    RM_API api;
    NvBool (*getProperty)(OBJGPU *, int);
};
typedef struct Device {
    NvU32 nCudaLimitRefCnt;
    OBJGPU *gpu;
} Device;
typedef struct { NvBool bCudaLimit; } NV0080_CTRL_PERF_CUDA_LIMIT_CONTROL_PARAMS;
#define IS_VIRTUAL(g) ((g)->isVirtual)
#define DRF_VAL(a,b,c,d) (((d) >> 16) & 0xffffu)
#define GPU_GET_PHYSICAL_RMAPI(g) (&(g)->api)
#define GPU_RES_GET_GPU(d) ((d)->gpu)
#define RES_GET_CLIENT_HANDLE(d) 0x100u
#define RES_GET_HANDLE(d) 0x101u
#define NV_ASSERT_OR_RETURN(cond, err) do { if (!(cond)) return (err); } while (0)
#define NV_CHECK_OR_RETURN(level, cond, err) NV_ASSERT_OR_RETURN(cond, err)
#define NV_CHECK_OK_OR_RETURN(level, expr) do { int st = (expr); if (st) return st; } while (0)
static unsigned logs;
static void log_message(int level, const char *fmt, ...) { (void)level; (void)fmt; logs++; }
#define NV_PRINTF log_message
static unsigned calls;
static NvU32 last_cmd;
static int last_active, rpc_status;
static int control(RM_API *a, NvU32 c, NvU32 h, NvU32 cmd, void *params, size_t sz) {
    (void)a; (void)c; (void)h; (void)sz;
    calls++; last_cmd = cmd;
    if (params) last_active = ((NV0080_CTRL_PERF_CUDA_LIMIT_CONTROL_PARAMS *)params)->bCudaLimit;
    return rpc_status;
}
static int property(OBJGPU *g, int id) { (void)id; return g->tegra; }
'''

TESTS = r'''
static int request(Device *d, int active) {
    NV0080_CTRL_PERF_CUDA_LIMIT_CONTROL_PARAMS p = { active };
    return deviceCtrlCmdKPerfCudaLimitSetControl_IMPL(d, &p);
}
static void test_device(unsigned pci, int virt, int tegra) {
    OBJGPU g = { .idInfo = { pci }, .isVirtual = virt, .tegra = tegra,
                 .api = { control }, .getProperty = property };
    Device d = {0, &g};
    int skip = NV_DV2_SKIP_HOST_CUDA_LIMIT && ((pci >> 16) == 0x2b8c) && !virt;
    calls = logs = 0; rpc_status = 0;
    assert(dv2LimitExperimentMatchesGpu(&g) == (((pci >> 16) == 0x2b8c) && !virt));
    assert(request(&d, 1) == NV_OK);
    if (virt || tegra) {
        assert(d.nCudaLimitRefCnt == 0 && calls == 0);
        assert(request(&d, 0) == NV_OK);
        assert(deviceKPerfCudaLimitCliDisable(&d, &g) == NV_OK);
        return;
    }
    assert(d.nCudaLimitRefCnt == 1 && calls == (unsigned)!skip);
    if (!skip) assert(last_active == 1);
    assert(request(&d, 1) == NV_OK);
    assert(d.nCudaLimitRefCnt == 2 && calls == (unsigned)!skip);
    assert(request(&d, 0) == NV_OK);
    assert(d.nCudaLimitRefCnt == 1 && calls == (unsigned)!skip);
    assert(request(&d, 0) == NV_OK);
    assert(d.nCudaLimitRefCnt == 0 && calls == 2u * !skip);
    if (!skip) assert(last_active == 0);
    assert(request(&d, 0) == NV_ERR_INVALID_REQUEST);
    assert(calls == 2u * !skip);
    calls = 0;
    assert(request(&d, 1) == NV_OK);
    assert(request(&d, 1) == NV_OK);
    assert(deviceKPerfCudaLimitCliDisable(&d, &g) == NV_OK);
    assert(d.nCudaLimitRefCnt == 0 && calls == 2u * !skip);
    if (!skip) assert(last_cmd == NV0080_CTRL_CMD_INTERNAL_PERF_CUDA_LIMIT_DISABLE);
    assert(deviceKPerfCudaLimitCliDisable(&d, &g) == NV_OK);
    assert(calls == 2u * !skip);

    /* Multiple clients must retain independent balances. */
    Device e = {0, &g}; calls = 0;
    assert(request(&d, 1) == NV_OK && request(&e, 1) == NV_OK);
    assert(deviceKPerfCudaLimitCliDisable(&d, &g) == NV_OK);
    assert(e.nCudaLimitRefCnt == 1);
    assert(request(&e, 0) == NV_OK);
    assert(calls == 4u * !skip);

    /* Forwarded error handling must be unchanged; existing code keeps its count. */
    calls = 0; rpc_status = 17;
    assert(request(&d, 1) == (skip ? NV_OK : 17));
    assert(d.nCudaLimitRefCnt == 1);
    assert(deviceKPerfCudaLimitCliDisable(&d, &g) == (skip ? NV_OK : 17));
    assert(d.nCudaLimitRefCnt == (skip ? 0u : 1u));
    rpc_status = 0;
    assert(deviceKPerfCudaLimitCliDisable(&d, &g) == NV_OK);
    assert(d.nCudaLimitRefCnt == 0);
    if (!dv2LimitExperimentTraceEnabled(&g)) assert(logs == 0);
}
int main(void) {
    test_device(0x2b8c10deu, 0, 0);
    test_device(0x2b8510deu, 0, 0);
    test_device(0x2b8710deu, 0, 0);
    test_device(0x2b8c10deu, 1, 0);
    test_device(0x2b8c10deu, 0, 1);
    puts("PASS: targeting, default behavior, nested requests, underflow, teardown, multiple clients, errors");
    return 0;
}
'''


def without_includes(text):
    return "\n".join(line for line in text.splitlines() if not line.startswith("#include "))


def main():
    results = []
    compiler = shutil.which("gcc")
    if not compiler:
        raise SystemExit("gcc is required for CPU-only mocked control-flow checks")
    with tempfile.TemporaryDirectory(prefix="dv2-patch-validation-") as scratch:
        stage = Path(scratch)
        for name, contents in original.items():
            path = stage / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(contents, encoding="utf-8", newline="\n")
        for name in ("0001-dv2-limit-diagnostics.patch", "0002-dv2-experimental-host-cuda-limit-bypass.patch"):
            run(["git", "apply", "--check", "--whitespace=error-all", str(OUT / name)], cwd=stage)
            run(["git", "apply", "--whitespace=error-all", str(OUT / name)], cwd=stage)
            results.append(f"PASS: {name} applies in sequence to isolated source copies")
        body = without_includes((stage / HEADER).read_text()) + "\n" + without_includes((stage / PERF).read_text())
        harness = stage / "control_flow.c"
        harness.write_text(PREAMBLE + body + TESTS, encoding="utf-8")
        for trace, bypass in ((0, 0), (1, 0), (0, 1), (1, 1)):
            executable = stage / f"check-{trace}-{bypass}.exe"
            run([compiler, "-std=c11", "-Wall", "-Wextra", "-Werror",
                 f"-DNV_DV2_TRACE_LIMITS={trace}", f"-DNV_DV2_SKIP_HOST_CUDA_LIMIT={bypass}",
                 str(harness), "-o", str(executable)])
            result = run([str(executable)])
            results.append(f"trace={trace}, bypass={bypass}: {result.stdout.strip()}")
        invalid = subprocess.run([compiler, "-std=c11", "-fsyntax-only", "-DNV_DV2_SKIP_HOST_CUDA_LIMIT=2", str(harness)], text=True, capture_output=True)
        assert invalid.returncode != 0 and "flags must be 0 or 1" in invalid.stderr
        results.append("PASS: invalid build flag rejected")
    # No unreviewed tracked source modifications may be introduced by generation or checks.
    run(["git", "diff", "--exit-code", "--", PERF, GR], cwd=ROOT)
    report = {
        "base_commit": run(["git", "rev-parse", "HEAD"], cwd=ROOT).stdout.strip(),
        "compiler": run([compiler, "--version"]).stdout.splitlines()[0],
        "checks": results,
        "not_validated": ["full Linux module compilation", "firmware behavior", "AI performance", "hardware bypass effectiveness"],
        "gpu_access": False,
    }
    (OUT / "validation.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print("\n".join(results))


if __name__ == "__main__":
    main()
