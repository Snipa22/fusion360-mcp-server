# addon/server/tool_templates.py
"""
Canonical geometry templates for each Fusion 360 tool type.
Extracted live from Fusion's documentToolLibrary sample tools.
Keys match what Tool.createFromJson expects exactly — do not rename them.
"""
import sys as _sys, os as _os
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))

GEOMETRY_TEMPLATES = {
    "drill": {
        "CSP": False, "DC": 1.0, "HAND": True, "LB": 25.0, "LCF": 15.0,
        "NOF": 2, "NT": 1, "OAL": 30.0, "RE": 0, "SFDM": 1.0, "SIG": 118,
        "TP": 0, "assemblyGaugeLength": 25.0, "shoulder-length": 22.0,
        "thread-profile-angle": 60, "tip-diameter": 0, "tip-length": 0, "tip-offset": 0,
    },
    "flat end mill": {
        "CSP": False, "DC": 1.0, "HAND": True, "LB": 10.0, "LCF": 3.0,
        "NOF": 2, "NT": 1, "OAL": 50.0, "RE": 0, "SFDM": 1.0, "TA": 0, "TP": 0,
        "assemblyGaugeLength": 10.0, "shoulder-diameter": 1.0, "shoulder-length": 8.0,
        "thread-profile-angle": 60, "tip-diameter": 0, "tip-length": 0, "tip-offset": 0,
    },
    "ball end mill": {
        "CSP": False, "DC": 1.0, "HAND": True, "LB": 10.0, "LCF": 3.0,
        "NOF": 2, "NT": 1, "OAL": 50.0, "RE": 0.5, "SFDM": 1.0, "TA": 0, "TP": 0,
        "assemblyGaugeLength": 10.0, "shoulder-diameter": 1.0, "shoulder-length": 8.0,
        "thread-profile-angle": 60, "tip-diameter": 0, "tip-length": 0, "tip-offset": 0,
    },
    "bull nose end mill": {
        "CSP": False, "DC": 6.0, "HAND": True, "LB": 24.0, "LCF": 18.0,
        "NOF": 4, "NT": 1, "OAL": 36.0, "RE": 0.75, "SFDM": 6.0, "TA": 0, "TP": 0,
        "assemblyGaugeLength": 24.0, "shoulder-diameter": 6.0, "shoulder-length": 21.0,
        "thread-profile-angle": 60, "tip-diameter": 4.5, "tip-length": 0, "tip-offset": 0,
    },
    "chamfer mill": {
        "CSP": False, "DC": 12.0, "HAND": True, "LB": 36.0, "LCF": 18.0,
        "NOF": 2, "NT": 1, "OAL": 72.0, "RE": 0, "SFDM": 12.0, "TA": 45, "TP": 0,
        "assemblyGaugeLength": 36.0, "shoulder-diameter": 12.0, "shoulder-length": 27.0,
        "thread-profile-angle": 60, "tip-diameter": 0, "tip-length": 0, "tip-offset": 0,
    },
    "face mill": {
        "CSP": False, "DC": 50.0, "DCX": 50.0, "HAND": True, "LB": 48.0, "LCF": 12.0,
        "NOF": 6, "NT": 1, "OAL": 96.0, "RE": 0, "SFDM": 50.0, "TA": 0, "TP": 0,
        "assemblyGaugeLength": 48.0, "shoulder-diameter": 50.0, "shoulder-length": 12.0,
        "thread-profile-angle": 60, "tip-diameter": 50.0, "tip-length": 0, "tip-offset": 0,
        "upper-radius": 0,
    },
    "tapered mill": {
        "CSP": False, "DC": 12.0, "HAND": True, "LB": 72.0, "LCF": 24.0,
        "NOF": 4, "NT": 1, "OAL": 144.0, "RE": 2.5, "SFDM": 20.46, "TA": 10, "TP": 0,
        "assemblyGaugeLength": 72.0, "shoulder-diameter": 20.46, "shoulder-length": 24.0,
        "thread-profile-angle": 60, "tip-diameter": 7.0, "tip-length": 0, "tip-offset": 0,
    },
    "radius mill": {
        "CSP": False, "DC": 12.0, "HAND": True, "LB": 36.0, "LCF": 12.0,
        "NOF": 4, "NT": 1, "OAL": 72.0, "RE": 6.0, "SFDM": 24.0, "TA": 0, "TP": 0,
        "assemblyGaugeLength": 36.0, "shoulder-diameter": 24.0, "shoulder-length": 12.0,
        "thread-profile-angle": 60, "tip-diameter": 0, "tip-length": 0, "tip-offset": 0,
    },
    "dovetail mill": {
        "CSP": False, "DC": 20.0, "HAND": True, "LB": 40.0, "LCF": 10.0,
        "NOF": 7, "NT": 1, "OAL": 80.0, "RE": 0, "SFDM": 10.0, "TA": 30, "TP": 0,
        "assemblyGaugeLength": 40.0, "shoulder-diameter": 8.45, "shoulder-length": 20.0,
        "thread-profile-angle": 60, "tip-diameter": 20.0, "tip-length": 0, "tip-offset": 0,
    },
    "lollipop mill": {
        "CSP": False, "DC": 12.0, "HAND": True, "LB": 36.0, "LCF": 10.8,
        "NOF": 5, "NT": 1, "OAL": 72.0, "RE": 6.0, "SFDM": 6.0, "TA": 0, "TP": 0,
        "assemblyGaugeLength": 36.0, "shoulder-diameter": 6.0, "shoulder-length": 10.8,
        "thread-profile-angle": 60, "tip-diameter": 0, "tip-length": 0, "tip-offset": 0,
    },
    "slot mill": {
        "CSP": False, "DC": 20.0, "HAND": True, "LB": 60.0, "LCF": 4.0,
        "NOF": 7, "NT": 1, "OAL": 120.0, "RE": 0, "SFDM": 6.67, "TA": 0, "TP": 0,
        "assemblyGaugeLength": 60.0, "shoulder-diameter": 6.67, "shoulder-length": 20.0,
        "thread-profile-angle": 60, "tip-diameter": 20.0, "tip-length": 0, "tip-offset": 0,
    },
    "thread mill": {
        "CSP": False, "DC": 10.0, "HAND": True, "LB": 50.0, "LCF": 20.0,
        "NOF": 2, "NT": 1, "OAL": 50.0, "RE": 0, "SFDM": 10.0, "TA": 0, "TP": 0,
        "TPN": 0, "TPX": 0,
        "assemblyGaugeLength": 50.0, "shoulder-diameter": 8.27, "shoulder-length": 30.0,
        "thread-profile-angle": 60, "thread-tip-type": "point",
        "tip-diameter": 10.0, "tip-length": 0, "tip-offset": 0,
    },
    "boring bar": {
        "CSP": False, "DC": 10.0, "HAND": True, "LB": 50.0, "LCF": 20.0,
        "NOF": 1, "NT": 1, "OAL": 60.0, "RE": 0, "SFDM": 10.0, "SIG": 0, "TP": 0,
        "assemblyGaugeLength": 50.0, "shoulder-length": 30.0,
        "thread-profile-angle": 60, "tip-diameter": 0, "tip-length": 0, "tip-offset": 0,
    },
    "counter bore": {
        "CSP": False, "DC": 6.0, "HAND": True, "LB": 24.0, "LCF": 12.0,
        "NOF": 4, "NT": 1, "OAL": 36.0, "RE": 0, "SFDM": 4.8, "SIG": 0, "TP": 0,
        "assemblyGaugeLength": 24.0, "shoulder-length": 12.0,
        "thread-profile-angle": 60, "tip-diameter": 0, "tip-length": 0, "tip-offset": 0,
    },
    "counter sink": {
        "CSP": False, "DC": 12.0, "HAND": True, "LB": 24.0, "LCF": 5.4,
        "NOF": 3, "NT": 1, "OAL": 96.0, "RE": 0, "SFDM": 7.2, "SIG": 90, "TP": 0,
        "assemblyGaugeLength": 24.0, "shoulder-length": 8.1,
        "thread-profile-angle": 60, "tip-diameter": 1.2, "tip-length": 0, "tip-offset": 0,
    },
    "spot drill": {
        "CSP": False, "DC": 6.0, "HAND": True, "LB": 18.0, "LCF": 12.0,
        "NOF": 2, "NT": 1, "OAL": 36.0, "RE": 0, "SFDM": 6.0, "SIG": 120, "TP": 0,
        "assemblyGaugeLength": 18.0, "shoulder-length": 15.0,
        "thread-profile-angle": 60, "tip-diameter": 0, "tip-length": 0, "tip-offset": 0,
    },
    "center drill": {
        "CSP": False, "DC": 6.0, "HAND": True, "LB": 18.0, "LCF": 12.0,
        "NOF": 2, "NT": 1, "OAL": 36.0, "RE": 0, "SFDM": 6.0, "SIG": 118, "TA": 90, "TP": 0,
        "assemblyGaugeLength": 18.0, "shoulder-length": 15.0,
        "thread-profile-angle": 60, "tip-diameter": 2.0, "tip-length": 2.0, "tip-offset": 0,
    },
    "reamer": {
        "CSP": False, "DC": 6.0, "HAND": True, "LB": 48.0, "LCF": 18.0,
        "NOF": 6, "NT": 1, "OAL": 60.0, "RE": 0, "SFDM": 5.4, "SIG": 0, "TP": 0,
        "assemblyGaugeLength": 48.0, "shoulder-length": 18.0,
        "thread-profile-angle": 60, "tip-diameter": 0, "tip-length": 0, "tip-offset": 0,
    },
    "tap right hand": {
        "CSP": False, "DC": 10.0, "HAND": True, "LB": 50.0, "LCF": 20.0,
        "NOF": 3, "NT": 1, "OAL": 50.0, "RE": 0, "SFDM": 10.0, "SIG": 0, "TP": 0,
        "assemblyGaugeLength": 50.0, "shoulder-length": 30.0,
        "thread-profile-angle": 60, "tip-diameter": 0, "tip-length": 0, "tip-offset": 0,
    },
    "tap left hand": {
        "CSP": False, "DC": 10.0, "HAND": True, "LB": 50.0, "LCF": 20.0,
        "NOF": 3, "NT": 1, "OAL": 50.0, "RE": 0, "SFDM": 10.0, "SIG": 0, "TP": 0,
        "assemblyGaugeLength": 50.0, "shoulder-length": 30.0,
        "thread-profile-angle": 60, "tip-diameter": 0, "tip-length": 0, "tip-offset": 0,
    },
    "probe": {
        "CSP": False, "DC": 5.0, "HAND": True, "LB": 20.0, "LCF": 10.0,
        "NOF": 0, "NT": 1, "OAL": 20.0, "RE": 2.5, "SFDM": 4.0, "TA": 0, "TP": 0,
        "assemblyGaugeLength": 20.0, "shoulder-length": 10.0,
        "thread-profile-angle": 60, "tip-diameter": 0, "tip-length": 0, "tip-offset": 0,
    },
    # Circle segment family — adds profile-radius, axial-distance, lower/upper-radius
    "circle segment barrel": {
        "CSP": False, "DC": 12.0, "HAND": True, "LB": 72.0, "LCF": 24.0,
        "NOF": 3, "NT": 1, "OAL": 72.0, "RE": 0, "SFDM": 6.0, "TA": 0, "TP": 0,
        "assemblyGaugeLength": 72.0, "axial-distance": 12.0, "lower-radius": 0,
        "profile-radius": 48.0, "shoulder-diameter": 6.0, "shoulder-length": 60.0,
        "thread-profile-angle": 60, "tip-diameter": 12.0, "tip-length": 0, "tip-offset": 0,
        "upper-radius": 0,
    },
    "circle segment lens": {
        "CSP": False, "DC": 10.0, "HAND": True, "LB": 40.0, "LCF": 20.0,
        "NOF": 3, "NT": 1, "OAL": 40.0, "RE": 0, "SFDM": 10.0, "TA": 0, "TP": 0,
        "assemblyGaugeLength": 40.0, "axial-distance": 0, "lower-radius": 10.0,
        "profile-radius": 100.0, "shoulder-diameter": 10.0, "shoulder-length": 40.0,
        "thread-profile-angle": 60, "tip-diameter": 8.0, "tip-length": 0, "tip-offset": 0,
        "upper-radius": 0,
    },
    "circle segment oval": {
        "CSP": False, "DC": 6.0, "HAND": True, "LB": 60.0, "LCF": 19.76,
        "NOF": 3, "NT": 1, "OAL": 60.0, "RE": 0, "SFDM": 6.0, "TA": 0, "TP": 0,
        "assemblyGaugeLength": 60.0, "axial-distance": 0, "lower-radius": 0,
        "profile-radius": 90.0, "shoulder-diameter": 6.0, "shoulder-length": 30.0,
        "thread-profile-angle": 60, "tip-diameter": 6.0, "tip-length": 0, "tip-offset": 0,
        "upper-radius": 0,
    },
    "circle segment taper": {
        "CSP": False, "DC": 16.0, "HAND": True, "LB": 64.0, "LCF": 15.69,
        "NOF": 3, "NT": 1, "OAL": 64.0, "RE": 0, "SFDM": 16.0, "TA": 20, "TP": 0,
        "assemblyGaugeLength": 64.0, "axial-distance": 0, "lower-radius": 4.0,
        "profile-radius": 500.0, "shoulder-diameter": 16.0, "shoulder-length": 30.0,
        "thread-profile-angle": 60, "tip-diameter": 0, "tip-length": 0, "tip-offset": 0,
        "upper-radius": 8.0,
    },
    # Turning family — completely different schema
    "turning general": {
        "EPSR": 0, "INSD": 9.67, "LH": 0, "OAL": 30.0, "RA": 0,
        "RE": 0.8, "S": 3.97, "SC": "C", "SCTY": "T", "SIZE_SPECIFICATION_MODE": "CE",
        "TC": "M", "tool_grooveWidth": 0, "tool_insertWidth": 0, "tool_internalThread": False,
    },
    "turning boring": {
        "EPSR": 0, "INSD": 9.67, "LH": 0, "OAL": 30.0, "RA": 0,
        "RE": 0.8, "S": 3.97, "SC": "C", "SCTY": "T", "SIZE_SPECIFICATION_MODE": "CE",
        "TC": "M", "tool_grooveWidth": 0, "tool_insertWidth": 0, "tool_internalThread": False,
    },
    "turning grooving": {
        "EPSR": 0, "INSD": 10.0, "LH": 3.75, "OAL": 30.0, "RA": 0,
        "RE": 0.8, "S": 3.97, "SC": "groove square", "SCTY": "T",
        "TC": "M", "tool_grooveWidth": 3.0, "tool_insertWidth": 2.7, "tool_internalThread": False,
    },
    "turning threading": {
        "EPSR": 0, "INSD": 10.0, "LH": 0, "OAL": 30.0, "RA": 0,
        "RE": 0, "S": 3.97, "SC": "thread iso double", "SCTY": "T", "TC": "M",
        "TP": 0, "TPN": 0, "TPX": 0,
        "thread-profile-angle": 60, "thread-tip-radius": 0.144, "thread-tip-type": "round",
        "tool_grooveWidth": 0, "tool_insertWidth": 0, "tool_internalThread": False,
    },
    # Jet cutting family
    "waterjet": {"CW": 2.0, "JET_HEAD_CLEARANCE": 0, "JET_NOZZLE_DIAMETER": 0},
    "laser cutter": {"CW": 2.0, "JET_HEAD_CLEARANCE": 0, "JET_NOZZLE_DIAMETER": 0},
    "plasma cutter": {"CW": 2.0, "JET_HEAD_CLEARANCE": 0, "JET_NOZZLE_DIAMETER": 0.8},
    # Holder
    "holder": {},
}

# Identify which family each type belongs to
FAMILY_ROTARY = {
    "drill", "flat end mill", "ball end mill", "bull nose end mill", "chamfer mill",
    "face mill", "tapered mill", "radius mill", "dovetail mill", "lollipop mill",
    "slot mill", "thread mill", "boring bar", "counter bore", "counter sink",
    "spot drill", "center drill", "reamer", "tap right hand", "tap left hand", "probe",
    "circle segment barrel", "circle segment lens", "circle segment oval", "circle segment taper",
}
FAMILY_TURNING = {"turning general", "turning boring", "turning grooving", "turning threading"}
FAMILY_JET = {"waterjet", "laser cutter", "plasma cutter"}
