"""Agent-owned domain packages.

The shared agent contract (statuses, the exception hierarchy and the wire error
protocol) lives in `base/api_contracts/agent_contract.py`: it is a leaf that the
gateway wire and `cluster` both depend on, so it sits below this package. This
door re-exports nothing; import the member module you need.
"""
