"""Optional model-based judging stage.

Runs behind the deterministic rules in scoring.py, never instead of them.
Importing this package does not require the anthropic SDK; only constructing
a live Judge does.
"""
