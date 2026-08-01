# V-Shape IPM Motor AI Optimizer

A self-contained AI-driven multi-objective optimization tool for the V-Shape
Interior Permanent Magnet (IPM) motor, powered by NSGA-II with a 4-objective
Pareto front: **Efficiency** (max), **Torque Ripple** (min), **Power Density**
(max), and **Material Cost** (min). The Python runtime is bundled — **no
Python installation required**.

## System Requirements

| Requirement      | Minimum                                          |
| ---------------- | ------------------------------------------------ |
| OS               | Windows 10/11 64-bit                             |
| Ansys Electronics Desktop (AEDT) | **2022.2 or newer**                   |
| Python           | Not required (bundled)                           |

## Required Input Files

All files must stay in the same folder as the `.exe`. Do not rename them.

| File                          | Purpose                                      | Notes                                       |
| ----------------------------- | -------------------------------------------- | ------------------------------------------- |
| `MotorOptimizer.exe`          | Main program                                 | Do not rename                               |
| `Ai_Optimization_Bounds.xlsx` | Design variable bounds (19 parameters)       | Edit to change the search space             |
| `Matlab_Ai_Optimization.aedt` | Ansys Maxwell project (full 360° model)      | Saved by AEDT 2022.2; requires AEDT 2022.2+ |
| `_internal\`                  | Bundled Python runtime & libraries           | Do not delete or move                       |
| `run_ansys.bat`               | One-click launcher                           | Optional convenience                        |

## Quick Start (recommended)

1. Double-click `run_ansys.bat`. It runs:

   ```bat
   MotorOptimizer.exe --mode ansys --pop-size 8 --generations 10 --non-graphical
   ```

2. Wait — each generation runs a real Maxwell FEA simulation.
3. Results are written to `outputs\run_YYYYMMDD_HHMMSS\`:

   | File                            | Description                 |
   | ------------------------------- | --------------------------- |
   | `best_optimized_design_v5.2.csv`| Best design & all objectives |
   | `optimization_report.md`        | Full report                 |
   | `*.png`                         | Pareto & convergence charts |

## Running from the Command Line

Open **Command Prompt (CMD)** and change to the package folder (the one containing `MotorOptimizer.exe` — the program only finds its input files there):

```bat
cd /d "C:\path\to\MotorOptimizer"
```

Then run any of the following:

```bat
:: 1) Verify installation (no Ansys needed) — expect "11/11 passed"
MotorOptimizer.exe --test

:: 2) Quick sanity run against Ansys (1 generation, 2 candidates)
MotorOptimizer.exe --mode ansys --pop-size 2 --generations 1

:: 3) Full optimization (defaults: pop 8, 10 generations, headless)
MotorOptimizer.exe --mode ansys

:: 4) Resume an interrupted run
MotorOptimizer.exe --resume --mode ansys
```

> `run_ansys.bat` is only a shortcut for command 3 plus a `pause` at the end — running the commands above in CMD works exactly the same.

## CLI Options

| Option                          | Default           | Description                                   |
| ------------------------------- | ----------------- | --------------------------------------------- |
| `--mode {offline,matlab,ansys}` | `offline`         | Evaluation backend; use `ansys` for Maxwell FEA |
| `--algorithm {nsga2,ga}`        | `nsga2`           | Optimization engine (NSGA-II multi-objective) |
| `--pop-size N`                  | 8                 | Population size per generation                |
| `--generations N`               | 10                | Maximum number of generations                 |
| `--crossover F`                 | 0.7               | Crossover probability                         |
| `--mutation F`                  | 0.2               | Mutation rate per gene                        |
| `--seed N`                      | —                 | Random seed for reproducibility               |
| `--resume`                      | —                 | Continue from the last checkpoint             |
| `--matlab-exe PATH`             | `C:\MATLAB\R2023b\bin\matlab.exe` | MATLAB executable path (mode `matlab`) |
| `--non-graphical`               | True              | Run Ansys Maxwell headless (RAM/CPU savings)  |
| `--show-gui`                    | —                 | Show the Ansys GUI                            |
| `--keep-temp`                   | —                 | Keep temporary `.aedt` project files          |
| `--w-eff`                       | 1.0               | Efficiency objective weight                   |
| `--w-ripple`                    | 1.0               | Torque ripple objective weight                |
| `--w-pwr`                       | 0.5               | Power density objective weight                |
| `--w-cost`                      | 0.05              | Material cost objective weight                |
| `--no-ml`                       | —                 | Disable ML surrogate (force 100% FEA)         |
| `--no-screening`                | —                 | Disable surrogate candidate screening         |
| `--no-local-search`             | —                 | Disable elite local search                    |
| `--plot-pareto`                 | —                 | Generate 2D Pareto plot                       |
| `--plot-all`                    | —                 | Generate all analysis & convergence charts    |
| `--no-report`                   | —                 | Skip Markdown report generation               |
| `--test`                        | —                 | Run built-in unit tests (11 checks) and exit  |

## Troubleshooting

| Problem                              | Solution                                                        |
| ------------------------------------ | --------------------------------------------------------------- |
| `--test` fails / "file not found"    | Keep `Ai_Optimization_Bounds.xlsx` and the `.aedt` next to the `.exe` |
| "Project saved by a newer version"   | This package requires AEDT **2022.2 or newer**                  |
| "Could not connect to Ansys"         | Open AEDT manually once, or run `ansysedt.exe -regserver`       |
| Ansys locked / not responding        | Task Manager → End Task `ansysedt.exe`; delete `*.lock` files; rerun |
| "VCRUNTIME140.dll" error             | Install Microsoft Visual C++ Redistributable (x64)              |

## Notes

- Run **one instance at a time** — concurrent runs cause Ansys file locks.
- This is the **standard** package (full 360° model). For a faster 1/6 sector
  model, use the **OneSixth** package (requires AEDT 2026.1+).
