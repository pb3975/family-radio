"""Self-test: the V1 keep-out check must fail when a printed feature blocks a plug.

    blender-5.2 -b --python-exit-code 1 --python cad/test_v1_keepouts.py

A centred adapter overlaps the power plug, so check_plugs must raise. A longer
power plug (34 mm instead of 27 mm) reaches down into the cable saddle, so
check_keepouts must raise. The real dimensions must pass.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import lib  # noqa: E402
import v1  # noqa: E402

lib.reset()
v1.check_keepouts([v1.back_panel()])

lib.reset()
v1.ADAPTER_DX = 0.0
try:
    v1.check_plugs()
except AssertionError as e:
    print(f"plug-overlap self-test passed: {e}")
else:
    raise SystemExit("plug check missed overlapping adapter and power-plug envelopes")
v1.ADAPTER_DX = -1.4

v1.PLUG = (11.0, 7.5, 34.0)
try:
    v1.check_keepouts([v1.back_panel()])
except AssertionError as e:
    assert "power-plug" in str(e), e
    print(f"keep-out self-test passed: {e}")
else:
    raise SystemExit("keep-out check missed a plug that hits the cable saddle")
