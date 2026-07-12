# Copyright (c) Soumyadip Sarkar.
# All rights reserved.
#
# This source code is licensed under the Apache-style license found in the
# LICENSE file in the root directory of this source tree.
"""Sphinx configuration for the sql2sqlx documentation.

Builds hand-written MyST Markdown pages plus an API reference generated
straight from the package's Google-style docstrings (autodoc + napoleon).
"""

import sys
from datetime import datetime, timezone
from pathlib import Path

DOCS_ROOT = Path(__file__).resolve().parent
REPO_ROOT = DOCS_ROOT.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from sql2sqlx.version import __version__  # noqa: E402

project = "sql2sqlx"
author = "Soumyadip Sarkar"
copyright = f"{datetime.now(timezone.utc):%Y}, Soumyadip Sarkar"
release = __version__
version = __version__

extensions = [
    "sphinx.ext.autodoc",
    "sphinx.ext.napoleon",
    "sphinx.ext.viewcode",
    "myst_nb",
]

autodoc_member_order = "bysource"
autodoc_typehints = "description"
napoleon_google_docstring = True
napoleon_numpy_docstring = False
myst_enable_extensions = ["colon_fence", "deflist"]
myst_heading_anchors = 3
nb_execution_mode = "off"
nb_execution_timeout = 120

templates_path = ["_templates"]
exclude_patterns = ["_build"]

html_theme = "furo"
html_title = f"sql2sqlx {__version__}"
html_static_path = []

html_show_sphinx = False
