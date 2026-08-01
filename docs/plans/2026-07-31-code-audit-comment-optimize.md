# Code Audit, Comment & Optimization Plan

> **For agentic workers:** 6 parallel subagents, one per module. Each reads its section, identifies bugs/optimizations, adds English comments, returns exact `edit` operations (oldString → newString).

**Goal:** Understand, fix bugs, optimize, and add English comments to `motor_optimizer_ver5.2(fix lan3)_remote.py` (~2325 lines).

**Architecture:** Split into 6 independent modules by line range. Each module is self-contained with clear dependencies on shared utilities (normalize_ind_keys, bounds, etc.). Parallel dispatch → integrate all edits → run tests → final verification.

**Approach:** Subagent per module (research only — read, analyze, return edits). I apply edits centrally to avoid conflicts.

---

### Module A: Lines 1–427 (Constants, Constraints, Physics Surrogate)

**Scope:** Imports, constants, key normalization, 6 geometric constraints, `compute_dependent_variables`, `physics_surrogate`.

**Subagent task:** Read lines 1–427. Analyze for bugs, code smells, optimization opportunities. Add English docstrings/comments to every function. Return exact `edit` pairs.

### Module B: Lines 428–644 (ML Surrogate, Sensitivity)

**Scope:** `MLSurrogate` class (GP/KNN, training, prediction, confidence), `get_per_objective_sensitivity`, `check_pareto_non_domination`.

**Subagent task:** Read lines 428–644. Analyze. Add comments. Return edits.

### Module C: Lines 645–896 (NSGA-II Core)

**Scope:** `fast_non_dominated_sort`, `crowding_distance_assignment`, `compute_pareto_hypervolume`, `compute_pareto_spacing`, `nsga2_selection`, `nsga2_tournament_selection`, `rank_candidates_surrogate`.

**Subagent task:** Read lines 645–896. Analyze. Add comments. Return edits.

### Module D: Lines 897–1124 (Evolutionary Operators)

**Scope:** `get_baseline_individual`, `random_individual`, `repair_individual`, `crossover`, `sensitivity_mutate`, `mutate`, `check_population_diversity`, `detect_stagnation`.

**Subagent task:** Read lines 897–1124. Analyze. Add comments. Return edits.

### Module E: Lines 1125–1634 (Ansys/MATLAB Integration, CSV Parsing, Evaluation)

**Scope:** `_cleanup_temp_project_files`, `run_ansys_direct`, `write_param_excel`, `run_matlab`, `evaluate_population`, `_parse_csv_outputs`.

**Subagent task:** Read lines 1125–1634. Analyze. Add comments. Return edits.

### Module F: Lines 1635–2325 (Visualization, Report, Tests, Main)

**Scope:** 4 plot functions, `generate_report`, `log_history_entry`, `clean_post_run_temp_files`, `run_unit_tests`, `save_state`/`load_state`, `main()`.

**Subagent task:** Read lines 1635–2325. Analyze. Add comments. Return edits.

---

## Execution

1. Dispatch 6 subagents in parallel (one per module)
2. Each returns analysis + exact edit operations
3. I apply all edits and resolve any conflicts
4. Run `--test` to verify no regressions
5. Run `--generations 2 --pop-size 4 --offline` quick smoke test
