# NaSnap — Open Items

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
