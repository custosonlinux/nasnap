# NaSnap — Open Items

---

## Skalierungs-Architektur: Aufsplittung in Web / Scheduler / Worker

**Prio: Hoch (für Enterprise-Umgebungen), aber Umsetzung zurückgestellt — nur Design, noch keine Freigabe zur Implementierung**

Ziel-Umgebungen: 10–20 PVE-Hosts, ~1000 VMs, große Storage-Systeme. Die aktuelle
Architektur (ein Gunicorn-Prozess, `WORKERS=1`, SQLite, In-Memory-Jobregistry)
wurde für kleine/mittlere Installationen gebaut und stößt bei dieser Größenordnung
an mehrere unabhängige Grenzen — nicht primär Rohdurchsatz, sondern Blockierung
und fehlende horizontale Skalierung.

### Problem (konkret an bestehendem Code festgemacht)

1. **`WORKERS=1` ist erzwungen**, weil der Scheduler (`_scheduler_loop` in
   `api/schedules.py`) als In-Process-Daemon-Thread in `create_app()` läuft.
   Mehrere Gunicorn-Worker würden jeden Zeitplan mehrfach feuern.
2. **Lange SSH/PVE/ONTAP-Calls blockieren den einzigen Worker.** Das Codebase
   umgeht das bereits mehrfach mit dem "Background-Refresh-Cache"-Muster
   (`_vm_cache` in `api/snapshots.py`, `_cap_cache` in `api/provisioning.py`) —
   ein Symptom dafür, dass die Architektur eigentlich einen echten Worker-Pool
   bräuchte, nicht mehr Workarounds im Web-Prozess.
3. **`_job_registry.py` ist ein In-Memory-Dict** (Thread-Objekt + Cancel-Event
   pro `job_id`). Funktioniert nur, solange Job-Start und Job-Ausführung im
   selben Prozess passieren. Sobald Web und Worker getrennte Container sind,
   funktioniert `jobs/cancel` nicht mehr ohne Weiteres.
4. **SQLite als Datei** ist für einen einzelnen Prozess in Ordnung, aber riskant
   sobald mehrere Container (Web + Scheduler + mehrere Worker) gleichzeitig
   schreibend zugreifen — insbesondere über einen Bind-Mount/NFS-Volume.
5. **PVE-Polling ist pro Web-Prozess im RAM gecacht** (`_vm_cache`,
   `_STORAGE_UNIFIED_CACHE_KEY` clientseitig) — bei mehreren Web-Replicas wäre
   das inkonsistent (jeder Replica pollt unabhängig, kein geteilter Zustand).
6. **Parallele Operationen** (Bulk Migrate, Multi-Datastore-Schedules) laufen
   heute als `ThreadPoolExecutor` innerhalb eines einzelnen Prozesses — skaliert
   nicht über die CPU/Netzwerk-Kapazität eines einzelnen Containers hinaus.

### Zielarchitektur

```
┌─────────────┐      ┌──────────────┐      ┌──────────────────┐
│  nasnap-web  │─────▶│  Job-Queue    │◀────│ nasnap-scheduler  │
│ (N Replicas) │      │ (Redis+RQ o.  │      │   (1 Replica,     │
│  Gunicorn,   │      │  DB-Tabelle)  │      │   Singleton)      │
│  kein Sched. │      └───────┬──────┘      └──────────────────┘
└──────┬───────┘              │
       │                      ▼
       │             ┌──────────────────┐
       │             │  nasnap-worker    │
       │             │  (M Replicas)     │
       │             │  SSH/PVE/ONTAP    │
       │             └─────────┬────────┘
       │                       │
       ▼                       ▼
┌─────────────────────────────────────┐
│         nasnap-db (Postgres)         │
│  netapp_jobs, netapp_snapshots, …     │
└──────────────────────────────────────┘
```

- **`nasnap-web`**: Flask/Gunicorn, mehrere Worker möglich, da kein Scheduler
  und keine langlaufenden Calls mehr inline laufen. Legt Jobs an (DB-Insert +
  Queue-Publish) und liest Status/Progress aus der DB — identisch zum
  heutigen Polling-Pattern (`jobs/status?job_id=`), das bleibt unverändert.
- **`nasnap-scheduler`**: Singleton (genau 1 Replica) — exakt die heutige
  `WORKERS=1`-Beschränkung, nur isoliert auf eine kleine, austauschbare
  Komponente statt auf den gesamten Webserver. Feuert Zeitpläne, legt Jobs in
  die Queue, führt sie nicht mehr selbst aus.
- **`nasnap-worker`**: N Replicas, konsumieren Jobs aus der Queue
  (Snapshot/Restore/Clone/Bulk-Migrate/SFR). Jeder Worker öffnet seine eigene
  PVE-/ONTAP-Session — das Pattern existiert bereits (`pve_for_mapping`,
  `build_pve_client`), muss nur aus dem Web-Prozess in den Worker-Prozess
  wandern.
- **Queue**: Redis+RQ (einfach, bewährt) oder minimal-invasiv eine
  DB-Tabelle als Queue (`netapp_job_queue`, Worker pollen `SELECT ... FOR
  UPDATE SKIP LOCKED` — geht erst mit Postgres, nicht mit SQLite).
- **DB**: Postgres statt SQLite — nicht wegen Durchsatz, sondern weil mehrere
  Prozesse/Container jetzt gleichzeitig schreiben.

### Phasenplan (jede Phase einzeln lieferbar, Umsetzung erst nach Freigabe)

**Phase 1 — Scheduler aus dem Web-Prozess lösen** (kleinstes Risiko, größter
sofortiger Gewinn: `WORKERS>1` wird für den Web-Tier möglich)
- Scheduler-Loop aus `create_app()` in einen eigenen Einstiegspunkt
  (`scheduler_main.py` o.ä.) extrahieren, als separates Deployment/Container
  mit fest 1 Replica.
- Web-Prozess ruft `start_scheduler()` nicht mehr selbst auf.
- **Aufwand: 2–3 Tage**

**Phase 2 — Jobqueue + Worker-Container**
- Neue Queue-Anbindung (Redis+RQ empfohlen — geringster Umbau, gute
  Python-Integration).
- Alle `start_*_job()`-Funktionen (`snapshot_engine.py`, `clone_engine.py`,
  `restore_engine.py`, `migrate_engine.py`, …) von
  `threading.Thread(daemon=True).start()` auf `queue.enqueue(...)` umstellen —
  die eigentlichen `_run_*`-Funktionen bleiben inhaltlich fast unverändert,
  nur der Start-Mechanismus ändert sich.
- `_job_registry.py` (Cancel-Events) auf einen DB-Flag (`netapp_jobs.cancel_requested`)
  oder Redis umstellen, da Web- und Worker-Prozess getrennt sind.
- **Aufwand: 5–8 Tage** (inkl. Migration aller bestehenden Engines)

**Phase 3 — SQLite → Postgres**
- Schema-Migration (`schema.sql` ist bereits reines Standard-SQL, sollte
  weitgehend kompatibel sein — SQLite-spezifische Syntax wie `INSERT OR
  REPLACE` muss auf `INSERT ... ON CONFLICT DO UPDATE` vereinheitlicht werden,
  Grossteil des Codes nutzt das bereits).
- DB-Zugriffsschicht (`db.py`) auf einen Postgres-Treiber umstellen, Thread-
  Local-Connection-Pattern durch echten Connection-Pool (z.B. `psycopg` Pool)
  ersetzen.
- Bestehendes Backup/Restore-Feature (JSON-Export) muss weiter funktionieren.
- **Aufwand: 5–8 Tage** (inkl. Testing der Backup/Restore-Kompatibilität)

**Phase 4 — Zentrales PVE-Polling** (optional, nach Bedarf)
- `_vm_cache`/`_cap_cache` aus dem Web-Prozess-RAM in eine DB-Tabelle oder
  Redis verlagern, damit mehrere Web-Replicas denselben Cache-Stand sehen.
- **Aufwand: 2–3 Tage**

### Gesamtaufwand

**14–22 Tage** verteilt auf 4 unabhängig lieferbare Phasen. Phase 1 kann isoliert
umgesetzt und getestet werden, ohne dass Phase 2–4 sofort folgen müssen.

### Status

Nur Design — **Umsetzung wartet auf explizite Freigabe.** Nicht mit der
Implementierung beginnen, bevor das nicht ausdrücklich angefordert wird.

---

## VM-Datenbank Garbage Collection

**Prio: Mittel**

VMs, die gelöscht oder migriert wurden, bleiben dauerhaft in der RC-Ansicht sichtbar, obwohl kein Restore mehr möglich ist. Das gilt auch für Datastores, deren Mapping gelöscht wurde. Die Bereinigung muss automatisch erfolgen — kein Admin kann sich merken, welche Einträge veraltet sind.

### Problem

Die `netapp_snapshots`-Tabelle enthält `vmids_json` pro Snapshot. Wenn alle Snapshots einer VM gelöscht sind (durch Retention), bleibt die VM trotzdem in der Restore-Ansicht (weil frühere Einträge in der DB verbleiben). Dasselbe gilt für Datastores, deren `netapp_volume_mapping` gelöscht wurde — der `ON DELETE CASCADE` löscht zwar die Snapshots, aber die RC-Ansicht aggregiert VMs über alle bekannten `vmids_json`-Einträge.

### Bereinigungsregeln

1. **Snapshot ohne zugehöriges Mapping** → Mapping wurde gelöscht, CASCADE löscht Snapshots — kein Problem.
2. **VM ohne Snapshots mehr** → VM taucht in `vmids_json` keines aktiven Snapshots mehr auf → VM-Eintrag muss aus der Aggregation verschwinden. Aktuell kein expliziter VM-Eintrag in der DB (VMs werden dynamisch aus `vmids_json` aggregiert) → GC muss alten `vmids_json`-Einträge in *noch vorhandenen* Snapshots prüfen.
3. **Snapshot in DB, aber nicht mehr auf ONTAP** → Snapshot wurde direkt auf ONTAP gelöscht ohne NaSnap → Eintrag in DB ist Leiche.

### Geplanter Ansatz

- **Automatischer Abgleich** beim Snapshot-Scan (Index-Import):  
  Der Index in jedem Snapshot enthält die Snapshot-History. Beim Startup-Scan oder manuellen Index-Scan wird geprüft, welche Snapshots tatsächlich noch auf ONTAP existieren. Einträge ohne ONTAP-Gegenstück werden als `status='orphaned'` markiert oder gelöscht.
- **Hintergrund-GC-Thread** (täglich, z.B. 03:00 Uhr):  
  Vergleicht `netapp_snapshots` mit ONTAP-Snapshot-Liste via REST API. Snapshots, die nicht mehr auf ONTAP existieren, werden entfernt. Danach: alle VMIDs, die in keinem verbleibenden `done`-Snapshot mehr vorkommen, sind automatisch bereinigt.
- **Mapping-Prüfung**:  
  GC prüft auch, ob das ONTAP-Volume für jedes Mapping noch existiert. Fehlt das Volume, werden alle zugehörigen Snapshots als orphaned markiert.

### Was wiederverwendet werden kann

- ONTAP-Client `list_snapshots(volume_uuid)` — bereits vorhanden
- Index-Scan-Logik aus `_ds_scan_creds()` + `_reconcile_index_into_db()`
- DB-Abfrage `DELETE FROM netapp_snapshots WHERE ...` (inkl. ON DELETE CASCADE auf Jobs/Manifeste)

### Was neu gebaut werden muss

- GC-Thread mit konfigurierbarem Intervall (Default: täglich)
- ONTAP-Snapshot-Abgleich je Mapping
- UI-Hinweis in Settings: "Letzte GC-Ausführung / N orphaned entries entfernt"
- Optional: manueller "Run GC now"-Button in Settings

**Aufwand: 2–3 Tage**
- 1 Tag: ONTAP-Abgleich-Logik + DB-Cleanup
- 0,5 Tag: GC-Thread + Scheduling
- 0,5 Tag: Settings-UI (Status-Anzeige + manueller Trigger)
- 0,5 Tag: Tests + Edge Cases (offline ONTAP, teilweise Snapshots)

---

## Active Directory / LDAP Authentication

**Prio: Hoch**

Benutzer können sich mit ihrem AD/LDAP-Konto an der WebGUI anmelden. Lokale Accounts bleiben als Fallback erhalten.

### Scope

- Settings-Seite: LDAP-Server, Port, SSL/TLS, Base DN, Bind-User, Bind-Passwort, User-Suchfilter, Gruppe→Rolle-Mapping, "Test Connection"-Button
- Login-Flow: AD-Bind zuerst, Fallback auf lokalen Account wenn AD nicht erreichbar oder Benutzer lokal bekannt
- Gruppen-zu-Rolle-Mapping: eine AD-Gruppe → Admin, eine → Viewer (konfigurierbar)
- Lokale Notfall-Accounts bleiben immer aktiv (kein Aussperren bei AD-Ausfall)
- Session-Handling bleibt unverändert (HMAC-Token)
- Bibliothek: `ldap3` (pure Python, keine C-Abhängigkeiten)

### Technische Einschätzung

- `nasnap_core/utils/auth.py` erweitern: LDAP-Bind als alternativer Auth-Pfad
- Neue Tabelle `nasnap_ldap_config` in der DB (ein Eintrag, verschlüsseltes Bind-Passwort)
- Settings-Tab "Authentication" mit Formular + Test-Button
- Login-Endpoint: versucht erst lokalen Match, dann LDAP-Bind wenn konfiguriert

**Aufwand: 3–4 Tage**
- 1 Tag: LDAP-Bind-Logik + DB-Schema + Settings-API
- 1 Tag: Settings-UI (Formular, Test-Button, Verbindungsstatus)
- 1 Tag: Login-Flow-Integration, Fehlerbehandlung, Fallback-Logik
- 0,5 Tag: Tests + Edge Cases (AD-Ausfall, falsches Passwort, Gruppen-Mapping)

---

## Single File Restore für SAN (iSCSI / NVMe-oF)

**Prio: Mittel**

SFR auf Block-Storage: Datei aus einem ONTAP-Snapshot in eine laufende VM kopieren, ohne Vollrestore.

### Problem

Auf SAN gibt es kein direkt zugängliches Dateisystem auf dem PVE-Host — nur ein Block-Device (LUN). Der Workflow braucht deshalb einen temporären ONTAP-Clone, bevor das Mounting möglich ist.

### Geplanter Ablauf

```
ONTAP Snapshot
  └─ FlexClone (read-only, temp)
       └─ LUN-Mapping → PVE Host
            └─ kpartx / multipath → Block-Device
                 └─ qemu-nbd mount (wie NFS SFR)
                      └─ Datei-Browser + QGA-Transfer (identisch zu NFS SFR)
  └─ Cleanup: umount → LUN unmap → FlexClone löschen
```

### Was wiederverwendet werden kann

- Kompletter Datei-Browser (links) — identisch zu NFS
- Kompletter QGA-Transfer-Code — identisch zu NFS
- ONTAP-FlexClone + LUN-Mapping aus `restore_engine.py` — bereits vorhanden

### Was neu gebaut werden muss

- SFR-Session-Typ "san" mit FlexClone-Lifecycle-Management
- Robustes Cleanup bei Session-Timeout oder Fehler (FlexClone-Leichen vermeiden)
- `file_restore.py`: neuer Mount-Pfad für SAN-Sessions
- UI: minimale Anpassung (SFR-Button für SAN-VMs freischalten)

### Risiken

- Cleanup-Zuverlässigkeit: ein hängengebliebener FlexClone blockiert Speicher auf ONTAP
- kpartx/multipath-Mapping kann auf manchen PVE-Hosts Probleme machen
- Timeout-Handling komplexer als bei NFS (mehr Ressourcen im Spiel)

**Aufwand: 4–5 Tage**
- 1 Tag: SAN-Mount-Sequenz (FlexClone → LUN map → kpartx → qemu-nbd)
- 1 Tag: Session-Lifecycle + Cleanup-Daemon für SAN-Sessions
- 1 Tag: Integration in `file_restore.py` + API-Anpassungen
- 0,5 Tag: UI (SFR-Button für SAN-VMs aktivieren)
- 1–1,5 Tag: Tests + Fehlerbehandlung + Cleanup-Robustheit

---

## DR Failover (niedrige Prio, zurückgestellt)

Vollständiges Failover-Szenario mit SnapMirror-Secondary als Produktivsystem.
Implementation vorhanden, aber noch nicht ausreichend getestet.
Wird zurückgestellt bis Core-Features stabil sind.

Enthält:
- Planned Failover (sauber, mit Reverse-Resync)
- Emergency Failover (dirty, SnapMirror gebrochen)
- DR Test via FlexClone (ohne Produktionsunterbrechung)
- DR Failback

**Aufwand: 5–8 Tage** (Implementierung vorhanden, hauptsächlich Testing + Edge Cases)

---

## Erledigte Features (zur Referenz)

| Feature | Version |
|---|---|
| Datastore Index / Self-Describing Snapshots | v1.2.0 |
| SAN Datastore Index (snapmanifest LV) | v1.3.0 |
| Multi-Datastore Protection Plans | v1.4.0 |
| Single File Restore (NFS, Linux + Windows VMs) | v1.5.0 |
| Snapshot Timeline — Bucket-Clustering + Dashboard-Farben | v1.5.0 |
