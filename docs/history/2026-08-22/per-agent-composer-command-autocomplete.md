# Per-agent composer command autocomplete

The composer now requests the command catalog for the agent the user is
composing for. The no-selection state deliberately retains the global catalog
request so existing gateway-local behavior remains available outside an agent
context.

Successful catalogs are cached by agent id for the lifetime of the composer.
The cache is component-local so it cannot outlive that composer instance, and
failed requests are not cached so a later switch back retries. On an uncached
switch the visible list is cleared while the new request is pending, preventing
one agent's commands from appearing for another.
