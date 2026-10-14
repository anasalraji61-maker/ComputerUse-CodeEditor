@echo off
cd /d "%~dp0"
echo Real-model benchmark: spends real API credit (hard cap 1 USD). Press Ctrl+C to cancel.
py -m bench.run_bench --provider anthropic --runs 5 --max-usd 1.0 --ask-key
pause
