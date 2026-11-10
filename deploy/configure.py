"""Create unique secrets without overwriting an existing installation."""
import base64
import os
from pathlib import Path
import secrets
import ssl

root = Path(__file__).resolve().parent.parent
env = root / '.env'
with env.open('x', encoding='utf-8') as f:
    f.write('PUBLIC_ORIGIN=https://peregovornii.ugmkstroy.ru\n')
    f.write('POSTGRES_PASSWORD=' + secrets.token_hex(32) + '\n')
    f.write('SETTINGS_KEY=' + base64.urlsafe_b64encode(secrets.token_bytes(32)).decode() + '\n')
    f.write('BOOTSTRAP_TOKEN=' + secrets.token_urlsafe(48) + '\n')
os.chmod(env, 0o600)
certs = root / 'deploy/certs'
certs.mkdir(exist_ok=True)
# Public roots allow local startup; replace with the corporate CA chain for AD.
ca = certs / 'ad-ca.pem'
if not ca.exists():
    context = ssl.create_default_context()
    ca.write_text(''.join(ssl.DER_cert_to_PEM_cert(cert) for cert in context.get_ca_certs(binary_form=True)), encoding='ascii')
print('Created .env and deploy/certs/ad-ca.pem. Keep .env secret and back it up with the database.')
