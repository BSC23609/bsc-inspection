"""Maintenance (CMMS) module — in-house replacement for Opmaint, inside the QMS app.

Covers: an asset register, per-asset checklists (each checkpoint tagged with a
category — Hydraulic / Mechanical / Electrical), preventive-maintenance schedules
triggered by date interval AND/OR running-hours, category→staff assignment
(reusing the employees login, with Electrical varying by location), completion
logging (machine history), and WhatsApp reminders (due-today to the assignee,
overdue escalation to a supervisor) via WATI.

All tables are Neon and self-create on first use.
"""
import json
import re
import base64
import calendar
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
from typing import Optional
from fastapi import APIRouter, Depends, Query, HTTPException
from fastapi.responses import JSONResponse

from .. import db, wati, graph
from .. import config as _cfg
from ..auth import require_employee

# Read config defensively so a slightly older app/config.py (missing one of the
# newer maintenance vars) can never crash the whole app at import time.
WATI_MAINT_DUE_TEMPLATE       = getattr(_cfg, "WATI_MAINT_DUE_TEMPLATE", "")
WATI_MAINT_OVERDUE_TEMPLATE   = getattr(_cfg, "WATI_MAINT_OVERDUE_TEMPLATE", "")
WATI_MAINT_BREAKDOWN_TEMPLATE = getattr(_cfg, "WATI_MAINT_BREAKDOWN_TEMPLATE", "bsc_breakdown_alert")
MAINT_SUPERVISOR_MOBILE       = getattr(_cfg, "MAINT_SUPERVISOR_MOBILE", "")
AGENT_TOKEN                   = getattr(_cfg, "AGENT_TOKEN", "BSC@123")
ONEDRIVE_BASE                 = getattr(_cfg, "ONEDRIVE_BASE", "BSC Inspections")

router = APIRouter(prefix="/maint", tags=["maintenance"])
IST = ZoneInfo("Asia/Kolkata")
CATEGORIES = ["Hydraulic", "Mechanical", "Electrical"]
_ensured = False

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
        for col, typ in (("btype", "TEXT"), ("photos", "JSONB"), ("assignees", "JSONB")):
            await db.execute(f"ALTER TABLE maint_breakdowns ADD COLUMN IF NOT EXISTS {col} {typ}")
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
    except Exception as e:
        print("[maint] breakdown schema step skipped:", e)
    await db.execute("CREATE INDEX IF NOT EXISTS ix_m_cp ON maint_checkpoints (checklist_id, seq)")
    await db.execute("CREATE INDEX IF NOT EXISTS ix_m_log ON maint_logs (asset_id, done_at DESC)")
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


def _today() -> date:
    return datetime.now(IST).date()


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


def _is_offday(d):
    return d.weekday() == 6 or d.isoformat() in HOLIDAYS   # Sunday (6) or a holiday


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
    await db.execute("UPDATE maint_assets SET running_hours=$1, hours_updated_at=now() WHERE id=$2", hrs, aid)
    return {"ok": True}


@router.post("/assets/toggle")
async def assets_toggle(body: dict, emp: dict = Depends(require_employee)):
    await ensure_tables()
    await db.execute("UPDATE maint_assets SET active=$1 WHERE id=$2", bool(body.get("active")), body.get("id"))
    return {"ok": True}


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
    # replace checkpoints
    await db.execute("DELETE FROM maint_checkpoints WHERE checklist_id=$1", cid)
    for i, p in enumerate(body.get("checkpoints") or []):
        txt = (p.get("text") or "").strip()
        if not txt:
            continue
        await db.execute(
            "INSERT INTO maint_checkpoints (checklist_id,seq,text,category,input_type,unit,spec) "
            "VALUES ($1,$2,$3,$4,$5,$6,$7)",
            cid, i + 1, txt, (p.get("category") or "").strip(),
            (p.get("input_type") or "check").strip(), (p.get("unit") or "").strip(),
            (p.get("spec") or "").strip())
    return {"ok": True, "id": cid}


@router.post("/checklist/delete")
async def checklist_delete(body: dict, emp: dict = Depends(require_employee)):
    await ensure_tables()
    cid = body.get("id")
    await db.execute("DELETE FROM maint_checkpoints WHERE checklist_id=$1", cid)
    await db.execute("DELETE FROM maint_checklists WHERE id=$1", cid)
    return {"ok": True}


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
    eid = body.get("emp_id")
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
    await db.execute("DELETE FROM maint_leave WHERE id=$1", body.get("id"))
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
            "WHERE b.status='open' ORDER BY b.raised_at DESC")
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


async def _send_breakdown_alert(bd: dict):
    """Fire bsc_breakdown_alert to the assignee (if any) + every supervisor."""
    label = (str(bd.get("asset_code") or "") + " " + str(bd.get("asset_name") or "")).strip() or "—"
    detail = (bd.get("problem") or "—").strip() or "—"
    btype = _norm_type(bd.get("btype"))
    problem = (f"{btype}: {detail}" if btype else detail)
    by = (bd.get("reported_by") or "—").strip() or "—"
    tm = datetime.now(IST).strftime("%d-%m-%Y %H:%M")
    params = [label, problem, by, tm]   # matches the approved template's {{1}}–{{4}} exactly
    targets, seen = [], set()
    # all assigned maintenance users (multi) get the alert
    aids = bd.get("assignees") or ([bd["assignee_emp_id"]] if bd.get("assignee_emp_id") else [])
    emap = await _emp_map()
    for aid in aids:
        mob = (emap.get(aid) or {}).get("mobile") or (bd.get("assignee_mobile") if aid == bd.get("assignee_emp_id") else "")
        m = wati.normalize_number(mob)
        if m and m not in seen:
            targets.append(m); seen.add(m)
    for sup in _supervisor_mobiles():
        m = wati.normalize_number(sup)
        if m and m not in seen:
            targets.append(m); seen.add(m)
    results = []
    for m in targets:
        ok, detail = await wati.send_template(WATI_MAINT_BREAKDOWN_TEMPLATE, m, params)
        results.append({"to": m, "result": "sent" if ok else f"failed: {detail}"})
    return results


@router.get("/breakdowns/list")
async def breakdowns_list(emp: dict = Depends(require_employee)):
    await ensure_tables()
    rows = await db.fetch(
        "SELECT b.id, b.asset_id, b.btype, b.problem, b.reported_by, b.assignee_emp_id, b.assignees, "
        "b.status, b.raised_at, b.closed_at, b.closed_by, b.remarks, b.photos, "
        "a.code AS asset_code, a.name AS asset_name, e.name AS assignee_name "
        "FROM maint_breakdowns b LEFT JOIN maint_assets a ON a.id=b.asset_id "
        "LEFT JOIN employees e ON e.id=b.assignee_emp_id "
        "WHERE b.status='open' OR b.closed_at > now() - interval '7 days' "
        "ORDER BY (b.status='open') DESC, b.raised_at DESC")
    return _json([dict(r) for r in rows])


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
    params = ["TEST MACHINE", "Other: WATI test message", "Diagnostics",
              datetime.now(IST).strftime("%d-%m-%Y %H:%M")]
    ok, detail = await wati.send_template(WATI_MAINT_BREAKDOWN_TEMPLATE, num, params)
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


@router.post("/breakdown/raise")
async def breakdown_raise(body: dict, emp: dict = Depends(require_employee)):
    await ensure_tables()
    try:
        aid = int(body.get("asset_id"))          # column is INT — coerce the form's string
    except (TypeError, ValueError):
        aid = None
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
        "assignee_emp_id,assignees,status,remarks,raised_at) "
        "VALUES ($1,$2,$3,$4,$5,$6,$7,'open',$8,COALESCE($9,now())) RETURNING id",
        aid, btype, problem, reporter, emp.get("id"), primary, aids, remarks, raised_at)
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
        "SELECT b.*, a.code AS asset_code, a.name AS asset_name, e.mobile AS assignee_mobile "
        "FROM maint_breakdowns b LEFT JOIN maint_assets a ON a.id=b.asset_id "
        "LEFT JOIN employees e ON e.id=b.assignee_emp_id WHERE b.id=$1", bid)
    alert = []
    try:
        alert = await _send_breakdown_alert(dict(bd))
    except Exception as e:
        print("[maint] breakdown alert skipped:", e)
    return {"ok": True, "id": bid, "photos": len(paths), "alert": alert}


@router.post("/breakdown/close")
async def breakdown_close(body: dict, emp: dict = Depends(require_employee)):
    await ensure_tables()
    if not body.get("id"):
        raise HTTPException(400, "Missing id")
    note = (body.get("remarks") or "").strip()
    # keep the original raise remarks; append the close note rather than overwrite
    await db.execute(
        "UPDATE maint_breakdowns SET status='closed', closed_at=now(), closed_by=$2, "
        "remarks = CASE WHEN $3='' THEN remarks "
        "WHEN COALESCE(remarks,'')='' THEN $3 ELSE remarks || ' | closed: ' || $3 END "
        "WHERE id=$1", body.get("id"),
        emp.get("name") or emp.get("emp_no") or "staff", note)
    return {"ok": True}


def _require_admin(emp: dict):
    if not emp.get("is_admin"):
        raise HTTPException(403, "Admin only")


@router.post("/breakdown/delete")
async def breakdown_delete(body: dict, emp: dict = Depends(require_employee)):
    """Delete ONE raised ticket (admin only)."""
    await ensure_tables()
    _require_admin(emp)
    try:
        bid = int(body.get("id"))
    except (TypeError, ValueError):
        raise HTTPException(400, "Missing id")
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
    await db.execute(
        "INSERT INTO maint_logs (asset_id,schedule_id,checklist_id,done_by,done_by_emp_id,"
        "running_hours,status,results,remarks,next_due) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)",
        s["asset_id"], sid, s.get("checklist_id"),
        emp.get("name") or emp.get("emp_no"), emp.get("id"),
        hrs, status, json.dumps(results), (body.get("remarks") or "").strip(), next_due)
    await db.execute("UPDATE maint_schedules SET last_done=$1, last_done_hours=$2 WHERE id=$3",
                     today, hrs, sid)
    if hrs is not None:
        await db.execute("UPDATE maint_assets SET running_hours=$1, hours_updated_at=now() WHERE id=$2",
                         hrs, s["asset_id"])
    return {"ok": True, "status": status, "next_due": next_due.isoformat() if next_due else None}


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
@router.get("/board")
async def board(emp: dict = Depends(require_employee)):
    await ensure_tables()
    await _refresh_holidays()
    today = _today()
    due, overdue, upcoming = [], [], []
    for s in await _schedule_rows():
        if not s.get("active"):
            continue
        info = compute_due(s, s.get("asset_hours"), today)
        s.update(info)
        if info["overdue"]:
            overdue.append(s)
        elif info["due"]:
            due.append(s)
        elif info["next_due"]:
            nd = _d(info["next_due"])
            if nd and nd <= today + timedelta(days=7):
                upcoming.append(s)
    recent = await db.fetch(
        "SELECT l.*, a.name AS asset_name, a.code AS asset_code, s.title AS sched_title "
        "FROM maint_logs l LEFT JOIN maint_assets a ON a.id=l.asset_id "
        "LEFT JOIN maint_schedules s ON s.id=l.schedule_id ORDER BY l.done_at DESC LIMIT 12")
    return _json({"today": today.isoformat(), "overdue": overdue, "due": due,
                  "upcoming": upcoming, "recent": [dict(r) for r in recent]})


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
    """Numbered list, one per line (1  text / 2  text …). WhatsApp may strip the
    line breaks depending on the provider; the numbers keep it readable either way."""
    out, used = [], 0
    for i, it in enumerate(items):
        line = f"{i + 1}  {it}"
        if used + len(line) + 1 > cap:
            out.append(f"…and {len(items) - i} more"); break
        out.append(line); used += len(line) + 1
    return "\n".join(out) if out else "—"


async def _run_reminders(dry: bool = False, kind: str = "all") -> dict:
    await ensure_tables()
    await _refresh_holidays()
    today = _today()
    on_leave = await _on_leave_ids(today)
    tdisp = today.strftime("%d-%m-%Y")

    # each person's morning digest keeps preventive and breakdown lists separate
    due_by_assignee = {}   # emp_id -> {"name","mobile","pm":[],"bd":[]}
    sup_pm = []            # preventive: overdue + due-on-leave cover + unassigned -> supervisors
    sup_bd = []            # breakdown: unassigned / on-leave open breakdowns -> supervisors

    def _bucket(aid, name, mobile, kind, text):
        g = due_by_assignee.setdefault(aid, {"name": name, "mobile": mobile, "pm": [], "bd": []})
        g[kind].append(text)

    # ---- preventive maintenance: each person's PM list = pending (overdue + due) ----
    for s in await _schedule_rows():
        if not s.get("active"):
            continue
        info = compute_due(s, s.get("asset_hours"), today)
        if not info["due"] and not info["overdue"]:
            continue
        label = _asset_label(s)
        task = s.get("title") or "PM"
        assignee = s.get("assignee_name") or "unassigned"
        aid = s.get("assignee_emp_id")
        mobile = s.get("assignee_mobile")
        if info["overdue"]:
            if aid and aid in on_leave:
                continue  # assignee on leave — don't flag their overdue work
            sup_pm.append(f"{label} - {task} - {assignee}")   # 3pm escalation
            if aid and mobile:
                _bucket(aid, assignee, mobile, "pm", f"{label} - {task} (overdue)")  # 9am
        else:  # due today
            if aid and aid in on_leave:
                continue  # on leave — not flagged to the supervisor's overdue alert
            elif aid and mobile:
                _bucket(aid, assignee, mobile, "pm", f"{label} - {task}")
            else:
                sup_pm.append(f"{label} - {task} - unassigned")

    # ---- open breakdowns: to EACH assigned maintenance user; else to supervisors ----
    emap = await _emp_map()
    for b in await _open_breakdowns():
        label = (str(b.get("asset_code") or "") + " " + str(b.get("asset_name") or "")).strip() or "—"
        prob = (b.get("problem") or "").strip() or "—"
        aids = b.get("assignees") or ([b["assignee_emp_id"]] if b.get("assignee_emp_id") else [])
        if not aids:
            sup_bd.append(f"{label} - {prob} - unassigned")
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
                sup_bd.append(f"{label} - {prob} - {who}")

    sent, skipped = [], []

    async def _dispatch(tmpl, mobile, params, meta):
        rec = dict(meta); rec["to"] = mobile
        if not mobile:
            rec["result"] = "skipped: no mobile"; skipped.append(rec); return
        if not tmpl:
            rec["result"] = "skipped: template not set"; skipped.append(rec); return
        if dry:
            rec["result"] = "dry-run"; sent.append(rec); return
        ok, detail = await wati.send_template(tmpl, mobile, params)
        rec["result"] = "sent" if ok else f"failed: {detail}"
        (sent if ok else skipped).append(rec)

    # due digests (morning run) — preventive and breakdown kept in separate variables
    if kind in ("due", "all"):
        for aid, g in due_by_assignee.items():
            pm_list = _join_items(g["pm"])
            bd_list = _join_items(g["bd"])
            # body vars: 1 name, 2 date, 3 preventive list, 4 breakdown list,
            # then the dynamic-URL button suffix (WATI param "5"): .../{{1}} -> "maint"
            params = [g["name"], tdisp, pm_list, bd_list, "maint"]
            await _dispatch(WATI_MAINT_DUE_TEMPLATE, g["mobile"], params,
                            {"kind": "due-digest", "assignee": g["name"],
                             "preventive": len(g["pm"]), "breakdown": len(g["bd"])})

    # overdue/cover digest to every supervisor (afternoon run) — preventive + breakdown apart
    if kind in ("overdue", "all") and (sup_pm or sup_bd):
        pm_num = _numbered(sup_pm)
        bd_num = _numbered(sup_bd)
        # body vars: 1 date, 2 preventive list, 3 breakdown list
        params = [tdisp, pm_num, bd_num]
        sups = _supervisor_mobiles()
        for sup in sups:
            await _dispatch(WATI_MAINT_OVERDUE_TEMPLATE, sup, params,
                            {"kind": "overdue-digest", "preventive": len(sup_pm),
                             "breakdown": len(sup_bd)})
        if not sups:
            skipped.append({"kind": "overdue-digest",
                            "preventive": len(sup_pm), "breakdown": len(sup_bd),
                            "result": "skipped: no supervisor number (set MAINT_SUPERVISOR_MOBILE)"})
    return {"today": today.isoformat(), "sent": sent, "skipped": skipped,
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
    return _json(await _run_reminders(dry=bool(dry), kind=k))


@router.get("/reminders/preview")
async def reminders_preview(emp: dict = Depends(require_employee)):
    """Staff-only preview of what the reminder run would send right now."""
    return _json(await _run_reminders(dry=True))
