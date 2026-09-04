# Bootstrap observer transport encryption

The prepared ops observer runs before ordinary Settings initialization, but its
secret-bearing listener has the same transport boundary as the normal ops
daemon. The parent must project `AVA_TRANSPORT_ENCRYPTION` alongside the DB URL,
cluster secret, and ops port. The restricted entry verifies that projection
without importing Settings.

An off-box bind is refused before socket creation when the declaration is
missing, empty, `none`, or any value outside `tls`, `mtls`, and `overlay`.
Loopback/no-secret behavior is unchanged. This preserves the settings-free boot
boundary while preventing bearer credentials from being served over an
undeclared cleartext transport.
