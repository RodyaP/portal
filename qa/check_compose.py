"""Structural YAML checks; does not replace docker compose config or a build."""
from pathlib import Path
import yaml

class Loader(yaml.SafeLoader):
    pass

Loader.add_constructor('!override', lambda loader, node: loader.construct_sequence(node))
root = Path(__file__).resolve().parent.parent
production = yaml.load((root/'compose.yaml').read_text(), Loader=Loader)
local = yaml.load((root/'compose.local.yaml').read_text(), Loader=Loader)
tests = yaml.load((root/'compose.test.yaml').read_text(), Loader=Loader)
assert 'ports' not in production['services']['db']
assert 'ports' not in production['services']['app']
assert production['services']['proxy']['ports'] == ['443:443']
assert local['services']['proxy']['ports'] == ['127.0.0.1:8080:80']
assert production['services']['app']['depends_on']['db']['condition'] == 'service_healthy'
assert tests['services']['db']['tmpfs'] == ['/var/lib/postgresql/data']
assert 'volumes' not in tests['services']['db']
print('Compose YAML structure OK; Docker runtime validation still required.')
