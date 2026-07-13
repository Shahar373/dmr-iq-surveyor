# Missing center-frequency crash

The original batch report attempted to call `float()` on `None` when a recording did not expose a center frequency through container metadata.

The fix:

- treats missing numeric values as missing instead of coercing them;
- excludes missing values from consistency comparisons;
- records the center-frequency source;
- falls back to an SDRconnect filename suffix such as `_163671500HZ`;
- includes regression tests for both filename fallback and fully missing values.
