"""A task suite described by config."""

from __future__ import annotations

from dataclasses import dataclass

from .families import generate
from .model import Task


@dataclass(frozen=True)
class SuiteConfig:
    seed: int
    families: tuple[str, ...]
    tasks_per_family: int
    jobs_per_task: int
    n_invoices: int
    n_vendors: int
    max_lines: int
    first_invoice_number: int


def build_suite(cfg: SuiteConfig) -> tuple[Task, ...]:
    if cfg.tasks_per_family < 1:
        raise ValueError("tasks_per_family must be positive")
    return tuple(
        generate(
            family,
            i,
            cfg.seed,
            cfg.jobs_per_task,
            cfg.n_invoices,
            cfg.n_vendors,
            cfg.max_lines,
            cfg.first_invoice_number,
        )
        for family in cfg.families
        for i in range(cfg.tasks_per_family)
    )
