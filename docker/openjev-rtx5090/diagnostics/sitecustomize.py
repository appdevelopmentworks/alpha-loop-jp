"""Emit Python stacks if OpenJev/vLLM startup stalls.

Loaded only by a diagnostic container with PYTHONPATH=/opt/alpha-loop-diagnostics.
"""

import faulthandler
import sys


faulthandler.enable(file=sys.stderr)
faulthandler.dump_traceback_later(75, repeat=True, file=sys.stderr)
