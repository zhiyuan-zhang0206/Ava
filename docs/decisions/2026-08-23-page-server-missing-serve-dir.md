# Page-server missing serve directories

The page-server supervisor now treats an unavailable `serve_dir` as a page-row
degradation rather than a failed subprocess launch. It retries at 30, 60, 120,
240, then 300 seconds, alerts on the first observation, and closes the row on
the fifth observation (seven and a half minutes after the first failure).

This preserves recovery for a workspace that is briefly rebuilding without
restarting a process that cannot ever start, while the bounded close removes an
unusable page from both supervision and the frontend. The row is never reopened:
the serving agent must register a fresh page when its directory is available.
