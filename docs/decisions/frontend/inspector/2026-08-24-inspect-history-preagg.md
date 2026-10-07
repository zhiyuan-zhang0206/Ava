# Inspector frozen-archive pre-aggregation

Cold seven-day and whole-life inspector reads had two independent costs: every
request rescanned the immutable PostgreSQL `events` archive for its duration
distribution, two node-duration sums, and lifecycle replay; and the retained
pre-cutover, unlabeled Loki slice needed several sequential fetches.

`agent_archive_stats` materializes each archived agent's duration distribution,
active and exec seconds, and lifecycle events at migration time. Whole-life
inspector reads use that row, including lifecycle replay for every requested
window, while windowed archive reads retain their raw fallback. The live
projected read raises its per-slice limit to 20000, fitting the retained
19k-row window into one Loki fetch rather than four.

The inspector response deadline is 30 seconds while the legacy Loki slice
remains in retention. That slice expires on 2026-08-30 11:10Z, after which the
slow legacy-read portion self-heals; each individual Loki query remains bounded
at eight seconds.
