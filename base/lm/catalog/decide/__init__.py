"""Decision models: typed questions answered with probability distributions.

``types`` holds the value types and the provider-facing protocol (importable by the
provider contract); ``http`` the shared provider POST. The ``decide()`` entry point lives in
``base.lm.decision.call``: it needs the model catalog, so it sits above ``catalog``.
"""
