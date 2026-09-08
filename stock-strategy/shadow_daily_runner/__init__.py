"""Official EOD preparation and external runner for prospective shadow V0.1.

This package owns data ingestion and scheduling only.  It deliberately does
not import a broker SDK and does not contain an order or fill path.
"""

__all__ = ["__version__"]

__version__ = "0.1.0"
