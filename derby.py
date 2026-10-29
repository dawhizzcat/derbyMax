import queue
import random
import re
import sqlite3
import threading
import tkinter as tk
from datetime import datetime
from tkinter import messagebox, ttk

import serial

dbPath = "derby.db"
lanes = [1, 2, 3, 4]
tick = 10000
tokenRe = re.compile(rb"(\d)=(\d+)\.(\d+)[^\d\s]")
schema = """
create table if not exists contenders (id integer primary key, name text unique not null, active integer not null default 1);
create table if not exists heats (id integer primary key, ts text not null);
create table if not exists races (
id integer primary key,
heatId integer not null references heats(id),
raceNum integer not null);
create table if not exists raceLanes (
raceId integer not null references races(id),
lane integer not null,
contenderId integer references contenders(id),
ticks integer,
place integer,
primary key (raceId, lane));
create table if not exists plannedHeats (
id integer primary key,
seq integer not null,
status text not null default 'pending');
create table if not exists plannedLanes (
plannedId integer not null references plannedHeats(id),
lane integer not null,
contenderId integer not null references contenders(id),
primary key (plannedId, lane));
"""
migrateSql = """
insert into races (heatId, raceNum) select distinct heatId, 1 from heatLanes;
insert into raceLanes (raceId, lane, contenderId, ticks, place)
select r.id, l.lane, l.contenderId, l.ticks, l.place from heatLanes l join races r on r.heatId = l.heatId;
drop table heatLanes;
"""

def fmtTime(ticks):
    if ticks is None:
        return "DNF"
    return f"{ticks // tick}.{ticks % tick:04d}"

def parseRace(raw):
    results = {}
    for lane, whole, frac in tokenRe.findall(raw):
        lane = int(lane)
        if lane in lanes:
            results[lane] = int(whole) * tick + int(frac.ljust(4, b"0")[:4])
    return results

def splitGroups(items, perHeat):
    count = -(-len(items) // perHeat)
    base, extra = divmod(len(items), count)
    groups = []
    start = 0
    for i in range(count):
        size = base + (1 if i < extra else 0)
        groups.append(items[start:start + size])
        start += size
    return groups

class SerialReader(threading.Thread):
    def __init__(self, portName, baud, outQueue):
        super().__init__(daemon=True)
        self.portName = portName
        self.baud = baud
        self.outQueue = outQueue
        self.stopFlag = threading.Event()

    def run(self):
        try:
            port = serial.Serial(self.portName, self.baud, timeout=0.2)
        except serial.SerialException as e:
            self.outQueue.put(("error", str(e)))
            self.outQueue.put(("disconnected", self))
            return
        self.outQueue.put(("connected", self.portName))
        buf = b""
        groupId = 0
        try:
            while not self.stopFlag.is_set():
                data = port.read(64)
                if not data:
                    continue
                buf += data
                while True:
                    ended = b"\r\n" in buf
                    if ended:
                        raw, buf = buf.split(b"\r\n", 1)
                    else:
                        raw = buf
                    results = parseRace(raw)
                    if results:
                        self.outQueue.put(("race", (groupId, results, ended)))
                    if not ended:
                        break
                    groupId += 1
        except (serial.SerialException, OSError) as e:
            self.outQueue.put(("error", str(e)))
        finally:
            port.close()
            self.outQueue.put(("disconnected", self))

class Db:
    def __init__(self, path):
        self.conn = sqlite3.connect(path)
        self.conn.executescript(schema)
        cols = [r[1] for r in self.conn.execute("pragma table_info(contenders)")]
        if "active" not in cols:
            self.conn.execute("alter table contenders add column active integer not null default 1")
        if self.conn.execute("select name from sqlite_master where name = 'heatLanes'").fetchone():
            self.conn.executescript(migrateSql)
        self.conn.commit()

    def contenders(self):
        return self.conn.execute("select id, name, active from contenders order by name").fetchall()

    def addContender(self, name):
        self.conn.execute("insert into contenders (name) values (?)", (name,))
        self.conn.commit()

    def deleteContender(self, cid):
        self.conn.execute("delete from contenders where id = ?", (cid,))
        self.conn.commit()

    def setActive(self, cid, active):
        self.conn.execute("update contenders set active = ? where id = ?", (active, cid))
        self.conn.commit()

    def contenderStats(self, cid):
        return self.conn.execute("select count(*), coalesce(sum(place = 1), 0), min(ticks) from raceLanes where contenderId = ?", (cid,)).fetchone()

    def inSchedule(self, cid):
        return self.conn.execute("select count(*) from plannedLanes where contenderId = ?", (cid,)).fetchone()[0] > 0

    def addHeat(self):
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        heatId = self.conn.execute("insert into heats (ts) values (?)", (ts,)).lastrowid
        self.conn.commit()
        return heatId

    def addRace(self, heatId, raceNum, data):
        raceId = self.conn.execute("insert into races (heatId, raceNum) values (?, ?)", (heatId, raceNum)).lastrowid
        for lane, (cid, ticks, place) in data.items():
            self.conn.execute("insert into raceLanes (raceId, lane, contenderId, ticks, place) values (?, ?, ?, ?, ?)", (raceId, lane, cid, ticks, place))
        self.conn.commit()
        return raceId

    def deleteHeat(self, heatId):
        self.conn.execute("delete from raceLanes where raceId in (select id from races where heatId = ?)", (heatId,))
        self.conn.execute("delete from races where heatId = ?", (heatId,))
        self.conn.execute("delete from heats where id = ?", (heatId,))
        self.conn.commit()

    def heats(self):
        return self.conn.execute("select h.id, h.ts, r.id, r.raceNum, l.lane, c.name, l.ticks, l.place from heats h join races r on r.heatId = h.id join raceLanes l on l.raceId = r.id left join contenders c on c.id = l.contenderId order by h.id desc, r.raceNum, l.lane").fetchall()

    def history(self, cid):
        return self.conn.execute("select h.id, r.raceNum, h.ts, l.lane, l.ticks, l.place from raceLanes l join races r on r.id = l.raceId join heats h on h.id = r.heatId where l.contenderId = ? order by h.id, r.raceNum", (cid,)).fetchall()

    def standings(self):
        return self.conn.execute("select c.id, c.name, c.active, count(l.lane), count(l.ticks), coalesce(sum(l.place = 1), 0), coalesce(sum(l.ticks), 0), min(l.ticks) from contenders c left join raceLanes l on l.contenderId = c.id group by c.id").fetchall()

    def laneAverages(self):
        return self.conn.execute("select lane, avg(ticks), count(ticks) from raceLanes where ticks is not null group by lane order by lane").fetchall()

    def addPlanned(self, layouts):
        seq = self.conn.execute("select coalesce(max(seq), 0) from plannedHeats").fetchone()[0]
        for layout in layouts:
            seq += 1
            pid = self.conn.execute("insert into plannedHeats (seq) values (?)", (seq,)).lastrowid
            for lane, cid in layout.items():
                self.conn.execute("insert into plannedLanes (plannedId, lane, contenderId) values (?, ?, ?)", (pid, lane, cid))
        self.conn.commit()

    def planned(self):
        heats = {}
        for pid, seq, status in self.conn.execute("select id, seq, status from plannedHeats order by seq"):
            heats[pid] = {"seq": seq, "status": status, "layout": {}}
        for pid, lane, cid in self.conn.execute("select plannedId, lane, contenderId from plannedLanes"):
            heats[pid]["layout"][lane] = cid
        return heats

    def setPlannedStatus(self, pid, status):
        self.conn.execute("update plannedHeats set status = ? where id = ?", (status, pid))
        self.conn.commit()

    def pendingCount(self):
        return self.conn.execute("select count(*) from plannedHeats where status = 'pending'").fetchone()[0]

    def nextPending(self):
        row = self.conn.execute("select id from plannedHeats where status = 'pending' order by seq limit 1").fetchone()
        return row[0] if row else None

    def clearPlanned(self, onlyPending):
        where = " where status = 'pending'" if onlyPending else ""
        self.conn.execute("delete from plannedLanes where plannedId in (select id from plannedHeats" + where + ")")
        self.conn.execute("delete from plannedHeats" + where)
        self.conn.commit()

class App:
    def __init__(self, root):
        self.root = root
        root.title("Derby Timer")
        self.db = Db(dbPath)
        self.q = queue.Queue()
        self.reader = None
        self.state = "setup"
        self.plannedId = None
        self.heatId = None
        self.raceNum = 0
        self.totalRaces = 4
        self.baseLayout = {}
        self.raceLanes = {}
        self.raceResults = {}
        self.racePlaces = {}
        self.lastGroup = None
        self.lastEnded = True
        self.ignoreGroup = None
        self.simCount = 0
        self.nameToId = {}
        self.idToName = {}
        self.activeMap = {}
        self.names = []
        self.activeNames = []
        self.laneVars = {lane: tk.StringVar() for lane in lanes}
        self.timeVars = {lane: tk.StringVar(value="--") for lane in lanes}
        self.placeVars = {lane: tk.StringVar(value="") for lane in lanes}
        self.laneCombos = {}
        self.raceCountVar = tk.StringVar(value="4")
        self.portVar = tk.StringVar(value="COM18")
        self.baudVar = tk.StringVar(value="9600")
        self.status = tk.StringVar(value="Not connected")
        self.heatStatus = tk.StringVar(value="Pick contenders or load a scheduled heat, then start")
        self.rankVar = tk.StringVar(value="Average time")
        self.scheduleInfo = tk.StringVar(value="")
        self.laneInfo = tk.StringVar(value="")
        self.buildSerialBar()
        self.buildContenders()
        self.buildHeatPanel()
        self.buildTabs()
        root.columnconfigure(0, weight=1)
        root.columnconfigure(1, weight=1)
        root.rowconfigure(2, weight=1)
        root.protocol("WM_DELETE_WINDOW", self.onClose)
        self.refreshAll()
        self.setState("setup")
        self.poll()

    def buildSerialBar(self):
        f = ttk.Frame(self.root, padding=6)
        f.grid(row=0, column=0, columnspan=2, sticky="ew")
        ttk.Label(f, text="Port").pack(side="left")
        ttk.Entry(f, textvariable=self.portVar, width=8).pack(side="left", padx=4)
        ttk.Label(f, text="Baud").pack(side="left")
        ttk.Entry(f, textvariable=self.baudVar, width=8).pack(side="left", padx=4)
        self.connectBtn = ttk.Button(f, text="Connect", command=self.toggleConnect)
        self.connectBtn.pack(side="left", padx=4)
        ttk.Button(f, text="Simulate race", command=self.simulate).pack(side="left", padx=4)
        ttk.Label(f, textvariable=self.status).pack(side="left", padx=10)

    def buildContenders(self):
        f = ttk.LabelFrame(self.root, text="Contenders", padding=6)
        f.grid(row=1, column=0, sticky="nsew", padx=6, pady=4)
        top = ttk.Frame(f)
        top.pack(fill="x")
        self.nameVar = tk.StringVar()
        entry = ttk.Entry(top, textvariable=self.nameVar)
        entry.pack(side="left", fill="x", expand=True)
        entry.bind("<Return>", lambda e: self.addContender())
        ttk.Button(top, text="Add", command=self.addContender).pack(side="left", padx=4)
        self.contTree = ttk.Treeview(f, columns=("name", "racing", "races", "wins", "best"), show="headings", height=8)
        for col, text, width in [("name", "Name", 140), ("racing", "Racing", 55), ("races", "Races", 50), ("wins", "Wins", 45), ("best", "Best", 65)]:
            self.contTree.heading(col, text=text)
            self.contTree.column(col, width=width, anchor="w" if col == "name" else "center")
        self.contTree.pack(fill="both", expand=True, pady=4)
        self.contTree.bind("<Double-1>", lambda e: self.viewSelected())
        btns = ttk.Frame(f)
        btns.pack(fill="x")
        ttk.Button(btns, text="View profile", command=self.viewSelected).pack(side="left")
        ttk.Button(btns, text="Toggle racing", command=self.toggleRacing).pack(side="left", padx=4)
        ttk.Button(btns, text="Delete", command=self.deleteSelected).pack(side="left")

    def buildHeatPanel(self):
        f = ttk.LabelFrame(self.root, text="Heat", padding=6)
        f.grid(row=1, column=1, sticky="nsew", padx=6, pady=4)
        for i, lane in enumerate(lanes):
            ttk.Label(f, text=f"Lane {lane}").grid(row=i, column=0, pady=4, sticky="w")
            combo = ttk.Combobox(f, textvariable=self.laneVars[lane], state="readonly", width=20)
            combo.grid(row=i, column=1, padx=6)
            combo.bind("<<ComboboxSelected>>", lambda e: self.onLaneChange())
            self.laneCombos[lane] = combo
            ttk.Label(f, textvariable=self.timeVars[lane], font=("TkDefaultFont", 14, "bold"), width=9).grid(row=i, column=2)
            ttk.Label(f, textvariable=self.placeVars[lane], width=4).grid(row=i, column=3)
        opts = ttk.Frame(f)
        opts.grid(row=len(lanes), column=0, columnspan=4, sticky="w", pady=2)
        ttk.Label(opts, text="Races per heat").pack(side="left")
        self.raceSpin = ttk.Spinbox(opts, from_=1, to=8, textvariable=self.raceCountVar, width=3)
        self.raceSpin.pack(side="left", padx=4)
        ttk.Label(f, textvariable=self.heatStatus, wraplength=420).grid(row=len(lanes) + 1, column=0, columnspan=4, sticky="w", pady=4)
        setupRow = ttk.Frame(f)
        setupRow.grid(row=len(lanes) + 2, column=0, columnspan=4, sticky="w")
        runRow = ttk.Frame(f)
        runRow.grid(row=len(lanes) + 3, column=0, columnspan=4, sticky="w", pady=4)
        self.nextSchedBtn = ttk.Button(setupRow, text="Load next scheduled", command=self.loadNext)
        self.randomBtn = ttk.Button(setupRow, text="Random", command=self.randomHeat)
        self.scrambleBtn = ttk.Button(setupRow, text="Scramble lanes", command=self.scrambleLanes)
        self.clearBtn = ttk.Button(setupRow, text="Clear lanes", command=self.clearLanes)
        self.startBtn = ttk.Button(setupRow, text="Start heat", command=self.startHeat)
        self.recordBtn = ttk.Button(runRow, text="Record now (DNF missing)", command=self.recordNow)
        self.nextBtn = ttk.Button(runRow, text="Next race", command=self.nextRace)
        self.endBtn = ttk.Button(runRow, text="End heat", command=self.endHeat)
        self.setupBtns = [self.nextSchedBtn, self.randomBtn, self.scrambleBtn, self.clearBtn, self.startBtn]
        self.runBtns = [self.recordBtn, self.nextBtn, self.endBtn]
        for b in self.setupBtns + self.runBtns:
            b.pack(side="left", padx=3)

    def buildTabs(self):
        nb = ttk.Notebook(self.root)
        nb.grid(row=2, column=0, columnspan=2, sticky="nsew", padx=6, pady=4)
        self.scheduleTab = ttk.Frame(nb, padding=6)
        self.standingsTab = ttk.Frame(nb, padding=6)
        self.historyTab = ttk.Frame(nb, padding=6)
        nb.add(self.scheduleTab, text="Schedule")
        nb.add(self.standingsTab, text="Standings")
        nb.add(self.historyTab, text="History")
        self.buildSchedule()
        self.buildStandings()
        self.buildHistory()

    def buildSchedule(self):
        f = self.scheduleTab
        top = ttk.Frame(f)
        top.pack(fill="x")
        ttk.Button(top, text="Generate schedule", command=self.generateSchedule).pack(side="left")
        ttk.Button(top, text="Load selected", command=self.loadSelected).pack(side="left", padx=4)
        ttk.Button(top, text="Mark pending", command=self.markPending).pack(side="left")
        ttk.Button(top, text="Clear schedule", command=self.clearSchedule).pack(side="left", padx=4)
        ttk.Label(top, textvariable=self.scheduleInfo).pack(side="left", padx=10)
        self.schedTree = ttk.Treeview(f, columns=("slot", "status", "l1", "l2", "l3", "l4"), show="headings", height=8)
        for col, text, width in [("slot", "Slot", 50), ("status", "Status", 80), ("l1", "Lane 1", 150), ("l2", "Lane 2", 150), ("l3", "Lane 3", 150), ("l4", "Lane 4", 150)]:
            self.schedTree.heading(col, text=text)
            self.schedTree.column(col, width=width)
        self.schedTree.tag_configure("done", foreground="gray")
        self.schedTree.pack(fill="both", expand=True, pady=4)
        self.schedTree.bind("<Double-1>", lambda e: self.loadSelected())

    def buildStandings(self):
        f = self.standingsTab
        top = ttk.Frame(f)
        top.pack(fill="x")
        ttk.Label(top, text="Rank by").pack(side="left")
        combo = ttk.Combobox(top, textvariable=self.rankVar, values=["Average time", "Best time", "Wins"], state="readonly", width=14)
        combo.pack(side="left", padx=4)
        combo.bind("<<ComboboxSelected>>", lambda e: self.refreshStandings())
        cols = ("rank", "name", "races", "dnf", "wins", "avg", "best")
        self.standTree = ttk.Treeview(f, columns=cols, show="headings", height=8)
        for col, text, width in [("rank", "Rank", 50), ("name", "Name", 160), ("races", "Races", 60), ("dnf", "DNF", 50), ("wins", "Wins", 50), ("avg", "Average", 90), ("best", "Best", 90)]:
            self.standTree.heading(col, text=text)
            self.standTree.column(col, width=width, anchor="w" if col == "name" else "center")
        self.standTree.pack(fill="both", expand=True, pady=4)
        ttk.Label(f, textvariable=self.laneInfo).pack(anchor="w")

    def buildHistory(self):
        f = self.historyTab
        cols = ("heat", "race", "time", "winner", "l1", "l2", "l3", "l4")
        self.histTree = ttk.Treeview(f, columns=cols, show="headings", height=8)
        for col, text, width in [("heat", "Heat", 60), ("race", "Race", 55), ("time", "Recorded", 130), ("winner", "Winner / wins", 130), ("l1", "Lane 1", 150), ("l2", "Lane 2", 150), ("l3", "Lane 3", 150), ("l4", "Lane 4", 150)]:
            self.histTree.heading(col, text=text)
            self.histTree.column(col, width=width)
        self.histTree.pack(fill="both", expand=True)
        ttk.Button(f, text="Delete heat", command=self.deleteHeat).pack(anchor="w", pady=4)

    def setState(self, state):
        self.state = state
        on = {"setup": self.setupBtns, "armed": [self.recordBtn, self.endBtn], "between": [self.nextBtn, self.endBtn]}[state]
        for b in self.setupBtns + self.runBtns:
            b.config(state="normal" if b in on else "disabled")
        for lane in lanes:
            self.laneCombos[lane].config(state="readonly" if state == "setup" else "disabled")
        self.raceSpin.config(state="normal" if state == "setup" else "disabled")

    def toggleConnect(self):
        if self.reader:
            self.reader.stopFlag.set()
            return
        try:
            baud = int(self.baudVar.get())
        except ValueError:
            messagebox.showwarning("Baud", "Baud must be a number")
            return
        self.reader = SerialReader(self.portVar.get().strip(), baud, self.q)
        self.reader.start()
        self.connectBtn.config(text="Disconnect")
        self.status.set("Connecting...")

    def poll(self):
        while True:
            try:
                kind, payload = self.q.get_nowait()
            except queue.Empty:
                break
            if kind == "race":
                self.onRace(*payload)
            elif kind == "connected":
                self.lastGroup = None
                self.lastEnded = True
                self.ignoreGroup = None
                self.status.set(f"Connected: {payload}")
            elif kind == "error":
                self.status.set(f"Error: {payload}")
            elif kind == "disconnected" and payload is self.reader:
                self.reader = None
                self.connectBtn.config(text="Connect")
                if not self.status.get().startswith("Error"):
                    self.status.set("Not connected")
        self.root.after(100, self.poll)

    def onRace(self, groupId, results, ended):
        self.lastGroup = groupId
        self.lastEnded = ended
        if self.state != "armed" or groupId == self.ignoreGroup:
            return
        for lane, ticks in results.items():
            if lane in self.raceLanes:
                self.raceResults[lane] = ticks
        self.showTimes()
        if all(lane in self.raceResults for lane in self.raceLanes):
            self.finishRace()
        else:
            self.updateWaiting()
            if ended:
                self.heatStatus.set(self.heatStatus.get() + "  (timer ended the race)")

    def simulate(self):
        if self.state != "armed":
            messagebox.showwarning("Simulate", "Start a race first")
            return
        self.simCount += 1
        results = {lane: random.randint(28000, 36000) for lane in self.raceLanes}
        self.onRace(-self.simCount, results, True)

    def blockOpenGroup(self):
        self.ignoreGroup = None if self.lastEnded else self.lastGroup

    def refreshLaneOptions(self):
        for lane in lanes:
            taken = {self.laneVars[o].get() for o in lanes if o != lane}
            self.laneCombos[lane]["values"] = [""] + [n for n in self.names if n not in taken]

    def onLaneChange(self):
        self.plannedId = None
        self.raceResults = {}
        self.racePlaces = {}
        self.heatStatus.set("Pick contenders or load a scheduled heat, then start")
        self.refreshLaneOptions()
        self.showTimes()

    def randomHeat(self):
        if not self.activeNames:
            messagebox.showwarning("No contenders", "No contenders are marked as racing")
            return
        picks = random.sample(self.activeNames, min(len(lanes), len(self.activeNames)))
        for lane in lanes:
            self.laneVars[lane].set(picks[lane - 1] if lane <= len(picks) else "")
        self.scrambleLanes()

    def scrambleLanes(self):
        names = [self.laneVars[lane].get() for lane in lanes if self.laneVars[lane].get()]
        if not names:
            return
        random.shuffle(names)
        spots = random.sample(lanes, len(names))
        for lane in lanes:
            self.laneVars[lane].set("")
        for lane, name in zip(spots, names):
            self.laneVars[lane].set(name)
        self.onLaneChange()

    def clearLanes(self):
        for lane in lanes:
            self.laneVars[lane].set("")
        self.onLaneChange()

    def generateSchedule(self):
        active = [cid for cid, name, act in self.db.contenders() if act]
        if not active:
            messagebox.showwarning("No contenders", "No contenders are marked as racing")
            return
        if self.db.pendingCount() and not messagebox.askyesno("Generate schedule", "Replace the heats still waiting in the schedule?"):
            return
        self.db.clearPlanned(True)
        random.shuffle(active)
        layouts = []
        for group in splitGroups(active, len(lanes)):
            spots = random.sample(lanes, len(group))
            layouts.append(dict(zip(spots, group)))
        self.db.addPlanned(layouts)
        self.refreshSchedule()
        self.scheduleInfo.set(self.scheduleInfo.get() + f"  ({len(active)} contenders in {len(layouts)} heats)")

    def selectedPlanned(self):
        sel = self.schedTree.selection()
        if not sel:
            return None
        return int(sel[0])

    def loadSelected(self):
        pid = self.selectedPlanned()
        if pid is not None:
            self.loadPlanned(pid)

    def loadNext(self):
        pid = self.db.nextPending()
        if pid is None:
            messagebox.showinfo("Schedule", "No scheduled heats are waiting. Generate a schedule first.")
            return
        self.loadPlanned(pid)

    def loadPlanned(self, pid):
        if self.state != "setup":
            messagebox.showwarning("Heat in progress", "Finish or end the current heat first")
            return
        heat = self.db.planned().get(pid)
        if heat is None:
            return
        for lane in lanes:
            cid = heat["layout"].get(lane)
            self.laneVars[lane].set(self.idToName.get(cid, "") if cid is not None else "")
        self.onLaneChange()
        self.plannedId = pid
        self.heatStatus.set(f"Scheduled heat {heat['seq']} loaded. Press Start heat.")

    def markPending(self):
        pid = self.selectedPlanned()
        if pid is not None:
            self.db.setPlannedStatus(pid, "pending")
            self.refreshSchedule()

    def clearSchedule(self):
        if messagebox.askyesno("Clear schedule", "Remove every heat from the schedule? Recorded results are not affected."):
            self.db.clearPlanned(False)
            self.refreshSchedule()

    def completePlanned(self, status):
        if self.plannedId is not None:
            self.db.setPlannedStatus(self.plannedId, status)
            self.plannedId = None
            self.refreshSchedule()

    def startHeat(self):
        layout = {}
        for lane in lanes:
            cid = self.nameToId.get(self.laneVars[lane].get())
            if cid is not None:
                layout[lane] = cid
        if not layout:
            messagebox.showwarning("No contenders", "Select at least one contender")
            return
        try:
            self.totalRaces = max(1, min(8, int(self.raceCountVar.get())))
        except ValueError:
            self.totalRaces = 4
        self.raceCountVar.set(str(self.totalRaces))
        self.baseLayout = layout
        self.heatId = None
        self.raceNum = 1
        if self.plannedId is not None:
            self.db.setPlannedStatus(self.plannedId, "running")
            self.refreshSchedule()
        self.applyLayout()
        self.startRace()

    def applyLayout(self):
        shift = self.raceNum - 1
        self.raceLanes = {lanes[(lanes.index(lane) + shift) % len(lanes)]: cid for lane, cid in self.baseLayout.items()}
        for lane in lanes:
            cid = self.raceLanes.get(lane)
            self.laneVars[lane].set(self.idToName[cid] if cid is not None else "")
        self.refreshLaneOptions()

    def startRace(self):
        self.raceResults = {}
        self.racePlaces = {}
        self.blockOpenGroup()
        self.setState("armed")
        self.showTimes()
        self.updateWaiting()

    def nextRace(self):
        if self.state != "between":
            return
        self.raceNum += 1
        self.applyLayout()
        self.startRace()

    def updateWaiting(self):
        missing = [str(l) for l in sorted(self.raceLanes) if l not in self.raceResults]
        self.heatStatus.set(f"Race {self.raceNum} of {self.totalRaces}: waiting for lane " + ", ".join(missing))

    def showTimes(self):
        for lane in lanes:
            if lane in self.raceResults:
                self.timeVars[lane].set(fmtTime(self.raceResults[lane]))
            elif self.state == "armed" and lane in self.raceLanes:
                self.timeVars[lane].set("...")
            else:
                self.timeVars[lane].set("--")
            place = self.racePlaces.get(lane)
            self.placeVars[lane].set(f"P{place}" if place else "")

    def recordNow(self):
        if self.state != "armed":
            return
        missing = [str(l) for l in sorted(self.raceLanes) if l not in self.raceResults]
        if not self.raceResults:
            msg = "No times yet. Every lane will be recorded as DNF."
        else:
            msg = "Lane " + ", ".join(missing) + " will be recorded as DNF."
        if messagebox.askyesno("Record race", msg + "\n\nRecord now?"):
            self.finishRace()

    def finishRace(self):
        finished = sorted(self.raceResults.values())
        places = {lane: finished.index(t) + 1 for lane, t in self.raceResults.items()}
        data = {lane: (cid, self.raceResults.get(lane), places.get(lane)) for lane, cid in self.raceLanes.items()}
        if self.heatId is None:
            self.heatId = self.db.addHeat()
        self.db.addRace(self.heatId, self.raceNum, data)
        winners = [self.idToName[self.raceLanes[lane]] for lane, p in places.items() if p == 1]
        winText = " / ".join(winners) if winners else "no finishers"
        self.racePlaces = places
        for lane in self.raceLanes:
            self.raceResults.setdefault(lane, None)
        self.blockOpenGroup()
        self.showTimes()
        if self.raceNum >= self.totalRaces:
            self.setState("setup")
            self.completePlanned("done")
            more = " Press Load next scheduled for the next group." if self.db.pendingCount() else ""
            self.heatStatus.set(f"Heat {self.heatId} complete. Last race winner: {winText}.{more}")
        else:
            self.setState("between")
            self.heatStatus.set(f"Race {self.raceNum} of {self.totalRaces} recorded. Winner: {winText}. Press Next race.")
        self.refreshAll()

    def endHeat(self):
        recorded = self.raceNum if self.state == "between" else self.raceNum - 1
        if recorded > 0 and not messagebox.askyesno("End heat", f"End the heat now? {recorded} of {self.totalRaces} races are saved."):
            return
        self.blockOpenGroup()
        self.raceResults = {}
        self.racePlaces = {}
        self.setState("setup")
        self.completePlanned("done" if recorded > 0 else "pending")
        self.showTimes()
        self.heatStatus.set(f"Heat ended after {recorded} races" if recorded > 0 else "Heat cancelled")

    def addContender(self):
        name = self.nameVar.get().strip()
        if not name:
            return
        try:
            self.db.addContender(name)
        except sqlite3.IntegrityError:
            messagebox.showwarning("Duplicate", f"{name} already exists")
            return
        self.nameVar.set("")
        self.refreshAll()

    def selectedContender(self):
        sel = self.contTree.selection()
        if not sel:
            return None
        return int(sel[0])

    def toggleRacing(self):
        cid = self.selectedContender()
        if cid is None:
            return
        self.db.setActive(cid, 0 if self.activeMap[cid] else 1)
        self.refreshAll()
        self.contTree.selection_set(str(cid))

    def deleteSelected(self):
        cid = self.selectedContender()
        if cid is None:
            return
        name = self.idToName[cid]
        if name in [v.get() for v in self.laneVars.values()]:
            messagebox.showwarning("In use", f"{name} is in the current heat")
            return
        if self.db.contenderStats(cid)[0] > 0:
            messagebox.showwarning("Cannot delete", f"{name} has recorded races. Delete those heats first.")
            return
        if self.db.inSchedule(cid):
            messagebox.showwarning("Cannot delete", f"{name} is in the schedule. Clear the schedule first, or toggle racing off instead.")
            return
        if messagebox.askyesno("Delete", f"Delete {name}?"):
            self.db.deleteContender(cid)
            self.refreshAll()

    def refreshAll(self):
        self.refreshContenders()
        self.refreshSchedule()
        self.refreshStandings()
        self.refreshHistory()

    def refreshContenders(self):
        rows = self.db.contenders()
        self.names = [name for _, name, _ in rows]
        self.activeNames = [name for _, name, act in rows if act]
        self.nameToId = {name: cid for cid, name, _ in rows}
        self.idToName = {cid: name for cid, name, _ in rows}
        self.activeMap = {cid: act for cid, _, act in rows}
        self.contTree.delete(*self.contTree.get_children())
        for cid, name, act in rows:
            count, wins, best = self.db.contenderStats(cid)
            self.contTree.insert("", "end", iid=str(cid), values=(name, "yes" if act else "no", count, wins, fmtTime(best) if best else "--"))
        for lane in lanes:
            if self.laneVars[lane].get() not in self.names:
                self.laneVars[lane].set("")
        self.refreshLaneOptions()

    def refreshSchedule(self):
        self.schedTree.delete(*self.schedTree.get_children())
        heats = self.db.planned()
        for pid, heat in heats.items():
            cells = [self.idToName.get(heat["layout"].get(lane), "-") if lane in heat["layout"] else "-" for lane in lanes]
            tags = ("done",) if heat["status"] == "done" else ()
            self.schedTree.insert("", "end", iid=str(pid), values=(heat["seq"], heat["status"], *cells), tags=tags)
        done = sum(1 for h in heats.values() if h["status"] == "done")
        self.scheduleInfo.set(f"{done} of {len(heats)} heats done" if heats else "No schedule yet")

    def refreshStandings(self):
        mode = self.rankVar.get()
        rows = []
        for cid, name, act, races, finished, wins, total, best in self.db.standings():
            if races == 0 and not act:
                continue
            avg = round(total / finished) if finished else None
            rows.append({"name": name, "races": races, "dnf": races - finished, "wins": wins, "avg": avg, "best": best})
        ranked = [r for r in rows if r["avg"] is not None]
        unranked = [r for r in rows if r["avg"] is None]
        if mode == "Best time":
            key = lambda r: (r["best"],)
        elif mode == "Wins":
            key = lambda r: (-r["wins"], r["avg"])
        else:
            key = lambda r: (r["avg"],)
        ranked.sort(key=lambda r: key(r) + (r["name"],))
        self.standTree.delete(*self.standTree.get_children())
        prev = None
        rank = 0
        for i, r in enumerate(ranked):
            if key(r) != prev:
                rank = i + 1
                prev = key(r)
            self.standTree.insert("", "end", values=(rank, r["name"], r["races"], r["dnf"], r["wins"], fmtTime(r["avg"]), fmtTime(r["best"])))
        for r in sorted(unranked, key=lambda r: r["name"]):
            self.standTree.insert("", "end", values=("-", r["name"], r["races"], r["dnf"], r["wins"], "--", "--"))
        laneRows = self.db.laneAverages()
        if laneRows:
            self.laneInfo.set("Average time by lane:   " + "    ".join(f"Lane {lane}: {fmtTime(round(avg))}" for lane, avg, n in laneRows))
        else:
            self.laneInfo.set("")

    def refreshHistory(self):
        self.histTree.delete(*self.histTree.get_children())
        heats = {}
        for heatId, ts, raceId, raceNum, lane, name, ticks, place in self.db.heats():
            heat = heats.setdefault(heatId, {"ts": ts, "races": {}})
            race = heat["races"].setdefault(raceId, {"num": raceNum, "lanes": {}, "winners": []})
            text = f"{name or '?'}  {fmtTime(ticks)}"
            if place:
                text += f"  P{place}"
            race["lanes"][lane] = text
            if place == 1:
                race["winners"].append(name or "?")
        for heatId, heat in heats.items():
            wins = {}
            for race in heat["races"].values():
                for w in race["winners"]:
                    wins[w] = wins.get(w, 0) + 1
            summary = ", ".join(f"{n} {c}" for n, c in sorted(wins.items(), key=lambda x: -x[1]))
            parent = f"h{heatId}"
            self.histTree.insert("", "end", iid=parent, open=True, values=(f"Heat {heatId}", "", heat["ts"], summary, "", "", "", ""))
            for raceId, race in heat["races"].items():
                cells = [race["lanes"].get(lane, "-") for lane in lanes]
                self.histTree.insert(parent, "end", iid=f"r{raceId}", values=("", f"Race {race['num']}", "", " / ".join(race["winners"]) or "-", *cells))

    def deleteHeat(self):
        sel = self.histTree.selection()
        if not sel:
            return
        iid = sel[0]
        if iid.startswith("r"):
            iid = self.histTree.parent(iid)
        heatId = int(iid[1:])
        if messagebox.askyesno("Delete heat", f"Delete heat {heatId} and all its races?"):
            self.db.deleteHeat(heatId)
            self.refreshAll()

    def viewSelected(self):
        cid = self.selectedContender()
        if cid is None:
            return
        name = self.idToName[cid]
        rows = self.db.history(cid)
        done = [r[4] for r in rows if r[4] is not None]
        best = fmtTime(min(done)) if done else "--"
        avg = fmtTime(round(sum(done) / len(done))) if done else "--"
        wins = sum(1 for r in rows if r[5] == 1)
        win = tk.Toplevel(self.root)
        win.title(name)
        ttk.Label(win, text=name, font=("TkDefaultFont", 16, "bold")).pack(padx=10, pady=(10, 2))
        ttk.Label(win, text=f"Races: {len(rows)}    Wins: {wins}    Best: {best}    Average: {avg}").pack(padx=10)
        tree = ttk.Treeview(win, columns=("heat", "race", "date", "lane", "time", "place"), show="headings", height=12)
        for col, text, width in [("heat", "Heat", 50), ("race", "Race", 50), ("date", "Recorded", 140), ("lane", "Lane", 50), ("time", "Time", 80), ("place", "Place", 50)]:
            tree.heading(col, text=text)
            tree.column(col, width=width, anchor="center")
        tree.pack(fill="both", expand=True, padx=10, pady=10)
        for heatId, raceNum, ts, lane, ticks, place in rows:
            tree.insert("", "end", values=(heatId, raceNum, ts, lane, fmtTime(ticks), place or "-"))

    def onClose(self):
        if self.reader:
            self.reader.stopFlag.set()
        self.root.destroy()

if __name__ == "__main__":
    root = tk.Tk()
    App(root)
    root.mainloop()
