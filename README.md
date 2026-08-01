# V-Shape IPM Motor AI Optimizer

AI-driven multi-objective optimization for **V-Shape Interior Permanent Magnet (IPM) motors** using **NSGA-II** with a 4-objective Pareto engine, an **ML surrogate model** for fast candidate screening, and direct **Ansys Maxwell** FEA integration.

```
python motor_optimizer_ver5.2(fix lan3)_remote.py --mode ansys --pop-size 8 --generations 10 --plot-all
```

---

## Quick Start (End Users)

No Python or source code needed — the Python runtime is bundled in the packages.

1. Download the `dist/` folder from this repository:
   - `dist/MotorOptimizer/` — standard package (full 360° model, AEDT 2022.2+)
   - `dist/MotorOptimizer_OneSixth/` — faster 1/6-sector model (AEDT 2026.1+)
2. Keep the package folder **complete and unchanged** (the `.exe` requires the
   `_internal\` folder next to it — do not download the `.exe` alone).
3. Double-click `run_ansys.bat` inside the folder and wait for the optimization
   to finish. Results appear in `outputs\run_YYYYMMDD_HHMMSS\`.

---

## Table of Contents

- [Quick Start (End Users)](#quick-start-end-users)
- [Features](#features)
- [Architecture](#architecture)
- [Optimization Objectives](#optimization-objectives)
- [Design Variables](#design-variables)
- [Geometric Constraints](#geometric-constraints)
- [Installation](#installation)
- [Usage](#usage)
- [CLI Reference](#cli-reference)
- [Outputs](#outputs)
- [Testing](#testing)
- [Repository Layout](#repository-layout)

---

## Features

- **4-objective Pareto optimization** (NSGA-II): efficiency, torque ripple, power density, and material cost.
- **ML surrogate screening** — Gaussian Process / KNN models (4 independent outputs) fall back to real FEA when uncertainty exceeds 2.5%.
- **Direct Ansys Maxwell 3D integration** via win32com ActiveX (Method B) or PyAEDT (Method A, optional).
- **RAM Guard** — automatic AEDT session restart when free memory drops below 50%, eliminating RPC disconnects and OOM.
- **COM thread-affinity fix** — reliable headless operation on remote/VPN/VM machines.
- **Elite local search** around Pareto rank-0 individuals with full constraint re-check.
- **Per-objective sensitivity mutation** driven by Spearman rank correlation.
- **Warm-start** from the best known individual in `simulation_history.csv`.
- **Resume** interrupted runs from checkpoint.
- **Timestamped outputs** (`outputs/run_YYYYMMDD_HHMMSS/`).

## Architecture

```
┌─────────────────────────────┐
│ NSGA-II (4D Pareto engine)  │
│  - non-dominated sorting    │
│  - crowding distance        │
│  - hypervolume tracking     │
│  - elite local search       │
└─────────────┬───────────────┘
              │ candidate designs (19 vars)
              ▼
┌─────────────────────────────┐
│ ML Surrogate (screening)    │
│  GP (RBF) / KNN — 4 outputs │
│  uncertainty > 2.5% → FEA   │
└─────────────┬───────────────┘
              │ selected candidates
              ▼
┌─────────────────────────────┐
│ Ansys Maxwell 3D (FEA)      │
│  COM thread-affinity fix    │
│  RAM guard / session reset  │
└─────────────┬───────────────┘
              │ objectives
              ▼
         Pareto front → report + plots
```

## Optimization Objectives

| Objective | Definition | Unit | Direction |
|---|---|---|---|
| Efficiency (η) | `P_out / P_in × 100` | % | **Maximize** |
| Power Density | `P_out / W_total` | kW/kg | **Maximize** |
| Material Cost | `Σ(V_i × costPerVolume_i)` | USD | **Minimize** |
| Torque Ripple | `pk2pk(T) / mean(T) × 100` | % | **Minimize** |

**Secondary composite score** (used for ranking in reports):

```
Score = (w_eff × Eff) − (w_ripple × Ripple) + (w_pwr × Pwr) − (w_cost × Cost/150)
```

## Design Variables

19 free design variables, loaded from `Ai_Optimization_Bounds.xlsx` (edit that file to change the search space):

| Variable | Description | Min | Max | Step | Unit |
|---|---|---|---|---|---|
| `Dr_in` | Rotor inner diameter | 50.0 | 90.0 | 5.0 | mm |
| `Air_gap` | Air gap | 0.5 | 1.5 | 0.1 | mm |
| `Lamda` | Stack length factor | 0.8 | 1.0 | 0.1 | — |
| `Bridge` | Rotor flux-bridge thickness | 1.0 | 3.0 | 0.1 | mm |
| `Hs0` | Slot opening height | 1.0 | 2.0 | 0.1 | mm |
| `Hs1` | Slot shoulder height | 1.0 | 2.0 | 0.1 | mm |
| `Hs2` | Slot body height | 16.0 | 30.0 | 1.0 | mm |
| `Bs0` | Slot opening width | 1.5 | 4.0 | 0.5 | mm |
| `Bs1` | Slot shoulder width | 3.0 | 10.0 | 0.5 | mm |
| `Bs2` | Slot bottom width | 5.0 | 14.0 | 1.0 | mm |
| `O1` | Magnet pocket V1 offset | 0.0 | 13.0 | 1.0 | mm |
| `O2` | Magnet pocket V2 offset | 2.0 | 7.0 | 0.5 | mm |
| `B1` | Magnet pocket width | 3.2 | 5.0 | 0.5 | mm |
| `rib` | Central rib width | 2.0 | 15.0 | 1.0 | mm |
| `hrib` | Central rib height | 2.0 | 6.0 | 0.5 | mm |
| `Mt` | Magnet thickness | 4.0 | 6.0 | 0.2 | mm |
| `Mw` | Magnet width | 10.0 | 30.0 | 2.0 | mm |
| `magDmin` | Magnet pocket bottom clearance | 0.0 | 10.0 | 1.0 | mm |
| `Thet_deg` | V-magnet opening angle | 0.0 | 90.0 | 1.0 | deg |

10 dependent variables (auto-derived per candidate): `D_ag`, `Ds_in`, `Dr_out`, `Speed_rpm` (= 1000 RPM at 6 poles / 50 Hz), `A_slot`, `D1`, `Acond`, `N`, `t0`, `Thet`.

## Geometric Constraints

Every candidate must satisfy all 6 constraints (violations are auto-repaired):

| # | Constraint | Condition |
|---|---|---|
| 1 | SlotHeight | `Hs0 + Hs1 + Hs2 < (Ds_out − Ds_in)/2 − 12.25 mm` |
| 2 | SlotWidthProgression | `Bs0 ≤ Bs1 ≤ Bs2` |
| 3 | BridgeThickness | `B1 ≤ Mt − 0.3 mm` |
| 4 | RotorFitsStator | `Dr_out > Dr_in` |
| 5 | MagnetDuctFit | `Mw > 2 × B1` |
| 6 | RibHeightLimit | `hrib ≤ min(O2, 4.5, 2 × Bridge)` |

## Installation

Requires **Python 3.10+** (tested on 3.13/3.14) and **Ansys Electronics Desktop** (for `--mode ansys`; use AEDT **2022.2 or newer** with the standard model, **2026.1 or newer** with the 1/6-sector OneSixth model).

```bash
git clone https://github.com/Khoingo0154/Ai_Optimization_Of_Vshape_IPM_motor.git
cd Ai_Optimization_Of_Vshape_IPM_motor

python -m venv .venv
.venv\Scripts\activate        # Windows
pip install -r requirements.txt
```

Note: `pyaedt` is optional (only needed for Method A); Method B (win32com ActiveX) is used by default.

## Usage

Basic production run (Ansys Maxwell FEA):

```powershell
python "motor_optimizer_ver5.2(fix lan3)_remote.py" --mode ansys --pop-size 8 --generations 10 --plot-all
```

Quick sanity run against Ansys (1 generation, 2 candidates):

```powershell
python "motor_optimizer_ver5.2(fix lan3)_remote.py" --mode ansys --pop-size 2 --generations 1
```

## CLI Reference

| Option | Default | Description |
|---|---|---|
| `--mode {offline,matlab,ansys}` | `offline` | Evaluation backend: `ansys` (Maxwell FEA), `matlab` (MATLAB bridge), `offline` (surrogate only) |
| `--algorithm {nsga2,ga}` | `nsga2` | Optimization engine: NSGA-II (multi-objective) or GA (single-objective) |
| `--pop-size N` | `8` | Population size per generation |
| `--generations N` | `10` | Maximum number of generations |
| `--crossover F` | `0.7` | Crossover probability |
| `--mutation F` | `0.2` | Mutation rate per gene |
| `--seed N` | — | Random seed for reproducibility |
| `--resume` | — | Resume from checkpoint |
| `--matlab-exe PATH` | `C:\MATLAB\R2023b\bin\matlab.exe` | MATLAB executable path |
| `--non-graphical` | `True` | Run Ansys Maxwell headless (RAM/CPU savings) |
| `--show-gui` | — | Show the Ansys GUI |
| `--keep-temp` | — | Keep temporary `.aedt` project files |
| `--w-eff F` | `1.0` | Efficiency objective weight |
| `--w-ripple F` | `1.0` | Torque ripple objective weight |
| `--w-pwr F` | `0.5` | Power density objective weight |
| `--w-cost F` | `0.05` | Material cost objective weight |
| `--no-ml` | — | Disable ML surrogate (force 100% FEA) |
| `--no-screening` | — | Disable surrogate candidate screening |
| `--no-local-search` | — | Disable elite local search |
| `--plot-pareto` | — | Generate 2D Pareto plot |
| `--plot-all` | — | Generate all analysis & convergence plots |
| `--no-report` | — | Skip Markdown report generation |
| `--test` | — | Run built-in unit tests (11 checks) and exit |

## Outputs

Every run writes to `outputs/run_YYYYMMDD_HHMMSS/`, with `outputs/latest/` symlinked to the most recent run:

| File | Description |
|---|---|
| `best_optimized_design_v5.2.csv` | Best design: 19 variables + objectives |
| `simulation_history.csv` | Full history of all evaluated candidates |
| `log_history.csv` | Per-candidate log & constraint violations |
| `optimizer.log` | Timestamped execution log |
| `optimization_report.md` | Auto-generated Markdown report |
| `pareto_front.png` | 2D Pareto front (efficiency vs ripple) |
| `pareto_3d.png` | 3D Pareto (efficiency, ripple, cost) |
| `parallel_coordinates.png` | 4-objective parallel coordinates |
| `convergence_history.png` | 4D hypervolume & best-score convergence |

## Testing

Run the built-in unit-test suite (11 checks — constraints, NSGA-II sorting, crowding distance, hypervolume, dependent variables, repair mechanism, key normalization):

```powershell
python "motor_optimizer_ver5.2(fix lan3)_remote.py" --test
```

Expected output:

```
Unit Test Suite Results: 11/11 passed, 0/11 failed
```

## Repository Layout

```
├── motor_optimizer_ver5.2(fix lan3)_remote.py   # main optimizer (standard 360° model)
├── motor_optimizer_ver5.2(fix lan3)_oneSixth_remote.py  # 1/6-sector variant (AEDT 2026.1+)
├── Ai_Optimization_Bounds.xlsx                  # 19 design variables bounds
├── Matlab_Ai_Optimization.aedt                  # Ansys Maxwell project (standard)
├── Matlab_Ai_Optimization_oneSixth.aedt         # Ansys Maxwell project (1/6 sector)
├── requirements.txt
└── dist/
    ├── MotorOptimizer/           # standalone exe package (standard)
    └── MotorOptimizer_OneSixth/  # standalone exe package (1/6 sector)
```

> **Note:** The `dist/` folders contain self-contained Windows executables (Python bundled) for deployment on machines without Python. Run `MotorOptimizer.exe --test` on the target machine to verify.
