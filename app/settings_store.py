"""Shared QMS settings — stored as JSON at the SAME OneDrive path the Node app
uses (BSC Inspections/Config/settings.json), so both apps read/write one source."""
import json

from . import graph
from .config import SETTINGS_PATH

DEFAULT_SETTINGS = {
    "inspectors_inward": ["Sathya", "Kumar", "Mahendran", "Dhanush"],
    "inspectors_quality": ["Sathya", "Kumar", "Mahendran", "Dhanush", "Vignesh"],
    "qc_shearing_default": "Vignesh",
    "mills": ["SAIL", "SAIL RSP", "SAIL BSP", "JSW", "TATA", "AMNS", "NMDC", "JSPL"],
    "vendors": {"SAIL": "", "NMDC": "", "RINL": "", "JSW": ""},
    "grades": ["E250", "E350 / ST52", "HSFQ450 / 450 / EQV", "HSFQ550 / 550 / EQV"],
    "complaint_emails": {
        "sales_raise_to": ["pdqc@bharatsteels.in", "kannan@bharatsteels.in", "gourav@bharatsteels.in"],
        "internal_close_to": ["info@bharatsteels.in", "gourav@bharatsteels.in", "kannan@bharatsteels.in"],
        "vendor_escalation_cc": ["pdqc@bharatsteels.in", "kannan@bharatsteels.in", "gourav@bharatsteels.in"],
        "resolved_to": ["info@bharatsteels.in", "pdqc@bharatsteels.in", "kannan@bharatsteels.in", "gourav@bharatsteels.in"],
    },
    "inward_emails": {
        "default_to": ["support@bharatsteels.in"],
        "wheels_india_to": ["support@bharatsteels.in", "kannan@bharatsteels.in"],
    },
    "rejection_types": ["WIDTH VARIATION", "LENGTH VARIATION", "DIAGONAL VARIATION", "LINE MARK",
                        "DENT MARK", "CORE LAMINATION", "D SCALE", "RUST", "PITTED", "BURR", "BREAKAWAY"],
    "sample_frequency": [
        {"min": 1, "max": 2, "samples": "1-2"},
        {"min": 3, "max": 20, "samples": 3},
        {"min": 21, "max": 50, "samples": 5},
        {"min": 51, "max": 150, "samples": 8},
        {"min": 151, "max": 300, "samples": 11},
        {"min": 301, "max": 450, "samples": 15},
    ],
    "calc_constants": {"HR": 7.89, "CHEQUERED": 8.1},
    "tolerance_profiles": {
        "wheels_india": {"width_under": 15, "width_over": 15, "width_threshold": 1500,
                         "thickness": 0.2, "length": 3, "diagonal": 3},
        "other": {"width_under": 20, "width_over": 25, "width_threshold": 1500,
                  "thickness": 0.5, "length": 10, "diagonal": 5},
    },
    "pdi_tolerance": {"thickness": 0.5, "width": 5, "length": 10},
}


async def load_settings() -> dict:
    merged = dict(DEFAULT_SETTINGS)
    try:
        token = await graph.get_token()
        text = await graph.read_text(token, SETTINGS_PATH)
        if text:
            merged.update(json.loads(text))
    except Exception as e:
        print("[settings-load]", e)
    return merged


async def save_settings(settings: dict) -> dict:
    """Merge into current settings and write back; returns the merged result."""
    current = await load_settings()
    merged = dict(current)
    merged.update(settings or {})
    token = await graph.get_token()
    # ensure the Config folder exists (upload to a path auto-creates parents on OneDrive)
    await graph.upload_file(token, SETTINGS_PATH,
                            json.dumps(merged, indent=2).encode("utf-8"), "application/json")
    return merged
