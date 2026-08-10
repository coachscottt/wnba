"""Sum-constraint test (build/05-minutes-model.md): simulated team minutes
sum to exactly 200 in regulation, and 225 only after an EXPLICIT simulated
overtime event. Run:  python -m tests.test_minutes_sum
"""

from __future__ import annotations

import sys

import numpy as np

from src.log import get_logger
from src.model_minutes import simulate_team

log = get_logger("test_minutes_sum")


def main() -> int:
    rng = np.random.default_rng(7)
    failures = 0
    for trial in range(100):
        n = rng.integers(8, 13)
        p_dnp = rng.uniform(0, 0.6, n)
        mu = rng.uniform(0.01, 0.2, n)
        m = simulate_team(p_dnp, mu, c=192.0, sims=50, rng=rng)
        if not np.allclose(m.sum(axis=1), 200.0, atol=1e-6):
            failures += 1
            log.info(f"  FAIL regulation trial {trial}: sums "
                     f"{np.unique(m.sum(axis=1).round(4))[:5]}")
        m_ot = simulate_team(p_dnp, mu, c=192.0, sims=50, rng=rng, ot_periods=1)
        if not np.allclose(m_ot.sum(axis=1), 225.0, atol=1e-6):
            failures += 1
            log.info(f"  FAIL overtime trial {trial}: sums "
                     f"{np.unique(m_ot.sum(axis=1).round(4))[:5]}")
        if not (m >= 0).all() or not (m_ot >= 0).all():
            failures += 1
            log.info(f"  FAIL trial {trial}: negative minutes")
    if failures == 0:
        log.info("PASS: 100 random team-games x 50 sims — regulation sums are "
                 "exactly 200, +25 only with an explicit overtime event, "
                 "no negative minutes")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
