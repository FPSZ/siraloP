"""Shipped test support: in-memory fakes implementing each port.

Importable by both our own tests and teammates' tests. Not test-only code — it
is the reference double for every port, so keep it faithful to the frozen
contracts in agent/ports/.
"""
