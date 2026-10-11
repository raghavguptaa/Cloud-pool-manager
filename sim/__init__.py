"""Offline replay of the captured telemetry against the controller that produced it.

`sim.telemetry` reads the exports in `data/`, `sim.replay` feeds them to
`governor.fsm.decide()` and diffs the result against the hand-recorded timeline, and
`sim.figures` redraws `results/fig1_baseline.png` and `results/fig2_governor.png`.

Nothing in this package touches AWS, the network or the clock: `python -m sim.replay`
is reproducible on a laptop with no credentials.
"""
