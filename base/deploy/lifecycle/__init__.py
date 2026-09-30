"""Local unit lifecycle state: serving generations, service set, status journal, locks.

``start_serving`` holds the generation-owned serving state of one ``ava start``;
``service_selection`` the durable desired service set; ``launch_failures`` the
sessions the last start could not launch; ``status_journal`` the durable status
of hosted pause / stop / restart operations; ``home_lifecycle_locks`` the home
mutex shared by local start/stop/pause.
"""
