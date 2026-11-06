No blocking findings in the current diff.

- All seven inventory SHA-256 hashes match, including the checker. Read-sharing locks remain held through receipt creation.
- The raw-module check runs before the terminal receipt. Failure throws before pending removal, preserving reconciliation state.
- `$LASTEXITCODE` is checked immediately after Python’s pipeline; `Out-Null` does not mask it.
- In-memory verification accepted the positive fixture and rejected missing events and unknown versions.

The gate test was reviewed statically; it writes temporary files, so I did not execute it. It tests the extracted gate, not full receipt/pending behavior.

`formal=false` remains diagnostic evidence only. No modifications, VM access, B contact, or additional agents/models.