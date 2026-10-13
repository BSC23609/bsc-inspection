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
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
from typing import Optional
from fastapi import APIRouter, Depends, Query, HTTPException
from fastapi.responses import JSONResponse

from .. import db, wati
from ..auth import require_employee
from ..config import (WATI_MAINT_DUE_TEMPLATE, WATI_MAINT_OVERDUE_TEMPLATE,
                      MAINT_SUPERVISOR_MOBILE, AGENT_TOKEN)

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
    await db.execute("""CREATE TABLE IF NOT EXISTS maint_logs (
        id SERIAL PRIMARY KEY, asset_id INT, schedule_id INT, checklist_id INT,
        done_by TEXT, done_by_emp_id INT, done_at TIMESTAMPTZ DEFAULT now(),
        running_hours NUMERIC, status TEXT, results JSONB, remarks TEXT, next_due DATE)""")
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
                await db.execute(
                    "INSERT INTO maint_schedules (asset_id,title,checklist_id,freq_days,category,location,"
                    "start_date,active) VALUES ($1,$2,$3,$4,'','',$5,true)",
                    aid, f"{kind} PM", cid, 1 if kind.lower() == "daily" else 90, today)
    except Exception as e:
        print("[maint] checklist seed skipped:", e)
    _ensured = True


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


# ---------- due computation ----------
def compute_due(sch: dict, asset_hours, today: date):
    base = sch.get("last_done") or sch.get("start_date")
    freq = sch.get("freq_days")
    next_due = None
    if freq:
        next_due = (base + timedelta(days=int(freq))) if base else today
    date_due = bool(next_due and next_due <= today)
    hours_due = False
    hrs_rem = None
    if sch.get("hours_interval"):
        last_h = float(sch.get("last_done_hours") or 0)
        target = last_h + float(sch["hours_interval"])
        cur = float(asset_hours or 0)
        hours_due = cur >= target
        hrs_rem = round(target - cur, 1)
    due = bool(date_due or hours_due)
    overdue = bool(next_due and next_due < today)
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
    today = _today()
    out = []
    for s in await _schedule_rows():
        info = compute_due(s, s.get("asset_hours"), today)
        s.update(info)
        out.append(s)
    return _json(out)


@router.post("/schedule/save")
async def schedule_save(body: dict, emp: dict = Depends(require_employee)):
    await ensure_tables()
    asset_id = body.get("asset_id")
    title = (body.get("title") or "").strip()
    if not asset_id:
        raise HTTPException(400, "Choose an asset")
    if not title:
        raise HTTPException(400, "Schedule title is required")
    category = (body.get("category") or "").strip()
    location = (body.get("location") or "").strip()
    assignee = body.get("assignee_emp_id") or None
    vals = (asset_id, title, body.get("checklist_id") or None,
            _num(body.get("freq_days")), _num(body.get("hours_interval")),
            category, location, assignee, _d(body.get("start_date")),
            _d(body.get("last_done")), _num(body.get("last_done_hours")))
    sid = body.get("id")
    if sid:
        await db.execute(
            "UPDATE maint_schedules SET asset_id=$1,title=$2,checklist_id=$3,freq_days=$4,"
            "hours_interval=$5,category=$6,location=$7,assignee_emp_id=$8,start_date=$9,"
            "last_done=$10,last_done_hours=$11 WHERE id=$12", *vals, sid)
        return {"ok": True, "id": sid}
    sid = await db.fetchval(
        "INSERT INTO maint_schedules (asset_id,title,checklist_id,freq_days,hours_interval,"
        "category,location,assignee_emp_id,start_date,last_done,last_done_hours) "
        "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11) RETURNING id", *vals)
    return {"ok": True, "id": sid}


@router.post("/schedule/toggle")
async def schedule_toggle(body: dict, emp: dict = Depends(require_employee)):
    await ensure_tables()
    await db.execute("UPDATE maint_schedules SET active=$1 WHERE id=$2", bool(body.get("active")), body.get("id"))
    return {"ok": True}


@router.post("/schedule/delete")
async def schedule_delete(body: dict, emp: dict = Depends(require_employee)):
    await ensure_tables()
    await db.execute("DELETE FROM maint_schedules WHERE id=$1", body.get("id"))
    return {"ok": True}


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
    next_due = None
    if s.get("freq_days"):
        next_due = today + timedelta(days=int(s["freq_days"]))
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
async def _run_reminders(dry: bool = False) -> dict:
    await ensure_tables()
    today = _today()
    sent, skipped = [], []
    for s in await _schedule_rows():
        if not s.get("active"):
            continue
        info = compute_due(s, s.get("asset_hours"), today)
        if not info["due"] and not info["overdue"]:
            continue
        assignee_mobile = s.get("assignee_mobile") or ""
        asset = f'{s.get("asset_name") or ""} ({s.get("asset_code") or ""})'.strip()
        when = "OVERDUE" if info["overdue"] else "due today"
        if info["overdue"]:
            tmpl, target = WATI_MAINT_OVERDUE_TEMPLATE, (MAINT_SUPERVISOR_MOBILE or assignee_mobile)
            params = [asset, s.get("title") or "", info.get("next_due") or "", s.get("assignee_name") or "unassigned"]
        else:
            tmpl, target = WATI_MAINT_DUE_TEMPLATE, assignee_mobile
            params = [s.get("assignee_name") or "", asset, s.get("title") or "", info.get("next_due") or "today"]
        rec = {"schedule_id": s["id"], "asset": asset, "title": s.get("title"),
               "state": when, "to": target, "assignee": s.get("assignee_name")}
        if not target:
            rec["result"] = "skipped: no mobile"; skipped.append(rec); continue
        if not tmpl:
            rec["result"] = "skipped: template not set"; skipped.append(rec); continue
        if dry:
            rec["result"] = "dry-run"; sent.append(rec); continue
        ok, detail = await wati.send_template(tmpl, target, params)
        rec["result"] = "sent" if ok else f"failed: {detail}"
        (sent if ok else skipped).append(rec)
    return {"today": today.isoformat(), "sent": sent, "skipped": skipped,
            "counts": {"sent": len(sent), "skipped": len(skipped)}}


@router.get("/run-reminders")
async def run_reminders(token: str = Query(default=""), dry: int = Query(default=0)):
    """Protected cron endpoint — call daily. ?token=<AGENT_TOKEN>  (&dry=1 to preview)."""
    if not AGENT_TOKEN or token != AGENT_TOKEN:
        raise HTTPException(401, "bad token")
    return _json(await _run_reminders(dry=bool(dry)))


@router.get("/reminders/preview")
async def reminders_preview(emp: dict = Depends(require_employee)):
    """Staff-only preview of what the reminder run would send right now."""
    return _json(await _run_reminders(dry=True))
