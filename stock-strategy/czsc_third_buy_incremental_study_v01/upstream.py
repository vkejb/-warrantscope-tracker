from __future__ import annotations

import ast
import hashlib
import importlib
import os
from pathlib import Path
import subprocess
import sys

from .config import CFG, Config


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _function_source(source: str, function_name: str) -> str:
    tree = ast.parse(source)
    node = next(
        (item for item in tree.body if isinstance(item, ast.FunctionDef) and item.name == function_name),
        None,
    )
    if node is None:
        raise RuntimeError(f"upstream signal function is absent: {function_name}")
    segment = ast.get_source_segment(source, node)
    if not segment:
        raise RuntimeError("could not extract exact upstream signal definition")
    return segment


def load_upstream(upstream_root: Path, dependency_root: Path | None, cfg: Config = CFG):
    """Load and verify the exact upstream package; never reimplement the signal."""

    upstream_root = upstream_root.resolve()
    source_path = upstream_root / cfg.upstream_source_path
    source_bytes = source_path.read_bytes()
    if _sha256_bytes(source_bytes) != cfg.upstream_source_sha256:
        raise RuntimeError("upstream cxt.py hash mismatch")
    source = source_bytes.decode("utf-8")
    definition = _function_source(source, cfg.signal_function)
    if _sha256_bytes(definition.encode()) != cfg.signal_definition_sha256:
        raise RuntimeError("upstream signal definition hash mismatch")
    commit = subprocess.run(
        ["git", "-C", str(upstream_root), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    if commit != cfg.upstream_commit:
        raise RuntimeError(f"upstream commit mismatch: {commit}")

    if dependency_root is not None:
        sys.path.insert(0, str(dependency_root.resolve()))
    sys.path.insert(0, str(upstream_root))
    os.environ.setdefault("CZSC_HOME", "/private/tmp/czsc-third-buy-home")
    os.environ.setdefault("MPLCONFIGDIR", "/private/tmp/czsc-third-buy-mpl")
    os.environ.setdefault("czsc_welcome", "0")
    czsc = importlib.import_module("czsc")
    if str(czsc.__version__) != cfg.upstream_version:
        raise RuntimeError(f"upstream package version mismatch: {czsc.__version__}")
    function = getattr(importlib.import_module("czsc.signals.cxt"), cfg.signal_function)
    return {
        "CZSC": getattr(czsc, "CZSC"),
        "RawBar": getattr(czsc, "RawBar"),
        "Freq": getattr(czsc, "Freq"),
        "signal": function,
    }, {
        "repo": cfg.upstream_repo,
        "version": cfg.upstream_version,
        "commit": commit,
        "source_path": cfg.upstream_source_path,
        "source_sha256": cfg.upstream_source_sha256,
        "signal_function": cfg.signal_function,
        "signal_definition_sha256": cfg.signal_definition_sha256,
        "signal_logic_modified": False,
        "czsc_defaults": {"min_bi_len": 6, "max_bi_num": 50, "bi_change_th": 1.0},
    }
