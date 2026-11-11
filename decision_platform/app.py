"""Composition root: wires config registry, store, dependency gateway and service.
Swapping SQLite for Postgres or a simulator for a real HTTP client happens
here and nowhere else."""

import json
import os

from .config import ConfigRegistry
from .dependencies import SIMULATORS, Clock, DependencyGateway
from .service import DecisionService
from .store import Store

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_CONFIG_DIR = os.path.join(ROOT, "configs")
DEFAULT_FIXTURES = os.path.join(ROOT, "examples", "dependency_fixtures.json")


def build_service(config_dir=DEFAULT_CONFIG_DIR, db_path=":memory:", fixtures_path=DEFAULT_FIXTURES,
                  clock=None, sleeper=None, breaker_threshold=5, breaker_reset_s=30.0):
    clock = clock or Clock()
    fixtures = {}
    if fixtures_path and os.path.exists(fixtures_path):
        with open(fixtures_path) as fh:
            fixtures = json.load(fh)
    gateway = DependencyGateway(clock=clock, sleeper=sleeper)
    for name, cls in SIMULATORS.items():
        gateway.register(cls(fixtures=fixtures.get(name, {})), breaker_threshold, breaker_reset_s)
    return DecisionService(ConfigRegistry(config_dir), Store(db_path), gateway, clock=clock)
