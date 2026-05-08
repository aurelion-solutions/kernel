# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""File-based loader for Lens policy cartridges.

Reads YAML manifests from cartridges/lens/ and returns typed CartridgeManifest
objects. Pure I/O — no engine dependencies.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import ValidationError
import yaml
from src.inventory.policy.cartridges.schemas import CartridgeManifest


class CartridgeLoadError(Exception):
    """Raised when a cartridge YAML file is missing, malformed, or invalid."""


class FileCartridgeLoader:
    def load_file(self, path: Path) -> CartridgeManifest:
        try:
            with path.open('rb') as fh:
                raw = yaml.safe_load(fh)
        except FileNotFoundError as exc:
            raise CartridgeLoadError(f'Cartridge file not found: {path}') from exc
        except yaml.YAMLError as exc:
            raise CartridgeLoadError(f'Failed to parse cartridge YAML {path}: {exc}') from exc

        if not isinstance(raw, dict):
            raise CartridgeLoadError(f'{path.name}: cartridge must be a YAML mapping, got {type(raw).__name__}')

        try:
            return CartridgeManifest.model_validate(raw)
        except ValidationError as exc:
            raise CartridgeLoadError(f'{path.name}: invalid cartridge manifest: {exc}') from exc

    def load_dir(self, path: Path) -> list[CartridgeManifest]:
        if not path.exists():
            raise CartridgeLoadError(f'Cartridge directory not found: {path}')
        return [self.load_file(p) for p in sorted(path.rglob('*.yaml'))]
