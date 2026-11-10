"""Freeze installed production dependencies after validation in a clean venv."""
from importlib.metadata import distribution
from pathlib import Path
from packaging.requirements import Requirement

root = Path(__file__).resolve().parent.parent
pending = [Requirement(line) for line in (root / 'requirements.txt').read_text().splitlines() if line.strip()]
pending.append(Requirement('tzdata'))
found = {}
seen = set()
while pending:
    req = pending.pop()
    key = (req.name.lower(), tuple(sorted(req.extras)))
    if key in seen:
        continue
    seen.add(key)
    dist = distribution(req.name)
    found[dist.metadata['Name']] = dist.version
    for entry in dist.requires or []:
        dependency = Requirement(entry)
        if dependency.marker is None or any(dependency.marker.evaluate({'extra': extra}) for extra in ['', *req.extras]):
            pending.append(dependency)
(root / 'requirements.lock.txt').write_text(''.join(f'{name}=={version}\n' for name, version in sorted(found.items(), key=lambda item:item[0].lower())), encoding='utf-8')
