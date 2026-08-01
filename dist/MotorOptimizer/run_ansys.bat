@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo ============================================
echo  V-Shape IPM Motor AI Optimizer
echo  (Standard model - Ansys 2022.2+)
echo ============================================
echo.
echo  Running optimization with Ansys Maxwell (headless)...
echo  Pop-size: 8 | Generations: 10
echo  You may close this window to run in background.
echo.

"MotorOptimizer.exe" --mode ansys --pop-size 8 --generations 10 --non-graphical

echo.
echo  DONE! Results saved in: outputs\run_YYYYMMDD_HHMMSS\
echo  - best_optimized_design_v5.2.csv : best design
echo  - optimization_report.md         : report
echo  - *.png                          : charts
echo.
pause
