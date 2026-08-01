# V-Shape IPM Motor AI Optimizer — OneSixth Edition

A self-contained AI-driven multi-objective optimization tool for the V-Shape
Interior Permanent Magnet (IPM) motor, powered by NSGA-II with a 4-objective
Pareto front: **Efficiency** (max), **Torque Ripple** (min), **Power Density**
(max), and **Material Cost** (min). The Python runtime is bundled — **no
Python installation required**.

This edition uses a **1/6 sector symmetry model** (Multiplier = 6), which
makes each Maxwell FEA evaluation **3–6× faster** than the full 360° model
while producing equivalent results.

## System Requirements

| Requirement      | Minimum                                          |
| ---------------- | ------------------------------------------------ |
| OS               | Windows 10/11 64-bit                             |
| Ansys Electronics Desktop (AEDT) | **2026.1 or newer**                   |
| Python           | Not required (bundled)                           |

## Required Input Files

All files must stay in the same folder as the `.exe`. Do not rename them.

| File                            | Purpose                                      | Notes                                       |
| ------------------------------- | -------------------------------------------- | ------------------------------------------- |
| `MotorOptimizer_OneSixth.exe`   | Main program                                 | Do not rename                               |
| `Ai_Optimization_Bounds.xlsx`   | Design variable bounds (19 parameters)       | Edit to change the search space             |
| `Matlab_Ai_Optimization_oneSixth.aedt` | Ansys Maxwell project (1/6 sector model) | Saved by AEDT 2026.1; requires AEDT 2026.1+ |
| `_internal\`                    | Bundled Python runtime & libraries           | Do not delete or move                       |
| `run_ansys.bat`                 | One-click launcher                           | Optional convenience                        |

## Quick Start (recommended)

1. Double-click `run_ansys.bat`. It runs:

   ```bat
   MotorOptimizer_OneSixth.exe --mode ansys --pop-size 8 --generations 10 --non-graphical
   ```

2. Wait — each generation runs a real Maxwell FEA simulation on the 1/6 model.
3. Results are written to `outputs\run_YYYYMMDD_HHMMSS\`:

   | File                            | Description                  |
   | ------------------------------- | ---------------------------- |
   | `best_optimized_design_v5.2.csv`| Best design & all objectives |
   | `optimization_report.md`        | Full report                  |
   | `*.png`                         | Pareto & convergence charts  |

## Running from the Command Line

```bat
:: 1) Verify installation (no Ansys needed) — expect "11/11 passed"
MotorOptimizer_OneSixth.exe --test

:: 2) Quick sanity run against Ansys (1 generation, 2 candidates)
MotorOptimizer_OneSixth.exe --mode ansys --pop-size 2 --generations 1

:: 3) Full optimization (defaults: pop 8, 10 generations, headless)
MotorOptimizer_OneSixth.exe --mode ansys

:: 4) Resume an interrupted run
MotorOptimizer_OneSixth.exe --resume --mode ansys
```

## CLI Options

| Option                          | Default          | Description                                   |
| ------------------------------- | ---------------- | --------------------------------------------- |
| `--pop-size N`                  | 8                | Population size per generation                |
| `--generations N`               | 10               | Maximum number of generations                 |
| `--mode {offline,matlab,ansys}` | offline          | Evaluation mode; use `ansys` for Maxwell FEA  |
| `--seed N`                      | —                | Random seed for reproducibility               |
| `--resume`                      | —                | Continue from the last checkpoint             |
| `--show-gui`                    | —                | Show the Ansys GUI (default: headless)        |
| `--w-eff`                       | 1.0              | Efficiency objective weight                   |
| `--w-ripple`                    | 1.0              | Torque ripple objective weight                |
| `--w-pwr`                       | 0.5              | Power density objective weight                |
| `--w-cost`                      | 0.05             | Cost objective weight                         |
| `--plot-all`                    | —                | Generate all charts after completion          |
| `--test`                        | —                | Run built-in unit tests and exit              |

## Troubleshooting

| Problem                              | Solution                                                        |
| ------------------------------------ | --------------------------------------------------------------- |
| `--test` fails / "file not found"    | Keep `Ai_Optimization_Bounds.xlsx` and the `.aedt` next to the `.exe` |
| "Project saved by a newer version"   | This package requires AEDT **2026.1 or newer**                  |
| "Could not connect to Ansys"         | Open AEDT manually once, or run `ansysedt.exe -regserver`       |
| Ansys locked / not responding        | Task Manager → End Task `ansysedt.exe`; delete `*.lock` files; rerun |
| "VCRUNTIME140.dll" error             | Install Microsoft Visual C++ Redistributable (x64)              |

## Notes

- Run **one instance at a time** — concurrent runs cause Ansys file locks.
- This is the **OneSixth** package (1/6 sector, Multiplier = 6). For the full
  360° model, use the **standard** package (requires AEDT 2022.2+).
