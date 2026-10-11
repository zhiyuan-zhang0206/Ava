"""Decision models: typed questions answered with probability distributions.

``types`` holds the value types and the provider-facing protocol (importable by the
provider contract); ``call`` holds the ``decide()`` entry point, which needs the model
catalog and therefore cannot be re-exported here without an import cycle.
"""
