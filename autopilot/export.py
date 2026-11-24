"""The weekly export: one zip with everything needed to judge what worked, and nothing secret.

    python3 autopilot/autopilot.py export        # -> trader/export/autopilot-export-<date>.zip
    (or export.bat, or the button in the dashboard)

In:  every trading day (each candidate with every stage, features and Time Machine numbers),
     the trades, the event log, the equity curve, the learning runs, the settings, the pack's
     broker / risk logs and the portfolio.
Out: .env (the Alpaca keys) is never read. Personal fields (sec_user_agent = name and email)
     are blanked. Account numbers are already shortened (PA…xyz) in the status file.
"""
import datetime
import io
import json
import os
import zipfile

PRIVATE_KEYS = {"sec_user_agent"}
FILES = [
    ("trader/autopilot/history.json", None), ("trader/autopilot/state.json", None),
    ("trader/autopilot/status.json", None), ("trader/autopilot/events.jsonl", None),
    ("trader/autopilot/equity.jsonl", None), ("trader/autopilot/learning.json", None),
    ("trader/autopilot.json", "settings"), ("trader/config.json", "settings"),
    ("trader/portfolio.json", None), ("trader/state/broker.json", None), ("trader/state/risk.json", None),
]


def _clean(obj):
    if isinstance(obj, dict):
        return {k: ("" if k in PRIVATE_KEYS else _clean(v)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_clean(v) for v in obj]
    return obj


def build(root):
    """-> (zip bytes, manifest)"""
    buf = io.BytesIO()
    manifest = {"created": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"), "files": [], "days": []}
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for rel, kind in FILES:
            p = os.path.join(root, rel)
            if not os.path.exists(p):
                continue
            with open(p, encoding="utf-8") as f:
                text = f.read()
            if rel.endswith(".json"):
                try:
                    text = json.dumps(_clean(json.loads(text)), indent=1)
                except ValueError:
                    pass
            z.writestr(rel, text)
            manifest["files"].append(rel)
        days = os.path.join(root, "trader", "autopilot", "days")
        if os.path.isdir(days):
            for name in sorted(os.listdir(days)):
                if name.endswith(".json"):
                    with open(os.path.join(days, name), encoding="utf-8") as f:
                        z.writestr("trader/autopilot/days/" + name, json.dumps(_clean(json.load(f))))
                    manifest["days"].append(name[:-5])
        z.writestr("manifest.json", json.dumps(manifest, indent=1))
    return buf.getvalue(), manifest


def write(root):
    data, manifest = build(root)
    out_dir = os.path.join(root, "trader", "export")
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, "autopilot-export-%s.zip" % datetime.date.today().isoformat())
    with open(path, "wb") as f:
        f.write(data)
    return path, manifest
