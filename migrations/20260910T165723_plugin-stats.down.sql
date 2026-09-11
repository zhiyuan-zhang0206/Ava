-- Reversibility: the table holds only derived runtime values, so dropping it
-- loses nothing a plugin cannot rewrite on its next refresh.

DROP TABLE IF EXISTS plugin_stats;
