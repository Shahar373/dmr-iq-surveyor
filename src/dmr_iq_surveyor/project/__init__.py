"""Project and Campaign identity: manifests, the claim a database carries, and
the binding that makes a running process refuse to touch anything else.

A Project is a long-lived analytical target with one SQLite database. A
Campaign is one collection round inside it. Neither is inferred: both are
declared in a file an operator wrote, and a database says in its own tables
which Project it belongs to.
"""

from __future__ import annotations
