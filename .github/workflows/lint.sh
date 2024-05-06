#!/usr/bin/env bash
pip install -e ".[test,lint]"
mypy --install-types --non-interactive .
ruff .
black --check --diff .
mdformat --check *.md
pipx run 'validate-pyproject[all]' pyproject.toml
