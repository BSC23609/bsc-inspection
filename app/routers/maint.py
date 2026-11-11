"""Maintenance (CMMS) module — in-house replacement for Opmaint, inside the QMS app.

Covers: an asset register, per-asset checklists (each checkpoint tagged with a
category — Hydraulic / Mechanical / Electrical), preventive-maintenance schedules
triggered by date interval AND/OR running-hours, category→staff assignment
(reusing the employees login, with Electrical varying by location), completion
logging (machine history), and WhatsApp reminders (due-today to the assignee,
overdue escalation to a supervisor) via WATI.

All tables are Neon and self-create on first use.
"""
import os
import json
import re
import base64
import calendar
import asyncio
from datetime import date, datetime, timedelta, time
from datetime import date as _dt_date   # alias for use where a param named `date` shadows it
from zoneinfo import ZoneInfo
from typing import Optional
from fastapi import APIRouter, Depends, Query, HTTPException
from fastapi.responses import JSONResponse, Response

from .. import db, wati, graph
from .. import config as _cfg
from .. import overdue_img
from ..auth import require_employee, current_employee

# Read config defensively so a slightly older app/config.py (missing one of the
# newer maintenance vars) can never crash the whole app at import time.
# WATI template names. Env/config still wins if set; otherwise use the approved
# templates created in WATI (Oct 2026): reminder to user, overdue digest, breakdown.
WATI_MAINT_DUE_TEMPLATE       = getattr(_cfg, "WATI_MAINT_DUE_TEMPLATE", "") or "bsc_maint_reminder"
# Overdue digest to admins is now a WhatsApp IMAGE-header template (like the daily
# stock report): the template header points at /maint/overdue-report.png and the
# body carries no variables. The old text template was bsc_pm_overdue.
WATI_MAINT_OVERDUE_TEMPLATE   = getattr(_cfg, "WATI_MAINT_OVERDUE_TEMPLATE", "") or "bsc_maint_overdue_report"
WATI_MAINT_BREAKDOWN_TEMPLATE = os.environ.get("WATI_MAINT_BREAKDOWN_TEMPLATE") or "bsc_breakdown_alert2"
MAINT_SUPERVISOR_MOBILE       = getattr(_cfg, "MAINT_SUPERVISOR_MOBILE", "")
AGENT_TOKEN                   = getattr(_cfg, "AGENT_TOKEN", "BSC@123")
# Token for the public overdue PNG (baked into the WhatsApp template's image URL). Defaults
# to AGENT_TOKEN so it works out of the box; set env OVERDUE_IMG_TOKEN to a URL-safe value
# (e.g. letters/digits, no '@') to avoid exposing AGENT_TOKEN in WATI's template store.
OVERDUE_IMG_TOKEN             = (os.environ.get("OVERDUE_IMG_TOKEN")
                                 or getattr(_cfg, "OVERDUE_IMG_TOKEN", "") or AGENT_TOKEN)
# WhatsApp/Meta caches a template's header image BY URL, so a FIXED header URL can make the
# overdue report replay an older image even though the app serves a fresh PNG. To defeat that,
# set the WATI bsc_maint_overdue_report template header to a send-time VARIABLE and put that
# placeholder's name here (env WATI_OVERDUE_IMG_PARAM, e.g. "image"). Each send then passes the
# live PNG URL with a unique &t= timestamp, so Meta fetches the current board every time. Left
# blank, the send behaves exactly as before (fixed-URL header — still cacheable).
WATI_OVERDUE_IMG_PARAM        = (os.environ.get("WATI_OVERDUE_IMG_PARAM")
                                 or getattr(_cfg, "WATI_OVERDUE_IMG_PARAM", "") or "")
ONEDRIVE_BASE                 = getattr(_cfg, "ONEDRIVE_BASE", "BSC Inspections")

router = APIRouter(prefix="/maint", tags=["maintenance"])
IST = ZoneInfo("Asia/Kolkata")
CATEGORIES = ["Hydraulic", "Mechanical", "Electrical"]
_ensured = False
# Bump this string whenever the ensure_tables() DDL changes, so one request after a
# deploy re-applies the schema and every later request skips it (big per-request speedup).
SCHEMA_VER = "2026-10-31g"   # bump when adding/altering a table so ensure_tables re-runs the DDL
MAINT_BUILD = "2026-11-05-v75-restore-open-option"   # bump on each deploy to confirm Vercel updated
ASSIGN_SEED_KEY = "assign_seed_v1"   # one-time load of per-checkpoint user assignments
NEW_MACHINES_SEED_KEY = "new_machines_seed_v1"   # one-time load of the 5 SHED-B machines (015,017,018,019,020)
FULL_RESEED_KEY = "full_reseed_from_xlsx_v1"   # one-time: rebuild ALL machines from the Oct-2026 checklist workbook
NEW_MACHINE_CODES = ["M/C-015", "M/C-017", "M/C-018", "M/C-019", "M/C-020"]

# Equipment master list (MASTER LIST OF EQUIPMENTS, Rev 0, 01-Jun-2024).
# Seeded once into maint_assets when the table is empty. Edit/add assets in the
# Assets screen afterwards — re-seeding never overwrites existing rows.
# (code, name, type, location, make, model, notes)
SEED_ASSETS = [
    ("M/C-001", "CTL-1-6MM TO 25 MM", "CTL Line", "A SHED", "WUXI-JAINFA", "", "Capacity: 6 MM TO 25 MM"),
    ("M/C-001-2", "CTL-2-1.6 TO 6 MM", "CTL Line", "A SHED", "WUXI-JAINFA", "", "Capacity: 1 MM TO 6 MM"),
    ("EOT-001", "EOT CRANE-15 TON", "EOT Crane", "A SHED", "PRABHA", "", "Capacity: 15 TON"),
    ("EOT-002", "EOT -CRANE-40 TON", "EOT Crane", "A SHED", "PRABHA", "", "Capacity: 40 TON"),
    ("M/C-003", "TRAN.TROLLEY-35 TON-AMSAK MAKE", "Transfer Trolley", "A SHED", "AMSAK CRANES", "", "Capacity: 35 TON"),
    ("M/C-004", "AIR COMPRESSOR-500 LTRS-ELGI MAKE", "Compressor", "A SHED", "ELGI", "", "Capacity: 500 LTR"),
    ("EOT-006", "EOT CRANE-15 TON-PRABA MAKE", "EOT Crane", "SHED B", "PRABHA", "", "Capacity: 15 TON"),
    ("EOT-003", "EOT CRANE-15 TON PRABA MAKE", "EOT Crane", "SHED F", "PRABHA", "", "Capacity: 15 TON"),
    ("EOT-004", "EOT CRANE-35 TON-AMSAKE MAKE", "EOT Crane", "SHED B", "AMSAK", "", "Capacity: 35 TON"),
    ("EOT-005", "EOT CRANE-MM CRANE-15 TON", "EOT Crane", "SHED C", "MM CRANES", "14 XW", "Capacity: 15 TON"),
    ("EOT-007", "EOT CRANE-15 TON-K2 CRANE", "EOT Crane", "E SHED", "K2 CRANES", "14XW", "Capacity: 15 TON"),
    ("EOT-008", "EOT CRANE 15 TON-K2 CRANE", "EOT Crane", "E SHED", "K2 CRNES", "", "Capacity: 15 TON"),
    ("E0T-009", "EOT CRANE 15 TON-K2 CRANE", "EOT Crane", "E SHED", "K2 CRANES", "", "Capacity: 15 TON"),
    ("M/C-005", "TMT BENDING M/C 8 MM TO 32 MM", "", "SHED C", "ARUNODYA M/C TOOLS", "", "Capacity: 8-32 MM"),
    ("M/C-006", "TMT COIL STRAIGTENING M/C", "", "SHED C", "ARUNODYA M/C TOOLS", "SRBM1", "Capacity: 8-12 MM"),
    ("M/C-007", "TMT COIL STARAIGHTENING &BENDING M/C 8 MM TO 12 MM", "", "SHED C", "ARUNODYA M/C TOOLS", "", "Capacity: 8-12 MM"),
    ("M/C-008", "TMT COIL STARAIGHTENING &BENDING M/C 8 MM TO 12 MM", "", "SHED C", "ARUNODYA M/C TOOLS", "", "Capacity: 8-12 MM"),
    ("M/C-009", "RO PLANT", "", "PUMPING STATION", "GSE", "", "Capacity: 250LTR/HOUR"),
    ("DG-002", "GEN-SET 125 KVA", "DG Set", "SUB STATION", "CUMMINS", "", "Capacity: 125 KVA"),
    ("DG-001", "GEN-SET 42 KVA", "DG Set", "WEIGHMENT", "ALG POWER", "", "Capacity: 42 KVA"),
    ("M/C-010", "WEIGHMENT M/C-1", "", "DESPATCH OFFICE", "", "", "Capacity: 100 TON"),
    ("M/C-011", "WEIGHMENT M/C-2", "", "DESPATCH OFFICE", "", "", "Capacity: 100 TON"),
    ("MC-001", "MOBILE CRANE - 14 TON", "Crane", "YARD", "ACE", "14 XW", "Capacity: 14 TON"),
    ("MC-002", "MOBILE CRANE- 14 TON", "Crane", "YARD", "ACE", "14 XW", "Capacity: 14 TON"),
    ("M/C-015", "SHEARING MACHINE 25MM", "Shearing", "SHED B", "ENERGYMISSION", "", "Capacity: 25MM"),
    ("M/C-016", "SHEARING MACHINE 3MM", "Shearing", "SHED B", "", "", "Capacity: 3MM"),
    ("M/C-017", "LEVELLER NACHINE", "Leveller", "SHED B", "", "", "Capacity: 25MM"),
    ("M/C-018", "IRON WORKER MACHINE", "Iron Worker", "SHED B", "", "", "Capacity: 25MM"),
    ("M/C-019", "TRANSFER TROLLEY", "Transfer Trolley", "SHED A", "", "INHOUSE", "Capacity: 32 TON"),
    ("M/C-020", "SEMI GANDRY CRANE", "Gantry Crane", "SHED B", "AMSAK CRANES PVT LTD", "AMSAK", "Capacity: 3 TON"),
    ("M/C-014", "EMBOSSING M/C (CTL-1)", "Embossing", "A SHED", "", "", ""),
]

# Per-machine checklists (Daily + Quarterly) from the machine checklist master.
# Seeded once into maint_checklists/checkpoints + a schedule each, when empty.
# (asset_code, kind, [ (checkpoint_text, input_type, spec/options) ])
SEED_CHECKLISTS = [
    ("DG-001", "Daily", [
        ("Check the Disel Level", "radio", "OK, Not OK"),
        ("Check the Crank case oil level", "radio", "OK, Not OK"),
        ("Check the oil pressure (Ideal Range: 5.2 - 6 Kg/cm2)", "value", "Ideal Range: 5.2 - 6 Kg/cm2"),
        ("Check the Water temperature (Ideal Range: 45 - 75 C)", "value", "Ideal Range: 45 - 75 C"),
        ("Check the Battery condition", "radio", "OK, Not OK"),
        ("Check the Alternator function", "radio", "OK, Not OK"),
    ]),
    ("DG-001", "Quarterly", [
        ("Check the engine oil and oil filter,to prevent sludge buildup", "radio", "OK, Not OK"),
        ("Check the coolant levels and inspect the radiator, holes for leaks", "radio", "No Leakage, Leakage"),
        ("Check the fuel systemt prevent clogs and ensure proper fuel flow", "radio", "OK, Not OK"),
        ("Check air intake and exhaust system for dirt and dust or clogging", "radio", "OK, Not OK"),
        ("Check the battery conditions for corrosion and also check the wiring system condition", "radio", "OK, Not OK"),
        ("Check the overall condition visually for the performance of the generator", "radio", "OK, Not OK"),
    ]),
    ("DG-002", "Daily", [
        ("Check the Disel Level", "radio", "OK, Not OK"),
        ("Check the Crank case oil level", "radio", "OK, Not OK"),
        ("Check the oil pressure (Ideal Range: 5.2 - 6 Kg/cm2)", "value", "Ideal Range: 5.2 - 6 Kg/cm2"),
        ("Check the Water temperature (Ideal Range: 45 - 75 C)", "value", "Ideal Range: 45 - 75 C"),
        ("Check the Battery condition", "radio", "OK, Not OK"),
        ("Check the Alternator function", "radio", "OK, Not OK"),
    ]),
    ("DG-002", "Quarterly", [
        ("Check the engine oil and oil filter,to prevent sludge buildup", "radio", "OK, Not OK"),
        ("Check the coolant levels and inspect the radiator, holes for leaks", "radio", "No Leakage, Leakage"),
        ("Check the fuel systemt prevent clogs and ensure proper fuel flow", "radio", "OK, Not OK"),
        ("Check air intake and exhaust system for dirt and dust or clogging", "radio", "OK, Not OK"),
        ("Check the battery conditions for corrosion and also check the wiring system condition", "radio", "OK, Not OK"),
        ("Check the overall condition visually for the performance of the generator", "radio", "OK, Not OK"),
    ]),
    ("E0T-009", "Quarterly", [
        ("Check the LT Drive Wheel condition", "radio", "OK, Not OK"),
        ("Check the LT,CT &Up down Drive Gear box condition", "radio", "OK, Not OK"),
        ("Check the LT drive plummer block condition and it's Bolt tightness", "radio", "Tightness OK, Tightness Not OK"),
        ("Check the Up & down Hoist Rope condition", "radio", "OK, Not OK"),
        ("Check the Lifting hook assymbly condition", "radio", "OK, Not OK"),
        ("Check all Electrical cable wire tightness", "radio", "Tightness OK, Tightness Not OK"),
        ("Check hydraulic thruster brake function", "radio", "OK, Not OK"),
        ("Check the Remote condition", "radio", "OK, Not OK"),
        ("Check the Cable Guide trolley condition", "radio", "OK, Not OK"),
        ("Check the Rope guide pulley condition", "radio", "OK, Not OK"),
    ]),
    ("EOT-001", "Quarterly", [
        ("Check the LT Drive Wheel condition", "radio", "OK, Not OK"),
        ("Check the LT,CT &Up down Drive Gear box condition", "radio", "OK, Not OK"),
        ("Check the LT drive plummer block condition and it's Bolt tightness", "radio", "Tightness OK, Tightness Not OK"),
        ("Check the Up & down Hoist Rope condition", "radio", "OK, Not OK"),
        ("Check the Lifting hook assymbly condition", "radio", "OK, Not OK"),
        ("Check all Electrical cable wire tightness", "radio", "Tightness OK, Tightness Not OK"),
        ("Check hydraulic thruster brake function", "radio", "OK, Not OK"),
        ("Check the Remote condition", "radio", "OK, Not OK"),
        ("Check the Cable Guide trolley condition", "radio", "OK, Not OK"),
        ("Check the Rope guide pulley condition", "radio", "OK, Not OK"),
    ]),
    ("EOT-002", "Quarterly", [
        ("Check the LT Drive Wheel condition", "radio", "OK, Not OK"),
        ("Check the LT,CT &Up down Drive Gear box condition", "radio", "OK, Not OK"),
        ("Check the LT drive plummer block condition and it's Bolt tightness", "radio", "Tightness OK, Tightness Not OK"),
        ("Check the Up & down Hoist Rope condition", "radio", "OK, Not OK"),
    ]),
    ("EOT-003", "Quarterly", [
        ("Check the LT Drive Wheel condition", "radio", "OK, Not OK"),
        ("Check the LT,CT &Up down Drive Gear box condition", "radio", "OK, Not OK"),
        ("Check the LT drive plummer block condition and it's Bolt tightness", "radio", "Tightness OK, Tightness Not OK"),
        ("Check the Up & down Hoist Rope condition", "radio", "OK, Not OK"),
        ("Check the Lifting hook assymbly condition", "radio", "OK, Not OK"),
        ("Check all Electrical cable wire tightness", "radio", "Tightness OK, Tightness Not OK"),
        ("Check hydraulic thruster brake function", "radio", "OK, Not OK"),
        ("Check the Remote condition", "radio", "OK, Not OK"),
        ("Check the Cable Guide trolley condition", "radio", "OK, Not OK"),
        ("Check the Rope guide pulley condition", "radio", "OK, Not OK"),
    ]),
    ("EOT-004", "Quarterly", [
        ("Check the LT Drive Wheel condition", "radio", "OK, Not OK"),
        ("Check the LT,CT &Up down Drive Gear box condition", "radio", "OK, Not OK"),
        ("Check the LT drive plummer block condition and it's Bolt tightness", "radio", "Tightness OK, Tightness Not OK"),
        ("Check the Up & down Hoist Rope condition", "radio", "OK, Not OK"),
        ("Check the Lifting hook assymbly condition", "radio", "OK, Not OK"),
        ("Check all Electrical cable wire tightness", "radio", "Tightness OK, Tightness Not OK"),
        ("Check hydraulic thruster brake function", "radio", "OK, Not OK"),
        ("Check the Remote condition", "radio", "OK, Not OK"),
        ("Check the Cable Guide trolley condition", "radio", "OK, Not OK"),
        ("Check the Rope guide pulley condition", "radio", "OK, Not OK"),
    ]),
    ("EOT-005", "Quarterly", [
        ("Check the LT Drive Wheel condition", "radio", "OK, Not OK"),
        ("Check the LT,CT &Up down Drive Gear box condition", "radio", "OK, Not OK"),
        ("Check the LT drive plummer block condition and it's Bolt tightness", "radio", "Tightness OK, Tightness Not OK"),
        ("Check the Up & down Hoist Rope condition", "radio", "OK, Not OK"),
        ("Check the Lifting hook assymbly condition", "radio", "OK, Not OK"),
        ("Check all Electrical cable wire tightness", "radio", "Tightness OK, Tightness Not OK"),
        ("Check hydraulic thruster brake function", "radio", "OK, Not OK"),
        ("Check the Remote condition", "radio", "OK, Not OK"),
        ("Check the Cable Guide trolley condition", "radio", "OK, Not OK"),
        ("Check the Rope guide pulley condition", "radio", "OK, Not OK"),
    ]),
    ("EOT-006", "Quarterly", [
        ("Check the LT Drive Wheel condition", "radio", "OK, Not OK"),
        ("Check the LT,CT &Up down Drive Gear box condition", "radio", "OK, Not OK"),
        ("Check the LT drive plummer block condition and it's Bolt tightness", "radio", "Tightness OK, Tightness Not OK"),
        ("Check the Up & down Hoist Rope condition", "radio", "OK, Not OK"),
        ("Check the Lifting hook assymbly condition", "radio", "OK, Not OK"),
        ("Check all Electrical cable wire tightness", "radio", "Tightness OK, Tightness Not OK"),
        ("Check hydraulic thruster brake function", "radio", "OK, Not OK"),
        ("Check the Remote condition", "radio", "OK, Not OK"),
        ("Check the Cable Guide trolley condition", "radio", "OK, Not OK"),
        ("Check the Rope guide pulley condition", "radio", "OK, Not OK"),
    ]),
    ("EOT-007", "Quarterly", [
        ("Check the LT Drive Wheel condition", "radio", "OK, Not OK"),
        ("Check the LT,CT &Up down Drive Gear box condition", "radio", "OK, Not OK"),
        ("Check the LT drive plummer block condition and it's Bolt tightness", "radio", "Tightness OK, Tightness Not OK"),
        ("Check the Up & down Hoist Rope condition", "radio", "OK, Not OK"),
        ("Check the Lifting hook assymbly condition", "radio", "OK, Not OK"),
        ("Check all Electrical cable wire tightness", "radio", "Tightness OK, Tightness Not OK"),
        ("Check hydraulic thruster brake function", "radio", "OK, Not OK"),
        ("Check the Remote condition", "radio", "OK, Not OK"),
        ("Check the Cable Guide trolley condition", "radio", "OK, Not OK"),
        ("Check the Rope guide pulley condition", "radio", "OK, Not OK"),
    ]),
    ("EOT-008", "Quarterly", [
        ("Check the LT Drive Wheel condition", "radio", "OK, Not OK"),
        ("Check the LT,CT &Up down Drive Gear box condition", "radio", "OK, Not OK"),
        ("Check the LT drive plummer block condition and it's Bolt tightness", "radio", "Tightness OK, Tightness Not OK"),
        ("Check the Up & down Hoist Rope condition", "radio", "OK, Not OK"),
        ("Check the Lifting hook assymbly condition", "radio", "OK, Not OK"),
        ("Check all Electrical cable wire tightness", "radio", "Tightness OK, Tightness Not OK"),
        ("Check hydraulic thruster brake function", "radio", "OK, Not OK"),
        ("Check the Remote condition", "radio", "OK, Not OK"),
        ("Check the Cable Guide trolley condition", "radio", "OK, Not OK"),
        ("Check the Rope guide pulley condition", "radio", "OK, Not OK"),
    ]),
    ("M/C-001", "Daily", [
        ("Condition of universal joint coupling bolt tighness in both leveller & oiling", "radio", "Tightness OK, Tightness Not OK"),
        ("Coil car 4 wheels fixing bolt tightness", "radio", "Tightness OK, Tightness Not OK"),
        ("Check the stacker hydraulic cylinders bracket fixing bolt condition", "radio", "Tightness OK, Tightness Not OK"),
        ("Fine leveller Automatic grease Lubrication checking", "radio", "Done, Not Done"),
        ("Stacking Mounting bolt tightness", "radio", "Tightness OK, Tightness Not OK"),
        ("Uncoiler Expand & collapse function", "radio", "OK, Not OK"),
        ("Uncoiler slide area cleaning & oiling", "radio", "Cleaning & Oiling OK, Not OK"),
        ("Coil pressing cylinder roller bush oiling", "radio", "Done, Not Done"),
        ("Check all the hydraulic lines for oil leakage", "radio", "No Leakage, Leakage"),
        ("check power pack oil temperature (Ideal Range: 45 - 55 C)", "value", "Ideal Range: 45 - 55 C"),
        ("Rough Lveller Motor current (Ideal Range 280 - 401 Amps)", "value", "Ideal Range 280 - 401 Amps"),
        ("Fine leveller motor current (Ideal Range 301 - 430 Amps)", "value", "Ideal Range 301 - 430 Amps"),
        ("Conveyor sprocket checking & Greasing", "radio", "OK, Not OK"),
        ("Shearing cylinders Mounting bolt tightness", "radio", "Tightness OK, Tightness Not OK"),
        ("Encoder coupling unit checking", "radio", "Condition OK, Not OK"),
    ]),
    ("M/C-001", "Quarterly", [
        ("Power pack strainer cleaning", "radio", "Done,Not done"),
        ("Coil car Hydromotor coupling nylon bush condition", "radio", "OK, Not OK"),
        ("Conveyor plummer block bolt tightness between fine leveller & shearing M/c", "radio", "Tightness OK, Tightness Not OK"),
        ("Check the Shearing m/c Hydraulic cylinder mounting bolt", "radio", "Tightness OK, Tightness Not OK"),
        ("Check shearing m/c hydraulic cylinder piston rod mounting condition", "radio", "Tightness OK, Tightness Not OK"),
        ("Loop table Mounting bolt & plummer block bolt tightness", "radio", "Tightness OK, Tightness Not OK"),
        ("Both leveller Mounting bolt tightness", "radio", "Tightness OK, Tightness Not OK"),
        ("Check the Excentric shaft bracket mounting bolt tightness", "radio", "Tightness OK, Tightness Not OK"),
        ("Check the All the foundation bolt Nut tightness", "radio", "Tightness OK, Tightness Not OK"),
        ("Check Motor and Gearbox – Leveller 1 condition", "radio", "OK, Not OK"),
        ("Check Motor and Gearbox – Leveller 2 condtion", "radio", "OK, Not OK"),
    ]),
    ("M/C-001-2", "Daily", [
        ("Condition of universal joint coupling bolt tighness in both leveller & oiling", "radio", "Tightness OK, Tightness Not OK"),
        ("Coil car 4 wheels fixing bolt tightness", "radio", "Tightness OK, Tightness Not OK"),
        ("Check the stacker hydraulic cylinders bracket fixing bolt condition", "radio", "Tightness OK, Tightness Not OK"),
        ("Fine leveller Manual grease Lubrication checking", "radio", "Done, Not done"),
        ("Stacking Mounting bolt tightness", "radio", "Tightness OK, Tightness Not OK"),
        ("Uncoiler Expand & collapse function", "radio", "OK, Not OK"),
        ("Uncoiler slide area cleaning & oiling", "radio", "Done,Not done"),
        ("Coil pressing cylinder roller bush oiling", "radio", "Done, Not done"),
        ("Check all the hydraulic lines for oil leakage", "radio", "No Leakage, Leakage"),
        ("Check power pack oil temperature (Ideal Range: 45 - 55C)", "value", "Ideal Range: 45 - 55C"),
        ("Rough Lveller Motor current (Ideal Range: 110 - 163.4 Amps)", "value", "Ideal Range: 110 - 163.4 Amps"),
        ("Fine leveller motor current (Ideal Range: 194 - 283.8 Amps)", "value", "Ideal Range: 194 - 283.8 Amps"),
        ("Excentric cam Mounting bolt tightness", "radio", "Tightness OK, Tightness Not OK"),
        ("Fly wheel greasing", "radio", "Done, Not done"),
        ("Conveyer Sprocket checking & greasing work", "radio", "Done, Not done"),
        ("Encoder coupling unit checking", "radio", "Done, Not done"),
    ]),
    ("M/C-001-2", "Quarterly", [
        ("Power pack strainer cleaning", "radio", "Done, Not done"),
        ("Coil car Checking working", "radio", "OK, Not OK"),
        ("Conveyor plummer block bolt tightness between fine leveller & shearing M/c", "radio", "Tightness OK, Tightness Not OK"),
        ("Check the Shearing m/c Hydraulic cylinder mounting bolt", "radio", "Tightness OK, Tightness Not OK"),
        ("Check Mechanical Shear All Bolt checking and Lubricating", "radio", "Done, Not done"),
        ("Loop table Mounting bolt & plummer block bolt tightness", "radio", "Tightness OK, Tightness Not OK"),
        ("Both leveller Mounting bolt tightness", "radio", "Tightness OK, Tightness Not OK"),
        ("Check All the foundation bolt Nut tightness", "radio", "Tightness OK, Tightness Not OK"),
        ("Check Motor and Gearbox – Leveller 1 condition", "radio", "OK, Not OK"),
        ("Check Motor and Gearbox – Leveller 2 condition", "radio", "OK, Not OK"),
    ]),
    ("M/C-003", "Daily", [
        ("Check Motor current (Ideal Range: 2 - 3.5 Amps)", "value", "Ideal Range: 2 - 3.5 Amps"),
        ("Check Drive coupling condition", "radio", "OK,Not OK"),
        ("Check the remote function", "radio", "OK,Not OK"),
        ("Check gear box oil level", "radio", "OK,Not OK"),
        ("Wheel bearing greasing to be done", "radio", "Done, not Done"),
        ("Monitor Noise level of Trolley in Function", "radio", "OK,Not OK"),
    ]),
    ("M/C-003", "Quarterly", [
        ("Check the condition of Mounting Rails", "radio", "OK, Not OK"),
        ("Check the Trolley wheel Greasing", "radio", "Done,Not Done"),
        ("Check the remote function", "radio", "OK, Not OK"),
        ("Check gear box oil level", "radio", "OK, Not OK"),
        ("Wheel bearing greasing to be done", "radio", "Done,Not Done"),
        ("Monitor Noise level of Trolley in Function", "radio", "OK, Not OK"),
    ]),
    ("M/C-005", "Daily", [
        ("Check the coil stand Condition", "radio", "OK, Not OK"),
        ("Check the Motor Belt condtion", "radio", "OK, Not OK"),
        ("Check the Oil Level in Gear box (Fill if required)", "radio", "OK, Not OK"),
        ("Check the Drive Rollers condition", "radio", "OK, Not OK"),
        ("Check the Stand welding condition", "radio", "OK, Not OK"),
        ("Check the Hydraulic Motor current (Ideal Range: 7 - 15 Amps)", "radio", ""),
        ("Check the Cover Plate provided on the Roler top", "radio", "Present, Not Present"),
        ("Check the Roller Bearing Condition", "radio", "OK, Not OK"),
        ("Check the Machine Base fixing Bolt condition", "radio", "Tightness OK, Tightness Not OK"),
        ("Check the Hydraulic oil lvel", "radio", "OK, Not OK"),
        ("Check oil leakage in Power pack Hydraulic valves", "radio", "No Leakage, Leakage"),
        ("Check the Blade condition and cleaning", "radio", "OK, Not OK"),
    ]),
    ("M/C-005", "Quarterly", [
        ("Check the Motor Belt condtion", "radio", "OK, Not OK"),
        ("Check the Oil Level in Gear box (Fill if required)", "radio", "OK, Not OK"),
        ("Check the Drive Rollers condition", "radio", "OK, Not OK"),
        ("Check the Stand welding condition", "radio", "OK, Not OK"),
        ("Check Rod Guiding Stand condition", "radio", "OK, Not OK"),
        ("Check the Machine Base fixing Bolt condition", "radio", "Tightness OK, Tightness Not OK"),
        ("Check the Hydraulic oil lvel", "radio", "OK, Not OK"),
        ("Gear box Sprocket cleaning", "radio", "Cleaning Done, Cleaning Not Done"),
    ]),
    ("M/C-006", "Daily", [
        ("Check the coil stand Condition", "radio", "OK, Not OK"),
        ("Check the Motor Belt condtion", "radio", "OK, Not OK"),
        ("Check the Oil Level in Gear box (Fill if required)", "radio", "OK, Not OK"),
        ("Check the Drive Rollers condition", "radio", "OK, Not OK"),
        ("Check the Stand welding condition", "radio", "OK, Not OK"),
        ("Check the Cover Plate provided on the Roler top", "radio", "Present, Not Present"),
        ("Check the Roller Bearing Condition", "radio", "OK, Not OK"),
        ("Check the Machine Base fixing Bolt condition", "radio", "Tightness OK, Tightness Not OK"),
        ("Check the Hydraulic oil lvel", "radio", "OK, Not OK"),
        ("Check oil leakage in Poer pack Hydraulic valves", "radio", "OK, Not OK"),
        ("Check the Blade condition and cleaning", "radio", "Cleaning Done, Cleaning Not Done"),
    ]),
    ("M/C-006", "Quarterly", [
        ("Check the Motor Belt condtion", "radio", "OK, Not OK"),
        ("Check the Oil Level in Gear box (Fill if required)", "radio", "OK, Not OK"),
        ("Check the Drive Rollers condition", "radio", "OK, Not OK"),
        ("Check the Stand welding condition", "radio", "OK, Not OK"),
        ("Rod Guiding Stand condition checking", "radio", "OK, Not OK"),
        ("Check the Machine Base fixing Bolt condition", "radio", "Tightness OK, Tightness Not OK"),
        ("Check the Hydraulic oil lvel", "radio", "OK, Not OK"),
        ("Gear box Sprocket cleaning", "radio", "Cleaning Done, Cleaning Not Done"),
    ]),
    ("M/C-007", "Daily", [
        ("Check the coil stand Condition", "radio", "OK, Not OK"),
        ("Check the Motor Belt condtion", "radio", "OK, Not OK"),
        ("Check the Oil Level in Gear box and fill if required", "radio", "OK, Not OK"),
        ("Check the Drive Rollers condition", "radio", "OK, Not OK"),
        ("Check the Stand welding condition", "radio", "OK, Not OK"),
        ("Check the Cover Plate provided on the Roler top", "radio", "Present, Not Present"),
        ("Check the Roller Bearing Condition", "radio", "OK, Not OK"),
        ("Check the Machine Base fixing Bolt condition", "radio", "Tightness OK, Tightness Not OK"),
        ("Check the Hydraulic oil lvel", "radio", "OK, Not OK"),
        ("check oil leakage in Power pack Hydraulic valves", "radio", "OK, Not OK"),
        ("Check the Blade condition and cleaning", "radio", "Cleaning Done, Cleaning Not Done"),
    ]),
    ("M/C-007", "Quarterly", [
        ("Check the Motor Belt condtion", "radio", "OK, Not OK"),
        ("Check the Oil Level in Gear box (Fill if required)", "radio", "OK, Not OK"),
        ("Check the Drive Rollers condition", "radio", "OK, Not OK"),
        ("Check the Stand welding condition", "radio", "OK, Not OK"),
        ("Rod Guiding Stand condition checking", "radio", "OK, Not OK"),
        ("Check the Machine Base fixing Bolt condition", "radio", "Tightness OK, Tightness Not OK"),
        ("Check the Hydraulic oil lvel", "radio", "OK, Not OK"),
        ("Gear box Sprocket cleaning", "radio", "Cleaning Done, Cleaning Not Done"),
    ]),
    ("M/C-008", "Daily", [
        ("Check the coil stand Condition", "radio", "OK, Not OK"),
        ("Check the Motor Belt condtion", "radio", "OK, Not OK"),
        ("Check the Oil Level in Gear box and fill if required", "radio", "OK, Not OK"),
        ("Check the Drive Rollers condition", "radio", "OK, Not OK"),
        ("Check the Stand welding condition", "radio", "OK, Not OK"),
        ("Check the Cover Plate provided on the Roler top", "radio", "Present, Not Present"),
        ("Check the Roller Bearing Condition", "radio", "OK, Not OK"),
        ("Check the Machine Base fixing Bolt condition", "radio", "Tightness OK, Tightness Not OK"),
        ("Check the Hydraulic oil lvel", "radio", "OK, Not OK"),
        ("check oil leakage in Poer pack Hydraulic valves", "radio", "OK, Not OK"),
        ("Check the Blade condition and cleaning", "radio", "Cleaning Done, Cleaning Not Done"),
    ]),
    ("M/C-008", "Quarterly", [
        ("Check the Motor Belt condtion", "radio", "OK, Not OK"),
        ("Check the Oil Level in Gear box and fill if required", "radio", "OK, Not OK"),
        ("Check the Drive Rollers condition", "radio", "OK, Not OK"),
        ("Check the Stand welding condition", "radio", "OK, Not OK"),
        ("Rod Guiding Stand condition checking", "radio", "OK, Not OK"),
        ("Check the Machine Base fixing Bolt condition", "radio", "Tightness OK, Tightness Not OK"),
        ("Check the Hydraulic oil lvel", "radio", "OK, Not OK"),
        ("Gear box Sprocket cleaning", "radio", "Cleaning Done, Cleaning Not Done"),
    ]),
    ("M/C-010", "Daily", [
        ("Check the Weigh bride clealiness", "radio", "Cleaning Done,Cleaning Not Done"),
        ("Daily Monitor Weight variation and inform AMC Person if exceed more than 80 kgs", "radio", "Done, Not Done"),
        ("Check the Invertor Battery condtion (Fill Distilled water if required)", "radio", "OK, Not OK"),
    ]),
    ("M/C-010", "Quarterly", [
        ("Check for cracks,movements or deteriotion of weighbridge", "radio", "OK, Not OK"),
        ("Inspect for any signs of dmage or corrosionto the supporting structure", "radio", "OK, Not OK"),
        ("Check for any physical damageto the cells,cable & junction boxes", "radio", "OK, Not OK"),
        ("Inspect load cell cables for wear particularly for surface mounted scales", "radio", "OK, Not OK"),
        ("Ensure all connections are are tight including load cell instalation kits and indicator connections", "radio", "OK, Not OK"),
        ("Verify the Mv outputt no load (Ideal Range: 0.0mV or +/-0.1mV)", "value", "Ideal Range: 0.0mV or +/-0.1mV"),
        ("Clean the junction box,terminals and earth links,ensuring firm terminations", "radio", "Cleaning Done, Cleaning Not Done"),
        ("Conduct functional test on load cell cables and check for polarity", "radio", "OK, Not OK"),
    ]),
    ("M/C-011", "Daily", [
        ("Check the Weigh bride clealiness", "radio", "Cleaning Done,Cleaning Not Done"),
        ("Daily Monitor Weight variation and inform AMC Person if exceed more than 80 kgs", "radio", "Done, Not Done"),
        ("Check the Invertor Battery condtion (Fill Distilled water if required)", "radio", "OK, Not OK"),
    ]),
    ("M/C-011", "Quarterly", [
        ("Check for cracks,movements or deteriotion of weighbridge", "radio", "OK, Not OK"),
        ("Inspect for any signs of dmage or corrosionto the supporting structure", "radio", "OK, Not OK"),
        ("Check for any physical damageto the cells,cable & junction boxes", "radio", "OK, Not OK"),
        ("Inspect load cell cables for wear particularly for surface mounted scales", "radio", "OK, Not OK"),
        ("Ensure all connections are are tight including load cell instalation kits and indicator connections", "radio", "OK, Not OK"),
        ("Verify the Mv outputt no load (Ideal Range: 0.0mV or +/-0.1mV)", "value", "Ideal Range: 0.0mV or +/-0.1mV"),
        ("Clean the junction box,terminals and earth links,ensuring firm terminations", "radio", "Cleaning Done, Cleaning Not Done"),
        ("Conduct functional test on load cell cables and check for polarity", "radio", "OK, Not OK"),
    ]),
    ("M/C-014", "Daily", [
        ("Keep the Machine Clean and Neat", "radio", "Cleaning Done, Cleaning Not Done"),
        ("Check the Inverter fuction", "radio", "OK, Not OK"),
        ("Check the Encoder Coupling Condition", "radio", "OK, Not OK"),
        ("Ensure Nozzle cleaning", "radio", "Cleaning Done, Cleaning Not Done"),
    ]),
    ("M/C-014", "Quarterly", [
        ("Inspect and lubricateall moving parts including gears,bearings,cams and linkages", "radio", "Done, Not Done"),
        ("Check the embossing rollers/cylnders for wear, scratches or misalignment", "radio", "OK,Not OK"),
        ("Verify pressure settings and adjust for uniform embossing depth", "radio", "Done, Not Done"),
        ("Tighten bolts and fasteners on structuraland dynamic parts", "radio", "Tightness OK, Tightness Not OK"),
        ("Inspect electrical wiring and connections", "radio", "Done, Not Done"),
        ("Test emergency stop and safety interlocks for responsiveness and complaince", "radio", "Done, Not Done"),
        ("Check hydraulic and pneumatics lines for leaks or wear", "radio", "OK,Not OK"),
        ("Check pressure levels and inspect valves and actuators", "radio", "OK,Not OK"),
        ("Inspect dies for damage or wear (Replace if necessary)", "radio", "Done, Not Done"),
    ]),
    ("M/C-015", "Daily", [
        ("Check hydraulic oil level.", "radio", "OK, Not OK"),
        ("Check hydraulic oil leakage from cylinders, hoses and fittings.", "radio", "OK, Not OK"),
        ("Check machine cleanliness and remove scrap pieces.", "radio", "OK, Not OK"),
        ("Check upper and lower blade condition.", "radio", "OK, Not OK"),
        ("Check blade clearance.", "radio", "OK, Not OK"),
        ("Lubricate all lubrication points..", "radio", "OK, Not OK"),
        ("Check hold-down cylinder operation.", "radio", "OK, Not OK"),
        ("Check back gauge movement.", "radio", "OK, Not OK"),
        ("Check side guide condition.", "radio", "OK, Not OK"),
        ("Check hydraulic pressure during operation.", "radio", "OK, Not OK"),
        ("Check emergency stop switch.", "radio", "OK, Not OK"),
        ("Check foot pedal operation.", "radio", "OK, Not OK"),
        ("Check electrical switches and indicator lamps.", "radio", "OK, Not OK"),
        ("Check abnormal vibration or noise.", "radio", "OK, Not OK"),
        ("Check safety guards and covers.", "radio", "OK, Not OK"),
        ("Clean work table and surrounding area.", "radio", "OK, Not OK"),
    ]),
    ("M/C-015", "Quarterly", [
        ("Check power pack hydraulic oil level", "radio", "OK, Not OK"),
        ("Inspect hydraulic oil condition and replace filter if due.", "radio", "OK, Not OK"),
        ("Check hydraulic gear coupling.", "radio", "OK, Not OK"),
        ("Check hydraulic pump operation and abnormal noise.", "radio", "OK, Not OK"),
        ("Check stamping/hold-down cylinder oil leakage.", "radio", "OK, Not OK"),
        ("Check stamping cylinder mounting bolts.", "radio", "OK, Not OK"),
        ("Check side guide roller bolts.", "radio", "OK, Not OK"),
        ("Check feeding roller bearing condition and lubrication.", "radio", "OK, Not OK"),
        ("Check air cylinder mounting bolts.", "radio", "OK, Not OK"),
        ("Check blade condition (wear, cracks and sharpness).", "radio", "OK, Not OK"),
        ("Check blade clearance and alignment.", "radio", "OK, Not OK"),
        ("Check all lubrication points and grease thoroughly.", "radio", "OK, Not OK"),
        ("Check hydraulic hoses, pipe fittings and seals.", "radio", "OK, Not OK"),
        ("Check hydraulic cylinder pins and bushes.", "radio", "OK, Not OK"),
        ("Check electrical panel terminal tightness.", "radio", "OK, Not OK"),
        ("Check motor foundation and mounting bolts.", "radio", "OK, Not OK"),
        ("Check limit switches and stroke settings.", "radio", "OK, Not OK"),
        ("Check back gauge alignment and movement.", "radio", "OK, Not OK"),
        ("Check machine foundation bolts.", "radio", "OK, Not OK"),
        ("Inspect machine frame for cracks or deformation.", "radio", "OK, Not OK"),
        ("Run machine under load and check cutting quality.", "radio", "OK, Not OK"),
        ("Check the back gauge ball screw and nut; replace if needed.", "radio", "OK, Not OK"),
        ("Photo upload (read only)", "radio", "OK, Not OK"),
        ("Signature saved", "radio", "OK, Not OK"),
        ("Voice upload (read only)", "radio", "OK, Not OK"),
    ]),
    ("M/C-017", "Daily", [
        ("Clean the lubrication of all the roller bush and hand pump", "radio", "OK, Not OK"),
        ("Clean the belt condition and chain condition", "radio", "OK, Not OK"),
        ("Measure the motor current", "radio", "OK, Not OK"),
        ("Clean the machine complete", "radio", "OK, Not OK"),
    ]),
    ("M/C-017", "Quarterly", [
        ("Check the gearbox oil level", "radio", "OK, Not OK"),
        ("Check the roller bush condition", "radio", "OK, Not OK"),
        ("Check the roller bush lubrication", "radio", "OK, Not OK"),
        ("Check all gear condition", "radio", "OK, Not OK"),
        ("Check gearbox motor and V-belt condition", "radio", "OK, Not OK"),
        ("Check drive chain condition", "radio", "OK, Not OK"),
        ("Check the drive belt condition", "radio", "OK, Not OK"),
        ("Inspect the reduction gearbox condition", "radio", "OK, Not OK"),
        ("Inspect the roller PB bush condition", "radio", "OK, Not OK"),
        ("Photo upload (read only)", "radio", "OK, Not OK"),
        ("Signature saved", "radio", "OK, Not OK"),
        ("Voice upload (read only)", "radio", "OK, Not OK"),
    ]),
    ("M/C-018", "Daily", [
        ("Check hydraulic oil level.", "radio", "OK, Not OK"),
        ("Check for hydraulic oil leakage from cylinders, hoses and fittings.", "radio", "OK, Not OK"),
        ("Check machine cleanliness. Remove dust and metal scrap.", "radio", "OK, Not OK"),
        ("Clean Punch & Die set.", "radio", "OK, Not OK"),
        ("Lubricate Punch & Die with oil.", "radio", "OK, Not OK"),
        ("Lubricate all grease points.", "radio", "OK, Not OK"),
        ("Check blade condition (Sharpness/Cracks).", "radio", "OK, Not OK"),
        ("Check blade clearance.", "radio", "OK, Not OK"),
        ("Inspect blade sharpness and blade clearance.", "radio", "OK, Not OK"),
        ("Check punch and die alignment.", "radio", "OK, Not OK"),
        ("Check hold-down clamp operation.", "radio", "OK, Not OK"),
        ("Check emergency stop switch.", "radio", "OK, Not OK"),
        ("Check foot pedal operation.", "radio", "OK, Not OK"),
        ("Check limit switches functioning.", "radio", "OK, Not OK"),
        ("Check abnormal vibration/ noise.", "radio", "OK, Not OK"),
        ("Check electrical cables and switches.", "radio", "OK, Not OK"),
        ("Check machine guards and safety covers.", "radio", "OK, Not OK"),
        ("Check work area cleanliness.", "radio", "OK, Not OK"),
    ]),
    ("M/C-019", "Quarterly", [
        ("Check motor current", "radio", "OK, Not OK"),
        ("Check drive coupling condition", "radio", "OK, Not OK"),
        ("Check cable tray functioning", "radio", "OK, Not OK"),
        ("Check gearbox oil level", "radio", "OK, Not OK"),
        ("Wheel bearing grease to be done", "radio", "OK, Not OK"),
        ("Monitor noise level of trolley in function", "radio", "OK, Not OK"),
        ("Photo upload (read only)", "radio", "OK, Not OK"),
        ("Signature saved", "radio", "OK, Not OK"),
        ("Voice upload (read only)", "radio", "OK, Not OK"),
    ]),
    ("M/C-020", "Quarterly", [
        ("Check the LT drive wheel condition", "radio", "OK, Not OK"),
        ("Check the gear box oil level condition", "radio", "OK, Not OK"),
        ("Check the LT drive plummer block condition and its bolt tightness", "radio", "OK, Not OK"),
        ("Check the up and down hoist rope condition", "radio", "OK, Not OK"),
        ("Check the lifting hook assembly condition", "radio", "OK, Not OK"),
        ("Check the remote condition", "radio", "OK, Not OK"),
        ("Check all wire cable wire insulation", "radio", "OK, Not OK"),
        ("Check the cable guide trolley condition", "radio", "OK, Not OK"),
        ("Check the guide pulley and rope condition", "radio", "OK, Not OK"),
    ]),
]


async def ensure_tables():
    global _ensured
    if _ensured:
        return
    # Fast path: when this exact schema version is already applied, skip the ~25
    # create/alter/index/seed statements and return after a single cheap SELECT.
    try:
        if await db.fetchval("SELECT mval FROM maint_meta WHERE mkey='maint_schema_ver'") == SCHEMA_VER:
            _ensured = True
            return
    except Exception:
        pass
    await db.execute("""CREATE TABLE IF NOT EXISTS maint_assets (
        id SERIAL PRIMARY KEY, code TEXT UNIQUE, name TEXT, atype TEXT, location TEXT,
        make TEXT, model TEXT, serial_no TEXT, running_hours NUMERIC DEFAULT 0,
        hours_updated_at TIMESTAMPTZ, active BOOLEAN DEFAULT true, notes TEXT,
        created_at TIMESTAMPTZ DEFAULT now())""")
    await db.execute("""CREATE TABLE IF NOT EXISTS maint_checklists (
        id SERIAL PRIMARY KEY, asset_id INT, name TEXT, active BOOLEAN DEFAULT true,
        created_at TIMESTAMPTZ DEFAULT now())""")
    await db.execute("""CREATE TABLE IF NOT EXISTS maint_checkpoints (
        id SERIAL PRIMARY KEY, checklist_id INT, seq INT, text TEXT, category TEXT,
        input_type TEXT DEFAULT 'check', unit TEXT, spec TEXT)""")
    await db.execute("""CREATE TABLE IF NOT EXISTS maint_schedules (
        id SERIAL PRIMARY KEY, asset_id INT, title TEXT, checklist_id INT,
        freq_days INT, hours_interval NUMERIC, category TEXT, location TEXT,
        assignee_emp_id INT, start_date DATE, last_done DATE, last_done_hours NUMERIC,
        active BOOLEAN DEFAULT true, created_at TIMESTAMPTZ DEFAULT now())""")
    # frequency model (Add Plan): freq_type + day_of_month + end_date + due_time
    for col, typ in (("freq_type", "TEXT"), ("day_of_month", "INT"), ("end_date", "DATE"),
                     ("due_time", "TEXT")):
        await db.execute(f"ALTER TABLE maint_schedules ADD COLUMN IF NOT EXISTS {col} {typ}")
    # every machine's schedule defaults to 09:00 (morning) unless a time was set
    try:
        await db.execute("UPDATE maint_schedules SET due_time='09:00' WHERE COALESCE(due_time,'')=''")
    except Exception as e:
        print("[maint] due_time backfill skipped:", e)
    await db.execute("""CREATE TABLE IF NOT EXISTS maint_holidays (
        id SERIAL PRIMARY KEY, hday DATE UNIQUE, name TEXT, created_at TIMESTAMPTZ DEFAULT now())""")
    await db.execute("""CREATE TABLE IF NOT EXISTS maint_leave (
        id SERIAL PRIMARY KEY, emp_id INT, from_date DATE, to_date DATE, reason TEXT,
        created_at TIMESTAMPTZ DEFAULT now())""")
    await db.execute("""CREATE TABLE IF NOT EXISTS maint_logs (
        id SERIAL PRIMARY KEY, asset_id INT, schedule_id INT, checklist_id INT,
        done_by TEXT, done_by_emp_id INT, done_at TIMESTAMPTZ DEFAULT now(),
        running_hours NUMERIC, status TEXT, results JSONB, remarks TEXT, next_due DATE)""")
    await db.execute("""CREATE TABLE IF NOT EXISTS maint_breakdowns (
        id SERIAL PRIMARY KEY, asset_id INT, problem TEXT, reported_by TEXT,
        reported_by_emp_id INT, assignee_emp_id INT, status TEXT DEFAULT 'open',
        raised_at TIMESTAMPTZ DEFAULT now(), closed_at TIMESTAMPTZ, closed_by TEXT,
        remarks TEXT)""")
    try:
        for col, typ in (("btype", "TEXT"), ("photos", "JSONB"), ("assignees", "JSONB"),
                         ("resolution", "TEXT"), ("wo_type", "TEXT"),
                         ("close_types", "JSONB"), ("close_causes", "JSONB")):
            await db.execute(f"ALTER TABLE maint_breakdowns ADD COLUMN IF NOT EXISTS {col} {typ}")
        # Self-heal the status column: older / migration-restored rows can carry a blank,
        # NULL or odd-cased status ('Open', ' open ', 'OPEN'), which made the Close button
        # never render (it only showed when status was exactly 'open') — so those work
        # orders could not be closed. Normalise to clean lowercase; blanks default to open.
        await db.execute(
            "UPDATE maint_breakdowns SET status='open' "
            "WHERE status IS NULL OR btrim(status)=''")
        await db.execute(
            "UPDATE maint_breakdowns SET status=lower(btrim(status)) "
            "WHERE status <> lower(btrim(status))")
        # PM completions can carry a work start/end time (entered on the Complete form),
        # so the Machine History Card shows a span and total hours like a breakdown does.
        for col in ("started_at", "ended_at"):
            await db.execute(f"ALTER TABLE maint_logs ADD COLUMN IF NOT EXISTS {col} TIMESTAMPTZ")
        await db.execute("CREATE INDEX IF NOT EXISTS ix_m_bd ON maint_breakdowns (status, raised_at DESC)")
        # maintenance staff per breakdown type — now MANY per type (auto-pick on the form)
        await db.execute("""CREATE TABLE IF NOT EXISTS maint_type_staff (
            category TEXT, emp_id INT)""")
        # drop the old one-per-type primary key if this DB still has it, allow many
        await db.execute("ALTER TABLE maint_type_staff DROP CONSTRAINT IF EXISTS maint_type_staff_pkey")
        await db.execute("CREATE UNIQUE INDEX IF NOT EXISTS ix_m_ts ON maint_type_staff (category, emp_id)")
        # per-asset ELECTRICAL maintenance users (Electrical breakdowns pick from these)
        await db.execute("""CREATE TABLE IF NOT EXISTS maint_asset_elec (
            asset_id INT, emp_id INT)""")
        await db.execute("CREATE UNIQUE INDEX IF NOT EXISTS ix_m_ae ON maint_asset_elec (asset_id, emp_id)")
        # daily per-machine running-hour / meter readings
        await db.execute("""CREATE TABLE IF NOT EXISTS maint_readings (
            id SERIAL PRIMARY KEY, asset_id INT, read_date DATE, running_hours NUMERIC,
            reading NUMERIC, note TEXT, taken_by TEXT, taken_by_emp_id INT,
            created_at TIMESTAMPTZ DEFAULT now())""")
        await db.execute("CREATE UNIQUE INDEX IF NOT EXISTS ix_m_rd ON maint_readings (asset_id, read_date)")
        # WhatsApp delivery log — every WATI send from the maintenance module + its outcome
        await db.execute("""CREATE TABLE IF NOT EXISTS maint_wati_log (
            id SERIAL PRIMARY KEY, sent_at TIMESTAMPTZ DEFAULT now(),
            template TEXT, phone TEXT, name TEXT, kind TEXT, result TEXT, detail TEXT)""")
        await db.execute("CREATE INDEX IF NOT EXISTS ix_m_wlog ON maint_wati_log (sent_at DESC)")
    except Exception as e:
        print("[maint] breakdown schema step skipped:", e)
    await db.execute("CREATE INDEX IF NOT EXISTS ix_m_cp ON maint_checkpoints (checklist_id, seq)")
    await db.execute("CREATE INDEX IF NOT EXISTS ix_m_log ON maint_logs (asset_id, done_at DESC)")
    # per-checkpoint user assignment + completion log (My Checklist)
    try:
        for col, typ in (("assigned_to", "TEXT"), ("assigned_emp_id", "INT")):
            await db.execute(f"ALTER TABLE maint_checkpoints ADD COLUMN IF NOT EXISTS {col} {typ}")
        await db.execute("""CREATE TABLE IF NOT EXISTS maint_checkpoint_done (
            id SERIAL PRIMARY KEY, checkpoint_id INT, emp_id INT, period_key TEXT,
            done_at TIMESTAMPTZ DEFAULT now(), note TEXT, result TEXT)""")
        await db.execute("ALTER TABLE maint_checkpoint_done ADD COLUMN IF NOT EXISTS result TEXT")
        await db.execute("CREATE UNIQUE INDEX IF NOT EXISTS ix_m_cpd ON maint_checkpoint_done (checkpoint_id, period_key)")
        await db.execute("CREATE INDEX IF NOT EXISTS ix_m_cp_emp ON maint_checkpoints (assigned_emp_id)")
    except Exception as e:
        print("[maint] checkpoint-assignment schema step skipped:", e)
    # seed the equipment master list once, only when no assets exist yet
    try:
        if not await db.fetchval("SELECT COUNT(*) FROM maint_assets"):
            for code, name, atype, loc, make, model, notes in SEED_ASSETS:
                await db.execute(
                    "INSERT INTO maint_assets (code,name,atype,location,make,model,serial_no,"
                    "running_hours,notes,hours_updated_at) VALUES ($1,$2,$3,$4,$5,$6,'',0,$7,now()) "
                    "ON CONFLICT (code) DO NOTHING",
                    code, name, atype, loc, make, model, notes)
    except Exception as e:
        print("[maint] asset seed skipped:", e)
    # seed per-machine checklists + Daily/Quarterly schedules once
    try:
        if not await db.fetchval("SELECT COUNT(*) FROM maint_checklists"):
            today = _today()
            for code, kind, pts in SEED_CHECKLISTS:
                aid = await db.fetchval("SELECT id FROM maint_assets WHERE lower(code)=lower($1)", code)
                if not aid:
                    aid = await db.fetchval(
                        "INSERT INTO maint_assets (code,name,active,hours_updated_at) "
                        "VALUES ($1,$1,true,now()) ON CONFLICT (code) DO NOTHING RETURNING id", code)
                    if not aid:
                        aid = await db.fetchval("SELECT id FROM maint_assets WHERE lower(code)=lower($1)", code)
                if not aid:
                    continue
                cid = await db.fetchval(
                    "INSERT INTO maint_checklists (asset_id,name) VALUES ($1,$2) RETURNING id",
                    aid, f"{kind} Checklist")
                for i, (text, typ, spec) in enumerate(pts):
                    await db.execute(
                        "INSERT INTO maint_checkpoints (checklist_id,seq,text,category,input_type,unit,spec) "
                        "VALUES ($1,$2,$3,'',$4,'',$5)", cid, i + 1, text, typ, spec)
                if kind.lower() == "daily":
                    ft, dom, fdays = "daily", None, 1
                else:
                    ft, dom, fdays = "quarterly", today.day, 90
                await db.execute(
                    "INSERT INTO maint_schedules (asset_id,title,checklist_id,freq_type,day_of_month,"
                    "freq_days,start_date,due_time,active) VALUES ($1,$2,$3,$4,$5,$6,$7,'09:00',true)",
                    aid, f"{kind} PM", cid, ft, dom, fdays, today)
    except Exception as e:
        print("[maint] checklist seed skipped:", e)
    # one-time: apply the Annual PM Schedule dates to each machine's quarterly plan
    try:
        await db.execute("CREATE TABLE IF NOT EXISTS maint_meta (mkey TEXT PRIMARY KEY, mval TEXT)")
        done = await db.fetchval("SELECT mval FROM maint_meta WHERE mkey=$1", ANNUAL_PLAN_KEY)
        if not done:
            await _apply_annual_plan()
            await db.execute(
                "INSERT INTO maint_meta (mkey,mval) VALUES ($1,'1') ON CONFLICT (mkey) DO NOTHING",
                ANNUAL_PLAN_KEY)
    except Exception as e:
        print("[maint] annual plan apply skipped:", e)
    # one-time: load the per-checkpoint user assignments from the shipped seed,
    # but only if no assignment exists yet (never overwrites a manual/Excel import)
    try:
        done = await db.fetchval("SELECT mval FROM maint_meta WHERE mkey=$1", ASSIGN_SEED_KEY)
        if not done:
            have = await db.fetchval(
                "SELECT 1 FROM maint_checkpoints WHERE assigned_emp_id IS NOT NULL LIMIT 1")
            if not have:
                await _seed_assignments()
            await db.execute(
                "INSERT INTO maint_meta (mkey,mval) VALUES ($1,'1') ON CONFLICT (mkey) DO NOTHING",
                ASSIGN_SEED_KEY)
    except Exception as e:
        print("[maint] assignment seed skipped:", e)
    # one-time: load the 5 SHED-B machines added Oct 2026 (015,017,018,019,020)
    # with their daily+quarterly checkpoints and per-checkpoint user assignments.
    # Runs once regardless of other machines already having assignments.
    try:
        done2 = await db.fetchval("SELECT mval FROM maint_meta WHERE mkey=$1", NEW_MACHINES_SEED_KEY)
        if not done2:
            await _seed_new_machines()
            await db.execute(
                "INSERT INTO maint_meta (mkey,mval) VALUES ($1,'1') ON CONFLICT (mkey) DO NOTHING",
                NEW_MACHINES_SEED_KEY)
    except Exception as e:
        print("[maint] new-machines seed skipped:", e)
    # NOTE: the full rebuild of all machines from the workbook is NOT done here on the
    # request path (too slow for Vercel's 10s limit). It runs via the admin endpoint
    # /maint/admin/reseed?token=... which uses one connection + batched inserts.
    # ensure every Daily checklist has an active daily plan (idempotent)
    await _ensure_daily_plans()
    # make sure the 5 new machines have a Quarterly PM plan (created only if missing)
    await _ensure_new_machine_quarterly()
    # link each Quarterly PM plan to its machine's Quarterly checklist (idempotent)
    await _ensure_quarterly_links()
    # stamp the schema version so later requests take the fast path above
    try:
        await db.execute(
            "INSERT INTO maint_meta (mkey,mval) VALUES ('maint_schema_ver',$1) "
            "ON CONFLICT (mkey) DO UPDATE SET mval=$1", SCHEMA_VER)
    except Exception as e:
        print("[maint] schema version stamp skipped:", e)
    _ensured = True


# --- Annual Preventive Maintenance Schedule (BSCQMS-PRD-005) ---------------
# Each machine's quarterly PM: day-of-month + the first (start) month; it then
# repeats every 3 months. Applied once to the quarterly schedules, at 09:00.
ANNUAL_PLAN_YEAR = 2026
ANNUAL_PLAN_KEY = "annual_plan_2026_v2"   # bump to re-apply after editing the plan
# code -> (day_of_month, start_month, name, location). All 32 machines from the
# Annual PM Schedule; the two duplicated codes were given unique codes:
#   TMT Coil Straightening cum Bending -> M/C-012 ; Transformer 800 KVA -> M/C-022
SCHEDULE_PLAN = {
    'DG-001': (15, 3, 'GEN SET 42 KVA', 'WEIGHMENT AREA'),
    'DG-002': (18, 1, 'GEN SET 320 KVA', 'NEAR OFFICE'),
    'EOT-001': (5, 1, 'EOT CRANE - 40 TON', 'SHED A'),
    'EOT-002': (3, 3, 'EOT CRANE - 15 TON', 'SHED A'),
    'EOT-003': (26, 2, 'EOT CRANE - 15 TON', 'SHED F'),
    'EOT-004': (8, 1, 'EOT CRANE - 15 TON', 'SHED B'),
    'EOT-005': (8, 3, 'EOT CRANE - 15 TON', 'SHED C'),
    'EOT-006': (8, 2, 'EOT CRANE - 35 TON', 'SHED B'),
    'EOT-007': (24, 3, 'EOT CRANE - 15 TON', 'SHED E'),
    'EOT-008': (27, 1, 'EOT CRANE - 15 TON', 'SHED E'),
    'EOT-009': (27, 3, 'EOT CRANE-15TON', 'SHED F'),
    'M/C-001': (2, 1, 'CTL-1 -6 MM TO 25 MM', 'SHED A'),
    'M/C-002': (2, 2, 'CTL-2 1.6 MM TO 6 MM', 'SHED A'),
    'M/C-003': (5, 2, 'TRANSFER TROLLEY', 'SHED A'),
    'M/C-005': (12, 1, 'TMT BENDING M/C-8MM to 32 MM', 'SHED C'),
    'M/C-006': (5, 3, 'AIR COMPRESSOR', 'SHED A'),
    'M/C-007': (11, 2, 'TMT COIL STRAIGHTENING M/C', 'SHED C'),
    'M/C-008': (15, 1, 'TMT COIL STRAIGHTENINGCUM BENDING', 'SHED C'),
    'M/C-009': (14, 2, 'RO PLANT', 'NEAR SHED B'),
    'M/C-010': (17, 2, 'WEIGHING M/C 100 TON', 'NEAR C SHED'),
    'M/C-011': (18, 3, 'WEIGHING M/C 100 TON', 'NEAR C SHED'),
    'M/C-012': (12, 3, 'TMT COIL STRAIGHTENING CUM BENDING', 'SHED C'),
    'M/C-013': (21, 3, 'EMBOSSING M/C', 'CTL-1'),
    'M/C-015': (30, 1, 'SHEARING MACHINE', 'SHED B'),
    'M/C-017': (3, 2, 'LEVELLER MACHINE', 'SHED B'),
    'M/C-018': (30, 3, 'IRON WORKER MACHINE', 'SHED B'),
    'M/C-019': (3, 1, 'TRANSFER TROLLEY', 'SHED A'),
    'M/C-020': (9, 2, 'SEMI-GANDRY CRANE', 'SHED B'),
    'M/C-021': (20, 2, 'BOOSTER PUMP', 'ADMIN OFFICE'),
    'M/C-022': (21, 1, 'TRANSFORMER 800 KVA', 'EB ROOM'),
    'MC-001': (24, 1, 'ESCORT -14TON', 'SHED A'),
    'MC-002': (23, 2, 'ESCORT - 14 TON', 'YARD'),
}


def _next_plan_start(day, sm, today):
    """Next upcoming quarterly date (day-of-month, months sm,sm+3,sm+6,sm+9) on/after today."""
    months = [((sm - 1 + 3 * k) % 12) + 1 for k in range(4)]
    best = None
    for y in (today.year, today.year + 1):
        for mo in months:
            dim = calendar.monthrange(y, mo)[1]
            d = date(y, mo, min(day, dim))
            if d >= today and (best is None or d < best):
                best = d
    return best or date(today.year, sm, min(day, calendar.monthrange(today.year, sm)[1]))


async def _apply_annual_plan():
    """Create/refresh every machine's quarterly PM straight from the Annual Schedule,
    at 09:00 — no Add Plan needed. Missing machines are created."""
    today = _today()
    made = 0
    for code, (day, sm, name, loc) in SCHEDULE_PLAN.items():
        try:
            aid = await db.fetchval("SELECT id FROM maint_assets WHERE lower(code)=lower($1)", code)
            if not aid:   # create the machine if it isn't in the register yet
                aid = await db.fetchval(
                    "INSERT INTO maint_assets (code,name,location,active,hours_updated_at) "
                    "VALUES ($1,$2,$3,true,now()) ON CONFLICT (code) DO NOTHING RETURNING id",
                    code, name, loc)
                if not aid:
                    aid = await db.fetchval("SELECT id FROM maint_assets WHERE lower(code)=lower($1)", code)
            if not aid:
                continue
            start = _next_plan_start(day, sm, today)   # schedule forward from today
            sid = await db.fetchval(
                "SELECT id FROM maint_schedules WHERE asset_id=$1 AND lower(COALESCE(freq_type,''))='quarterly' "
                "ORDER BY id LIMIT 1", aid)
            if sid:
                await db.execute(
                    "UPDATE maint_schedules SET freq_type='quarterly', day_of_month=$2, "
                    "freq_days=90, start_date=$3, due_time='09:00', active=true WHERE id=$1",
                    sid, day, start)
            else:   # no quarterly plan yet -> create one from the annual schedule
                await db.execute(
                    "INSERT INTO maint_schedules (asset_id,title,freq_type,day_of_month,freq_days,"
                    "start_date,due_time,active) VALUES ($1,'Quarterly PM','quarterly',$2,90,$3,'09:00',true)",
                    aid, day, start)
            made += 1
        except Exception as e:
            print("[maint] annual plan row skipped:", code, e)
    print(f"[maint] annual plan applied to {made} machines")


async def _ensure_daily_plans():
    """Give every machine that has a Daily checklist an active daily PM plan,
    so daily maintenance shows on the board and feeds reminders."""
    today = _today()
    made = 0
    try:
        rows = await db.fetch(
            "SELECT c.id AS cid, c.asset_id AS aid FROM maint_checklists c "
            "WHERE lower(COALESCE(c.name,'')) LIKE '%daily%'")
        for r in rows:
            aid, cid = r["aid"], r["cid"]
            if not aid:
                continue
            ex = await db.fetchval(
                "SELECT id FROM maint_schedules WHERE asset_id=$1 "
                "AND lower(COALESCE(freq_type,''))='daily' ORDER BY id LIMIT 1", aid)
            if ex:
                await db.execute(
                    "UPDATE maint_schedules SET checklist_id=COALESCE(checklist_id,$2), "
                    "freq_days=1, active=true WHERE id=$1", ex, cid)
                continue
            await db.execute(
                "INSERT INTO maint_schedules (asset_id,title,checklist_id,freq_type,freq_days,"
                "start_date,due_time,active) VALUES ($1,'Daily PM',$2,'daily',1,$3,'09:00',true)",
                aid, cid, today)
            made += 1
        if made:
            print(f"[maint] created {made} daily plans")
    except Exception as e:
        print("[maint] ensure daily plans skipped:", e)


def _today() -> date:
    return datetime.now(IST).date()


def _parse_local_dt(v):
    """Parse a datetime-local form value ('YYYY-MM-DDTHH:MM', local IST) into a
    tz-aware datetime, or None when blank/invalid."""
    v = (v or "").strip().replace("T", " ")
    if not v:
        return None
    try:
        return datetime.fromisoformat(v[:16]).replace(tzinfo=IST)
    except Exception:
        return None


def _num(v, default=None):
    try:
        if v is None or v == "":
            return default
        f = float(v)
        return int(f) if f == int(f) else f
    except Exception:
        return default


def _d(v):
    """Parse a YYYY-MM-DD string to date, else None."""
    if not v:
        return None
    if isinstance(v, date):
        return v
    try:
        return datetime.strptime(str(v)[:10], "%Y-%m-%d").date()
    except Exception:
        return None


def _dt(v):
    """Parse an HTML datetime-local value ('YYYY-MM-DDTHH:MM') to an IST datetime, else None."""
    if not v:
        return None
    if isinstance(v, datetime):
        return v
    s = str(v).strip().replace("T", " ")[:19]
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=IST)
        except Exception:
            continue
    return None


# ---------- recurrence + due computation ----------
FREQS = ("daily", "weekly", "monthly", "quarterly")
# Non-working days other than Sundays (ISO 'YYYY-MM-DD'), loaded from maint_holidays.
HOLIDAYS = set()


async def _refresh_holidays():
    global HOLIDAYS
    try:
        rows = await db.fetch("SELECT hday FROM maint_holidays")
        HOLIDAYS = {r["hday"].isoformat() for r in rows if r["hday"]}
    except Exception as e:
        print("[maint] holidays load skipped:", e)


async def _on_leave_ids(today: date) -> set:
    """Employee ids on leave today."""
    try:
        rows = await db.fetch(
            "SELECT emp_id FROM maint_leave WHERE from_date<=$1 AND to_date>=$1", today)
        return {r["emp_id"] for r in rows if r["emp_id"]}
    except Exception:
        return set()


def _supervisor_mobiles() -> list:
    """MAINT_SUPERVISOR_MOBILE may be a comma/space separated list of numbers."""
    raw = MAINT_SUPERVISOR_MOBILE or ""
    return [p.strip() for p in raw.replace(";", ",").split(",") if p.strip()]


async def _overdue_recipient_mobiles() -> list:
    """Mobiles of the user(s) chosen on the Schedules page to receive the overdue
    digest. Falls back to the legacy MAINT_SUPERVISOR_MOBILE if none are chosen."""
    try:
        raw = await db.fetchval("SELECT mval FROM maint_meta WHERE mkey='overdue_recipients'")
        ids = [int(x) for x in (raw or "").split(",") if x.strip().isdigit()]
        if not ids:
            return _supervisor_mobiles()
        rows = await db.fetch(
            "SELECT COALESCE(mobile,'') AS mobile FROM employees WHERE id = ANY($1::int[])", ids)
        mobs = [r["mobile"].strip() for r in rows if (r["mobile"] or "").strip()]
        return mobs or _supervisor_mobiles()
    except Exception:
        return _supervisor_mobiles()


async def _overdue_recipients() -> list:
    """Recipients of the overdue (bsc_pm_overdue) digest as {name, mobile}. This is
    EVERY admin user (with a mobile), merged with any recipients chosen on the
    Schedules page, de-duplicated by phone number."""
    out, seen = [], set()

    def _add(name, mobile):
        mob = (mobile or "").strip()
        if not mob:
            return
        key = wati.normalize_number(mob) or mob
        if key in seen:
            return
        seen.add(key)
        out.append({"name": (name or "").strip(), "mobile": mob})

    # recipients explicitly chosen on the Schedules page
    try:
        raw = await db.fetchval("SELECT mval FROM maint_meta WHERE mkey='overdue_recipients'")
        ids = [int(x) for x in (raw or "").split(",") if x.strip().isdigit()]
        if ids:
            for r in await db.fetch(
                    "SELECT COALESCE(name,'') AS name, COALESCE(mobile,'') AS mobile "
                    "FROM employees WHERE id = ANY($1::int[])", ids):
                _add(r["name"], r["mobile"])
    except Exception:
        pass

    # ALL admins — the overdue digest must reach every admin
    try:
        for r in await db.fetch(
                "SELECT COALESCE(name,'') AS name, COALESCE(mobile,'') AS mobile FROM employees "
                "WHERE COALESCE(is_admin,false)=true AND COALESCE(active,true)=true "
                "AND COALESCE(mobile,'')<>''"):
            _add(r["name"], r["mobile"])
    except Exception:
        pass

    if out:
        return out
    return [{"name": "", "mobile": m} for m in _supervisor_mobiles()]


def _overdue_img_params():
    """Parameters for the overdue image-header template.

    WhatsApp/Meta caches a template's header image BY URL, so repeatedly sending the SAME
    fixed URL lets it replay a stale copy of the board. When the WATI template header is set
    to a send-time VARIABLE — whose placeholder name is configured in WATI_OVERDUE_IMG_PARAM
    (e.g. "image") — we return that variable set to the live PNG URL plus a unique &t= epoch,
    so Meta fetches a fresh image on every send and the report can never lag the board.

    Left unconfigured (blank), we return [] and the template uses its fixed header URL exactly
    as before — so deploying this change alone never alters the current behaviour."""
    if not WATI_OVERDUE_IMG_PARAM:
        return []
    ts = int(datetime.now(IST).timestamp())
    url = (f"https://qms.bharatsteels.in/maint/overdue-report.png"
           f"?token={OVERDUE_IMG_TOKEN}&t={ts}")
    return {WATI_OVERDUE_IMG_PARAM: url}


def _is_offday(d):
    return d.weekday() == 6 or d.isoformat() in HOLIDAYS   # Sunday (6) or a holiday


# Plant working window for breakdown-hour (downtime) calculation.
WORK_START_H = 9      # 09:00
WORK_END_H = 19       # 19:00 (7 PM)


def _working_hours(start, end):
    """Hours between start and end counting ONLY the plant working window
    (09:00–19:00) on working days — Sundays and configured holidays contribute 0.
    start/end are tz-aware IST datetimes. Returns a float (0 if end<=start)."""
    if not start or not end:
        return None
    try:
        if end <= start:
            return 0.0
        start = start.astimezone(IST)
        end = end.astimezone(IST)
    except Exception:
        return None
    total = 0.0
    day = start.date()
    last = end.date()
    while day <= last:
        if not _is_offday(day):
            ws = datetime.combine(day, time(WORK_START_H, 0), tzinfo=IST)
            we = datetime.combine(day, time(WORK_END_H, 0), tzinfo=IST)
            s = max(start, ws)
            e = min(end, we)
            if e > s:
                total += (e - s).total_seconds() / 3600.0
        day = day + timedelta(days=1)
    return round(total, 2)


async def _downtime_working(frm_dt, to_dt, aid=0):
    """(total working-window downtime hours, closed count) for breakdowns CLOSED in
    [frm_dt, to_dt], using the 09:00–19:00 window and skipping Sundays/holidays."""
    await _refresh_holidays()
    rows = await db.fetch(
        "SELECT raised_at, closed_at FROM maint_breakdowns WHERE status='closed' "
        "AND closed_at IS NOT NULL AND closed_at>=$1 AND closed_at<=$2 AND ($3=0 OR asset_id=$3)",
        frm_dt, to_dt, aid)
    tot, n = 0.0, 0
    for r in rows:
        h = _working_hours(r["raised_at"], r["closed_at"])
        if h is not None:
            tot += h
            n += 1
    return round(tot, 2), n


def _roll_working(d):
    """Move a date off Sundays/holidays to the next working day."""
    if not d:
        return d
    for _ in range(31):
        if _is_offday(d):
            d = d + timedelta(days=1)
        else:
            break
    return d


def _add_months(d, n):
    m = d.month - 1 + n
    y = d.year + m // 12
    m = m % 12 + 1
    return date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


def _on_dom(y, m, dom):
    return date(y, m, min(int(dom or 1), calendar.monthrange(y, m)[1]))


def _raw_next(ft, dom, base):
    if ft == "daily":
        return base + timedelta(days=1)
    if ft == "weekly":
        return base + timedelta(days=7)
    if ft == "monthly":
        nb = _add_months(base, 1)
        return _on_dom(nb.year, nb.month, dom)
    if ft == "quarterly":
        nb = _add_months(base, 3)
        return _on_dom(nb.year, nb.month, dom)
    return None


def _first_due(ft, dom, start):
    if ft in ("daily", "weekly"):
        return start
    cand = _on_dom(start.year, start.month, dom)
    if cand < start:
        cand = _raw_next(ft, dom, cand)
    return cand


def next_due_date(sch, today):
    """Compute the next due date from the schedule's frequency model (with a
    legacy freq_days fallback), rolled off Sundays/holidays."""
    ft = (sch.get("freq_type") or "").lower()
    start = sch.get("start_date")
    last = sch.get("last_done")
    dom = sch.get("day_of_month") or (start.day if start else today.day)
    if ft in FREQS:
        nd = _raw_next(ft, dom, last) if last else _first_due(ft, dom, start or today)
    elif sch.get("freq_days"):
        base = last or start
        nd = (base + timedelta(days=int(sch["freq_days"]))) if base else today
    else:
        return None
    return _roll_working(nd)


def compute_due(sch: dict, asset_hours, today: date):
    ft = (sch.get("freq_type") or "").lower()
    is_daily = (ft == "daily") or (not ft and sch.get("freq_days") and int(sch["freq_days"]) == 1)
    end = sch.get("end_date")
    next_due = next_due_date(sch, today)
    date_due = bool(next_due and next_due <= today)
    overdue = bool(next_due and next_due < today)
    # schedule ended
    if end and next_due and next_due > end:
        date_due = overdue = False
    if end and today > end:
        date_due = overdue = False
    # daily maintenance never runs on Sundays
    if is_daily and today.weekday() == 6:
        date_due = overdue = False
    hours_due = False
    hrs_rem = None
    if sch.get("hours_interval"):
        last_h = float(sch.get("last_done_hours") or 0)
        target = last_h + float(sch["hours_interval"])
        cur = float(asset_hours or 0)
        hours_due = cur >= target
        hrs_rem = round(target - cur, 1)
    due = bool(date_due or hours_due)
    return {"next_due": next_due.isoformat() if next_due else None,
            "due": due, "overdue": overdue, "hours_due": hours_due, "hours_remaining": hrs_rem}


def _q_cur_due(sch, today):
    """A quarterly schedule's scheduled date within the CURRENT calendar quarter.

    The quarterly fires on `day_of_month` every 3 months; its anchor month is taken from
    start_date's month (so the Feb/May/Aug/Nov vs Jan/Apr/Jul/Oct vs Mar/Jun/Sep/Dec
    cycle survives even when the stored start_date was rolled forward to next year). Returns
    the firing date that falls in this quarter, or None. Used to decide whether a quarterly
    is due this period regardless of a forward-rolled start_date."""
    start = sch.get("start_date")
    dom = int(sch.get("day_of_month") or (start.day if start else today.day))
    sm = start.month if start else today.month
    q_start = ((today.month - 1) // 3) * 3 + 1          # 1, 4, 7, or 10
    for mo in (q_start, q_start + 1, q_start + 2):
        if (mo - sm) % 3 == 0:
            dim = calendar.monthrange(today.year, mo)[1]
            return date(today.year, mo, min(dom, dim))
    return None


# =======================================================================
#  ASSETS
# =======================================================================
@router.get("/meta")
async def meta(emp: dict = Depends(require_employee)):
    await ensure_tables()
    return JSONResponse({"categories": CATEGORIES}, headers={"Cache-Control": "no-store"})


def _json(obj):
    return JSONResponse(json.loads(json.dumps(obj, default=str)), headers={"Cache-Control": "no-store"})


@router.get("/assets/list")
async def assets_list(emp: dict = Depends(require_employee)):
    await ensure_tables()
    rows = await db.fetch("SELECT * FROM maint_assets ORDER BY active DESC, lower(name) NULLS LAST, code")
    return _json([dict(r) for r in rows])


@router.post("/assets/save")
async def assets_save(body: dict, emp: dict = Depends(require_employee)):
    await ensure_tables()
    code = (body.get("code") or "").strip()
    name = (body.get("name") or "").strip()
    if not code:
        raise HTTPException(400, "Asset code is required")
    if not name:
        raise HTTPException(400, "Asset name is required")
    vals = (code, name, (body.get("atype") or "").strip(), (body.get("location") or "").strip(),
            (body.get("make") or "").strip(), (body.get("model") or "").strip(),
            (body.get("serial_no") or "").strip(), _num(body.get("running_hours"), 0) or 0,
            (body.get("notes") or "").strip())
    aid = body.get("id")
    try:
        aid = int(aid) if str(aid or "").strip() else None   # id comes in as a string; the column is INT
    except (TypeError, ValueError):
        raise HTTPException(400, "Invalid asset id")
    try:
        if aid:
            await db.execute(
                "UPDATE maint_assets SET code=$1,name=$2,atype=$3,location=$4,make=$5,model=$6,"
                "serial_no=$7,running_hours=$8,notes=$9 WHERE id=$10", *vals, aid)
            return {"ok": True, "id": aid}
        dup = await db.fetchval("SELECT id FROM maint_assets WHERE lower(code)=lower($1)", code)
        if dup:
            raise HTTPException(400, "An asset with that code already exists")
        new_id = await db.fetchval(
            "INSERT INTO maint_assets (code,name,atype,location,make,model,serial_no,running_hours,notes,"
            "hours_updated_at) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,now()) RETURNING id", *vals)
        return {"ok": True, "id": new_id}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"DB error: {e}")


@router.post("/assets/hours")
async def assets_hours(body: dict, emp: dict = Depends(require_employee)):
    await ensure_tables()
    aid = body.get("id")
    hrs = _num(body.get("running_hours"))
    if not aid or hrs is None:
        raise HTTPException(400, "Asset and running hours required")
    try:
        aid = int(aid)
    except (TypeError, ValueError):
        raise HTTPException(400, "Invalid asset id")
    await db.execute("UPDATE maint_assets SET running_hours=$1, hours_updated_at=now() WHERE id=$2", hrs, aid)
    return {"ok": True}


@router.post("/assets/toggle")
async def assets_toggle(body: dict, emp: dict = Depends(require_employee)):
    await ensure_tables()
    try:
        aid = int(body.get("id"))
    except (TypeError, ValueError):
        raise HTTPException(400, "Invalid asset id")
    await db.execute("UPDATE maint_assets SET active=$1 WHERE id=$2", bool(body.get("active")), aid)
    return {"ok": True}


@router.post("/asset/delete")
async def asset_delete(body: dict, emp: dict = Depends(require_employee)):
    """Permanently delete a machine and everything attached to it (admin only)."""
    await ensure_tables()
    _require_admin(emp)
    aid = body.get("id")
    if not aid:
        raise HTTPException(400, "id required")
    try:
        aid = int(aid)
    except (TypeError, ValueError):
        raise HTTPException(400, "Invalid asset id")
    # completion records for this machine's checkpoints
    await db.execute(
        "DELETE FROM maint_checkpoint_done WHERE checkpoint_id IN "
        "(SELECT p.id FROM maint_checkpoints p JOIN maint_checklists c ON c.id=p.checklist_id "
        "WHERE c.asset_id=$1)", aid)
    await db.execute(
        "DELETE FROM maint_checkpoints WHERE checklist_id IN "
        "(SELECT id FROM maint_checklists WHERE asset_id=$1)", aid)
    await db.execute("DELETE FROM maint_checklists WHERE asset_id=$1", aid)
    for t in ("maint_schedules", "maint_logs", "maint_breakdowns", "maint_readings", "maint_asset_elec"):
        try:
            await db.execute(f"DELETE FROM {t} WHERE asset_id=$1", aid)
        except Exception as e:
            print("[maint] asset delete step skipped:", t, e)
    await db.execute("DELETE FROM maint_assets WHERE id=$1", aid)
    return {"ok": True}


@router.get("/assets/duplicates")
async def assets_duplicates(emp: dict = Depends(require_employee)):
    """Group machines that share the same code or the same name, so duplicates
    can be spotted and removed. Each asset carries its checklist / plan counts."""
    await ensure_tables()
    rows = await db.fetch(
        "SELECT a.id, a.code, a.name, a.active, "
        "(SELECT COUNT(*) FROM maint_checklists c WHERE c.asset_id=a.id) AS checklists, "
        "(SELECT COUNT(*) FROM maint_schedules s WHERE s.asset_id=a.id) AS plans "
        "FROM maint_assets a ORDER BY a.id")
    assets = [dict(r) for r in rows]

    def ncode(c):
        # Compare codes conservatively: uppercase, drop spaces only, and fold
        # the genuine data-entry typos letter-O->0 and letter-I->1 so
        # "E0T-009" and "EOT-009" count as the same machine. Separators like
        # "/" and "-" are KEPT, so "M/C-001" (a line) and "MC-001" (a crane)
        # stay distinct and are never flagged as duplicates of each other.
        s = re.sub(r"\s+", "", (c or "").upper())
        return s.translate(str.maketrans("OI", "01"))

    by_code = {}
    for a in assets:
        by_code.setdefault(ncode(a["code"]), []).append(a)
    groups = []
    for k, v in sorted(by_code.items()):
        if k and len(v) > 1:
            groups.append({"reason": "Same code", "label": v[0]["code"], "assets": v})
    return _json({"groups": groups})


# =======================================================================
#  CHECKLISTS + CHECKPOINTS
# =======================================================================
@router.get("/checklists/list")
async def checklists_list(asset: int = Query(default=0), emp: dict = Depends(require_employee)):
    await ensure_tables()
    if asset:
        rows = await db.fetch(
            "SELECT c.*, (SELECT COUNT(*) FROM maint_checkpoints p WHERE p.checklist_id=c.id) AS points "
            "FROM maint_checklists c WHERE asset_id=$1 ORDER BY lower(name)", asset)
    else:
        rows = await db.fetch(
            "SELECT c.*, a.name AS asset_name, a.code AS asset_code, "
            "(SELECT COUNT(*) FROM maint_checkpoints p WHERE p.checklist_id=c.id) AS points "
            "FROM maint_checklists c LEFT JOIN maint_assets a ON a.id=c.asset_id "
            "ORDER BY a.name NULLS LAST, lower(c.name)")
    return _json([dict(r) for r in rows])


@router.get("/checklist/get")
async def checklist_get(id: int = Query(...), emp: dict = Depends(require_employee)):
    await ensure_tables()
    c = await db.fetchrow("SELECT * FROM maint_checklists WHERE id=$1", id)
    if not c:
        raise HTTPException(404, "Checklist not found")
    pts = await db.fetch("SELECT * FROM maint_checkpoints WHERE checklist_id=$1 ORDER BY seq, id", id)
    return _json({"checklist": dict(c), "checkpoints": [dict(p) for p in pts]})


async def _employees_all():
    """[{id, emp_no, name}] of active employees — for assignment & name matching."""
    try:
        rows = await db.fetch(
            "SELECT id, emp_no, name FROM employees WHERE COALESCE(active,true)=true "
            "ORDER BY lower(name) NULLS LAST, emp_no")
        return [dict(r) for r in rows]
    except Exception:
        return []


def _resolve_emp(name, emps):
    """Map an assignment name (as typed in the workbook) to an employee id, or None."""
    n = (name or "").strip().lower()
    if not n:
        return None
    for e in emps:                       # exact full name
        if (e.get("name") or "").strip().lower() == n:
            return e["id"]
    for e in emps:                       # exact employee code
        if (e.get("emp_no") or "").strip().lower() == n:
            return e["id"]
    for e in emps:                       # first word of the employee's name
        enm = (e.get("name") or "").strip().lower()
        if enm and (enm.split() or [""])[0] == n:
            return e["id"]
    for e in emps:                       # name appears as a whole word
        enm = (e.get("name") or "").strip().lower()
        if enm and n in enm.split():
            return e["id"]
    # last resort: letters/digits only; match on a shared leading run of >=6 chars
    # (handles "Dhinakaran" vs "DHINAKRAN K" — a dropped/added letter or an initial).
    na = re.sub(r"[^a-z0-9]", "", n)
    if len(na) >= 6:
        for e in emps:
            ea = re.sub(r"[^a-z0-9]", "", (e.get("name") or "").strip().lower())
            if not ea:
                continue
            common = 0
            for ca, cb in zip(na, ea):
                if ca == cb:
                    common += 1
                else:
                    break
            if common >= 6 and abs(len(ea) - len(na)) <= 4:
                return e["id"]
    return None


def _period_key(checklist_name, d=None):
    """Completion window: daily -> the date; quarterly -> YYYY-Qn."""
    d = d or _today()
    if "quarter" in (checklist_name or "").lower():
        return f"{d.year}-Q{(d.month - 1) // 3 + 1}"
    return d.isoformat()


def _freq_label(checklist_name):
    return "Quarterly" if "quarter" in (checklist_name or "").lower() else "Daily"


@router.get("/emp-list")
async def emp_list(emp: dict = Depends(require_employee)):
    await ensure_tables()
    return _json(await _employees_all())


@router.post("/checklist/save")
async def checklist_save(body: dict, emp: dict = Depends(require_employee)):
    await ensure_tables()
    asset_id = body.get("asset_id")
    name = (body.get("name") or "").strip()
    if not asset_id:
        raise HTTPException(400, "Choose an asset")
    if not name:
        raise HTTPException(400, "Checklist name is required")
    cid = body.get("id")
    if cid:
        await db.execute("UPDATE maint_checklists SET name=$1, asset_id=$2 WHERE id=$3", name, asset_id, cid)
    else:
        cid = await db.fetchval(
            "INSERT INTO maint_checklists (asset_id,name) VALUES ($1,$2) RETURNING id", asset_id, name)
    emps = await _employees_all()
    # replace checkpoints
    await db.execute("DELETE FROM maint_checkpoints WHERE checklist_id=$1", cid)
    for i, p in enumerate(body.get("checkpoints") or []):
        txt = (p.get("text") or "").strip()
        if not txt:
            continue
        who = (p.get("assigned_to") or "").strip()
        eid = _resolve_emp(who, emps) if who else None
        await db.execute(
            "INSERT INTO maint_checkpoints (checklist_id,seq,text,category,input_type,unit,spec,"
            "assigned_to,assigned_emp_id) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)",
            cid, i + 1, txt, (p.get("category") or "").strip(),
            (p.get("input_type") or "check").strip(), (p.get("unit") or "").strip(),
            (p.get("spec") or "").strip(), who or None, eid)
    return {"ok": True, "id": cid}


@router.post("/checklist/delete")
async def checklist_delete(body: dict, emp: dict = Depends(require_employee)):
    await ensure_tables()
    cid = body.get("id")
    await db.execute("DELETE FROM maint_checkpoints WHERE checklist_id=$1", cid)
    await db.execute("DELETE FROM maint_checklists WHERE id=$1", cid)
    return {"ok": True}


# =======================================================================
#  CHECKPOINT ASSIGNMENT  (admin screen: per-checkpoint add/edit/delete +
#  bulk reassign everything from one person to another)
# =======================================================================
@router.get("/employees/list")
async def maint_employees_list(emp: dict = Depends(require_employee)):
    """Active employees, for the Assigned-To picker and the reassign tool."""
    await ensure_tables()
    return _json(await _employees_all())


@router.post("/checkpoint/save")
async def checkpoint_save(body: dict, emp: dict = Depends(require_employee)):
    """Add (no id) or edit (with id) ONE checkpoint. Admin only. Preserves the
    checkpoint id on edit so completion history stays linked."""
    await ensure_tables()
    _require_admin(emp)
    try:
        cid = int(body.get("checklist_id"))
    except (TypeError, ValueError):
        raise HTTPException(400, "Choose a checklist first")
    text = (body.get("text") or "").strip()
    if not text:
        raise HTTPException(400, "Enter the checkpoint")
    itype = (body.get("input_type") or "check").strip().lower()
    if itype not in ("check", "value"):
        itype = "check"
    spec = (body.get("spec") or "").strip()
    unit = (body.get("unit") or "").strip()
    eid = body.get("assigned_emp_id")
    try:
        eid = int(eid) if eid not in (None, "", "0", 0) else None
    except (TypeError, ValueError):
        eid = None
    who = None
    if eid:
        r = await db.fetchrow("SELECT name, emp_no FROM employees WHERE id=$1", eid)
        who = (r["name"] or r["emp_no"]) if r else None
    cat = _scope_cat(text)
    pid = body.get("id")
    try:
        pid = int(pid) if pid else None
    except (TypeError, ValueError):
        pid = None
    if pid:
        await db.execute(
            "UPDATE maint_checkpoints SET text=$1, input_type=$2, spec=$3, unit=$4, "
            "assigned_to=$5, assigned_emp_id=$6, "
            "category=CASE WHEN COALESCE(category,'')='' THEN $7 ELSE category END "
            "WHERE id=$8 AND checklist_id=$9", text, itype, spec, unit, who, eid, cat, pid, cid)
    else:
        seq = await db.fetchval(
            "SELECT COALESCE(MAX(seq),0)+1 FROM maint_checkpoints WHERE checklist_id=$1", cid)
        pid = await db.fetchval(
            "INSERT INTO maint_checkpoints (checklist_id,seq,text,category,input_type,unit,spec,"
            "assigned_to,assigned_emp_id) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9) RETURNING id",
            cid, seq, text, cat, itype, unit, spec, who, eid)
    await _ensure_schedule_assignees()
    return {"ok": True, "id": pid}


@router.post("/checkpoint/delete")
async def checkpoint_delete(body: dict, emp: dict = Depends(require_employee)):
    """Delete ONE checkpoint (and its completion ticks). Admin only."""
    await ensure_tables()
    _require_admin(emp)
    try:
        pid = int(body.get("id"))
    except (TypeError, ValueError):
        raise HTTPException(400, "Missing id")
    await db.execute("DELETE FROM maint_checkpoint_done WHERE checkpoint_id=$1", pid)
    await db.execute("DELETE FROM maint_checkpoints WHERE id=$1", pid)
    await _ensure_schedule_assignees()
    return {"ok": True}


@router.post("/checkpoints/reassign")
async def checkpoints_reassign(body: dict, emp: dict = Depends(require_employee)):
    """Move every checkpoint assigned to one person over to another person — e.g. when
    someone leaves and a new person takes over their machines. Admin only.
    body: from_emp_id, to_emp_id, scope = all | asset | checklist (+ asset_id/checklist_id)."""
    await ensure_tables()
    _require_admin(emp)
    try:
        frm = int(body.get("from_emp_id"))
    except (TypeError, ValueError):
        raise HTTPException(400, "Choose the person to move work FROM")
    try:
        to = int(body.get("to_emp_id"))
    except (TypeError, ValueError):
        raise HTTPException(400, "Choose the person to move work TO")
    if frm == to:
        raise HTTPException(400, "Pick two different people")
    r = await db.fetchrow("SELECT name, emp_no FROM employees WHERE id=$1", to)
    if not r:
        raise HTTPException(404, "The new person was not found")
    toname = (r["name"] or r["emp_no"])
    scope = (body.get("scope") or "all").strip()
    q = "UPDATE maint_checkpoints SET assigned_emp_id=$1, assigned_to=$2 WHERE assigned_emp_id=$3"
    params = [to, toname, frm]
    if scope == "checklist" and body.get("checklist_id"):
        q += " AND checklist_id=$4"; params.append(int(body["checklist_id"]))
    elif scope == "asset" and body.get("asset_id"):
        q += " AND checklist_id IN (SELECT id FROM maint_checklists WHERE asset_id=$4)"
        params.append(int(body["asset_id"]))
    res = await db.execute(q, *params)
    try:
        moved = int(str(res).split()[-1])
    except Exception:
        moved = 0
    await _ensure_schedule_assignees()
    return {"ok": True, "moved": moved, "to": toname}


@router.post("/checkpoints/assign-bulk")
async def checkpoints_assign_bulk(body: dict, emp: dict = Depends(require_employee)):
    """Set the assigned person on a SELECTED set of checkpoints at once. Admin only.
    body: ids = [checkpoint ids], emp_id (empty/0 = unassign)."""
    await ensure_tables()
    _require_admin(emp)
    ids = []
    for x in (body.get("ids") or []):
        try:
            ids.append(int(x))
        except (TypeError, ValueError):
            pass
    if not ids:
        raise HTTPException(400, "Select at least one checkpoint")
    eid = body.get("emp_id")
    try:
        eid = int(eid) if eid not in (None, "", "0", 0) else None
    except (TypeError, ValueError):
        eid = None
    who = None
    if eid:
        r = await db.fetchrow("SELECT name, emp_no FROM employees WHERE id=$1", eid)
        if not r:
            raise HTTPException(404, "User not found")
        who = (r["name"] or r["emp_no"])
    res = await db.execute(
        "UPDATE maint_checkpoints SET assigned_emp_id=$1, assigned_to=$2 WHERE id = ANY($3::int[])",
        eid, who, ids)
    try:
        n = int(str(res).split()[-1])
    except Exception:
        n = 0
    await _ensure_schedule_assignees()
    return {"ok": True, "updated": n, "to": who or "Unassigned"}


_CL_GENERIC = ("upload  a photo", "upload a photo", "describe  any abnormalities",
               "describe any abnormalities", "sign here", "record voice")


def _scope_cat(label: str) -> str:
    """Auto-tag a checkpoint to Electrical / Mechanical / Hydraulic from its wording."""
    t = (label or "").lower().strip()
    if "current" in t or "amps" in t:
        return "Electrical"
    if "bolt" in t or " nut" in t or " bush" in t:
        return "Mechanical"
    if any(k in t for k in ("hydraulic oil", "hydraulic line", "hydraulic valve", "power pack",
                            "powerpack", "oil leak", "oil temperature", "thruster brake",
                            "piston rod", "pneumatic", "hydraulic and")):
        return "Hydraulic"
    if "hydraulic" in t and "function" in t:
        return "Hydraulic"
    if any(k in t for k in ("inverter", "invertor", "encoder", "sensor", "panel", "cable wire",
                            "wiring", " wire ", "battery", "alternator", "load cell", "junction box",
                            "earth", "mv output", " mv ", "indicator", "remote", "vfd", " plc",
                            "solenoid", "photocell", "proximity", "relay", "contactor",
                            "limit switch", "electrical", "polarity", "the cells")):
        return "Electrical"
    return "Mechanical"


@router.post("/checklists/import")
async def checklists_import(body: dict, emp: dict = Depends(require_employee)):
    """Bulk-create checklists from an uploaded workbook (one sheet per machine).
    The client parses the xlsx and posts {sheets:[{title, points:[{text,options,min,max}]}]}."""
    await ensure_tables()
    rows = await db.fetch("SELECT id, code, name FROM maint_assets")
    by_code = {}
    by_name = []
    for a in rows:
        c = (a["code"] or "").upper().replace(" ", "")
        if c:
            by_code[c] = a["id"]
        by_name.append((a["id"], (a["name"] or "").lower()))

    def match_asset(title: str):
        t = (title or "").upper()
        m = re.search(r'M/C\s*N?O?\.?-?\s*0*(\d{1,3})', t)
        if m and ("M/C-%03d" % int(m.group(1))) in by_code:
            return by_code["M/C-%03d" % int(m.group(1))]
        m = re.search(r'EOT[-\s]*0*(\d{1,3})', t)
        if m and ("EOT-%03d" % int(m.group(1))) in by_code:
            return by_code["EOT-%03d" % int(m.group(1))]
        tl = (title or "").lower()
        for aid, nm in by_name:
            if nm and len(nm) >= 5 and nm[:16] in tl:
                return aid
        return None

    emps = await _employees_all()
    created = updated = cps = 0
    unmatched = []
    matched_users, unmatched_users = set(), set()
    for sh in (body.get("sheets") or []):
        title = (sh.get("title") or "").strip()
        if not title:
            continue
        aid = match_asset(title)
        if not aid:
            unmatched.append(title[:50])
            continue
        c, u, n, mu, uu = await _rebuild_asset_checklists(aid, sh.get("rows") or sh.get("points") or [], emps)
        created += c; updated += u; cps += n
        matched_users |= mu; unmatched_users |= uu
    return {"ok": True, "created": created, "updated": updated, "checkpoints": cps,
            "unmatched": unmatched, "matched_users": sorted(matched_users),
            "unmatched_users": sorted(unmatched_users)}


def _cl_name_for(freq):
    f = (freq or "").strip().lower()
    if "quarter" in f:
        return "Quarterly Checklist"
    if "dail" in f or "day" in f:
        return "Daily Checklist"
    return (freq.strip().title() + " Checklist") if freq.strip() else "Daily Checklist"


async def _rebuild_asset_checklists(aid, rows, emps):
    """Rebuild one asset's Daily/Quarterly checklists from rows, carrying the
    per-checkpoint user assignment. rows use short keys (f/t/s/u) or long
    (freq/text/scope/user). Returns (created, updated, checkpoints, matched, unmatched)."""
    groups = {}
    for p in (rows or []):
        txt = (str(p.get("t") if p.get("t") is not None else p.get("text") or "")).strip()
        if not txt or txt.lower() in _CL_GENERIC:
            continue
        scope = (str(p.get("s") if p.get("s") is not None else p.get("scope") or "")).strip() or _scope_cat(txt)
        who = (str(p.get("u") if p.get("u") is not None else p.get("user") or p.get("assigned_to") or "")).strip()
        freq = p.get("f") if p.get("f") is not None else p.get("freq")
        groups.setdefault(_cl_name_for(freq), []).append((txt, scope, who))
    created = updated = cps = 0
    matched_users, unmatched_users = set(), set()
    for cl_name, items in groups.items():
        cid = await db.fetchval(
            "SELECT id FROM maint_checklists WHERE asset_id=$1 AND lower(name)=lower($2)", aid, cl_name)
        if cid:
            await db.execute("DELETE FROM maint_checkpoints WHERE checklist_id=$1", cid)
            updated += 1
        else:
            cid = await db.fetchval(
                "INSERT INTO maint_checklists (asset_id,name) VALUES ($1,$2) RETURNING id", aid, cl_name)
            created += 1
        for i, (txt, cat, who) in enumerate(items):
            eid = _resolve_emp(who, emps) if who else None
            if who:
                (matched_users if eid else unmatched_users).add(who)
            await db.execute(
                "INSERT INTO maint_checkpoints (checklist_id,seq,text,category,input_type,unit,spec,"
                "assigned_to,assigned_emp_id) VALUES ($1,$2,$3,$4,'check','','',$5,$6)",
                cid, i + 1, txt, cat, who or None, eid)
            cps += 1
    return created, updated, cps, matched_users, unmatched_users


async def _seed_new_machines():
    """One-time: rebuild ONLY the 5 SHED-B machines (015,017,018,019,020) from the
    shipped seed, so their daily/quarterly checkpoints + user assignments load even
    though other machines already have assignments. Idempotent per machine."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "maint_assign_seed.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        print("[maint] new-machines seed file not read:", e)
        return
    emps = await _employees_all()
    total = 0
    for entry in data:
        code = (entry.get("code") or "").strip()
        if code not in NEW_MACHINE_CODES:
            continue
        aid = await db.fetchval("SELECT id FROM maint_assets WHERE lower(code)=lower($1)", code)
        if not aid:
            continue
        _, _, n, _, _ = await _rebuild_asset_checklists(aid, entry.get("rows") or [], emps)
        total += n
    print(f"[maint] new-machines seed loaded {total} checkpoints")


async def _reseed_all_fast():
    """Rebuild every machine's Daily/Quarterly checklists + per-checkpoint assignees
    from the shipped seed, fast: one pooled connection + batched checkpoint inserts.
    Safe to run repeatedly (deletes then re-inserts each checklist)."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "maint_assign_seed.json")
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    emps = await _employees_all()
    p = await db.pool()
    machines = cps = 0
    unmatched = set()
    async with p.acquire() as con:
        rows = await con.fetch("SELECT id, lower(code) AS code FROM maint_assets")
        by_code = {r["code"]: r["id"] for r in rows}
        for entry in data:
            code = (entry.get("code") or "").strip().lower()
            aid = by_code.get(code)
            if not aid:
                continue
            groups = {}
            for row in (entry.get("rows") or []):
                txt = (str(row.get("t") or "")).strip()
                if not txt or txt.lower() in _CL_GENERIC:
                    continue
                cat = (str(row.get("s") or "")).strip() or _scope_cat(txt)
                who = (str(row.get("u") or "")).strip()
                groups.setdefault(_cl_name_for(row.get("f")), []).append((txt, cat, who))
            for cl_name, items in groups.items():
                cid = await con.fetchval(
                    "SELECT id FROM maint_checklists WHERE asset_id=$1 AND lower(name)=lower($2)", aid, cl_name)
                if cid:
                    await con.execute("DELETE FROM maint_checkpoints WHERE checklist_id=$1", cid)
                else:
                    cid = await con.fetchval(
                        "INSERT INTO maint_checklists (asset_id,name) VALUES ($1,$2) RETURNING id", aid, cl_name)
                batch = []
                for i, (txt, cat, who) in enumerate(items):
                    eid = _resolve_emp(who, emps) if who else None
                    if who and not eid:
                        unmatched.add(who)
                    batch.append((cid, i + 1, txt, cat, 'check', '', '', who or None, eid))
                    cps += 1
                if batch:
                    await con.executemany(
                        "INSERT INTO maint_checkpoints (checklist_id,seq,text,category,input_type,unit,spec,"
                        "assigned_to,assigned_emp_id) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)", batch)
            machines += 1
    return {"machines": machines, "checkpoints": cps, "unmatched_users": sorted(unmatched)}


async def _ensure_new_machine_quarterly():
    """Make sure the 5 SHED-B machines each have a Quarterly PM plan, created from the
    Annual Schedule only if one is missing (never disturbs an existing plan)."""
    today = _today()
    for code in NEW_MACHINE_CODES:
        info = SCHEDULE_PLAN.get(code)
        if not info:
            continue
        day, sm = info[0], info[1]
        try:
            aid = await db.fetchval("SELECT id FROM maint_assets WHERE lower(code)=lower($1)", code)
            if not aid:
                continue
            sid = await db.fetchval(
                "SELECT id FROM maint_schedules WHERE asset_id=$1 "
                "AND lower(COALESCE(freq_type,''))='quarterly' ORDER BY id LIMIT 1", aid)
            if sid:
                continue
            start = _next_plan_start(day, sm, today)
            await db.execute(
                "INSERT INTO maint_schedules (asset_id,title,freq_type,day_of_month,freq_days,"
                "start_date,due_time,active) VALUES ($1,'Quarterly PM','quarterly',$2,90,$3,'09:00',true)",
                aid, day, start)
        except Exception as e:
            print("[maint] new-machine quarterly skipped:", code, e)


async def _ensure_schedule_assignees():
    """Give each schedule an overall assignee = the person who does the most checkpoints
    on its linked checklist, so the board shows who's responsible and reminders can send."""
    try:
        rows = await db.fetch(
            "SELECT s.id AS sid, "
            "(SELECT cp.assigned_emp_id FROM maint_checkpoints cp "
            " WHERE cp.checklist_id = s.checklist_id AND cp.assigned_emp_id IS NOT NULL "
            " GROUP BY cp.assigned_emp_id ORDER BY COUNT(*) DESC, cp.assigned_emp_id LIMIT 1) AS eid "
            "FROM maint_schedules s WHERE s.checklist_id IS NOT NULL")
        for r in rows:
            if r["eid"]:
                await db.execute("UPDATE maint_schedules SET assignee_emp_id=$1 WHERE id=$2",
                                 r["eid"], r["sid"])
    except Exception as e:
        print("[maint] schedule assignee skipped:", e)


async def _ensure_quarterly_links():
    """Link every Quarterly PM plan to its machine's Quarterly checklist (only when
    the plan has no checklist yet), so completing a quarterly job shows its checkpoints."""
    try:
        rows = await db.fetch(
            "SELECT c.id AS cid, c.asset_id AS aid FROM maint_checklists c "
            "WHERE lower(COALESCE(c.name,'')) LIKE '%quarter%'")
        for r in rows:
            if not r["aid"]:
                continue
            await db.execute(
                "UPDATE maint_schedules SET checklist_id=$2 "
                "WHERE asset_id=$1 AND lower(COALESCE(freq_type,''))='quarterly' AND checklist_id IS NULL",
                r["aid"], r["cid"])
    except Exception as e:
        print("[maint] quarterly link skipped:", e)


async def _seed_assignments():
    """Load the shipped per-checkpoint assignment seed (one machine per entry,
    keyed by asset code) and rebuild those machines' Daily/Quarterly checklists."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "maint_assign_seed.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        print("[maint] assignment seed file not read:", e)
        return
    emps = await _employees_all()
    total = 0
    for entry in data:
        code = (entry.get("code") or "").strip()
        if not code:
            continue
        aid = await db.fetchval("SELECT id FROM maint_assets WHERE lower(code)=lower($1)", code)
        if not aid:
            continue
        _, _, n, _, _ = await _rebuild_asset_checklists(aid, entry.get("rows") or [], emps)
        total += n
    print(f"[maint] assignment seed loaded: {total} checkpoints")


# =======================================================================
#  MY CHECKLIST  (per-user assigned checkpoints + tick-to-complete)
# =======================================================================
@router.get("/my/list")
async def my_checklist(emp: dict = Depends(require_employee)):
    """The logged-in person's assigned work, grouped by machine + Daily/Quarterly,
    each with a due / overdue / done status taken from that checklist's PM schedule."""
    await ensure_tables()
    eid = emp["id"]
    today = _today()
    groups = await db.fetch(
        "SELECT p.checklist_id, COUNT(*) AS my_count, c.name AS checklist_name, "
        "a.id AS asset_id, a.code AS asset_code, a.name AS asset_name, "
        "a.running_hours AS asset_hours, "
        "s.id AS schedule_id, s.freq_type, s.day_of_month, s.freq_days, s.start_date, "
        "s.last_done, s.end_date, s.hours_interval, s.last_done_hours "
        "FROM maint_checkpoints p "
        "JOIN maint_checklists c ON c.id=p.checklist_id "
        "LEFT JOIN maint_assets a ON a.id=c.asset_id "
        "LEFT JOIN maint_schedules s ON s.checklist_id=c.id AND COALESCE(s.active,true)=true "
        "WHERE p.assigned_emp_id=$1 "
        "GROUP BY p.checklist_id, c.name, a.id, a.code, a.name, a.running_hours, "
        "s.id, s.freq_type, s.day_of_month, s.freq_days, s.start_date, s.last_done, "
        "s.end_date, s.hours_interval, s.last_done_hours "
        "ORDER BY a.name NULLS LAST, c.name", eid)
    # how many of my checkpoints are already done, per checklist + period
    donemap = {}
    for r in await db.fetch(
            "SELECT p.checklist_id AS cid, d.period_key AS pk, COUNT(*) AS n "
            "FROM maint_checkpoint_done d JOIN maint_checkpoints p ON p.id=d.checkpoint_id "
            "WHERE p.assigned_emp_id=$1 GROUP BY p.checklist_id, d.period_key", eid):
        donemap[(r["cid"], r["pk"])] = r["n"]
    # The actual checkpoint points assigned to this person, per checklist, so the list
    # can show the point descriptions (not just a count), each with its done time.
    cps_by_cl, cp_ids = {}, []
    for cp in await db.fetch(
            "SELECT id, checklist_id, seq, text, category FROM maint_checkpoints "
            "WHERE assigned_emp_id=$1 ORDER BY seq, id", eid):
        cps_by_cl.setdefault(cp["checklist_id"], []).append(dict(cp))
        cp_ids.append(cp["id"])
    done_cp = {}
    if cp_ids:
        for d in await db.fetch(
                "SELECT checkpoint_id, period_key, result, done_at FROM maint_checkpoint_done "
                "WHERE checkpoint_id = ANY($1::int[])", cp_ids):
            done_cp[(d["checkpoint_id"], d["period_key"])] = d
    items = []
    for g in groups:
        g = dict(g)
        pk = _period_key(g["checklist_name"], today)
        freq = _freq_label(g["checklist_name"])
        done_n = donemap.get((g["checklist_id"], pk), 0)
        done = done_n >= g["my_count"] and g["my_count"] > 0
        next_due = None
        overdue = due = False
        if freq == "Daily":
            # daily resets every day; due today unless it's Sunday (plant off)
            due = today.weekday() != 6
            if done or not due:
                # done for today (or today is an off day) -> next due is the next working
                # day, so a completed daily doesn't keep showing today's date as "next due"
                nd = today + timedelta(days=1)
                while nd.weekday() == 6:   # skip Sundays
                    nd += timedelta(days=1)
                next_due = nd.isoformat()
            else:
                next_due = today.isoformat()
        elif g.get("schedule_id"):
            info = compute_due(g, g.get("asset_hours"), today)
            overdue = bool(info.get("overdue"))
            due = bool(info.get("due"))
            nd = info.get("next_due")
            next_due = nd.isoformat() if hasattr(nd, "isoformat") else (nd or None)
        else:
            due = True  # quarterly with no schedule -> due this period
        status = "done" if done else ("overdue" if overdue else ("due" if due else "upcoming"))
        # the point descriptions assigned to me on this checklist, with done time
        points = []
        for cp in cps_by_cl.get(g["checklist_id"], []):
            dd = done_cp.get((cp["id"], pk))
            da = dd["done_at"] if dd else None
            points.append({
                "text": cp["text"], "category": cp.get("category"),
                "done": bool(dd),
                "result": (dd["result"] if dd else None),
                "done_at": da.isoformat() if isinstance(da, datetime) else None})
        items.append({"checklist_id": g["checklist_id"], "schedule_id": g.get("schedule_id"),
                      "asset_id": g.get("asset_id"), "asset_code": g.get("asset_code"),
                      "asset_name": g.get("asset_name"), "checklist_name": g["checklist_name"],
                      "freq": _freq_label(g["checklist_name"]), "my_count": g["my_count"],
                      "done_n": done_n, "status": status, "next_due": next_due,
                      "period_key": pk, "points": points})
    return _json({"items": items, "today": today.isoformat(),
                  "me": emp.get("name") or emp.get("emp_no")})


@router.get("/my/form")
async def my_checklist_form(checklist: int = Query(...), emp: dict = Depends(require_employee)):
    """The logged-in person's checkpoints for one checklist, for the fill-in popup."""
    await ensure_tables()
    eid = emp["id"]
    c = await db.fetchrow(
        "SELECT c.id, c.name, a.code AS asset_code, a.name AS asset_name "
        "FROM maint_checklists c LEFT JOIN maint_assets a ON a.id=c.asset_id WHERE c.id=$1", checklist)
    if not c:
        raise HTTPException(404, "Checklist not found")
    rows = await db.fetch(
        "SELECT id, seq, text, category, input_type, unit, spec FROM maint_checkpoints "
        "WHERE checklist_id=$1 AND assigned_emp_id=$2 ORDER BY seq, id", checklist, eid)
    pk = _period_key(c["name"])
    ids = [r["id"] for r in rows]
    prev = {}
    if ids:
        for d in await db.fetch(
                "SELECT checkpoint_id, result, note, done_at FROM maint_checkpoint_done "
                "WHERE period_key=$1 AND checkpoint_id = ANY($2::int[])", pk, ids):
            prev[d["checkpoint_id"]] = {"result": d["result"], "note": d["note"],
                                        "done_at": d["done_at"]}
    pts = []
    for r in rows:
        d = dict(r)
        p = prev.get(r["id"]) or {}
        d["result"] = p.get("result")
        d["note"] = p.get("note")
        d["done"] = r["id"] in prev
        da = p.get("done_at")
        d["done_at"] = da.isoformat() if isinstance(da, datetime) else None
        pts.append(d)
    sch = await db.fetchval("SELECT id FROM maint_schedules WHERE checklist_id=$1 AND COALESCE(active,true)=true LIMIT 1", checklist)
    return _json({"checklist": dict(c), "freq": _freq_label(c["name"]),
                  "schedule_id": sch, "period_key": pk, "checkpoints": pts})


@router.post("/my/submit")
async def my_checklist_submit(body: dict, emp: dict = Depends(require_employee)):
    """Record the person's results for their checkpoints in one checklist."""
    await ensure_tables()
    eid = emp["id"]
    cid = body.get("checklist_id")
    c = await db.fetchrow("SELECT id, name, asset_id FROM maint_checklists WHERE id=$1", cid)
    if not c:
        raise HTTPException(404, "Checklist not found")
    pk = _period_key(c["name"])
    mine = {r["id"] for r in await db.fetch(
        "SELECT id FROM maint_checkpoints WHERE checklist_id=$1 AND assigned_emp_id=$2", cid, eid)}
    results = body.get("results") or []
    saved = 0
    for r in results:
        pid = r.get("checkpoint_id")
        if pid not in mine:
            continue
        res = (r.get("result") or ("Not OK" if r.get("ok") is False else "OK")).strip()
        note = (r.get("note") or "").strip()
        await db.execute(
            "INSERT INTO maint_checkpoint_done (checkpoint_id,emp_id,period_key,result,note) "
            "VALUES ($1,$2,$3,$4,$5) ON CONFLICT (checkpoint_id,period_key) "
            "DO UPDATE SET emp_id=$2, result=$4, note=$5, done_at=now()",
            pid, eid, pk, res, note)
        saved += 1
    # audit entry in the maintenance log (does not advance the PM schedule)
    status = "issues" if any((r.get("ok") is False or r.get("result") == "Not OK") for r in results) else "done"
    sid = body.get("schedule_id")
    done_by = emp.get("name") or emp.get("emp_no")
    started = _parse_local_dt(body.get("start_time"))
    ended = _parse_local_dt(body.get("end_time"))
    log_id = None
    try:
        log_id = await db.fetchval(
            "INSERT INTO maint_logs (asset_id,schedule_id,checklist_id,done_by,done_by_emp_id,"
            "status,results,remarks,started_at,ended_at) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10) RETURNING id",
            c["asset_id"], sid, cid, done_by, eid,
            status, json.dumps(results), (body.get("remarks") or "").strip(), started, ended)
    except Exception as e:
        print("[maint] my submit log skipped:", e)
    # If the WHOLE checklist is now complete for this period (every checkpoint that is
    # assigned and not marked absent has a result), advance the PM schedule's last_done
    # so the task stops showing as due/overdue and no reminder is sent for it again.
    # (For a checklist split across people this only advances once the last person submits.)
    # Is EVERY assigned, non-absent checkpoint for this period now complete? When a
    # quarterly (or any) checklist is split across several people, this is true only
    # once the LAST person submits — which gates both the schedule advance and the PDF.
    all_done = False
    try:
        all_cps = await db.fetch(
            "SELECT id, assigned_emp_id FROM maint_checkpoints WHERE checklist_id=$1", cid)
        absent = await _absent_emp_ids(cid, pk)
        required = [r["id"] for r in all_cps
                    if r["assigned_emp_id"] and r["assigned_emp_id"] not in absent]
        if required:
            done_ids = {d["checkpoint_id"] for d in await db.fetch(
                "SELECT checkpoint_id FROM maint_checkpoint_done "
                "WHERE period_key=$1 AND checkpoint_id = ANY($2::int[])",
                pk, [r["id"] for r in all_cps])}
            all_done = all(cp_id in done_ids for cp_id in required)
            if all_done:
                today_d = _today()
                for sr in await db.fetch(
                        "SELECT id, asset_id FROM maint_schedules "
                        "WHERE checklist_id=$1 AND COALESCE(active,true)=true", cid):
                    ah = await db.fetchval(
                        "SELECT running_hours FROM maint_assets WHERE id=$1", sr["asset_id"])
                    await db.execute(
                        "UPDATE maint_schedules SET last_done=$1, last_done_hours=$2 WHERE id=$3",
                        today_d, ah, sr["id"])
    except Exception as e:
        print("[maint] my submit schedule-advance skipped:", e)
    # Work-order PDF -> OneDrive/Maintenance/Preventive — generated ONLY once every
    # assigned person has finished their checkpoints for this period (no intermediate
    # 'pending' PDF). _pm_rows_full then builds it from the full checklist, so it shows
    # each point, who did it and when. Background so the save returns immediately.
    if all_done:
        try:
            a = await db.fetchrow("SELECT code, name, location FROM maint_assets WHERE id=$1", c["asset_id"])
            asyncio.create_task(_file_pm_report(
                asset_code=(a["code"] if a else ""), asset_name=(a["name"] if a else ""),
                checklist=c["name"], doc_no=str(log_id or ""), done_by=done_by,
                results=results, checklist_id=cid, location=(a["location"] if a else "")))
        except Exception as e:
            print("[maint] my submit report skipped:", e)
    return {"ok": True, "saved": saved, "status": status,
            "period_key": pk, "all_done": all_done}


# =======================================================================
#  SCHEDULES
# =======================================================================
async def _schedule_rows():
    rows = await db.fetch("""
        SELECT s.*, a.name AS asset_name, a.code AS asset_code, a.location AS asset_location,
               a.running_hours AS asset_hours, c.name AS checklist_name,
               e.name AS assignee_name, e.emp_no AS assignee_code, COALESCE(e.mobile,'') AS assignee_mobile
        FROM maint_schedules s
        LEFT JOIN maint_assets a ON a.id=s.asset_id
        LEFT JOIN maint_checklists c ON c.id=s.checklist_id
        LEFT JOIN employees e ON e.id=s.assignee_emp_id
        ORDER BY s.active DESC, a.name NULLS LAST, s.title""")
    return [dict(r) for r in rows]


@router.get("/schedules/list")
async def schedules_list(emp: dict = Depends(require_employee)):
    await ensure_tables()
    await _refresh_holidays()
    today = _today()
    out = []
    for s in await _schedule_rows():
        info = compute_due(s, s.get("asset_hours"), today)
        s.update(info)
        out.append(s)
    return _json(out)


_FREQ_DAYS = {"daily": 1, "weekly": 7, "monthly": 30, "quarterly": 90}


@router.post("/schedule/save")
async def schedule_save(body: dict, emp: dict = Depends(require_employee)):
    await ensure_tables()
    asset_id = body.get("asset_id")
    title = (body.get("title") or "").strip()
    if not asset_id:
        raise HTTPException(400, "Choose a machine")
    if not title:
        raise HTTPException(400, "Enter a task")
    ft = (body.get("freq_type") or "").strip().lower()
    if ft not in FREQS:
        raise HTTPException(400, "Choose a frequency")
    dom = _num(body.get("day_of_month"))
    if ft in ("monthly", "quarterly"):
        if not dom or not (1 <= int(dom) <= 31):
            raise HTTPException(400, "Enter a day of month (1–31)")
        dom = int(dom)
    else:
        dom = None
    assignee = body.get("assignee_emp_id") or None
    due_time = (body.get("due_time") or "").strip() or "09:00"   # default 9:00 AM
    vals = (asset_id, title, body.get("checklist_id") or None, ft, dom,
            _FREQ_DAYS.get(ft), _num(body.get("hours_interval")), assignee,
            _d(body.get("start_date")) or _today(), _d(body.get("end_date")),
            due_time, bool(body.get("active", True)))
    sid = body.get("id")
    if sid:
        await db.execute(
            "UPDATE maint_schedules SET asset_id=$1,title=$2,checklist_id=$3,freq_type=$4,"
            "day_of_month=$5,freq_days=$6,hours_interval=$7,assignee_emp_id=$8,start_date=$9,"
            "end_date=$10,due_time=$11,active=$12 WHERE id=$13", *vals, sid)
        return {"ok": True, "id": sid}
    sid = await db.fetchval(
        "INSERT INTO maint_schedules (asset_id,title,checklist_id,freq_type,day_of_month,"
        "freq_days,hours_interval,assignee_emp_id,start_date,end_date,due_time,active) "
        "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12) RETURNING id", *vals)
    return {"ok": True, "id": sid}


@router.post("/schedule/toggle")
async def schedule_toggle(body: dict, emp: dict = Depends(require_employee)):
    await ensure_tables()
    await db.execute("UPDATE maint_schedules SET active=$1 WHERE id=$2", bool(body.get("active")), body.get("id"))
    return {"ok": True}


@router.post("/schedules/toggle-all")
async def schedules_toggle_all(body: dict, emp: dict = Depends(require_employee)):
    """Pause or resume every plan at once."""
    await ensure_tables()
    await db.execute("UPDATE maint_schedules SET active=$1", bool(body.get("active")))
    n = await db.fetchval("SELECT COUNT(*) FROM maint_schedules")
    return {"ok": True, "count": int(n or 0)}


@router.post("/schedules/delete-all")
async def schedules_delete_all(emp: dict = Depends(require_employee)):
    """Delete every plan (checklists and assets are untouched)."""
    await ensure_tables()
    n = await db.fetchval("SELECT COUNT(*) FROM maint_schedules")
    await db.execute("DELETE FROM maint_schedules")
    return {"ok": True, "count": int(n or 0)}


@router.post("/schedule/delete")
async def schedule_delete(body: dict, emp: dict = Depends(require_employee)):
    await ensure_tables()
    await db.execute("DELETE FROM maint_schedules WHERE id=$1", body.get("id"))
    return {"ok": True}


# ---------- holidays (roll PM due dates to the next working day) ----------
@router.get("/holidays/list")
async def holidays_list(emp: dict = Depends(require_employee)):
    await ensure_tables()
    rows = await db.fetch("SELECT id, hday, name FROM maint_holidays ORDER BY hday")
    return _json([dict(r) for r in rows])


@router.post("/holidays/save")
async def holidays_save(body: dict, emp: dict = Depends(require_employee)):
    await ensure_tables()
    d = _d(body.get("date"))
    if not d:
        raise HTTPException(400, "Pick a valid date")
    await db.execute(
        "INSERT INTO maint_holidays (hday,name) VALUES ($1,$2) "
        "ON CONFLICT (hday) DO UPDATE SET name=EXCLUDED.name", d, (body.get("name") or "").strip())
    await _refresh_holidays()
    return {"ok": True}


@router.post("/holidays/delete")
async def holidays_delete(body: dict, emp: dict = Depends(require_employee)):
    await ensure_tables()
    await db.execute("DELETE FROM maint_holidays WHERE id=$1", body.get("id"))
    await _refresh_holidays()
    return {"ok": True}


# ---------- leave (while on leave a person is skipped entirely — no reminders,
#            and their tasks are not raised to the supervisor's overdue alert) ----------
@router.get("/leave/list")
async def leave_list(emp: dict = Depends(require_employee)):
    await ensure_tables()
    rows = await db.fetch(
        "SELECT l.id, l.emp_id, l.from_date, l.to_date, l.reason, e.name AS emp_name, "
        "e.emp_no AS emp_code FROM maint_leave l LEFT JOIN employees e ON e.id=l.emp_id "
        "ORDER BY l.from_date DESC")
    return _json([dict(r) for r in rows])


@router.post("/leave/save")
async def leave_save(body: dict, emp: dict = Depends(require_employee)):
    await ensure_tables()
    try:
        eid = int(body.get("emp_id"))          # select sends a string — INT column needs an int
    except (TypeError, ValueError):
        raise HTTPException(400, "Pick a staff member")
    f = _d(body.get("from_date"))
    t = _d(body.get("to_date")) or f
    if not eid or not f:
        raise HTTPException(400, "Pick a staff member and a from-date")
    if t < f:
        raise HTTPException(400, "To-date can't be before from-date")
    await db.execute(
        "INSERT INTO maint_leave (emp_id,from_date,to_date,reason) VALUES ($1,$2,$3,$4)",
        eid, f, t, (body.get("reason") or "").strip())
    return {"ok": True}


@router.post("/leave/delete")
async def leave_delete(body: dict, emp: dict = Depends(require_employee)):
    await ensure_tables()
    try:
        lid = int(body.get("id"))
    except (TypeError, ValueError):
        raise HTTPException(400, "Missing id")
    await db.execute("DELETE FROM maint_leave WHERE id=$1", lid)
    return {"ok": True}


# ---------- breakdowns (unplanned stoppages — raise, list, close) ----------
async def _open_breakdowns():
    """Open breakdown rows with asset + (primary) assignee joined, newest first.
    The full assignee list is in b.assignees (JSONB)."""
    try:
        rows = await db.fetch(
            "SELECT b.*, a.code AS asset_code, a.name AS asset_name, "
            "e.name AS assignee_name, e.mobile AS assignee_mobile "
            "FROM maint_breakdowns b "
            "LEFT JOIN maint_assets a ON a.id=b.asset_id "
            "LEFT JOIN employees e ON e.id=b.assignee_emp_id "
            # A breakdown counts as OPEN only when it is not closed by EITHER signal:
            # a clean 'open' status AND no completion time. Once it has a closed_at it
            # is done and must leave the board, even if the status text is off-case.
            "WHERE lower(btrim(coalesce(b.status,'open')))='open' AND b.closed_at IS NULL "
            "ORDER BY b.raised_at DESC")
        return [dict(r) for r in rows]
    except Exception:
        return []


async def _emp_map():
    """{emp_id: {'name','mobile'}} for resolving multi-assignee breakdowns/types."""
    try:
        rows = await db.fetch("SELECT id, name, emp_no, COALESCE(mobile,'') AS mobile FROM employees")
        return {r["id"]: {"name": r["name"] or r["emp_no"], "mobile": r["mobile"]} for r in rows}
    except Exception:
        return {}


BREAKDOWN_TYPES = ["Electrical", "Mechanical", "Hydraulic", "Other"]


def _norm_type(t):
    t = (t or "").strip().lower()
    for x in BREAKDOWN_TYPES:
        if x.lower() == t:
            return x
    return "Other" if t else ""


async def _breakdown_photos(bid: int, photos: list) -> list:
    """Upload breakdown photos (data-URL base64) to OneDrive, return stored paths."""
    out = []
    try:
        token = await graph.get_token()
    except Exception as e:
        print("[maint] breakdown photo token failed:", e)
        return out
    for idx, p in enumerate(photos or []):
        m = re.match(r"^data:image/(png|jpe?g|webp);base64,(.+)$", p or "", re.I | re.S)
        if not m:
            continue
        ext = "jpg" if m.group(1).lower().startswith("jp") else m.group(1).lower()
        try:
            raw = base64.b64decode(m.group(2))
        except Exception:
            continue
        stamp = datetime.now(IST).strftime("%Y%m%d-%H%M%S")
        path = f"{ONEDRIVE_BASE}/Maintenance/Breakdowns/BD{bid}_{idx+1}_{stamp}.{ext}"
        ct = "image/png" if ext == "png" else "image/jpeg" if ext in ("jpg", "jpeg") else "image/webp"
        try:
            await graph.upload_file(token, path, raw, ct)
            out.append(path)
        except Exception as e:
            print("[maint] breakdown photo upload failed:", e)
    return out


# =======================================================================
#  WORK-ORDER PDF REPORTS -> OneDrive (Maintenance/Preventive + /Breakdown)
#  Each checklist completion files a preventive report; each breakdown raised
#  files a breakdown report. All wrapped so a failure never breaks the action.
# =======================================================================
def _pm_rows(results: list, texts: dict, done_by: str, when: str) -> list:
    """Build the inspection-report rows (#, checkpoint text, result, recorded)."""
    out = []
    for i, r in enumerate(results or [], 1):
        pid = r.get("checkpoint_id")
        txt = texts.get(pid) or r.get("text") or (f"Checkpoint {pid}" if pid else "Checkpoint")
        res = (r.get("result") or ("Not OK" if r.get("ok") is False else "OK")) or "OK"
        note = (r.get("note") or "").strip()
        if note:
            res = f"{res} — {note}"
        out.append((str(i), txt, res, f"{when} · {done_by}"))
    return out


def _safe_code(code: str) -> str:
    """Machine code made safe for a OneDrive file/folder segment (no path chars)."""
    return (re.sub(r'[\\/:*?"<>|]+', "-", (code or "ASSET")).strip() or "ASSET")


def _machine_folder(code: str, name: str) -> str:
    """One folder per machine, e.g. 'M-C-005 TMT BENDING M-C 8 MM TO 32 MM'."""
    seg = f"{_safe_code(code)} {name or ''}".strip()
    seg = re.sub(r'[\\/:*?"<>|]+', "-", seg)
    seg = re.sub(r"\s+", " ", seg).strip()
    return seg[:120] or _safe_code(code)


_WO_ABSENT_READY = False
_WO_SKIP_READY = False


async def _ensure_skip_table():
    """Per-work-order 'skip this period' marks (admin, Quarterly PM). When a quarterly is
    skipped for a period it stops showing on the Monitor, the board and the reminders for
    that period, but the schedule keeps running for the NEXT quarter. Keyed by
    (checklist_id, period_key)."""
    global _WO_SKIP_READY
    if _WO_SKIP_READY:
        return
    try:
        await db.execute("""CREATE TABLE IF NOT EXISTS maint_wo_skip (
            id SERIAL PRIMARY KEY, checklist_id INT, period_key TEXT,
            skipped_by TEXT, skipped_at TIMESTAMPTZ DEFAULT now())""")
        await db.execute("CREATE UNIQUE INDEX IF NOT EXISTS ix_m_woskip "
                         "ON maint_wo_skip (checklist_id, period_key)")
        _WO_SKIP_READY = True
    except Exception as e:
        print("[maint] skip table ensure skip:", e)


async def _skipped_set(pks=None) -> set:
    """Set of (checklist_id, period_key) marked skipped (optionally limited to pks)."""
    await _ensure_skip_table()
    try:
        if pks:
            rows = await db.fetch(
                "SELECT checklist_id, period_key FROM maint_wo_skip WHERE period_key = ANY($1::text[])",
                list(pks))
        else:
            rows = await db.fetch("SELECT checklist_id, period_key FROM maint_wo_skip")
        return {(r["checklist_id"], r["period_key"]) for r in rows}
    except Exception:
        return set()


async def _ensure_absent_table():
    """Per-work-order 'assignee absent' marks (admin, Daily PM). Lazily created so we
    don't need a SCHEMA_VER bump. Keyed by (checklist_id, period_key, emp_id)."""
    global _WO_ABSENT_READY
    if _WO_ABSENT_READY:
        return
    try:
        await db.execute("""CREATE TABLE IF NOT EXISTS maint_wo_absent (
            id SERIAL PRIMARY KEY, checklist_id INT, period_key TEXT, emp_id INT,
            marked_by TEXT, marked_at TIMESTAMPTZ DEFAULT now())""")
        await db.execute("CREATE UNIQUE INDEX IF NOT EXISTS ix_m_woabs "
                         "ON maint_wo_absent (checklist_id, period_key, emp_id)")
        _WO_ABSENT_READY = True
    except Exception as e:
        print("[maint] absent table ensure skip:", e)


async def _absent_emp_ids(checklist_id, period_key) -> set:
    """Employee ids marked absent for this checklist+period."""
    await _ensure_absent_table()
    try:
        rows = await db.fetch(
            "SELECT emp_id FROM maint_wo_absent WHERE checklist_id=$1 AND period_key=$2",
            checklist_id, period_key)
        return {r["emp_id"] for r in rows if r["emp_id"] is not None}
    except Exception:
        return set()


async def _pm_rows_full(checklist_id, period_key):
    """Build the inspection rows from the FULL checklist state for one period:
    EVERY checkpoint of the checklist, each with its result and who marked it; a
    checkpoint not yet done shows 'Pending' with the person it is assigned to. This
    is what makes one combined report fill up as each assigned person submits their
    own checkpoints. Checkpoints assigned to anyone marked ABSENT for this period are
    left out entirely, so an absent person's points don't hold up or clutter the
    report. Returns (rows, contributors, pending_count)."""
    cps = await db.fetch(
        "SELECT id, seq, text, assigned_to, assigned_emp_id FROM maint_checkpoints "
        "WHERE checklist_id=$1 ORDER BY seq, id", checklist_id)
    rows, contributors, pending = [], [], 0
    if not cps:
        return rows, contributors, pending
    absent = await _absent_emp_ids(checklist_id, period_key)
    if absent:
        cps = [c for c in cps if c["assigned_emp_id"] not in absent]
    if not cps:
        return rows, contributors, pending
    ids = [r["id"] for r in cps]
    done = {}
    for d in await db.fetch(
            "SELECT checkpoint_id, emp_id, result, note, done_at FROM maint_checkpoint_done "
            "WHERE period_key=$1 AND checkpoint_id = ANY($2::int[])", period_key, ids):
        done[d["checkpoint_id"]] = dict(d)
    emp_ids = list({d["emp_id"] for d in done.values() if d.get("emp_id")})
    names = {}
    if emp_ids:
        for e in await db.fetch(
                "SELECT id, name, emp_no FROM employees WHERE id = ANY($1::int[])", emp_ids):
            names[e["id"]] = e["name"] or e["emp_no"]
    for i, cp in enumerate(cps, 1):
        d = done.get(cp["id"])
        if d:
            who = names.get(d.get("emp_id")) or (cp["assigned_to"] or "—")
            res = (d.get("result") or "OK")
            if (d.get("note") or "").strip():
                res = f"{res} — {d['note'].strip()}"
            dt = d.get("done_at")
            when_s = dt.astimezone(IST).strftime("%d %b %H:%M") if isinstance(dt, datetime) else ""
            rec = (f"{when_s} · {who}").strip(" ·")
            if who and who != "—" and who not in contributors:
                contributors.append(who)
        else:
            pending += 1
            res = "Pending"
            rec = cp["assigned_to"] or "—"
        rows.append((str(i), cp["text"], res, rec))
    return rows, contributors, pending


async def _file_pm_report(asset_code, asset_name, checklist, doc_no, done_by,
                          results, checklist_id, location="", when=None):
    """Generate a preventive-maintenance work-order PDF and save it to
    OneDrive/<base>/Maintenance/Reports/<machine>/<Daily|Quarterly Preventive
    Maintenance>/<dd-mm-yy>_<code>_pm.pdf. The filename is fixed per machine+day, so a
    re-submit the same day overwrites it — one PM report per machine per frequency per
    day. When the checklist's checkpoints are split
    across several people, the report is built from the full checklist state for the
    day (every checkpoint + who did it, pending ones flagged), so it fills up as each
    person submits instead of showing only the last submitter. Never raises."""
    try:
        from .. import maint_pdf
        now = datetime.now(IST)
        when = when or now          # the completion moment (backfill passes the log's date)
        wdate = when.astimezone(IST).date() if isinstance(when, datetime) else None
        rows, contributors, pending = [], [], 0
        if checklist_id:
            pk = _period_key(checklist, wdate)
            rows, contributors, pending = await _pm_rows_full(checklist_id, pk)
        if not rows:
            # fallback: no per-checkpoint data for this period (e.g. older history) —
            # build from the submitter's / log's own results, as before.
            texts = {}
            if checklist_id:
                for r in await db.fetch(
                        "SELECT id, text FROM maint_checkpoints WHERE checklist_id=$1", checklist_id):
                    texts[r["id"]] = r["text"]
            rows = _pm_rows(results, texts, done_by or "—", when.strftime("%d %b %H:%M"))
        completed_by = ", ".join(contributors) if contributors else (done_by or "—")
        status = "Completed" if pending == 0 else f"In progress — {pending} pending"
        meta = {"doc_no": doc_no or when.strftime("%y%m%d%H%M"),
                "asset_code": asset_code or "", "asset_name": asset_name or "",
                "checklist": checklist or "Checklist", "started": None, "completed": when,
                "due": None, "raised_by": done_by or "—", "completed_by": completed_by,
                "status": status,
                "category": "Scheduled Preventive Maintenance",
                "location": location or "--",
                "qr_url": "https://qms.bharatsteels.in/maint"}
        pdf = maint_pdf.build_pm_report(meta, rows)
        code = _safe_code(asset_code)
        folder = _machine_folder(asset_code, asset_name)
        # Reports/<machine>/<Daily|Quarterly Preventive Maintenance>/<file>
        sub = ("Quarterly Preventive Maintenance" if "quarter" in (checklist or "").lower()
               else "Daily Preventive Maintenance")
        fname = f"{when.strftime('%d-%m-%y')}_{code}_pm.pdf"   # one per machine per day per frequency
        path = f"{ONEDRIVE_BASE}/Maintenance/Reports/{folder}/{sub}/{fname}"
        token = await graph.get_token()
        await graph.upload_file(token, path, pdf, "application/pdf")
        print("[maint] PM report saved:", path)
        return path
    except Exception as e:
        print("[maint] PM report skipped:", e)
        return None


async def _file_breakdown_report(bd: dict, assignee_names: str):
    """Generate a breakdown work-order PDF and save it to
    OneDrive/<base>/Maintenance/Reports/<machine>/Breakdown Maintenance/<dd-mm-yy>_<code>_bm_<id>.pdf. The
    ticket id is kept in the name so two breakdowns on the same machine/day don't
    overwrite each other (each breakdown is its own event). Never raises."""
    try:
        from .. import maint_pdf
        now = datetime.now(IST)
        raised = bd.get("raised_at") or now
        meta = {"doc_no": str(bd.get("id") or now.strftime("%y%m%d%H%M")),
                "asset_code": bd.get("asset_code") or "", "asset_name": bd.get("asset_name") or "",
                "raised": raised, "raised_by": bd.get("reported_by") or "—",
                "btype": bd.get("btype") or "--", "problem": bd.get("problem") or "--",
                "assignees": assignee_names or "--", "location": bd.get("location") or "--",
                "status": (bd.get("status") or "open").title(),
                "closed": bd.get("closed_at"), "closed_by": bd.get("closed_by"),
                "resolution": bd.get("resolution"),
                "qr_url": "https://qms.bharatsteels.in/maint"}
        pdf = maint_pdf.build_breakdown_report(meta)
        code = _safe_code(bd.get("asset_code"))
        folder = _machine_folder(bd.get("asset_code"), bd.get("asset_name"))
        rdate = raised.strftime("%d-%m-%y") if isinstance(raised, datetime) else now.strftime("%d-%m-%y")
        fname = f"{rdate}_{code}_bm_{bd.get('id')}.pdf"
        path = f"{ONEDRIVE_BASE}/Maintenance/Reports/{folder}/Breakdown Maintenance/{fname}"
        token = await graph.get_token()
        await graph.upload_file(token, path, pdf, "application/pdf")
        print("[maint] breakdown report saved:", path)
        return path
    except Exception as e:
        print("[maint] breakdown report skipped:", e)
        return None


async def _regen_breakdown_pdf(bid: int):
    """Rebuild the OneDrive breakdown PDF for one work order from its CURRENT DB state
    (so a work order closed before the on-close regeneration shipped gets its 'pending'
    PDF replaced with the completed one). Returns the saved path, or None."""
    bd = await db.fetchrow(
        "SELECT b.*, a.code AS asset_code, a.name AS asset_name, a.location AS location "
        "FROM maint_breakdowns b LEFT JOIN maint_assets a ON a.id=b.asset_id WHERE b.id=$1", bid)
    if not bd:
        return None
    bd = dict(bd)
    aids = _bd_aids(bd)
    names = []
    if aids:
        nm = {r["id"]: (r["name"] or r["emp_no"]) for r in await db.fetch(
            "SELECT id, name, emp_no FROM employees WHERE id = ANY($1::int[])", aids)}
        names = [nm[i] for i in aids if nm.get(i)]
    return await _file_breakdown_report(bd, ", ".join(names))


_WLOG_TABLE_READY = False


async def _ensure_wati_log_table():
    """Create the delivery-log table if the schema fast-path skipped it (idempotent,
    runs once per process). Avoids a SCHEMA_VER bump re-running the heavy DDL/seeds."""
    global _WLOG_TABLE_READY
    if _WLOG_TABLE_READY:
        return
    try:
        await db.execute("""CREATE TABLE IF NOT EXISTS maint_wati_log (
            id SERIAL PRIMARY KEY, sent_at TIMESTAMPTZ DEFAULT now(),
            template TEXT, phone TEXT, name TEXT, kind TEXT, result TEXT, detail TEXT)""")
        await db.execute("CREATE INDEX IF NOT EXISTS ix_m_wlog ON maint_wati_log (sent_at DESC)")
        _WLOG_TABLE_READY = True
    except Exception as e:
        print("[maint] wati-log table ensure skip:", e)


async def _log_wati(template, phone, name, ok, detail, kind=""):
    """Record one WATI send + its outcome to the maintenance delivery log."""
    await _ensure_wati_log_table()
    d = str(detail or "")
    if ok:
        res = "sent"
    elif d.strip().startswith(("{", "[")) or any(k in d.lower() for k in ("http", "missing/wrong", "error")):
        res = "http_error"
    else:
        res = "failed"
    try:
        await db.execute(
            "INSERT INTO maint_wati_log (template, phone, name, kind, result, detail) "
            "VALUES ($1,$2,$3,$4,$5,$6)",
            str(template or ""), str(phone or ""), str(name or ""), str(kind or ""), res, d[:500])
    except Exception as e:
        print("[maint] wati-log skip:", e)


def _safe_param(v):
    """WhatsApp rejects a template parameter that contains a newline, a tab, or more
    than 4 consecutive spaces (the WATI 'Sample Content param text...' error). Flatten
    multi-line lists to a single ' / '-separated line so the send isn't dropped."""
    s = str(v if v is not None else "")
    s = re.sub(r"[\r\n\t]+", "    ", s)   # newlines/tabs -> 4-space gap (WhatsApp bans them in a variable)
    s = re.sub(r" {5,}", "    ", s)       # never MORE than 4 consecutive spaces
    return s.strip()


async def _wsend(template, phone, params, name="", kind=""):
    """Send a WATI template AND log the outcome. Returns (ok, detail) like send_template."""
    plist = params if isinstance(params, (list, tuple)) else [params]
    plist = [_safe_param(p) for p in plist]
    ok, detail = await wati.send_template(template, phone, plist)
    await _log_wati(template, phone, name, ok, detail, kind)
    return ok, detail


async def _send_breakdown_alert(bd: dict):
    """Fire bsc_breakdown_alert to the assignee (if any) + every supervisor."""
    label = (str(bd.get("asset_code") or "") + " " + str(bd.get("asset_name") or "")).strip() or "—"
    detail = (bd.get("problem") or "—").strip() or "—"
    btype = _norm_type(bd.get("btype"))
    problem = (f"{btype}: {detail}" if btype else detail)
    by = (bd.get("reported_by") or "—").strip() or "—"
    tm = datetime.now(IST).strftime("%d-%m-%Y %H:%M")
    ref = str(bd.get("id") or "")
    # Template order: {{1}}=Ref (also the button URL ?id=), {{2}}=Machine, {{3}}=Problem,
    # {{4}}=Reported by, {{5}}=Time. Keeping the ref in slot 1 makes the dynamic button URL
    # https://qms.bharatsteels.in/maint/breakdown?id={{1}} a valid link to this exact breakdown.
    params = [ref, label, problem, by, tm]
    targets, seen = [], set()
    # all assigned maintenance users (multi) get the alert
    aids = bd.get("assignees") or ([bd["assignee_emp_id"]] if bd.get("assignee_emp_id") else [])
    emap = await _emp_map()
    for aid in aids:
        info = emap.get(aid) or {}
        mob = info.get("mobile") or (bd.get("assignee_mobile") if aid == bd.get("assignee_emp_id") else "")
        m = wati.normalize_number(mob)
        if m and m not in seen:
            targets.append((m, info.get("name") or "")); seen.add(m)
    for sup in _supervisor_mobiles():
        m = wati.normalize_number(sup)
        if m and m not in seen:
            targets.append((m, "Supervisor")); seen.add(m)
    results = []
    for m, nm in targets:
        ok, detail = await _wsend(WATI_MAINT_BREAKDOWN_TEMPLATE, m, params, name=nm, kind="breakdown")
        results.append({"to": m, "result": "sent" if ok else f"failed: {detail}"})
    return results


def _bd_aids(row) -> list:
    """Assignee emp-id list for a breakdown row (assignees JSONB, falling back to the
    single assignee_emp_id)."""
    ag = row.get("assignees")
    if isinstance(ag, str):
        try:
            ag = json.loads(ag)
        except Exception:
            ag = []
    if not ag:
        ag = [row["assignee_emp_id"]] if row.get("assignee_emp_id") else []
    out = []
    for x in (ag or []):
        try:
            out.append(int(x))
        except (TypeError, ValueError):
            pass
    return out


def _can_close_bd(emp: dict, row) -> bool:
    """A work order may be closed by an admin or by a person it is assigned to."""
    return bool(emp.get("is_admin")) or (emp.get("id") in _bd_aids(row))


def _can_delete_bd(emp: dict, row) -> bool:
    """A work order may be deleted by an admin, the person who raised it, or an assignee."""
    return (bool(emp.get("is_admin"))
            or emp.get("id") == row.get("reported_by_emp_id")
            or (emp.get("id") in _bd_aids(row)))


@router.get("/breakdowns/list")
async def breakdowns_list(emp: dict = Depends(require_employee)):
    """Work orders for the Work Order page, split for its two tables and role-scoped:
    a normal user sees only the work orders they RAISED ('assigned by me') and the ones
    ASSIGNED to them ('assigned to'); an admin sees every work order (in 'all')."""
    await ensure_tables()
    uid = emp.get("id")
    is_admin = bool(emp.get("is_admin"))
    rows = await db.fetch(
        "SELECT b.id, b.asset_id, b.btype, b.wo_type, b.problem, b.reported_by, b.reported_by_emp_id, "
        "b.assignee_emp_id, b.assignees, b.status, b.raised_at, b.closed_at, b.closed_by, b.remarks, "
        "b.resolution, b.photos, b.close_types, b.close_causes, "
        "a.code AS asset_code, a.name AS asset_name, e.name AS assignee_name "
        "FROM maint_breakdowns b LEFT JOIN maint_assets a ON a.id=b.asset_id "
        "LEFT JOIN employees e ON e.id=b.assignee_emp_id "
        "WHERE lower(coalesce(b.status,'open')) <> 'closed' "
        "OR b.closed_at > now() - interval '7 days' "
        "ORDER BY (lower(coalesce(b.status,'open')) <> 'closed') DESC, b.raised_at DESC")
    data = [dict(r) for r in rows]
    raised, assigned, all_rows = [], [], []
    for b in data:
        aids = _bd_aids(b)
        if is_admin:
            all_rows.append(b)
        else:
            if b.get("reported_by_emp_id") == uid:
                raised.append(b)
            if uid in aids:
                assigned.append(b)
    return _json({"is_admin": is_admin, "uid": uid,
                  "raised": raised, "assigned": assigned, "all": all_rows})


def _mask(num: str) -> str:
    num = str(num or "")
    return (num[:4] + "****" + num[-2:]) if len(num) >= 6 else (num or "—")


@router.get("/wati-status")
async def wati_status(emp: dict = Depends(require_employee)):
    """Diagnostics: what WhatsApp (WATI) config the running app actually sees.
    Open qms.bharatsteels.in/maint/wati-status in a browser."""
    await ensure_tables()
    st = wati.status()
    sups = _supervisor_mobiles()
    st["breakdown_template"] = WATI_MAINT_BREAKDOWN_TEMPLATE
    st["supervisor_count"] = len(sups)
    st["supervisor_sample"] = _mask(sups[0]) if sups else "(none set)"
    return _json(st)


@router.get("/wati-test")
async def wati_test(to: str = "", emp: dict = Depends(require_employee)):
    """Diagnostics: send the breakdown template to ONE number and return WATI's raw reply.
    Open qms.bharatsteels.in/maint/wati-test?to=919XXXXXXXXX in a browser."""
    await ensure_tables()
    num = wati.normalize_number(to) if to else ""
    if not num:
        sups = _supervisor_mobiles()
        num = wati.normalize_number(sups[0]) if sups else ""
    if not num:
        return _json({"ok": False,
                      "detail": "No number. Add ?to=919XXXXXXXXX to the URL, or set MAINT_SUPERVISOR_MOBILE."})
    params = ["0", "TEST MACHINE", "Other: WATI test message", "Diagnostics",
              datetime.now(IST).strftime("%d-%m-%Y %H:%M")]
    ok, detail = await _wsend(WATI_MAINT_BREAKDOWN_TEMPLATE, num, params, name="(test)", kind="test")
    return _json({"ok": ok, "sent_to": num, "template": WATI_MAINT_BREAKDOWN_TEMPLATE, "wati_reply": detail})


@router.get("/wati-template")
async def wati_template(name: str = "", emp: dict = Depends(require_employee)):
    """Diagnostics: read ONE approved template's definition straight from WATI, so we can
    see its exact body variables + button. Open qms.bharatsteels.in/maint/wati-template"""
    await ensure_tables()
    name = name or WATI_MAINT_BREAKDOWN_TEMPLATE
    j, detail = await wati.fetch_templates()
    if j is None:
        return _json({"error": detail})
    items = None
    if isinstance(j, dict):
        items = (j.get("messageTemplates") or j.get("templates")
                 or j.get("data") or j.get("result") or [])
    elif isinstance(j, list):
        items = j
    items = items or []

    def _nm(t):
        return t.get("elementName") or t.get("templateName") or t.get("name") or ""

    match = next((t for t in items if _nm(t) == name), None)
    if not match:
        return _json({"error": f"template '{name}' not found",
                      "available": [_nm(t) for t in items][:60]})
    # Pull out the useful bits: body text, its {{n}} count, and buttons
    body_text = ""
    buttons = None
    comps = match.get("components") or match.get("template") or []
    if isinstance(comps, list):
        for c in comps:
            ct = (c.get("type") or "").upper()
            if ct == "BODY":
                body_text = c.get("text") or body_text
            if ct == "BUTTONS":
                buttons = c.get("buttons")
    body_text = body_text or match.get("body") or ""
    if buttons is None:
        buttons = match.get("buttons")
    var_count = len(re.findall(r"\{\{\s*\d+\s*\}\}", body_text or ""))
    return _json({"name": name, "body_text": body_text, "body_variable_count": var_count,
                  "buttons": buttons, "raw": match})


@router.get("/wati-log/list")
async def wati_log_list(result: str = Query(default=""), template: str = Query(default=""),
                        phone: str = Query(default=""), limit: int = Query(default=300),
                        emp: dict = Depends(require_employee)):
    """Rows for the WhatsApp delivery log (newest first) with simple filters, plus a
    last-24h count by result. Used by the maintenance WhatsApp Log page (admin only)."""
    await ensure_tables()
    if not emp.get("is_admin"):
        raise HTTPException(403, "Admins only")
    await _ensure_wati_log_table()
    where, args = [], []
    if result:
        args.append(result); where.append(f"result=${len(args)}")
    if template:
        args.append(f"%{template}%"); where.append(f"template ILIKE ${len(args)}")
    if phone:
        args.append(f"%{re.sub(r'[^0-9]', '', phone)}%"); where.append(f"phone ILIKE ${len(args)}")
    wsql = (" WHERE " + " AND ".join(where)) if where else ""
    args.append(max(1, min(int(limit or 300), 1000)))
    rows = await db.fetch(
        "SELECT id, sent_at, template, phone, name, kind, result, detail "
        "FROM maint_wati_log" + wsql + f" ORDER BY sent_at DESC LIMIT ${len(args)}", *args)
    summ = await db.fetch(
        "SELECT result, COUNT(*) AS n FROM maint_wati_log "
        "WHERE sent_at > now() - interval '24 hours' GROUP BY result")
    return _json({"rows": [dict(r) for r in rows],
                  "summary": {r["result"]: r["n"] for r in summ}})


# ---- fixed maintenance person per breakdown type (hard-coded in the backend) ----
# Mechanical -> Velu C ; Hydraulic -> Surajit Sasanka Maity. Electrical is per-asset
# (maint_asset_elec). Matched to the employees table by mobile (preferred) or name.
FIXED_TYPE_STAFF = {
    "Mechanical": {"mobile": "9840816602", "name": "velu"},
    "Hydraulic":  {"mobile": "9324774674", "name": "surajit"},
}


async def _fixed_type_staff() -> dict:
    """Resolve the hard-coded fixed users to live employee ids. {category: [emp_id]}."""
    out = {}
    for cat, info in FIXED_TYPE_STAFF.items():
        try:
            row = await db.fetchrow(
                "SELECT id FROM employees "
                "WHERE COALESCE(mobile,'') LIKE '%'||$1||'%' OR lower(COALESCE(name,'')) LIKE '%'||$2||'%' "
                "ORDER BY (COALESCE(mobile,'') LIKE '%'||$1||'%') DESC, id LIMIT 1",
                info["mobile"], info["name"])
            if row:
                out[cat] = [row["id"]]
        except Exception as e:
            print("[maint] fixed type-staff resolve skipped:", cat, e)
    return out


@router.get("/type-staff/list")
async def type_staff_list(emp: dict = Depends(require_employee)):
    """{category: [emp_id]} — Mechanical/Hydraulic are fixed in the backend."""
    await ensure_tables()
    return _json(await _fixed_type_staff())


@router.post("/type-staff/save")
async def type_staff_save(body: dict, emp: dict = Depends(require_employee)):
    """Replace the whole staff list for one breakdown type. body: {category, emp_ids:[...]}."""
    await ensure_tables()
    cat = _norm_type(body.get("category"))
    if not cat:
        raise HTTPException(400, "Pick a breakdown type")
    ids = body.get("emp_ids")
    if ids is None and body.get("emp_id"):   # back-compat single value
        ids = [body.get("emp_id")]
    seen, clean = set(), []
    for x in (ids or []):
        try:
            n = int(x)
        except Exception:
            continue
        if n and n not in seen:
            seen.add(n); clean.append(n)
    await db.execute("DELETE FROM maint_type_staff WHERE category=$1", cat)
    for eid in clean:
        await db.execute(
            "INSERT INTO maint_type_staff (category,emp_id) VALUES ($1,$2) ON CONFLICT DO NOTHING",
            cat, eid)
    return {"ok": True}


# ---- per-asset electrical maintenance users ----
# Default rule (when no explicit mapping is set for the asset):
#   SHED A / SHED B  (and RO PLANT, booster, embossing, transformer) -> Ragupathi
#   everything else (DG sets, SHED C/E/F, ...)                        -> Mohan
ELEC_RAGUPATHI = "ragupathi"
ELEC_MOHAN = "mohan"
_ELEC_RAG_NAMES = ("RO PLANT", "ROPLANT", "BOOSTER", "EMBOSS", "TRANSFORMER", "ESCORT")
_ELEC_MOHAN_NAMES = ("WEIGH",)   # weighing machines -> Mohan (overrides the Ragupathi rule)


async def _elec_default_ids():
    async def _one(nm):
        try:
            r = await db.fetchrow(
                "SELECT id FROM employees WHERE lower(COALESCE(name,'')) LIKE '%'||$1||'%' ORDER BY id LIMIT 1", nm)
            return r["id"] if r else None
        except Exception:
            return None
    return await _one(ELEC_RAGUPATHI), await _one(ELEC_MOHAN)


def _elec_default_for(asset: dict, rag, moh):
    loc = (asset.get("location") or "").upper().replace(" ", "")
    nm = (asset.get("name") or "").upper()
    if any(k in nm for k in _ELEC_MOHAN_NAMES):   # weighing -> Mohan, takes precedence
        return moh
    is_rag = ("SHEDA" in loc or "ASHED" in loc or "SHEDB" in loc or "BSHED" in loc
              or any(k in nm for k in _ELEC_RAG_NAMES))
    return (rag if is_rag else moh)


@router.get("/asset-elec/list")
async def asset_elec_list(emp: dict = Depends(require_employee)):
    """{asset_id: [emp_id, ...]} — explicit electrical users, else the location-based default."""
    await ensure_tables()
    try:
        rows = await db.fetch("SELECT asset_id, emp_id FROM maint_asset_elec")
    except Exception:
        rows = []
    out = {}
    for r in rows:
        out.setdefault(str(r["asset_id"]), []).append(r["emp_id"])
    # fill a default for every active asset that has no explicit mapping
    try:
        rag, moh = await _elec_default_ids()
        if rag or moh:
            assets = await db.fetch("SELECT id, name, location FROM maint_assets WHERE COALESCE(active,true)")
            for a in assets:
                k = str(a["id"])
                if k not in out:
                    d = _elec_default_for(dict(a), rag, moh)
                    if d:
                        out[k] = [d]
    except Exception as e:
        print("[maint] elec default fill skipped:", e)
    return _json(out)


@router.post("/asset-elec/save")
async def asset_elec_save(body: dict, emp: dict = Depends(require_employee)):
    """Replace the electrical-user list for one asset. body: {asset_id, emp_ids:[...]}."""
    await ensure_tables()
    aid = body.get("asset_id")
    if not aid:
        raise HTTPException(400, "Missing asset")
    seen, clean = set(), []
    for x in (body.get("emp_ids") or []):
        try:
            n = int(x)
        except Exception:
            continue
        if n and n not in seen:
            seen.add(n); clean.append(n)
    await db.execute("DELETE FROM maint_asset_elec WHERE asset_id=$1", int(aid))
    for eid in clean:
        await db.execute(
            "INSERT INTO maint_asset_elec (asset_id,emp_id) VALUES ($1,$2) ON CONFLICT DO NOTHING",
            int(aid), eid)
    return {"ok": True}


def _wo_type(v):
    """Work-order type: 'Breakdown' or 'Unscheduled PM'. Anything unknown -> Breakdown."""
    s = (v or "").strip().lower()
    if s in ("unscheduled pm", "unscheduled preventive maintenance", "unscheduled", "upm", "pm"):
        return "Unscheduled PM"
    return "Breakdown"


@router.post("/breakdown/raise")
async def breakdown_raise(body: dict, emp: dict = Depends(require_employee)):
    await ensure_tables()
    try:
        aid = int(body.get("asset_id"))          # column is INT — coerce the form's string
    except (TypeError, ValueError):
        aid = None
    # Work-order type — only maintenance users / admins may choose; everyone else is Breakdown.
    wo_type = _wo_type(body.get("wo_type")) if (emp.get("is_maint") or emp.get("is_admin")) else "Breakdown"
    btype = _norm_type(body.get("btype"))
    problem = (body.get("problem") or "").strip()
    # one or many maintenance users
    assignees = body.get("assignees")
    if assignees is None and body.get("assignee_emp_id"):
        assignees = [body.get("assignee_emp_id")]
    seen, aids = set(), []
    for x in (assignees or []):
        try:
            n = int(x)
        except Exception:
            continue
        if n and n not in seen:
            seen.add(n); aids.append(n)
    if not aid:
        raise HTTPException(400, "Select a machine")
    if not btype:
        raise HTTPException(400, "Select a breakdown type")
    if not problem:
        raise HTTPException(400, "Describe the breakdown")
    if not aids:
        raise HTTPException(400, "Select at least one maintenance user")
    primary = aids[0]
    raised_at = _dt(body.get("raised_at"))   # user-supplied date+time; falls back to now
    reporter = emp.get("name") or emp.get("emp_no") or "staff"
    remarks = (body.get("remarks") or "").strip()
    bid = await db.fetchval(
        "INSERT INTO maint_breakdowns (asset_id,btype,problem,reported_by,reported_by_emp_id,"
        "assignee_emp_id,assignees,status,remarks,raised_at,wo_type) "
        "VALUES ($1,$2,$3,$4,$5,$6,$7,'open',$8,COALESCE($9,now()),$10) RETURNING id",
        aid, btype, problem, reporter, emp.get("id"), primary, aids, remarks, raised_at, wo_type)
    # photos -> OneDrive (optional)
    paths = []
    try:
        paths = await _breakdown_photos(bid, body.get("photos") or [])
        if paths:
            # jsonb codec encodes the list itself — pass the raw list, not a string
            await db.execute("UPDATE maint_breakdowns SET photos=$1 WHERE id=$2",
                             paths, bid)
    except Exception as e:
        print("[maint] breakdown photos skipped:", e)
    bd = await db.fetchrow(
        "SELECT b.*, a.code AS asset_code, a.name AS asset_name, a.location AS location, "
        "e.mobile AS assignee_mobile "
        "FROM maint_breakdowns b LEFT JOIN maint_assets a ON a.id=b.asset_id "
        "LEFT JOIN employees e ON e.id=b.assignee_emp_id WHERE b.id=$1", bid)
    alert = []
    try:
        alert = await _send_breakdown_alert(dict(bd))
    except Exception as e:
        print("[maint] breakdown alert skipped:", e)
    # work-order PDF -> OneDrive/Maintenance/Breakdown (background; see note above)
    try:
        names = []
        if aids:
            nm = {r["id"]: (r["name"] or r["emp_no"]) for r in await db.fetch(
                "SELECT id, name, emp_no FROM employees WHERE id = ANY($1::int[])", aids)}
            names = [nm[i] for i in aids if nm.get(i)]
        asyncio.create_task(_file_breakdown_report(dict(bd), ", ".join(names)))
    except Exception as e:
        print("[maint] breakdown report skipped:", e)
    return {"ok": True, "id": bid, "photos": len(paths), "alert": alert}


@router.post("/breakdown/close")
async def breakdown_close(body: dict, emp: dict = Depends(require_employee)):
    await ensure_tables()
    try:
        bid = int(body.get("id"))
    except (TypeError, ValueError):
        raise HTTPException(400, "Missing id")
    _crow = await db.fetchrow(
        "SELECT reported_by_emp_id, assignee_emp_id, assignees FROM maint_breakdowns WHERE id=$1", bid)
    if not _crow:
        raise HTTPException(404, "Work order not found")
    if not _can_close_bd(emp, dict(_crow)):
        raise HTTPException(403, "You can only close work orders assigned to you")
    action = (body.get("action") or "").strip()        # what was done to fix it
    note = (body.get("remarks") or "").strip()          # optional extra remark
    # completion time from the form (local IST); fall back to now
    closed = None
    ct = (body.get("closed_at") or "").strip()
    if ct:
        try:
            closed = datetime.fromisoformat(ct[:16]).replace(tzinfo=IST)
        except Exception:
            closed = None
    # fault classification captured at close (both multi-select) — kept as lists
    def _slist(x):
        if isinstance(x, list):
            return [str(i).strip() for i in x if str(i).strip()]
        s = str(x or "").strip()
        return [s] if s else []
    ctypes = _slist(body.get("types"))
    ccauses = _slist(body.get("causes"))
    # Type and Cause are mandatory to close a work order
    if not ctypes:
        raise HTTPException(400, "Select at least one Type")
    if not ccauses:
        raise HTTPException(400, "Select at least one Cause")
    await db.execute(
        "UPDATE maint_breakdowns SET status='closed', closed_at=COALESCE($4, now()), "
        "closed_by=$2, resolution=$5, close_types=$6, close_causes=$7, "
        "remarks = CASE WHEN $3='' THEN remarks "
        "WHEN COALESCE(remarks,'')='' THEN $3 ELSE remarks || ' | closed: ' || $3 END "
        "WHERE id=$1", bid,
        emp.get("name") or emp.get("emp_no") or "staff", note, closed, action,
        json.dumps(ctypes), json.dumps(ccauses))
    # Regenerate the OneDrive breakdown PDF so it reflects the CLOSED / completed state
    # (overwrites the earlier "pending" copy). Awaited — Vercel kills post-response work.
    pdf_path = None
    try:
        pdf_path = await _regen_breakdown_pdf(bid)
    except Exception as e:
        print("[maint] breakdown close PDF regen skipped:", e)
    return {"ok": True, "pdf": pdf_path}


@router.get("/breakdown/audit")
async def breakdown_audit(token: str = Query(default=""), code: str = Query(default=""),
                          only_open: int = Query(default=0),
                          emp: dict = Depends(current_employee)):
    """See every breakdown's real status, to explain why the board shows one as open.
    The board only shows a breakdown when its status is exactly 'open'.
      /maint/breakdown/audit?token=<AGENT_TOKEN>                 all breakdowns
      /maint/breakdown/audit?token=<AGENT_TOKEN>&only_open=1      only the open ones
      /maint/breakdown/audit?token=<AGENT_TOKEN>&code=M/C-002     one machine
    """
    await ensure_tables()
    if not ((emp and emp.get("is_admin")) or (AGENT_TOKEN and token == AGENT_TOKEN)):
        raise HTTPException(403, "Admins only")
    where, args = [], []
    if code.strip():
        args.append(code.strip()); where.append(f"upper(a.code)=upper(${len(args)})")
    if only_open:
        where.append("lower(coalesce(b.status,'open'))='open'")
    sql = ("SELECT b.id, a.code AS asset_code, a.name AS asset_name, b.status, "
           "b.raised_at, b.closed_at, b.closed_by, b.reported_by, b.assignee_emp_id, b.assignees, "
           "e.name AS assignee_name "
           "FROM maint_breakdowns b LEFT JOIN maint_assets a ON a.id=b.asset_id "
           "LEFT JOIN employees e ON e.id=b.assignee_emp_id")
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY b.raised_at DESC"
    rows = await db.fetch(sql, *args)
    out = []
    for r in rows:
        d = dict(r)
        st = (d.get("status") or "")
        out.append({
            "id": d["id"], "machine": ((d.get("asset_code") or "") + " " + (d.get("asset_name") or "")).strip(),
            "status_stored": st,
            "shows_on_board": st == "open",   # board uses an exact 'open' match
            "raised_at": str(d.get("raised_at") or "")[:16],
            "closed_at": str(d.get("closed_at") or "") or None,
            "closed_by": d.get("closed_by"),
            "raised_by": d.get("reported_by"),
            "assignee": d.get("assignee_name")})
    open_board = sum(1 for x in out if x["shows_on_board"])
    return _json({"count": len(out), "showing_on_board": open_board, "breakdowns": out})


@router.get("/breakdown/cleanup")
async def breakdown_cleanup(token: str = Query(default=""), ids: str = Query(default=""),
                            code: str = Query(default=""), action: str = Query(default="close"),
                            apply: int = Query(default=0),
                            emp: dict = Depends(current_employee)):
    """Clear leftover OPEN breakdowns that are stuck on the board (never properly closed,
    or duplicate test rows). Only ever touches rows that are still open (no close time).
      close (default): mark them completed so they leave the board.
      delete:          remove the row entirely (for junk / duplicates).
    Target by ids or by machine code. Admin or ?token=AGENT_TOKEN. Dry-run unless apply=1.
      /maint/breakdown/cleanup?token=<AGENT_TOKEN>&code=M/C-002                 (dry run)
      /maint/breakdown/cleanup?token=<AGENT_TOKEN>&ids=12,13&action=close&apply=1
      /maint/breakdown/cleanup?token=<AGENT_TOKEN>&ids=12&action=delete&apply=1
    """
    await ensure_tables()
    if not ((emp and emp.get("is_admin")) or (AGENT_TOKEN and token == AGENT_TOKEN)):
        raise HTTPException(403, "Admins only")
    action = (action or "close").strip().lower()
    if action not in ("close", "delete"):
        raise HTTPException(400, "action must be close or delete")
    # Only consider rows that are still OPEN (nothing with a close time is touched).
    where = ["lower(btrim(coalesce(b.status,'open')))='open'", "b.closed_at IS NULL"]
    args = []
    id_list = [int(x) for x in ids.split(",") if x.strip().isdigit()]
    if id_list:
        args.append(id_list); where.append(f"b.id = ANY(${len(args)}::int[])")
    if code.strip():
        args.append(code.strip()); where.append(f"upper(a.code)=upper(${len(args)})")
    if not id_list and not code.strip():
        raise HTTPException(400, "Pass ids= or code= so nothing is cleared by accident")
    rows = await db.fetch(
        "SELECT b.id, a.code AS asset_code, a.name AS asset_name, b.problem, b.raised_at "
        "FROM maint_breakdowns b LEFT JOIN maint_assets a ON a.id=b.asset_id "
        "WHERE " + " AND ".join(where) + " ORDER BY b.raised_at DESC", *args)
    targets = [{"id": r["id"], "machine": ((r["asset_code"] or "") + " " + (r["asset_name"] or "")).strip(),
                "problem": (r["problem"] or "")[:60], "raised_at": str(r["raised_at"] or "")[:16]}
               for r in rows]
    if not apply:
        return _json({"apply": False, "action": action, "would_affect": len(targets), "targets": targets})
    done = []
    for t in targets:
        bid = t["id"]
        try:
            if action == "delete":
                await db.execute("DELETE FROM maint_breakdowns WHERE id=$1", bid)
            else:
                await db.execute(
                    "UPDATE maint_breakdowns SET status='closed', closed_at=now(), "
                    "closed_by=COALESCE(closed_by,$2), "
                    "resolution=COALESCE(NULLIF(btrim(resolution),''),'Closed (admin cleanup)') "
                    "WHERE id=$1", bid, (emp.get("name") if emp else None) or "admin cleanup")
                try:
                    await _regen_breakdown_pdf(bid)
                except Exception:
                    pass
            done.append(t)
        except Exception as e:
            t["error"] = str(e)[:160]
    return _json({"apply": True, "action": action, "affected": len(done), "targets": done})


@router.get("/breakdown/regen-pdf")
async def breakdown_regen_pdf(token: str = Query(default=""), id: int = Query(default=0),
                              ids: str = Query(default=""), days: int = Query(default=7),
                              emp: dict = Depends(current_employee)):
    """Rebuild OneDrive breakdown PDFs from the CURRENT database state — fixes PDFs that
    still show 'pending' because the work order was closed before on-close regeneration
    shipped. Admin or ?token=AGENT_TOKEN.
      ?id=30         one work order
      ?ids=30,34     several
      (no id/ids)    every breakdown closed in the last <days> days (default 7)."""
    await ensure_tables()
    if not ((emp and emp.get("is_admin")) or (AGENT_TOKEN and token == AGENT_TOKEN)):
        raise HTTPException(403, "Admins only")
    targets = []
    if id:
        targets = [id]
    elif ids.strip():
        targets = [int(x) for x in ids.split(",") if x.strip().isdigit()]
    else:
        rows = await db.fetch(
            "SELECT id FROM maint_breakdowns WHERE status='closed' "
            "AND closed_at > now() - ($1 * interval '1 day') ORDER BY id", int(days))
        targets = [r["id"] for r in rows]
    out = []
    for bid in targets:
        row = await db.fetchrow(
            "SELECT id, status, closed_at, resolution FROM maint_breakdowns WHERE id=$1", bid)
        if not row:
            out.append({"id": bid, "ok": False,
                        "reason": "row not found in database (likely lost in migration)"})
            continue
        try:
            p = await _regen_breakdown_pdf(bid)
            out.append({"id": bid, "ok": bool(p),
                        "status": row["status"], "closed": bool(row["closed_at"]),
                        "has_action_taken": bool((row["resolution"] or "").strip()),
                        "reason": (None if p else "PDF build or OneDrive upload failed"),
                        "pdf": p})
        except Exception as e:
            out.append({"id": bid, "ok": False, "reason": str(e)[:160]})
    return _json({"count": len(out), "regenerated": out})


@router.get("/reconcile")
async def reconcile_migration(token: str = Query(default=""), emp: dict = Depends(current_employee)):
    """Migration-loss report: find records whose OneDrive PDF exists but whose row is
    MISSING from the current database. Breakdown PDFs carry the DB id in the file name
    (..._bm_<id>.pdf); coil inward PDFs carry the batch (<batch>_(date)_Inward.pdf). We
    list those PDFs and check each id/batch against the DB. Admin or ?token=AGENT_TOKEN.
    Open qms.bharatsteels.in/maint/reconcile?token=<AGENT_TOKEN>."""
    await ensure_tables()
    if not ((emp and emp.get("is_admin")) or (AGENT_TOKEN and token == AGENT_TOKEN)):
        raise HTTPException(403, "Admins only")
    import re as _re
    tok = await graph.get_token()

    # ---- breakdowns: Maintenance/Reports/<machine>/Breakdown Maintenance/<date>_<code>_bm_<id>.pdf
    base = f"{ONEDRIVE_BASE}/Maintenance/Reports"
    bd_ids, bd_err = set(), None
    try:
        machines = [m for m in await graph.list_children(tok, base) if m.get("is_folder")]

        async def _scan(m):
            found = set()
            for f in await graph.list_children(tok, f"{base}/{m['name']}/Breakdown Maintenance"):
                mm = _re.search(r"_bm_(\d+)\.pdf$", f.get("name") or "", _re.I)
                if mm:
                    found.add(int(mm.group(1)))
            return found
        for res in await asyncio.gather(*[_scan(m) for m in machines], return_exceptions=True):
            if isinstance(res, set):
                bd_ids |= res
    except Exception as e:
        bd_err = str(e)[:150]
    bd_missing = []
    if bd_ids:
        have = {r["id"] for r in await db.fetch(
            "SELECT id FROM maint_breakdowns WHERE id = ANY($1::int[])", list(bd_ids))}
        bd_missing = sorted(bd_ids - have)

    # ---- coils: Inward/PDF/<batch>_(date)_Inward.pdf
    # A coil is "present" if its batch is in the new DB table OR in the legacy Inward_Log.xlsx
    # (the app's coil list merges both). Only batches in NEITHER are genuine migration losses;
    # the 900+ legacy coils have PDFs but live only in the xlsx and are NOT lost.
    def _nb(x):
        return (x or "").strip().upper()
    coil_batches, coil_err = {}, None   # normalised batch -> original filename
    try:
        for f in await graph.list_children(tok, f"{ONEDRIVE_BASE}/Inward/PDF"):
            nm = f.get("name") or ""
            if nm.lower().endswith(".pdf") and "_(" in nm:
                b = _nb(nm.split("_(")[0])
                if b:
                    coil_batches.setdefault(b, nm)
    except Exception as e:
        coil_err = str(e)[:150]
    db_batches = legacy_batches = 0
    coil_missing = []
    if coil_batches:
        present = {_nb(r["batch_number"]) for r in await db.fetch(
            "SELECT batch_number FROM coil_inward")}
        db_batches = len(present)
        try:
            from .coil import _read_inward_legacy
            leg = {_nb(r.get("batch_number")) for r in await _read_inward_legacy()}
            leg.discard("")
            legacy_batches = len(leg)
            present |= leg
        except Exception as e:
            coil_err = (coil_err or "") + f" | legacy read: {str(e)[:90]}"
        coil_missing = sorted([b for b in coil_batches if b not in present])

    return _json({
        "note": "missing = PDF in OneDrive but the record is in NEITHER the database NOR the legacy Inward_Log.xlsx (genuine migration loss). Legacy coils that live only in the xlsx are excluded.",
        "breakdowns": {"onedrive_pdfs": len(bd_ids), "missing_count": len(bd_missing),
                       "missing_ids": bd_missing, "error": bd_err},
        "coils": {"onedrive_pdfs": len(coil_batches), "in_db": db_batches,
                  "in_legacy_xlsx": legacy_batches, "missing_count": len(coil_missing),
                  "missing_batches": coil_missing, "error": coil_err},
    })


@router.get("/breakdown/restore")
async def breakdown_restore(token: str = Query(default=""), id: int = Query(default=0),
                            code: str = Query(default=""), problem: str = Query(default=""),
                            by: str = Query(default=""), who: str = Query(default=""),
                            btype: str = Query(default="Mechanical"),
                            raised: str = Query(default=""), closed: str = Query(default=""),
                            action: str = Query(default="Completed"),
                            status: str = Query(default="closed"),
                            emp: dict = Depends(current_employee)):
    """Restore / re-state a breakdown work order: re-insert (or update) the row with its
    ORIGINAL id and rewrite its OneDrive PDF. status=closed (default) marks it completed;
    status=open marks it an active, unfinished work order (clears the action/closed fields).
    Admin or ?token=AGENT_TOKEN. Dates as YYYY-MM-DDTHH:MM (IST). Example:
    /maint/breakdown/restore?token=..&id=30&code=M/C-002&who=Velu C&by=Kumar Balakrishnan
      &problem=Shearing clamp shadblod change work&raised=2026-11-03T15:54&status=open"""
    await ensure_tables()
    if not ((emp and emp.get("is_admin")) or (AGENT_TOKEN and token == AGENT_TOKEN)):
        raise HTTPException(403, "Admins only")
    if not id or not code.strip():
        raise HTTPException(400, "id and code are required")
    is_closed = "open" not in status.strip().lower()
    status_val = "closed" if is_closed else "open"
    asset = await db.fetchrow("SELECT id FROM maint_assets WHERE upper(code)=upper($1) LIMIT 1", code.strip())
    asset_id = asset["id"] if asset else None
    aemp = await db.fetchrow("SELECT id FROM employees WHERE lower(name)=lower($1) LIMIT 1",
                             who.strip()) if who.strip() else None
    aid = aemp["id"] if aemp else None
    bemp = await db.fetchrow("SELECT id FROM employees WHERE lower(name)=lower($1) LIMIT 1",
                             by.strip()) if by.strip() else None
    bid_emp = bemp["id"] if bemp else None

    def _pdt(s, default):
        s = (s or "").strip()
        if not s:
            return default
        try:
            return datetime.fromisoformat(s[:16]).replace(tzinfo=IST)
        except Exception:
            return default
    raised_at = _pdt(raised, datetime.now(IST))
    closed_at = _pdt(closed, raised_at) if is_closed else None
    closed_by = (who.strip() or by.strip() or "staff") if is_closed else None
    resolution = (action.strip() or "Completed") if is_closed else None
    assignees = json.dumps([aid] if aid else [])
    await db.execute(
        "INSERT INTO maint_breakdowns (id, asset_id, btype, problem, reported_by, reported_by_emp_id, "
        "assignee_emp_id, assignees, status, raised_at, closed_at, closed_by, resolution, wo_type, "
        "close_types, close_causes) "
        "VALUES ($1,$2,$3,$4,$5,$6,$7,$8::jsonb,$9,$10,$11,$12,$13,'Breakdown','[]'::jsonb,'[]'::jsonb) "
        "ON CONFLICT (id) DO UPDATE SET asset_id=EXCLUDED.asset_id, btype=EXCLUDED.btype, "
        "problem=EXCLUDED.problem, reported_by=EXCLUDED.reported_by, "
        "reported_by_emp_id=EXCLUDED.reported_by_emp_id, assignee_emp_id=EXCLUDED.assignee_emp_id, "
        "assignees=EXCLUDED.assignees, status=EXCLUDED.status, raised_at=EXCLUDED.raised_at, "
        "closed_at=EXCLUDED.closed_at, closed_by=EXCLUDED.closed_by, resolution=EXCLUDED.resolution, "
        "wo_type='Breakdown'",
        id, asset_id, btype.strip() or "Mechanical", problem.strip() or "--",
        by.strip() or "—", bid_emp, aid, assignees, status_val, raised_at, closed_at,
        closed_by, resolution)
    try:
        await db.execute(
            "SELECT setval(pg_get_serial_sequence('maint_breakdowns','id'), "
            "GREATEST((SELECT COALESCE(MAX(id),1) FROM maint_breakdowns), $1))", id)
    except Exception as e:
        print("[maint] restore setval skipped:", e)
    pdf_path = None
    try:
        pdf_path = await _regen_breakdown_pdf(id)
    except Exception as e:
        print("[maint] restore PDF regen skipped:", e)
    return _json({"ok": True, "id": id, "status": status_val, "asset_id": asset_id,
                  "assignee_id": aid, "reported_by_emp_id": bid_emp, "pdf": pdf_path,
                  "warnings": [w for w in [
                      None if asset_id else f"asset code '{code}' not found — asset_id is NULL",
                      None if (aid or not who.strip()) else f"assignee '{who}' not matched",
                  ] if w]})


def _parse_bd_pdf(b: bytes) -> dict:
    """Extract breakdown fields from one of our generated work-order PDFs."""
    import io as _io
    import re as _re
    from pypdf import PdfReader
    txt = PdfReader(_io.BytesIO(b)).pages[0].extract_text() or ""
    lines = [l.strip() for l in txt.splitlines() if l.strip()]

    def after(label):
        for i, l in enumerate(lines):
            if l == label and i + 1 < len(lines):
                return lines[i + 1].strip()
        return ""

    def block(label, stops):
        out, started = [], False
        for l in lines:
            if not started:
                if l == label:
                    started = True
                continue
            if any(_re.match(s, l) for s in stops):
                break
            out.append(l)
        return " ".join(out).strip()

    date_pat = r"\d{1,2}\s+\w{3,}\s+\d{4}"
    asset_field = after("Asset:")
    code = asset_field.split(" - ")[0].strip() if asset_field else ""
    st = (after("Status:") or "open").lower()
    status = "closed" if "clos" in st else "open"
    action = block("Action taken", [date_pat, r"^pending$", r"^Raised by:$", r"^\d+$"])
    if action.strip() in ("--", "pending", ""):
        action = ""
    con = after("Closed on:")
    return {"code": code, "status": status,
            "problem": block("Problem reported", [date_pat, r"^\d+$", r"^Action taken$"]) or "--",
            "action": action, "raised": after("Raised on:"), "by": after("Raised by:"),
            "who": after("Assigned to:"), "btype": after("Breakdown type:") or "Mechanical",
            "closed_on": "" if con in ("--", "") else con}


def _parse_dt(s, default):
    s = (s or "").strip()
    if not s:
        return default
    try:
        from dateutil import parser as _dtp
        d = _dtp.parse(s)
        return d.replace(tzinfo=IST) if d.tzinfo is None else d.astimezone(IST)
    except Exception:
        return default


@router.get("/breakdown/restore-all")
async def breakdown_restore_all(token: str = Query(default=""), apply: int = Query(default=0),
                                emp: dict = Depends(current_employee)):
    """Restore EVERY breakdown whose OneDrive PDF exists but whose row is missing (migration
    loss): read each PDF, re-insert the row in the exact state the PDF shows (open stays open,
    closed stays closed), and regenerate the PDF. Admin or ?token=AGENT_TOKEN.
    apply=0 (default) is a DRY RUN — shows what it WOULD restore, writes nothing.
    apply=1 performs the restore. Open qms.bharatsteels.in/maint/breakdown/restore-all?token=.."""
    await ensure_tables()
    if not ((emp and emp.get("is_admin")) or (AGENT_TOKEN and token == AGENT_TOKEN)):
        raise HTTPException(403, "Admins only")
    import re as _re
    tok = await graph.get_token()
    base = f"{ONEDRIVE_BASE}/Maintenance/Reports"
    id_item = {}
    try:
        machines = [m for m in await graph.list_children(tok, base) if m.get("is_folder")]

        async def _scan(m):
            found = {}
            for f in await graph.list_children(tok, f"{base}/{m['name']}/Breakdown Maintenance"):
                mm = _re.search(r"_bm_(\d+)\.pdf$", f.get("name") or "", _re.I)
                if mm and f.get("id"):
                    found[int(mm.group(1))] = f["id"]
            return found
        for res in await asyncio.gather(*[_scan(m) for m in machines], return_exceptions=True):
            if isinstance(res, dict):
                id_item.update(res)
    except Exception as e:
        return _json({"ok": False, "error": f"scan failed: {str(e)[:150]}"})
    have = {r["id"] for r in await db.fetch(
        "SELECT id FROM maint_breakdowns WHERE id = ANY($1::int[])", list(id_item))} if id_item else set()
    missing = sorted(set(id_item) - have)

    records = []
    for bid in missing:
        try:
            raw = await graph.download_item(tok, id_item[bid])
            if not raw:
                records.append({"id": bid, "ok": False, "reason": "could not download PDF"})
                continue
            p = _parse_bd_pdf(raw)
            asset = await db.fetchrow("SELECT id FROM maint_assets WHERE upper(code)=upper($1) LIMIT 1",
                                      p["code"]) if p["code"] else None
            asset_id = asset["id"] if asset else None
            aemp = await db.fetchrow("SELECT id FROM employees WHERE lower(name)=lower($1) LIMIT 1",
                                     p["who"]) if p["who"] else None
            aid = aemp["id"] if aemp else None
            bemp = await db.fetchrow("SELECT id FROM employees WHERE lower(name)=lower($1) LIMIT 1",
                                     p["by"]) if p["by"] else None
            bid_emp = bemp["id"] if bemp else None
            raised_at = _parse_dt(p["raised"], datetime.now(IST))
            is_closed = p["status"] == "closed"
            closed_at = _parse_dt(p["closed_on"], raised_at) if is_closed else None
            rec = {"id": bid, "machine": p["code"], "machine_found": bool(asset_id),
                   "status": p["status"], "problem": p["problem"],
                   "assigned_to": p["who"], "assignee_found": bool(aid),
                   "raised_by": p["by"], "raised_on": p["raised"],
                   "action_taken": p["action"] or ("—" if is_closed else "(still open)")}
            if apply:
                assignees = json.dumps([aid] if aid else [])
                await db.execute(
                    "INSERT INTO maint_breakdowns (id, asset_id, btype, problem, reported_by, "
                    "reported_by_emp_id, assignee_emp_id, assignees, status, raised_at, closed_at, "
                    "closed_by, resolution, wo_type, close_types, close_causes) "
                    "VALUES ($1,$2,$3,$4,$5,$6,$7,$8::jsonb,$9,$10,$11,$12,$13,'Breakdown','[]'::jsonb,'[]'::jsonb) "
                    "ON CONFLICT (id) DO UPDATE SET asset_id=EXCLUDED.asset_id, btype=EXCLUDED.btype, "
                    "problem=EXCLUDED.problem, reported_by=EXCLUDED.reported_by, "
                    "reported_by_emp_id=EXCLUDED.reported_by_emp_id, assignee_emp_id=EXCLUDED.assignee_emp_id, "
                    "assignees=EXCLUDED.assignees, status=EXCLUDED.status, raised_at=EXCLUDED.raised_at, "
                    "closed_at=EXCLUDED.closed_at, closed_by=EXCLUDED.closed_by, resolution=EXCLUDED.resolution, "
                    "wo_type='Breakdown'",
                    bid, asset_id, p["btype"], p["problem"], p["by"] or "—", bid_emp, aid, assignees,
                    p["status"], raised_at, closed_at,
                    (p["who"] or p["by"] or "staff") if is_closed else None,
                    (p["action"] or None) if is_closed else None)
                await db.execute(
                    "SELECT setval(pg_get_serial_sequence('maint_breakdowns','id'), "
                    "GREATEST((SELECT COALESCE(MAX(id),1) FROM maint_breakdowns), $1))", bid)
                rec["restored"] = True
                rec["pdf"] = await _regen_breakdown_pdf(bid)
            records.append(rec)
        except Exception as e:
            records.append({"id": bid, "ok": False, "reason": str(e)[:160]})
    return _json({"mode": ("APPLIED — rows restored" if apply
                           else "DRY RUN (nothing written). Review, then add &apply=1 to restore."),
                  "missing_count": len(missing), "missing_ids": missing, "records": records})


def _require_admin(emp: dict):
    if not emp.get("is_admin"):
        raise HTTPException(403, "Admin only")


@router.post("/breakdown/delete")
async def breakdown_delete(body: dict, emp: dict = Depends(require_employee)):
    """Delete ONE work order. Allowed for an admin, the person who RAISED it, or a
    person it is ASSIGNED to."""
    await ensure_tables()
    try:
        bid = int(body.get("id"))
    except (TypeError, ValueError):
        raise HTTPException(400, "Missing id")
    _drow = await db.fetchrow(
        "SELECT reported_by_emp_id, assignee_emp_id, assignees FROM maint_breakdowns WHERE id=$1", bid)
    if not _drow:
        raise HTTPException(404, "Work order not found")
    if not _can_delete_bd(emp, dict(_drow)):
        raise HTTPException(403, "You can only delete your own work orders")
    await db.execute("DELETE FROM maint_breakdowns WHERE id=$1", bid)
    return {"ok": True}


@router.post("/breakdowns/delete-all")
async def breakdowns_delete_all(emp: dict = Depends(require_employee)):
    """Delete EVERY raised ticket (admin only)."""
    await ensure_tables()
    _require_admin(emp)
    n = await db.fetchval("SELECT COUNT(*) FROM maint_breakdowns")
    await db.execute("DELETE FROM maint_breakdowns")
    return {"ok": True, "deleted": int(n or 0)}


# =======================================================================
#  DASHBOARD — maintenance analytics (KPIs, charts)
# =======================================================================
async def _dash_val(sql, *args, default=0):
    try:
        v = await db.fetchval(sql, *args)
        return v if v is not None else default
    except Exception as e:
        print("[maint-dash]", e)
        return default


@router.get("/dashboard/data")
async def dashboard_data(emp: dict = Depends(require_employee)):
    """Aggregated maintenance KPIs + chart series for the dashboard page."""
    await ensure_tables()
    out = {}
    # ---- headline KPIs ----
    out["open_breakdowns"] = int(await _dash_val(
        "SELECT COUNT(*) FROM maint_breakdowns WHERE status='open'"))
    out["closed_30d"] = int(await _dash_val(
        "SELECT COUNT(*) FROM maint_breakdowns WHERE status='closed' "
        "AND closed_at > now() - interval '30 days'"))
    out["pm_done_30d"] = int(await _dash_val(
        "SELECT COUNT(*) FROM maint_logs WHERE done_at > now() - interval '30 days'"))
    out["active_assets"] = int(await _dash_val(
        "SELECT COUNT(*) FROM maint_assets WHERE COALESCE(active,true)"))
    out["active_schedules"] = int(await _dash_val(
        "SELECT COUNT(*) FROM maint_schedules WHERE COALESCE(active,true)"))
    # MTTR = average working-window downtime per closed breakdown (last 180 days)
    _mto = datetime.now(IST)
    _mdt, _mn = await _downtime_working(_mto - timedelta(days=180), _mto, 0)
    out["mttr_hours"] = round(_mdt / _mn, 1) if _mn else 0.0
    # oldest open breakdown age (days)
    out["oldest_open_days"] = int(await _dash_val(
        "SELECT COALESCE(MAX(EXTRACT(DAY FROM (now()-raised_at))),0) "
        "FROM maint_breakdowns WHERE status='open'"))
    # ---- breakdowns by type (last 180 days) ----
    bt = {"Electrical": 0, "Mechanical": 0, "Hydraulic": 0, "Other": 0}
    try:
        rows = await db.fetch(
            "SELECT COALESCE(NULLIF(btype,''),'Other') AS t, COUNT(*) c FROM maint_breakdowns "
            "WHERE raised_at > now() - interval '180 days' GROUP BY 1")
        for r in rows:
            bt[r["t"] if r["t"] in bt else "Other"] = bt.get(r["t"], 0) + int(r["c"])
    except Exception as e:
        print("[maint-dash bt]", e)
    out["by_type"] = bt
    # ---- top machines by breakdowns (all time) ----
    top = []
    try:
        rows = await db.fetch(
            "SELECT COALESCE(a.code,'?') code, COALESCE(a.name,'(unknown)') name, COUNT(*) c "
            "FROM maint_breakdowns b LEFT JOIN maint_assets a ON a.id=b.asset_id "
            "GROUP BY 1,2 ORDER BY c DESC LIMIT 6")
        top = [{"label": (r["code"] + " " + r["name"]).strip()[:28], "count": int(r["c"])} for r in rows]
    except Exception as e:
        print("[maint-dash top]", e)
    out["top_machines"] = top
    # ---- 6-month trend: breakdowns raised vs PM done ----
    months, bd, pm = [], [], []
    try:
        for i in range(5, -1, -1):
            lbl = await db.fetchval(
                "SELECT to_char(date_trunc('month', now()) - make_interval(months => $1), 'Mon')", i)
            c_bd = await _dash_val(
                "SELECT COUNT(*) FROM maint_breakdowns WHERE date_trunc('month',raised_at) "
                "= date_trunc('month', now()) - make_interval(months => $1)", i)
            c_pm = await _dash_val(
                "SELECT COUNT(*) FROM maint_logs WHERE date_trunc('month',done_at) "
                "= date_trunc('month', now()) - make_interval(months => $1)", i)
            months.append(lbl); bd.append(int(c_bd)); pm.append(int(c_pm))
    except Exception as e:
        print("[maint-dash trend]", e)
    out["trend"] = {"months": months, "breakdowns": bd, "pm_done": pm}
    # ---- current open breakdown list ----
    openlist = []
    try:
        rows = await db.fetch(
            "SELECT b.id, COALESCE(a.code,'') code, COALESCE(a.name,'') name, "
            "COALESCE(NULLIF(b.btype,''),'Other') btype, b.problem, b.raised_at, b.reported_by "
            "FROM maint_breakdowns b LEFT JOIN maint_assets a ON a.id=b.asset_id "
            "WHERE b.status='open' ORDER BY b.raised_at ASC LIMIT 15")
        openlist = [dict(r) for r in rows]
    except Exception as e:
        print("[maint-dash open]", e)
    out["open_list"] = openlist
    return _json(out)


@router.get("/analytics/data")
async def analytics_data(days: int = 90, asset: int = 0, frm: str = "", to: str = "",
                         emp: dict = Depends(require_employee)):
    """Reliability analytics (MTTR, MTBF, availability, downtime, PM compliance). Window is
    the preset `days`, or an explicit date-time range (frm/to), and optionally one machine."""
    await ensure_tables()

    def _pdt(s):
        try:
            return datetime.fromisoformat(str(s)[:16]).replace(tzinfo=IST)
        except Exception:
            return None

    now = datetime.now(IST)
    to_dt = _pdt(to) or now
    frm_dt = _pdt(frm)
    if not frm_dt:
        try:
            days = max(1, min(int(days), 1460))
        except Exception:
            days = 90
        frm_dt = to_dt - timedelta(days=days)
    if frm_dt >= to_dt:
        frm_dt = to_dt - timedelta(days=90)
    hours = max((to_dt - frm_dt).total_seconds() / 3600.0, 1.0)
    try:
        aid = int(asset)
    except Exception:
        aid = 0

    machines = 1 if aid else int(await _dash_val("SELECT COUNT(*) FROM maint_assets WHERE COALESCE(active,true)"))
    failures = int(await _dash_val(
        "SELECT COUNT(*) FROM maint_breakdowns WHERE raised_at>=$1 AND raised_at<=$2 "
        "AND ($3=0 OR asset_id=$3)", frm_dt, to_dt, aid))
    closed = int(await _dash_val(
        "SELECT COUNT(*) FROM maint_breakdowns WHERE status='closed' AND closed_at IS NOT NULL "
        "AND closed_at>=$1 AND closed_at<=$2 AND ($3=0 OR asset_id=$3)", frm_dt, to_dt, aid))
    downtime, _dt_n = await _downtime_working(frm_dt, to_dt, aid)
    open_now = int(await _dash_val(
        "SELECT COUNT(*) FROM maint_breakdowns WHERE status='open' AND ($1=0 OR asset_id=$1)", aid))
    pm_done = int(await _dash_val(
        "SELECT COUNT(*) FROM maint_logs WHERE done_at>=$1 AND done_at<=$2 AND ($3=0 OR asset_id=$3)",
        frm_dt, to_dt, aid))
    pm_issues = int(await _dash_val(
        "SELECT COUNT(*) FROM maint_logs WHERE status='issues' AND done_at>=$1 AND done_at<=$2 "
        "AND ($3=0 OR asset_id=$3)", frm_dt, to_dt, aid))
    active_sched = int(await _dash_val(
        "SELECT COUNT(*) FROM maint_schedules WHERE COALESCE(active,true) AND ($1=0 OR asset_id=$1)", aid))
    base = machines * hours
    uptime = max(base - downtime, 0.0)
    out = {
        "from": frm_dt.strftime("%d-%m-%Y %H:%M"), "to": to_dt.strftime("%d-%m-%Y %H:%M"),
        "machines": machines, "failures": failures, "closed": closed,
        "downtime_hours": round(downtime, 1), "open_now": open_now,
        "mttr_hours": round(downtime / closed, 1) if closed else 0,
        "mtbf_hours": round(uptime / failures, 1) if failures else 0,
        "mtbf_days": round(uptime / failures / 24.0, 1) if failures else 0,
        "availability_pct": round(uptime / base * 100, 1) if base else 100,
        "pm_done": pm_done, "pm_issues": pm_issues,
        "pm_ok_pct": round((pm_done - pm_issues) / pm_done * 100, 1) if pm_done else 100,
        "active_schedules": active_sched,
    }
    bt = {"Electrical": 0, "Mechanical": 0, "Hydraulic": 0, "Other": 0}
    try:
        for r in await db.fetch(
            "SELECT COALESCE(NULLIF(btype,''),'Other') t, COUNT(*) c FROM maint_breakdowns "
            "WHERE raised_at>=$1 AND raised_at<=$2 AND ($3=0 OR asset_id=$3) GROUP BY 1",
            frm_dt, to_dt, aid):
            bt[r["t"] if r["t"] in bt else "Other"] = bt.get(r["t"], 0) + int(r["c"])
    except Exception as e:
        print("[maint-an bt]", e)
    out["by_type"] = bt
    months, fl, dt, mt = [], [], [], []
    try:
        for i in range(5, -1, -1):
            lbl = await db.fetchval("SELECT to_char(date_trunc('month',now())-make_interval(months=>$1),'Mon')", i)
            f = await _dash_val(
                "SELECT COUNT(*) FROM maint_breakdowns WHERE date_trunc('month',raised_at)"
                "=date_trunc('month',now())-make_interval(months=>$1) AND ($2=0 OR asset_id=$2)", i, aid)
            # month window [mstart, mend) in IST, then working-window downtime
            _b = datetime.now(IST).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            _my, _mm = _b.year, _b.month - i
            while _mm <= 0:
                _mm += 12; _my -= 1
            mstart = _b.replace(year=_my, month=_mm)
            _ny, _nm = (_my + 1, 1) if _mm == 12 else (_my, _mm + 1)
            mend = _b.replace(year=_ny, month=_nm)
            dsum, cc = await _downtime_working(mstart, mend, aid)
            months.append(lbl); fl.append(int(f)); dv = round(float(dsum or 0), 1)
            dt.append(dv); mt.append(round(dv / int(cc), 1) if cc else 0)
    except Exception as e:
        print("[maint-an trend]", e)
    out["trend"] = {"months": months, "failures": fl, "downtime": dt, "mttr": mt}
    rows = []
    try:
        q = await db.fetch(
            "SELECT a.code, a.name, COUNT(*) failures, "
            "COALESCE(SUM(EXTRACT(EPOCH FROM (b.closed_at-b.raised_at))/3600.0) "
            "  FILTER (WHERE b.status='closed' AND b.closed_at IS NOT NULL),0) downtime, "
            "COUNT(*) FILTER (WHERE b.status='closed' AND b.closed_at IS NOT NULL) closedc, "
            "MAX(b.raised_at) last_fail "
            "FROM maint_breakdowns b JOIN maint_assets a ON a.id=b.asset_id "
            "WHERE b.raised_at>=$1 AND b.raised_at<=$2 AND ($3=0 OR b.asset_id=$3) "
            "GROUP BY a.code,a.name ORDER BY failures DESC, downtime DESC LIMIT 50",
            frm_dt, to_dt, aid)
        for r in q:
            f = int(r["failures"]); dn = float(r["downtime"] or 0); cc = int(r["closedc"])
            up = max(hours - dn, 0.0)
            rows.append({
                "machine": ((r["code"] or "") + " " + (r["name"] or "")).strip(),
                "failures": f, "downtime": round(dn, 1),
                "mttr": round(dn / cc, 1) if cc else 0,
                "mtbf_days": round(up / f / 24.0, 1) if f else 0,
                "avail": round(up / hours * 100, 1) if hours else 100,
                "last_fail": r["last_fail"]})
    except Exception as e:
        print("[maint-an rows]", e)
    out["machines_table"] = rows
    return _json(out)


# =======================================================================
#  RUN-HOUR / METER READINGS  (daily per-machine capture)
# =======================================================================
@router.get("/readings/list")
async def readings_list(emp: dict = Depends(require_employee)):
    await ensure_tables()
    assets = await db.fetch(
        "SELECT id, code, name, COALESCE(running_hours,0) AS running_hours "
        "FROM maint_assets WHERE COALESCE(active,true) ORDER BY code, name")
    todays = {}
    try:
        rows = await db.fetch(
            "SELECT asset_id, running_hours, reading, note FROM maint_readings WHERE read_date=$1", _today())
        for r in rows:
            todays[str(r["asset_id"])] = {
                "running_hours": float(r["running_hours"]) if r["running_hours"] is not None else None,
                "reading": float(r["reading"]) if r["reading"] is not None else None,
                "note": r["note"] or ""}
    except Exception as e:
        print("[maint readings today]", e)
    recent = []
    try:
        rows = await db.fetch(
            "SELECT r.read_date, r.running_hours, r.reading, r.note, r.taken_by, "
            "COALESCE(a.code,'') code, COALESCE(a.name,'') name "
            "FROM maint_readings r LEFT JOIN maint_assets a ON a.id=r.asset_id "
            "ORDER BY r.read_date DESC, a.code LIMIT 150")
        recent = [dict(x) for x in rows]
    except Exception as e:
        print("[maint readings recent]", e)
    return _json({"today": _today().isoformat(),
                  "assets": [dict(a) for a in assets], "todays": todays, "recent": recent})


@router.post("/readings/save")
async def readings_save(body: dict, emp: dict = Depends(require_employee)):
    await ensure_tables()
    try:
        rd = date.fromisoformat(str(body.get("read_date"))[:10]) if body.get("read_date") else _today()
    except Exception:
        rd = _today()
    by = emp.get("name") or emp.get("emp_no") or "staff"
    saved = 0
    for r in (body.get("rows") or []):
        try:
            aid = int(r.get("asset_id"))
        except (TypeError, ValueError):
            continue
        rh = _num(r.get("running_hours")); mr = _num(r.get("reading"))
        note = (r.get("note") or "").strip()
        if rh is None and mr is None and not note:
            continue
        await db.execute(
            "INSERT INTO maint_readings (asset_id,read_date,running_hours,reading,note,taken_by,taken_by_emp_id) "
            "VALUES ($1,$2,$3,$4,$5,$6,$7) ON CONFLICT (asset_id,read_date) DO UPDATE SET "
            "running_hours=EXCLUDED.running_hours, reading=EXCLUDED.reading, note=EXCLUDED.note, "
            "taken_by=EXCLUDED.taken_by, taken_by_emp_id=EXCLUDED.taken_by_emp_id, created_at=now()",
            aid, rd, rh, mr, note, by, emp.get("id"))
        if rh is not None:
            try:
                await db.execute("UPDATE maint_assets SET running_hours=$1, hours_updated_at=now() WHERE id=$2", rh, aid)
            except Exception as e:
                print("[maint readings asset-hrs]", e)
        saved += 1
    return {"ok": True, "saved": saved}


# =======================================================================
#  DO / COMPLETE + HISTORY
# =======================================================================
@router.get("/do/get")
async def do_get(schedule: int = Query(...), emp: dict = Depends(require_employee)):
    await ensure_tables()
    s = await db.fetchrow("""
        SELECT s.*, a.name AS asset_name, a.code AS asset_code, a.running_hours AS asset_hours,
               c.name AS checklist_name
        FROM maint_schedules s LEFT JOIN maint_assets a ON a.id=s.asset_id
        LEFT JOIN maint_checklists c ON c.id=s.checklist_id WHERE s.id=$1""", schedule)
    if not s:
        raise HTTPException(404, "Schedule not found")
    s = dict(s)
    pts = []
    if s.get("checklist_id"):
        rows = await db.fetch("SELECT * FROM maint_checkpoints WHERE checklist_id=$1 ORDER BY seq, id", s["checklist_id"])
        pts = [dict(r) for r in rows]
    return _json({"schedule": s, "checkpoints": pts})


@router.post("/complete")
async def complete(body: dict, emp: dict = Depends(require_employee)):
    await ensure_tables()
    sid = body.get("schedule_id")
    s = await db.fetchrow("SELECT * FROM maint_schedules WHERE id=$1", sid)
    if not s:
        raise HTTPException(404, "Schedule not found")
    s = dict(s)
    results = body.get("results") or []
    status = "issues" if any((not r.get("ok", True)) for r in results) else "done"
    hrs = _num(body.get("running_hours"))
    today = _today()
    next_due = next_due_date({**s, "last_done": today}, today)
    done_by = emp.get("name") or emp.get("emp_no")
    started = _parse_local_dt(body.get("start_time"))
    ended = _parse_local_dt(body.get("end_time"))
    log_id = await db.fetchval(
        "INSERT INTO maint_logs (asset_id,schedule_id,checklist_id,done_by,done_by_emp_id,"
        "running_hours,status,results,remarks,next_due,started_at,ended_at) "
        "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12) RETURNING id",
        s["asset_id"], sid, s.get("checklist_id"),
        done_by, emp.get("id"),
        hrs, status, json.dumps(results), (body.get("remarks") or "").strip(), next_due,
        started, ended)
    await db.execute("UPDATE maint_schedules SET last_done=$1, last_done_hours=$2 WHERE id=$3",
                     today, hrs, sid)
    if hrs is not None:
        await db.execute("UPDATE maint_assets SET running_hours=$1, hours_updated_at=now() WHERE id=$2",
                         hrs, s["asset_id"])
    # checklist name (for the period key and the report title)
    cl = None
    if s.get("checklist_id"):
        cl = await db.fetchval("SELECT name FROM maint_checklists WHERE id=$1", s["checklist_id"])
    # record per-checkpoint done state so a board 'Complete' matches a My-Checklist
    # submit — the combined report and the Monitor both read maint_checkpoint_done.
    if s.get("checklist_id"):
        try:
            pk = _period_key(cl or s.get("title") or "", today)
            await ensure_tables()
            for r in results:
                cpid = r.get("cp_id")
                if not cpid:
                    continue
                res = (str(r.get("value") or "").strip()
                       or ("OK" if r.get("ok", True) else "Not OK"))
                note = (r.get("remark") or "").strip()
                await db.execute(
                    "INSERT INTO maint_checkpoint_done (checkpoint_id,emp_id,period_key,result,note) "
                    "VALUES ($1,$2,$3,$4,$5) ON CONFLICT (checkpoint_id,period_key) "
                    "DO UPDATE SET emp_id=$2, result=$4, note=$5, done_at=now()",
                    int(cpid), emp.get("id"), pk, res, note)
        except Exception as e:
            print("[maint] complete checkpoint_done skipped:", e)
    # work-order PDF -> OneDrive (background; see note above)
    try:
        a = await db.fetchrow("SELECT code, name, location FROM maint_assets WHERE id=$1", s["asset_id"])
        asyncio.create_task(_file_pm_report(
            asset_code=(a["code"] if a else ""), asset_name=(a["name"] if a else ""),
            checklist=cl or s.get("title") or "Checklist", doc_no=str(log_id or ""),
            done_by=done_by, results=results, checklist_id=s.get("checklist_id"),
            location=(a["location"] if a else "")))
    except Exception as e:
        print("[maint] complete report skipped:", e)
    return {"ok": True, "status": status, "next_due": next_due.isoformat() if next_due else None}


@router.post("/log/delete-latest")
async def log_delete_latest(body: dict, emp: dict = Depends(require_employee)):
    """Admin-only: delete the most recent completion log for a schedule and revert the
    schedule's last_done to the previous completion, so the task shows as due again.
    Backs the board's 'Delete log' button (undo a wrong/duplicate Complete). The
    OneDrive PDF already filed for that completion is not removed."""
    await ensure_tables()
    if not emp.get("is_admin"):
        raise HTTPException(403, "Admins only")
    try:
        sid = int(body.get("schedule_id"))
    except (TypeError, ValueError):
        raise HTTPException(400, "Bad schedule id")
    last = await db.fetchrow(
        "SELECT id, done_at, done_by FROM maint_logs WHERE schedule_id=$1 "
        "ORDER BY done_at DESC NULLS LAST, id DESC LIMIT 1", sid)
    if not last:
        return _json({"ok": False, "msg": "No completion log to delete for this task."})
    await db.execute("DELETE FROM maint_logs WHERE id=$1", last["id"])
    # revert the schedule to the previous completion (or none)
    prev = await db.fetchrow(
        "SELECT done_at, running_hours FROM maint_logs WHERE schedule_id=$1 "
        "ORDER BY done_at DESC NULLS LAST, id DESC LIMIT 1", sid)
    if prev:
        pd = prev["done_at"].date() if isinstance(prev["done_at"], datetime) else None
        await db.execute("UPDATE maint_schedules SET last_done=$1, last_done_hours=$2 WHERE id=$3",
                         pd, prev["running_hours"], sid)
    else:
        await db.execute(
            "UPDATE maint_schedules SET last_done=NULL, last_done_hours=NULL WHERE id=$1", sid)
    dt = last["done_at"]
    dstr = (dt.astimezone(IST).strftime("%d-%m-%Y %H:%M")
            if isinstance(dt, datetime) else str(dt or ""))
    return _json({"ok": True,
                  "msg": f"Deleted the completion of {dstr} by {last['done_by'] or '—'}; "
                         "the task now shows as due again."})


def _url_token(s: str) -> str:
    """A clean, single-word, URL-safe name for the reminder greeting.
    The reminder template's dynamic URL button reuses the greeting's {{1}}
    value inside its link, so it cannot contain a space (WhatsApp silently
    drops the message) — and an underscore reads badly in the greeting. So
    use the person's main name word instead, skipping single-letter initials:
    'G YUVARAJ' -> 'YUVARAJ', 'Velu C' -> 'Velu', 'Kumar Balakrishnan' -> 'Kumar'.
    The button link then points to /<word>, which the app redirects to the board."""
    words = [w for w in re.split(r"\s+", (s or "").strip()) if w]
    pick = next((w for w in words if len(re.sub(r"[^A-Za-z0-9]", "", w)) > 1),
                words[0] if words else "")
    return re.sub(r"[^A-Za-z0-9_.-]", "", pick) or "user"


@router.post("/remind-one")
async def remind_one(body: dict, emp: dict = Depends(require_employee)):
    """Send a single WhatsApp reminder to the user assigned to one schedule
    (the 'Send reminder' button next to Complete on the Maintenance Board)."""
    await ensure_tables()
    sid = body.get("schedule_id")
    s = await db.fetchrow(
        "SELECT s.id, s.title, s.assignee_emp_id, "
        "a.code AS asset_code, a.name AS asset_name, "
        "e.name AS assignee_name, COALESCE(e.mobile,'') AS assignee_mobile "
        "FROM maint_schedules s "
        "LEFT JOIN maint_assets a ON a.id=s.asset_id "
        "LEFT JOIN employees e ON e.id=s.assignee_emp_id "
        "WHERE s.id=$1", sid)
    if not s:
        raise HTTPException(404, "Schedule not found")
    s = dict(s)
    name = (s.get("assignee_name") or "").strip()
    mobile = (s.get("assignee_mobile") or "").strip()
    if not s.get("assignee_emp_id") or not name:
        return _json({"ok": False, "msg": "No user is assigned to this task. Assign one on the Schedules page first."})
    if not mobile:
        return _json({"ok": False, "msg": name + " has no WhatsApp number saved. Add their mobile on the Users page."})
    if not WATI_MAINT_DUE_TEMPLATE:
        return _json({"ok": False, "msg": "The WhatsApp reminder template isn't set up yet. Once the WATI template is approved and WATI_MAINT_DUE_TEMPLATE is set, this button will send."})
    today = _today()
    tdisp = today.strftime("%d-%m-%Y")
    label = ((s.get("asset_code") or "") + " " + (s.get("asset_name") or "")).strip()
    item = (label + " - " + (s.get("title") or "maintenance")).strip(" -")
    # same body vars as the due digest: 1 name, 2 date, 3 preventive list, 4 breakdown list, 5 button
    # param "1" also feeds the URL button, so it must be URL-safe (no spaces)
    params = [_url_token(name), tdisp, item, "—", "maint"]
    m = wati.normalize_number(mobile) or mobile
    ok, detail = await _wsend(WATI_MAINT_DUE_TEMPLATE, m, params, name=name, kind="reminder")
    if ok:
        return _json({"ok": True, "msg": "Reminder sent to " + name + " on WhatsApp (" + mobile + ")."})
    return _json({"ok": False, "msg": "WhatsApp send failed: " + str(detail)})


@router.get("/history/list")
async def history_list(asset: int = Query(default=0), limit: int = Query(default=100),
                       emp: dict = Depends(require_employee)):
    await ensure_tables()
    if asset:
        rows = await db.fetch(
            "SELECT l.*, a.name AS asset_name, a.code AS asset_code, s.title AS sched_title "
            "FROM maint_logs l LEFT JOIN maint_assets a ON a.id=l.asset_id "
            "LEFT JOIN maint_schedules s ON s.id=l.schedule_id "
            "WHERE l.asset_id=$1 ORDER BY l.done_at DESC LIMIT $2", asset, limit)
    else:
        rows = await db.fetch(
            "SELECT l.*, a.name AS asset_name, a.code AS asset_code, s.title AS sched_title "
            "FROM maint_logs l LEFT JOIN maint_assets a ON a.id=l.asset_id "
            "LEFT JOIN maint_schedules s ON s.id=l.schedule_id "
            "ORDER BY l.done_at DESC LIMIT $1", limit)
    return _json([dict(r) for r in rows])


# =======================================================================
#  BOARD (due / overdue / upcoming)
# =======================================================================
DAILY_LOOKBACK_DAYS = 7   # board scans this many calendar days back for missed daily PM


async def _daily_window_state(sched_all, daily_cl_ids, today):
    """Gather per-checkpoint completion, absent marks and completion logs for every
    DAILY checklist across the recent working-day window, for the board's overdue scan.
    Returns (cps_by_cl, done_set, absent_set, log_set, window_days):
      cps_by_cl   : {checklist_id: [checkpoint dicts]}
      done_set    : {(checkpoint_id, period_iso)}
      absent_set  : {(checklist_id, emp_id, period_iso)}
      log_set     : {(checklist_id, period_iso)}  -- a completion log exists that day
      window_days : [date, ...] working days in the window, most-recent first
    """
    window_days = []
    d = today
    for _ in range(DAILY_LOOKBACK_DAYS):
        if not _is_offday(d):
            window_days.append(d)
        d = d - timedelta(days=1)
    window_isos = [x.isoformat() for x in window_days]

    cps_by_cl = {}
    if daily_cl_ids:
        for cp in await db.fetch(
                "SELECT id, checklist_id, assigned_emp_id FROM maint_checkpoints "
                "WHERE checklist_id = ANY($1::int[])", daily_cl_ids):
            cps_by_cl.setdefault(cp["checklist_id"], []).append(dict(cp))
    all_cp_ids = [cp["id"] for lst in cps_by_cl.values() for cp in lst]

    done_set = set()
    if all_cp_ids and window_isos:
        for r in await db.fetch(
                "SELECT checkpoint_id, period_key FROM maint_checkpoint_done "
                "WHERE period_key = ANY($1::text[]) AND checkpoint_id = ANY($2::int[])",
                window_isos, all_cp_ids):
            done_set.add((r["checkpoint_id"], r["period_key"]))

    absent_set = set()
    if window_isos:
        for a in await db.fetch(
                "SELECT checklist_id, emp_id, period_key FROM maint_wo_absent "
                "WHERE period_key = ANY($1::text[])", window_isos):
            absent_set.add((a["checklist_id"], a["emp_id"], a["period_key"]))

    log_set = set()
    if daily_cl_ids and window_days:
        win_start = datetime.combine(window_days[-1], time(0, 0), tzinfo=IST)
        for r in await db.fetch(
                "SELECT checklist_id, done_at FROM maint_logs "
                "WHERE checklist_id = ANY($1::int[]) AND done_at >= $2",
                daily_cl_ids, win_start):
            da = r["done_at"]
            if isinstance(da, datetime):
                log_set.add((r["checklist_id"], da.astimezone(IST).date().isoformat()))

    return cps_by_cl, done_set, absent_set, log_set, window_days


async def _quarterly_state(sched_all, today):
    """Per-person completion of each QUARTERLY checklist for the CURRENT quarter period,
    so a quarterly that one person finished but another hasn't still counts as pending —
    exactly like the daily look-back. Returns (cps_by_cl, pk_by_cl, done_set, absent_set)."""
    q = [s for s in sched_all if s.get("active") and s.get("checklist_id")
         and (s.get("freq_type") or "").lower() == "quarterly"]
    cl_ids = sorted({s["checklist_id"] for s in q})
    pk_by_cl = {s["checklist_id"]: _period_key(s.get("checklist_name") or "", today) for s in q}
    cps_by_cl = {}
    if cl_ids:
        for cp in await db.fetch(
                "SELECT id, checklist_id, assigned_emp_id FROM maint_checkpoints "
                "WHERE checklist_id = ANY($1::int[])", cl_ids):
            cps_by_cl.setdefault(cp["checklist_id"], []).append(dict(cp))
    all_cp = [cp["id"] for lst in cps_by_cl.values() for cp in lst]
    pks = sorted(set(pk_by_cl.values()))
    done_set = set()
    if all_cp and pks:
        for d in await db.fetch(
                "SELECT checkpoint_id, period_key FROM maint_checkpoint_done "
                "WHERE period_key = ANY($1::text[]) AND checkpoint_id = ANY($2::int[])", pks, all_cp):
            done_set.add((d["checkpoint_id"], d["period_key"]))
    absent_set = set()
    if pks:
        for a in await db.fetch(
                "SELECT checklist_id, emp_id, period_key FROM maint_wo_absent "
                "WHERE period_key = ANY($1::text[])", pks):
            absent_set.add((a["checklist_id"], a["emp_id"], a["period_key"]))
    skip_set = await _skipped_set(pks)
    return cps_by_cl, pk_by_cl, done_set, absent_set, skip_set


def _quarterly_progress(clid, cps_by_cl, pk_by_cl, done_set, absent_set, skip_set=None):
    """(fully_done, started) for a quarterly checklist's current period, or (None, None)
    when there are no checkpoints to track (fall back to the date model). A period that
    was explicitly SKIPPED by an admin counts as fully done, so it stops showing as
    due/overdue on the board and in the reminders for this period."""
    cps = cps_by_cl.get(clid) or []
    if not cps:
        return None, None
    pk = pk_by_cl.get(clid)
    if skip_set and (clid, pk) in skip_set:
        return True, True
    effective = done_n = 0
    started = False
    for cp in cps:
        aid = cp.get("assigned_emp_id")
        if (cp["id"], pk) in done_set:
            started = True
        if aid and (clid, aid, pk) in absent_set:
            continue
        effective += 1
        if (cp["id"], pk) in done_set:
            done_n += 1
    fully_done = (effective > 0 and done_n >= effective)
    return fully_done, started


@router.get("/board")
async def board(emp: dict = Depends(require_employee)):
    await ensure_tables()
    await _refresh_holidays()
    today = _today()
    on_leave = await _on_leave_ids(today)   # assignees on leave today
    # two independent boards: Daily and Periodic (quarterly/interval)
    daily = {"overdue": [], "due": [], "upcoming": []}
    periodic = {"overdue": [], "due": [], "upcoming": []}
    sched_all = await _schedule_rows()

    # ---- per-person completion of each DAILY checklist over a LOOK-BACK window ----
    # A daily machine is OVERDUE if, on ANY working day in the recent window, at least
    # one assignee was still pending (nobody started, or some finished and others did
    # not). A missed day can't be caught up under its own date, so completing today
    # does NOT clear an earlier miss — the machine stays overdue (showing the oldest
    # pending day) until that day is fully done OR the missing people are marked absent
    # for it, or until the day ages out of the window. The board reads the real
    # per-checkpoint state per day, never just last_done.
    await _ensure_absent_table()

    def _is_daily(s):
        ft = (s.get("freq_type") or "").lower()
        return ft == "daily" or (not ft and s.get("freq_days") and int(s["freq_days"]) == 1)

    daily_cl_ids = sorted({s["checklist_id"] for s in sched_all
                           if s.get("active") and s.get("checklist_id") and _is_daily(s)})
    cps_by_cl, done_set, absent_set, log_set, window_days = await _daily_window_state(
        sched_all, daily_cl_ids, today)
    # quarterly per-person completion for the current quarter (so a part-done quarterly shows)
    q_cps, q_pk, q_done, q_absent, q_skip = await _quarterly_state(sched_all, today)

    cur_due = window_days[0] if window_days else today   # the current due working day

    def _day_overdue(checklist_id, period_iso, cps, is_current):
        """True if this day should count as OVERDUE (pending) for the board. Mirrors
        the Monitor: a day is complete when every non-excused (absent / on-leave-today)
        assignee checkpoint is done, or an old-style full log exists. A PAST day is
        flagged only if it has completion evidence (a log or any checkpoint done) but
        is still incomplete — a past day with no record at all is treated as untracked
        and skipped. The current due day is flagged even with no record (not started)."""
        if not cps:
            return False
        has_cpdone = any((cp["id"], period_iso) in done_set for cp in cps)
        has_log = (checklist_id, period_iso) in log_set
        pending = 0
        for cp in cps:
            aid = cp.get("assigned_emp_id")
            excused = aid and ((checklist_id, aid, period_iso) in absent_set
                               or (period_iso == today.isoformat() and aid in on_leave))
            if excused:
                continue
            if (cp["id"], period_iso) not in done_set:
                pending += 1
        if pending == 0:
            return False                       # complete
        if not has_cpdone and has_log:
            return False                       # old-style full log -> treat as done
        if has_cpdone or has_log or is_current:
            return True                        # tracked partial miss, or today not done
        return False                           # past day, no record -> untracked, skip

    for s in sched_all:
        if not s.get("active"):
            continue
        is_daily = _is_daily(s)
        info = compute_due(s, s.get("asset_hours"), today)
        s.update(info)
        if is_daily:
            clid = s.get("checklist_id")
            cps = cps_by_cl.get(clid) or []
            start_d = s.get("start_date")
            end_d = s.get("end_date")
            incomplete = []
            if clid and cps:
                for d in window_days:                   # most-recent-first list
                    if (start_d and d < start_d) or (end_d and d > end_d):
                        continue
                    if _day_overdue(clid, d.isoformat(), cps, d == cur_due):
                        incomplete.append(d)
            else:
                # no checklist to track per-person: only judge the current due day,
                # done if last_done reached it (assignee not on leave)
                ld = s.get("last_done")
                aid = s.get("assignee_emp_id")
                if (not (start_d and start_d > cur_due) and not (end_d and cur_due > end_d)
                        and (not ld or ld < cur_due) and not (aid and aid in on_leave)):
                    incomplete.append(cur_due)
            if incomplete:
                past = [d for d in incomplete if d < today]
                if past:
                    # a PAST working day was left pending -> OVERDUE (show oldest day)
                    s["next_due"] = min(past).isoformat()
                    s["pending_days"] = len(past)
                    daily["overdue"].append(s)
                else:
                    # only today's work is still pending -> DUE TODAY (not overdue)
                    s["next_due"] = today.isoformat()
                    daily["due"].append(s)
            elif info["next_due"]:
                nd = _d(info["next_due"])
                if nd and nd <= today + timedelta(days=7):
                    daily["upcoming"].append(s)
        else:
            # Periodic (quarterly / interval). Quarterly reads per-person completion for
            # the current quarter, so a part-done one (someone finished, someone didn't)
            # shows as OVERDUE even though last_done advanced. Fully done -> not shown.
            clid = s.get("checklist_id")
            fully_done, started = _quarterly_progress(clid, q_cps, q_pk, q_done, q_absent, q_skip) \
                if ((s.get("freq_type") or "").lower() == "quarterly" and clid) else (None, None)
            if fully_done:
                pass  # completed this quarter -> drop off the board
            elif info["overdue"]:
                periodic["overdue"].append(s)
            elif info["due"]:
                periodic["due"].append(s)
            elif started:
                # part-done but the date model thinks it's done (last_done advanced) -> overdue
                s["next_due"] = q_pk.get(clid) or info.get("next_due")
                periodic["overdue"].append(s)
            elif info["next_due"]:
                nd = _d(info["next_due"])
                if nd and nd <= today + timedelta(days=7):
                    periodic["upcoming"].append(s)
    # recent activity, split by the schedule's frequency
    rows = await db.fetch(
        "SELECT l.*, a.name AS asset_name, a.code AS asset_code, s.title AS sched_title, "
        "lower(COALESCE(s.freq_type,'')) AS freq_type, s.freq_days AS sfreq_days "
        "FROM maint_logs l LEFT JOIN maint_assets a ON a.id=l.asset_id "
        "LEFT JOIN maint_schedules s ON s.id=l.schedule_id ORDER BY l.done_at DESC LIMIT 24")
    d_recent, p_recent = [], []
    for r in rows:
        r = dict(r)
        rf = (r.get("freq_type") or "")
        is_d = rf == "daily" or (not rf and r.get("sfreq_days") and int(r["sfreq_days"]) == 1)
        (d_recent if is_d else p_recent).append(r)
    daily["recent"] = d_recent[:12]
    periodic["recent"] = p_recent[:12]
    return _json({"today": today.isoformat(), "daily": daily, "periodic": periodic,
                  # kept for backward compatibility with any older client
                  "overdue": periodic["overdue"], "due": periodic["due"],
                  "upcoming": periodic["upcoming"], "recent": p_recent[:12]})


@router.get("/board/matrix")
async def board_matrix(emp: dict = Depends(require_employee)):
    """One row per machine for the single-list board: each machine's Daily PM, Quarterly PM
    and Breakdown status in its own column. Status uses the SAME per-person logic as the
    board (daily look-back, quarterly current-quarter completion)."""
    await ensure_tables()
    await _refresh_holidays()
    await _ensure_absent_table()
    today = _today()
    on_leave = await _on_leave_ids(today)
    sched_all = await _schedule_rows()
    # Visibility: admins see every machine's overdues; a normal user sees only the
    # work that is assigned to them (their checkpoint on a daily/quarterly checklist,
    # or a breakdown assigned to them).
    uid = emp.get("id")
    is_admin = bool(emp.get("is_admin"))

    def _mine_cl(clid, cps_map):
        """Is the current user an assignee on this checklist (or an admin)?"""
        if is_admin:
            return True
        return any(cp.get("assigned_emp_id") == uid for cp in (cps_map.get(clid) or []))

    def _is_daily(s):
        ft = (s.get("freq_type") or "").lower()
        return ft == "daily" or (not ft and s.get("freq_days") and int(s["freq_days"]) == 1)

    def _is_q(s):
        return (s.get("freq_type") or "").lower() == "quarterly"

    daily_cl_ids = sorted({s["checklist_id"] for s in sched_all
                           if s.get("active") and s.get("checklist_id") and _is_daily(s)})
    cps_by_cl, done_set, absent_set, log_set, window_days = await _daily_window_state(
        sched_all, daily_cl_ids, today)
    cur_due = window_days[0] if window_days else today
    q_cps, q_pk, q_done, q_absent, q_skip = await _quarterly_state(sched_all, today)

    def _day_overdue(clid, piso, cps, is_cur):
        if not cps:
            return False
        has_cpdone = any((cp["id"], piso) in done_set for cp in cps)
        has_log = (clid, piso) in log_set
        pending = 0
        for cp in cps:
            aid = cp.get("assigned_emp_id")
            if aid and ((clid, aid, piso) in absent_set or (piso == today.isoformat() and aid in on_leave)):
                continue
            if (cp["id"], piso) not in done_set:
                pending += 1
        if pending == 0:
            return False
        if not has_cpdone and has_log:
            return False
        return bool(has_cpdone or has_log or is_cur)

    def _daily_state(s):
        clid = s.get("checklist_id"); cps = cps_by_cl.get(clid) or []
        start_d = s.get("start_date"); end_d = s.get("end_date")
        in_window = not ((start_d and start_d > cur_due) or (end_d and cur_due > end_d))
        # A daily is on the board ONLY while TODAY's check is still pending. Once today's
        # is done the machine clears, even if an earlier day was missed — a completed
        # daily must not keep showing an old date as overdue.
        if clid and cps:
            today_pending = in_window and _day_overdue(clid, cur_due.isoformat(), cps, True)
        else:
            ld = s.get("last_done"); aid = s.get("assignee_emp_id")
            today_pending = (in_window and (not ld or ld < cur_due)
                             and not (aid and aid in on_leave))
        info = compute_due(s, s.get("asset_hours"), today)
        if today_pending:
            # A daily that is pending today is simply "due today". We deliberately do NOT
            # drag an earlier missed day onto the board as an old overdue date: a daily
            # cannot be back-filled, so an old date would cling forever and read as "still
            # overdue" even after the work has been done on later days. Today pending -> due;
            # today done -> the row clears below.
            return {"state": "due", "due": today.isoformat()}
        nd = _d(info["next_due"]) if info.get("next_due") else None
        if nd and nd <= today + timedelta(days=7):
            return {"state": "upcoming", "due": info.get("next_due")}
        return {"state": "ok", "due": info.get("next_due")}

    def _q_state(s):
        clid = s.get("checklist_id")
        info = compute_due(s, s.get("asset_hours"), today)
        fully, started = _quarterly_progress(clid, q_cps, q_pk, q_done, q_absent, q_skip) if clid else (None, None)
        if fully:
            return {"state": "ok", "due": info.get("next_due")}
        # Quarterly due/overdue follows the ANNUAL SCHEDULE DATE only. A machine shows
        # 'due' on its scheduled quarterly date and 'overdue' after that date passes
        # without completion. Partially ticking a checkpoint early must NOT force an
        # 'overdue' when the scheduled date has not yet arrived (that used to flag every
        # started-but-incomplete machine regardless of its date).
        if info["overdue"]:
            return {"state": "overdue", "due": info.get("next_due")}
        if info["due"]:
            return {"state": "due", "due": info.get("next_due")}
        nd = _d(info["next_due"]) if info.get("next_due") else None
        if nd and nd <= today + timedelta(days=7):
            return {"state": "upcoming", "due": info.get("next_due")}
        return {"state": "ok", "due": info.get("next_due")}

    emp_name = {e["id"]: (e["name"] or e["emp_no"])
                for e in await db.fetch("SELECT id, name, emp_no FROM employees")}

    def _assignees(clid):
        """Distinct assignee names on a checklist's checkpoints."""
        seen, names = [], []
        for cp in (cps_by_cl.get(clid) or []) + (q_cps.get(clid) or []):
            a = cp.get("assigned_emp_id")
            if a and a not in seen:
                seen.append(a); names.append(emp_name.get(a, "#%s" % a))
        return names

    def _assignee_status(clid, cps, dset, pk, absent, leave=None):
        """Per-person Done / Pending / Absent for one checklist's checkpoints in a period.
        A person is Done when every checkpoint assigned to them (and not skipped as absent)
        is completed; otherwise Pending. Used to show who has finished on the board."""
        leave = leave or set()
        out, seen = [], set()
        for cp in (cps or []):
            aid = cp.get("assigned_emp_id")
            if not aid or aid in seen:
                continue
            seen.add(aid)
            if (clid, aid, pk) in absent or aid in leave:
                st = "Absent"
            else:
                theirs = [c for c in cps if c.get("assigned_emp_id") == aid]
                st = "Done" if (theirs and all((c["id"], pk) in dset for c in theirs)) else "Pending"
            out.append({"emp": emp_name.get(aid, "#%s" % aid), "status": st})
        return out

    by_asset = {}
    for s in sched_all:
        if not s.get("active"):
            continue
        aid = s.get("asset_id")
        g = by_asset.get(aid)
        if g is None:
            g = by_asset[aid] = {"asset_id": aid, "code": s.get("asset_code") or "",
                                 "name": s.get("asset_name") or "",
                                 "daily": None, "daily_sched": None,
                                 "quarterly": None, "quarterly_sched": None, "breakdowns": []}
        if _is_daily(s) and g["daily"] is None:
            if is_admin or _mine_cl(s.get("checklist_id"), cps_by_cl) or s.get("assignee_emp_id") == uid:
                clid = s.get("checklist_id")
                g["daily"] = _daily_state(s); g["daily_sched"] = s.get("id")
                g["daily"]["assignees"] = _assignees(clid) or (
                    [s.get("assignee_name")] if s.get("assignee_name") else [])
                # Per-person status for the SAME day the row represents (the state's due
                # date) — so an overdue row shows who was pending THAT day, not today.
                dday = g["daily"].get("due") or cur_due.isoformat()
                _lv = on_leave if dday == today.isoformat() else set()
                g["daily"]["assignee_status"] = _assignee_status(
                    clid, cps_by_cl.get(clid) or [], done_set, dday, absent_set, _lv)
        elif _is_q(s) and g["quarterly"] is None:
            if is_admin or _mine_cl(s.get("checklist_id"), q_cps) or s.get("assignee_emp_id") == uid:
                clid = s.get("checklist_id")
                g["quarterly"] = _q_state(s); g["quarterly_sched"] = s.get("id")
                g["quarterly"]["assignees"] = _assignees(clid) or (
                    [s.get("assignee_name")] if s.get("assignee_name") else [])
                g["quarterly"]["assignee_status"] = _assignee_status(
                    clid, q_cps.get(clid) or [], q_done, q_pk.get(clid), q_absent)

    for b in await _open_breakdowns():
        b_aids = b.get("assignees") or ([b.get("assignee_emp_id")] if b.get("assignee_emp_id") else [])
        if not is_admin and uid not in b_aids:
            continue
        aid = b.get("asset_id")
        g = by_asset.get(aid)
        if g is None:
            g = by_asset[aid] = {"asset_id": aid, "code": b.get("asset_code") or "",
                                 "name": b.get("asset_name") or "",
                                 "daily": None, "daily_sched": None,
                                 "quarterly": None, "quarterly_sched": None, "breakdowns": []}
        bnames = []
        for a in (b.get("assignees") or ([b.get("assignee_emp_id")] if b.get("assignee_emp_id") else [])):
            nm = emp_name.get(a)
            if nm:
                bnames.append(nm)
        if not bnames and b.get("assignee_name"):
            bnames = [b.get("assignee_name")]
        raised = b.get("raised_at")
        raised_iso = raised.astimezone(IST).date().isoformat() if isinstance(raised, datetime) else ""
        g["breakdowns"].append({"id": b.get("id"), "problem": (b.get("problem") or ""),
                                "date": raised_iso, "assignees": bnames,
                                "by": (b.get("reported_by") or "").strip() or "-"})

    # --- quarterly from ACTIVITY (covers quarterly checklists with NO schedule, like a
    # machine whose quarterly was recorded but never scheduled). A quarterly checklist that
    # is started this quarter but not fully done shows as overdue, mapped to its machine.
    qcl_rows = await db.fetch(
        "SELECT c.id AS clid, c.asset_id, c.name AS cname, a.code AS acode, a.name AS aname "
        "FROM maint_checklists c LEFT JOIN maint_assets a ON a.id=c.asset_id "
        "WHERE c.name ILIKE '%quarter%'")
    qcl_ids = [r["clid"] for r in qcl_rows]
    qcps2 = {}
    if qcl_ids:
        for cp in await db.fetch(
                "SELECT id, checklist_id, assigned_emp_id FROM maint_checkpoints "
                "WHERE checklist_id = ANY($1::int[])", qcl_ids):
            qcps2.setdefault(cp["checklist_id"], []).append(dict(cp))
    qpk2 = {r["clid"]: _period_key(r["cname"] or "", today) for r in qcl_rows}
    qallcp = [cp["id"] for lst in qcps2.values() for cp in lst]
    qpks = sorted(set(qpk2.values()))
    cp2cl = {cp["id"]: clid for clid, lst in qcps2.items() for cp in lst}
    qdone2 = set()
    qdate = {}   # checklist_id -> earliest completion date this quarter (iso) for display
    if qallcp and qpks:
        for d in await db.fetch(
                "SELECT checkpoint_id, period_key, done_at FROM maint_checkpoint_done "
                "WHERE period_key = ANY($1::text[]) AND checkpoint_id = ANY($2::int[])", qpks, qallcp):
            qdone2.add((d["checkpoint_id"], d["period_key"]))
            cl = cp2cl.get(d["checkpoint_id"]); da = d.get("done_at")
            if cl and isinstance(da, datetime):
                diso = da.astimezone(IST).date().isoformat()
                if cl not in qdate or diso < qdate[cl]:
                    qdate[cl] = diso
    qabsent2 = set()
    if qpks:
        for a in await db.fetch(
                "SELECT checklist_id, emp_id, period_key FROM maint_wo_absent "
                "WHERE period_key = ANY($1::text[])", qpks):
            qabsent2.add((a["checklist_id"], a["emp_id"], a["period_key"]))
    for r in qcl_rows:
        clid = r["clid"]; cps = qcps2.get(clid) or []
        if not cps:
            continue
        if not is_admin and not any(cp.get("assigned_emp_id") == uid for cp in cps):
            continue
        pk = qpk2.get(clid)
        started = any((cp["id"], pk) in qdone2 for cp in cps)
        eff = don = 0
        for cp in cps:
            aid = cp.get("assigned_emp_id")
            if aid and (clid, aid, pk) in qabsent2:
                continue
            eff += 1
            if (cp["id"], pk) in qdone2:
                don += 1
        fully = (eff > 0 and don >= eff)
        if not (started and not fully):
            continue
        g = by_asset.get(r["asset_id"])
        if g is None:
            g = by_asset[r["asset_id"]] = {"asset_id": r["asset_id"], "code": r["acode"] or "",
                                           "name": r["aname"] or "", "daily": None, "daily_sched": None,
                                           "quarterly": None, "quarterly_sched": None, "breakdowns": []}
        if g.get("quarterly") is not None:
            continue  # this machine has a quarterly SCHEDULE — trust its date-based
                      # state; don't override it to overdue from partial activity
        names, seen = [], set()
        for cp in cps:
            a2 = cp.get("assigned_emp_id")
            if not a2 or a2 in seen or (clid, a2, pk) in qabsent2:
                continue
            their = [c["id"] for c in cps if c.get("assigned_emp_id") == a2]
            if any((cid, pk) not in qdone2 for cid in their):
                seen.add(a2); names.append(emp_name.get(a2, "#%s" % a2))
        g["quarterly"] = {"state": "overdue", "due": qdate.get(clid) or pk,
                           "assignees": [n for n in names if n]}

    rows = sorted(by_asset.values(), key=lambda r: str(r.get("code")))
    return _json({"today": today.isoformat(), "rows": rows})


@router.get("/quarterly-audit")
async def quarterly_audit(token: str = Query(default="")):
    """Read-only audit of QUARTERLY PM. Open /maint/quarterly-audit?token=<AGENT_TOKEN>.
    Shows every quarterly schedule (day-of-month, start, last_done, computed next due) and
    every quarterly completion log grouped by the date it was recorded — so a wrong date
    (e.g. 31-Oct) can be seen and removed precisely."""
    if not AGENT_TOKEN or token != AGENT_TOKEN:
        raise HTTPException(403, "Bad token")
    await ensure_tables()
    await _refresh_holidays()
    today = _today()
    scheds = []
    for s in await _schedule_rows():
        if (s.get("freq_type") or "").lower() != "quarterly":
            continue
        info = compute_due(s, s.get("asset_hours"), today)
        scheds.append({
            "sched_id": s.get("id"), "code": s.get("asset_code"), "name": s.get("asset_name"),
            "title": s.get("title"), "checklist_id": s.get("checklist_id"),
            "day_of_month": s.get("day_of_month"), "start_date": str(s.get("start_date")),
            "last_done": str(s.get("last_done")), "next_due": info.get("next_due"),
            "active": s.get("active")})
    scheds.sort(key=lambda r: str(r.get("code")))
    # quarterly completion logs (checklist name contains "quarter"), grouped by IST date
    logrows = await db.fetch(
        "SELECT l.id, l.done_at, l.checklist_id, l.schedule_id, a.code AS code, "
        "a.name AS name, c.name AS cname FROM maint_logs l "
        "LEFT JOIN maint_assets a ON a.id=l.asset_id "
        "LEFT JOIN maint_checklists c ON c.id=l.checklist_id "
        "WHERE c.name ILIKE '%quarter%' AND l.done_at IS NOT NULL ORDER BY l.done_at DESC")
    by_date = {}
    for r in logrows:
        diso = r["done_at"].astimezone(IST).date().isoformat() if isinstance(r["done_at"], datetime) else "?"
        by_date.setdefault(diso, []).append(
            {"code": r["code"], "name": r["name"], "checklist_id": r["checklist_id"],
             "schedule_id": r["schedule_id"], "cname": r["cname"], "log_id": r["id"]})
    return _json({"today": today.isoformat(),
                  "quarterly_schedule_count": len(scheds), "schedules": scheds,
                  "quarterly_log_dates": {d: len(v) for d, v in by_date.items()},
                  "logs_by_date": by_date})


@router.get("/cleanup-quarterly")
async def cleanup_quarterly(token: str = Query(default=""),
                            date: str = Query(default="2026-10-31"),
                            confirm: int = Query(default=0)):
    """Remove QUARTERLY PM work orders recorded on a given date (default 31-Oct), which
    were never part of the annual plan. Open /maint/cleanup-quarterly?token=<AGENT_TOKEN>
    to PREVIEW (nothing deleted). Add &confirm=1 to actually delete the records and their
    report PDFs. Only touches quarterly checklists (name contains 'quarter')."""
    if not AGENT_TOKEN or token != AGENT_TOKEN:
        raise HTTPException(403, "Bad token")
    await ensure_tables()
    await _ensure_absent_table()
    await _refresh_holidays()
    try:
        target = date.strip()[:10]
        tdate = _dt_date.fromisoformat(target)
    except Exception:
        raise HTTPException(400, "Bad date (use YYYY-MM-DD)")

    # quarterly completion logs recorded on the target IST date
    logrows = await db.fetch(
        "SELECT l.id, l.done_at, l.checklist_id, l.asset_id, a.code AS code, a.name AS name, "
        "c.name AS cname FROM maint_logs l "
        "LEFT JOIN maint_assets a ON a.id=l.asset_id "
        "LEFT JOIN maint_checklists c ON c.id=l.checklist_id "
        "WHERE c.name ILIKE '%quarter%' AND l.done_at IS NOT NULL")
    groups = {}   # (checklist_id, asset_id) -> {code,name,cname,pk,log_ids}
    for r in logrows:
        if not isinstance(r["done_at"], datetime):
            continue
        if r["done_at"].astimezone(IST).date() != tdate:
            continue
        key = (r["checklist_id"], r["asset_id"])
        g = groups.get(key)
        if g is None:
            g = groups[key] = {"checklist_id": r["checklist_id"], "asset_id": r["asset_id"],
                               "code": r["code"] or "", "name": r["name"] or "",
                               "cname": r["cname"] or "",
                               "pk": _period_key(r["cname"] or "", tdate), "log_ids": []}
        g["log_ids"].append(r["id"])

    items = sorted(groups.values(), key=lambda g: str(g["code"]))
    preview = [{"code": g["code"], "name": g["name"], "checklist": g["cname"],
                "checklist_id": g["checklist_id"], "period": g["pk"],
                "logs": len(g["log_ids"])} for g in items]

    if not confirm:
        return _json({"date": target, "mode": "preview (nothing deleted)",
                      "work_orders_found": len(items), "total_logs": sum(len(g["log_ids"]) for g in items),
                      "note": "add &confirm=1 to delete these records and their PDFs",
                      "work_orders": preview})

    # confirm=1 -> delete
    try:
        gtoken = await graph.get_token()
    except Exception:
        gtoken = None
    results = []
    for g in items:
        n = await _delete_pm_workorder(g["checklist_id"], g["pk"], g["cname"], g["asset_id"])
        pdf_deleted = False
        if gtoken:
            try:
                itm = await _locate_report_item(gtoken, g["code"], g["name"], "Quarterly PM",
                                                target, bd=0)
                if itm:
                    pdf_deleted = await graph.delete_item(gtoken, itm["id"])
            except Exception:
                pdf_deleted = False
        results.append({"code": g["code"], "name": g["name"], "deleted_logs": n,
                        "pdf_deleted": pdf_deleted})
    return _json({"date": target, "mode": "DELETED",
                  "work_orders_deleted": len(results),
                  "total_logs_deleted": sum(r["deleted_logs"] for r in results),
                  "results": results})


def _daily_done_pdf(target, tdate, items):
    """One inspection report PER MACHINE (the app's own PM-report layout, #235 style),
    every checkpoint shown OK / status Completed, merged into a single PDF."""
    import io
    from .. import maint_pdf
    from pypdf import PdfReader, PdfWriter
    when = datetime.combine(tdate, time(17, 0)).replace(tzinfo=IST)
    when_s = when.strftime("%d %b %H:%M")
    writer = PdfWriter()

    def _add(meta, rows):
        b = maint_pdf.build_pm_report(meta, rows)
        for p in PdfReader(io.BytesIO(b)).pages:
            writer.add_page(p)

    if not items:
        _add({"doc_no": target, "asset_code": "", "asset_name": "", "checklist": "Daily Checklist",
              "started": None, "completed": when, "due": None, "raised_by": "-", "completed_by": "-",
              "status": "No daily overdue on this date", "category": "Scheduled Preventive Maintenance",
              "location": "--", "qr_url": "https://qms.bharatsteels.in/maint"}, [])
    for it in items:
        who = ", ".join(it.get("assignees") or []) or "-"
        rows = []
        for i, cp in enumerate(it.get("checkpoints") or [], 1):
            rec = ("%s · %s" % (when_s, cp.get("assignee") or "-")).strip(" ·")
            rows.append((str(cp.get("seq") or i), cp.get("text") or "-", "OK", rec))
        meta = {"doc_no": it.get("checklist_id") or target,
                "asset_code": it.get("asset_code") or "", "asset_name": it.get("asset_name") or "",
                "checklist": it.get("checklist") or "Daily Checklist",
                "started": None, "completed": when, "due": None,
                "raised_by": (it.get("assignees") or ["-"])[0], "completed_by": who,
                "status": "Completed", "category": "Scheduled Preventive Maintenance",
                "location": it.get("asset_location") or "--",
                "qr_url": "https://qms.bharatsteels.in/maint"}
        _add(meta, rows)

    out = io.BytesIO()
    writer.write(out)
    data = out.getvalue()
    out.close()
    return Response(content=data, media_type="application/pdf",
                    headers={"Content-Disposition": 'inline; filename="daily_maint_done_%s.pdf"' % target,
                             "Cache-Control": "no-store"})


@router.get("/mark-daily-done")
async def mark_daily_done(token: str = Query(default=""),
                          date: str = Query(default="2026-10-30"),
                          confirm: int = Query(default=0),
                          pdf: int = Query(default=0),
                          assets: str = Query(default="")):
    """Record the DAILY PM that is overdue on a given date (default 30-Oct) as COMPLETED
    on that date, clearing it from the overdue board. Open
    /maint/mark-daily-done?token=<AGENT_TOKEN> to PREVIEW (nothing written). Add &confirm=1
    to write the completion records. Add &pdf=1 for a PDF of the rows marked done.
    Only targets daily checklists that already have partial completion on that date (i.e.
    the ones the board shows as overdue)."""
    if not AGENT_TOKEN or token != AGENT_TOKEN:
        raise HTTPException(403, "Bad token")
    await ensure_tables()
    await _ensure_absent_table()
    try:
        target = date.strip()[:10]
        tdate = _dt_date.fromisoformat(target)
    except Exception:
        raise HTTPException(400, "Bad date (use YYYY-MM-DD)")

    sched = await _schedule_rows()
    daily = {}
    for s in sched:
        if not s.get("active"):
            continue
        ft = (s.get("freq_type") or "").lower()
        is_daily = ft == "daily" or (not ft and s.get("freq_days") and int(s["freq_days"]) == 1)
        clid = s.get("checklist_id")
        if is_daily and clid and clid not in daily:
            daily[clid] = {"asset_code": s.get("asset_code") or "",
                           "asset_name": s.get("asset_name") or "",
                           "checklist_name": s.get("checklist_name") or "",
                           "asset_location": s.get("asset_location") or ""}
    clids = list(daily.keys())
    cps = {}
    if clids:
        for cp in await db.fetch("SELECT id, checklist_id, assigned_emp_id, seq, text FROM maint_checkpoints "
                                 "WHERE checklist_id = ANY($1::int[]) ORDER BY checklist_id, seq NULLS LAST, id", clids):
            cps.setdefault(cp["checklist_id"], []).append(
                {"id": cp["id"], "emp": cp["assigned_emp_id"],
                 "seq": cp.get("seq"), "text": cp.get("text")})
    all_cp_ids = [c["id"] for lst in cps.values() for c in lst]
    done = set()
    if all_cp_ids:
        for d in await db.fetch("SELECT checkpoint_id FROM maint_checkpoint_done "
                                "WHERE period_key=$1 AND checkpoint_id = ANY($2::int[])",
                                target, all_cp_ids):
            done.add(d["checkpoint_id"])
    absent = set()
    for a in await db.fetch("SELECT checklist_id, emp_id FROM maint_wo_absent WHERE period_key=$1", target):
        absent.add((a["checklist_id"], a["emp_id"]))
    emp_name = {e["id"]: (e["name"] or e["emp_no"])
                for e in await db.fetch("SELECT id, name, emp_no FROM employees")}

    def _norm(code):
        return str(code or "").upper().replace(" ", "")
    asset_set = {_norm(a) for a in assets.split(",") if a.strip()}

    items, to_insert, norm_ids = [], [], []
    for clid in clids:
        lst = cps.get(clid) or []
        if not lst:
            continue
        code = daily[clid]["asset_code"]
        if asset_set and _norm(code) not in asset_set:
            continue   # restricted to the machines named in &assets=
        done_cps = [c for c in lst if c["id"] in done]
        forced = bool(asset_set)   # naming a machine forces it, even with no prior activity
        if not done_cps and not forced:
            continue   # no activity on this date and not explicitly requested -> skip
        pending = [c for c in lst if c["id"] not in done
                   and not (c["emp"] and (clid, c["emp"]) in absent)]
        names = sorted({emp_name.get(c["emp"], "") for c in lst if c["emp"]} - {""})
        cp_detail = [{"seq": c.get("seq"), "text": c.get("text") or "",
                      "assignee": emp_name.get(c["emp"], "") or "-", "status": "OK"} for c in lst]
        items.append({"asset_code": code, "asset_name": daily[clid]["asset_name"],
                      "checklist": daily[clid]["checklist_name"], "checklist_id": clid,
                      "asset_location": daily[clid].get("asset_location") or "",
                      "total_cps": len(lst), "already_done": len(done_cps),
                      "pending": len(pending), "assignees": names, "checkpoints": cp_detail})
        norm_ids.extend(c["id"] for c in lst)
        for c in pending:
            to_insert.append((c["id"], c["emp"]))
    items.sort(key=lambda x: str(x["asset_code"]))

    # Mark complete first (if confirmed), so a combined &confirm=1&pdf=1 returns the
    # PDF of exactly the rows just marked. `items` is captured before the write.
    marked, normalized, reports = 0, 0, 0
    if confirm:
        done_at = datetime.combine(tdate, time(17, 0)).replace(tzinfo=IST)
        for cp_id, emp in to_insert:
            res = await db.execute(
                "INSERT INTO maint_checkpoint_done (checkpoint_id, emp_id, period_key, done_at, result, note) "
                "VALUES ($1,$2,$3,$4,'OK','') "
                "ON CONFLICT (checkpoint_id, period_key) DO NOTHING",
                cp_id, emp, target, done_at)
            if str(res).strip().endswith("1"):
                marked += 1
        # Normalise already-recorded rows on this date to OK (fixes placeholder/'done'
        # results) WITHOUT overwriting a genuine 'Not OK' inspection result.
        if norm_ids:
            r2 = await db.execute(
                "UPDATE maint_checkpoint_done SET result='OK', note='' "
                "WHERE period_key=$1 AND checkpoint_id = ANY($2::int[]) "
                "AND (result IS NULL OR result='' OR lower(result) IN ('done','pending') "
                "     OR note = 'marked complete (admin)')",
                target, norm_ids)
            try:
                normalized = int(str(r2).split()[-1])
            except Exception:
                normalized = 0
        # Regenerate each machine's stored OneDrive PM report so it now shows every
        # checkpoint OK / status Completed (overwrites the same per-day file). Best-effort.
        for it in items:
            try:
                await _file_pm_report(it.get("asset_code"), it.get("asset_name"),
                                      it.get("checklist"), None,
                                      (it.get("assignees") or ["-"])[0], [],
                                      it.get("checklist_id"),
                                      location=it.get("asset_location") or "", when=done_at)
                reports += 1
            except Exception:
                pass
    if pdf:
        return _daily_done_pdf(target, tdate, items)
    slim = [{k: v for k, v in it.items() if k != "checkpoints"} for it in items]
    if not confirm:
        return _json({"date": target, "mode": "preview (nothing written)",
                      "machines": len(items),
                      "checkpoints_pending_to_mark": len(to_insert),
                      "note": "add &confirm=1 to record as completed (OK) + regenerate OneDrive "
                              "reports; add &confirm=1&pdf=1 for the PDF; add "
                              "&assets=M/C-005,M/C-006 to target specific machines",
                      "items": slim})
    return _json({"date": target, "mode": "APPLIED - recorded as completed (OK)",
                  "machines": len(items), "checkpoints_marked_done": marked,
                  "checkpoints_normalised_to_ok": normalized,
                  "onedrive_reports_generated": reports, "items": slim})


@router.get("/board-debug")
async def board_debug(token: str = Query(default="")):
    """Read-only diagnostic for the daily-overdue classification. Open
    /maint/board-debug?token=<AGENT_TOKEN>. Shows, per active DAILY schedule, how the
    board classifies it and why (due day, checklist, last_done, per-checkpoint state)."""
    if not AGENT_TOKEN or token != AGENT_TOKEN:
        raise HTTPException(403, "Bad token")
    await ensure_tables()
    await _refresh_holidays()
    await _ensure_absent_table()
    today = _today()
    on_leave = await _on_leave_ids(today)

    def _is_daily(s):
        ft = (s.get("freq_type") or "").lower()
        return ft == "daily" or (not ft and s.get("freq_days") and int(s["freq_days"]) == 1)

    sched_all = await _schedule_rows()
    daily_cl_ids = sorted({s["checklist_id"] for s in sched_all
                           if s.get("active") and s.get("checklist_id") and _is_daily(s)})
    cps_by_cl, done_set, absent_set, log_set, window_days = await _daily_window_state(
        sched_all, daily_cl_ids, today)

    cur_due = window_days[0] if window_days else today

    def _day_overdue(checklist_id, period_iso, cps, is_current):
        if not cps:
            return False
        has_cpdone = any((cp["id"], period_iso) in done_set for cp in cps)
        has_log = (checklist_id, period_iso) in log_set
        pending = 0
        for cp in cps:
            aid = cp.get("assigned_emp_id")
            excused = aid and ((checklist_id, aid, period_iso) in absent_set
                               or (period_iso == today.isoformat() and aid in on_leave))
            if excused:
                continue
            if (cp["id"], period_iso) not in done_set:
                pending += 1
        if pending == 0:
            return False
        if not has_cpdone and has_log:
            return False
        if has_cpdone or has_log or is_current:
            return True
        return False

    rows = []
    for s in sched_all:
        if not s.get("active") or not _is_daily(s):
            continue
        clid = s.get("checklist_id")
        cps = cps_by_cl.get(clid) or []
        start_d = s.get("start_date"); end_d = s.get("end_date")
        day_rows = []
        incomplete = []
        for d in window_days:
            if (start_d and d < start_d) or (end_d and d > end_d):
                continue
            diso = d.isoformat()
            if clid and cps:
                done_n = sum(1 for cp in cps if (cp["id"], diso) in done_set)
                od = _day_overdue(clid, diso, cps, d == cur_due)
            else:
                ld = s.get("last_done")
                done_n = None
                od = (d == cur_due) and (not ld or ld < d)
            if od:
                incomplete.append(diso)
            day_rows.append({"day": diso, "done_cp": done_n, "total_cp": len(cps),
                             "has_log": (clid, diso) in log_set, "overdue": od})
        rows.append({
            "sched_id": s.get("id"), "code": s.get("asset_code"), "name": s.get("asset_name"),
            "checklist_id": clid, "n_checkpoints": len(cps),
            "last_done": str(s.get("last_done")), "start_date": str(s.get("start_date")),
            "end_date": str(s.get("end_date")),
            "OVERDUE": bool(incomplete), "incomplete_days": incomplete, "days": day_rows})
    rows.sort(key=lambda r: str(r.get("code")))
    return _json({"today": today.isoformat(),
                  "window_days": [d.isoformat() for d in window_days],
                  "lookback_days": DAILY_LOOKBACK_DAYS,
                  "is_today_offday": _is_offday(today),
                  "holidays_count": len(HOLIDAYS),
                  "today_in_holidays": today.isoformat() in HOLIDAYS,
                  "on_leave_today": sorted(list(on_leave)),
                  "daily_schedule_count": len(rows),
                  "overdue_count": sum(1 for r in rows if r["OVERDUE"]),
                  "schedules": rows})


# =======================================================================
#  WHATSAPP REMINDERS (cron / agent endpoint)
# =======================================================================
def _asset_label(s):
    code = s.get("asset_code") or ""
    name = s.get("asset_name") or ""
    return (code + " " + name).strip()


def _join_items(items, cap=900):
    """One-line join with ' | ' (safe for templates)."""
    out, used = [], 0
    for i, it in enumerate(items):
        if used + len(it) + 3 > cap:
            out.append(f"…and {len(items) - i} more"); break
        out.append(it); used += len(it) + 3
    return " | ".join(out) if out else "—"


def _numbered(items, cap=1000):
    """One-line list for the WhatsApp overdue/due digest: "1) <machine> • 2) <machine> …".
    WhatsApp won't keep real line breaks inside a template variable, so machines are set
    off with a bullet divider and ") " numbering; " - " field separators become " — "."""
    SEP = " • "
    out, used = [], 0
    for i, it in enumerate(items):
        it = str(it).replace(" - ", " — ")
        line = f"{i + 1}) {it}"
        if used + len(line) + len(SEP) > cap:
            out.append(f"…and {len(items) - i} more"); break
        out.append(line); used += len(line) + len(SEP)
    return SEP.join(out) if out else "—"


def _overdue_table(items, bd=False, cap=1800):
    """Report-style 'table' for the WhatsApp overdue digest (admins only) — one numbered
    block per machine carrying S.No, Date, Machine Name, Category and Assigned To, laid
    out like the Maintenance Board. Uses U+2028 line separators: WhatsApp renders them as
    line breaks and they survive _safe_param (which only strips \\r\\n\\t). '—' when empty."""
    LS = " "
    if not items:
        return "—"

    def _cat(c):
        c = (c or "").lower()
        if "quarter" in c:
            return "Quarterly Maintenance"
        if "daily" in c:
            return "Daily Maintenance"
        return (c.title() or "-") if c else "-"

    blocks, used = [], 0
    for i, it in enumerate(items, 1):
        if isinstance(it, str):   # safety — never crash a reminder
            blocks.append(f"{i}) {it}")
            continue
        machine = ((it.get("code") or "").strip() + " " + (it.get("name") or "").strip()).strip() or "—"
        date = it.get("date") or "-"
        who = it.get("who") or "-"
        cat = "Breakdown" if bd else _cat(it.get("cat"))
        lines = [f"{i}) {date}", f"   {machine}", f"   {cat}  |  {who}"]
        if bd and (it.get("prob") or "").strip():
            lines.append(f"   Problem: {it.get('prob')}")
        block = LS.join(lines)
        if used + len(block) + 2 > cap:
            blocks.append(f"…and {len(items) - i + 1} more")
            break
        blocks.append(block)
        used += len(block) + 2
    return (LS + LS).join(blocks)


async def _overdue_reminder_enabled() -> bool:
    """Master on/off for the bsc_pm_overdue supervisor digest (default ON). Stored in
    maint_meta['overdue_reminder_enabled'] = 'on' | 'off'. The morning due digest is
    unaffected — only the overdue/cover digest is gated."""
    try:
        v = await db.fetchval("SELECT mval FROM maint_meta WHERE mkey='overdue_reminder_enabled'")
    except Exception:
        return True
    return (v or "on").strip().lower() != "off"


async def _run_reminders(dry: bool = False, kind: str = "all",
                         force_overdue: bool = False, board_mode: bool = False) -> dict:
    await ensure_tables()
    await _refresh_holidays()
    today = _today()
    on_leave = await _on_leave_ids(today)
    tdisp = today.strftime("%d-%m-%Y")

    def _due_disp(nd):
        """Format a schedule's due/pending date (ISO) as DD-MM-YYYY for the digest."""
        if not nd:
            return ""
        try:
            return _dt_date.fromisoformat(str(nd)[:10]).strftime("%d-%m-%Y")
        except Exception:
            return str(nd)

    # The bsc_pm_overdue supervisor digest only goes out within the working window
    # 09:00–before 19:00 IST. Outside that window no overdue item is added to the
    # supervisor escalation.
    # force_overdue=True ignores the time window — used when rendering the on-demand
    # overdue PNG (/maint/overdue-report.png), so the image is complete whenever fetched.
    # board_mode=True makes sup_pm/sup_bd the COMPLETE board for that PNG: every daily and
    # quarterly OVERDUE, every open breakdown, and every DUE-TODAY item — assigned or not —
    # rather than the narrower supervisor escalation (which omits assigned due/breakdown).
    allow_overdue = True if force_overdue else (9 <= datetime.now(IST).hour < 19)
    # The per-person due-today digest is confined to the same plant window 09:00–before
    # 19:00 IST, so a stray off-hours cron run (GitHub Actions drifts) never sends at night.
    allow_due = 9 <= datetime.now(IST).hour < 19
    overdue_suppressed = 0

    # each person's morning digest keeps preventive and breakdown lists separate
    due_by_assignee = {}   # emp_id -> {"name","mobile","pm":[],"bd":[]}
    sup_pm = []            # preventive: overdue + due-on-leave cover + unassigned -> supervisors
    sup_bd = []            # breakdown: unassigned / on-leave open breakdowns -> supervisors

    def _bucket(aid, name, mobile, kind, text):
        g = due_by_assignee.setdefault(aid, {"name": name, "mobile": mobile, "pm": [], "bd": []})
        g[kind].append(text)

    # ---- preventive maintenance: each person's PM list = pending (overdue + due) ----
    # DAILY PM overdue is decided EXACTLY like the Maintenance Board: per-person checkpoint
    # completion over a 7-day look-back, so a day where some people never finished counts as
    # overdue even though last_done advanced. QUARTERLY / periodic PM uses the date model.
    # The bsc_pm_overdue supervisor digest (sup_pm) carries whatever is overdue (daily past
    # miss + quarterly past due) plus unassigned-due cover; it is sent only inside the
    # 09:00–before 19:00 IST window. Each assignee also gets their own morning due digest.
    sched_all = await _schedule_rows()

    def _is_daily_s(s):
        ft = (s.get("freq_type") or "").lower()
        return ft == "daily" or (not ft and s.get("freq_days") and int(s["freq_days"]) == 1)

    daily_cl_ids = sorted({s["checklist_id"] for s in sched_all
                           if s.get("active") and s.get("checklist_id") and _is_daily_s(s)})
    cps_by_cl, done_set, absent_set, log_set, window_days = await _daily_window_state(
        sched_all, daily_cl_ids, today)
    q_cps, q_pk, q_done, q_absent, q_skip = await _quarterly_state(sched_all, today)
    cur_due = window_days[0] if window_days else today

    def _day_pending(checklist_id, period_iso, cps, is_current):
        """Mirror of the board's _day_overdue: a day is pending if a non-excused assignee
        checkpoint is not done; a PAST day counts only if it has completion evidence."""
        if not cps:
            return False
        has_cpdone = any((cp["id"], period_iso) in done_set for cp in cps)
        has_log = (checklist_id, period_iso) in log_set
        pending = 0
        for cp in cps:
            aidc = cp.get("assigned_emp_id")
            excused = aidc and ((checklist_id, aidc, period_iso) in absent_set
                                or (period_iso == today.isoformat() and aidc in on_leave))
            if excused:
                continue
            if (cp["id"], period_iso) not in done_set:
                pending += 1
        if pending == 0:
            return False
        if not has_cpdone and has_log:
            return False
        return bool(has_cpdone or has_log or is_current)

    emap = await _emp_map()

    def _pending_cp_aids(cps, dset, period, clid):
        """Checkpoint assignees who still have a not-done, non-excused checkpoint in
        `period`, so the per-person PM reminder reaches EVERYONE with pending checkpoints
        on the machine, not just the schedule's single main assignee."""
        aids, seen = [], set()
        for cp in (cps or []):
            a = cp.get("assigned_emp_id")
            if not a or a in seen:
                continue
            seen.add(a)
            if (clid, a, period) in absent_set or a in on_leave:
                continue
            theirs = [c for c in cps if c.get("assigned_emp_id") == a]
            if any((c["id"], period) not in dset for c in theirs):
                aids.append(a)
        return aids

    def _bucket_pending(cps, dset, period, clid, sched_aid, sched_name, sched_mobile, text):
        """Send the PM reminder to each checkpoint assignee with pending work (by their own
        mobile); fall back to the schedule's single assignee when the checklist has no
        per-checkpoint assignees."""
        pa = _pending_cp_aids(cps, dset, period, clid)
        if pa:
            for a in pa:
                e = emap.get(a) or {}
                if e.get("mobile"):
                    _bucket(a, e.get("name") or sched_name, e["mobile"], "pm", text)
        elif sched_aid and sched_mobile:
            _bucket(sched_aid, sched_name, sched_mobile, "pm", text)

    for s in sched_all:
        if not s.get("active"):
            continue
        label = _asset_label(s)
        task = s.get("title") or "PM"
        assignee = s.get("assignee_name") or "unassigned"
        aid = s.get("assignee_emp_id")
        mobile = s.get("assignee_mobile")

        if _is_daily_s(s):
            clid = s.get("checklist_id")
            cps = cps_by_cl.get(clid) or []
            start_d = s.get("start_date"); end_d = s.get("end_date")
            incomplete = []
            if clid and cps:
                for d in window_days:
                    if (start_d and d < start_d) or (end_d and d > end_d):
                        continue
                    if _day_pending(clid, d.isoformat(), cps, d == cur_due):
                        incomplete.append(d)
            else:
                ld = s.get("last_done")
                if (not (start_d and start_d > cur_due) and not (end_d and cur_due > end_d)
                        and (not ld or ld < cur_due) and not (aid and aid in on_leave)):
                    incomplete.append(cur_due)
            if not incomplete:
                continue
            # A daily is only ever "due today" — remind while TODAY's check is still pending,
            # and stop the moment today's is done. We do NOT escalate an earlier missed day as
            # overdue: a daily cannot be back-filled, so an old date would keep firing forever.
            if cur_due not in incomplete:
                continue
            if aid and aid in on_leave:
                continue
            if board_mode:  # full board PNG: show every due-today item, assigned or not
                sup_pm.append({"code": s.get("asset_code") or "", "name": s.get("asset_name") or "",
                               "cat": task, "who": assignee if aid else "Unassigned",
                               "date": tdisp, "status": "Due today", "by": "-"})
            elif _pending_cp_aids(cps, done_set, cur_due.isoformat(), clid) or (aid and mobile):
                _bucket_pending(cps, done_set, cur_due.isoformat(), clid, aid, assignee, mobile,
                                f"{label} - {task}")
            else:
                sup_pm.append({"code": s.get("asset_code") or "", "name": s.get("asset_name") or "",
                               "cat": task, "who": "Unassigned", "date": tdisp,
                               "status": "Due today", "by": "-"})
            continue

        # quarterly / periodic -> date model, plus per-person completion for quarterly
        info = compute_due(s, s.get("asset_hours"), today)
        clid = s.get("checklist_id")
        q_fully, q_started = _quarterly_progress(clid, q_cps, q_pk, q_done, q_absent, q_skip) \
            if ((s.get("freq_type") or "").lower() == "quarterly" and clid) else (None, None)
        if q_fully:
            continue  # quarterly fully done this quarter
        # Overdue/due uses the EXACT same rule as the Maintenance Board (_q_state): a
        # quarterly is overdue only once its SCHEDULED date has passed (info["overdue"]),
        # due on its scheduled date (info["due"]) — never just because a checkpoint was
        # ticked early. Both sides derive from compute_due()+_quarterly_progress, so the
        # reminder can only ever show what the board shows (no phantom "started" overdues).
        is_overdue = bool(info["overdue"])
        is_due = bool(info["due"] and not is_overdue)
        if not (is_overdue or is_due):
            continue
        if is_overdue:
            if aid and aid in on_leave:
                continue  # assignee on leave — don't flag their overdue work
            if not allow_overdue:
                overdue_suppressed += 1
                continue  # outside 09:00–19:00 IST — no overdue escalation
            od = _due_disp(info.get("next_due"))
            sup_pm.append({"code": s.get("asset_code") or "", "name": s.get("asset_name") or "",
                           "cat": task, "who": assignee, "date": od or tdisp,
                           "status": "Overdue", "by": "-"})
            _bucket_pending(q_cps.get(clid) or [], q_done, q_pk.get(clid), clid, aid, assignee, mobile,
                            f"{label} - {task} (overdue" + (f" since {od}" if od else "") + ")")
        else:  # due today
            if aid and aid in on_leave:
                continue  # on leave — not flagged to the supervisor's overdue alert
            if board_mode:  # full board PNG: show every due-today item, assigned or not
                sup_pm.append({"code": s.get("asset_code") or "", "name": s.get("asset_name") or "",
                               "cat": task, "who": assignee if aid else "Unassigned",
                               "date": tdisp, "status": "Due today", "by": "-"})
            elif _pending_cp_aids(q_cps.get(clid) or [], q_done, q_pk.get(clid), clid) or (aid and mobile):
                _bucket_pending(q_cps.get(clid) or [], q_done, q_pk.get(clid), clid, aid, assignee, mobile,
                                f"{label} - {task}")
            else:
                sup_pm.append({"code": s.get("asset_code") or "", "name": s.get("asset_name") or "",
                               "cat": task, "who": "Unassigned", "date": tdisp,
                               "status": "Due today", "by": "-"})  # unassigned-due cover

    # ---- open breakdowns: to EACH assigned maintenance user; else to supervisors ----
    for b in await _open_breakdowns():
        label = (str(b.get("asset_code") or "") + " " + str(b.get("asset_name") or "")).strip() or "—"
        prob = (b.get("problem") or "").strip() or "—"
        _braised = b.get("raised_at")
        bdate = _braised.astimezone(IST).strftime("%d-%b-%Y") if isinstance(_braised, datetime) else "-"
        # Who raised / assigned this work order — shown in the report's "Assigned By" column.
        brep = (b.get("reported_by") or "").strip() or "-"
        aids = b.get("assignees") or ([b["assignee_emp_id"]] if b.get("assignee_emp_id") else [])
        if board_mode:  # full board PNG: one row per open breakdown, every assignee shown
            nms = [(emap.get(a) or {}).get("name") for a in aids]
            nms = [n for n in nms if n]
            sup_bd.append({"code": b.get("asset_code") or "", "name": b.get("asset_name") or "",
                           "prob": prob, "who": ", ".join(nms) if nms else "Unassigned",
                           "status": "Open", "date": bdate, "by": brep})
            continue
        if not aids:
            sup_bd.append({"code": b.get("asset_code") or "", "name": b.get("asset_name") or "",
                           "prob": prob, "who": "Unassigned", "status": "Open", "date": bdate,
                           "by": brep})
            continue
        for aid in aids:
            if aid in on_leave:
                continue  # that person is on leave — skip (not raised to supervisor)
            info = emap.get(aid) or {}
            mob = info.get("mobile") or (b.get("assignee_mobile") if aid == b.get("assignee_emp_id") else "")
            who = info.get("name") or "unassigned"
            if mob:
                _bucket(aid, who, mob, "bd", f"{label} - {prob}")
            else:
                sup_bd.append({"code": b.get("asset_code") or "", "name": b.get("asset_name") or "",
                               "prob": prob, "who": who, "status": "Open", "date": bdate,
                               "by": brep})

    sent, skipped = [], []

    async def _dispatch(tmpl, mobile, params, meta):
        rec = dict(meta); rec["to"] = mobile
        if not mobile:
            rec["result"] = "skipped: no mobile"; skipped.append(rec); return
        if not tmpl:
            rec["result"] = "skipped: template not set"; skipped.append(rec); return
        if dry:
            rec["result"] = "dry-run"; sent.append(rec); return
        ok, detail = await _wsend(tmpl, mobile, params,
                                  name=(meta.get("assignee") or "Supervisor"),
                                  kind=(meta.get("kind") or ""))
        rec["result"] = "sent" if ok else f"failed: {detail}"
        (sent if ok else skipped).append(rec)

    # due digests — preventive and breakdown kept in separate variables. Held outside the
    # 09:00–before 19:00 IST window so no reminder goes out at night.
    if kind in ("due", "all") and not allow_due and due_by_assignee:
        skipped.append({"kind": "due-digest", "recipients": len(due_by_assignee),
                        "result": "skipped: outside 09:00–19:00 IST window"})
    elif kind in ("due", "all") and allow_due:
        for aid, g in due_by_assignee.items():
            pm_list = _join_items(g["pm"])
            bd_list = _join_items(g["bd"])
            # body vars: 1 name, 2 date, 3 preventive list, 4 breakdown list, 5 button.
            # WATI feeds the URL button's {{1}} from param "1" (the name), so it must
            # be URL-safe (spaces -> underscore) or WhatsApp drops the message.
            params = [_url_token(g["name"]), tdisp, pm_list, bd_list, "maint"]
            await _dispatch(WATI_MAINT_DUE_TEMPLATE, g["mobile"], params,
                            {"kind": "due-digest", "assignee": g["name"],
                             "preventive": len(g["pm"]), "breakdown": len(g["bd"])})

    # Admin overdue report — the WhatsApp digest whose image header is the Maintenance
    # Board overdue PNG. Whether to SEND it is decided by what the BOARD actually shows,
    # not by the narrow supervisor-escalation lists (sup_pm/sup_bd). Those escalation lists
    # intentionally omit items ASSIGNED to a person (e.g. an assigned overdue breakdown, which
    # routes to that person's own digest instead), so on a day whose only overdue items are
    # assigned they are empty and the admin report used to be suppressed — even though the
    # board, and the PNG the admins would receive, had rows. We therefore gate the send on
    # the FULL board content (board_mode), computed once here with a dry pass, so the admin
    # report reaches every admin whenever the board is not empty. WhatsApp fetches the PNG
    # header live at send time, so its content always matches the board too. This is why the
    # problem cannot recur: the send decision and the image now share one source of truth.
    board_pm, board_bd = sup_pm, sup_bd
    if kind in ("overdue", "all") and not board_mode:
        try:
            _bdata = await _run_reminders(dry=True, kind="overdue",
                                          force_overdue=True, board_mode=True)
            board_pm = _bdata.get("overdue_pm") or []
            board_bd = _bdata.get("overdue_bd") or []
        except Exception:
            board_pm, board_bd = sup_pm, sup_bd
    # It goes out ONLY inside the plant window 09:00–before 19:00 IST; outside that the whole
    # digest is held.
    _want_overdue = kind in ("overdue", "all") and (board_pm or board_bd)
    if _want_overdue and not allow_overdue:
        skipped.append({"kind": "overdue-digest", "preventive": len(board_pm),
                        "breakdown": len(board_bd),
                        "result": "skipped: outside 09:00–19:00 IST window"})
    elif _want_overdue and not await _overdue_reminder_enabled():
        skipped.append({"kind": "overdue-digest", "preventive": len(board_pm),
                        "breakdown": len(board_bd),
                        "result": "skipped: overdue reminder is turned OFF "
                                  "(turn it on at /maint/overdue-reminder?token=...&state=on)"})
    elif _want_overdue:
        # Image-header template (bsc_maint_overdue_report): the overdue board goes out
        # as a PNG (served live at /maint/overdue-report.png). No body variables — the
        # whole table is in the image. params carries the cache-busted header image URL
        # when the template header is a send-time variable (see _overdue_img_params);
        # otherwise it is empty and the template's fixed header URL is used.
        params = _overdue_img_params()
        sups = await _overdue_recipients()
        for sup in sups:
            nm = sup.get("name") or "Overdue recipient"
            mob = sup.get("mobile") or ""
            await _dispatch(WATI_MAINT_OVERDUE_TEMPLATE, wati.normalize_number(mob) or mob, params,
                            {"kind": "overdue-digest", "assignee": nm,
                             "preventive": len(board_pm), "breakdown": len(board_bd)})
        if not sups:
            skipped.append({"kind": "overdue-digest",
                            "preventive": len(board_pm), "breakdown": len(board_bd),
                            "result": "skipped: no admin has a mobile number set "
                                      "(add a mobile to the admin users, or pick recipients on the Schedules page)"})
    if overdue_suppressed:
        skipped.append({"kind": "overdue", "result": "skipped: outside 09:00–19:00 IST window",
                        "overdue_items": overdue_suppressed})
    return {"today": today.isoformat(), "sent": sent, "skipped": skipped,
            "allow_overdue": allow_overdue,
            "overdue_pm": sup_pm, "overdue_bd": sup_bd,
            "counts": {"sent": len(sent), "skipped": len(skipped)}}


@router.get("/run-reminders")
async def run_reminders(token: str = Query(default=""), dry: int = Query(default=0),
                        kind: str = Query(default="all")):
    """Protected cron endpoint. ?token=<AGENT_TOKEN>
       &kind=due     -> morning due reminders to assignees (run ~9am)
       &kind=overdue -> overdue list to supervisors (run ~3pm)
       &kind=all     -> both (default).  &dry=1 to preview without sending."""
    if not AGENT_TOKEN or token != AGENT_TOKEN:
        raise HTTPException(401, "bad token")
    k = kind if kind in ("due", "overdue", "all") else "all"
    # HARD WINDOW GUARD — the single chokepoint that makes off-hours sends impossible.
    # GitHub Actions can execute a scheduled run hours late (even at night), so a real
    # (non-dry) run outside 09:00–before 19:00 IST sends NOTHING AT ALL and returns early,
    # before any reminder type is evaluated. This covers every current and future reminder
    # type on this endpoint; the per-digest allow_due/allow_overdue checks remain as backup.
    # &dry=1 previews are allowed any time for testing.
    now_ist = datetime.now(IST)
    if not bool(dry) and not (9 <= now_ist.hour < 19):
        return _json({"today": _today().isoformat(), "sent": [],
                      "skipped": [{"kind": k, "result":
                                   f"skipped: outside 09:00–19:00 IST window (now {now_ist.strftime('%H:%M')} IST)"}],
                      "window_blocked": True,
                      "counts": {"sent": 0, "skipped": 1}})
    return _json(await _run_reminders(dry=bool(dry), kind=k))


@router.get("/overdue-report.png")
async def overdue_report_png(token: str = Query(default="")):
    """Live PNG of the maintenance overdue board (S.No, Date, M/C Code, Machine Name,
    Category, Status, Assigned To), rendered like the daily stock report. This URL is the
    image header of the bsc_maint_overdue_report WhatsApp template, so WhatsApp fetches a
    current image each time the overdue digest is sent. Public but token-gated with
    OVERDUE_IMG_TOKEN (falls back to AGENT_TOKEN):
    /maint/overdue-report.png?token=<OVERDUE_IMG_TOKEN>. force_overdue=True so the image is
    complete at any hour."""
    if token != OVERDUE_IMG_TOKEN and token != AGENT_TOKEN:
        raise HTTPException(403, "Bad token")
    try:
        data = await _run_reminders(dry=True, kind="overdue", force_overdue=True, board_mode=True)
        pm = data.get("overdue_pm") or []
        bd = data.get("overdue_bd") or []
    except Exception:
        pm, bd = [], []
    # date AND time the report is generated (= when WhatsApp fetches it to send), so each
    # overdue report carries the exact moment it went out, e.g. "04-11-2026 03:12 PM".
    tdisp = datetime.now(IST).strftime("%d-%m-%Y %I:%M %p")
    png = overdue_img.build_overdue_png(tdisp, pm, bd)
    return Response(content=png, media_type="image/png",
                    headers={"Cache-Control": "no-store, max-age=0",
                             "Content-Disposition": "inline; filename=overdue.png"})


@router.get("/overdue-reminder")
async def overdue_reminder_switch(token: str = Query(default=""), state: str = Query(default="")):
    """Turn the bsc_pm_overdue supervisor digest ON or OFF (the morning due digest is
    not affected). Open /maint/overdue-reminder?token=<AGENT_TOKEN> to see the current
    state, add &state=off to stop it sending, &state=on to resume."""
    if not AGENT_TOKEN or token != AGENT_TOKEN:
        raise HTTPException(403, "Bad token")
    await ensure_tables()
    st = (state or "").strip().lower()
    if st in ("off", "on"):
        await db.execute(
            "INSERT INTO maint_meta (mkey,mval) VALUES ('overdue_reminder_enabled',$1) "
            "ON CONFLICT (mkey) DO UPDATE SET mval=$1", st)
    cur = (await db.fetchval("SELECT mval FROM maint_meta WHERE mkey='overdue_reminder_enabled'")
           or "on").strip().lower()
    return _json({"overdue_reminder": cur,
                  "sends_overdue_digest": cur != "off",
                  "note": "add &state=off to stop the bsc_pm_overdue digest, &state=on to resume"})


@router.get("/admin/clear-overdue")
async def clear_overdue(token: str = Query(default=""), before: str = Query(default=""),
                        dry: int = Query(default=0), emp: dict = Depends(current_employee)):
    """Clear the overdue backlog: every schedule whose next due date is on or before
    `before` (YYYY-MM-DD, default yesterday) is marked done on that date, so it stops
    showing overdue while the schedule keeps running from its next cycle. The schedules
    themselves are NOT deleted. Admin only (or ?token=AGENT_TOKEN). Add &dry=1 to preview."""
    await ensure_tables()
    if not ((emp and emp.get("is_admin")) or (AGENT_TOKEN and token == AGENT_TOKEN)):
        raise HTTPException(403, "Admins only")
    try:
        cutoff = date.fromisoformat(before) if before else (_today() - timedelta(days=1))
    except ValueError:
        raise HTTPException(400, "bad date — use YYYY-MM-DD, e.g. 2026-10-21")
    today = _today()
    rows = await db.fetch("SELECT * FROM maint_schedules WHERE COALESCE(active, true)")
    hit = []
    for s in rows:
        s = dict(s)
        nd = next_due_date(s, today)
        if nd and nd <= cutoff:
            hit.append(s["id"])
    if hit and not dry:
        await db.execute("UPDATE maint_schedules SET last_done=$1 WHERE id = ANY($2::int[])", cutoff, hit)
    return _json({"ok": True, "cleared": len(hit), "cutoff": cutoff.isoformat(), "dry_run": bool(dry)})


@router.get("/admin/test-overdue")
async def test_overdue(token: str = Query(default=""), to: str = Query(default=""),
                       emp: dict = Depends(require_employee)):
    """Send a SAMPLE overdue digest (dummy machine list) to one number so you can
    preview the formatting/line-breaks. Admin or ?token=AGENT_TOKEN.
    Open qms.bharatsteels.in/maint/admin/test-overdue?token=...&to=919XXXXXXXXX"""
    await ensure_tables()
    if not (emp.get("is_admin") or (AGENT_TOKEN and token == AGENT_TOKEN)):
        raise HTTPException(403, "Admins only")
    num = wati.normalize_number(to) if to else ""
    if not num:
        subs = await _overdue_recipient_mobiles()
        num = wati.normalize_number(subs[0]) if subs else ""
    if not num:
        return _json({"ok": False, "detail": "No number — add ?to=919XXXXXXXXX to the URL."})
    # The overdue digest is now an IMAGE-header template (bsc_maint_overdue_report) with
    # no body variables — the whole board is the PNG at /maint/overdue-report.png — so the
    # test send passes empty params, exactly like the live digest.
    ok, detail = await _wsend(WATI_MAINT_OVERDUE_TEMPLATE, num, _overdue_img_params(),
                              name="(test)", kind="test")
    return _json({"ok": ok, "sent_to": num, "template": WATI_MAINT_OVERDUE_TEMPLATE,
                  "build": MAINT_BUILD,
                  "image_url": "/maint/overdue-report.png?token=<AGENT_TOKEN>",
                  "note": "image-header template; the live board is the PNG in the header",
                  "wati_reply": detail})


@router.get("/admin/export-onedrive")
async def export_onedrive(token: str = Query(default=""), emp: dict = Depends(current_employee)):
    """Generate the Maintenance Log / Machine History Card workbook from the app's
    recorded data (maint_logs + maint_breakdowns) and save it to OneDrive. Admin or
    ?token=AGENT_TOKEN. Run once a day by the GitHub Actions export job."""
    await ensure_tables()
    if not ((emp and emp.get("is_admin")) or (AGENT_TOKEN and token == AGENT_TOKEN)):
        raise HTTPException(403, "Admins only")
    from .. import maint_export
    await _refresh_holidays()
    logs = await db.fetch("SELECT * FROM maint_logs ORDER BY done_at")
    bds = await db.fetch("SELECT * FROM maint_breakdowns ORDER BY raised_at")
    assets = await db.fetch("SELECT * FROM maint_assets ORDER BY code")
    emps = await db.fetch("SELECT id, name, emp_no FROM employees")
    content = maint_export.build(logs, bds, assets, emps, holidays=HOLIDAYS)
    path = f"{ONEDRIVE_BASE}/Maintenance/Maintenance_Log_Machine_History_Card.xlsx"
    try:
        gtoken = await graph.get_token()
        await graph.upload_file(
            gtoken, path, content,
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    except Exception as e:
        return _json({"ok": False, "path": path, "size": len(content), "detail": str(e)[:300]})
    return _json({"ok": True, "path": path, "size": len(content),
                  "events": len(logs) + len(bds), "machines": len(assets)})


@router.get("/admin/backfill-reports")
async def backfill_reports(token: str = Query(default=""), limit: int = Query(default=6),
                           skip: int = Query(default=0), kind: str = Query(default="pm"),
                           days: int = Query(default=0),
                           emp: dict = Depends(current_employee)):
    """Seed OneDrive with a work-order PDF for the recorded history — one PM report per
    machine per DAY (matching the Excel log), filed in that machine's own folder as
    <dd-mm-yy>_<code>_pm.pdf, and one report per breakdown as <dd-mm-yy>_<code>_bm_<id>.pdf.
    Paginated so each call stays under Vercel's 10s limit: it processes `limit` records
    starting at `skip`, and the reply's `next_url` is the link to click for the next page
    until `remaining` is 0.
      kind=pm  -> preventive reports, one per machine per day (default)
      kind=bd  -> breakdown reports, one per breakdown
    Admin or ?token=AGENT_TOKEN. New completions/breakdowns file their own report
    automatically from now on; this is only for back-filling existing history."""
    await ensure_tables()
    if not ((emp and emp.get("is_admin")) or (AGENT_TOKEN and token == AGENT_TOKEN)):
        raise HTTPException(403, "Admins only")
    limit = max(1, min(int(limit or 6), 12))
    skip = max(0, int(skip or 0))
    k = kind if kind in ("pm", "bd") else "pm"
    days = max(0, int(days or 0))
    cutoff = (_today() - timedelta(days=days - 1)) if days > 0 else None  # days=1 -> today only
    saved, paths, missing = 0, [], []

    if k == "pm":
        # all completions, then keep the latest per (machine, day) so the backfill
        # mirrors the Excel log: one PM report per machine per day. `days` limits it to
        # recent completions so the daily auto-reconcile stays small and fast.
        q = ("SELECT l.*, a.code AS asset_code, a.name AS asset_name, "
             "a.location AS location, c.name AS checklist_name "
             "FROM maint_logs l "
             "LEFT JOIN maint_assets a ON a.id=l.asset_id "
             "LEFT JOIN maint_checklists c ON c.id=l.checklist_id "
             "WHERE l.asset_id IS NOT NULL AND l.done_at IS NOT NULL "
             + ("AND l.done_at::date >= $1 " if cutoff else "")
             + "ORDER BY l.done_at")
        allrows = await (db.fetch(q, cutoff) if cutoff else db.fetch(q))
        latest = {}
        for r in allrows:
            r = dict(r)
            # one report per machine per FREQUENCY per day, so a daily and a quarterly
            # completion on the same day each get their own report in their own subfolder
            key = (r["asset_id"], _freq_label(r.get("checklist_name")), r["done_at"].date())
            prev = latest.get(key)
            if prev is None or r["done_at"] >= prev["done_at"]:
                latest[key] = r
        rows = sorted(latest.values(), key=lambda r: (r.get("asset_code") or "", r["done_at"]))
        total = len(rows)
        for l in rows[skip:skip + limit]:
            try:
                res = l["results"]
                if isinstance(res, str):
                    res = json.loads(res or "[]")
                p = await _file_pm_report(
                    asset_code=l["asset_code"], asset_name=l["asset_name"],
                    checklist=l["checklist_name"] or "Checklist", doc_no=str(l["id"]),
                    done_by=l["done_by"], results=res or [], checklist_id=l["checklist_id"],
                    location=l["location"], when=l["done_at"])
                if p:
                    saved += 1; paths.append(p)
            except Exception as e:
                print("[maint] backfill PM skipped:", e)
        # machines that have NO completion at all -> no report can be made yet
        # (only meaningful on a full run, not the recent-days reconcile)
        if not cutoff:
            done_assets = {r["asset_id"] for r in rows}
            for a in await db.fetch("SELECT id, code, name FROM maint_assets ORDER BY code"):
                if a["id"] not in done_assets:
                    missing.append(f'{a["code"]} {a["name"]}'.strip())
    else:
        q = ("SELECT b.*, a.code AS asset_code, a.name AS asset_name, a.location AS location "
             "FROM maint_breakdowns b LEFT JOIN maint_assets a ON a.id=b.asset_id "
             "WHERE b.asset_id IS NOT NULL AND b.raised_at IS NOT NULL "
             + ("AND b.raised_at::date >= $1 " if cutoff else "")
             + "ORDER BY a.code, b.raised_at")
        rows = await (db.fetch(q, cutoff) if cutoff else db.fetch(q))
        rows = [dict(r) for r in rows]
        total = len(rows)
        for b in rows[skip:skip + limit]:
            try:
                ids = b.get("assignees") or ([b["assignee_emp_id"]] if b.get("assignee_emp_id") else [])
                names = []
                if ids:
                    nm = {r["id"]: (r["name"] or r["emp_no"]) for r in await db.fetch(
                        "SELECT id, name, emp_no FROM employees WHERE id = ANY($1::int[])", ids)}
                    names = [nm[i] for i in ids if nm.get(i)]
                p = await _file_breakdown_report(b, ", ".join(names))
                if p:
                    saved += 1; paths.append(p)
            except Exception as e:
                print("[maint] backfill BD skipped:", e)

    done_through = min(skip + limit, total)
    remaining = max(0, total - done_through)
    base = f"https://qms.bharatsteels.in/maint/admin/backfill-reports?token={token}&kind={k}&limit={limit}"
    if days:
        base += f"&days={days}"
    out = {"ok": True, "kind": k, "records_total": total, "saved_this_page": saved,
           "done_through": done_through, "remaining": remaining,
           "note_naming": ("Saved under BSC Inspections/Maintenance/Reports/<machine>/"
                           + ("<Daily|Quarterly Preventive Maintenance>/ as <dd-mm-yy>_<code>_pm.pdf"
                              if k == "pm"
                              else "Breakdown Maintenance/ as <dd-mm-yy>_<code>_bm_<id>.pdf")),
           "files": paths}
    if remaining:
        out["next_url"] = f"{base}&skip={done_through}"
    else:
        out["note"] = ("All preventive history done. Now run kind=bd for breakdown reports."
                       if k == "pm" else "All breakdown reports done.")
    if missing:
        out["machines_without_any_completion"] = missing
        out["note_missing"] = ("These machines have no completed checklist yet, so no "
                               "preventive report exists for them — they'll get one the "
                               "first time their checklist is completed in the app.")
    return _json(out)


_REPORT_SUBS = ("Daily Preventive Maintenance",
                "Quarterly Preventive Maintenance",
                "Breakdown Maintenance")


@router.get("/admin/ensure-report-folders")
async def ensure_report_folders(token: str = Query(default=""), skip: int = Query(default=0),
                                limit: int = Query(default=3),
                                emp: dict = Depends(current_employee)):
    """Make every machine folder already under Maintenance/Reports/ have all three
    subfolders (Daily / Quarterly / Breakdown), and tidy any loose PDFs that sit
    directly in a machine folder (e.g. folders moved in from the old Preventive) by
    moving them into that machine's 'Daily Preventive Maintenance' subfolder.
    Idempotent and paginated — follow next_url until remaining is 0. Admin or ?token=."""
    await ensure_tables()
    if not ((emp and emp.get("is_admin")) or (AGENT_TOKEN and token == AGENT_TOKEN)):
        raise HTTPException(403, "Admins only")
    limit = max(1, min(int(limit or 3), 5))
    skip = max(0, int(skip or 0))
    base = f"{ONEDRIVE_BASE}/Maintenance/Reports"
    try:
        gtoken = await graph.get_token()
    except Exception as e:
        raise HTTPException(500, f"Graph token error: {e}")
    machines = sorted(
        [m["name"] for m in await graph.list_children(gtoken, base) if m.get("is_folder")])
    total = len(machines)
    done = []
    for name in machines[skip:skip + limit]:
        mpath = f"{base}/{name}"
        moved = 0
        try:
            for sub in _REPORT_SUBS:                       # create the 3 subfolders
                await graph.create_folder(gtoken, mpath, sub)
            # move any loose PDFs (sitting directly in the machine folder) into Daily
            loose = [c for c in await graph.list_children(gtoken, mpath)
                     if (not c.get("is_folder")) and (c.get("name") or "").lower().endswith(".pdf")]
            if loose:
                daily_id = await graph.get_item_id(gtoken, f"{mpath}/Daily Preventive Maintenance")
                for c in loose:
                    if daily_id and await graph.move_item(gtoken, c["id"], daily_id):
                        moved += 1
        except Exception as e:
            print("[maint] ensure-report-folders skip", name, e)
        done.append({"machine": name, "loose_pdfs_moved": moved})
    done_through = min(skip + limit, total)
    remaining = max(0, total - done_through)
    out = {"ok": True, "machines_total": total, "done_this_page": done,
           "done_through": done_through, "remaining": remaining}
    if remaining:
        out["next_url"] = (f"https://qms.bharatsteels.in/maint/admin/ensure-report-folders"
                           f"?token={token}&limit={limit}&skip={done_through}")
    else:
        out["note"] = ("Every machine in Reports now has Daily / Quarterly / Breakdown "
                       "subfolders; any loose PDFs were moved into Daily Preventive Maintenance.")
    return _json(out)


def _wo_category(cname: str) -> str:
    return "Quarterly PM" if "quarter" in (cname or "").lower() else "Daily PM"


@router.get("/monitor/data")
async def monitor_data(days: int = Query(default=30), emp: dict = Depends(require_employee)):
    """Admin-only work-order monitor: every PM completion (grouped per machine +
    checklist + period) and every breakdown, with per-assignee status. Also lists
    today's not-yet-started Daily PM work orders so the admin can mark an absentee."""
    await ensure_tables()
    await _ensure_absent_table()
    if not emp.get("is_admin"):
        raise HTTPException(403, "Admins only")
    days = max(1, min(int(days or 30), 400))
    today = _today()
    cutoff = today - timedelta(days=days - 1)
    emps = {e["id"]: (e["name"] or e["emp_no"])
            for e in await db.fetch("SELECT id, name, emp_no FROM employees")}

    # ---- PM work orders from the logs, grouped by (asset, checklist, period) ----
    wo = {}
    for l in await db.fetch(
            "SELECT l.asset_id, l.checklist_id, l.done_at, a.code AS acode, a.name AS aname, "
            "c.name AS cname FROM maint_logs l "
            "LEFT JOIN maint_assets a ON a.id=l.asset_id "
            "LEFT JOIN maint_checklists c ON c.id=l.checklist_id "
            "WHERE l.done_at::date >= $1 AND l.asset_id IS NOT NULL ORDER BY l.done_at", cutoff):
        cname = l["cname"] or ""
        dd = l["done_at"].astimezone(IST).date() if isinstance(l["done_at"], datetime) else None
        pk = _period_key(cname, dd)
        key = (l["asset_id"], l["checklist_id"], pk)
        g = wo.get(key)
        if g is None:
            wo[key] = {"date": dd, "pk": pk, "acode": l["acode"], "aname": l["aname"],
                       "cname": cname, "checklist_id": l["checklist_id"], "has_log": True}
        elif dd and (g["date"] is None or dd > g["date"]):
            g["date"] = dd

    # Add today's not-yet-started DAILY work orders (so absentees can be marked) and any
    # QUARTERLY whose scheduled date in THIS calendar quarter has already arrived — so a
    # due/overdue quarterly shows even when nobody has started it yet (no log). Quarterly
    # checklists whose quarter date is still ahead are left off.
    q_due_clids = set()
    q_due_date = {}     # checklist_id -> its scheduled date THIS quarter (for the Date column)
    for s in await db.fetch(
            "SELECT s.*, a.code AS acode, a.name AS aname, c.name AS cname "
            "FROM maint_schedules s LEFT JOIN maint_assets a ON a.id=s.asset_id "
            "LEFT JOIN maint_checklists c ON c.id=s.checklist_id "
            "WHERE COALESCE(s.active,true)=true AND s.checklist_id IS NOT NULL"):
        cname = s["cname"] or ""
        ft = (s["freq_type"] or "").lower()
        is_daily = ft == "daily" or (not ft and s["freq_days"] and int(s["freq_days"]) == 1)
        is_q = "quarter" in cname.lower()
        if is_daily:
            pk = _period_key(cname, today)
        elif is_q:
            cur_due = _q_cur_due(dict(s), today)
            if cur_due:
                q_due_date[s["checklist_id"]] = cur_due
            if not (cur_due and cur_due <= today):
                continue
            q_due_clids.add(s["checklist_id"])
            pk = _period_key(cname, today)
        else:
            continue
        key = (s["asset_id"], s["checklist_id"], pk)
        if key not in wo:
            wo[key] = {"date": today, "pk": pk, "acode": s["acode"], "aname": s["aname"],
                       "cname": cname, "checklist_id": s["checklist_id"], "has_log": False}

    # checkpoints (with assignee) for the involved checklists
    cl_ids = list({k[1] for k in wo.keys() if k[1]})
    cps_by_cl = {}
    if cl_ids:
        for cp in await db.fetch(
                "SELECT id, checklist_id, assigned_emp_id FROM maint_checkpoints "
                "WHERE checklist_id = ANY($1::int[])", cl_ids):
            cps_by_cl.setdefault(cp["checklist_id"], []).append(cp)
    # which checkpoints are done, for the involved periods
    pks = list({k[2] for k in wo.keys()})
    all_cp_ids = [cp["id"] for lst in cps_by_cl.values() for cp in lst]
    done_set = set()
    if all_cp_ids and pks:
        for d in await db.fetch(
                "SELECT checkpoint_id, period_key FROM maint_checkpoint_done "
                "WHERE period_key = ANY($1::text[]) AND checkpoint_id = ANY($2::int[])", pks, all_cp_ids):
            done_set.add((d["checkpoint_id"], d["period_key"]))
    absent_set = {(a["checklist_id"], a["period_key"], a["emp_id"])
                  for a in await db.fetch("SELECT checklist_id, period_key, emp_id FROM maint_wo_absent")}
    skip_set = await _skipped_set(pks)   # admin-skipped quarterly periods -> hidden here

    rows = []
    for (asset_id, checklist_id, pk), g in wo.items():
        cps = cps_by_cl.get(checklist_id, [])
        assignee_ids, seen = [], set()
        for cp in cps:
            aid = cp["assigned_emp_id"]
            if aid and aid not in seen:
                seen.add(aid); assignee_ids.append(aid)
        # Only show PM work orders that actually have someone assigned. A machine whose
        # (quarterly or daily) checklist has no assignee is not actionable here, so it
        # must not appear in the Monitor.
        if not assignee_ids:
            continue
        is_daily = "quarter" not in (g["cname"] or "").lower()
        # Quarterly: show only when its scheduled quarterly date is actually due/overdue.
        if not is_daily and checklist_id not in q_due_clids:
            continue
        # admin skipped this quarterly for this period -> drop it from the Monitor
        if not is_daily and (checklist_id, pk) in skip_set:
            continue
        # is there any precise per-checkpoint 'done' data for this work order's period?
        has_cpdone = any((cp["id"], pk) in done_set for cp in cps)
        statuses = []
        for aid in assignee_ids:
            if (checklist_id, pk, aid) in absent_set:
                st = "Absent"
            else:
                their = [cp["id"] for cp in cps if cp["assigned_emp_id"] == aid]
                if their and all((cid, pk) in done_set for cid in their):
                    st = "Done"
                elif g.get("has_log") and not has_cpdone:
                    # the checklist was completed (a log + report exist) but no
                    # per-checkpoint detail maps to the current checkpoints (older log,
                    # or checkpoints re-created since) — treat as Done, not falsely Pending
                    st = "Done"
                else:
                    st = "Pending"
            statuses.append({"emp": emps.get(aid, "#%s" % aid), "emp_id": aid, "status": st})
        # Checkpoints with NO assignee can never be completed by a user, so they keep a
        # report stuck on "pending" forever. Surface them as a single "Unassigned" row the
        # admin can mark done (emp_id 0). Only shown while one is still pending.
        unassigned = [cp["id"] for cp in cps if not cp["assigned_emp_id"]]
        if unassigned and not all((uid, pk) in done_set for uid in unassigned) \
                and not (g.get("has_log") and not has_cpdone):
            statuses.append({"emp": "Unassigned", "emp_id": 0, "status": "Pending"})
        # For a quarterly, the Date column is the machine's SCHEDULED date this quarter
        # (from the annual plan) — not the day it happened to be completed on.
        row_date = g["date"]
        if not is_daily and q_due_date.get(checklist_id):
            row_date = q_due_date[checklist_id]
        rows.append({
            "date": row_date.isoformat() if row_date else "",
            "category": _wo_category(g["cname"]), "machine_code": g["acode"] or "",
            "machine_name": g["aname"] or "", "assigner": "Admin",
            "statuses": statuses, "checklist_id": checklist_id, "period_key": pk,
            "can_absent": is_daily, "kind": "pm", "has_report": bool(g.get("has_log"))})

    # ---- breakdown work orders ----
    for b in await db.fetch(
            "SELECT b.id, b.reported_by, b.assignee_emp_id, b.assignees, b.status, b.raised_at, "
            "a.code AS acode, a.name AS aname FROM maint_breakdowns b "
            "LEFT JOIN maint_assets a ON a.id=b.asset_id WHERE b.raised_at::date >= $1 "
            "ORDER BY b.raised_at DESC", cutoff):
        aids = []
        raw = b["assignees"]
        if raw:
            try:
                raw = raw if isinstance(raw, list) else json.loads(raw)
                aids = [int(x) for x in raw]
            except Exception:
                aids = []
        if not aids and b["assignee_emp_id"]:
            aids = [b["assignee_emp_id"]]
        bstatus = (b["status"] or "open").title()
        rows.append({
            "date": b["raised_at"].astimezone(IST).date().isoformat()
                    if isinstance(b["raised_at"], datetime) else "",
            "category": "BM", "machine_code": b["acode"] or "", "machine_name": b["aname"] or "",
            "assigner": b["reported_by"] or "—",
            "statuses": [{"emp": emps.get(aid, "#%s" % aid), "emp_id": aid, "status": bstatus}
                         for aid in aids],
            "checklist_id": None, "period_key": None, "can_absent": False, "kind": "bm",
            "bd_id": b["id"], "has_report": True})

    rows.sort(key=lambda r: (r["date"], r["machine_code"]), reverse=True)
    return _json({"ok": True, "rows": rows, "days": days})


@router.get("/monitor/report")
async def monitor_report(code: str = Query(default=""), name: str = Query(default=""),
                         cat: str = Query(default=""), date: str = Query(default=""),
                         bd: int = Query(default=0), dl: int = Query(default=0),
                         emp: dict = Depends(require_employee)):
    """Admin-only: stream the generated report PDF for a Monitor row straight from
    OneDrive — View (inline) or Download (attachment). It lists the machine's report
    subfolder and matches the file by machine code + date (allowing a 1-day timezone
    edge), or by breakdown id for a BM, so it works even if the saved filename's date
    differs slightly from the row's date."""
    if not emp.get("is_admin"):
        raise HTTPException(403, "Admins only")
    folder = _machine_folder(code, name)
    safe = _safe_code(code)
    if cat == "BM":
        sub = "Breakdown Maintenance"
    elif cat == "Quarterly PM":
        sub = "Quarterly Preventive Maintenance"
    else:
        sub = "Daily Preventive Maintenance"
    path = f"{ONEDRIVE_BASE}/Maintenance/Reports/{folder}/{sub}"
    try:
        token = await graph.get_token()
        children = await graph.list_children(token, path)
    except Exception as e:
        raise HTTPException(502, f"OneDrive error: {e}")
    pdfs = [c for c in children
            if not c.get("is_folder") and (c.get("name") or "").lower().endswith(".pdf")]
    target = None
    if cat == "BM":
        suffix = f"_bm_{bd}.pdf".lower()
        target = next((c for c in pdfs if (c.get("name") or "").lower().endswith(suffix)), None)
    else:
        want = f"_{safe}_pm.pdf".lower()
        cand = [c for c in pdfs if (c.get("name") or "").lower().endswith(want)]
        d0 = None
        try:
            d0 = datetime.strptime((date or "").strip(), "%Y-%m-%d").date()
        except ValueError:
            d0 = None
        if d0:
            for delta in (0, -1, 1):          # tolerate a 1-day tz edge in the filename
                pref = (d0 + timedelta(days=delta)).strftime("%d-%m-%y").lower() + "_"
                target = next((c for c in cand if (c.get("name") or "").lower().startswith(pref)), None)
                if target:
                    break
        if not target and len(cand) == 1:
            target = cand[0]
    if not target:
        raise HTTPException(404, "No report file found for this work order.")
    data = await graph.download_item(token, target["id"])
    if data is None:
        raise HTTPException(404, "Report file could not be read.")
    fn = target.get("name") or "report.pdf"
    disp = "attachment" if dl else "inline"
    return Response(content=data, media_type="application/pdf",
                    headers={"Content-Disposition": f'{disp}; filename="{fn}"'})


async def _locate_report_item(token, code, name, cat, date_iso, bd=0):
    """Find the OneDrive report PDF for a work order (same matching as /monitor/report).
    Returns the drive item dict {id,name} or None."""
    folder = _machine_folder(code, name)
    safe = _safe_code(code)
    sub = ("Breakdown Maintenance" if cat == "BM"
           else "Quarterly Preventive Maintenance" if cat == "Quarterly PM"
           else "Daily Preventive Maintenance")
    path = f"{ONEDRIVE_BASE}/Maintenance/Reports/{folder}/{sub}"
    try:
        children = await graph.list_children(token, path)
    except Exception:
        return None
    pdfs = [c for c in children
            if not c.get("is_folder") and (c.get("name") or "").lower().endswith(".pdf")]
    if cat == "BM":
        suf = f"_bm_{bd}.pdf".lower()
        return next((c for c in pdfs if (c.get("name") or "").lower().endswith(suf)), None)
    want = f"_{safe}_pm.pdf".lower()
    cand = [c for c in pdfs if (c.get("name") or "").lower().endswith(want)]
    d0 = None
    try:
        d0 = datetime.strptime((date_iso or "").strip(), "%Y-%m-%d").date()
    except ValueError:
        d0 = None
    if d0:
        for delta in (0, -1, 1):
            pref = (d0 + timedelta(days=delta)).strftime("%d-%m-%y").lower() + "_"
            t = next((c for c in cand if (c.get("name") or "").lower().startswith(pref)), None)
            if t:
                return t
    return cand[0] if len(cand) == 1 else None


async def _delete_pm_workorder(checklist_id, pk, cname, asset_id) -> int:
    """Delete a PM work order for one period: every maint_log in that period, the
    per-checkpoint 'done' rows, any absent marks, and revert the schedule's last_done
    to the latest remaining completion (so the task shows due again). Returns how many
    logs were removed."""
    logs = await db.fetch(
        "SELECT id, done_at FROM maint_logs WHERE checklist_id=$1 AND asset_id=$2 "
        "AND done_at IS NOT NULL", checklist_id, asset_id)
    del_ids = [l["id"] for l in logs
               if _period_key(cname, l["done_at"].astimezone(IST).date()) == pk]
    if del_ids:
        await db.execute("DELETE FROM maint_logs WHERE id = ANY($1::int[])", del_ids)
    cp_ids = [r["id"] for r in await db.fetch(
        "SELECT id FROM maint_checkpoints WHERE checklist_id=$1", checklist_id)]
    if cp_ids:
        await db.execute(
            "DELETE FROM maint_checkpoint_done WHERE period_key=$1 AND checkpoint_id = ANY($2::int[])",
            pk, cp_ids)
    try:
        await db.execute(
            "DELETE FROM maint_wo_absent WHERE checklist_id=$1 AND period_key=$2", checklist_id, pk)
    except Exception:
        pass
    remaining = await db.fetchval(
        "SELECT MAX(done_at) FROM maint_logs WHERE checklist_id=$1 AND asset_id=$2",
        checklist_id, asset_id)
    last_d = remaining.astimezone(IST).date() if remaining else None
    await db.execute(
        "UPDATE maint_schedules SET last_done=$1 WHERE checklist_id=$2", last_d, checklist_id)
    return len(del_ids)


@router.post("/workorder/delete")
async def workorder_delete(body: dict, emp: dict = Depends(require_employee)):
    """Admin-only. Permanently delete a whole work order and its report PDF.
      kind=pm  : by schedule_id (board = latest completion) OR checklist_id+period_key
                 (monitor = that exact period). Removes the period's logs + per-checkpoint
                 data + absent marks and reverts the schedule, then deletes the PDF.
      kind=bm  : by bd_id. Removes the breakdown ticket, then deletes its PDF.
    code/name/date are used to locate the OneDrive PDF."""
    await ensure_tables()
    await _ensure_absent_table()
    if not emp.get("is_admin"):
        raise HTTPException(403, "Admins only")
    kind = (body.get("kind") or "pm").lower()
    code = body.get("code") or ""
    name = body.get("name") or ""
    date_iso = body.get("date") or ""
    pdf_deleted = False
    try:
        token = await graph.get_token()
    except Exception:
        token = None

    if kind == "bm":
        try:
            bd_id = int(body.get("bd_id") or 0)
        except (TypeError, ValueError):
            bd_id = 0
        if not bd_id:
            raise HTTPException(400, "Missing breakdown id")
        row = await db.fetchrow(
            "SELECT a.code AS acode, a.name AS aname FROM maint_breakdowns b "
            "LEFT JOIN maint_assets a ON a.id=b.asset_id WHERE b.id=$1", bd_id)
        await db.execute("DELETE FROM maint_breakdowns WHERE id=$1", bd_id)
        if token:
            item = await _locate_report_item(
                token, code or (row["acode"] if row else ""),
                name or (row["aname"] if row else ""), "BM", date_iso, bd=bd_id)
            if item:
                pdf_deleted = await graph.delete_item(token, item["id"])
        return _json({"ok": True, "deleted": "breakdown", "pdf_deleted": pdf_deleted})

    # ---- PM ----
    schedule_id = body.get("schedule_id")
    checklist_id = body.get("checklist_id")
    pk = body.get("period_key")
    cname = ""
    asset_id = None
    if schedule_id and not (checklist_id and pk):
        s = await db.fetchrow(
            "SELECT s.checklist_id, s.asset_id, c.name AS cname FROM maint_schedules s "
            "LEFT JOIN maint_checklists c ON c.id=s.checklist_id WHERE s.id=$1", schedule_id)
        if not s:
            raise HTTPException(404, "Schedule not found")
        checklist_id, asset_id, cname = s["checklist_id"], s["asset_id"], s["cname"] or ""
        latest = await db.fetchrow(
            "SELECT done_at FROM maint_logs WHERE (schedule_id=$1 OR (checklist_id=$2 AND asset_id=$3)) "
            "AND done_at IS NOT NULL ORDER BY done_at DESC LIMIT 1",
            schedule_id, checklist_id, asset_id)
        if not latest:
            return _json({"ok": False, "msg": "No completed work order to delete for this task."})
        d = latest["done_at"].astimezone(IST).date()
        pk = _period_key(cname, d)
        date_iso = d.isoformat()
    else:
        if not (checklist_id and pk):
            raise HTTPException(400, "Missing checklist_id/period_key")
        c = await db.fetchrow("SELECT name FROM maint_checklists WHERE id=$1", checklist_id)
        cname = c["name"] if c else ""
        asset_id = await db.fetchval(
            "SELECT asset_id FROM maint_logs WHERE checklist_id=$1 AND done_at IS NOT NULL "
            "ORDER BY done_at DESC LIMIT 1", checklist_id)
        if asset_id is None:
            asset_id = await db.fetchval(
                "SELECT asset_id FROM maint_schedules WHERE checklist_id=$1 LIMIT 1", checklist_id)

    n = await _delete_pm_workorder(checklist_id, pk, cname, asset_id)
    if token:
        cat = "Quarterly PM" if "quarter" in (cname or "").lower() else "Daily PM"
        item = await _locate_report_item(token, code, name, cat, date_iso, bd=0)
        if item:
            pdf_deleted = await graph.delete_item(token, item["id"])
    return _json({"ok": True, "deleted_logs": n, "pdf_deleted": pdf_deleted})



@router.post("/monitor/mark-absent")
async def monitor_mark_absent(body: dict, emp: dict = Depends(require_employee)):
    """Admin-only (Daily PM only): mark an assignee absent for a work order. If they
    were the only assignee left, the day's report is skipped and the schedule advanced
    so it won't show overdue; otherwise the report is saved now, excluding the absent
    person's checkpoints."""
    await ensure_tables()
    await _ensure_absent_table()
    if not emp.get("is_admin"):
        raise HTTPException(403, "Admins only")
    try:
        cid = int(body.get("checklist_id"))
        aid = int(body.get("emp_id"))
    except (TypeError, ValueError):
        raise HTTPException(400, "Bad checklist/emp id")
    pk = (body.get("period_key") or "").strip()
    if not pk:
        raise HTTPException(400, "Missing period")
    cname = await db.fetchval("SELECT name FROM maint_checklists WHERE id=$1", cid) or ""
    if "quarter" in cname.lower():
        raise HTTPException(400, "Mark absent is only for Daily PM.")
    await db.execute(
        "INSERT INTO maint_wo_absent (checklist_id, period_key, emp_id, marked_by) "
        "VALUES ($1,$2,$3,$4) ON CONFLICT (checklist_id, period_key, emp_id) DO NOTHING",
        cid, pk, aid, emp.get("name") or emp.get("emp_no"))
    assignee_ids = [r["assigned_emp_id"] for r in await db.fetch(
        "SELECT DISTINCT assigned_emp_id FROM maint_checkpoints "
        "WHERE checklist_id=$1 AND assigned_emp_id IS NOT NULL", cid)]
    absent = await _absent_emp_ids(cid, pk)
    remaining = [x for x in assignee_ids if x not in absent]
    sch = await db.fetchrow(
        "SELECT s.id, s.asset_id, a.code AS acode, a.name AS aname, a.location AS loc "
        "FROM maint_schedules s LEFT JOIN maint_assets a ON a.id=s.asset_id "
        "WHERE s.checklist_id=$1 ORDER BY s.id LIMIT 1", cid)
    try:
        pdate = date.fromisoformat(pk[:10])
    except ValueError:
        pdate = _today()
    if not remaining:
        if sch:
            await db.execute("UPDATE maint_schedules SET last_done=$1 WHERE id=$2", pdate, sch["id"])
        return _json({"ok": True, "action": "skipped",
                      "msg": "Only assignee was absent — no report saved; the day is marked done "
                             "so it won't show overdue."})
    when = datetime(pdate.year, pdate.month, pdate.day, 12, 0, tzinfo=IST)
    p = await _file_pm_report(
        asset_code=(sch["acode"] if sch else ""), asset_name=(sch["aname"] if sch else ""),
        checklist=cname, doc_no="", done_by="(admin — absentee excluded)",
        results=[], checklist_id=cid, location=(sch["loc"] if sch else ""), when=when)
    return _json({"ok": True, "action": "saved",
                  "msg": ("Marked absent. Report saved excluding the absent person's checkpoints."
                          if p else "Marked absent; the report will file when OneDrive is reachable.")})


@router.post("/monitor/mark-done")
async def monitor_mark_done(body: dict, emp: dict = Depends(require_employee)):
    """Admin-only: mark a pending assignee's checkpoints for a work order as DONE with
    result OK (for when the work was completed on the floor but not logged in the app).
    Works for Daily and Quarterly PM. When this completes every assigned, non-absent
    checkpoint for the period, the PM schedule is advanced and the work-order PDF is
    generated (all points OK), exactly as a normal My-Checklist submit would."""
    await ensure_tables()
    await _ensure_absent_table()
    if not emp.get("is_admin"):
        raise HTTPException(403, "Admins only")
    try:
        cid = int(body.get("checklist_id"))
        aid = int(body.get("emp_id"))   # 0 = the "Unassigned" checkpoints row
    except (TypeError, ValueError):
        raise HTTPException(400, "Bad checklist/emp id")
    pk = (body.get("period_key") or "").strip()
    if not pk:
        raise HTTPException(400, "Missing period")

    # period date (noon IST) for the done_at stamp
    try:
        pdate = _dt_date.fromisoformat(pk[:10])
    except ValueError:
        pdate = _today()
    done_at = datetime(pdate.year, pdate.month, pdate.day, 12, 0, tzinfo=IST)

    # The selected assignee's checkpoints (or, for emp_id 0, the unassigned ones) on this
    # checklist that are not yet recorded this period.
    if aid:
        their = await db.fetch(
            "SELECT id FROM maint_checkpoints WHERE checklist_id=$1 AND assigned_emp_id=$2", cid, aid)
    else:
        their = await db.fetch(
            "SELECT id FROM maint_checkpoints WHERE checklist_id=$1 AND assigned_emp_id IS NULL", cid)
    if not their:
        raise HTTPException(404, "No checkpoints for that assignee")
    their_ids = [r["id"] for r in their]
    done_ids = {d["checkpoint_id"] for d in await db.fetch(
        "SELECT checkpoint_id FROM maint_checkpoint_done "
        "WHERE period_key=$1 AND checkpoint_id = ANY($2::int[])", pk, their_ids)}
    emp_val = aid or None   # store NULL for unassigned checkpoints
    marked = 0
    for pid in their_ids:
        if pid in done_ids:
            continue
        await db.execute(
            "INSERT INTO maint_checkpoint_done (checkpoint_id,emp_id,period_key,result,note,done_at) "
            "VALUES ($1,$2,$3,'OK','',$4) ON CONFLICT (checkpoint_id,period_key) "
            "DO UPDATE SET emp_id=$2, result='OK', note='', done_at=$4",
            pid, emp_val, pk, done_at)
        marked += 1

    # Is the WHOLE period now complete (every assigned, non-absent checkpoint has a result)?
    all_cps = await db.fetch(
        "SELECT id, assigned_emp_id FROM maint_checkpoints WHERE checklist_id=$1", cid)
    absent = await _absent_emp_ids(cid, pk)
    required = [r["id"] for r in all_cps
                if r["assigned_emp_id"] and r["assigned_emp_id"] not in absent]
    all_done = False
    if required:
        done_now = {d["checkpoint_id"] for d in await db.fetch(
            "SELECT checkpoint_id FROM maint_checkpoint_done "
            "WHERE period_key=$1 AND checkpoint_id = ANY($2::int[])",
            pk, [r["id"] for r in all_cps])}
        all_done = all(cp_id in done_now for cp_id in required)

    cname = await db.fetchval("SELECT name FROM maint_checklists WHERE id=$1", cid) or ""
    sch = await db.fetchrow(
        "SELECT s.id, s.asset_id, a.code AS acode, a.name AS aname, a.location AS loc, "
        "a.running_hours AS rh FROM maint_schedules s LEFT JOIN maint_assets a ON a.id=s.asset_id "
        "WHERE s.checklist_id=$1 ORDER BY s.id LIMIT 1", cid)

    if all_done:
        # Every assigned, non-absent checkpoint is done. Sweep any UNASSIGNED pending
        # checkpoints to OK too, so the work-order PDF comes out fully complete (no
        # orphan "pending" row) rather than "In progress — 1 pending".
        orphan = [r["id"] for r in all_cps if not r["assigned_emp_id"]]
        if orphan:
            for pid in await db.fetch(
                    "SELECT id FROM maint_checkpoints WHERE id = ANY($1::int[])", orphan):
                await db.execute(
                    "INSERT INTO maint_checkpoint_done (checkpoint_id,emp_id,period_key,result,note,done_at) "
                    "VALUES ($1,NULL,$2,'OK','',$3) ON CONFLICT (checkpoint_id,period_key) DO NOTHING",
                    pid["id"], pk, done_at)
        # advance every schedule on this checklist so it stops showing due/overdue
        for sr in await db.fetch(
                "SELECT id, asset_id FROM maint_schedules "
                "WHERE checklist_id=$1 AND COALESCE(active,true)=true", cid):
            ah = await db.fetchval("SELECT running_hours FROM maint_assets WHERE id=$1", sr["asset_id"])
            await db.execute("UPDATE maint_schedules SET last_done=$1, last_done_hours=$2 WHERE id=$3",
                             pdate, ah, sr["id"])
        # generate the work-order PDF (rebuilt from maint_checkpoint_done -> all OK)
        done_by = (await db.fetchval("SELECT name FROM employees WHERE id=$1", aid)
                   or "(admin — marked done)")
        p = await _file_pm_report(
            asset_code=(sch["acode"] if sch else ""), asset_name=(sch["aname"] if sch else ""),
            checklist=cname, doc_no="", done_by=done_by,
            results=[], checklist_id=cid, location=(sch["loc"] if sch else ""), when=done_at)
        return _json({"ok": True, "all_done": True, "marked": marked,
                      "msg": ("Marked done. All checks complete — work-order PDF generated (all OK)."
                              if p else "Marked done. All checks complete; the PDF will file when "
                                        "OneDrive is reachable.")})
    return _json({"ok": True, "all_done": False, "marked": marked,
                  "msg": "Marked done for this person. Still waiting on other assignees before the PDF."})


@router.post("/monitor/skip-quarter")
async def monitor_skip_quarter(body: dict, emp: dict = Depends(require_employee)):
    """Admin-only (Quarterly PM): skip a quarterly work order for THIS period. It is
    removed from the Monitor, the board and the reminders for this quarter (no PDF, not
    marked done). The schedule is rolled to its next quarter, so the machine's NEXT
    quarterly PM is scheduled as normal."""
    await ensure_tables()
    await _ensure_skip_table()
    if not emp.get("is_admin"):
        raise HTTPException(403, "Admins only")
    try:
        cid = int(body.get("checklist_id"))
    except (TypeError, ValueError):
        raise HTTPException(400, "Bad checklist id")
    pk = (body.get("period_key") or "").strip()
    if not pk:
        raise HTTPException(400, "Missing period")
    cname = await db.fetchval("SELECT name FROM maint_checklists WHERE id=$1", cid) or ""
    if "quarter" not in cname.lower():
        raise HTTPException(400, "Skip is only for Quarterly PM.")
    await db.execute(
        "INSERT INTO maint_wo_skip (checklist_id, period_key, skipped_by) VALUES ($1,$2,$3) "
        "ON CONFLICT (checklist_id, period_key) DO NOTHING",
        cid, pk, emp.get("name") or emp.get("emp_no"))
    # roll each schedule on this checklist to its next quarter so the date model also
    # advances (last_done set to this quarter's scheduled date).
    today_d = _today()
    for sr in await db.fetch(
            "SELECT * FROM maint_schedules WHERE checklist_id=$1 AND COALESCE(active,true)=true", cid):
        cur_due = _q_cur_due(dict(sr), today_d) or today_d
        await db.execute("UPDATE maint_schedules SET last_done=$1 WHERE id=$2", cur_due, sr["id"])
    return _json({"ok": True, "msg": "Skipped this quarter. It's cleared from the Monitor; "
                                     "the next quarterly PM stays scheduled."})


@router.get("/admin/backfill-checkpoint-done")
async def backfill_checkpoint_done(token: str = Query(default=""), limit: int = Query(default=50),
                                   skip: int = Query(default=0),
                                   emp: dict = Depends(current_employee)):
    """Populate maint_checkpoint_done from historical board 'Complete' logs (whose
    per-checkpoint results were only kept in the log JSON). Fixes the Monitor's
    Done/Pending and lets the combined PDFs regenerate correctly. Idempotent (never
    overwrites a My-Checklist entry), paginated — follow next_url until remaining is 0."""
    await ensure_tables()
    if not ((emp and emp.get("is_admin")) or (AGENT_TOKEN and token == AGENT_TOKEN)):
        raise HTTPException(403, "Admins only")
    limit = max(1, min(int(limit or 1000), 5000))
    skip = max(0, int(skip or 0))
    logs = await db.fetch(
        "SELECT l.id, l.done_by_emp_id, l.done_at, l.results, c.name AS cname "
        "FROM maint_logs l LEFT JOIN maint_checklists c ON c.id=l.checklist_id "
        "WHERE l.checklist_id IS NOT NULL AND l.done_at IS NOT NULL ORDER BY l.id")
    total = len(logs)
    # collect all (checkpoint_id, emp_id, period_key, result, note) for this page
    pending = []
    for l in logs[skip:skip + limit]:
        res = l["results"]
        if isinstance(res, str):
            try:
                res = json.loads(res or "[]")
            except Exception:
                res = []
        if not isinstance(res, list) or not res:
            continue
        dd = l["done_at"].astimezone(IST).date() if isinstance(l["done_at"], datetime) else None
        pk = _period_key(l["cname"] or "", dd)
        for r in res:
            if not isinstance(r, dict):
                continue
            cpid = r.get("cp_id")
            if not cpid:
                continue
            try:
                cpid = int(cpid)
            except (TypeError, ValueError):
                continue
            resv = (str(r.get("value") or "").strip()
                    or ("OK" if r.get("ok", True) else "Not OK"))
            note = (r.get("remark") or "").strip()
            pending.append((cpid, l["done_by_emp_id"], pk, resv, note))
    # bulk insert in chunks — one round-trip per chunk instead of one per row
    written = 0
    CH = 300
    for i in range(0, len(pending), CH):
        chunk = pending[i:i + CH]
        vals, params = [], []
        for j, row in enumerate(chunk):
            b = j * 5
            vals.append(f"(${b+1},${b+2},${b+3},${b+4},${b+5})")
            params.extend(row)
        try:
            await db.execute(
                "INSERT INTO maint_checkpoint_done (checkpoint_id,emp_id,period_key,result,note) "
                "VALUES " + ",".join(vals) +
                " ON CONFLICT (checkpoint_id,period_key) DO NOTHING", *params)
            written += len(chunk)
        except Exception as e:
            print("[maint] cp-done bulk chunk skip:", e)
    done_through = min(skip + limit, total)
    remaining = max(0, total - done_through)
    out = {"ok": True, "logs_total": total, "written_this_page": written,
           "done_through": done_through, "remaining": remaining}
    if remaining:
        out["next_url"] = (f"https://qms.bharatsteels.in/maint/admin/backfill-checkpoint-done"
                           f"?token={token}&limit={limit}&skip={done_through}")
    else:
        out["note"] = ("Per-checkpoint state filled from the logs. The Monitor now shows the "
                       "right status; re-run backfill-reports if you also want the PDFs rebuilt.")
    return _json(out)


@router.get("/version")
async def maint_version():
    """Read-only build check — no login, no side effects. Open
    qms.bharatsteels.in/maint/version after deploying: if `build` is not the latest,
    Vercel is still serving an older build and the deploy hasn't landed."""
    return _json({"build": MAINT_BUILD, "schema": SCHEMA_VER})


@router.get("/reminders/preview")
async def reminders_preview(emp: dict = Depends(require_employee)):
    """Staff-only preview of what the reminder run would send right now."""
    return _json(await _run_reminders(dry=True))


@router.get("/overdue-recipients")
async def overdue_recipients_get(emp: dict = Depends(require_employee)):
    """The employee ids chosen to receive the overdue WhatsApp digest."""
    await ensure_tables()
    raw = await db.fetchval("SELECT mval FROM maint_meta WHERE mkey='overdue_recipients'")
    ids = [x for x in (raw or "").split(",") if x.strip()]
    return _json({"emp_ids": ids})


@router.post("/overdue-recipients")
async def overdue_recipients_set(body: dict, emp: dict = Depends(require_employee)):
    """Save which employee(s) get the overdue digest."""
    await ensure_tables()
    ids = body.get("emp_ids") or []
    clean = ",".join(str(int(x)) for x in ids if str(x).strip().isdigit())
    await db.execute(
        "INSERT INTO maint_meta (mkey,mval) VALUES ('overdue_recipients',$1) "
        "ON CONFLICT (mkey) DO UPDATE SET mval=$1", clean)
    return {"ok": True, "emp_ids": [x for x in clean.split(",") if x]}


@router.get("/admin/reseed")
async def admin_reseed(token: str = Query(default="")):
    """Protected one-time rebuild of EVERY machine's checklists + per-checkpoint
    assignees from the shipped workbook seed. Open once in a browser:
       /maint/admin/reseed?token=<AGENT_TOKEN>
    Fast (batched) so it finishes inside the request limit."""
    if not AGENT_TOKEN or token != AGENT_TOKEN:
        raise HTTPException(401, "bad token")
    await ensure_tables()
    res = await _reseed_all_fast()
    await _ensure_daily_plans()
    await _ensure_new_machine_quarterly()
    await _ensure_quarterly_links()
    await _ensure_schedule_assignees()
    try:
        await db.execute(
            "INSERT INTO maint_meta (mkey,mval) VALUES ($1,'1') ON CONFLICT (mkey) DO UPDATE SET mval='1'",
            FULL_RESEED_KEY)
    except Exception:
        pass
    return _json({"ok": True, **res})
