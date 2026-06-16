"""Docker health check — reads /data/server.json to find port + TLS mode."""
import json, os, urllib.request, ssl, sys

data_dir  = os.environ.get('NASNAP_DATA', '/data')
conf_file = os.path.join(data_dir, 'server.json')

conf   = json.load(open(conf_file)) if os.path.exists(conf_file) else {}
port   = conf.get('port', 5000)
tls    = conf.get('tls',  'http')
scheme = 'https' if tls == 'self-signed' else 'http'
url    = f'{scheme}://localhost:{port}/login'

ctx = None
if tls == 'self-signed':
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode    = ssl.CERT_NONE

try:
    urllib.request.urlopen(url, context=ctx, timeout=5)
except Exception as e:
    print(f'Health check failed: {e}', file=sys.stderr)
    sys.exit(1)
