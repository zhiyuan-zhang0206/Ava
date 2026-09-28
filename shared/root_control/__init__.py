"""The root supervisor's local control contract, below every consumer.

Wire protocol, blocking client, and the native local transports and custody-file
primitives that both ends speak. The supervisor itself (``services.ava_root``)
serves this contract; lower layers such as the start-serving gate and the
Windows terminal backend use it without importing the service.
"""
