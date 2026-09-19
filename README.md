# RTX 5090 D v2: limiter investigation and experimental patches

**This bundle is not a verified RTX 5090 D v2 AI unlock.** It contains a diagnostic patch and an optional experiment that suppresses one specific host-requested CUDA clock-limit path. There is no evidence yet that this path causes the D v2's advertised AI-throughput restriction.

**Current finding:** the SM-throttle configuration query is implemented in GPU-resident GSP firmware; the open CPU-side driver requests, caches, and reports its results. GPU-side enforcement is a hypothesis supported by this interface, not an established explanation of the D v2 AI cap. The available source cannot distinguish a fixed hardware-fuse limit from a firmware-programmed hardware restriction.

Read the [enforcement analysis](ENFORCEMENT_ANALYSIS.md) for the complete call chain, evidence, uncertainty, and corrected release timeline. Patch 2 targets a separate generic CUDA clock-limit path and should not be treated as a likely SM-throttle bypass on present evidence.

Base: NVIDIA open GPU kernel modules **615.71.09**, commit `61dcc93722ecb418bb5f2e00923f05b4b8051dd1`.

The patches have not been applied to this checkout's tracked driver files, installed, or run on the GPU. Both feature flags default to **0**.

## Standalone repository

Remote: <https://github.com/JackyYuenDacai/rtx5090dv2-bypass-limiter-experiment>

This repository contains the patch bundle, not the NVIDIA driver source. To regenerate or validate the bundle after cloning it separately, point `RTX5090DV2_DRIVER_ROOT` at a driver checkout of the base commit stated above:

```sh
export RTX5090DV2_DRIVER_ROOT=/path/to/open-gpu-kernel-modules
python build_bundle.py
python validate_bundle.py
```

PowerShell equivalent:

```powershell
$env:RTX5090DV2_DRIVER_ROOT = 'F:\GitHub\open-GPU-kernel-modules'
python build_bundle.py
python validate_bundle.py
```

When this repository remains inside the driver checkout at `artifacts/rtx5090dv2`, the scripts discover the enclosing driver sources automatically. Validation requires Git and GCC and runs CPU-only mocked checks. It does not access the GPU.

## What the investigation established

| Candidate | Evidence | Implication |
| --- | --- | --- |
| Device name / ID | `g_nv_name_released.h:795` identifies PCI device `0x2B8C`. | Editing the name table does not change the hardware's identity or demonstrate an unlock. |
| `GET_SM_ISSUE_THROTTLE_CTRL` | `ctrl2080gr.h:1694` defines mask and credit indexes; its documentation describes fuse values. `kernel_graphics.c:1445` requests/caches them. | Useful evidence to collect, but this is a getter. The exposed code does not document the mask-bit meanings or how the credit value maps to throughput. |
| `GET_SM_ISSUE_RATE_MODIFIER_V2` | `ctrl2080gr.h:1651` exposes instruction-family selectors. | The data may describe hardware throughput restrictions. A nonzero value alone does not establish the D v2 AI cap. |
| Firmware boundary | Internal throttle query method `0x20800b05` has flags `0x1c0c0` in `g_subdevice_nvoc.c:8882`, including `ROUTE_TO_PHYSICAL` (`0x40`). `resource.c:264` forwards such firmware-client controls through RPC. | The open code does not contain the underlying implementation needed to establish a firmware/fuse bypass. |
| Generic CUDA clock limit | `kern_cuda_limit.c:95` forwards reference-counted client requests to `0x802009`. `ctrl0080perf.h:46` documents CUDA-based clock limiting. | This is a concrete host-side intervention point. It predates Blackwell and is not proven to be the D v2 limiter. |
| Explicit D v2 special case | `mem_mgr_gm107.c:1427` contains a Windows suspend/resume buffer workaround for `0x2B8C`. | Unrelated to instruction throughput. |

The SM throttle query first appears in the available Git history in **580.65.06 (2025-08-04)**; the credit index appears in **595.45.04 (2026-03-05)**. This dates public interfaces, not the introduction of hardware enforcement. The generic CUDA-limit implementation was already present in **515.43.04 (2022-05-09)**.

The August query addition was eight days before the D v2 launch, but NVIDIA had already advertised **2,375 AI TOPS for the original 5090 D in January 2025**. The proximity to the D v2 launch therefore does not establish when the AI restriction began or how it is enforced. Sources and implications are recorded in the [timeline analysis](ENFORCEMENT_ANALYSIS.md#release-timing-does-not-establish-enforcement).

## Files and behavior

1. `0001-dv2-limit-diagnostics.patch`
   - Adds a shared experiment header and build-time flags.
   - With tracing enabled, logs existing firmware-query status and returned index/data pairs during GR initialization.
   - Logs CUDA-limit requests and forwarding results.
   - Does not modify returned capabilities, fuse data, or the requested limit.
2. `0002-dv2-experimental-host-cuda-limit-bypass.patch`
   - Apply after patch 1.
   - Tests only the generic host CUDA clock-limit hypothesis; it does not intercept or modify the SM-throttle query or its firmware implementation.
   - When explicitly compiled with bypass enabled, suppresses this handler's CUDA-limit enable/disable forwarding on physical PCI device `0x2B8C`.
   - Preserves per-client reference counts, nested requests, unmatched-disable errors, and local teardown cleanup.
   - Suppresses the corresponding teardown RPC because this experimental build never forwarded that client's enable requests through this handler.
   - Leaves other GPU IDs and virtual GPUs on their original paths.

This second patch acknowledges the caller's request without enforcing this particular host-requested clock limit. That is the intentional experiment. It does **not** issue a firmware "unlock" command, change SM fuses, remove existing independent firmware limits, alter thermal/power policies, or restore the D v2's reduced physical memory configuration. Other callers or firmware-internal paths can still impose limits.

Suppressing a generic CUDA safety clock limit could affect stability or power behavior. The source does not establish why it is requested on this card. Use diagnostics to establish whether the path is involved before interpreting any performance change.

## Platform limitation

The NVIDIA source repository builds **Linux kernel modules**; this standalone repository supplies patches for that source. The identity query performed before the request to avoid direct testing reported a local Windows RTX 5090 D v2, driver **616.92**. These patches cannot modify that installed Windows driver. WSL GPU access also uses the Windows host driver; rebuilding this Linux module inside WSL does not replace it.

An eventual hardware evaluation would need a native Linux driver installation with firmware and user-space components matching the source release. No hardware evaluation was performed here, and no Windows binary or firmware patch is supplied.

## Reviewing and building later

The following commands are documentation only; none were run against the installed driver.

From an independent driver checkout of the stated base commit, set `BUNDLE` to the absolute path of this repository:

```sh
BUNDLE=/path/to/rtx5090dv2-bypass-limiter-experiment
git apply --check "$BUNDLE/0001-dv2-limit-diagnostics.patch"
git apply "$BUNDLE/0001-dv2-limit-diagnostics.patch"
git apply --check "$BUNDLE/0002-dv2-experimental-host-cuda-limit-bypass.patch"
git apply "$BUNDLE/0002-dv2-experimental-host-cuda-limit-bypass.patch"
```

Build a diagnostic baseline using the repository's normal Linux prerequisites:

```sh
make modules -j"$(nproc)" \
  EXTRA_CFLAGS='-DNV_DV2_TRACE_LIMITS=1 -DNV_DV2_SKIP_HOST_CUDA_LIMIT=0'
```

For a separate clean experimental build, change only:

```sh
make modules -j"$(nproc)" \
  EXTRA_CFLAGS='-DNV_DV2_TRACE_LIMITS=1 -DNV_DV2_SKIP_HOST_CUDA_LIMIT=1'
```

Use separate clean build trees: changing compiler flags alone may not rebuild existing objects. These are compiler flags introduced by the patch, **not** `NVreg_RegistryDwords` options. Bypass mode also enables the diagnostic logging. Full module compilation and linking were not validated in this environment.

To remove the source changes, reverse the patches in the opposite order:

```sh
git apply -R "$BUNDLE/0002-dv2-experimental-host-cuda-limit-bypass.patch"
git apply -R "$BUNDLE/0001-dv2-limit-diagnostics.patch"
```

## Interpreting a future evaluation

- The baseline must produce `DV2EXP` initialization logs to confirm the diagnostic build is active. An unsupported firmware query is not a zero/unlimited result.
- If tracing is known to be active but the workload produces no `CUDA request=1`, this handler is not imposing that workload's limit; suppressing it is not a demonstrated solution.
- `CUDA bypass active=1` proves a host request was suppressed. It does not prove firmware behavior changed or AI throughput increased.
- The SM values are initialization-time configuration snapshots, not time-series measurements of active throttling. Do not assign meanings to undocumented mask bits or treat zero as universally unlimited.
- Any future before/after comparison should use the same workload, numerical mode, driver/firmware, cooling and power configuration, with correct output and repeated steady-state measurements. A change in reported names, fuse values, clocks alone, or a single short benchmark does not establish a bypass.
- With only one D v2, an A/B test could show an improvement on that card, but cannot establish parity with a standard 5090.

## Offline validation performed

`validation.json` records the results. `validate_bundle.py` applies the patches only to temporary source copies and compiles/runs the patched CUDA-limit functions with mocked GPU/RM objects. It never opens GPU devices or launches GPU workloads.

Checked all four trace/bypass flag combinations: default behavior, PCI targeting, virtual-GPU and Tegra exclusions, nested requests, unmatched disables, teardown, multiple clients, and forwarded error behavior. Invalid flag values are rejected at compilation. Patch applicability and whitespace checks passed.

These tests validate host-side control flow only. They do not establish full-driver build compatibility, firmware effects, performance improvements, or successful removal of the AI restriction.

The deeper enforcement investigation was source-only. It did not change either patch or the validation scripts, and `validation.json` remains a record of the existing offline checks, not evidence for the enforcement hypothesis.
