# AI Optimization of V-Shape IPM Motor

This project implements a genetic algorithm and multi-objective optimization system (NSGA-II) for V-Shape Interior Permanent Magnet (IPM) motor design, coupling Python, MATLAB ActiveX, and Ansys Maxwell FEM simulations.

## Project Structure

```
Ai_Optimization_Of_Vshape_IPM_motor/
│
├── input/                              ← Input data & geometry specification
│   ├── Ai_Optimization_Bounds.xlsx     ← 19 design variable bounds and step constraints
│   ├── Ai_Optimization_ParamValues.xlsx← Parameter values written by Python for MATLAB
│   ├── Matlab_Ai_Optimization.aedt     ← 3D Ansys Maxwell motor model template
│   └── ... (specification documents)
│
├── output/                             ← Automatically generated output artifacts
│   ├── best_optimized_design_v5.1.csv  ← Best design configuration from version 5.1
│   ├── best_optimized_design_v5.csv    ← Best design from version 5
│   ├── best_optimized_design_v2.csv    ← Best design from version 2
│   ├── simulation_history.csv          ← Complete optimization candidate history
│   ├── pareto_front.png                ← 2D Efficiency vs Torque Ripple Pareto plot
│   ├── pareto_3d.png                   ← 3D Pareto plot (Efficiency vs TorqueRipple vs Cost)
│   ├── parallel_coordinates.png        ← Parallel coordinates trade-off chart
│   ├── convergence_history.png         ← Best score convergence curve
│   ├── sensitivity_analysis.csv        ← Spearman rank correlation matrix for 19 variables
│   ├── optimization_report.md          ← Automatically generated Markdown summary report
│   └── optimizer.log                   ← Per-run log output
│
├── motor_optimizer_ver5.1_remote.py ← Primary Production-grade script (Flat root-level execution)
├── Python_code/                        ← Python source code archive
│   ├── motor_optimizer_ver5.1.py       ← Standard v5.1 script (Subdirectory input/output)
│   ├── motor_optimizer_ver5.py         ← Integrated GA/NSGA-II optimizer (v5)
│   ├── motor_optimizer_ver2.py         ← Standard GA baseline (v2)
│   ├── motor_optimizer_ver3.py / ver4.py← Experimental stubs (v3 & v4)
│   └── requirements.txt                ← Python dependencies
│
├── Ai_optimization.m                   ← MATLAB ActiveX automation script for Ansys Maxwell
├── AGENTS.md                           ← Comprehensive AI Agent developer instructions
├── Technical_Reference.md              ← In-depth technical & mathematical reference
├── workflow_optimization.md            ← System flowchart and execution pipeline
└── README.md                           ← This file
```

## Optimizer Versions

### Version 5.1 Remote (Production-Grade Primary Script - Recommended)
- **Flat Root-Level I/O**: Reads and writes all configuration (Excel bounds), simulation history, reports, and plot images directly in the root folder.
- **Custom MATLAB Path (`--matlab-exe`)**: Supports passing explicit path to `matlab.exe` (e.g. `--matlab-exe "C:\MATLAB\R2023b\bin\matlab.exe"`).
- **Engineer Manual Override (`--interactive` / `--override-csv`)**: Allows engineers to interactively inspect or override parameters post-optimization, validating constraints and generating comparison tables (`engineer_manual_design.csv`).
- **Strict Error Handling**: Immediately halts execution with clear error logs if MATLAB or Ansys simulation fails.
- **Complete NSGA-II Engine**: Dedicated reproduction using binary tournament on Pareto rank + crowding distance.
- **Gaussian Process & Standardized ML Surrogate**: Uses `sklearn` GaussianProcessRegressor with RBF kernel and standardized feature vectors $[0, 1]$, with auto-fallback to KNN-IDW.
- **Built-in Unit Tests (`--test`)**: 7 automated unit tests verifying constraints, dominance sorting, crowding distance, repair logic, and scoring without full optimization runs.
- **Advanced 4-Plot Visualization (`--plot-all`)**: Generates 2D Pareto front, 3D Pareto space, Parallel coordinates, and Convergence curves.
- **Smart Diagnostics & Reports**: Auto-detects population diversity collapse, stagnation, and generates `optimization_report.md`.

### Version 5 (Unified Synthesis)
- Integrated GA and NSGA-II engines.
- Hybrid ML/Physics surrogate model.
- Spearman sensitivity analysis (`--sensitivity`).
- 2D Pareto front plot (`--plot-pareto`).

### Version 2 (Standard Baseline)
- Single-objective weighted Genetic Algorithm.
- 4 physical geometric constraints & step-snapping repair.
- Checkpoint/resume functionality (`optimizer_state.pkl`).
- Direct MATLAB / Ansys Maxwell ActiveX automation.

---

## Running the Optimizer

### Built-in Unit Testing
```bash
python motor_optimizer_ver5.1_remote.py --test
```

### Quick Offline Optimization with NSGA-II & Full Visualizations
```bash
python motor_optimizer_ver5.1_remote.py --algorithm nsga2 --pop-size 12 --generations 30 --mode offline --plot-all
```

### Full Ansys Maxwell FEM Simulation Run (via MATLAB)
```bash
python motor_optimizer_ver5.1_remote.py --algorithm nsga2 --pop-size 8 --generations 10 --mode matlab --matlab-exe "C:\MATLAB\R2023b\bin\matlab.exe" --plot-all
```

---

## Key Features

- **Strict Feasibility Guarantee**: 4 geometric constraints enforced with multi-strategy repair.
- **Canonical Variable Ordering**: Maintains strict alignment for 19 parameters across Python, Excel, MATLAB, and Ansys.
- **Multi-Objective Engine**: Full Deb's NSGA-II implementation for non-dominated sorting and crowding distance assignment.
- **Data-Driven & Physics Hybrid Surrogate**: Standardized Gaussian Process / KNN-IDW surrogate dynamically trained on evaluation history.
- **Comprehensive Visualizations**: Auto-generates 2D/3D Pareto, Parallel Coordinates, and Convergence charts.
- **Automated Reporting**: Generates Markdown report summarizing config, top 5 candidate designs, and parameter rankings.

---

## Dependencies

Requires Python 3.10+ with standard scientific packages:
- `pandas`
- `numpy`
- `openpyxl`
- `scipy`
- `matplotlib`
- `scikit-learn` (optional, for Gaussian Process surrogate)

Install dependencies with:
```bash
pip install -r Python_code/requirements.txt
```