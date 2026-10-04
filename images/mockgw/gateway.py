"""Placeholder. The real mock egress gateway is Task 9 (R3).

This file exists only so images/mockgw/Dockerfile can COPY it and the image can
be proved to build. It deliberately refuses to run: a gateway that started and
logged nothing would make an isolation test pass for the wrong reason, which is
precisely the failure mode Task 9's tests are built to catch.
"""

import sys

if __name__ == "__main__":
    sys.exit("reprobe mockgw: not implemented yet (Task 9). Refusing to start.")
