"""Event declarations by domain.

Each module here declares one domain's payload schemas next to the events that carry
them, as an ``EVENTS`` mapping built with the ``base.events.vocabulary`` builders.
``load_events`` merges every module in this package, so adding an event edits only its
domain module, and a new domain is a new file here with nothing else to register.
"""
