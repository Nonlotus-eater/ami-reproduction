"""Process-shared, fail-closed request reservations and FIFO provider admission.

Only sanitized metadata is persisted. Currency uses integer nanoyuan, rounded
up, not binary floats. Dead/unknown requests retain their full reservation.
"""
from contextlib import contextmanager
from decimal import Decimal, ROUND_CEILING
import json
import math
import os
from pathlib import Path
import sqlite3
import time
import uuid
from .request_lease import POLICY, RequestLease, request_alive

SCALE = 1_000_000_000

class ControlStopped(RuntimeError): pass
class BudgetExhausted(ControlStopped): pass
class DuplicateReservation(ControlStopped): pass


def money(value):
    d = Decimal(str(value))
    if not d.is_finite() or d < 0:
        raise ValueError('Expected finite nonnegative currency')
    return int((d * SCALE).to_integral_value(rounding=ROUND_CEILING))


def slot_available(spec, active, rpm, tpm, tokens):
    return ((spec.get('inflight') is None or active < spec['inflight'])
            and (spec.get('rpm') is None or rpm < spec['rpm'])
            and (spec.get('tpm') is None or tpm + tokens <= spec['tpm']))


class SharedControl:
    def __init__(self, path):
        self.path = str(Path(path).resolve())
        with self.reader() as db:
            value = db.execute("SELECT value FROM settings WHERE key='config'").fetchone()
            if not value: raise ValueError('Shared control is not initialized')
            self.config = json.loads(value[0])
            if self.config.get('liveness_policy') != POLICY:
                raise ValueError('Legacy PID-based ledger: prepare a fresh run with the updated launcher')

    @classmethod
    def create(cls, path, *, cap_cny, providers, rates, identity):
        if cap_cny is not None and money(cap_cny) <= 0: raise ValueError('Positive explicit budget required')
        for spec in providers.values():
            if any(spec.get(k) is not None and (type(spec[k]) is not int or spec[k] <= 0) for k in ('inflight','rpm','tpm')):
                raise ValueError('Use positive inflight/RPM/TPM or null for no limit')
        if set(providers) != {'llm','embedding'}: raise ValueError('Both provider groups required')
        if set(rates) != {'input','output','embedding'} or any(not math.isfinite(float(x)) or float(x)<=0 for x in rates.values()):
            raise ValueError('Explicit positive rates required')
        config = {'cap_units':None if cap_cny is None else money(cap_cny),'providers':providers,'rates':rates,'identity':identity,'window_seconds':60,'liveness_policy':POLICY}
        p = Path(path);p.parent.mkdir(parents=True,exist_ok=True)
        with sqlite3.connect(p,timeout=30) as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.executescript('''
            CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS calls(id TEXT PRIMARY KEY,pid INTEGER,kind TEXT,phase TEXT,scope TEXT,state TEXT,reserved INTEGER,charged INTEGER,input_bound INTEGER,output_bound INTEGER,started REAL,ended REAL,status INTEGER,actual_input INTEGER,actual_output INTEGER);
            CREATE TABLE IF NOT EXISTS tickets(id INTEGER PRIMARY KEY AUTOINCREMENT,call_id TEXT UNIQUE,provider TEXT,pid INTEGER,state TEXT,tokens INTEGER,queued REAL,granted REAL,ended REAL,wait_seconds REAL);
            CREATE INDEX IF NOT EXISTS admission ON tickets(provider,state,id);
            CREATE INDEX IF NOT EXISTS rate_window ON tickets(provider,granted);
            ''')
            db.execute('BEGIN IMMEDIATE')
            old = db.execute("SELECT value FROM settings WHERE key='config'").fetchone()
            encoded = json.dumps(config,sort_keys=True)
            if old and old[0] != encoded: raise ValueError('Existing ledger identity/budget/limits changed')
            db.execute("INSERT OR IGNORE INTO settings VALUES('config',?)",(encoded,))
        return cls(p)

    @contextmanager
    def reader(self):
        db=sqlite3.connect(Path(self.path).as_uri()+'?mode=ro',uri=True,timeout=30)
        db.row_factory=sqlite3.Row
        try:yield db
        finally:db.close()

    @contextmanager
    def transaction(self):
        db=sqlite3.connect(self.path,timeout=30)
        db.row_factory=sqlite3.Row
        try:
            db.execute('BEGIN IMMEDIATE')
            yield db
            db.commit()
        except BaseException:
            db.rollback();raise
        finally: db.close()

    def _check(self,db):
        row=db.execute("SELECT value FROM settings WHERE key='stop'").fetchone()
        if row: raise ControlStopped(row[0])

    def check(self):
        with self.reader() as db:self._check(db)

    def stop(self,reason):
        # Caller supplies fixed codes, never exception/provider text.
        if reason not in {'budget_exhausted','provider_failure','user_stop','identity_changed','unknown_charge','usage_bound_exceeded','fatal_queue_failure'}:
            reason='provider_failure'
        with self.transaction() as db:
            db.execute("INSERT OR IGNORE INTO settings VALUES('stop',?)",(reason,))

    def cost(self,kind,input_tokens,output_tokens=0):
        if any(type(v) is not int or v<0 for v in (input_tokens,output_tokens)):raise ValueError('Invalid token count')
        r=self.config['rates']
        amount=Decimal(str(r['embedding'] if kind=='embedding' else r['input']))*input_tokens
        if kind!='embedding':amount+=Decimal(str(r['output']))*output_tokens
        return money(amount/Decimal(1_000_000))

    def reserve(self,kind,phase,scope,input_bound,output_bound,*,request_id=None):
        ident=request_id or uuid.uuid4().hex
        cost=self.cost(kind,input_bound,output_bound)
        rejected=False
        with self.transaction() as db:
            self._check(db)
            if db.execute('SELECT 1 FROM calls WHERE id=?',(ident,)).fetchone():raise DuplicateReservation('Reservation is never replayed')
            used=db.execute('SELECT COALESCE(SUM(charged),0) FROM calls').fetchone()[0]
            if self.config['cap_units'] is not None and used+cost>self.config['cap_units']:
                db.execute("INSERT OR IGNORE INTO settings VALUES('stop','budget_exhausted')")
                rejected=True
            else:
                db.execute('INSERT INTO calls(id,pid,kind,phase,scope,state,reserved,charged,input_bound,output_bound,started,ended,status) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    (ident,os.getpid(),kind,phase,scope,'reserved',cost,cost,input_bound,output_bound,time.time(),None,None))
        if rejected:raise BudgetExhausted('Shared round budget exhausted before transport')
        return ident

    def settle(self,ident,input_tokens,output_tokens=0,status=200):
        with self.transaction() as db:
            row=db.execute('SELECT * FROM calls WHERE id=?',(ident,)).fetchone()
            if not row or row['state']!='reserved':raise DuplicateReservation('Reservation already resolved')
            actual=self.cost(row['kind'],input_tokens,output_tokens)
            if input_tokens>row['input_bound'] or output_tokens>row['output_bound'] or actual>row['reserved']:
                # A violated billing bound invalidates the guarantee. Preserve debt
                # rather than hiding it, and permanently close the round.
                db.execute("UPDATE calls SET state='unknown',charged=MAX(charged,?),ended=?,status=? WHERE id=?",(actual,time.time(),status,ident))
                db.execute("INSERT OR IGNORE INTO settings VALUES('stop','usage_bound_exceeded')")
                over=True
            else:
                db.execute("UPDATE calls SET state='settled',charged=?,ended=?,status=?,actual_input=?,actual_output=? WHERE id=?",(actual,time.time(),status,input_tokens,output_tokens,ident))
                over=False
        if over:raise ControlStopped('Provider exceeded conservative reservation')

    def unknown(self,ident,status=None):
        with self.transaction() as db:
            db.execute("UPDATE calls SET state='unknown',ended=?,status=? WHERE id=? AND state='reserved'",(time.time(),status,ident))

    def cancel_unsent(self,ident):
        with self.transaction() as db:
            if db.execute("SELECT 1 FROM tickets WHERE call_id=? AND granted IS NOT NULL",(ident,)).fetchone():
                raise ValueError('Cannot cancel a possibly billed request')
            db.execute("UPDATE calls SET state='cancelled_unsent',charged=0,ended=? WHERE id=? AND state='reserved'",(time.time(),ident))
            db.execute("UPDATE tickets SET state='cancelled',ended=? WHERE call_id=? AND state='waiting'",(time.time(),ident))

    def request_alive(self, ident):
        return request_alive(self.path, ident)

    def reap_abandoned(self, db):
        """Caller holds the SQLite write transaction; unknown owners stay live."""
        count = 0
        for row in db.execute("SELECT call_id,state FROM tickets WHERE state IN ('active','waiting')").fetchall():
            if self.request_alive(row['call_id']) is not False:
                continue
            db.execute("UPDATE tickets SET state='abandoned',ended=? WHERE call_id=?",(time.time(),row['call_id']))
            if row['state']=='waiting':
                db.execute("UPDATE calls SET state='cancelled_unsent',charged=0,ended=? WHERE id=? AND state='reserved'",(time.time(),row['call_id']))
            else:
                db.execute("UPDATE calls SET state='unknown',ended=? WHERE id=? AND state='reserved'",(time.time(),row['call_id']))
            count += 1
        return count

    @contextmanager
    def admit(self,ident,provider,tokens,*,timeout=240):
        spec=self.config['providers'][provider]
        if spec.get('tpm') is not None and tokens>spec['tpm']:
            self.cancel_unsent(ident)
            self.stop('provider_failure')
            raise ControlStopped('One request exceeds configured TPM; never truncate it')
        # Ownership exists before publishing a ticket, and lasts through final
        # ticket accounting. No observer needs to interpret another namespace's PID.
        with RequestLease(self.path, ident):
            with self._admit_owned(ident, provider, tokens, timeout=timeout):
                yield

    @contextmanager
    def _admit_owned(self,ident,provider,tokens,*,timeout):
        start=time.monotonic();granted=False;last_reap=0
        with self.transaction() as db:
            self._check(db)
            ticket=db.execute('INSERT INTO tickets(call_id,provider,pid,state,tokens,queued) VALUES(?,?,?,?,?,?)',
                       (ident,provider,os.getpid(),'waiting',tokens,time.time())).lastrowid
        polling=sqlite3.connect(Path(self.path).as_uri()+'?mode=ro',uri=True,timeout=30)
        polling.row_factory=sqlite3.Row
        try:
            while True:
                # Polling does not acquire SQLite's single writer lock. These
                # observations are optimistic; grant is rechecked atomically.
                self._check(polling)
                first=polling.execute("SELECT id,call_id FROM tickets WHERE provider=? AND state='waiting' ORDER BY id LIMIT 1",(provider,)).fetchone()
                head=first is not None and ticket==first['id'];dead_head=False
                available=False
                if head:
                    cfg=json.loads(polling.execute("SELECT value FROM settings WHERE key='config'").fetchone()[0]);spec=cfg['providers'][provider]
                    active=polling.execute("SELECT COUNT(*) FROM tickets WHERE provider=? AND state='active'",(provider,)).fetchone()[0]
                    rpm,tpm=polling.execute('SELECT COUNT(*),COALESCE(SUM(tokens),0) FROM tickets WHERE provider=? AND granted>?',(provider,time.time()-cfg['window_seconds'])).fetchone()
                    available=slot_available(spec,active,rpm,tpm,tokens)
                if first is not None:
                    dead_head=self.request_alive(first['call_id']) is False
                reap=dead_head or (head and time.monotonic()-last_reap>=1)
                if available or reap:
                    with self.transaction() as db:
                        self._check(db)
                        if reap:
                            # A dead queued head must not starve live waiters.
                            # Unsent work can cancel; possibly billed work stays unknown.
                            self.reap_abandoned(db)
                            last_reap=time.monotonic()
                        cfg=json.loads(db.execute("SELECT value FROM settings WHERE key='config'").fetchone()[0]);spec=cfg['providers'][provider]
                        first_id=db.execute("SELECT MIN(id) FROM tickets WHERE provider=? AND state='waiting'",(provider,)).fetchone()[0]
                        active=db.execute("SELECT COUNT(*) FROM tickets WHERE provider=? AND state='active'",(provider,)).fetchone()[0]
                        rpm,tpm=db.execute('SELECT COUNT(*),COALESCE(SUM(tokens),0) FROM tickets WHERE provider=? AND granted>?',(provider,time.time()-cfg['window_seconds'])).fetchone()
                        if ticket==first_id and slot_available(spec,active,rpm,tpm,tokens):
                            db.execute("UPDATE tickets SET state='active',granted=?,wait_seconds=? WHERE call_id=?",(time.time(),time.monotonic()-start,ident));granted=True
                if granted:break
                if time.monotonic()-start>timeout:raise ControlStopped('Provider admission timeout')
                time.sleep(.02)
            yield
        finally:
            polling.close()
            if granted:
                with self.transaction() as db:db.execute("UPDATE tickets SET state='finished',ended=? WHERE call_id=?",(time.time(),ident))
            else:self.cancel_unsent(ident)

    def snapshot(self):
        with self.transaction() as db:
            calls=[dict(x) for x in db.execute('SELECT * FROM calls ORDER BY started,id')]
            tickets=[dict(x) for x in db.execute('SELECT * FROM tickets ORDER BY id')]
            stopped=db.execute("SELECT value FROM settings WHERE key='stop'").fetchone()
        peaks={}
        for provider in self.config['providers']:
            events=[]
            for t in tickets:
                if t['provider']==provider and t['granted'] is not None:
                    events.append((t['granted'],1));events.append((t['ended'] or time.time(),-1))
            active=peak=0
            for _,delta in sorted(events,key=lambda x:(x[0],x[1])):active+=delta;peak=max(peak,active)
            peaks[provider]=peak
        return {'cap_cny':None if self.config['cap_units'] is None else self.config['cap_units']/SCALE,'committed_and_unknown_cny':sum(x['charged'] for x in calls)/SCALE,
                'stop_reason':stopped[0] if stopped else None,'calls':calls,'provider_tickets':tickets,
                'provider_peak_inflight':peaks,'provider_wait_seconds':sum(t['wait_seconds'] or 0 for t in tickets),
                'unknown_requests':[x['id'] for x in calls if x['state'] in ('unknown','reserved')]}
