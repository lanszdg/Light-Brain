"""Single-writer SQLite ledger and immutable, content-addressed artifacts."""

from contextlib import contextmanager
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import sqlite3
import time
from uuid import uuid4


class Conflict(ValueError):
    """The supplied version, lease or immutable identity conflicts with durable state."""


class NotFound(KeyError):
    """The requested object is unknown within the specified scope."""


def encode(data):
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(data):
    return sha256(encode(data).encode("utf-8")).hexdigest()


def nonempty(value, label):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a nonempty string")
    return value


def positive_lease(seconds):
    if isinstance(seconds, bool) or not isinstance(seconds, (float, int)) or not 0 < seconds <= 86400:
        raise ValueError("lease_seconds must be in (0, 86400]")
    return seconds


class Store:
    _reserved = {"id", "case_id", "brain_id", "project_id", "revision", "brain_revision",
                 "request", "created"}

    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.objects = self.root / "objects"
        self.objects.mkdir(exist_ok=True)
        self.path = self.root / "lightbrain.sqlite3"
        db = self._open()
        try:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1):
                raise RuntimeError(f"unsupported store schema {version}")
            schema = Path(__file__).with_name("schema.sql").read_text(encoding="utf-8")
            db.executescript("BEGIN IMMEDIATE;\n" + schema + "\nCOMMIT;")
        finally:
            db.close()

    def _open(self):
        db = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA temp_store=MEMORY")
        return db

    @contextmanager
    def _tx(self, write=False):
        db = self._open()
        try:
            db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _case(db, case_id):
        row = db.execute("SELECT c.*,b.revision AS brain_revision FROM cases c JOIN brains b ON "
                         "b.id=c.brain WHERE c.id=?", (case_id,)).fetchone()
        if not row:
            raise NotFound(case_id)
        return {**json.loads(row["data"]), "id": row["id"], "case_id": row["id"],
                "brain_id": row["brain"], "project_id": row["project"], "revision": row["revision"],
                "brain_revision": row["brain_revision"], "request": json.loads(row["request"]),
                "created": row["created"]}

    def create_case(self, brain, project, request, event_id=None):
        nonempty(brain, "brain")
        nonempty(project, "project")
        event_id = str(uuid4()) if event_id is None else nonempty(event_id, "event_id")
        key = digest(["create", brain, project, request])
        with self._tx(True) as db:
            previous = db.execute("SELECT * FROM events WHERE brain=? AND event_id=?",
                                  (brain, event_id)).fetchone()
            if previous:
                if previous["fingerprint"] != key:
                    raise Conflict("event ID already identifies another request")
                return self._case(db, previous["case_id"])
            case_id, now = str(uuid4()), time.time()
            db.execute("INSERT OR IGNORE INTO brains(id) VALUES (?)", (brain,))
            db.execute("UPDATE brains SET revision=revision+1 WHERE id=?", (brain,))
            db.execute("INSERT INTO cases VALUES(?,?,?,?,?,?,?)",
                       (case_id, brain, project, 1, encode(request), encode({"state":"new"}), now))
            db.execute("INSERT INTO events VALUES(?,?,?,?,?,?,?,?)",
                       (brain, event_id, case_id, 1, "created", encode({"request":request}), key, now))
            return self._case(db, case_id)

    def get_case(self, case_id):
        with self._tx() as db:
            return self._case(db, case_id)

    def list_cases(self, brain=None, project=None):
        with self._tx() as db:
            rows = db.execute("SELECT id FROM cases WHERE (? IS NULL OR brain=?) AND "
                              "(? IS NULL OR project=?) ORDER BY created,id", (brain,brain,project,project))
            return [self._case(db, row["id"]) for row in rows]

    def advance(self, case_id, expected_rev, event_id, event_type, patch, jobs=(),
                graph_delta=None, consumed_job=None, criteria=None, expected_brain_rev=None,
                method_changes=()):
        nonempty(event_id, "event_id")
        nonempty(event_type, "event_type")
        if not isinstance(patch, dict) or self._reserved.intersection(patch):
            raise ValueError("patch cannot overwrite immutable case identity")
        jobs = list(jobs)
        criteria = list(criteria or [])
        method_changes = list(method_changes)
        change = {"patch":patch, "jobs":jobs, "graph_delta":graph_delta, "consumed_job":consumed_job,
                  "criteria":criteria, "method_changes":method_changes}
        key = digest([case_id,event_type,change])
        with self._tx(True) as db:
            case = self._case(db, case_id)
            brain = case["brain_id"]
            prior = db.execute("SELECT fingerprint FROM events WHERE brain=? AND event_id=?",
                               (brain,event_id)).fetchone()
            if prior:
                if prior[0] != key:
                    raise Conflict("duplicate event has different content")
                return case
            if expected_rev != case["revision"] or (expected_brain_rev is not None and
                                                     expected_brain_rev != case["brain_revision"]):
                raise Conflict("decision snapshot is stale")
            rev, now = expected_rev + 1, time.time()
            data = {k:v for k,v in case.items() if k not in self._reserved}
            data.update(patch)
            db.execute("UPDATE cases SET revision=?,data=? WHERE id=?", (rev,encode(data),case_id))
            db.execute("UPDATE brains SET revision=revision+1 WHERE id=?", (brain,))
            self._update_graph(db, case_id, rev, graph_delta or {})
            for method in method_changes:
                scope = method["scope"]
                if (scope.get("brain_id") != brain or
                        scope.get("project_id", case["project_id"]) != case["project_id"]):
                    raise Conflict("case cannot register a method for another scope")
                self._save_method(db, brain, scope.get("project_id"), method)
            for criterion in criteria:
                cid = nonempty(criterion["id"], "criterion.id")
                version = criterion.get("version", 1)
                if type(version) is not int or version < 1:
                    raise ValueError("criterion version must be a positive integer")
                checksum = digest(criterion)
                prior = db.execute("SELECT digest FROM criteria WHERE case_id=? AND id=? AND version=?",
                                   (case_id,cid,version)).fetchone()
                if prior and prior[0] != checksum:
                    raise Conflict("frozen criterion changed without a new version")
                db.execute("INSERT OR IGNORE INTO criteria VALUES(?,?,?,?,?,?)",
                           (case_id,cid,version,rev,checksum,encode(criterion)))
            for job in jobs:
                jid = job.get("id") or str(uuid4())
                kind = nonempty(job["kind"], "job.kind")
                try:
                    db.execute("INSERT INTO jobs(id,case_id,revision,kind,payload,status,created) "
                               "VALUES(?,?,?,?,?,'queued',?)",
                               (jid,case_id,rev,kind,encode(job.get("payload",{})),now))
                except sqlite3.IntegrityError as exc:
                    raise Conflict("job ID is already used") from exc
            if consumed_job:
                result = db.execute("UPDATE jobs SET consumed_at=? WHERE id=? AND case_id=? AND "
                                    "status='finished' AND consumed_at IS NULL", (rev,consumed_job,case_id))
                if result.rowcount != 1:
                    raise Conflict("receipt does not belong to this case or was already consumed")
            db.execute("INSERT INTO events VALUES(?,?,?,?,?,?,?,?)",
                       (brain,event_id,case_id,rev,event_type,encode(change),key,now))
            return self._case(db,case_id)

    @staticmethod
    def _update_graph(db, case_id, rev, delta):
        if set(delta) - {"nodes", "edges", "remove_nodes", "remove_edges"}:
            raise ValueError("unknown graph delta key")
        for kind in ("nodes","edges"):
            updates = delta.get(kind, [])
            removed = delta.get("remove_" + kind, [])
            touched = [item["id"] for item in updates] + list(removed)
            if len(touched) != len(set(touched)):
                raise ValueError("a graph object may change only once per revision")
            for gid in touched:
                nonempty(gid, "graph ID")
                db.execute("UPDATE graph SET valid_to=? WHERE case_id=? AND kind=? AND id=? "
                           "AND valid_to IS NULL", (rev,case_id,kind,gid))
            for item in updates:
                db.execute("INSERT INTO graph VALUES(?,?,?,?,NULL,?)",
                           (case_id,kind,item["id"],rev,encode(item)))
        if delta:
            for row in db.execute("SELECT body FROM graph WHERE case_id=? AND kind='edges' "
                                  "AND valid_to IS NULL", (case_id,)):
                edge = json.loads(row[0])
                for endpoint in ("source","target"):
                    if not db.execute("SELECT 1 FROM graph WHERE case_id=? AND kind='nodes' "
                                      "AND id=? AND valid_to IS NULL", (case_id,edge[endpoint])).fetchone():
                        raise Conflict("graph edge refers to an absent endpoint in this snapshot")

    def graph_snapshot(self, case_id, revision=None):
        with self._tx() as db:
            case = self._case(db,case_id)
            rev = case["revision"] if revision is None else revision
            if type(rev) is not int or not 1 <= rev <= case["revision"]:
                raise NotFound(revision)
            result = {"case_id":case_id, "revision":rev, "nodes":[], "edges":[]}
            rows = db.execute("SELECT kind,body FROM graph WHERE case_id=? AND valid_from<=? AND "
                              "(valid_to IS NULL OR valid_to>?) ORDER BY id",(case_id,rev,rev))
            for row in rows:
                result[row["kind"]].append(json.loads(row["body"]))
            return result

    def list_events(self, case_id, after=0):
        with self._tx() as db:
            self._case(db,case_id)
            rows = db.execute("SELECT event_id,event_type,revision,body,created FROM events WHERE "
                              "case_id=? AND revision>? ORDER BY revision",(case_id,after))
            return [{**dict(row),"body":json.loads(row["body"])} for row in rows]

    def list_criteria(self, case_id):
        with self._tx() as db:
            self._case(db,case_id)
            rows = db.execute("SELECT * FROM criteria WHERE case_id=? ORDER BY id,version",(case_id,))
            return [{**json.loads(row["body"]),"version":row["version"],
                     "digest":row["digest"],"frozen_at":row["frozen_at"]} for row in rows]

    @staticmethod
    def _job(db, job_id):
        row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            raise NotFound(job_id)
        return {**dict(row), "job_id":row["id"], "payload":json.loads(row["payload"]),
                "receipt":json.loads(row["receipt"]) if row["receipt"] else None}

    def get_job(self, job_id):
        with self._tx() as db:
            return self._job(db,job_id)

    def list_jobs(self, case_id):
        with self._tx() as db:
            self._case(db,case_id)
            return [self._job(db,row[0]) for row in db.execute(
                "SELECT id FROM jobs WHERE case_id=? ORDER BY created,rowid", (case_id,))]

    def claim(self, worker, kinds=None, lease_seconds=60):
        nonempty(worker,"worker")
        positive_lease(lease_seconds)
        if isinstance(kinds,str):
            raise ValueError("expected a sequence of job kinds")
        with self._tx(True) as db:
            now = time.time()
            args = [now]
            kinds_sql = ""
            if kinds is not None:
                kinds=list(kinds)
                if not kinds:
                    return None
                kinds_sql = " AND kind IN (" + ",".join("?" for _ in kinds) + ")"
                args.extend(kinds)
            row = db.execute("SELECT id FROM jobs WHERE (status='queued' OR "
                             "(status='leased' AND lease_until<=?))" + kinds_sql +
                             " ORDER BY created,rowid LIMIT 1",args).fetchone()
            if not row:
                return None
            db.execute("UPDATE jobs SET status='leased',attempt=attempt+1,worker=?,lease_until=? "
                       "WHERE id=?", (worker,now+lease_seconds,row[0]))
            return self._job(db,row[0])

    @staticmethod
    def _owner(job, attempt, worker):
        if type(attempt) is not int or job["attempt"] != attempt or (
                worker is not None and worker != job["worker"]):
            raise Conflict("lease generation or worker mismatch")

    def heartbeat(self, job_id, attempt, lease_seconds=60, worker=None):
        positive_lease(lease_seconds)
        with self._tx(True) as db:
            job = self._job(db,job_id)
            self._owner(job,attempt,worker)
            now=time.time()
            if job["status"] != "leased" or job["lease_until"] <= now:
                raise Conflict("lease is no longer live")
            db.execute("UPDATE jobs SET lease_until=? WHERE id=?", (now+lease_seconds,job_id))
            return self._job(db,job_id)

    def finish(self, job_id, attempt, receipt, worker=None):
        if not isinstance(receipt,dict):
            raise ValueError("receipt must be a JSON object")
        body=encode(receipt)
        with self._tx(True) as db:
            job=self._job(db,job_id)
            self._owner(job,attempt,worker)
            if job["status"] == "finished":
                if encode(job["receipt"]) != body:
                    raise Conflict("completed job already has another receipt")
                return job
            if job["status"] != "leased" or job["lease_until"] <= time.time():
                raise Conflict("expired or unclaimed job")
            db.execute("UPDATE jobs SET status='finished',receipt=?,lease_until=NULL WHERE id=?",
                       (body,job_id))
            return self._job(db,job_id)

    def pending_receipts(self, case_id=None):
        with self._tx() as db:
            rows = db.execute("SELECT id FROM jobs WHERE status='finished' AND consumed_at IS NULL "
                              "AND (? IS NULL OR case_id=?) ORDER BY created,rowid", (case_id,case_id))
            return [self._job(db,row[0]) for row in rows]

    def ingest_file(self, path, case_id=None, job_id=None):
        if job_id and not case_id:
            raise ValueError("case required to attach a job artifact")
        tmp=self.objects / (str(uuid4()) + ".part")
        hasher=sha256()
        size=0
        try:
            with Path(path).open("rb") as source, tmp.open("xb") as out:
                while chunk := source.read(1024*1024):
                    out.write(chunk)
                    hasher.update(chunk)
                    size += len(chunk)
                out.flush()
                os.fsync(out.fileno())
            checksum=hasher.hexdigest()
            dst=self.objects / checksum[:2] / checksum
            dst.parent.mkdir(exist_ok=True)
            with self._tx(True) as db:
                if case_id:
                    self._case(db,case_id)
                if job_id and self._job(db,job_id)["case_id"] != case_id:
                    raise Conflict("artifact job belongs to another case")
                if not dst.exists():
                    os.replace(tmp,dst)
                db.execute("INSERT OR IGNORE INTO artifacts VALUES(?,?,?)", (checksum,size,time.time()))
                if case_id:
                    db.execute("INSERT OR IGNORE INTO artifact_refs VALUES(?,?,?)",
                               (checksum,case_id,job_id or ""))
            return {"id":checksum,"sha256":checksum,"size":size}
        finally:
            tmp.unlink(missing_ok=True)

    def read_path(self, digest, case_id=None):
        if not isinstance(digest,str) or not re.fullmatch("[0-9a-f]{64}",digest):
            raise NotFound("invalid artifact digest")
        with self._tx() as db:
            if case_id is None:
                found=db.execute("SELECT 1 FROM artifacts WHERE digest=?", (digest,)).fetchone()
            else:
                found=db.execute("SELECT 1 FROM artifact_refs WHERE digest=? AND case_id=?",
                                 (digest,case_id)).fetchone()
            path=self.objects / digest[:2] / digest
            if not found or not path.is_file():
                raise NotFound("artifact unavailable")
            return path

    def list_methods(self, brain, project, include_inactive=False):
        """Current project versions plus brain-wide versions. Revoked records shadow prior versions."""
        with self._tx() as db:
            rows=db.execute("SELECT * FROM methods WHERE brain=? AND project IN ('',?) "
                            "ORDER BY project,id,revision DESC", (brain,project or ""))
            records={}
            for row in rows:
                records.setdefault((row["project"],row["id"]),row)
            return [json.loads(r["body"]) for r in records.values()
                    if include_inactive or r["status"] == "active"]

    def method_history(self, brain, project, method_id):
        with self._tx() as db:
            rows=db.execute("SELECT body FROM methods WHERE brain=? AND project=? AND id=? "
                            "ORDER BY revision", (brain,project or "",method_id))
            return [json.loads(r[0]) for r in rows]

    def get_method(self, brain, project, method_id, revision=None):
        with self._tx() as db:
            row=db.execute("SELECT body FROM methods WHERE brain=? AND project=? AND id=? AND "
                           "(? IS NULL OR revision=?) ORDER BY revision DESC LIMIT 1",
                           (brain,project or "",method_id,revision,revision)).fetchone()
            if not row:
                raise NotFound(method_id)
            return json.loads(row[0])

    def register_method(self, brain, project, method):
        with self._tx(True) as db:
            return self._save_method(db,brain,project,method)

    @staticmethod
    def _save_method(db,brain,project,method):
        nonempty(brain,"brain")
        method_id=nonempty(method["id"],"method.id")
        scope={"brain_id":brain}
        if project:
            scope["project_id"]=project
        if method.get("scope",scope) != scope:
            raise Conflict("method scope differs from registration scope")
        latest=db.execute("SELECT max(revision) FROM methods WHERE brain=? AND project=? AND id=?",
                          (brain,project or "",method_id)).fetchone()[0] or 0
        version=method.get("revision",latest+1)
        if type(version) is not int or version<1:
            raise ValueError("method revision must be a positive integer")
        body={**method,"scope":scope,"revision":version,"status":method.get("status","active")}
        if body["status"] not in ("candidate","active","revoked"):
            raise ValueError("invalid method status")
        if version<=latest:
            old=db.execute("SELECT body FROM methods WHERE brain=? AND project=? AND id=? AND revision=?",
                           (brain,project or "",method_id,version)).fetchone()
            if old and old[0]==encode(body):
                return body
            raise Conflict("method revision already exists")
        if version!=latest+1:
            raise Conflict("method revision must follow previous version")
        db.execute("INSERT OR IGNORE INTO brains(id) VALUES(?)", (brain,))
        db.execute("INSERT INTO methods VALUES(?,?,?,?,?,?,?)",
                   (brain,project or "",method_id,version,body["status"],encode(body),time.time()))
        db.execute("UPDATE brains SET revision=revision+1 WHERE id=?", (brain,))
        return body

    def revoke_method(self, brain, project, method_id):
        with self._tx(True) as db:
            row=db.execute("SELECT body FROM methods WHERE brain=? AND project=? AND id=? "
                           "ORDER BY revision DESC LIMIT 1", (brain,project or "",method_id)).fetchone()
            if not row:
                raise NotFound(method_id)
            method=json.loads(row[0])
            if method["status"]=="revoked":
                return method
            return self._save_method(db,brain,project,{**method,"status":"revoked",
                                                       "revision":method["revision"]+1})
