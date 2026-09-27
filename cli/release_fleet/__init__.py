"""Fleet release transition: one coordinator decides; units follow.

Today this package holds only the pure workload policy (slice FC-8): the
request policy block, the frozen cohort, the start-barrier and watch-window
verdicts, alert routing and known-good publication. The coordinator, fleet and
unit journals and the channel (slice FC-7) execute its decisions.
"""
