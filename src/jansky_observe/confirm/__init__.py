"""Deterministic confirmation classifiers (plan §6, provenance §12.5)."""

from __future__ import annotations

from jansky_observe.confirm.baseline import (
    BaselineFit,
    db_to_linear,
    fit_baseline,
    linear_to_db,
)
from jansky_observe.confirm.classifier import (
    CLASSIFIER_NAME,
    CLASSIFIER_VERSION,
    ClassifierVerdict,
    averaged_spectrum,
    classify_capture_npz,
    classify_spectrum,
    running_classify,
)

__all__ = [
    "CLASSIFIER_NAME",
    "CLASSIFIER_VERSION",
    "BaselineFit",
    "ClassifierVerdict",
    "averaged_spectrum",
    "classify_capture_npz",
    "classify_spectrum",
    "db_to_linear",
    "fit_baseline",
    "linear_to_db",
    "running_classify",
]
