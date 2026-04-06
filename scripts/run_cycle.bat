@echo off
REM Run one Meta-Harness cycle against itself.
REM Run this from N:\META_HARNESS\
REM
REM Workflow:
REM   1. Run pytest + coverage to produce test_results.xml and coverage.xml
REM   2. Run generate_metrics.py to write metrics.json
REM   3. Run metaharness run (single cycle)

cd /d %~dp0..

where python >nul 2>&1 && set "PYEXE=python" || set "PYEXE=py"

echo [1/3] Running tests...
pytest tests/ --junitxml=.metaharness\test_results.xml --cov=. --cov-config=.coveragerc --cov-report=xml:coverage.xml -q
if errorlevel 1 (
    echo Tests failed. Generating metrics anyway for diagnosis.
)

echo [2/3] Generating metrics...
%PYEXE% scripts\generate_metrics.py
if errorlevel 1 (
    echo Metrics generation failed. Cannot run cycle without metrics.
    exit /b 1
)

echo [3/3] Running harness cycle...
metaharness run

echo Done.
