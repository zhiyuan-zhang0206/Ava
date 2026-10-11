"""Content-triggered recall: passive injection of memory-pool notes and of fitting skills.

`memory_recall` is the memory injection pass; `_memory_filter` is the relevance filter it runs
between retrieval and injection. `skill_recall` scores the skill index with one decision-model
request and renders the note that offers the best matches. `conversation_query` is the recent
conversation both passes key on. The door is docstring-only.
"""
