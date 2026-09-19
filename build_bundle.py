"""Generate experimental patches without modifying tracked driver files."""
from pathlib import Path
import difflib
import os

OUT = Path(__file__).resolve().parent
PERF = "src/nvidia/src/kernel/gpu/perf/kern_cuda_limit.c"
GR = "src/nvidia/src/kernel/gpu/gr/kernel_graphics.c"
HEADER = "src/nvidia/inc/kernel/gpu/perf/dv2_limit_experiment.h"


def find_driver_root():
    configured = os.environ.get("RTX5090DV2_DRIVER_ROOT")
    candidates = [Path(configured).expanduser().resolve()] if configured else OUT.parents
    for candidate in candidates:
        if all((candidate / path).is_file() for path in (PERF, GR)):
            return candidate
    raise SystemExit(
        "NVIDIA driver sources not found. Set RTX5090DV2_DRIVER_ROOT to a "
        "checkout of NVIDIA/open-gpu-kernel-modules at the README's base commit."
    )


ROOT = find_driver_root()


def replace_once(text, old, new):
    assert text.count(old) == 1, (old[:100], text.count(old))
    return text.replace(old, new, 1)


def patch(name, before, after):
    parts = []
    for path, modified in after.items():
        original = before.get(path, "")
        if original == modified:
            continue
        parts.append(f"diff --git a/{path} b/{path}\n")
        if path not in before:
            parts.append("new file mode 100644\n")
        parts.extend(difflib.unified_diff(
            original.splitlines(keepends=True), modified.splitlines(keepends=True),
            fromfile=f"a/{path}" if path in before else "/dev/null",
            tofile=f"b/{path}", n=5))
    (OUT / name).write_text("".join(parts), encoding="utf-8", newline="\n")


original = {p: (ROOT / p).read_text(encoding="utf-8") for p in (PERF, GR)}
diagnostic = dict(original)
diagnostic[HEADER] = '''/* SPDX-License-Identifier: MIT */
/* Experimental diagnostics. These controls are not upstream NVIDIA options. */
#ifndef DV2_LIMIT_EXPERIMENT_H
#define DV2_LIMIT_EXPERIMENT_H

#include "gpu/gpu.h"
#include "nvdevid.h"

/* Build-time settings: rebuild/reload all modules when changing either one. */
#ifndef NV_DV2_TRACE_LIMITS
#define NV_DV2_TRACE_LIMITS 0
#endif
#ifndef NV_DV2_SKIP_HOST_CUDA_LIMIT
#define NV_DV2_SKIP_HOST_CUDA_LIMIT 0
#endif

#if ((NV_DV2_TRACE_LIMITS != 0) && (NV_DV2_TRACE_LIMITS != 1)) || \\
    ((NV_DV2_SKIP_HOST_CUDA_LIMIT != 0) && (NV_DV2_SKIP_HOST_CUDA_LIMIT != 1))
#error "DV2 experiment flags must be 0 or 1"
#endif

static NV_INLINE NvBool
dv2LimitExperimentMatchesGpu(OBJGPU *pGpu)
{
    return !IS_VIRTUAL(pGpu) &&
           (DRF_VAL(_PCI, _DEVID, _DEVICE, pGpu->idInfo.PCIDeviceID) == 0x2B8CU);
}

static NV_INLINE NvBool
dv2LimitExperimentTraceEnabled(OBJGPU *pGpu)
{
    return ((NV_DV2_TRACE_LIMITS != 0) || (NV_DV2_SKIP_HOST_CUDA_LIMIT != 0)) &&
           dv2LimitExperimentMatchesGpu(pGpu);
}

static NV_INLINE NvBool
dv2LimitExperimentBypassEnabled(OBJGPU *pGpu)
{
    return (NV_DV2_SKIP_HOST_CUDA_LIMIT != 0) &&
           dv2LimitExperimentMatchesGpu(pGpu);
}

#endif
'''

diagnostic[PERF] = replace_once(diagnostic[PERF],
    '#include "gpu/perf/kern_cuda_limit.h"',
    '#include "gpu/perf/kern_cuda_limit.h"\n#include "gpu/perf/dv2_limit_experiment.h"')
diagnostic[PERF] = replace_once(diagnostic[PERF],
    '    // Obtain current Cuda limit activation setting.\n',
    '''    if (dv2LimitExperimentTraceEnabled(pGpu))
    {
        NV_PRINTF(LEVEL_WARNING,
                  "DV2EXP CUDA request=%u client=0x%x device=0x%x refs=%u\\n",
                  (NvU32)pParams->bCudaLimit, RES_GET_CLIENT_HANDLE(pDevice),
                  RES_GET_HANDLE(pDevice), pDevice->nCudaLimitRefCnt);
    }

    // Obtain current Cuda limit activation setting.
''')
diagnostic[PERF] = replace_once(diagnostic[PERF],
    '''                                 sizeof(*pParams));
    }

    return status;
''',
    '''                                 sizeof(*pParams));
        if (dv2LimitExperimentTraceEnabled(pGpu))
        {
            NV_PRINTF(LEVEL_WARNING,
                      "DV2EXP CUDA forwarded active=%u status=0x%x\\n",
                      (NvU32)bCudaLimitAfter, status);
        }
    }

    return status;
''')

diagnostic[GR] = replace_once(diagnostic[GR],
    '#include "rmapi/client.h"',
    '#include "rmapi/client.h"\n#include "gpu/perf/dv2_limit_experiment.h"')
for member, list_size, list_name, label in (
    ("smIssueRateModifierV2", "smIssueRateModifierListSize", "smIssueRateModifierList", "issue-rate-v2"),
    ("smIssueThrottleCtrl", "smIssueThrottleCtrlListSize", "smIssueThrottleCtrlList", "issue-throttle"),
):
    anchor = f"                             sizeof(pParams->{member}));\n\n    if (status == NV_OK)"
    record = f"pParams->{member}.{member}[grIdx]"
    max_size = "NV2080_CTRL_GR_SM_ISSUE_RATE_MODIFIER_V2_MAX_LIST_SIZE" if member.endswith("V2") else "NV2080_CTRL_GR_SM_ISSUE_THROTTLE_CTRL_MAX_LIST_SIZE"
    block = f'''                             sizeof(pParams->{member}));

    /* Observe the existing firmware response; do not change any returned values. */
    if (dv2LimitExperimentTraceEnabled(pGpu))
    {{
        NV_PRINTF(LEVEL_WARNING, "DV2EXP {label} gr=%u status=0x%x\\n",
                  grIdx, status);
        if (status == NV_OK)
        {{
            NvU32 count = {record}.{list_size};
            NV_PRINTF(LEVEL_WARNING, "DV2EXP {label} count=%u\\n", count);
            for (NvU32 i = 0; (i < count) && (i < {max_size}); i++)
            {{
                NV_PRINTF(LEVEL_WARNING,
                          "DV2EXP {label} index=0x%x data=0x%x\\n",
                          {record}.{list_name}[i].index,
                          {record}.{list_name}[i].data);
            }}
        }}
    }}

    if (status == NV_OK)'''
    diagnostic[GR] = replace_once(diagnostic[GR], anchor, block)

bypass = dict(diagnostic)
bypass[PERF] = replace_once(bypass[PERF],
    '''    if (pDevice->nCudaLimitRefCnt > 0)
    {
        status = pRmApi->Control''',
    '''    if (pDevice->nCudaLimitRefCnt > 0)
    {
        /* This build never forwarded this device's host CUDA-limit enables. */
        if (dv2LimitExperimentBypassEnabled(pGpu))
        {
            NV_PRINTF(LEVEL_WARNING,
                      "DV2EXP CUDA bypass teardown refs=%u\\n",
                      pDevice->nCudaLimitRefCnt);
            pDevice->nCudaLimitRefCnt = 0;
            return NV_OK;
        }

        status = pRmApi->Control''')
bypass[PERF] = replace_once(bypass[PERF],
    '''    if (bCudaLimitBefore != bCudaLimitAfter)
    {
        status = pRmApi->Control''',
    '''    if (bCudaLimitBefore != bCudaLimitAfter)
    {
        /*
         * Experimental: suppress ONLY this client-requested CUDA clock limit.
         * Keep refcount validation above, including unmatched-disable errors.
         * This does not override firmware policy or SM instruction-rate fuses.
         */
        if (dv2LimitExperimentBypassEnabled(pGpu))
        {
            NV_PRINTF(LEVEL_WARNING,
                      "DV2EXP CUDA bypass active=%u refs=%u (unverified AI effect)\\n",
                      (NvU32)bCudaLimitAfter, pDevice->nCudaLimitRefCnt);
            return NV_OK;
        }

        status = pRmApi->Control''')

if __name__ == "__main__":
    patch("0001-dv2-limit-diagnostics.patch", original, diagnostic)
    patch("0002-dv2-experimental-host-cuda-limit-bypass.patch", diagnostic, bypass)
    print("Generated two patches; tracked driver source was not modified.")
