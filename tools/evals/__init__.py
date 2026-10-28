"""The deterministic AI evaluation suite: golden cases (tests/evals/cases/*.yaml), a stand-in for the language model that replays scripted replies, a runner that asks each
case's question through the application's own entry points, and the metrics and the gate that CI enforces (tests/evals/thresholds.json).

    python -m tools.evals                  run every case and print the table
    python -m tools.evals --check          the same, and exit 1 when a threshold is missed (what CI runs)
    python -m tools.evals --show CASE-ID   print what the application really answered, for writing a new case

docs/EVALUATION.md explains how to add a case, run it on a real model and compare two models.
"""
