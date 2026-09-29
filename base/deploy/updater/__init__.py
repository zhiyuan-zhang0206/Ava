"""Retained updater evidence: the host-local handoff and its recovery journal.

``handoff`` reads the updater handoff and clears exactly one generation;
``recovery`` holds the strict schemas of the retained recovery evidence,
including the ``LauncherTerminal`` records of the retired updater's hop ledger.
"""
