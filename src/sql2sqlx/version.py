# Copyright (c) Soumyadip Sarkar.
# All rights reserved.
#
# This source code is licensed under the Apache-style license found in the
# LICENSE file in the root directory of this source tree.

"""Single source of truth for the package version.

Kept in its own module so every other module (including
:mod:`sql2sqlx.converter`, which stamps generated files) can import it
without circular-import risk, and so packaging tools can read it
statically.
"""

__version__ = "0.1.2"
