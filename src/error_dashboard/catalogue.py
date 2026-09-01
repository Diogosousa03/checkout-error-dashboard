"""Catalogue as config.

Loads and validates the error catalogue from config/catalogue.yaml into Pydantic
models: code -> {flow_phase, severity, description, cause}. Used to enrich the raw
Sentry counts with meaning (and to detect codes seen in Sentry but not catalogued).
"""

