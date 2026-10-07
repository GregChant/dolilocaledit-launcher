# Security policy

Doli Local Edit Launcher `1.x` is the supported stable series. Security fixes are published in
its latest patch release; development versions `0.x` are no longer supported.

Please use GitHub private vulnerability reporting for security issues. Do not open a public issue
containing an exploit, a real document, an access token, a local recovery path or customer data.

Official Windows binaries must have a valid timestamped Authenticode signature issued to
**Experts Conseils Chanton**. Compare downloaded files with the release `SHA256SUMS`; never trust
an asset from another repository or an unversioned URL.

Official Linux archives and `SHA256SUMS` have detached OpenPGP signatures. Verify the public key
fingerprint through an independent channel before importing it; the expected fingerprint is:

```text
A51F BBAB 9A50 9277 1768  E839 8C95 07E9 997C 0569
```

Intermediate saves never finish an editing session. Automatic completion requires observed
document closure; unavailable or failed probes remain unknown. An explicit local completion
request preserves the working copy. No credential is passed to the document observer or dialog.
