"""Measure the RSS cost of the heavy imports, per interpreter.

Why this exists: on a memory-constrained host every Python process pays for
its module graph, and this deployment runs SIX of them (flaskapp, db_api,
fetch_db_data, and one autotrade per account). An import that only one of
them actually needs is still paid for by all six.

Run on the deployment host — figures differ by platform and Python build:

    python tools/import_cost.py
"""
import subprocess
import sys

PROBES = {
    "scipy (stats + optimize)": "from scipy.stats import norm\n"
                                "from scipy.optimize import brentq",
    "pandas": "import pandas as pd",
    "numpy alone": "import numpy",
    "helpers.market_data": "from helpers import market_data",
    "helpers.positions_processing": "from helpers import positions_processing",
}

TEMPLATE = """
import os, sys
sys.path.insert(0, {root!r})


def rss_mb():
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024
    except Exception:
        pass
    try:
        import resource
        return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
    except Exception:
        # Windows has neither /proc nor `resource`. Return None rather than
        # 0.0 so the caller reports "unmeasurable here" instead of printing
        # a confident 0 MB that reads as "this import is free".
        return None


before = rss_mb()
{body}
after = rss_mb()
print("unmeasurable" if before is None or after is None
      else round(after - before, 1))
"""


def measure(body, root):
    code = TEMPLATE.format(body=body, root=root)
    out = subprocess.run([sys.executable, "-c", code],
                         capture_output=True, text=True)
    if out.returncode != 0:
        return None, (out.stderr.strip().splitlines() or ["?"])[-1]
    try:
        return float(out.stdout.strip()), None
    except ValueError:
        return None, out.stdout.strip()[:60]


def main():
    root = "."
    print(f"{sys.executable}\n")
    print(f"{'import':32} {'RSS cost':>10}")
    print("-" * 44)
    for label, body in PROBES.items():
        value, err = measure(body, root)
        if value is None:
            print(f"{label:32} {'n/a':>10}  ({err})")
        else:
            print(f"{label:32} {value:>7.1f} MB")
    print("\nMultiply by the number of Python processes running "
          "(flaskapp + db_api + fetch_db_data + one autotrade per account).")


if __name__ == "__main__":
    main()
