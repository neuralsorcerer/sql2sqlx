# Copyright (c) Soumyadip Sarkar.
# All rights reserved.
#
# This source code is licensed under the Apache-style license found in the
# LICENSE file in the root directory of this source tree.

"""Enable ``python -m sql2sqlx`` as an alias for the ``sql2sqlx`` command."""

import sys

from sql2sqlx.cli import main

if __name__ == "__main__":
    sys.exit(main())
