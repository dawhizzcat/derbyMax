"""Derby timer web backend. Keep your original script next to this as derby.py (unchanged).
Run:  pip install flask pyserial   then   python server.py"""
import queue, random, re, shutil, sqlite3, threading, webbrowser
from pathlib import Path
from flask import Flask, abort, jsonify, request, send_from_directory
from derby import Db, SerialReader, splitGroups, lanes, dbPath

app = Flask(__name__, static_folder="static", static_url_path="")
CLASS_DIR = Path(__file__).parent / "classes"   # one .derby file per class; copy these to share
CLASS_DIR.mkdir(exist_ok=True)
if Path(dbPath).exists() and not any(CLASS_DIR.glob("*.derby")):
    shutil.copy(dbPath, CLASS_DIR / "Original.derby")   # bring your old derby.db along
db = None
clean = lambda n: re.sub(r"[^\w\- ]", "", n or "").strip()[:50]
cpath = lambda n: CLASS_DIR / (clean(n) + ".derby")
q = queue.Queue()
E = dict(cls=None, state="setup", planned=None, heat=None, n=0, total=4, base={}, cur={}, res={}, places={},
         msg="Open or create a class to begin", reader=None,
         status="Not connected", last=None, ended=True, ignore=None, sim=0)
names = lambda: {c: n for c, n, _ in db.contenders()}

def layout():
    s = E["n"] - 1
    E["cur"] = {lanes[(lanes.index(l) + s) % len(lanes)]: c for l, c in E["base"].items()}

def block(): E["ignore"] = None if E["ended"] else E["last"]

def wait():
    miss = [str(l) for l in sorted(E["cur"]) if l not in E["res"]]
    E["msg"] = f"Race {E['n']} of {E['total']}: waiting for lane " + ", ".join(miss)

def startRace():
    E.update(res={}, places={}, state="armed"); block(); wait()

def donePlanned(status):
    if E["planned"] is not None:
        db.setPlannedStatus(E["planned"], status); E["planned"] = None

def finish():
    fin = sorted(E["res"].values())
    places = {l: fin.index(t) + 1 for l, t in E["res"].items()}
    data = {l: (c, E["res"].get(l), places.get(l)) for l, c in E["cur"].items()}
    if E["heat"] is None: E["heat"] = db.addHeat()
    db.addRace(E["heat"], E["n"], data)
    nm = names()
    win = " / ".join(nm[E["cur"][l]] for l, p in places.items() if p == 1) or "no finishers"
    E["places"] = places
    for l in E["cur"]: E["res"].setdefault(l, None)
    block()
    if E["n"] >= E["total"]:
        E["state"] = "setup"; donePlanned("done")
        E["msg"] = f"Heat {E['heat']} complete. Last race winner: {win}."
    else:
        E["state"] = "between"
        E["msg"] = f"Race {E['n']} of {E['total']} recorded. Winner: {win}. Press Next race."

def onRace(gid, results, ended):
    E["last"], E["ended"] = gid, ended
    if E["state"] != "armed" or gid == E["ignore"]: return
    for l, t in results.items():
        if l in E["cur"]: E["res"][l] = t
    if all(l in E["res"] for l in E["cur"]): finish()
    else:
        wait()
        if ended: E["msg"] += "  (timer ended the race)"

def drain():
    while True:
        try: k, p = q.get_nowait()
        except queue.Empty: return
        if k == "race": onRace(*p)
        elif k == "connected": E.update(last=None, ended=True, ignore=None, status="Connected: " + p)
        elif k == "error": E["status"] = "Error: " + p
        elif k == "disconnected" and p is E["reader"]:
            E["reader"] = None
            if not E["status"].startswith("Error"): E["status"] = "Not connected"

def clsAct(a, d):
    global db
    name = clean(d.get("name")); p = cpath(name)
    if not name: return {"error": "Enter a class name"}
    if a == "classDelete":
        if name == E["cls"]: return {"error": "Open a different class before deleting this one"}
        p.unlink(missing_ok=True); return {}
    if E["state"] != "setup": return {"error": "Finish or end the current heat before switching classes"}
    if a == "classNew" and p.exists(): return {"error": "A class with that name already exists"}
    if a == "classOpen" and not p.exists(): return {"error": "Class file not found"}
    if db: db.conn.close()
    db = Db(str(p))   # creates the file / upgrades old layouts
    E.update(cls=name, planned=None, heat=None, n=0, base={}, cur={}, res={}, places={},
             msg="Pick contenders or load a scheduled heat, then start")
    return {}

def act(a, d):
    if a.startswith("class"): return clsAct(a, d)
    if db is None and a != "connect": return {"error": "Open or create a class first"}
    if a == "connect":
        if E["reader"]: E["reader"].stopFlag.set(); return
        E["reader"] = SerialReader(d["port"].strip(), int(d["baud"]), q); E["reader"].start()
        E["status"] = "Connecting..."
    elif a == "simulate":
        if E["state"] != "armed": return {"error": "Start a race first"}
        E["sim"] += 1
        onRace(-E["sim"], {l: random.randint(28000, 36000) for l in E["cur"]}, True)
    elif a == "clearres": E.update(res={}, places={}, cur={})
    elif a == "start":
        base = {int(l): int(c) for l, c in d["lanes"].items() if c}
        if not base: return {"error": "Select at least one contender"}
        E.update(total=max(1, min(8, int(d.get("total") or 4))), base=base, heat=None, n=1, planned=d.get("planned"))
        if E["planned"] is not None: db.setPlannedStatus(E["planned"], "running")
        layout(); startRace()
    elif a == "next" and E["state"] == "between":
        E["n"] += 1; layout(); startRace()
    elif a == "record" and E["state"] == "armed": finish()
    elif a == "end":
        rec = E["n"] if E["state"] == "between" else E["n"] - 1
        block(); E.update(state="setup", res={}, places={}, cur={})
        donePlanned("done" if rec > 0 else "pending")
        E["msg"] = f"Heat ended after {rec} races" if rec > 0 else "Heat cancelled"
    elif a == "addContender":
        try: db.addContender(d["name"].strip())
        except Exception: return {"error": "That name already exists (or is empty)"}
    elif a == "toggle": db.setActive(d["id"], d["active"])
    elif a == "delete":
        if db.contenderStats(d["id"])[0]: return {"error": "This contender has recorded races. Delete those heats first."}
        if db.inSchedule(d["id"]): return {"error": "In the schedule. Clear the schedule or toggle racing off instead."}
        db.deleteContender(d["id"])
    elif a == "genSched":
        active = [c for c, n, on in db.contenders() if on]
        if not active: return {"error": "No contenders are marked as racing"}
        db.clearPlanned(True); random.shuffle(active)
        db.addPlanned([dict(zip(random.sample(lanes, len(g)), g)) for g in splitGroups(active, len(lanes))])
    elif a == "clearSched": db.clearPlanned(False)
    elif a == "markPending": db.setPlannedStatus(d["id"], "pending")
    elif a == "deleteHeat": db.deleteHeat(d["id"])
    return {}

@app.route("/")
def index(): return send_from_directory("static", "index.html")

@app.route("/api/state")
def state():
    drain()
    return jsonify({k: E[k] for k in ("cls", "state", "msg", "status", "n", "total", "cur", "res", "places")} | {"connected": E["reader"] is not None})

@app.route("/api/data")
def data():
    if db is None: return jsonify(cont=[], planned=[], heats=[], laneAvg=[])
    cont = [dict(id=i, name=n, active=a, races=r, fin=f, wins=w, total=t, best=b) for i, n, a, r, f, w, t, b in db.standings()]
    cont.sort(key=lambda c: c["name"].lower())
    planned = [dict(id=i, **h) for i, h in db.planned().items()]
    heats = {}
    for hid, ts, rid, num, lane, name, ticks, place in db.heats():
        h = heats.setdefault(hid, dict(id=hid, ts=ts, races={}))
        r = h["races"].setdefault(rid, dict(num=num, lanes={}))
        r["lanes"][lane] = dict(name=name, ticks=ticks, place=place)
    hl = [dict(h, races=list(h["races"].values())) for h in heats.values()]
    return jsonify(cont=cont, planned=planned, heats=hl, laneAvg=[dict(lane=l, avg=a, n=n) for l, a, n in db.laneAverages()])

@app.route("/api/history/<int:cid>")
def hist(cid):
    if db is None: return jsonify([])
    return jsonify([dict(heat=h, race=r, ts=ts, lane=l, ticks=t, place=p) for h, r, ts, l, t, p in db.history(cid)])

@app.route("/api/classes")
def classes():
    out = []
    for p in sorted(CLASS_DIR.glob("*.derby"), key=lambda p: p.stem.lower()):
        c = r = 0
        try:
            k = sqlite3.connect(p.resolve().as_uri() + "?mode=ro", uri=True)
            c = k.execute("select count(*) from contenders").fetchone()[0]
            r = k.execute("select count(*) from races").fetchone()[0]
            k.close()
        except Exception: pass
        out.append(dict(name=p.stem, contenders=c, races=r))
    return jsonify(out)

@app.route("/api/classfile/<name>")
def classfile(name):
    p = cpath(name)
    if not p.exists(): abort(404)
    return send_from_directory(CLASS_DIR, p.name, as_attachment=True)

@app.route("/api/classImport", methods=["POST"])
def classImport():
    f = request.files.get("file")
    if not f: return jsonify(error="Choose a class file first")
    base = clean(Path(f.filename).stem) or "Imported"; name = base; i = 2
    while cpath(name).exists(): name = f"{base} {i}"; i += 1
    p = cpath(name); f.save(p)
    try:
        k = sqlite3.connect(str(p)); k.execute("select count(*) from sqlite_master").fetchone(); k.close()
        Db(str(p)).conn.close()
    except Exception:
        p.unlink(missing_ok=True); return jsonify(error="That file is not a valid class file")
    return jsonify(name=name)

@app.route("/api/<a>", methods=["POST"])
def post(a):
    drain()
    return jsonify(act(a, request.get_json(silent=True) or {}))

if __name__ == "__main__":
    threading.Timer(1, lambda: webbrowser.open("http://127.0.0.1:5000")).start()
    app.run(port=5000, threaded=False)  # single-threaded: sqlite connection is shared
