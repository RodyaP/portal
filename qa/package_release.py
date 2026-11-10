"""Create a source release excluding local runtimes, credentials and backups."""
from pathlib import Path
import zipfile

root = Path(__file__).resolve().parent.parent
destination = root.parent / 'Peregovornii_UGMK_Docker.zip'
excluded = {'.venv','.qa-runtime','.git','.pytest_cache','__pycache__','node_modules','certs'}
with zipfile.ZipFile(destination, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
    for path in sorted(root.rglob('*')):
        relative = path.relative_to(root)
        if not path.is_file() or any(part in excluded for part in relative.parts):
            continue
        if path.name == '.env' or path.suffix in {'.dump','.pem','.key','.keytab','.zip','.pyc'}:
            continue
        archive.write(path, Path('Peregovornii_UGMK') / relative)
with zipfile.ZipFile(destination) as archive:
    names = archive.namelist()
    assert all(not any(part in excluded or part == '.env' for part in Path(name).parts) for name in names)
    for required in ('compose.yaml','Dockerfile','dist/app.js','dist/access.js','dist/config.js','dist/demo.js','DEPLOY_DOCKER.md'):
        assert 'Peregovornii_UGMK/' + required in names
print(destination)
print(f'Verified {len(names)} files; local runtimes and credentials excluded.')
