"""The root supervisor's local control contract, below every consumer.

Wire protocol and blocking Unix-socket client that both ends speak. The supervisor
itself (``services.supervision.ava_root``) serves this contract; lower layers such as the
start-serving gate use it without importing the service.
"""
