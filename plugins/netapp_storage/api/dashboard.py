"""
Dashboard API

  dashboard/systems   GET — connected NetApp endpoints + PVE hosts with live version/status info
"""

import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed

from flask import jsonify
from nasnap_core.core.db import get_db
from nasnap_core.api.plugins import register_plugin_route

from ..core._helpers import PLUGIN_ID

log = logging.getLogger(__name__)

_TIMEOUT = 5  # seconds per system check


def _check_ontap(db, ep_row):
    from ..core.ontap_client import OntapClient
    r = {'id': ep_row['id'], 'name': ep_row['name'], 'host': ep_row['host'], 'type': 'netapp'}
    try:
        row = db.query_one("SELECT * FROM netapp_endpoints WHERE id=?", (ep_row['id'],))
        ep = dict(row)
        ep['password'] = db._decrypt(ep.pop('password_encrypted', ''))
        client = OntapClient(
            host=ep['host'], username=ep['username'], password=ep['password'],
            ssl_verify=bool(ep.get('ssl_verify', 1)), timeout=_TIMEOUT,
        )

        # Cluster info + node models in parallel
        info = client._get("cluster", params={"fields": "name,version"})

        # Parse version — strip trailing date/time from "NetApp Release 9.15.1P4: Thu Dec 12 ..."
        ver_full = info.get('version', {}).get('full', '')
        m = re.search(r'Release\s+(\S+?)(?:\s*:|$)', ver_full)
        version = m.group(1) if m else ver_full

        # Hardware model(s) from cluster nodes
        models = []
        try:
            nodes_data = client._get("cluster/nodes", params={"fields": "name,model", "max_records": "4"})
            seen = set()
            for n in (nodes_data.get('records') or []):
                mdl = (n.get('model') or '').strip()
                if mdl and mdl not in seen:
                    models.append(mdl)
                    seen.add(mdl)
        except Exception:
            pass

        r['ok'] = True
        r['cluster_name'] = info.get('name', '')
        r['version'] = version
        r['models'] = models
    except Exception as exc:
        r['ok'] = False
        r['error'] = str(exc)[:150]
    return r


def _check_pve(db, host_row):
    import requests as _req
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    h = dict(host_row)
    password = db._decrypt(h.pop('password_encrypted', ''))
    port = int(h.get('port') or 8006)
    username = h.get('username') or 'root@pam'
    ssl_v = bool(h.get('ssl_verify', 0))
    base = f"https://{h['host']}:{port}/api2/json"
    r = {'id': h['id'], 'name': h['name'], 'host': h['host'], 'type': 'pve',
         'version': '', 'cpu_pct': None, 'mem_used': None, 'mem_total': None}
    try:
        lr = _req.post(
            f"{base}/access/ticket",
            data={'username': username, 'password': password},
            verify=ssl_v, timeout=_TIMEOUT,
        )
        if lr.status_code != 200:
            r['ok'] = False
            r['error'] = f"Login failed (HTTP {lr.status_code})"
            return r
        ld = lr.json()['data']
        cookies = {'PVEAuthCookie': ld['ticket']}
        headers = {'CSRFPreventionToken': ld['CSRFPreventionToken']}

        # GET /api2/json/nodes — returns all cluster nodes with CPU, RAM, pveversion
        nr = _req.get(f"{base}/nodes", cookies=cookies, headers=headers,
                      verify=ssl_v, timeout=_TIMEOUT)
        r['ok'] = True
        if nr.status_code == 200:
            nodes = nr.json().get('data', [])
            # Prefer the node whose name matches; fall back to first online node
            node = next((n for n in nodes if n.get('node') == h['name']), None)
            if node is None:
                node = next((n for n in nodes if n.get('status') == 'online'), None)
            if node is None and nodes:
                node = nodes[0]
            if node:
                cpu = node.get('cpu', 0) or 0
                r['cpu_pct'] = round(cpu * 100, 1)
                r['mem_used'] = node.get('mem') or 0
                r['mem_total'] = node.get('maxmem') or 0
                # pveversion: "pve-manager/8.2.4/9352..." → "8.2.4"
                pve_ver_full = node.get('pveversion', '')
                mv = re.search(r'pve-manager/(\d+\.\d+(?:\.\d+)?)', pve_ver_full)
                r['version'] = mv.group(1) if mv else ''
        else:
            r['ok'] = False
            r['error'] = f"Nodes endpoint HTTP {nr.status_code}"
    except Exception as exc:
        r['ok'] = False
        r['error'] = str(exc)[:150]
    return r


def _dashboard_systems():
    db = get_db()

    ep_rows = [dict(r) for r in db.query(
        "SELECT id, name, host FROM netapp_endpoints ORDER BY name")]
    pve_rows = [dict(r) for r in db.query(
        "SELECT id, name, host, port, username, password_encrypted, ssl_verify "
        "FROM netapp_pve_hosts ORDER BY name")]

    netapp_results, pve_results = [], []
    futures = {}

    with ThreadPoolExecutor(max_workers=10) as pool:
        for ep in ep_rows:
            f = pool.submit(_check_ontap, db, ep)
            futures[f] = 'netapp'
        for h in pve_rows:
            f = pool.submit(_check_pve, db, h)
            futures[f] = 'pve'

        for f in as_completed(futures, timeout=_TIMEOUT + 3):
            try:
                result = f.result()
                if futures[f] == 'netapp':
                    netapp_results.append(result)
                else:
                    pve_results.append(result)
            except Exception as exc:
                log.warning(f"[dashboard/systems] check failed: {exc}")

    netapp_results.sort(key=lambda x: x.get('name', ''))
    pve_results.sort(key=lambda x: x.get('name', ''))

    return jsonify({'netapp': netapp_results, 'pve': pve_results})


def register_routes():
    register_plugin_route(PLUGIN_ID, 'dashboard/systems', _dashboard_systems)
