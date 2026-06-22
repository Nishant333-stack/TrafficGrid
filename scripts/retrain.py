#!/usr/bin/env python3
"""Retrain models from events + officer feedback, then reload the prediction cache.

This is the runnable post-event learning loop: officer-reported actual durations
(captured via POST /events/{id}/feedback) are merged in as corrected training
labels, the severity/duration/risk artifacts are rebuilt into MODEL_DIR, and the
in-process model cache is cleared so predictions use the new models.

Usage:
    python scripts/retrain.py                 # from DATABASE_URL events + feedback
    python scripts/retrain.py --csv events.csv # local CSV fallback
    python scripts/retrain.py --no-feedback    # train on raw events only
"""
from __future__ import annotations

import argparse
import os


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", default=os.environ.get("TRAINING_CSV"))
    parser.add_argument("--no-feedback", action="store_true")
    args = parser.parse_args()

    from backend.ml.train_models import run_training

    summary = run_training(csv=args.csv, use_feedback=not args.no_feedback)

    try:
        from backend.ml.predict import reload_artifacts

        reload_artifacts()
    except Exception:
        pass

    print(f"Retrain complete: {summary}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
